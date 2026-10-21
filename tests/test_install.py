"""
Tests for the install command.

The kernel dir is a real git repo and files are real; only the resolver is stubbed.
"""

import json
import subprocess
from pathlib import Path

import pytest

from oot.errors import PatchError

from oot.commands.install import install, resolvers
from oot.config import Project, RepoConfig
from oot.metadata import BASE_DIR


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------


def git(path: Path, *args) -> str:
    r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture
def dirs(tmp_path):
    kernel = tmp_path / "kernel"
    patches = tmp_path / "patches"
    kernel.mkdir()
    patches.mkdir()
    git(kernel, "init")
    git(kernel, "config", "user.email", "test@example.com")
    git(kernel, "config", "user.name", "Test")
    git(kernel, "commit", "--allow-empty", "-m", "initial")
    return {"kernel": kernel, "patches": patches, "tmp": tmp_path}


@pytest.fixture
def project(dirs):
    return Project(
        dir=dirs["tmp"] / ".oot",
        kernel=RepoConfig(
            url="https://example.com/kernel.git", ref="main", dir=dirs["kernel"]
        ),
        patches=RepoConfig(
            url="https://example.com/patches.git", ref="main", dir=dirs["patches"]
        ),
    )


def write_metadata(patches_dir: Path, base_blob: str, files: list[dict]) -> Path:
    meta = {"base_blob": base_blob, "files": files}
    path = patches_dir / "metadata.json"
    path.write_text(json.dumps(meta))
    return path


def write(path: Path, content: str | bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content)


def commit_count(kernel: Path) -> int:
    return int(git(kernel, "rev-list", "--count", "HEAD"))


def never_called(path):
    raise AssertionError(f"resolver called for {path}")


REL = "sys/kern/kern_sched.c"


def modified(dirs, base, kernel, patch, rel=REL):
    """Set up one modified file: base copy, current kernel file and patches copy."""
    write(dirs["patches"] / BASE_DIR / rel, base)
    write(dirs["kernel"] / rel, kernel)
    write(dirs["patches"] / rel, patch)


# ---------------------------------------------------------------------------
# metadata location
# ---------------------------------------------------------------------------


def test_install_reads_custom_metadata_path(project, dirs):
    write(dirs["patches"] / "sys/kern/new.c", "new\n")
    custom = write_metadata(dirs["tmp"], "abc", [{"path": "sys/kern/new.c", "status": "new"}])

    install(project, never_called, metadata_path=str(custom))

    assert (dirs["kernel"] / "sys/kern/new.c").exists()


def test_install_raises_when_metadata_missing(project):
    with pytest.raises(PatchError, match="metadata file not found"):
        install(project, never_called)


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------


def test_resolver_abort_returns_abort():
    from oot.commands.install import Action

    assert resolvers["abort"]("any/path") == Action.ABORT


def test_resolver_skip_returns_skip():
    from oot.commands.install import Action

    assert resolvers["skip"]("any/path") == Action.SKIP


def test_resolver_force_returns_force():
    from oot.commands.install import Action

    assert resolvers["force"]("any/path") == Action.FORCE


# ---------------------------------------------------------------------------
# status=new — happy path
# ---------------------------------------------------------------------------


def test_install_copies_new_file(project, dirs):
    src = dirs["patches"] / "sys" / "kern" / "sched_petri.c"
    src.parent.mkdir(parents=True)
    src.write_text("// new scheduler\n")

    write_metadata(
        dirs["patches"], "abc123", [{"path": "sys/kern/sched_petri.c", "status": "new"}]
    )

    install(project, resolvers["abort"])

    dst = dirs["kernel"] / "sys" / "kern" / "sched_petri.c"
    assert dst.exists()
    assert dst.read_text() == "// new scheduler\n"


def test_install_creates_parent_dirs(project, dirs):
    src = dirs["patches"] / "sys" / "sys" / "petri_net.h"
    src.parent.mkdir(parents=True)
    src.write_text("// header\n")

    write_metadata(
        dirs["patches"], "abc123", [{"path": "sys/sys/petri_net.h", "status": "new"}]
    )

    install(project, resolvers["abort"])

    assert (dirs["kernel"] / "sys" / "sys" / "petri_net.h").exists()


