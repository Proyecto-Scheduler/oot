import logging
from typing import Callable, Literal

from oot.config import Project
from oot.errors import RepoStateError
from oot.git import Repo, is_empty_dir

logger = logging.getLogger(__name__)


def fetch(
    cfg: Project,
    target: Literal["kernel", "patches"],
    force: bool,
    confirm: Callable[[str], bool] = lambda _: False,
):
    target_cfg = getattr(cfg, target)

    repo = Repo(target_cfg.dir, target_cfg.url)

    if force:
        return repo.clone(depth=target_cfg.depth, ref=target_cfg.ref, force=True)

    if repo.is_git_repo():
        if repo.get_origin() is None:
            if confirm(
                f"\n {repo} is a git repo with no remote origin.\n"
                f"  Do you want to add {repo.url} as origin and continue?"
            ):
                repo.set_origin(repo.url)
            else:
                logger.info(
                    "Cancelled. Update 'dir' in your config file to point to a different directory."
                )
                return

        _refuse_to_discard_work(repo)
        return repo.update(ref=target_cfg.ref, depth=target_cfg.depth)

    if is_empty_dir(repo.path):
        logger.debug("Directory is empty, cloning inside...")
        return repo.clone(depth=target_cfg.depth, ref=target_cfg.ref)

    if repo.url is None:
        logger.info("Directory is not empty, and url is not set. Nothing to do here.")
        return
    else:
        raise RepoStateError(
            f"{repo.path} exists, is not empty, and is not a git repository.\n\n"
            f"Configured URL: {repo.url}\n\n"
            "Refusing to clone into a non-empty directory.\n\n"
            "Options:\n"
            "  • Remove the directory and run 'oot fetch' again\n"
            "  • Remove 'kernel.url' from the config to use the existing directory\n"
            "  • Use '--force' to overwrite the directory (WARNING: deletes contents)"
        )


def _refuse_to_discard_work(repo: Repo):
    """Updating resets and cleans the repo, so stop if that would lose work."""
    commits = repo.local_commit_count()
    dirty = repo.dirty_files()
    if not commits and not dirty:
        return

    lines = []
    if commits:
        lines.append(f"{commits} local commit(s) not in any remote branch or tag")
    if dirty:
        lines.append("uncommitted changes:\n" + "\n".join(f"    {d}" for d in dirty))
    raise RepoStateError(
        f"{repo.path} has work that updating would discard:\n  "
        + "\n  ".join(lines)
        + "\n\nRun 'oot sync' to save kernel work to the patches repo, push it, "
        "or use '--force' to discard it."
    )
