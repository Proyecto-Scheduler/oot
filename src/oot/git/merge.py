import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from oot.errors import PatchError


@dataclass
class MergeResult:
    content: bytes | None  # None when git cannot merge the inputs (binary files)
    conflicts: int


def merge3(base: bytes, kernel: bytes, patch: bytes) -> MergeResult:
    """3-way merge of a patch onto the current kernel file, via git merge-file."""
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for name, data in (("kernel", kernel), ("base", base), ("patch", patch)):
            p = Path(tmp) / name
            p.write_bytes(data)
            paths.append(p)

        r = subprocess.run(
            ["git", "merge-file", "-p", "-L", "kernel", "-L", "base", "-L", "patch", *paths],
            capture_output=True,
        )

    # merge-file exits with the conflict count (capped at 127), or 255 on error
    if r.returncode == 255:
        if b"binary" in r.stderr:
            return MergeResult(content=None, conflicts=1)
        raise PatchError(f"git merge-file failed:\n{r.stderr.decode(errors='replace')}")

    return MergeResult(content=r.stdout, conflicts=r.returncode)
