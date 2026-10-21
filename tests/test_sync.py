"""
Tests for the sync command.

Strategy: kernel_dir and patches_dir are real git repos (initialized in tmp_path).
This lets us test the actual snapshot/metadata logic without mocking git internals.
subprocess.run is NOT mocked — we rely on git being available in the test environment.
"""

import json
import subprocess
from pathlib import Path

import pytest

from oot.errors import PatchError

from oot.commands.sync import sync
from oot.config import Project, RepoConfig
from oot.metadata import BASE_DIR, Metadata


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def git(path: Path, *args):
    r = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"git {args} failed:\n{r.stderr}"
    return r


def init_repo(path: Path, initial_files: dict[str, str] | None = None) -> str:
    """
    Initialize a git repo at *path* with an optional set of files.
    Returns the HEAD commit hash.
    """
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")

    if initial_files:
        for rel, content in initial_files.items():
            p = path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        git(path, "add", "-A")
        git(path, "commit", "-m", "initial")

    return git(path, "rev-parse", "HEAD").stdout.strip()


def read_metadata(patches_dir: Path) -> Metadata:
    return Metadata.model_validate_json((patches_dir / "metadata.json").read_text())


def write_metadata(patches_dir: Path, base_blob: str, files: list[dict]):
    (patches_dir / "metadata.json").write_text(
        json.dumps({"base_blob": base_blob, "files": files})
    )
    git(patches_dir, "add", "metadata.json")
    git(patches_dir, "commit", "-m", "chore: metadata")


def kernel_blob(kernel_dir: Path, rel_path: str) -> str:
    r = git(kernel_dir, "ls-tree", "HEAD", rel_path)
    return r.stdout.split()[2]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kernel(tmp_path):
    """Kernel repo with a handful of initial files."""
    kdir = tmp_path / "kernel"
    init_repo(
        kdir,
        {
            "drivers/foo.c": "int foo() { return 0; }\n",
            "drivers/bar.c": "int bar() { return 1; }\n",
            "Makefile": "all:\n\t@echo done\n",
        },
    )
    # sync reads upstream (origin/<ref>), not HEAD
    git(kdir, "update-ref", "refs/remotes/origin/main", "HEAD")
    return kdir


@pytest.fixture
def patches(tmp_path):
    """Empty patches repo."""
    pdir = tmp_path / "patches"
    init_repo(pdir, {"README.md": "patches\n"})
    return pdir


@pytest.fixture
def project(tmp_path, kernel, patches):
    return Project(
        dir=tmp_path,
        kernel=RepoConfig(ref="main", dir=kernel),
        patches=RepoConfig(ref="main", dir=patches),
    )


# ---------------------------------------------------------------------------
# Basic snapshot: new files
# ---------------------------------------------------------------------------


def test_new_file_appears_in_metadata(project, kernel, patches):
    """A file copied to patches that doesn't exist in kernel → status 'new'."""
    (patches / "drivers" / "extra.c").parent.mkdir(parents=True, exist_ok=True)
    (patches / "drivers" / "extra.c").write_text("int extra() {}\n")

    sync(project, "add extra.c")

    meta = read_metadata(patches)
    paths = {f.path: f for f in meta.files}
    assert "drivers/extra.c" in paths
    assert paths["drivers/extra.c"].status == "new"
    assert paths["drivers/extra.c"].base_blob is None


def test_modified_file_appears_in_metadata(project, kernel, patches):
    """A file that exists in kernel but has different content → status 'modified'."""
    (patches / "drivers").mkdir(parents=True, exist_ok=True)
    (patches / "drivers" / "foo.c").write_text("int foo() { return 42; }\n")

    sync(project, "patch foo.c")

    meta = read_metadata(patches)
    paths = {f.path: f for f in meta.files}
    assert "drivers/foo.c" in paths
    assert paths["drivers/foo.c"].status == "modified"


def test_modified_file_stores_kernel_blob(project, kernel, patches):
    """base_blob for a modified file must point to the kernel blob at sync time."""
    (patches / "drivers").mkdir(parents=True, exist_ok=True)
    (patches / "drivers" / "foo.c").write_text("int foo() { return 99; }\n")

    sync(project, "patch foo.c")

    expected_blob = kernel_blob(kernel, "drivers/foo.c")
    meta = read_metadata(patches)
    entry = next(f for f in meta.files if f.path == "drivers/foo.c")
    assert entry.base_blob == expected_blob