def test_install_skips_new_file_if_identical_exists(project, dirs):
    content = "// same content\n"
    src = dirs["patches"] / "sys" / "kern" / "sched_petri.c"
    src.parent.mkdir(parents=True)
    src.write_text(content)

    dst = dirs["kernel"] / "sys" / "kern" / "sched_petri.c"
    dst.parent.mkdir(parents=True)
    dst.write_text(content)

    write_metadata(
        dirs["patches"], "abc123", [{"path": "sys/kern/sched_petri.c", "status": "new"}]
    )

    install(project, resolvers["abort"])

    assert dst.read_text() == content


def test_install_new_file_dry_run_does_not_copy(project, dirs):
    src = dirs["patches"] / "sys" / "kern" / "sched_petri.c"
    src.parent.mkdir(parents=True)
    src.write_text("// new\n")

    write_metadata(
        dirs["patches"], "abc123", [{"path": "sys/kern/sched_petri.c", "status": "new"}]
    )

    install(project, resolvers["abort"], dry_run=True)

    assert not (dirs["kernel"] / "sys" / "kern" / "sched_petri.c").exists()


# ---------------------------------------------------------------------------
# status=new — conflictos
# ---------------------------------------------------------------------------


def test_install_new_conflict_resolver_skip(project, dirs):
    src = dirs["patches"] / "sys" / "kern" / "sched_petri.c"
    src.parent.mkdir(parents=True)
    src.write_text("// new version\n")

    dst = dirs["kernel"] / "sys" / "kern" / "sched_petri.c"
    dst.parent.mkdir(parents=True)
    dst.write_text("// old version\n")

    write_metadata(
        dirs["patches"], "abc123", [{"path": "sys/kern/sched_petri.c", "status": "new"}]
    )

    install(project, resolvers["skip"])

    assert dst.read_text() == "// old version\n"


def test_install_new_conflict_resolver_force_overwrites(project, dirs):
    src = dirs["patches"] / "sys" / "kern" / "sched_petri.c"
    src.parent.mkdir(parents=True)
    src.write_text("// new version\n")

    dst = dirs["kernel"] / "sys" / "kern" / "sched_petri.c"
    dst.parent.mkdir(parents=True)
    dst.write_text("// old version\n")

    write_metadata(
        dirs["patches"], "abc123", [{"path": "sys/kern/sched_petri.c", "status": "new"}]
    )

    install(project, resolvers["force"])

    assert dst.read_text() == "// new version\n"


def test_install_new_conflict_resolver_abort_stops_install(project, dirs):
    for name in ["file_a.c", "file_b.c"]:
        src = dirs["patches"] / "sys" / "kern" / name
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("// new\n")

    dst_a = dirs["kernel"] / "sys" / "kern" / "file_a.c"
    dst_a.parent.mkdir(parents=True, exist_ok=True)
    dst_a.write_text("// old\n")

    write_metadata(
        dirs["patches"],
        "abc123",
        [
            {"path": "sys/kern/file_a.c", "status": "new"},
            {"path": "sys/kern/file_b.c", "status": "new"},
        ],
    )

    install(project, resolvers["abort"])

    assert not (dirs["kernel"] / "sys" / "kern" / "file_b.c").exists()


# ---------------------------------------------------------------------------
# status=modified
# ---------------------------------------------------------------------------


def test_modified_clean_merge_keeps_upstream_and_patch(project, dirs):
    modified(dirs, base="a\nb\nc\n", kernel="A\nb\nc\n", patch="a\nb\nC\n")
    write_metadata(dirs["patches"], "abc123", [{"path": REL, "status": "modified"}])

    install(project, never_called)

    assert (dirs["kernel"] / REL).read_text() == "A\nb\nC\n"


def test_modified_already_applied_is_left_alone(project, dirs):
    modified(dirs, base="a\n", kernel="b\n", patch="b\n")
    write_metadata(dirs["patches"], "abc123", [{"path": REL, "status": "modified"}])

    install(project, never_called)

    assert (dirs["kernel"] / REL).read_text() == "b\n"


