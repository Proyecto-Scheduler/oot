import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from oot.config import Project
from oot.errors import PatchError
from oot.git.repo import Repo
from oot.git.inspect import is_empty_dir
from oot.metadata import (
    BASE_DIR,
    METADATA_FILE,
    OOTIGNORE_FILE,
    FileMetadata,
    Metadata,
    ignored_paths,
    metadata_path,
    patch_files,
)

logger = logging.getLogger(__name__)

# git diff --name-status letters; anything else (e.g. T, a type change) counts as modified
_STATUS = {"A": "new", "D": "deleted"}


@dataclass
class Plan:
    upstream: str
    base: str
    head: str
    metadata: Metadata
    # new/modified: content at kernel HEAD; modified: content at base
    content: dict[str, bytes]
    base_copies: dict[str, bytes]
    add: list[str] = field(default_factory=list)
    update: list[str] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)
    unchanged: int = 0
    dirty: list[str] = field(default_factory=list)


def _mass_removal(plan: Plan) -> str | None:
    """Warning when the sync would remove more than half of the current patch set."""
    current = len(plan.update) + len(plan.remove) + plan.unchanged
    if not current or len(plan.remove) * 2 <= current:
        return None
    return (
        f"This sync would remove {len(plan.remove)} of {current} patches from the patch set.\n"
        "That usually means the patches are not installed in this kernel (run 'oot install' first)."
    )


def _is_repo_root(path: Path) -> bool:
    return (path / ".git").exists()


def _reserved(rel: str) -> bool:
    return rel in (METADATA_FILE, OOTIGNORE_FILE) or rel.startswith(f"{BASE_DIR.parts[0]}/")


def _make_plan(cfg: Project, kernel: Repo, patches_dir: Path) -> Plan:
    ref = cfg.kernel.ref
    upstream = kernel.upstream_ref(ref)
    if upstream is None:
        raise PatchError(
            f"'{ref}' is neither a tag nor origin/{ref} in {kernel.path}. "
            "Check 'kernel.ref' or run 'oot fetch -t kernel'."
        )
    head = kernel.rev_parse("HEAD")
    base = kernel.merge_base(head, upstream) if head else None
    if base is None:
        raise PatchError(
            f"Kernel HEAD has no common history with {upstream}.\n"
            "If the clone is shallow, deepen it (git fetch --unshallow) and try again."
        )

    patches_is_repo = _is_repo_root(patches_dir)
    if patches_dir.exists() and not patches_is_repo and not is_empty_dir(patches_dir):
        raise PatchError(f"{patches_dir} is not empty and is not a git repository")

    changes = kernel.changed_files(base, head)
    paths = [rel for _, rel in changes]
    collisions = [rel for rel in paths if _reserved(rel)]
    if patches_is_repo:
        collisions += sorted(ignored_paths(patches_dir, paths))
    if collisions:
        raise PatchError(
            "These kernel changes cannot be stored in the patches repo, because the "
            "paths are reserved by oot or ignored by .ootignore/.gitignore:\n  "
            + "\n  ".join(collisions)
        )

    files: list[FileMetadata] = []
    content: dict[str, bytes] = {}
    base_copies: dict[str, bytes] = {}
    for code, rel in changes:
        status = _STATUS.get(code[0], "modified")
        base_blob = None
        if status != "deleted":
            content[rel] = kernel.read_blob(f"{head}:{rel}")
        if status == "modified":
            base_blob = kernel.get_blob_hash(rel, base)
            base_copies[rel] = kernel.read_blob(base_blob)
        files.append(FileMetadata(path=rel, base_blob=base_blob, status=status))

    plan = Plan(
        upstream=upstream,
        base=base,
        head=head,
        metadata=Metadata(kernel_commit=base, files=sorted(files, key=lambda f: f.path)),
        content=content,
        base_copies=base_copies,
        dirty=kernel.dirty_files(),
    )

    # Compare with the current patch set to classify each path
    meta_path = metadata_path(patches_dir)
    old = {}
    if meta_path.is_file():
        old = {f.path: f for f in Metadata.load(meta_path).files}
    current = set(old) | (patch_files(patches_dir) if patches_is_repo else set())

    for entry in plan.metadata.files:
        rel = entry.path
        if rel not in current:
            plan.add.append(rel)
        elif _differs(patches_dir, old.get(rel), entry, content.get(rel)):
            plan.update.append(rel)
        else:
            plan.unchanged += 1
    plan.remove = sorted(current - {f.path for f in plan.metadata.files})

    return plan


