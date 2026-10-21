from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

from oot.errors import ConfigError


class RepoConfig(BaseModel):
    url: str | None = None
    ref: str
    depth: int = 1
    # Optional in the YAML; Project fills in <dir>/<name>, so it is always a Path after validation
    dir: Path = Field(default=None)


class Project(BaseModel):
    dir: Path
    kernel: RepoConfig
    patches: RepoConfig

    @model_validator(mode="after")
    def _resolve_dirs(self):
        self.dir = self.dir.expanduser().resolve()
        for name in ("kernel", "patches"):
            repo = getattr(self, name)
            repo.dir = (repo.dir or self.dir / name).expanduser().resolve()
        return self


def load_project(path: Path) -> Project:
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")

    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"Failed to read config: {e}")

    try:
        return Project.model_validate(data)
    except ValidationError as e:
        raise ConfigError(f"Invalid config {path}:\n{e}")
