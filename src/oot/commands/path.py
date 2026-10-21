from typing import Literal

from oot.config import Project


def path(cfg: Project, target: Literal["kernel", "patches"]):
    print(getattr(cfg, target).dir)