def _differs(patches_dir: Path, old: FileMetadata | None, new: FileMetadata, content: bytes | None) -> bool:
    if old is None or old.status != new.status or old.base_blob != new.base_blob:
        return True
    dst = patches_dir / new.path
    if new.status == "deleted":
        return dst.exists()
    if new.status == "modified" and not (patches_dir / BASE_DIR / new.path).is_file():
        return True
    return not dst.is_file() or dst.read_bytes() != content


def format_plan(plan: Plan, ref: str) -> str:
    status = {f.path: f.status for f in plan.metadata.files}
    lines = [f"Base: {ref} ({plan.base[:10]})    Kernel HEAD: {plan.head[:10]}", ""]

    rows = (
        [("+", rel, status[rel]) for rel in plan.add]
        + [("~", rel, status[rel]) for rel in plan.update]
        + [("-", rel, "") for rel in plan.remove]
    )
    if rows:
        width = max(len(rel) for _, rel, _ in rows)
        lines += [f"  {mark} {rel.ljust(width)}  {st}".rstrip() for mark, rel, st in sorted(rows, key=lambda r: r[1])]
        lines.append("")
        lines.append(
            f"Plan: {len(plan.add)} to add, {len(plan.update)} to update, "
            f"{len(plan.remove)} to remove. Unchanged: {plan.unchanged}."
        )
    else:
        lines.append("No changes. The patch set matches the kernel.")

    warning = _mass_removal(plan)
    if warning:
        lines += ["", f"Warning: {warning}"]

    if plan.dirty:
        lines += ["", "Uncommitted changes in the kernel (not synced):"]
        lines += [f"    {d}" for d in plan.dirty]

    return "\n".join(lines)


def _apply(plan: Plan, patches_dir: Path, branch: str, message: str):
    if not _is_repo_root(patches_dir):
        patches_dir.mkdir(parents=True, exist_ok=True)
        Repo(patches_dir, None).git("init", "-q", "-b", branch, check=True)
        logger.info("Initialised patches repo at %s", patches_dir)

    for rel in plan.remove:
        (patches_dir / rel).unlink(missing_ok=True)

    for entry in plan.metadata.files:
        dst = patches_dir / entry.path
        if entry.status == "deleted":
            dst.unlink(missing_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(plan.content[entry.path])

    base_root = patches_dir / BASE_DIR
    if base_root.is_dir():
        for p in base_root.rglob("*"):
            if p.is_file() and str(p.relative_to(base_root)) not in plan.base_copies:
                p.unlink()
    for rel, data in plan.base_copies.items():
        dst = base_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)

    plan.metadata.save(metadata_path(patches_dir))

    repo = Repo(patches_dir, None)
    repo.git("add", "-A", check=True)
    # Only commit if there is something staged
    if repo.git("diff", "--cached", "--quiet").returncode == 0:
        logger.info("Nothing to commit")
        return
    repo.git("commit", "-q", "-m", message, check=True)
    logger.info("Committed: %s", message)


def sync(
    cfg: Project,
    message: str,
    dry_run: bool = False,
    confirm: Callable[[str], bool] = lambda _: False,
):
    """Make the patches repo mirror the kernel's committed changes since its base."""
    kernel = Repo(cfg.kernel.dir, cfg.kernel.url)
    patches_dir = cfg.patches.dir

    plan = _make_plan(cfg, kernel, patches_dir)

    if dry_run:
        print(format_plan(plan, cfg.kernel.ref))
        return

    if plan.dirty and not confirm(
        f"Uncommitted changes in {kernel.path} will not be synced:\n"
        + "\n".join(f"    {d}" for d in plan.dirty)
        + "\nContinue with the committed state only? (No: stop so you can commit first)"
    ):
        logger.warning("Sync stopped. Commit your kernel changes and run 'oot sync' again.")
        return

    warning = _mass_removal(plan)
    if warning and not confirm(f"{warning}\nRemove them anyway?"):
        logger.warning("Sync stopped. Nothing was changed.")
        return

    _apply(plan, patches_dir, cfg.patches.ref, message)