def test_file_identical_to_kernel_is_not_in_metadata(project, kernel, patches):
    """A file copied verbatim from kernel has no patch effect → not included."""
    original = (kernel / "drivers" / "foo.c").read_text()
    (patches / "drivers").mkdir(parents=True, exist_ok=True)
    (patches / "drivers" / "foo.c").write_text(original)

    sync(project, "no-op copy")

    meta = read_metadata(patches)
    paths = {f.path for f in meta.files}
    assert "drivers/foo.c" not in paths


def test_metadata_kernel_commit_is_upstream(project, kernel, patches):
    """The top-level kernel_commit must equal origin/<ref>."""
    (patches / "drivers").mkdir(parents=True, exist_ok=True)
    (patches / "drivers" / "extra.c").write_text("new\n")

    sync(project, "snapshot")

    expected = git(kernel, "rev-parse", "origin/main").stdout.strip()
    meta = read_metadata(patches)
    assert meta.kernel_commit == expected


# ---------------------------------------------------------------------------
# Basic snapshot: deleted files
# ---------------------------------------------------------------------------


def test_deleted_file_from_prior_metadata_appears_as_deleted(project, kernel, patches):
    """
    A file that was 'modified' in prior metadata but is no longer on disk
    → status 'deleted'.
    """
    blob = kernel_blob(kernel, "drivers/foo.c")
    write_metadata(
        patches,
        git(kernel, "rev-parse", "HEAD").stdout.strip(),
        [{"path": "drivers/foo.c", "base_blob": blob, "status": "modified"}],
    )
    # Remove from patches (simulate user deleting the file)
    # patches/drivers/foo.c was never actually created, so it's already absent.

    sync(project, "delete foo.c")

    meta = read_metadata(patches)
    paths = {f.path: f for f in meta.files}
    assert "drivers/foo.c" in paths
    assert paths["drivers/foo.c"].status == "deleted"


# ---------------------------------------------------------------------------
# Git integration
# ---------------------------------------------------------------------------


def test_sync_creates_a_commit(project, kernel, patches):
    """sync must produce exactly one new commit in the patches repo."""
    before = git(patches, "rev-parse", "HEAD").stdout.strip()

    (patches / "newfile.c").write_text("new\n")
    sync(project, "my commit message")

    after = git(patches, "rev-parse", "HEAD").stdout.strip()
    assert before != after


def test_sync_commit_uses_provided_message(project, kernel, patches):
    """The commit message must match the argument passed to sync."""
    (patches / "newfile.c").write_text("new\n")
    sync(project, "feat: my specific message")

    log = git(patches, "log", "--format=%s", "-1").stdout.strip()
    assert log == "feat: my specific message"


def test_sync_does_not_commit_when_nothing_changed(project, kernel, patches):
    """If patches_dir is clean and metadata didn't change, no commit is made."""
    # First sync to establish a baseline
    (patches / "drivers").mkdir(parents=True, exist_ok=True)
    (patches / "drivers" / "foo.c").write_text("int foo() { return 42; }\n")
    sync(project, "first sync")

    commit_before = git(patches, "rev-parse", "HEAD").stdout.strip()

    # Second sync with no changes
    sync(project, "second sync")

    commit_after = git(patches, "rev-parse", "HEAD").stdout.strip()
    assert commit_before == commit_after


def test_sync_stages_new_files(project, kernel, patches):
    """All new files in patches_dir must be tracked after sync."""
    (patches / "drivers").mkdir(parents=True, exist_ok=True)
    (patches / "drivers" / "new.c").write_text("new\n")

    sync(project, "add new.c")

    tracked = git(patches, "ls-files").stdout.splitlines()
    assert "drivers/new.c" in tracked


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_dry_run_does_not_write_metadata(project, kernel, patches, capsys):
    """--dry-run must not modify metadata.json."""
    (patches / "drivers").mkdir(parents=True, exist_ok=True)
    (patches / "drivers" / "foo.c").write_text("int foo() { return 42; }\n")

    before = (
        (patches / "metadata.json").read_text()
        if (patches / "metadata.json").exists()
        else None
    )

    sync(project, "dry", dry_run=True)

    after = (
        (patches / "metadata.json").read_text()
        if (patches / "metadata.json").exists()
        else None
    )
    assert before == after


