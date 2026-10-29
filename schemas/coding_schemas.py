import unicodedata
import uuid
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator

from schemas.github_schemas import ensure_utf8, validate_repo_path

# Request text: a lone surrogate ("\ud800" in JSON) is a 422 here, not a crash later when
# the text is encoded for the size check, the database or an error message.
Text = Annotated[str, AfterValidator(ensure_utf8)]


# The editor's own limits (frontend lib/codelab/backendFiles.js): a path is at most 120
# characters, so the server never rejects a file the editor let the learner create.
MAX_FILE_PATH_LENGTH = 120
MAX_PROJECT_FILES = 200


_WINDOWS_FORBIDDEN = set('<>:"|?*')
# Windows device names, also with an extension (con.py). ¹ ² ³ count as port digits.
_WINDOWS_DEVICES = {
    "con", "prn", "aux", "nul", "conin$", "conout$",
    *(f"{port}{n}" for port in ("com", "lpt") for n in "123456789¹²³"),
}


def clean_file_path(value: str) -> str:
    """A relative path such as main.py or app/main.py (FastAPI workspaces have folders).
    The GitHub repository path rule (no `..`, control characters or `.git`) plus what
    Windows cannot store, so a saved project can be pushed and cloned anywhere."""
    # Whitespace at either end (a pasted "main.py\n") is trimmed; inside, it is checked below.
    value = value.strip()
    if not value:
        raise ValueError("File name is required")
    if len(value) > MAX_FILE_PATH_LENGTH:
        raise ValueError(f"File path must be at most {MAX_FILE_PATH_LENGTH} characters")
    validate_repo_path(value)
    # validate_repo_path covers ASCII controls; this adds C1 ones such as U+0085.
    if any(unicodedata.category(ch) == "Cc" for ch in value):
        raise ValueError("Path contains control characters.")
    for part in value.split("/"):
        if part != part.strip(" "):
            raise ValueError("Folder and file names must not start or end with spaces")
        if part.endswith("."):
            raise ValueError("Folder and file names must not end with a dot")
        if _WINDOWS_FORBIDDEN & set(part):
            raise ValueError('File names cannot contain < > : " | ? *')
        # Windows ignores spaces before the extension too: "nul .py" is the NUL device.
        if part.split(".", 1)[0].rstrip(" ").lower() in _WINDOWS_DEVICES:
            raise ValueError(f"{part} is a reserved name on Windows; choose another name")
    return value


def _fold_char(ch: str) -> str:
    # Simple case folding, as NTFS and APFS compare: ς and σ match, but ß stays one
    # letter (its full folding, "ss", is not used by file systems).
    folded = ch.casefold()
    return folded if len(folded) == 1 else ch.lower()


def _tree_key(path: str) -> str:
    # How case-insensitive file systems (Windows, macOS) compare names.
    return "".join(_fold_char(ch) for ch in unicodedata.normalize("NFC", path))


def check_file_tree(files, kept_names: Mapping[str, int] | None = None):
    """A saved file list must be writable as a file tree on any system: no paths that
    differ only in letter case or Unicode form, and no file also used as a folder (app
    next to app/main.py). Works on any list of objects with a `name`.

    `kept_names` counts the names already saved (under an older rule that allowed
    clashes): two spellings that are both saved may stay, and a saved name may appear
    as many times as it is saved."""
    if files is None:
        return files
    kept_names = kept_names or {}
    # Every file and folder path, by its comparison key, with the first spelling seen.
    seen: dict[str, str] = {}
    files_by_key: dict[str, str] = {}
    copies: Counter[str] = Counter()
    for file in files:
        copies[file.name] += 1
        key = _tree_key(file.name)
        if key in files_by_key:
            first = files_by_key[key]
            if first == file.name:
                if copies[file.name] <= kept_names.get(file.name, 0):
                    continue
                raise ValueError(f"Duplicate file: {file.name}")
            if first in kept_names and file.name in kept_names:
                continue
            raise ValueError(f"{file.name} and {first} are the same name on Windows and macOS")
        files_by_key[key] = file.name
    for name in files_by_key.values():
        parts = name.split("/")
        for depth in range(1, len(parts)):
            folder = "/".join(parts[:depth])
            key = _tree_key(folder)
            if key in files_by_key:
                raise ValueError(f"A file and a folder cannot share the name {files_by_key[key]}")
            first = seen.setdefault(key, folder)
            if first != folder:
                raise ValueError(f"Folders {folder} and {first} are the same name on Windows and macOS")
    return files


class CodingFile(BaseModel):
    """A file in a request. The name is only trimmed here: CodingService applies
    clean_file_path and check_file_tree, sparing names a project already has saved."""

    name: Text
    content: Text

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("File name is required")
        return value


class StoredCodingFile(BaseModel):
    """A file read back from the database. Not re-checked: rows written under an older
    name rule must still load."""

    name: str
    content: str


# A request's file list, bounded while parsing. Its names and tree are checked by the service.
FileList = Annotated[list[CodingFile], Field(max_length=MAX_PROJECT_FILES)]


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
    files: FileList | None = None
    source: Text | None = None
    stdin: Text = ""
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
    title: Text = Field(min_length=1, max_length=80)
    language: str
    files: FileList | None = None
    source: Text | None = None
    stdin: Text = ""
    description: Text = ""

    @model_validator(mode="after")
    def require_files_or_source(self) -> "ProjectCreateRequest":
        if self.files is None and self.source is None:
            raise ValueError("Either files or source is required")
        if self.files is not None and len(self.files) == 0:
            raise ValueError("At least one file is required")
        return self


class ProjectUpdateRequest(BaseModel):
    title: Text | None = Field(default=None, min_length=1, max_length=80)
    description: Text | None = None
    language: str | None = None
    files: FileList | None = None
    stdin: Text | None = None

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
    files: list[StoredCodingFile]
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
    title: Text | None = Field(default=None, max_length=80)
    language: str
    files: FileList | None = None
    source: Text | None = None
    stdin: Text = ""
    stdout: Text = ""

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
    files: list[StoredCodingFile]
    stdin: str
    stdout: str
    created_at: datetime
    author_display_name: str | None = None

    model_config = {"from_attributes": True}


class CodingStatsResponse(BaseModel):
    projects_count: int
    last_activity_at: datetime | None