def test_modified_raises_without_base_copy(project, dirs):
    write(dirs["kernel"] / REL, "a\n")
    write(dirs["patches"] / REL, "b\n")
    write_metadata(dirs["patches"], "abc123", [{"path": REL, "status": "modified"}])

    with pytest.raises(PatchError, match="oot sync"):
        install(project, never_called)


def test_modified_raises_if_target_missing_in_kernel(project, dirs):
    write(dirs["patches"] / BASE_DIR / REL, "a\n")
    write(dirs["patches"] / REL, "b\n")
    write_metadata(dirs["patches"], "abc123", [{"path": REL, "status": "modified"}])

    with pytest.raises(PatchError, match="not found in kernel repo"):
        install(project, never_called)


def test_modified_conflict_skip_leaves_kernel_file(project, dirs):
    modified(dirs, base="a\n", kernel="upstream\n", patch="mine\n")
    write_metadata(dirs["patches"], "abc123", [{"path": REL, "status": "modified"}])

    install(project, resolvers["skip"])

    assert (dirs["kernel"] / REL).read_text() == "upstream\n"


def test_modified_conflict_abort_stops_install(project, dirs):
    modified(dirs, base="a\n", kernel="upstream\n", patch="mine\n")
    write(dirs["patches"] / "sys/kern/later.c", "new\n")
    write_metadata(
        dirs["patches"],
        "abc123",
        [
            {"path": REL, "status": "modified"},
            {"path": "sys/kern/later.c", "status": "new"},
        ],
    )

    install(project, resolvers["abort"])

    assert not (dirs["kernel"] / "sys/kern/later.c").exists()


def test_modified_conflict_force_writes_markers_and_does_not_commit(project, dirs):
    modified(dirs, base="a\n", kernel="upstream\n", patch="mine\n")
    write_metadata(dirs["patches"], "abc123", [{"path": REL, "status": "modified"}])
    before = commit_count(dirs["kernel"])

    install(project, resolvers["force"])

    content = (dirs["kernel"] / REL).read_text()
    assert "<<<<<<< kernel" in content
    assert "upstream" in content and "mine" in content
    assert commit_count(dirs["kernel"]) == before


def test_modified_binary_conflict_force_takes_patch_copy(project, dirs):
    modified(dirs, base=b"a\0b", kernel=b"a\0c", patch=b"a\0d")
    write_metadata(dirs["patches"], "abc123", [{"path": REL, "status": "modified"}])

    install(project, resolvers["force"])

    assert (dirs["kernel"] / REL).read_bytes() == b"a\0d"


def test_modified_dry_run_reports_conflict_without_prompting(project, dirs):
    modified(dirs, base="a\n", kernel="upstream\n", patch="mine\n")
    write_metadata(dirs["patches"], "abc123", [{"path": REL, "status": "modified"}])

    install(project, never_called, dry_run=True)

    assert (dirs["kernel"] / REL).read_text() == "upstream\n"


def test_modified_dry_run_does_not_write(project, dirs):
    modified(dirs, base="a\nx\nb\n", kernel="A\nx\nb\n", patch="a\nx\nB\n")
    write_metadata(dirs["patches"], "abc123", [{"path": REL, "status": "modified"}])

    install(project, never_called, dry_run=True)

    assert (dirs["kernel"] / REL).read_text() == "A\nx\nb\n"


# ---------------------------------------------------------------------------
# Commits
# ---------------------------------------------------------------------------


def test_install_commits_once_for_all_files(project, dirs):
    modified(dirs, base="a\nx\nb\n", kernel="A\nx\nb\n", patch="a\nx\nB\n")
    write(dirs["patches"] / "sys/kern/new.c", "new\n")
    write_metadata(
        dirs["patches"],
        "abc123",
        [
            {"path": REL, "status": "modified"},
            {"path": "sys/kern/new.c", "status": "new"},
        ],
    )
    before = commit_count(dirs["kernel"])

    install(project, never_called)

    assert commit_count(dirs["kernel"]) == before + 1
    assert git(dirs["kernel"], "status", "--porcelain") == ""