def test_dry_run_does_not_commit(project, kernel, patches):
    """--dry-run must not create a new commit."""
    (patches / "newfile.c").write_text("new\n")
    before = git(patches, "rev-parse", "HEAD").stdout.strip()

    sync(project, "dry", dry_run=True)

    after = git(patches, "rev-parse", "HEAD").stdout.strip()
    assert before == after


def test_dry_run_prints_metadata_json(project, kernel, patches, capsys):
    """--dry-run must print the would-be metadata to stdout."""
    (patches / "drivers").mkdir(parents=True, exist_ok=True)
    (patches / "drivers" / "foo.c").write_text("int foo() { return 42; }\n")

    sync(project, "dry", dry_run=True)

    captured = capsys.readouterr().out
    parsed = json.loads(captured)
    assert "kernel_commit" in parsed
    assert "files" in parsed


# ---------------------------------------------------------------------------
# Consolidation: net-zero changes collapse
# ---------------------------------------------------------------------------


def test_new_file_added_then_removed_is_not_in_metadata(project, kernel, patches):
    """
    If a file was 'new' in the prior metadata but has since been removed from
    patches_dir, it must not appear in the new metadata at all.
    """
    # First sync: add a new file
    (patches / "drivers").mkdir(parents=True, exist_ok=True)
    (patches / "drivers" / "extra.c").write_text("new\n")
    sync(project, "add extra.c")

    # Remove the file and sync again
    (patches / "drivers" / "extra.c").unlink()
    sync(project, "remove extra.c")

    meta = read_metadata(patches)
    paths = {f.path for f in meta.files}
    assert "drivers/extra.c" not in paths


def test_deleted_file_restored_to_kernel_content_is_not_in_metadata(
    project, kernel, patches
):
    """
    A file that was marked 'deleted' in prior metadata but is back on disk
    with the exact same content as the kernel → net zero, no entry.
    """
    blob = kernel_blob(kernel, "drivers/foo.c")
    write_metadata(
        patches,
        git(kernel, "rev-parse", "HEAD").stdout.strip(),
        [{"path": "drivers/foo.c", "base_blob": blob, "status": "deleted"}],
    )

    # Restore the file with the kernel's original content
    original = (kernel / "drivers" / "foo.c").read_text()
    (patches / "drivers").mkdir(parents=True, exist_ok=True)
    (patches / "drivers" / "foo.c").write_text(original)

    sync(project, "restore foo.c")

    meta = read_metadata(patches)
    paths = {f.path for f in meta.files}
    assert "drivers/foo.c" not in paths


def test_modified_file_restored_to_kernel_content_is_not_in_metadata(
    project, kernel, patches
):
    """
    A file that was 'modified' in prior metadata, then brought back to the
    exact kernel content, must vanish from metadata.
    """
    # First sync with a modification
    (patches / "drivers").mkdir(parents=True, exist_ok=True)
    (patches / "drivers" / "foo.c").write_text("int foo() { return 42; }\n")
    sync(project, "modify foo.c")

    # Restore to kernel content
    original = (kernel / "drivers" / "foo.c").read_text()
    (patches / "drivers" / "foo.c").write_text(original)
    sync(project, "restore foo.c")

    meta = read_metadata(patches)
    paths = {f.path for f in meta.files}
    assert "drivers/foo.c" not in paths


# ---------------------------------------------------------------------------
# Consolidation: base_blob stability across syncs
# ---------------------------------------------------------------------------


def test_base_blob_preserved_across_resync(project, kernel, patches):
    """
    If a modified file is synced a second time with different content,
    its base_blob must still point to the original kernel blob, not the
    intermediate one — so install can still compute a valid diff.
    """
    (patches / "drivers").mkdir(parents=True, exist_ok=True)
    (patches / "drivers" / "foo.c").write_text("int foo() { return 42; }\n")
    sync(project, "first patch")

    expected_blob = kernel_blob(kernel, "drivers/foo.c")

    # Change the patch again
    (patches / "drivers" / "foo.c").write_text("int foo() { return 99; }\n")
    sync(project, "second patch")

    meta = read_metadata(patches)
    entry = next(f for f in meta.files if f.path == "drivers/foo.c")
    assert entry.base_blob == expected_blob


