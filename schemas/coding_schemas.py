import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class CodingFile(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    content: str

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("File name is required")
        if "/" in value or "\\" in value or value in {".", ".."}:
            raise ValueError("File name must be a plain file name")
        return value


class CompilerLanguage(BaseModel):
    id: str
    label: str
    version: str
    monacoId: str
    fileExtension: str
    defaultSnippet: str


class ExecuteRequest(BaseModel):
    language: str
    version: str | None = None
    files: list[CodingFile] | None = None
    source: str | None = None
    stdin: str = ""
    args: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_files_or_source(self) -> "ExecuteRequest":
        if self.files is None and self.source is None:
            raise ValueError("Either files or source is required")
        if self.files is not None and len(self.files) == 0:
            raise ValueError("At least one file is required")
        return self


class CompileResult(BaseModel):
    stdout: str
    stderr: str
    exit_code: int | None = None


class ExecuteResponse(BaseModel):
    status: Literal["success", "compile_error", "runtime_error", "timeout", "rate_limited", "internal_error"]
    stdout: str
    stderr: str
    exit_code: int | None
    signal: str | int | None
    compile: CompileResult | None
    time_ms: int
    wall_time_ms: int
    truncated: bool


class ProjectCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    language: str
    files: list[CodingFile] | None = None
    source: str | None = None
    stdin: str = ""
    description: str = ""

    @model_validator(mode="after")
    def require_files_or_source(self) -> "ProjectCreateRequest":
        if self.files is None and self.source is None:
            raise ValueError("Either files or source is required")
        if self.files is not None and len(self.files) == 0:
            raise ValueError("At least one file is required")
        return self


class ProjectUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = None
    language: str | None = None
    files: list[CodingFile] | None = None
    stdin: str | None = None

    @field_validator("files")
    @classmethod
    def require_non_empty_files(cls, value: list[CodingFile] | None) -> list[CodingFile] | None:
        if value is not None and len(value) == 0:
            raise ValueError("At least one file is required")
        return value


class ProjectResponse(BaseModel):
    id: uuid.UUID
    title: str
    description: str
    language: str
    files: list[CodingFile]
    stdin: str
    is_public: bool
    share_id: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ProjectListItem(BaseModel):
    id: uuid.UUID
    title: str
    language: str
    updated_at: datetime
    created_at: datetime

    model_config = {"from_attributes": True}


class ProjectListResponse(BaseModel):
    items: list[ProjectListItem]
    next_cursor: str | None


class ShareCreateRequest(BaseModel):
    project_id: uuid.UUID | None = None
    title: str | None = Field(default=None, max_length=80)
    language: str
    files: list[CodingFile] | None = None
    source: str | None = None
    stdin: str = ""
    stdout: str = ""

    @model_validator(mode="after")
    def require_files_or_source(self) -> "ShareCreateRequest":
        if self.files is None and self.source is None:
            raise ValueError("Either files or source is required")
        if self.files is not None and len(self.files) == 0:
            raise ValueError("At least one file is required")
        return self


class ShareCreateResponse(BaseModel):
    share_id: str
    url: str


class ShareResponse(BaseModel):
    share_id: str
    title: str
    language: str
    files: list[CodingFile]
    stdin: str
    stdout: str
    created_at: datetime
    author_display_name: str | None = None

    model_config = {"from_attributes": True}


class CodingStatsResponse(BaseModel):
    projects_count: int
    last_activity_at: datetime | None
