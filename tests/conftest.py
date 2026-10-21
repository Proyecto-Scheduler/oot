import pytest


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    """Repos created by oot itself (e.g. sync's git init) need an identity to commit."""
    for var in ("GIT_AUTHOR", "GIT_COMMITTER"):
        monkeypatch.setenv(f"{var}_NAME", "Test")
        monkeypatch.setenv(f"{var}_EMAIL", "test@example.com")