# ---------------------------------------------------------------------------
# Multiple files in a single sync
# ---------------------------------------------------------------------------


def test_sync_handles_multiple_files(project, kernel, patches):
    """sync must correctly classify several files in a single pass."""
    foo_blob = kernel_blob(kernel, "drivers/foo.c")
    bar_blob = kernel_blob(kernel, "drivers/bar.c")

    # Prior metadata tracks both foo.c (modified) and bar.c (modified).
    # After this sync: new.c is added, bar.c gets a new change, foo.c disappears.
    write_metadata(
        patches,
        git(kernel, "rev-parse", "HEAD").stdout.strip(),
        [
            {"path": "drivers/foo.c", "base_blob": foo_blob, "status": "modified"},
            {"path": "drivers/bar.c", "base_blob": bar_blob, "status": "modified"},
        ],
    )

    (patches / "drivers").mkdir(parents=True, exist_ok=True)
    # new file — not in kernel
    (patches / "drivers" / "new.c").write_text("new\n")
    # re-patched (different content from kernel)
    (patches / "drivers" / "bar.c").write_text("int bar() { return 99; }\n")
    # foo.c intentionally absent → should become "deleted"

    sync(project, "multi-file sync")

    meta = read_metadata(patches)
    by_path = {f.path: f for f in meta.files}

    assert by_path["drivers/new.c"].status == "new"
    assert by_path["drivers/bar.c"].status == "modified"
    assert by_path["drivers/foo.c"].status == "deleted"


# ---------------------------------------------------------------------------
# Upstream, not HEAD
# ---------------------------------------------------------------------------


def commit_to_kernel_head(kernel: Path, rel: str, content: str):
    """Simulate oot install: commit on HEAD without moving origin/main."""
    (kernel / rel).write_text(content)
    git(kernel, "commit", "-am", "installed patches")


def test_sync_after_install_still_records_modified(project, kernel, patches):
    (patches / "drivers").mkdir(parents=True, exist_ok=True)
    (patches / "drivers" / "foo.c").write_text("int foo() { return 42; }\n")
    commit_to_kernel_head(kernel, "drivers/foo.c", "int foo() { return 42; }\n")

    sync(project, "after install")

    entry = next(f for f in read_metadata(patches).files if f.path == "drivers/foo.c")
    assert entry.status == "modified"
    assert (patches / BASE_DIR / "drivers/foo.c").read_text() == "int foo() { return 0; }\n"


def test_sync_raises_without_upstream_ref(project, kernel, patches):
    git(kernel, "update-ref", "-d", "refs/remotes/origin/main")
    (patches / "x.c").write_text("x\n")

    with pytest.raises(PatchError, match="origin/main"):
        sync(project, "no upstream")


# ---------------------------------------------------------------------------
# Base copies
# ---------------------------------------------------------------------------


def test_modified_file_gets_base_copy_of_upstream(project, kernel, patches):
    (patches / "drivers").mkdir(parents=True, exist_ok=True)
    (patches / "drivers" / "foo.c").write_text("int foo() { return 42; }\n")

    sync(project, "patch foo.c")

    base = patches / BASE_DIR / "drivers/foo.c"
    assert base.read_text() == "int foo() { return 0; }\n"
    assert ".oot/base/drivers/foo.c" in git(patches, "ls-files").stdout.splitlines()


def test_base_copies_are_not_patches(project, kernel, patches):
    (patches / "drivers").mkdir(parents=True, exist_ok=True)
    (patches / "drivers" / "foo.c").write_text("int foo() { return 42; }\n")
    sync(project, "first")
    sync(project, "second")

    assert all(not f.path.startswith(".oot") for f in read_metadata(patches).files)


