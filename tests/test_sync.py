"""
Tests for the sync command (kernel repo -> patches repo).

Strategy: an "upstream" repo tagged v1 is cloned into kernel_dir, as oot fetch
would; work is committed on top. All git calls are real.
"""

import subprocess
from pathlib import Path

import pytest

from oot.commands.install import install, resolvers
from oot.commands.sync import sync
from oot.config import Project, RepoConfig
from oot.errors import PatchError
from oot.metadata import BASE_DIR, Metadata


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def git(path: Path, *args) -> str:
    r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)
    assert r.returncode == 0, f"git {args} failed:\n{r.stderr}"
    return r.stdout.strip()


def identity(path: Path):
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")


def write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def commit(repo: Path, files: dict[str, str | None], message: str = "work"):
    """Write (or delete, for None) files and commit them."""
    for rel, content in files.items():
        if content is None:
            git(repo, "rm", "-q", rel)
        else:
            write(repo / rel, content)
            git(repo, "add", rel)
    git(repo, "commit", "-qm", message)


def read_metadata(patches_dir: Path) -> Metadata:
    return Metadata.load(patches_dir / "metadata.json")


def entries(patches_dir: Path) -> dict[str, str]:
    return {f.path: f.status for f in read_metadata(patches_dir).files}


def never_called(msg):
    raise AssertionError(f"confirm called: {msg}")


