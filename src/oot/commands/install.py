import filecmp
import logging
import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from oot.config import Project
from oot.errors import PatchError
from oot.git.merge import merge3
from oot.git.repo import Repo
from oot.metadata import BASE_DIR, FileMetadata, Metadata
from oot.metadata import metadata_path as default_metadata_path

logger = logging.getLogger(__name__)

class Action(Enum):
    FORCE = "force"
    SKIP = "skip"
    ABORT = "abort"


# Fixed-policy resolvers for --on-conflict; the CLI prompt is the other adapter
resolvers = {a.value: (lambda _, a=a: a) for a in Action}


@dataclass
class Context:
    repo: Repo
    patches_dir: Path
    kernel_dir: Path
    resolve: Callable[[str], Action]
    dry_run: bool
    # files left with conflict markers; install does not commit when non-empty
    conflicted: list[str] = field(default_factory=list)


def install(
    cfg: Project,
    resolver,
    metadata_path: str | None = None,
    dry_run: bool = False,
    fail_fast: bool = True,
):

    metadata = Metadata.load(
        Path(metadata_path) if metadata_path else default_metadata_path(cfg.patches.dir)
    )
    repo = Repo(cfg.kernel.dir, cfg.kernel.url)

    ctx = Context(repo, cfg.patches.dir, cfg.kernel.dir, resolver, dry_run)

    handlers = {
        "modified": _install_modified,
        "new": _install_new,
        "deleted": _install_deleted,
    }

    for file in metadata.files:
        # status is a Literal validated by Metadata, so the lookup cannot miss
        handler = handlers[file.status]

        try:
            result = handler(ctx, file)

            if result == Action.SKIP:
                logger.info("Skipping %s", file.path)
            elif result == Action.ABORT:
                logger.info("Aborting install")
                break

        except Exception as e:
            if fail_fast:
                raise
            logger.error("Failed processing %s: %s", file.path, e)

    if ctx.dry_run:
        return

    if ctx.conflicted:
        logger.warning(
            "Conflict markers left in:\n  %s\nResolve them and commit in %s.",
            "\n  ".join(ctx.conflicted),
            ctx.kernel_dir,
        )
        return

    ctx.repo.git("add", "-A", check=True)
    if ctx.repo.git("diff", "--cached", "--quiet").returncode != 0:
        ctx.repo.git("commit", "-m", "chore: installed patches to kernel", check=True)


def _install_deleted(ctx: Context, file: FileMetadata):
    dst = ctx.kernel_dir / file.path

    if not dst.exists():
        # install should be somewhat idempotent, if the file doesn't exist, our job is done
        pass
    # directories are not handled in order to follow git's logic
    elif dst.is_file() or dst.is_symlink():
        if not dst.resolve().is_relative_to(ctx.kernel_dir.resolve()):
            raise PatchError(f"Path escapes kernel dir: {dst}")
        if not ctx.dry_run:
            dst.unlink()
    else:
        raise PatchError(f"Unsupported file type for path: {dst}")


def _install_modified(ctx: Context, file: FileMetadata):
    src = ctx.patches_dir / file.path
    dst = ctx.kernel_dir / file.path
    base = ctx.patches_dir / BASE_DIR / file.path

    if not src.is_file():
        raise PatchError(f"File {file.path} not found in patches repo")

    if not dst.is_file():
        raise PatchError(f"File {file.path} not found in kernel repo")

    if not base.is_file():
        raise PatchError(
            f"Base copy for {file.path} not found; run 'oot sync' to create it"
        )

    current = dst.read_bytes()
    result = merge3(base.read_bytes(), current, src.read_bytes())

    if result.conflicts:
        if ctx.dry_run:
            logger.warning("Would conflict: %s", file.path)
            return

        action = ctx.resolve(file.path)
        if action == Action.SKIP or action == Action.ABORT:
            return action

        if result.content is None:
            # binary: no markers possible, force means "take the patches copy"
            content = src.read_bytes()
        else:
            content = result.content
            ctx.conflicted.append(file.path)
    else:
        content = result.content

    if content == current:
        logger.debug("Already applied: %s", file.path)
        return

    if not ctx.dry_run:
        dst.write_bytes(content)

    logger.debug("Installed %s", file.path)


def _install_new(ctx: Context, file: FileMetadata):
    src = ctx.patches_dir / file.path
    dst = ctx.kernel_dir / file.path

    if not src.is_file():
        raise PatchError(f"patch file not found: {file.path}")

    if dst.exists():
        if filecmp.cmp(src, dst, shallow=False):
            logger.debug("File identical, skipping: %s", file.path)
            return

        if ctx.dry_run:
            logger.warning("Would conflict: %s", file.path)
            return

        action = ctx.resolve(file.path)

        if action == Action.SKIP or action == Action.ABORT:
            return action

        # force → overwrite
    if not ctx.dry_run:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dst)

    logger.debug("Installed %s", file.path)
