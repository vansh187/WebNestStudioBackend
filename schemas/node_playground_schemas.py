import json
import re

from pydantic import BaseModel, Field, field_validator, model_validator

from schemas.spring_playground_schemas import MAX_PLAYGROUND_BODY_BYTES, MAX_REQUESTS, SpringProjectRequest

# The same limits the Cloud Run playground applies (src/node.js, src/nodePaths.js), checked here
# first so a bad request never spends a run.
MAX_FILES = 40
MAX_FILES_BYTES = 262144
MAX_STDIN_BYTES = 16384
MAX_PATH_CHARS = 200
MAX_PATH_DEPTH = 7

_SEGMENT = re.compile(r"[A-Za-z0-9_][\w.-]*", re.ASCII)
_EXTENSIONS = frozenset({"js", "mjs", "cjs", "json", "html", "css", "txt", "md", "csv", "sql"})
_ENTRY_EXTENSIONS = frozenset({"js", "mjs", "cjs"})


def _utf8_len(value: str) -> int:
    return len(value.encode("utf-8"))


def _extension(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def is_node_path(name: str) -> bool:
    """A relative path of at most six folders, with no "..", hidden folders or node_modules."""
    if not name or len(name) > MAX_PATH_CHARS:
        return False
    parts = name.split("/")
    if len(parts) > MAX_PATH_DEPTH:
        return False
    *folders, file = parts
    if any(not _SEGMENT.fullmatch(part) or part.lower() == "node_modules" for part in folders):
        return False
    if file == ".env":
        return True
    return bool(_SEGMENT.fullmatch(file)) and file.lower() != "node_modules" and _extension(file) in _EXTENSIONS


class NodeProjectFile(BaseModel):
    path: str
    content: str

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        if not is_node_path(value):
            raise ValueError(
                "Files must have a relative path ending in .js, .mjs, .cjs, .json, .html, .css, .txt, .md, "
                ".csv or .sql (or be .env), and cannot be inside node_modules."
            )
        return value


class _NodeFiles(BaseModel):
    files: list[NodeProjectFile] = Field(min_length=1, max_length=MAX_FILES)
    entry: str

    def _check_files(self) -> None:
        paths = [file.path for file in self.files]
        if len(set(paths)) != len(paths):
            raise ValueError("A file is listed twice.")
        if sum(_utf8_len(file.content) for file in self.files) > MAX_FILES_BYTES:
            raise ValueError(f"The project files must be at most {MAX_FILES_BYTES // 1024} KiB in total.")
        if not is_node_path(self.entry) or _extension(self.entry) not in _ENTRY_EXTENSIONS:
            raise ValueError("entry must be a .js, .mjs or .cjs file, for example main.js.")
        if self.entry not in paths:
            raise ValueError("entry must be one of the project's files.")

    def _check_size(self) -> None:
        body = json.dumps(self.to_playground_body(), ensure_ascii=False).encode("utf-8")
        if len(body) > MAX_PLAYGROUND_BODY_BYTES:
            raise ValueError("The project and its requests are too large together. Reset data, or make the project smaller.")

    def to_playground_body(self) -> dict:  # pragma: no cover - overridden
        raise NotImplementedError


class NodeRunRequest(_NodeFiles):
    """Body of POST /api/node/run: a script run once with its stdin."""

    entry: str = "main.js"
    stdin: str = ""

    @field_validator("stdin")
    @classmethod
    def _check_stdin(cls, value: str) -> str:
        if _utf8_len(value) > MAX_STDIN_BYTES:
            raise ValueError(f"Input must be at most {MAX_STDIN_BYTES // 1024} KiB.")
        return value

    @model_validator(mode="after")
    def _check(self) -> "NodeRunRequest":
        self._check_files()
        self._check_size()
        return self

    def to_playground_body(self) -> dict:
        return {"files": [{"path": file.path, "content": file.content} for file in self.files], "entry": self.entry, "stdin": self.stdin}


class NodeProjectRunRequest(_NodeFiles):
    """Body of POST /api/node/project/run: an Express app started and sent these requests."""

    entry: str = "server.js"
    requests: list[SpringProjectRequest] = Field(default_factory=list, max_length=MAX_REQUESTS)

    @model_validator(mode="after")
    def _check(self) -> "NodeProjectRunRequest":
        self._check_files()
        self._check_size()
        return self

    def to_playground_body(self) -> dict:
        return {
            "files": [{"path": file.path, "content": file.content} for file in self.files],
            "entry": self.entry,
            "requests": [
                {"method": request.method, "path": request.path, "headers": request.headers, "body": request.body}
                for request in self.requests
            ],
        }