def test_install_dry_run_does_not_commit(project, dirs):
    write(dirs["patches"] / "sys/kern/new.c", "new\n")
    write_metadata(
        dirs["patches"], "abc123", [{"path": "sys/kern/new.c", "status": "new"}]
    )
    before = commit_count(dirs["kernel"])

    install(project, never_called, dry_run=True)

    assert commit_count(dirs["kernel"]) == before


def test_install_new_file_dry_run_does_not_prompt(project, dirs):
    write(dirs["patches"] / "sys/kern/new.c", "mine\n")
    write(dirs["kernel"] / "sys/kern/new.c", "theirs\n")
    write_metadata(
        dirs["patches"], "abc123", [{"path": "sys/kern/new.c", "status": "new"}]
    )

    install(project, never_called, dry_run=True)

    assert (dirs["kernel"] / "sys/kern/new.c").read_text() == "theirs\n"




# ---------------------------------------------------------------------------
# Patch file faltante
# ---------------------------------------------------------------------------


def test_install_raises_when_patch_file_missing(project, dirs):
    write_metadata(
        dirs["patches"], "abc123", [{"path": "sys/kern/nonexistent.c", "status": "new"}]
    )

    with pytest.raises(PatchError, match="patch file not found"):
        install(project, resolvers["abort"])


# ---------------------------------------------------------------------------
# fail_fast
# ---------------------------------------------------------------------------


def test_install_fail_fast_stops_on_first_error(project, dirs):
    for name in ["file_a.c", "file_b.c"]:
        src = dirs["patches"] / "sys" / "kern" / name
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("// content\n")

    write_metadata(
        dirs["patches"],
        "abc123",
        [
            {"path": "sys/kern/file_a.c", "status": "new"},
            {"path": "sys/kern/file_b.c", "status": "new"},
        ],
    )

    def exploding_resolver(path):
        raise RuntimeError(f"unexpected error for {path}")

    dst_a = dirs["kernel"] / "sys" / "kern" / "file_a.c"
    dst_a.parent.mkdir(parents=True, exist_ok=True)
    dst_a.write_text("// different\n")

    with pytest.raises(RuntimeError):
        install(project, exploding_resolver, fail_fast=True)

    assert not (dirs["kernel"] / "sys" / "kern" / "file_b.c").exists()


def test_install_no_fail_fast_continues_on_error(project, dirs, caplog):
    import logging

    for name in ["file_a.c", "file_b.c"]:
        src = dirs["patches"] / "sys" / "kern" / name
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("// content\n")

    write_metadata(
        dirs["patches"],
        "abc123",
        [
            {"path": "sys/kern/file_a.c", "status": "new"},
            {"path": "sys/kern/file_b.c", "status": "new"},
        ],
    )

    def exploding_resolver(path):
        raise RuntimeError(f"unexpected error for {path}")

    dst_a = dirs["kernel"] / "sys" / "kern" / "file_a.c"
    dst_a.parent.mkdir(parents=True, exist_ok=True)
    dst_a.write_text("// different\n")

    with caplog.at_level(logging.ERROR, logger="oot.commands.install"):
        install(project, exploding_resolver, fail_fast=False)

    assert (dirs["kernel"] / "sys" / "kern" / "file_b.c").exists()
    assert "file_a.c" in caplog.text


# ---------------------------------------------------------------------------
# Múltiples archivos
# ---------------------------------------------------------------------------


def test_install_processes_all_files(project, dirs):
    files = [
        ("sys/kern/sched_petri.c", "new"),
        ("sys/sys/petri_net.h", "new"),
    ]
    for path, _ in files:
        src = dirs["patches"] / path
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("// content\n")

    write_metadata(
        dirs["patches"], "abc123", [{"path": p, "status": s} for p, s in files]
    )

    install(project, resolvers["abort"])

    for path, _ in files:
        assert (dirs["kernel"] / path).exists()