UPSTREAM_FILES = {
    "sys/kern/sched_4bsd.c": "sched v1\n",
    "sys/kern/kern_exec.c": "exec v1\n",
    "sys/kern/old.c": "old v1\n",
    "Makefile": "all:\n",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def upstream(tmp_path):
    up = tmp_path / "upstream"
    up.mkdir()
    git(up, "init", "-q", "-b", "main")
    identity(up)
    commit(up, UPSTREAM_FILES, "v1")
    git(up, "tag", "v1")
    return up


def clone(upstream: Path, dest: Path) -> Path:
    subprocess.run(
        ["git", "clone", "-q", "--branch", "v1", f"file://{upstream}", str(dest)],
        capture_output=True,
        check=True,
    )
    identity(dest)
    return dest


@pytest.fixture
def kernel(tmp_path, upstream):
    return clone(upstream, tmp_path / "kernel")


@pytest.fixture
def patches(tmp_path):
    return tmp_path / "patches"


def make_project(tmp_path, kernel, patches, ref="v1"):
    return Project(
        dir=tmp_path,
        kernel=RepoConfig(ref=ref, dir=kernel),
        patches=RepoConfig(ref="main", dir=patches),
    )


@pytest.fixture
def project(tmp_path, kernel, patches):
    return make_project(tmp_path, kernel, patches)


@pytest.fixture
def worked(kernel):
    """Kernel with one commit that adds, modifies and deletes a file."""
    commit(
        kernel,
        {
            "sys/kern/sched_petri.c": "petri\n",
            "sys/kern/sched_4bsd.c": "sched v1 + petri\n",
            "sys/kern/old.c": None,
        },
    )
    return kernel


# ---------------------------------------------------------------------------
# First sync
# ---------------------------------------------------------------------------


def test_first_sync_records_new_modified_and_deleted(project, worked, patches):
    sync(project, "extract")

    assert entries(patches) == {
        "sys/kern/sched_petri.c": "new",
        "sys/kern/sched_4bsd.c": "modified",
        "sys/kern/old.c": "deleted",
    }


def test_first_sync_copies_committed_content(project, worked, patches):
    sync(project, "extract")

    assert (patches / "sys/kern/sched_petri.c").read_text() == "petri\n"
    assert (patches / "sys/kern/sched_4bsd.c").read_text() == "sched v1 + petri\n"
    assert not (patches / "sys/kern/old.c").exists()


def test_first_sync_writes_base_copy_and_blob_from_base(project, worked, patches, upstream):
    sync(project, "extract")

    rel = "sys/kern/sched_4bsd.c"
    assert (patches / BASE_DIR / rel).read_text() == "sched v1\n"
    entry = next(f for f in read_metadata(patches).files if f.path == rel)
    assert entry.base_blob == git(upstream, "rev-parse", f"v1:{rel}")
    assert not (patches / BASE_DIR / "sys/kern/sched_petri.c").exists()


def test_kernel_commit_is_the_base(project, worked, patches, upstream):
    sync(project, "extract")

    assert read_metadata(patches).kernel_commit == git(upstream, "rev-parse", "v1^{commit}")


def test_first_sync_initialises_patches_repo_and_commits(project, worked, patches):
    sync(project, "feat: petri scheduler")

    assert git(patches, "log", "--format=%s") == "feat: petri scheduler"
    assert git(patches, "status", "--porcelain") == ""


def test_patches_dir_that_is_not_a_repo_and_not_empty_raises(project, worked, patches):
    write(patches / "stray.txt", "x\n")

    with pytest.raises(PatchError, match="not a git repository"):
        sync(project, "extract")


def test_rename_is_recorded_as_new_plus_deleted(project, kernel, patches):
    git(kernel, "mv", "sys/kern/old.c", "sys/kern/renamed.c")
    git(kernel, "commit", "-qm", "rename")

    sync(project, "extract")

    assert entries(patches) == {"sys/kern/renamed.c": "new", "sys/kern/old.c": "deleted"}


def test_changes_across_several_commits_are_combined(project, kernel, patches):
    commit(kernel, {"sys/kern/a.c": "a\n"}, "one")
    commit(kernel, {"sys/kern/a.c": "a2\n", "sys/kern/kern_exec.c": "exec v1 + x\n"}, "two")

    sync(project, "extract")

    assert entries(patches) == {"sys/kern/a.c": "new", "sys/kern/kern_exec.c": "modified"}
    assert (patches / "sys/kern/a.c").read_text() == "a2\n"


# ---------------------------------------------------------------------------
# Later syncs: the patch set mirrors the kernel diff
# ---------------------------------------------------------------------------


def test_later_sync_adds_new_work_in_one_commit(project, worked, patches):
    sync(project, "first")
    commit(worked, {"sys/kern/kern_exec.c": "exec v1 + petri\n"})

    sync(project, "second")

    assert entries(patches)["sys/kern/kern_exec.c"] == "modified"
    assert git(patches, "log", "--format=%s").splitlines() == ["second", "first"]


def test_file_reverted_in_kernel_is_removed_from_patch_set(project, worked, patches):
    sync(project, "first")
    commit(worked, {"sys/kern/sched_4bsd.c": "sched v1\n"}, "revert")

    sync(project, "second")

    assert "sys/kern/sched_4bsd.c" not in entries(patches)
    assert not (patches / "sys/kern/sched_4bsd.c").exists()
    assert not (patches / BASE_DIR / "sys/kern/sched_4bsd.c").exists()


def test_stray_file_in_patches_repo_is_removed(project, worked, patches):
    sync(project, "first")
    write(patches / "sys/kern/hand_edit.c", "x\n")

    sync(project, "second")

    assert not (patches / "sys/kern/hand_edit.c").exists()


def test_no_commit_when_nothing_changed(project, worked, patches):
    sync(project, "first")
    before = git(patches, "rev-parse", "HEAD")

    sync(project, "second")

    assert git(patches, "rev-parse", "HEAD") == before


# ---------------------------------------------------------------------------
# .ootignore
# ---------------------------------------------------------------------------


def test_ootignored_files_are_left_alone(project, worked, patches):
    sync(project, "first")
    write(patches / ".ootignore", "/README.md\n")
    write(patches / "README.md", "my patches\n")

    sync(project, "second")

    assert (patches / "README.md").read_text() == "my patches\n"
    assert "README.md" not in entries(patches)


def test_kernel_change_to_ootignored_path_raises(project, kernel, patches):
    write(patches / ".ootignore", "/Makefile\n")
    git(patches.parent, "init", "-q", str(patches))
    commit(kernel, {"Makefile": "all: petri\n"})

    with pytest.raises(PatchError, match="Makefile"):
        sync(project, "extract")


# ---------------------------------------------------------------------------
# Base commit
# ---------------------------------------------------------------------------


def test_branch_base_is_fork_point_when_upstream_moved(tmp_path, upstream, patches):
    kernel = tmp_path / "kernel"
    subprocess.run(
        ["git", "clone", "-q", "--branch", "main", f"file://{upstream}", str(kernel)],
        check=True,
    )
    identity(kernel)
    fork_point = git(kernel, "rev-parse", "HEAD")
    commit(kernel, {"sys/kern/sched_4bsd.c": "sched v1 + petri\n"})
    # upstream moves on after the fork, then the kernel fetches it
    commit(upstream, {"sys/kern/sched_4bsd.c": "sched v2\n"}, "upstream v2")
    git(kernel, "fetch", "-q", "origin")

    sync(make_project(tmp_path, kernel, patches, ref="main"), "extract")

    assert read_metadata(patches).kernel_commit == fork_point
    assert (patches / BASE_DIR / "sys/kern/sched_4bsd.c").read_text() == "sched v1\n"


def test_unknown_ref_raises(tmp_path, worked, patches):
    with pytest.raises(PatchError, match="nope"):
        sync(make_project(tmp_path, worked, patches, ref="nope"), "extract")


def test_no_common_history_raises(project, worked, patches):
    git(worked, "checkout", "-q", "--orphan", "unrelated")
    git(worked, "commit", "-qm", "orphan")

    with pytest.raises(PatchError, match="common history"):
        sync(project, "extract")


# ---------------------------------------------------------------------------
# Uncommitted changes in the kernel
# ---------------------------------------------------------------------------


def test_clean_kernel_does_not_ask(project, worked, patches):
    sync(project, "extract", confirm=never_called)


def test_dirty_kernel_asks_and_stops_when_declined(project, worked, patches):
    write(worked / "sys/kern/kern_exec.c", "uncommitted\n")
    write(worked / "sys/kern/untracked.c", "untracked\n")
    asked = []

    sync(project, "extract", confirm=lambda msg: asked.append(msg) or False)

    assert "sys/kern/kern_exec.c" in asked[0]
    assert "sys/kern/untracked.c" in asked[0]
    assert not patches.exists()


def test_dirty_kernel_syncs_committed_state_when_accepted(project, worked, patches):
    write(worked / "sys/kern/kern_exec.c", "uncommitted\n")
    write(worked / "sys/kern/untracked.c", "untracked\n")
    git(worked, "add", "sys/kern/untracked.c")  # staged but uncommitted

    sync(project, "extract", confirm=lambda _: True)

    assert "sys/kern/kern_exec.c" not in entries(patches)
    assert "sys/kern/untracked.c" not in entries(patches)


def test_gitignored_kernel_files_are_not_dirty(project, worked, patches):
    commit(worked, {".gitignore": "*.o\n"})
    write(worked / "sys/kern/sched.o", "obj\n")

    sync(project, "extract", confirm=never_called)


# ---------------------------------------------------------------------------
# Guard: removing most of the patch set
# ---------------------------------------------------------------------------


@pytest.fixture
def synced_then_fresh(tmp_path, upstream, worked, patches):
    """A patch set of 3, and a fresh kernel clone without it installed."""
    sync(make_project(tmp_path, worked, patches), "first")
    fresh = clone(upstream, tmp_path / "fresh")
    return make_project(tmp_path, fresh, patches)


def test_removing_most_patches_asks_and_stops_when_declined(synced_then_fresh, patches):
    before = git(patches, "rev-parse", "HEAD")
    asked = []

    sync(synced_then_fresh, "oops", confirm=lambda msg: asked.append(msg) or False)

    assert "remove 3 of 3" in asked[0]
    assert "oot install" in asked[0]
    assert git(patches, "rev-parse", "HEAD") == before
    assert (patches / "sys/kern/sched_petri.c").exists()


def test_removing_most_patches_proceeds_when_accepted(synced_then_fresh, patches):
    sync(synced_then_fresh, "really", confirm=lambda _: True)

    assert read_metadata(patches).files == []
    assert not (patches / "sys/kern/sched_petri.c").exists()


def test_removing_a_minority_does_not_ask(project, worked, patches):
    sync(project, "first")
    commit(worked, {"sys/kern/sched_4bsd.c": "sched v1\n"}, "revert one")

    sync(project, "second", confirm=never_called)

    assert "sys/kern/sched_4bsd.c" not in entries(patches)


def test_removing_exactly_half_does_not_ask(project, kernel, patches):
    commit(kernel, {"sys/kern/a.c": "a\n", "sys/kern/b.c": "b\n"})
    sync(project, "first")
    commit(kernel, {"sys/kern/b.c": None})

    sync(project, "second", confirm=never_called)


def test_dry_run_warns_about_removing_most_patches(synced_then_fresh, capsys):
    capsys.readouterr()

    sync(synced_then_fresh, "x", dry_run=True, confirm=never_called)

    assert "would remove 3 of 3" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Dry run (plan)
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing(project, worked, patches, capsys):
    sync(project, "extract", dry_run=True)

    assert not patches.exists()


def test_dry_run_plan_for_first_sync(project, worked, patches, capsys):
    sync(project, "extract", dry_run=True)

    out = capsys.readouterr().out
    assert "+ sys/kern/sched_petri.c" in out
    assert "+ sys/kern/sched_4bsd.c" in out
    assert "+ sys/kern/old.c" in out
    assert "Plan: 3 to add, 0 to update, 0 to remove." in out


def test_dry_run_plan_for_later_sync(project, worked, patches, capsys):
    sync(project, "first")
    commit(
        worked,
        {
            "sys/kern/sched_petri.c": "petri 2\n",
            "sys/kern/sched_4bsd.c": "sched v1\n",
            "sys/kern/kern_exec.c": "exec v1 + x\n",
        },
    )
    capsys.readouterr()

    sync(project, "second", dry_run=True)

    out = capsys.readouterr().out
    assert "+ sys/kern/kern_exec.c" in out
    assert "~ sys/kern/sched_petri.c" in out
    assert "- sys/kern/sched_4bsd.c" in out
    assert "sys/kern/old.c" not in out
    assert "Plan: 1 to add, 1 to update, 1 to remove. Unchanged: 1." in out


def test_dry_run_warns_about_uncommitted_changes_without_asking(
    project, worked, patches, capsys
):
    write(worked / "sys/kern/kern_exec.c", "uncommitted\n")

    sync(project, "extract", dry_run=True, confirm=never_called)

    out = capsys.readouterr().out
    assert "not synced" in out
    assert "sys/kern/kern_exec.c" in out


# ---------------------------------------------------------------------------
# Round trip: the two-group workflow
# ---------------------------------------------------------------------------


def tree(repo: Path) -> dict[str, str]:
    files = git(repo, "ls-files").splitlines()
    return {f: (repo / f).read_text() for f in files}


def test_second_group_installs_continues_and_syncs(tmp_path, upstream, worked, patches):
    # group 1 extracts its work
    sync(make_project(tmp_path, worked, patches), "group 1")

    # group 2 starts from a fresh clone, installs, and keeps working
    kernel2 = clone(upstream, tmp_path / "kernel2")
    project2 = make_project(tmp_path, kernel2, patches)
    install(project2, resolvers["abort"])
    commit(kernel2, {"sys/kern/kern_exec.c": "exec v1 + group 2\n"}, "group 2 work")
    sync(project2, "group 2")

    assert entries(patches) == {
        "sys/kern/sched_petri.c": "new",
        "sys/kern/sched_4bsd.c": "modified",
        "sys/kern/old.c": "deleted",
        "sys/kern/kern_exec.c": "modified",
    }

    # a third clone with the patches installed matches group 2's tree
    kernel3 = clone(upstream, tmp_path / "kernel3")
    install(make_project(tmp_path, kernel3, patches), resolvers["abort"])
    assert tree(kernel3) == tree(kernel2)
