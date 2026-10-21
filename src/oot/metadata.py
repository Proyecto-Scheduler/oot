from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

# Base copies live here inside the patches repo, mirroring kernel paths
BASE_DIR = Path(".oot/base")


class FileMetadata(BaseModel):
    model_config = ConfigDict(strict=True)

    path: str
    base_blob: str | None = None
    status: Literal["new", "modified", "deleted"]


class Metadata(BaseModel):
    model_config = ConfigDict(strict=True)

    # "base_blob" is the pre-rename name of this field; still accepted on read
    kernel_commit: str = Field(
        validation_alias=AliasChoices("kernel_commit", "base_blob")
    )
    files: list[FileMetadata]
