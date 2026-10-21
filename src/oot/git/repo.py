import logging
import shutil
import subprocess
from pathlib import Path

from oot.errors import ConfigError, FetchError
from oot.git.inspect import is_empty_dir

logger = logging.getLogger(__name__)


class Repo:
    def __init__(self, path: Path, url: str | None):
        self.path = path
        self.url = url

    def git(self, *args, **kwargs):
        defaults = {"capture_output": True, "text": True}
        return subprocess.run(["git", "-C", self.path, *args], **{**defaults, **kwargs})

    def get_blob_hash(self, rel_path: str, rev: str = "HEAD") -> str | None:
        """Return the blob hash of rel_path at rev, or None if absent."""
        r = self.git("rev-parse", "--verify", "--quiet", f"{rev}:{rel_path}")
        return r.stdout.strip() if r.returncode == 0 else None

    def read_blob(self, blob: str) -> bytes | None:
        """Return a blob's raw content, or None if the object is not in the repo."""
        r = self.git("cat-file", "blob", blob, text=False)
        return r.stdout if r.returncode == 0 else None

    def rev_parse(self, rev: str) -> str | None:
        r = self.git("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
        return r.stdout.strip() if r.returncode == 0 else None

    def upstream_ref(self, ref: str) -> str | None:
        """The local ref for config ref: the tag if one exists, else origin/<ref>."""
        for candidate in (f"refs/tags/{ref}", f"refs/remotes/origin/{ref}"):
            if self.rev_parse(candidate):
                return candidate
        return None

    def merge_base(self, a: str, b: str) -> str | None:
        r = self.git("merge-base", a, b)
        return r.stdout.strip() if r.returncode == 0 else None

    def changed_files(self, base: str, head: str) -> list[tuple[str, str]]:
        """(status, path) for each file changed from base to head; renames are add + delete."""
        r = self.git("diff", "-z", "--no-renames", "--name-status", base, head, check=True)
        fields = r.stdout.split("\0")
        return [(fields[i], fields[i + 1]) for i in range(0, len(fields) - 1, 2)]

    def dirty_files(self) -> list[str]:
        """Staged, unstaged and untracked (not ignored) changes, as 'XY path' lines."""
        r = self.git("status", "--porcelain", "-z", "--no-renames", check=True)
        return [entry for entry in r.stdout.split("\0") if entry]

    def local_commit_count(self) -> int:
        """Commits on HEAD not reachable from any remote-tracking branch or tag."""
        r = self.git("rev-list", "--count", "HEAD", "--not", "--remotes", "--tags")
        return int(r.stdout.strip()) if r.returncode == 0 else 0

    def clone(self, ref: str, depth: int = 1, force: bool = False):
        logger.info(f"Cloning repo from {self.url} to {self.path}")

        if force and self.path.exists() and not is_empty_dir(self.path):
            logger.warning(f"Removing existing directory: {self.path}")
            shutil.rmtree(self.path)

        self.path.mkdir(parents=True, exist_ok=True)

        # output not captured so clone progress and errors reach the terminal
        r = self.git(
            "clone",
            "--depth",
            str(depth),
            "--branch",
            ref,
            self.url,
            self.path,
            capture_output=False,
        )
        if r.returncode != 0:
            raise FetchError(f"git clone {self.url} failed (exit {r.returncode})")

        return r

    def remote_has_tag(self, ref: str) -> bool:
        r = self.git("ls-remote", "--exit-code", "origin", f"refs/tags/{ref}")
        return r.returncode == 0

    def update(self, ref: str, depth: int = 1):
        origin = self.get_origin()
        if origin == self.url:
            return self._update_repo(ref, depth, tag=self.remote_has_tag(ref))
        else:
            raise ConfigError(
                f"{self} is a git repo with a different origin than the one in the config.\n"
                f"      Config  : {self.url}\n"
                f"      Current : {origin}\n"
                f"      To use this repo, update 'url' in your config file."
            )

    def _update_repo(self, ref: str, depth: int | None = None, tag: bool = False):
        if tag:
            target = f"refs/tags/{ref}"
            cmd = ["fetch", "origin", f"+{target}:{target}"]
        else:
            target = f"origin/{ref}"
            cmd = ["fetch", "origin", f"+refs/heads/{ref}:refs/remotes/origin/{ref}"]
        if depth is not None:
            cmd += ["--depth", str(depth)]

        r = self.git(*cmd)
        if r.returncode != 0:
            raise FetchError(f"git fetch failed:\n{r.stderr.strip()}")

        r = self.git("reset", "--hard", target)
        if r.returncode != 0:
            raise FetchError(
                f"git reset --hard {target} failed:\n{r.stderr.strip()}"
            )

        r = self.git("clean", "-fd")
        if r.returncode != 0:
            raise FetchError(f"git clean failed:\n{r.stderr.strip()}")

        logger.info(f"Updated to {ref}")

    def is_git_repo(self) -> bool:
        result = self.git("rev-parse", "--is-inside-work-tree")
        return result.returncode == 0

    def get_origin(self):
        result = self.git("remote", "get-url", "origin")

        if result.returncode != 0:
            return None

        return result.stdout.strip()

    def set_origin(self, origin):
        return self.git("remote", "add", "origin", origin, check=True)
