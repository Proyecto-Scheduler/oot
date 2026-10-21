"""CLI tests: the interactive conflict prompt end to end."""

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from oot.cli import cli


def git(path: Path, *args):
    subprocess.run(["git", "-C", str(path), *args], capture_output=True, check=True)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    kernel = tmp_path / "kernel"
    patches = tmp_path / "patches"
    (kernel / "sys").mkdir(parents=True)
    (patches / "sys").mkdir(parents=True)
    git(kernel, "init")
    git(kernel, "config", "user.email", "t@t")
    git(kernel, "config", "user.name", "t")
    git(kernel, "commit", "--allow-empty", "-m", "initial")

    # a.c conflicts (exists in kernel with other content), b.c installs cleanly
    (kernel / "sys/a.c").write_text("theirs\n")
    (patches / "sys/a.c").write_text("mine\n")
    (patches / "sys/b.c").write_text("new\n")
    (patches / "metadata.json").write_text(
        json.dumps(
            {
                "kernel_commit": "abc",
                "files": [
                    {"path": "sys/a.c", "status": "new"},
                    {"path": "sys/b.c", "status": "new"},
                ],
            }
        )
    )
    (tmp_path / "oot.yml").write_text(
        f"dir: {tmp_path}\n"
        f"kernel: {{ref: main, dir: {kernel}}}\n"
        f"patches: {{ref: main, dir: {patches}}}\n"
    )
    monkeypatch.chdir(tmp_path)
    return kernel


def run_install(answer: str):
    return CliRunner().invoke(cli, ["install"], input=f"{answer}\n")


def test_prompt_skip_keeps_kernel_file_and_continues(setup):
    r = run_install("skip")
    assert r.exit_code == 0, r.output
    assert (setup / "sys/a.c").read_text() == "theirs\n"
    assert (setup / "sys/b.c").exists()


def test_prompt_abort_stops_install(setup):
    r = run_install("abort")
    assert r.exit_code == 0, r.output
    assert (setup / "sys/a.c").read_text() == "theirs\n"
    assert not (setup / "sys/b.c").exists()


def test_prompt_force_overwrites(setup):
    r = run_install("force")
    assert r.exit_code == 0, r.output
    assert (setup / "sys/a.c").read_text() == "mine\n"


def test_on_conflict_flag_skips_prompt(setup):
    r = CliRunner().invoke(cli, ["install", "--on-conflict", "skip"])
    assert r.exit_code == 0, r.output
    assert (setup / "sys/a.c").read_text() == "theirs\n"
    assert (setup / "sys/b.c").exists()


def test_missing_default_config_is_a_clean_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(cli, ["path", "kernel"])
    assert r.exit_code == 1
    assert "No config file found" in r.output


def test_invalid_config_is_a_clean_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "oot.yml").write_text("dir: /x\nkernel: {}\npatches: {ref: b}\n")
    r = CliRunner().invoke(cli, ["path", "kernel"])
    assert r.exit_code == 1
    assert "ref" in r.output
