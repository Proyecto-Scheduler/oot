import logging
from pathlib import Path

from oot.config import Project
from oot.git.repo import Repo
from oot.metadata import BASE_DIR, FileMetadata, Metadata, metadata_path, patch_files

logger = logging.getLogger(__name__)

def _compute_entries(
    kernel_repo: Repo,
    patches_dir: Path,
    upstream: str,
    old: Metadata | None,
) -> list[FileMetadata]:
    """
    Produce a consolidated FileMetadata list by comparing the current state of
    patches_dir against upstream (origin/<ref>), never HEAD: install commits
    patches onto HEAD.

    Consolidation rules
    -------------------
    File on disk in patches_dir:
      - Not in upstream                         → "new"
      - In upstream, identical content          → drop (no patch needed)
      - In upstream, different content          → "modified" (preserve base_blob)

    File previously tracked (in old) but no longer on disk:
      - Was "new" in prior metadata             → drop entirely (we added it, we removed it)
      - Was "modified" or "deleted", in upstream → "deleted"
      - Was "modified" or "deleted", not in upstream → drop (already gone)
    """
    existing = {f.path: f for f in old.files} if old else {}
    on_disk = patch_files(patches_dir)
    entries: list[FileMetadata] = []

    for rel in on_disk:
        blob = kernel_repo.get_blob_hash(rel, upstream)

        if blob is None:
            # File not in upstream at all → new
            entries.append(FileMetadata(path=rel, base_blob=None, status="new"))
            continue

        if (patches_dir / rel).read_bytes() == kernel_repo.read_blob(blob):
            logger.debug("Identical to upstream, dropping: %s", rel)
            continue

        # Preserve the base established when the file was first captured
        # so that install still merges against the original.
        prev = existing.get(rel)
        if prev is not None and prev.status == "modified":
            # Older metadata left base_blob unset and relied on the global commit
            file_base_blob = prev.base_blob or kernel_repo.get_blob_hash(
                rel, old.kernel_commit
            )
        else:
            file_base_blob = blob

        entries.append(
            FileMetadata(path=rel, base_blob=file_base_blob, status="modified")
        )

    # Previously tracked files that disappeared from disk
    for rel, prev in existing.items():
        if rel in on_disk:
            continue  # already handled above

        if prev.status == "new":
            # We added it, now it's gone → net effect is nothing
            logger.debug("New file removed, consolidating away: %s", rel)
            continue

        if kernel_repo.get_blob_hash(rel, upstream) is None:
            # Not in upstream either → nothing to restore, drop
            logger.debug("Removed file absent from upstream, consolidating away: %s", rel)
        else:
            entries.append(FileMetadata(path=rel, base_blob=None, status="deleted"))

    return sorted(entries, key=lambda e: e.path)


def _plan_base_copies(
    kernel_repo: Repo, patches_dir: Path, entries: list[FileMetadata]
) -> tuple[dict[Path, bytes], list[Path]]:
    """Base copies to write (missing ones) and to delete (no longer modified)."""
    base_root = patches_dir / BASE_DIR
    wanted = {e.path: e for e in entries if e.status == "modified"}

    to_write: dict[Path, bytes] = {}
    for rel, entry in wanted.items():
        dst = base_root / rel
        if dst.is_file():
            continue
        content = kernel_repo.read_blob(entry.base_blob) if entry.base_blob else None
        if content is None:
            raise RuntimeError(
                f"Cannot create base copy for {rel}: blob {entry.base_blob} "
                f"is not in {kernel_repo.path}.\n"
                "Fetch the kernel commit it came from (e.g. a deeper 'kernel.depth') "
                "and run 'oot sync' again."
            )
        to_write[dst] = content

    stale = []
    if base_root.is_dir():
        stale = [
            p
            for p in base_root.rglob("*")
            if p.is_file() and str(p.relative_to(base_root)) not in wanted
        ]

    return to_write, stale


def sync(cfg: Project, message: str, dry_run: bool = False):

    patches_dir = cfg.patches.dir

    patches_repo = Repo(cfg.patches.dir, None)
    kernel_repo = Repo(cfg.kernel.dir, cfg.kernel.url)

    if not patches_repo.is_git_repo():
        raise RuntimeError(f"{patches_dir} is not a git repository")

    meta_path = metadata_path(patches_dir)
    old = Metadata.load(meta_path) if meta_path.is_file() else None

    upstream = f"origin/{cfg.kernel.ref}"
    kernel_commit = kernel_repo.rev_parse(upstream)
    if kernel_commit is None:
        raise RuntimeError(
            f"{upstream} not found in {kernel_repo.path}. Run 'oot fetch -t kernel'."
        )

    new_entries = _compute_entries(kernel_repo, patches_dir, upstream, old)
    to_write, stale = _plan_base_copies(kernel_repo, patches_dir, new_entries)
    new_metadata = Metadata(kernel_commit=kernel_commit, files=new_entries)

    if dry_run:
        print(new_metadata.model_dump_json(indent=2))
        return

    for path, content in to_write.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    for path in stale:
        path.unlink()

    new_metadata.save(meta_path)
    logger.info("Wrote %s", meta_path)

    patches_repo.git("add", "-A", check=True)

    # Only commit if there is something staged
    r = patches_repo.git("diff", "--cached", "--quiet")
    if r.returncode == 0:
        logger.info("Nothing to commit")
        return

    patches_repo.git("commit", "-m", message, check=True)
    logger.info("Committed: %s", message)
