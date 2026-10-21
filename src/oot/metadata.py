from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from oot.git.repo import Repo

METADATA_FILE = "metadata.json"
# Base copies live here inside the patches repo, mirroring kernel paths
BASE_DIR = Path(".oot/base")


def metadata_path(patches_dir: Path) -> Path:
    return patches_dir / METADATA_FILE


def patch_files(patches_dir: Path) -> set[str]:
    """Paths (relative) of the files in the patches repo that are patches.

    Tracked and untracked files present on disk, honouring .gitignore,
    minus metadata.json and base copies.
    """
    r = Repo(patches_dir, None).git(
        "ls-files", "-z", "--cached", "--others", "--exclude-standard",
        text=False, check=True,
    )
    return {
        rel
        for rel in r.stdout.decode().split("\0")
        if rel
        and rel != METADATA_FILE
        and not rel.startswith(f"{BASE_DIR.parts[0]}/")
        # --cached still lists tracked files deleted from the work tree
        and (patches_dir / rel).is_file()
    }


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
            raise FileNotFoundError(f"metadata file not found: {path}")
        return cls.model_validate_json(path.read_text())

    def save(self, path: Path):
        path.write_text(self.model_dump_json(indent=2) + "\n")
