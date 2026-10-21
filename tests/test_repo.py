from unittest.mock import MagicMock, patch

import pytest

from oot.errors import ConfigError, FetchError
from oot.git import Repo


def make_result(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr

    return m


# --- is_git_repo ---


def test_is_git_repo_true(tmp_path):
    repo = Repo(tmp_path, None)
    with patch("subprocess.run", return_value=make_result(0)):
        assert repo.is_git_repo() is True


def test_is_git_repo_false(tmp_path):
    repo = Repo(tmp_path, None)
    with patch("subprocess.run", return_value=make_result(1)):
        assert repo.is_git_repo() is False


# --- get_origin ---


def test_get_origin_returns_url(tmp_path):
    origin = "https://example.com/repo.git"
    repo = Repo(tmp_path, origin)
    with patch("subprocess.run", return_value=make_result(0, stdout=f"{origin}\n")):
        assert repo.get_origin() == origin


def test_get_origin_returns_none_on_failure(tmp_path):
    repo = Repo(tmp_path, None)
    with patch("subprocess.run", return_value=make_result(128)):
        assert repo.get_origin() is None


# --- set_origin ---


def test_set_origin_calls_git(tmp_path):
    origin = "https://example.com/repo.git"
    repo = Repo(tmp_path, origin)
    with patch("subprocess.run", return_value=make_result(0)) as mock_run:
        repo.set_origin(origin)
        args = mock_run.call_args[0][0]
        assert "remote" in args
        assert "add" in args
        assert "origin" in args
        assert origin in args


# --- update ---


def test_update_same_origin_calls_update_repo(tmp_path):
    origin = "https://example.com/repo.git"
    repo = Repo(tmp_path, origin)
    with (
        patch.object(repo, "get_origin", return_value=origin),
        patch.object(repo, "remote_has_tag", return_value=False),
        patch.object(repo, "_update_repo") as mock_update,
    ):
        repo.update(ref="main", depth=1)
        mock_update.assert_called_once_with("main", 1, tag=False)


def test_update_different_origin_raises(tmp_path):
    repo = Repo(tmp_path, "https://example.com/repo.git")
    with patch.object(repo, "get_origin", return_value="https://other.com/repo.git"):
        with pytest.raises(ConfigError, match="different origin"):
            repo.update(ref="main", depth=1)


# --- Repo._update_repo ---


def test_update_repo_fetch_fails(tmp_path):
    repo = Repo(tmp_path, "https://example.com/repo.git")

    with patch("subprocess.run", return_value=make_result(1, stderr="auth failed")):
        with pytest.raises(FetchError, match="git fetch failed"):
            repo._update_repo(ref="main")


def test_update_repo_reset_fails(tmp_path):
    repo = Repo(tmp_path, "https://example.com/repo.git")

    results = [
        make_result(0),  # fetch ok
        make_result(1, stderr="bad ref"),  # reset falla
    ]

    with patch("subprocess.run", side_effect=results):
        with pytest.raises(FetchError, match="git reset --hard origin/main failed"):
            repo._update_repo(ref="main")


def test_update_repo_clean_fails(tmp_path):
    repo = Repo(tmp_path, "https://example.com/repo.git")

    results = [
        make_result(0),  # fetch ok
        make_result(0),  # reset ok
        make_result(1, stderr="cannot clean"),  # clean falla
    ]

    with patch("subprocess.run", side_effect=results):
        with pytest.raises(FetchError, match="git clean failed"):
            repo._update_repo(ref="main")


def test_update_repo_calls_correct_git_commands_with_depth(tmp_path):
    repo = Repo(tmp_path, "https://example.com/repo.git")

    with patch("subprocess.run", return_value=make_result(0)) as mock_run:
        repo._update_repo(ref="main", depth=1)

    calls = [call.args[0] for call in mock_run.call_args_list]

    # fetch origin main --depth 1
    assert any(
        "fetch" in call
        and "origin" in call
        and "+refs/heads/main:refs/remotes/origin/main" in call
        and "--depth" in call
        and "1" in call
        for call in calls
    )

    # reset --hard origin/main
    assert any(
        "reset" in call and "--hard" in call and "origin/main" in call for call in calls
    )

    # clean -fd
    assert any("clean" in call and "-fd" in call for call in calls)


def test_update_repo_calls_correct_git_commands_without_depth(tmp_path):
    repo = Repo(tmp_path, "https://example.com/repo.git")

    with patch("subprocess.run", return_value=make_result(0)) as mock_run:
        repo._update_repo(ref="main")

    calls = [call.args[0] for call in mock_run.call_args_list]

    # fetch sin --depth
    assert any(
        "fetch" in call
        and "origin" in call
        and "+refs/heads/main:refs/remotes/origin/main" in call
        and "--depth" not in call
        for call in calls
    )


# --- clone ---


def test_clone_calls_git_clone(tmp_path):
    repo = Repo(tmp_path / "dest", "https://example.com/repo.git")
    with patch("subprocess.run", return_value=make_result(0)) as mock_run:
        repo.clone(ref="main", depth=1)
        args = mock_run.call_args[0][0]
        assert "clone" in args
        assert "--branch" in args
        assert "main" in args
        assert "https://example.com/repo.git" in [str(a) for a in args]


def test_clone_force_removes_existing(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "somefile").write_text("existing content")

    repo = Repo(dest, "https://example.com/repo.git")
    with patch("subprocess.run", return_value=make_result(0)):
        repo.clone(ref="main", depth=1, force=True)

    # El directorio fue borrado y recreado por clone → no tiene somefile
    assert not (dest / "somefile").exists()


def test_clone_force_on_empty_dir_does_not_fail(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    repo = Repo(dest, "https://example.com/repo.git")
    with patch("subprocess.run", return_value=make_result(0)):
        repo.clone(ref="main", depth=1, force=True)


def test_clone_without_force_does_not_remove_existing(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "file").write_text("data")

    repo = Repo(dest, "https://example.com/repo.git")

    with patch("subprocess.run", return_value=make_result(0)):
        repo.clone(ref="main", depth=1, force=False)

    assert (dest / "file").exists()


def test_clone_failure_raises(tmp_path):
    repo = Repo(tmp_path / "dest", "https://example.com/repo.git")
    with patch("subprocess.run", return_value=make_result(128)):
        with pytest.raises(FetchError, match="git clone .*repo.git failed"):
            repo.clone(ref="main", depth=1)
