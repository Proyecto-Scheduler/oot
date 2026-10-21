import logging
import shlex
import subprocess

from oot.config import Project

logger = logging.getLogger(__name__)


def git(cfg: Project, args: list[str]) -> int:
    """Run git in the patches repo; returns git's exit code."""
    cmd = ["git", "-C", str(cfg.patches.dir), *args]

    logger.debug("Running: %s", shlex.join(cmd))

    return subprocess.run(cmd).returncode
