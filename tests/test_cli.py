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


# --- oot git ---


def test_git_runs_in_patches_dir(setup):
    # neither the project dir nor the kernel's parent is the patches repo
    git(setup.parent / "patches", "init")
    r = CliRunner().invoke(cli, ["git", "rev-parse", "--is-inside-work-tree"])
    assert r.exit_code == 0, r.output


def test_git_propagates_exit_code(setup):
    git(setup.parent / "patches", "init")
    # 128 is git's own fatal-error code, distinct from click's 1
    r = CliRunner().invoke(cli, ["git", "rev-parse", "--verify", "nope"])
    assert r.exit_code == 128


# --- oot sync ---


def test_sync_command_writes_metadata_and_commits(setup):
    patches = setup.parent / "patches"
    git(patches, "init")
    git(patches, "config", "user.email", "t@t")
    git(patches, "config", "user.name", "t")
    git(setup, "update-ref", "refs/remotes/origin/main", "HEAD")

    r = CliRunner().invoke(cli, ["sync", "-m", "snapshot"])

    assert r.exit_code == 0, r.output
    meta = json.loads((patches / "metadata.json").read_text())
    assert {f["path"] for f in meta["files"]} == {"sys/a.c", "sys/b.c"}
    log = subprocess.run(
        ["git", "-C", str(patches), "log", "--format=%s"], capture_output=True, text=True
    )
    assert log.stdout.strip() == "snapshot"


def test_sync_command_requires_message(setup):
    r = CliRunner().invoke(cli, ["sync"])
    assert r.exit_code == 2
    assert "Missing option" in r.output


# --- error translation ---


def test_expected_error_is_a_clean_message(setup, monkeypatch):
    from oot.errors import PatchError

    def boom(*a, **k):
        raise PatchError("something the user can fix")

    monkeypatch.setattr("oot.commands.install", boom)
    r = CliRunner().invoke(cli, ["install"])
    assert r.exit_code == 1
    assert "Error: something the user can fix" in r.output
    assert isinstance(r.exception, SystemExit)


def test_git_failure_shows_command_and_stderr(setup, monkeypatch):
    def boom(*a, **k):
        raise subprocess.CalledProcessError(128, ["git", "commit"], stderr="fatal: no identity\n")

    monkeypatch.setattr("oot.commands.sync", boom)
    r = CliRunner().invoke(cli, ["sync", "-m", "x"])
    assert r.exit_code == 1
    assert "git commit" in r.output
    assert "fatal: no identity" in r.output


def test_programming_error_keeps_its_traceback(setup, monkeypatch):
    def boom(*a, **k):
        raise TypeError("a bug")

    monkeypatch.setattr("oot.commands.install", boom)
    r = CliRunner().invoke(cli, ["install"])
    assert isinstance(r.exception, TypeError)


def test_path_rejects_unknown_target(setup):
    r = CliRunner().invoke(cli, ["path", "nope"])
    assert r.exit_code == 2
    assert "kernel" in r.output and "patches" in r.output


def test_version_matches_pyproject():
    import re

    # regex rather than tomllib, which needs Python 3.11
    pyproject = (Path(__file__).parents[1] / "pyproject.toml").read_text()
    expected = re.search(r'^version = "(.+)"', pyproject, re.M).group(1)
    r = CliRunner().invoke(cli, ["--version"])
    assert r.exit_code == 0, r.output
    assert expected in r.output