def test_base_copy_kept_when_upstream_moves(project, kernel, patches):
    (patches / "drivers").mkdir(parents=True, exist_ok=True)
    (patches / "drivers" / "foo.c").write_text("int foo() { return 42; }\n")
    sync(project, "first")
    original_blob = kernel_blob(kernel, "drivers/foo.c")

    (kernel / "drivers" / "foo.c").write_text("int foo() { return -1; }\n")
    git(kernel, "commit", "-am", "upstream change")
    git(kernel, "update-ref", "refs/remotes/origin/main", "HEAD")
    (patches / "drivers" / "foo.c").write_text("int foo() { return 99; }\n")
    sync(project, "second")

    entry = next(f for f in read_metadata(patches).files if f.path == "drivers/foo.c")
    assert entry.base_blob == original_blob
    assert (patches / BASE_DIR / "drivers/foo.c").read_text() == "int foo() { return 0; }\n"


def test_base_copy_removed_when_file_matches_upstream_again(project, kernel, patches):
    (patches / "drivers").mkdir(parents=True, exist_ok=True)
    (patches / "drivers" / "foo.c").write_text("int foo() { return 42; }\n")
    sync(project, "modify")

    (patches / "drivers" / "foo.c").write_text((kernel / "drivers" / "foo.c").read_text())
    sync(project, "restore")

    assert not (patches / BASE_DIR / "drivers/foo.c").exists()


def test_base_copy_removed_when_file_becomes_deleted(project, kernel, patches):
    (patches / "drivers").mkdir(parents=True, exist_ok=True)
    (patches / "drivers" / "foo.c").write_text("int foo() { return 42; }\n")
    sync(project, "modify")

    (patches / "drivers" / "foo.c").unlink()
    sync(project, "delete")

    assert not (patches / BASE_DIR / "drivers/foo.c").exists()


def test_missing_base_copy_is_backfilled_from_recorded_blob(project, kernel, patches):
    """Old metadata (no base copies) gets them on the next sync."""
    blob = kernel_blob(kernel, "drivers/foo.c")
    write_metadata(
        patches,
        git(kernel, "rev-parse", "HEAD").stdout.strip(),
        [{"path": "drivers/foo.c", "base_blob": blob, "status": "modified"}],
    )
    (patches / "drivers").mkdir(parents=True, exist_ok=True)
    (patches / "drivers" / "foo.c").write_text("int foo() { return 42; }\n")

    sync(project, "backfill")

    assert (patches / BASE_DIR / "drivers/foo.c").read_text() == "int foo() { return 0; }\n"


def test_missing_base_blob_object_raises(project, kernel, patches):
    write_metadata(
        patches,
        git(kernel, "rev-parse", "HEAD").stdout.strip(),
        [{"path": "drivers/foo.c", "base_blob": "0" * 40, "status": "modified"}],
    )
    (patches / "drivers").mkdir(parents=True, exist_ok=True)
    (patches / "drivers" / "foo.c").write_text("int foo() { return 42; }\n")

    with pytest.raises(PatchError, match="drivers/foo.c"):
        sync(project, "no blob")


def test_dry_run_does_not_write_base_copies(project, kernel, patches, capsys):
    (patches / "drivers").mkdir(parents=True, exist_ok=True)
    (patches / "drivers" / "foo.c").write_text("int foo() { return 42; }\n")

    sync(project, "dry", dry_run=True)

    assert not (patches / BASE_DIR).exists()


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_sync_raises_when_patches_is_not_a_git_repo(project, tmp_path):
    """sync must raise if patches_dir is not a git repository."""
    non_repo = tmp_path / "not_a_repo"
    non_repo.mkdir()

    bad_project = Project(
        dir=tmp_path,
        kernel=RepoConfig(ref="main", dir=project.kernel.dir),
        patches=RepoConfig(ref="main", dir=non_repo),
    )

    with pytest.raises(PatchError, match="not a git repository"):
        sync(bad_project, "should fail")


def test_ootignored_files_are_not_recorded(project, kernel, patches):
    """The patches fixture has a README.md; .ootignore keeps it out of metadata."""
    (patches / ".ootignore").write_text("/README.md\n")
    (patches / "extra.c").write_text("x\n")

    sync(project, "with ootignore")

    assert {f.path for f in read_metadata(patches).files} == {"extra.c"}
