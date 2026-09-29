import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_CODE_BYTES = 65536
MAX_CLASS_NAME_CHARS = 160
MAX_STDIN_BYTES = 16384
MAX_ARGS = 16
MAX_ARG_BYTES = 1024

# A (optionally package-qualified) Java class name. ASCII-only so we never
# accept a name the playground itself would reject.
_CLASS_NAME_PATTERN = re.compile(r"[A-Za-z_$][\w$]*(\.[A-Za-z_$][\w$]*)*", re.ASCII)


def _utf8_len(value: str) -> int:
    return len(value.encode("utf-8"))


class JavaRunRequest(BaseModel):
    """Body of POST /api/java/run - camelCase, same shape the playground accepts."""

    model_config = ConfigDict(populate_by_name=True)

    code: str
    class_name: str = Field(default="Main", alias="className")
    stdin: str = ""
    args: list[str] = Field(default_factory=list)

    @field_validator("code")
    @classmethod
    def _check_code(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Code must not be empty.")
        if _utf8_len(value) > MAX_CODE_BYTES:
            raise ValueError(f"Code must be at most {MAX_CODE_BYTES} bytes.")
        return value

    @field_validator("class_name")
    @classmethod
    def _check_class_name(cls, value: str) -> str:
        if len(value) > MAX_CLASS_NAME_CHARS:
            raise ValueError(f"className must be at most {MAX_CLASS_NAME_CHARS} characters.")
        if not _CLASS_NAME_PATTERN.fullmatch(value):
            raise ValueError("className must be a valid Java class name.")
        return value

    @field_validator("stdin")
    @classmethod
    def _check_stdin(cls, value: str) -> str:
        if _utf8_len(value) > MAX_STDIN_BYTES:
            raise ValueError(f"stdin must be at most {MAX_STDIN_BYTES} bytes.")
        return value

    @field_validator("args")
    @classmethod
    def _check_args(cls, value: list[str]) -> list[str]:
        if len(value) > MAX_ARGS:
            raise ValueError(f"At most {MAX_ARGS} args are allowed.")
        for arg in value:
            if _utf8_len(arg) > MAX_ARG_BYTES:
                raise ValueError(f"Each arg must be at most {MAX_ARG_BYTES} bytes.")
            if "\x00" in arg:
                raise ValueError("Args must not contain NUL characters.")
        return value

    def to_playground_body(self) -> dict:
        return {"code": self.code, "className": self.class_name, "stdin": self.stdin, "args": self.args}
