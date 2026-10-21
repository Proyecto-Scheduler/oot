import json
import subprocess
from pathlib import Path

import pytest

from oot.errors import PatchError

from oot.metadata import FileMetadata, Metadata, metadata_path, patch_files


def git(path: Path, *args):
    subprocess.run(["git", "-C", str(path), *args], capture_output=True, check=True)


def write(path: Path, text: str = "x\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# --- load / save ---


def test_load_reads_metadata(tmp_path):
    p = tmp_path / "metadata.json"
    p.write_text(json.dumps({"kernel_commit": "abc", "files": [{"path": "a.c", "status": "new"}]}))
    meta = Metadata.load(p)
    assert meta.kernel_commit == "abc"
    assert meta.files[0].path == "a.c"


def test_load_accepts_old_base_blob_name(tmp_path):
    p = tmp_path / "metadata.json"
    p.write_text(json.dumps({"base_blob": "abc", "files": []}))
    assert Metadata.load(p).kernel_commit == "abc"


def test_load_raises_when_missing(tmp_path):
    with pytest.raises(PatchError, match="metadata file not found"):
        Metadata.load(tmp_path / "metadata.json")


def test_load_rejects_invalid_metadata(tmp_path):
    p = tmp_path / "metadata.json"
    p.write_text(json.dumps({"kernel_commit": "abc", "files": [{"path": "a.c", "status": "bogus"}]}))
    with pytest.raises(PatchError, match="Invalid metadata"):
        Metadata.load(p)


def test_save_round_trips(tmp_path):
    p = tmp_path / "metadata.json"
    meta = Metadata(kernel_commit="abc", files=[FileMetadata(path="a.c", status="new")])
    meta.save(p)
    assert Metadata.load(p) == meta
    assert p.read_text().endswith("\n")


def test_metadata_path_is_in_patches_dir(tmp_path):
    assert metadata_path(tmp_path) == tmp_path / "metadata.json"


# --- patch_files ---


@pytest.fixture
def patches(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    return tmp_path


def test_patch_files_lists_tracked_and_untracked(patches):
    write(patches / "sys/tracked.c")
    git(patches, "add", "-A")
    git(patches, "commit", "-m", "x")
    write(patches / "sys/untracked.c")
    assert patch_files(patches) == {"sys/tracked.c", "sys/untracked.c"}


def test_patch_files_excludes_metadata_and_base_copies(patches):
    write(patches / "metadata.json", "{}")
    write(patches / ".oot/base/sys/a.c")
    write(patches / "sys/a.c")
    assert patch_files(patches) == {"sys/a.c"}


def test_patch_files_honours_gitignore(patches):
    write(patches / ".gitignore", "*.o\n")
    write(patches / "sys/a.o")
    write(patches / "sys/a.c")
    assert "sys/a.o" not in patch_files(patches)
    assert "sys/a.c" in patch_files(patches)


def test_patch_files_skips_tracked_files_deleted_from_disk(patches):
    write(patches / "sys/gone.c")
    git(patches, "add", "-A")
    git(patches, "commit", "-m", "x")
    (patches / "sys/gone.c").unlink()
    assert patch_files(patches) == set()


def test_patch_files_handles_spaces_and_unicode(patches):
    write(patches / "sys/my file ñ.c")
    assert patch_files(patches) == {"sys/my file ñ.c"}
