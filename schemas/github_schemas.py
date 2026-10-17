import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# GitHub owner / repository names. Checked before either is put into a GitHub
# API URL, so a caller can never steer a request to another endpoint.
OWNER_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$"
REPO_PATTERN = r"^[A-Za-z0-9._-]{1,100}$"
SHA_PATTERN = r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$"
_RETURN_TO_PATTERN = re.compile(r"^/codelab/[A-Za-z0-9/_.~?=&%#+-]*$")
_BRANCH_FORBIDDEN = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")

_ENCODED_TRAVERSAL = re.compile(r"%(?:2e|2f|5c)", re.IGNORECASE)

MAX_PATH_LENGTH = 500


def ensure_utf8(value: str) -> str:
    """Rejects text that can't be encoded as UTF-8 (lone surrogates such as
    "\\ud800" from JSON), which would otherwise fail deep inside a request."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Text must be valid UTF-8.") from exc
    return value


def validate_repo_path(path: str) -> str:
    """A repository-relative file path: `/` separators, no leading `/`, no
    `.`/`..` or empty segments, nothing inside `.git`, no control characters."""
    if not path or len(path) > MAX_PATH_LENGTH:
        raise ValueError(f"Path must be 1-{MAX_PATH_LENGTH} characters.")
    ensure_utf8(path)
    if path.startswith("/") or "\\" in path:
        raise ValueError("Path must be relative and use '/' separators.")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
        raise ValueError("Path contains control characters.")
    for segment in path.split("/"):
        if segment in {"", ".", ".."}:
            raise ValueError("Path must not contain empty, '.' or '..' segments.")
        if segment.lower() == ".git":
            raise ValueError("Paths inside .git are not allowed.")
    return path


def validate_branch(branch: str) -> str:
    """Git ref-name rules (git check-ref-format), so a branch can be placed in
    a GitHub API URL path safely."""
    if (
        not branch
        or len(branch) > 255
        or _BRANCH_FORBIDDEN.search(branch)
        or ".." in branch
        or "@{" in branch
        or branch.startswith(("/", "-"))
        or branch.endswith(("/", ".", ".lock"))
        or "//" in branch
        or any(part.startswith(".") for part in branch.split("/"))
    ):
        raise ValueError("Invalid branch name.")
    return ensure_utf8(branch)


# --------------------------------------------------------------------------- #
# Connection / OAuth
# --------------------------------------------------------------------------- #
class GitHubConnectionResponse(BaseModel):
    connected: bool
    login: str | None = None
    avatar_url: str | None = None
    scopes: list[str] = Field(default_factory=list)
    connected_at: datetime | None = None


class OAuthStartRequest(BaseModel):
    return_to: str = Field(min_length=9, max_length=500)

    @field_validator("return_to")
    @classmethod
    def _same_site_codelab_path(cls, value: str) -> str:
        # A path on this site under /codelab/ only - never a scheme, host,
        # protocol-relative "//" or traversal - so it can't become an open redirect.
        if (
            not _RETURN_TO_PATTERN.match(value)
            or "//" in value
            or ".." in value
            or _ENCODED_TRAVERSAL.search(value)  # %2e %2f %5c: encoded . / \
        ):
            raise ValueError("return_to must be a path starting with /codelab/.")
        return value


class OAuthStartResponse(BaseModel):
    authorize_url: str


class OAuthCallbackRequest(BaseModel):
    code: str = Field(min_length=1, max_length=200)
    state: str = Field(min_length=1, max_length=200)

    @field_validator("code", "state")
    @classmethod
    def _check_text(cls, value: str) -> str:
        return ensure_utf8(value)


class OAuthCallbackResponse(GitHubConnectionResponse):
    return_to: str


# --------------------------------------------------------------------------- #
# Repositories
# --------------------------------------------------------------------------- #
class RepoItem(BaseModel):
    owner: str
    name: str
    full_name: str
    private: bool
    default_branch: str
    html_url: str
    updated_at: datetime | None = None


class RepoListResponse(BaseModel):
    items: list[RepoItem]
    page: int
    has_more: bool


class BranchItem(BaseModel):
    name: str
    commit_sha: str


class BranchListResponse(BaseModel):
    items: list[BranchItem]


class RepoCreateRequest(BaseModel):
    name: str = Field(pattern=REPO_PATTERN)
    private: bool = True
    description: str = Field(default="", max_length=350)

    @field_validator("name")
    @classmethod
    def _not_dot_name(cls, value: str) -> str:
        if value in {".", ".."}:
            raise ValueError("Invalid repository name.")
        return value

    @field_validator("description")
    @classmethod
    def _check_description(cls, value: str) -> str:
        return ensure_utf8(value)


class RepoCreateResponse(BaseModel):
    owner: str
    name: str
    full_name: str
    private: bool
    default_branch: str
    html_url: str
    head_commit_sha: str


# --------------------------------------------------------------------------- #
# Snapshot
# --------------------------------------------------------------------------- #
class SnapshotFile(BaseModel):
    path: str
    language: str
    content: str
    sha: str


class SkippedFile(BaseModel):
    path: str
    reason: Literal["binary", "too_large", "ignored_dir", "limit"]


class SnapshotResponse(BaseModel):
    owner: str
    repo: str
    branch: str
    commit_sha: str
    mode: Literal["backend", "web", "python", "java"]
    files: list[SnapshotFile]
    skipped: list[SkippedFile]


# --------------------------------------------------------------------------- #
# Commits
# --------------------------------------------------------------------------- #
class CommitChange(BaseModel):
    path: str
    action: Literal["upsert", "delete"]
    # Character cap per file; the 2 MB total is enforced (as 413) by the service.
    content: str | None = Field(default=None, max_length=2_000_000)

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        return validate_repo_path(value)

    @field_validator("content")
    @classmethod
    def _check_content(cls, value: str | None) -> str | None:
        return value if value is None else ensure_utf8(value)

    @model_validator(mode="after")
    def _content_matches_action(self) -> "CommitChange":
        if self.action == "upsert" and self.content is None:
            raise ValueError("content is required for upsert.")
        if self.action == "delete" and self.content is not None:
            raise ValueError("content must be absent for delete.")
        return self


class CommitRequest(BaseModel):
    branch: str
    base_commit_sha: str = Field(pattern=SHA_PATTERN)
    message: str = Field(min_length=1, max_length=200)
    # More than 300 changes is a 413 payload_too_large (service), not a 422;
    # this hard cap only bounds what one request can make the server hold.
    changes: list[CommitChange] = Field(min_length=1, max_length=5000)

    @field_validator("branch")
    @classmethod
    def _check_branch(cls, value: str) -> str:
        return validate_branch(value)

    @field_validator("message")
    @classmethod
    def _message_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Commit message is required.")
        return ensure_utf8(value)

    @model_validator(mode="after")
    def _unique_paths(self) -> "CommitRequest":
        seen: set[str] = set()
        for change in self.changes:
            if change.path in seen:
                raise ValueError(f"Duplicate path: {change.path}")
            seen.add(change.path)
        return self


class CommittedFile(BaseModel):
    path: str
    sha: str


class CommitResponse(BaseModel):
    commit_sha: str
    html_url: str
    files: list[CommittedFile]
