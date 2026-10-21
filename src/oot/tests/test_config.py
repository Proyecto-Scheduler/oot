from pathlib import Path

import pytest

from oot.config import Project, load_project
from oot.errors import ConfigError


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "oot.yml"
    p.write_text(text)
    return p


def test_repo_dirs_default_under_project_dir(tmp_path):
    p = Project.model_validate(
        {"dir": str(tmp_path), "kernel": {"ref": "a"}, "patches": {"ref": "b"}}
    )
    assert p.kernel.dir == tmp_path / "kernel"
    assert p.patches.dir == tmp_path / "patches"


def test_dirs_are_expanded_and_absolute(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    p = Project.model_validate(
        {
            "dir": "~/proj",
            "kernel": {"ref": "a", "dir": "rel/kernel"},
            "patches": {"ref": "b", "dir": "~/p"},
        }
    )
    assert p.dir == tmp_path / "proj"
    assert p.kernel.dir == tmp_path / "rel/kernel"
    assert p.patches.dir == tmp_path / "p"


def test_load_project_reads_yaml(tmp_path):
    cfg = write(tmp_path, f"dir: {tmp_path}\nkernel: {{ref: a}}\npatches: {{ref: b}}\n")
    p = load_project(cfg)
    assert p.kernel.ref == "a"
    assert p.kernel.dir == tmp_path / "kernel"


def test_load_project_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_project(tmp_path / "nope.yml")


def test_load_project_invalid_yaml(tmp_path):
    with pytest.raises(ConfigError, match="Failed to read config"):
        load_project(write(tmp_path, "dir: [unclosed\n"))


def test_load_project_schema_error_names_field(tmp_path):
    with pytest.raises(ConfigError, match="ref"):
        load_project(write(tmp_path, "dir: /x\nkernel: {}\npatches: {ref: b}\n"))
