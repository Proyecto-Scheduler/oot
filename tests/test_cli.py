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


@pytest.fixture
def worked(tmp_path, monkeypatch):
    """Kernel cloned at tag v1 with one committed change; config in cwd."""
    up = tmp_path / "up"
    up.mkdir()
    git(up, "init", "-b", "main")
    git(up, "config", "user.email", "t@t")
    git(up, "config", "user.name", "t")
    (up / "a.c").write_text("a\n")
    git(up, "add", "a.c")
    git(up, "commit", "-m", "v1")
    git(up, "tag", "v1")

    kernel = tmp_path / "kernel"
    subprocess.run(["git", "clone", "-q", "--branch", "v1", f"file://{up}", str(kernel)], check=True)
    git(kernel, "config", "user.email", "t@t")
    git(kernel, "config", "user.name", "t")
    (kernel / "a.c").write_text("a patched\n")
    git(kernel, "commit", "-am", "work")

    (tmp_path / "oot.yml").write_text(
        f"dir: {tmp_path}\nkernel: {{ref: v1, dir: {kernel}}}\npatches: {{ref: main}}\n"
    )
    monkeypatch.chdir(tmp_path)
    return kernel


def patches_log(tmp_path) -> str:
    r = subprocess.run(
        ["git", "-C", str(tmp_path / "patches"), "log", "--format=%s"],
        capture_output=True, text=True,
    )
    return r.stdout.strip()


def test_sync_command_writes_metadata_and_commits(worked, tmp_path):
    r = CliRunner().invoke(cli, ["sync", "-m", "snapshot"])

    assert r.exit_code == 0, r.output
    meta = json.loads((tmp_path / "patches" / "metadata.json").read_text())
    assert [f["path"] for f in meta["files"]] == ["a.c"]
    assert patches_log(tmp_path) == "snapshot"


def test_sync_dry_run_prints_plan(worked, tmp_path):
    r = CliRunner().invoke(cli, ["sync", "-m", "x", "--dry-run"])

    assert r.exit_code == 0, r.output
    assert "+ a.c" in r.output
    assert not (tmp_path / "patches").exists()


def test_sync_dirty_kernel_prompt_decline_stops(worked, tmp_path):
    (worked / "b.c").write_text("untracked\n")

    r = CliRunner().invoke(cli, ["sync", "-m", "x"], input="n\n")

    assert r.exit_code == 0, r.output
    assert "b.c" in r.output
    assert "commit" in r.output.lower()
    assert not (tmp_path / "patches").exists()


def test_sync_dirty_kernel_prompt_accept_continues(worked, tmp_path):
    (worked / "b.c").write_text("untracked\n")

    r = CliRunner().invoke(cli, ["sync", "-m", "x"], input="y\n")

    assert r.exit_code == 0, r.output
    assert patches_log(tmp_path) == "x"


def test_sync_removing_most_patches_prompt_decline_stops(worked, tmp_path):
    CliRunner().invoke(cli, ["sync", "-m", "first"])
    git(worked, "reset", "-q", "--hard", "v1")  # as if the patches were never installed

    r = CliRunner().invoke(cli, ["sync", "-m", "oops"], input="n\n")

    assert r.exit_code == 0, r.output
    assert "remove 1 of 1" in r.output
    assert patches_log(tmp_path) == "first"


def test_sync_command_requires_message(worked):
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
