"""
Integration tests for the fetch command.
Uses real git repos created in tmp_path — no network required.
"""

import subprocess
from pathlib import Path

import pytest

from oot.config import Project, RepoConfig
from oot.commands.fetch import fetch
from oot.errors import ConfigError, FetchError, RepoStateError


# --- Fixtures ---


def git(*args, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Shortcut para correr git en tests."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def bare_repo(tmp_path) -> Path:
    """Repo bare con un commit inicial — sirve como remote."""
    bare = tmp_path / "bare.git"
    bare.mkdir()
    git("init", "--bare", "-b", "main", str(bare))

    # Crear un commit inicial vía un repo temporal
    work = tmp_path / "work"
    work.mkdir()
    git("clone", str(bare), str(work))
    (work / "README").write_text("hello")
    git("config", "user.email", "test@test.com", cwd=work)
    git("config", "user.name", "Test", cwd=work)
    git("add", ".", cwd=work)
    git("commit", "-m", "init", cwd=work)
    git("push", "origin", "main", cwd=work)

    return bare


@pytest.fixture
def project(tmp_path, bare_repo) -> Project:
    """Project con kernel y patches apuntando al mismo bare repo local."""
    return Project(
        dir=tmp_path / ".oot",
        kernel=RepoConfig(
            url=f"file://{bare_repo}",
            ref="main",
            depth=1,
            dir=tmp_path / "kernel",
        ),
        patches=RepoConfig(
            url=f"file://{bare_repo}",
            ref="main",
            depth=1,
            dir=tmp_path / "patches",
        ),
    )


# --- fetch: dir no existe → clone ---


def test_fetch_kernel_clones_when_dir_missing(project):
    fetch(project, target="kernel", force=False)
    assert (project.kernel.dir / ".git").exists()


def test_fetch_patches_clones_when_dir_missing(project):
    fetch(project, target="patches", force=False)
    assert (project.patches.dir / ".git").exists()


def test_fetch_creates_readme(project):
    fetch(project, target="kernel", force=False)
    assert (project.kernel.dir / "README").exists()


# --- fetch: dir existe, es git, mismo origin → update ---


def test_fetch_updates_existing_repo(project):
    fetch(project, target="kernel", force=False)
    # Segunda llamada no debe fallar
    fetch(project, target="kernel", force=False)
    assert (project.kernel.dir / ".git").exists()


def test_fetch_pulls_new_commit(project, tmp_path, bare_repo):
    fetch(project, target="kernel", force=False)

    # Agregar un nuevo commit al bare repo
    work = tmp_path / "work2"
    work.mkdir()
    git("clone", str(bare_repo), str(work))
    (work / "newfile").write_text("new")
    git("config", "user.email", "test@test.com", cwd=work)
    git("config", "user.name", "Test", cwd=work)
    git("add", ".", cwd=work)
    git("commit", "-m", "add newfile", cwd=work)
    git("push", "origin", "main", cwd=work)

    fetch(project, target="kernel", force=False)
    assert (project.kernel.dir / "newfile").exists()


# --- fetch: dir existe, es git, origin distinto → ConfigError ---


def test_fetch_raises_on_different_origin(project, tmp_path):
    # Clonar desde el bare repo pero con una URL diferente a la config
    other_bare = tmp_path / "other.git"
    other_bare.mkdir()
    git("init", "--bare", "-b", "main", str(other_bare))

    dest = project.kernel.dir
    git("clone", f"file://{other_bare}", str(dest))

    with pytest.raises(ConfigError, match="different origin"):
        fetch(project, target="kernel", force=False)


# --- fetch: dir existe, no es git, está vacío → clone ---


def test_fetch_clones_into_empty_dir(project):
    project.kernel.dir.mkdir(parents=True)
    fetch(project, target="kernel", force=False)
    assert (project.kernel.dir / ".git").exists()


# --- fetch: dir existe, no es git, no está vacío, con url → RepoStateError ---


def test_fetch_raises_on_non_git_non_empty_dir(project):
    project.kernel.dir.mkdir(parents=True)
    (project.kernel.dir / "somefile").write_text("existing")

    with pytest.raises(RepoStateError, match="not a git repository"):
        fetch(project, target="kernel", force=False)


# --- fetch: dir existe, no es git, no está vacío, sin url → no hace nada ---


def test_fetch_does_nothing_when_no_url(project):
    project.kernel.dir.mkdir(parents=True)
    (project.kernel.dir / "somefile").write_text("existing")
    project.kernel.url = None

    # No debe lanzar excepción
    fetch(project, target="kernel", force=False)
    assert (project.kernel.dir / "somefile").exists()


# --- fetch --force ---


def test_fetch_force_overwrites_existing(project):
    project.kernel.dir.mkdir(parents=True)
    (project.kernel.dir / "existing").write_text("old content")

    fetch(project, target="kernel", force=True)

    assert (project.kernel.dir / ".git").exists()
    assert not (project.kernel.dir / "existing").exists()


def test_fetch_force_on_missing_dir(project):
    fetch(project, target="kernel", force=True)
    assert (project.kernel.dir / ".git").exists()


# --- fetch: git repo without origin → confirm ---


def no_origin_repo(path: Path) -> Path:
    path.mkdir()
    git("init", "-b", "main", str(path))
    return path


def test_fetch_no_origin_declined_leaves_repo_alone(project):
    no_origin_repo(project.kernel.dir)
    asked = []

    fetch(project, target="kernel", force=False, confirm=lambda msg: asked.append(msg) or False)

    assert len(asked) == 1
    r = subprocess.run(["git", "-C", str(project.kernel.dir), "remote"], capture_output=True, text=True)
    assert r.stdout.strip() == ""


def test_fetch_no_origin_accepted_adds_origin_and_updates(project):
    no_origin_repo(project.kernel.dir)

    fetch(project, target="kernel", force=False, confirm=lambda _: True)

    assert (project.kernel.dir / "README").read_text() == "hello"


def test_fetch_raises_when_clone_fails(project, tmp_path):
    project.kernel.url = f"file://{tmp_path}/does-not-exist.git"

    with pytest.raises(FetchError, match="git clone"):
        fetch(project, target="kernel", force=False)


# --- fetch: never discards local work without --force ---


def kernel_identity(project):
    git("config", "user.email", "t@t", cwd=project.kernel.dir)
    git("config", "user.name", "t", cwd=project.kernel.dir)


def test_fetch_refuses_to_reset_local_commits(project):
    fetch(project, target="kernel", force=False)
    kernel_identity(project)
    (project.kernel.dir / "work.c").write_text("work\n")
    git("add", "work.c", cwd=project.kernel.dir)
    git("commit", "-m", "work", cwd=project.kernel.dir)

    with pytest.raises(RepoStateError, match=r"(?s)1 local commit.*--force"):
        fetch(project, target="kernel", force=False)

    assert (project.kernel.dir / "work.c").exists()


def test_fetch_refuses_to_reset_uncommitted_changes(project):
    fetch(project, target="kernel", force=False)
    (project.kernel.dir / "README").write_text("edited")

    with pytest.raises(RepoStateError, match="README"):
        fetch(project, target="kernel", force=False)

    assert (project.kernel.dir / "README").read_text() == "edited"


def test_fetch_force_discards_local_work(project):
    fetch(project, target="kernel", force=False)
    (project.kernel.dir / "README").write_text("edited")

    fetch(project, target="kernel", force=True)

    assert (project.kernel.dir / "README").read_text() == "hello"


# --- fetch: tag refs ---


@pytest.fixture
def tagged(bare_repo, tmp_path):
    """Tag v1 on the initial commit, then move main forward."""
    work = tmp_path / "work-tag"
    git("clone", str(bare_repo), str(work))
    git("config", "user.email", "t@t", cwd=work)
    git("config", "user.name", "t", cwd=work)
    git("tag", "v1", cwd=work)
    (work / "README").write_text("after v1")
    git("commit", "-am", "after v1", cwd=work)
    git("push", "origin", "main", "v1", cwd=work)
    return bare_repo


def test_fetch_clones_tag(project, tagged):
    project.kernel.ref = "v1"

    fetch(project, target="kernel", force=False)

    assert (project.kernel.dir / "README").read_text() == "hello"


def test_fetch_updates_existing_clone_to_tag(project, tagged):
    fetch(project, target="kernel", force=False)  # main: "after v1"
    project.kernel.ref = "v1"

    fetch(project, target="kernel", force=False)

    assert (project.kernel.dir / "README").read_text() == "hello"
    head = git("rev-parse", "HEAD", cwd=project.kernel.dir).stdout.strip()
    tag = git("rev-parse", "refs/tags/v1^{commit}", cwd=project.kernel.dir).stdout.strip()
    assert head == tag
