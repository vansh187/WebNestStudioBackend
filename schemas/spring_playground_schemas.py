import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# The same limits the Cloud Run playground applies (src/project.js), checked here first so a bad
# request never spends a run.
MAX_FILES = 60
MAX_FILES_BYTES = 262144
MAX_REQUESTS = 30
MAX_REQUEST_BODY_BYTES = 65536
MAX_PATH_CHARS = 2048
MAX_HEADERS = 20
MAX_HEADER_VALUE_CHARS = 1024
MAX_CLASS_NAME_CHARS = 160
# The playground refuses a body over 512 KiB; this leaves room for its own fields.
MAX_PLAYGROUND_BODY_BYTES = 500 * 1024

_JAVA_PATH = re.compile(r"src/main/java/(?:[A-Za-z_$][\w$]*/)*[A-Za-z_$][\w$]*\.java", re.ASCII)
_RESOURCE_PATH = re.compile(r"src/main/resources/(?:application\.properties|data\.sql|schema\.sql)", re.ASCII)
_CLASS_NAME = re.compile(r"[A-Za-z_$][\w$]*(\.[A-Za-z_$][\w$]*)+", re.ASCII)
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", re.ASCII)


def _utf8_len(value: str) -> int:
    return len(value.encode("utf-8"))


class SpringProjectFile(BaseModel):
    path: str
    content: str

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        if not (_JAVA_PATH.fullmatch(value) or _RESOURCE_PATH.fullmatch(value)):
            raise ValueError(
                "Files must be .java files under src/main/java, or application.properties, data.sql or "
                "schema.sql under src/main/resources."
            )
        return value


class SpringProjectRequest(BaseModel):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    path: str
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None

    @field_validator("method", mode="before")
    @classmethod
    def _upper_method(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        if (
            not value.startswith("/")
            or value.startswith("//")
            or len(value) > MAX_PATH_CHARS
            or any(ch.isspace() or ch == "\x00" for ch in value)
        ):
            raise ValueError("A request path must start with / and contain no spaces.")
        return value

    @field_validator("headers")
    @classmethod
    def _check_headers(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > MAX_HEADERS:
            raise ValueError(f"At most {MAX_HEADERS} headers are allowed.")
        for name, header in value.items():
            if not _HEADER_NAME.fullmatch(name) or len(header) > MAX_HEADER_VALUE_CHARS or any(ch in header for ch in "\r\n\x00"):
                raise ValueError("A request header is not valid.")
        return value

    @field_validator("body")
    @classmethod
    def _check_body(cls, value: str | None) -> str | None:
        if value is not None and _utf8_len(value) > MAX_REQUEST_BODY_BYTES:
            raise ValueError(f"A request body must be at most {MAX_REQUEST_BODY_BYTES // 1024} KiB.")
        return value


class SpringRunRequest(BaseModel):
    """Body of POST /api/spring/run - camelCase, the shape the playground accepts."""

    model_config = ConfigDict(populate_by_name=True)

    files: list[SpringProjectFile] = Field(min_length=1, max_length=MAX_FILES)
    main_class: str = Field(alias="mainClass")
    requests: list[SpringProjectRequest] = Field(default_factory=list, max_length=MAX_REQUESTS)

    @field_validator("main_class")
    @classmethod
    def _check_main_class(cls, value: str) -> str:
        if len(value) > MAX_CLASS_NAME_CHARS or not _CLASS_NAME.fullmatch(value):
            raise ValueError("mainClass must be a class in a package, for example com.example.demo.DemoApplication.")
        return value

    @model_validator(mode="after")
    def _check_project(self) -> "SpringRunRequest":
        paths = [file.path for file in self.files]
        if len(set(paths)) != len(paths):
            raise ValueError("A file is listed twice.")
        if sum(_utf8_len(file.content) for file in self.files) > MAX_FILES_BYTES:
            raise ValueError(f"The project files must be at most {MAX_FILES_BYTES // 1024} KiB in total.")
        if f"src/main/java/{self.main_class.replace('.', '/')}.java" not in paths:
            raise ValueError("mainClass must match one of the project's .java files.")
        # Files and request bodies are each within their limits, but together they could still
        # be more than the playground takes; refuse here, before a run is counted.
        body = json.dumps(self.to_playground_body(), ensure_ascii=False).encode("utf-8")
        if len(body) > MAX_PLAYGROUND_BODY_BYTES:
            raise ValueError("The project and its requests are too large together. Reset data, or make the project smaller.")
        return self

    def to_playground_body(self) -> dict:
        return {
            "files": [{"path": file.path, "content": file.content} for file in self.files],
            "mainClass": self.main_class,
            "requests": [
                {"method": request.method, "path": request.path, "headers": request.headers, "body": request.body}
                for request in self.requests
            ],
        }
