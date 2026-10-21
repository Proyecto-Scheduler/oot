from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError

from oot.errors import PatchError
from oot.git.repo import Repo

METADATA_FILE = "metadata.json"
# gitignore-syntax list of versioned files in the patches repo that are not patches
OOTIGNORE_FILE = ".ootignore"
# Base copies live here inside the patches repo, mirroring kernel paths
BASE_DIR = Path(".oot/base")


def metadata_path(patches_dir: Path) -> Path:
    return patches_dir / METADATA_FILE


def patch_files(patches_dir: Path) -> set[str]:
    """Paths (relative) of the files in the patches repo that are patches.

    Tracked and untracked files present on disk, honouring .gitignore,
    minus metadata.json, base copies, .ootignore and what it matches.
    """
    repo = Repo(patches_dir, None)
    ls = ("ls-files", "-z", "--cached", "--others")

    def run(*args) -> set[str]:
        r = repo.git(*ls, *args, text=False, check=True)
        return {rel for rel in r.stdout.decode().split("\0") if rel}

    files = run("--exclude-standard")

    ootignore = patches_dir / OOTIGNORE_FILE
    if ootignore.is_file():
        # -i with only .ootignore as source: exactly the files it matches
        files -= run("-i", f"--exclude-from={ootignore}")

    return {
        rel
        for rel in files
        if rel not in (METADATA_FILE, OOTIGNORE_FILE)
        and not rel.startswith(f"{BASE_DIR.parts[0]}/")
        # --cached still lists tracked files deleted from the work tree
        and (patches_dir / rel).is_file()
    }


def ignored_paths(patches_dir: Path, paths: list[str]) -> set[str]:
    """Which of paths .ootignore or the patches repo's .gitignore would hide."""
    if not paths:
        return set()
    ootignore = patches_dir / OOTIGNORE_FILE
    r = Repo(patches_dir, None).git(
        "-c", f"core.excludesFile={ootignore}",
        "check-ignore", "--no-index", "-z", "--stdin",
        input="\0".join(paths) + "\0",
    )
    # exit 1 means nothing matched
    if r.returncode not in (0, 1):
        raise PatchError(f"git check-ignore failed:\n{r.stderr.strip()}")
    return {p for p in r.stdout.split("\0") if p}


class FileMetadata(BaseModel):
    model_config = ConfigDict(strict=True)

    path: str
    base_blob: str | None = None
    status: Literal["new", "modified", "deleted"]


class Metadata(BaseModel):
    model_config = ConfigDict(strict=True)

    # "base_blob" is the pre-rename name of this field; still accepted on read
    kernel_commit: str = Field(
        validation_alias=AliasChoices("kernel_commit", "base_blob")
    )
    files: list[FileMetadata]

    @classmethod
    def load(cls, path: Path) -> "Metadata":
        if not path.is_file():
            raise PatchError(f"metadata file not found: {path}")
        try:
            return cls.model_validate_json(path.read_text())
        except ValidationError as e:
            raise PatchError(f"Invalid metadata {path}:\n{e}")

    def save(self, path: Path):
        path.write_text(self.model_dump_json(indent=2) + "\n")
