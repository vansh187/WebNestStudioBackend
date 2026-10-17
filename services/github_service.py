import asyncio
import base64
import binascii
import hashlib
import logging
import posixpath
import re
import secrets
from datetime import timedelta
from urllib.parse import urlencode

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from core.exceptions import DomainError, GitHubError
from database.github_persistence import GitHubPersistence
from database.models import GitHubConnection, User
from schemas.github_schemas import (
    OWNER_PATTERN,
    REPO_PATTERN,
    BranchItem,
    BranchListResponse,
    CommitRequest,
    CommitResponse,
    CommittedFile,
    GitHubConnectionResponse,
    OAuthCallbackResponse,
    OAuthStartResponse,
    RepoCreateRequest,
    RepoCreateResponse,
    RepoItem,
    RepoListResponse,
    SkippedFile,
    SnapshotFile,
    SnapshotResponse,
    validate_branch,
)
from services.github_client import (
    GitHubClient,
    error_for,
    exchange_oauth_code,
    json_object,
    path_part,
    ref_part,
    revoke_grant,
    revoke_token,
    unavailable,
)

logger = logging.getLogger("webnest.github")

OAUTH_SCOPE = "repo"
STATE_TTL = timedelta(minutes=10)

# Snapshot limits (contract section 2.8) and commit limits (section 2.9).
MAX_FILE_BYTES = 200 * 1024
MAX_FILES = 300
MAX_TOTAL_BYTES = 2 * 1024 * 1024
MAX_CHANGES = 300
MAX_SKIPPED_LISTED = 1000
GITHUB_CONCURRENCY = 8
# Budget freed by files that turn out to be binary is refilled in at most
# this many extra download rounds.
MAX_FETCH_ROUNDS = 5
# Repo search (`q`) filters client-side over at most this many pages of 100.
MAX_SEARCH_PAGES = 10
MAX_BRANCH_PAGES = 10
# When GitHub truncates a recursive tree, directories are listed one by one,
# up to this many; the rest are reported as skipped ("limit").
MAX_WALK_DIRS = 150
# Whole-operation deadline, under the frontend's 60 s request timeout.
OPERATION_DEADLINE_SECONDS = 55
# Waits between polls for a new repository's first commit (~16 s in total).
REPO_READY_DELAYS = (0.5, 1, 1, 2, 2, 3, 3, 4)

# Never source code, wherever they appear.
IGNORED_DIRS_ANYWHERE = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".next", ".nuxt", ".gradle", ".idea", ".cache", "bower_components",
}
# Build output / vendored code at the repository root only: deeper down the
# same names are ordinary packages (com/acme/build, app/out, ...).
IGNORED_DIRS_AT_ROOT = {"dist", "build", "target", "out", "coverage", "vendor"}
BINARY_EXTENSIONS = {
    "png", "jpg", "jpeg", "gif", "bmp", "ico", "webp", "avif", "tif", "tiff", "psd", "pdf", "zip", "gz",
    "tgz", "bz2", "xz", "7z", "rar", "tar", "jar", "war", "ear", "class", "exe", "dll", "so", "dylib",
    "bin", "o", "a", "lib", "pyc", "pyo", "whl", "woff", "woff2", "ttf", "otf", "eot", "mp3", "mp4",
    "wav", "ogg", "webm", "mov", "avi", "flac", "sqlite", "db", "doc", "docx", "xls", "xlsx", "ppt",
    "pptx", "keystore", "jks", "p12",
}
LANGUAGES = {
    "py": "python", "java": "java", "js": "javascript", "mjs": "javascript", "cjs": "javascript",
    "jsx": "javascript", "ts": "typescript", "tsx": "typescript", "html": "html", "htm": "html",
    "css": "css", "scss": "scss", "json": "json", "md": "markdown", "xml": "xml", "yml": "yaml",
    "yaml": "yaml", "sql": "sql", "sh": "shell", "txt": "plaintext", "c": "c", "h": "c", "cpp": "cpp",
    "hpp": "cpp", "kt": "kotlin", "go": "go", "rb": "ruby", "php": "php", "properties": "properties",
    "gradle": "groovy", "toml": "toml", "ini": "ini", "cfg": "ini", "svg": "xml",
}
_PY_BACKEND = re.compile(r"^\s*(?:from|import)\s+(?:fastapi|flask|django|starlette)\b", re.MULTILINE)
_PY_REQUIREMENT = re.compile(r"^\s*(?:fastapi|flask|django|starlette)\b", re.MULTILINE | re.IGNORECASE)
_NODE_BACKEND = re.compile(r'"(?:express|fastify|koa|@nestjs/core|hapi)"\s*:')
_OWNER_RE = re.compile(OWNER_PATTERN)
_REPO_RE = re.compile(REPO_PATTERN)

# What reading a GitHub response of an unexpected shape raises (missing key,
# list instead of object, bad JSON, a value our response models reject).
_MALFORMED_RESPONSE = (KeyError, IndexError, TypeError, AttributeError, ValueError)
_NOT_FOUND_MESSAGE = "Repository or branch not found, or you don't have access to it."
_REAUTH_MESSAGE = "Your GitHub connection has expired. Please connect GitHub again."


def git_blob_sha(data: bytes) -> str:
    """The SHA-1 git (and GitHub) gives a blob with this content."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def language_for(path: str) -> str:
    name = posixpath.basename(path)
    if name == "Dockerfile":
        return "dockerfile"
    _, _, ext = name.rpartition(".")
    return LANGUAGES.get(ext.lower(), "plaintext") if "." in name else "plaintext"


def is_ignored_dir(name: str, depth: int) -> bool:
    """Whether a directory called `name`, `depth` levels below the repository
    root (0 = top level), is left out of imports."""
    return name in IGNORED_DIRS_ANYWHERE or (depth == 0 and name in IGNORED_DIRS_AT_ROOT)


def guess_mode(files: list[tuple[str, str]]) -> str:
    """Picks the CodeLab editor mode for imported (path, content) pairs."""
    java = sum(1 for p, _ in files if p.endswith(".java"))
    python = sum(1 for p, _ in files if p.endswith(".py"))
    web = sum(1 for p, _ in files if p.rsplit(".", 1)[-1].lower() in {"html", "htm", "css", "js", "jsx", "ts", "tsx"})
    py_backend = any(_PY_BACKEND.search(c) for p, c in files if p.endswith(".py")) or any(
        _PY_REQUIREMENT.search(c) for p, c in files if posixpath.basename(p) == "requirements.txt"
    )
    node_backend = any(_NODE_BACKEND.search(c) for p, c in files if posixpath.basename(p) == "package.json")
    if java and java >= python and java >= web:
        return "java"
    # A Python web framework wins over templates/static files, however many.
    if python and py_backend:
        return "backend"
    if python and python >= web:
        return "python"
    if node_backend:
        return "backend"
    return "web"


def classify_snapshot(entries: list[dict]) -> tuple[list[dict], list[SkippedFile]]:
    """Splits a git tree's blobs, in path order, into import candidates and
    files that are never imported (ignored_dir, binary, too_large). An
    ignored directory is reported once, not once per file inside it."""
    candidates: list[dict] = []
    skipped: list[SkippedFile] = []
    ignored_reported: set[str] = set()
    for entry in sorted((e for e in entries if e.get("type") == "blob"), key=lambda e: str(e.get("path", ""))):
        path = str(entry.get("path", ""))
        if not path or not entry.get("sha"):
            continue
        segments = path.split("/")
        ignored_at = next((i for i, s in enumerate(segments[:-1]) if is_ignored_dir(s, i)), None)
        if ignored_at is not None:
            prefix = "/".join(segments[: ignored_at + 1]) + "/"
            if prefix not in ignored_reported:
                ignored_reported.add(prefix)
                skipped.append(SkippedFile(path=prefix, reason="ignored_dir"))
            continue
        ext = segments[-1].rsplit(".", 1)[-1].lower() if "." in segments[-1] else ""
        if entry.get("mode") in ("120000", "160000") or ext in BINARY_EXTENSIONS:
            skipped.append(SkippedFile(path=path, reason="binary"))
        elif _size(entry) > MAX_FILE_BYTES:
            skipped.append(SkippedFile(path=path, reason="too_large"))
        else:
            candidates.append(entry)
    return candidates, skipped


def pick_batch(candidates: list[dict], files_left: int, bytes_left: int) -> tuple[list[dict], list[dict]]:
    """Takes candidates in order while they fit the remaining file count and
    byte budget; returns (taken, left over)."""
    batch: list[dict] = []
    rest: list[dict] = []
    for entry in candidates:
        size = _size(entry)
        if len(batch) < files_left and size <= bytes_left:
            batch.append(entry)
            bytes_left -= size
        else:
            rest.append(entry)
    return batch, rest


def plan_snapshot(entries: list[dict]) -> tuple[list[dict], list[SkippedFile]]:
    """The first download batch for a tree, and everything skipped so far."""
    candidates, skipped = classify_snapshot(entries)
    batch, rest = pick_batch(candidates, MAX_FILES, MAX_TOTAL_BYTES)
    skipped += [SkippedFile(path=e["path"], reason="limit") for e in rest]
    return batch, skipped[:MAX_SKIPPED_LISTED]


def _size(entry: dict) -> int:
    try:
        return max(0, int(entry.get("size") or 0))
    except (TypeError, ValueError):
        return 0


def _decode_text(data: bytes) -> str | None:
    """The file as text, or None when it is binary (NUL byte / not UTF-8)."""
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _malformed(exc: Exception) -> GitHubError:
    logger.error("Unexpected GitHub response shape: %s", type(exc).__name__)
    return unavailable("GitHub sent an unexpected response. Please try again.")


def _validated_repo(owner: str, repo: str) -> tuple[str, str]:
    if not _OWNER_RE.match(owner or "") or not _REPO_RE.match(repo or "") or repo in {".", ".."}:
        raise GitHubError(404, "repo_not_found", _NOT_FOUND_MESSAGE)
    return owner, repo


def _validation_error(loc: list[str], msg: str) -> GitHubError:
    return GitHubError(422, None, msg, errors=[{"loc": loc, "msg": msg}])


def _repo_item(data: dict) -> RepoItem:
    return RepoItem(
        owner=data["owner"]["login"],
        name=data["name"],
        full_name=data["full_name"],
        private=bool(data["private"]),
        default_branch=data.get("default_branch") or "main",
        html_url=data["html_url"],
        updated_at=data.get("updated_at"),
    )


def _parse_items(data, parse) -> list:
    """Parses a GitHub list response item by item. One malformed item is
    skipped (and logged) instead of failing the whole list; a body that is not
    a list at all is a malformed response."""
    if not isinstance(data, list):
        raise TypeError("expected a JSON list")
    items = []
    for raw in data:
        try:
            items.append(parse(raw))
        except _MALFORMED_RESPONSE as exc:
            logger.warning("Skipped a malformed GitHub list item: %s", type(exc).__name__)
    return items


def _branch_item(data: dict) -> BranchItem:
    return BranchItem(name=data["name"], commit_sha=data["commit"]["sha"])


def _has_next_page(response) -> bool:
    return 'rel="next"' in response.headers.get("link", "")


def _connection_view(connection: GitHubConnection | None) -> dict:
    if connection is None:
        return {"connected": False, "login": None, "avatar_url": None, "scopes": [], "connected_at": None}
    return {
        "connected": True,
        "login": connection.login,
        "avatar_url": connection.avatar_url,
        "scopes": list(connection.scopes or []),
        "connected_at": connection.connected_at,
    }


async def _bounded(semaphore: asyncio.Semaphore, func, *args):
    async with semaphore:
        return await func(*args)


async def _fetch_all(coroutines: list) -> list:
    """Runs the coroutines concurrently; the first failure cancels the rest
    (so a rate-limited import stops spending the learner's quota) and is
    re-raised."""
    tasks = [asyncio.ensure_future(c) for c in coroutines]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def _tree_level(gh: GitHubClient, base: str, tree_sha: str) -> list[dict]:
    """One directory of a git tree (not recursive)."""
    data = await gh.json("GET", f"{base}/git/trees/{path_part(tree_sha)}")
    return [item for item in (data.get("tree") or []) if isinstance(item, dict)]


async def _walk_tree(gh: GitHubClient, base: str, root_sha: str) -> tuple[list[dict], list[SkippedFile]]:
    """Lists a tree directory by directory, one level at a time with the
    directories of a level fetched in parallel - for when GitHub truncated
    the recursive listing. Ignored directories are not entered; directories
    past MAX_WALK_DIRS are reported as skipped, so the import never looks
    complete when it isn't."""
    semaphore = asyncio.Semaphore(GITHUB_CONCURRENCY)
    blobs: list[dict] = []
    skipped: list[SkippedFile] = []
    level: list[tuple[str, str]] = [("", root_sha)]
    visited = 0
    while level:
        allowed = level[: max(0, MAX_WALK_DIRS - visited)]
        skipped += [SkippedFile(path=f"{prefix}/", reason="limit") for prefix, _ in level[len(allowed):]]
        visited += len(allowed)
        listings = await _fetch_all([_bounded(semaphore, _tree_level, gh, base, sha) for _, sha in allowed])
        next_level: list[tuple[str, str]] = []
        for (prefix, _), items in zip(allowed, listings):
            depth = prefix.count("/") + 1 if prefix else 0
            for item in items:
                path = f"{prefix}/{item['path']}" if prefix else item["path"]
                if item.get("type") == "tree":
                    if is_ignored_dir(item["path"], depth):
                        skipped.append(SkippedFile(path=f"{path}/", reason="ignored_dir"))
                    else:
                        next_level.append((path, item["sha"]))
                elif item.get("type") == "blob":
                    blobs.append({**item, "path": path})
        level = next_level
    return blobs, skipped


async def _lookup_paths(gh: GitHubClient, base: str, root_sha: str, paths: list[str]) -> dict[str, dict]:
    """Tree entries for exactly these paths, reading only the directories on
    their way (each depth in parallel) - for when GitHub truncated the
    recursive listing."""
    semaphore = asyncio.Semaphore(GITHUB_CONCURRENCY)
    needed: set[str] = {""}
    for path in paths:
        parts = path.split("/")[:-1]
        needed.update("/".join(parts[: i + 1]) for i in range(len(parts)))
    listings: dict[str, dict[str, dict]] = {}
    by_depth: dict[int, list[str]] = {}
    for directory in needed:
        by_depth.setdefault(directory.count("/") + 1 if directory else 0, []).append(directory)

    for depth in sorted(by_depth):
        to_fetch: list[tuple[str, str]] = []
        for directory in by_depth[depth]:
            if directory == "":
                to_fetch.append((directory, root_sha))
                continue
            parent, _, name = directory.rpartition("/")
            node = listings.get(parent, {}).get(name)
            if node is not None and node.get("type") == "tree":
                to_fetch.append((directory, node["sha"]))
        results = await _fetch_all([_bounded(semaphore, _tree_level, gh, base, sha) for _, sha in to_fetch])
        for (directory, _), items in zip(to_fetch, results):
            listings[directory] = {item["path"]: item for item in items}

    found: dict[str, dict] = {}
    for path in paths:
        parent, _, name = path.rpartition("/")
        entry = listings.get(parent, {}).get(name)
        if entry is not None:
            found[path] = entry
    return found


class GitHubService:
    """CodeLab GitHub sync: OAuth connection, repository listing/creation,
    snapshot import and commits. The learner's token is only ever decrypted in
    memory for the GitHub calls of one request.

    Every public method runs through _guard: a caller only ever sees a
    DomainError with a user-safe message, never a raw exception."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._github = GitHubPersistence(session)
        self._settings = settings

    # ------------------------------------------------------------------ #
    # Error boundary
    # ------------------------------------------------------------------ #
    async def _guard(self, operation):
        try:
            return await operation
        except DomainError:
            # GitHubError, DatabaseError, ...: already a safe message + status.
            raise
        except Exception as exc:
            logger.error("Unexpected error in GitHub sync: %s", type(exc).__name__, exc_info=True)
            raise GitHubError(500, None, "Something went wrong with GitHub sync. Please try again.") from exc

    # ------------------------------------------------------------------ #
    # Token handling
    # ------------------------------------------------------------------ #
    def _fernet(self) -> Fernet:
        key = (self._settings.github_token_encryption_key or "").strip()
        if not key:
            digest = hashlib.sha256(b"webnest-github-token:" + self._settings.jwt_secret_key.encode()).digest()
            key = base64.urlsafe_b64encode(digest).decode()
        try:
            return Fernet(key)
        except (ValueError, TypeError, binascii.Error) as exc:
            logger.critical("GITHUB_TOKEN_ENCRYPTION_KEY is not a valid Fernet key")
            raise unavailable("GitHub sync is not configured correctly.") from exc

    def _decrypt(self, connection: GitHubConnection | None) -> str | None:
        """The connection's token, or None when it was encrypted with another
        key. A misconfigured key raises (502) instead, so it can never be
        mistaken for unusable tokens and wipe everyone's connection."""
        if connection is None:
            return None
        return self._decrypt_value(connection.access_token_encrypted)

    def _decrypt_value(self, encrypted: str | None) -> str | None:
        """Like _decrypt, for a stored encrypted token value."""
        if not encrypted:
            return None
        fernet = self._fernet()
        try:
            return fernet.decrypt(str(encrypted).encode()).decode()
        except (InvalidToken, UnicodeDecodeError):
            return None

    def _oauth_app(self) -> tuple[str, str]:
        client_id = (self._settings.github_client_id or "").strip()
        client_secret = (self._settings.github_client_secret or "").strip()
        if not (client_id and client_secret):
            raise unavailable("GitHub sync is not configured yet.")
        return client_id, client_secret

    async def _forget_connection(self, user: User) -> None:
        """Deletes a token that no longer works. Failing to delete it must not
        hide the reconnect error the caller is about to raise."""
        try:
            await self._github.delete_connection(user.id)
        except DomainError:
            logger.warning("Could not delete an unusable GitHub connection", exc_info=True)

    async def _token(self, user: User) -> str:
        connection = await self._github.get_connection(user.id)
        await self._github.release()
        if connection is None:
            raise GitHubError(409, "github_not_connected", "Connect your GitHub account first.")
        token = self._decrypt(connection)
        if token is None:
            # Encryption key changed: the stored token is unusable.
            await self._forget_connection(user)
            raise GitHubError(409, "github_reauth_required", _REAUTH_MESSAGE)
        return token

    async def _run(self, user: User, operation):
        """Runs `operation(gh)` with the user's token. A token GitHub no longer
        accepts is deleted, so the connection then reads as disconnected."""
        token = await self._token(user)
        try:
            async with GitHubClient(token, self._settings.github_api_timeout_seconds) as gh:
                # Answer before the frontend's 60 s timeout gives up on us;
                # cancelling also stops any downloads still in flight.
                return await asyncio.wait_for(operation(gh), timeout=OPERATION_DEADLINE_SECONDS)
        except asyncio.TimeoutError as exc:
            logger.warning("GitHub operation passed its %s s deadline", OPERATION_DEADLINE_SECONDS)
            raise unavailable("GitHub is responding too slowly right now. Please try again.") from exc
        except GitHubError as exc:
            if exc.code == "github_reauth_required":
                await self._forget_connection(user)
            raise
        except DomainError:
            raise
        except _MALFORMED_RESPONSE as exc:
            raise _malformed(exc) from exc

    # ------------------------------------------------------------------ #
    # Connection / OAuth
    # ------------------------------------------------------------------ #
    async def get_connection(self, user: User) -> GitHubConnectionResponse:
        return await self._guard(self._get_connection(user))

    async def _get_connection(self, user: User) -> GitHubConnectionResponse:
        connection = await self._github.get_connection(user.id)
        if connection is not None:
            try:
                usable = self._decrypt(connection) is not None
            except GitHubError:
                usable = True  # key misconfigured: can't tell, report what is stored
            if not usable:
                # Encrypted with a key that is gone: report it as disconnected.
                # A read never deletes; the next GitHub call (_token) or a
                # reconnect (upsert) cleans the row up.
                connection = None
        return GitHubConnectionResponse(**_connection_view(connection))

    async def start_oauth(self, user: User, return_to: str) -> OAuthStartResponse:
        return await self._guard(self._start_oauth(user, return_to))

    async def _start_oauth(self, user: User, return_to: str) -> OAuthStartResponse:
        client_id, _ = self._oauth_app()
        state = secrets.token_urlsafe(32)
        await self._github.create_state(user.id, hashlib.sha256(state.encode()).hexdigest(), return_to, STATE_TTL)
        query = urlencode(
            {"client_id": client_id, "redirect_uri": self._settings.github_redirect_uri, "scope": OAUTH_SCOPE, "state": state}
        )
        return OAuthStartResponse(authorize_url=f"https://github.com/login/oauth/authorize?{query}")

    async def complete_oauth(self, user: User, code: str, state: str) -> OAuthCallbackResponse:
        return await self._guard(self._complete_oauth(user, code, state))

    async def _complete_oauth(self, user: User, code: str, state: str) -> OAuthCallbackResponse:
        client_id, client_secret = self._oauth_app()
        timeout = self._settings.github_api_timeout_seconds
        return_to = await self._github.consume_state(user.id, hashlib.sha256(state.encode()).hexdigest())
        if return_to is None:
            raise GitHubError(400, "invalid_state", "This GitHub sign-in link has expired. Please try connecting again.")
        self._fernet()  # a misconfigured key fails here, before the code is spent

        token_data = await exchange_oauth_code(client_id, client_secret, code, self._settings.github_redirect_uri, timeout)
        token = str(token_data["access_token"])
        try:
            connection, previous_encrypted = await self._store_connection(user, token, token_data, timeout)
        except Exception as exc:
            # Not stored, so it could never be revoked later: revoke it now.
            await revoke_token(client_id, client_secret, token, timeout)
            if isinstance(exc, _MALFORMED_RESPONSE):
                raise _malformed(exc) from exc
            raise
        # The token this upsert replaced - read under a per-user lock, so a
        # second connect flow finishing at the same moment can't leave one
        # behind unrevoked.
        previous_token = self._decrypt_value(previous_encrypted)
        if previous_token and previous_token != token:
            # Reconnecting replaced the stored token: invalidate the old one
            # (that token only - the new one may belong to the same grant).
            await revoke_token(client_id, client_secret, previous_token, timeout)
        return OAuthCallbackResponse(**_connection_view(connection), return_to=return_to)

    async def _store_connection(
        self, user: User, token: str, token_data: dict, timeout: float
    ) -> tuple[GitHubConnection, str | None]:
        """Checks a freshly issued token and saves it as the user's connection.
        Returns the connection and the encrypted token it replaced."""
        scopes = sorted({s.strip() for s in str(token_data.get("scope", "")).split(",") if s.strip()})
        if OAUTH_SCOPE not in scopes:
            # The learner can untick scopes on GitHub's consent screen; without
            # `repo` every private repository would later read as "not found".
            raise GitHubError(
                400,
                "oauth_exchange_failed",
                "GitHub sync needs access to your repositories. Please connect again and approve repository access.",
            )
        async with GitHubClient(token, timeout) as gh:
            profile = await gh.json("GET", "/user")
        avatar_url = profile.get("avatar_url")
        return await self._github.upsert_connection(
            user.id,
            github_user_id=int(profile["id"]),
            login=str(profile["login"]),
            avatar_url=avatar_url if isinstance(avatar_url, str) else None,
            scopes=scopes,
            access_token_encrypted=self._fernet().encrypt(token.encode()).decode(),
        )

    async def disconnect(self, user: User) -> None:
        return await self._guard(self._disconnect(user))

    async def _disconnect(self, user: User) -> None:
        connection = await self._github.get_connection(user.id)
        if connection is None:
            return
        try:
            token = self._decrypt(connection)
        except GitHubError:
            token = None  # misconfigured key: still disconnect locally
        # Revoking the grant kills every token of that GitHub account for this
        # app - including another WebNest user's who linked the same account.
        # Then only this user's token is revoked.
        shared = await self._github.other_users_linked_to(connection.github_user_id, user.id)
        # Local token goes first: the GitHub revocation below is best effort.
        await self._github.delete_connection(user.id)
        client_id = (self._settings.github_client_id or "").strip()
        client_secret = (self._settings.github_client_secret or "").strip()
        if token and client_id and client_secret:
            revoke = revoke_token if shared else revoke_grant
            await revoke(client_id, client_secret, token, self._settings.github_api_timeout_seconds)

    # ------------------------------------------------------------------ #
    # Repositories
    # ------------------------------------------------------------------ #
    async def list_repos(self, user: User, page: int, per_page: int, q: str | None) -> RepoListResponse:
        return await self._guard(self._list_repos(user, page, per_page, q))

    async def _list_repos(self, user: User, page: int, per_page: int, q: str | None) -> RepoListResponse:
        params = {"sort": "updated", "direction": "desc", "affiliation": "owner,collaborator,organization_member"}
        needle = (q or "").strip().lower()

        async def operation(gh: GitHubClient) -> RepoListResponse:
            if not needle:
                response = await gh.request("GET", "/user/repos", params={**params, "page": page, "per_page": per_page})
                items = _parse_items(response.json(), _repo_item)
                return RepoListResponse(items=items, page=page, has_more=_has_next_page(response))
            # GitHub can't filter /user/repos by name: scan the most recently
            # updated repositories, stopping as soon as this page (plus one
            # match, to know has_more) is filled.
            start = (page - 1) * per_page
            matches: list[RepoItem] = []
            scan_capped = False
            for github_page in range(1, MAX_SEARCH_PAGES + 1):
                response = await gh.request("GET", "/user/repos", params={**params, "page": github_page, "per_page": 100})
                matches += [r for r in _parse_items(response.json(), _repo_item) if needle in r.name.lower()]
                if len(matches) > start + per_page or not _has_next_page(response):
                    break
                scan_capped = github_page == MAX_SEARCH_PAGES
            # When the scan limit cut the search short, more matches may exist.
            return RepoListResponse(
                items=matches[start : start + per_page],
                page=page,
                has_more=len(matches) > start + per_page or scan_capped,
            )

        return await self._run(user, operation)

    async def list_branches(self, user: User, owner: str, repo: str) -> BranchListResponse:
        return await self._guard(self._list_branches(user, owner, repo))

    async def _list_branches(self, user: User, owner: str, repo: str) -> BranchListResponse:
        owner, repo = _validated_repo(owner, repo)

        async def operation(gh: GitHubClient) -> BranchListResponse:
            items: list[BranchItem] = []
            for page in range(1, MAX_BRANCH_PAGES + 1):
                response = await gh.request(
                    "GET", f"/repos/{path_part(owner)}/{path_part(repo)}/branches", params={"per_page": 100, "page": page}
                )
                items += _parse_items(response.json(), _branch_item)
                if not _has_next_page(response):
                    break
            else:
                logger.info("Branch list cut at %s branches", len(items))
            return BranchListResponse(items=items)

        return await self._run(user, operation)

    async def create_repo(self, user: User, payload: RepoCreateRequest) -> RepoCreateResponse:
        return await self._guard(self._create_repo(user, payload))

    async def _create_repo(self, user: User, payload: RepoCreateRequest) -> RepoCreateResponse:
        async def operation(gh: GitHubClient) -> RepoCreateResponse:
            response = await gh.send(
                "POST",
                "/user/repos",
                json={"name": payload.name, "private": payload.private, "description": payload.description, "auto_init": True},
            )
            if response.status_code == 422:
                errors = json_object(response).get("errors")
                if not isinstance(errors, list):
                    errors = []
                if any(isinstance(e, dict) and "already exists" in str(e.get("message", "")) for e in errors):
                    raise _validation_error(["body", "name"], "A repository with this name already exists.")
                raise _validation_error(["body", "name"], "GitHub did not accept this repository name.")
            if response.status_code >= 400:
                raise error_for(response)
            item = _repo_item(response.json())
            branch_path = f"/repos/{path_part(item.owner)}/{path_part(item.name)}/branches/{ref_part(item.default_branch)}"
            # auto_init's first commit can take a moment to become visible.
            for delay in (0, *REPO_READY_DELAYS):
                if delay:
                    await asyncio.sleep(delay)
                branch = await gh.send("GET", branch_path)
                if branch.status_code == 200:
                    return RepoCreateResponse(
                        **item.model_dump(exclude={"updated_at"}), head_commit_sha=branch.json()["commit"]["sha"]
                    )
                if branch.status_code not in (404, 409):
                    raise error_for(branch)
            logger.warning("A new repository's first commit did not appear in time")
            raise unavailable(
                f"The repository {item.full_name} was created on GitHub but is not ready yet. "
                "Pick it from your repository list in a moment instead of creating it again."
            )

        return await self._run(user, operation)

    # ------------------------------------------------------------------ #
    # Snapshot
    # ------------------------------------------------------------------ #
    async def snapshot(self, user: User, owner: str, repo: str, branch: str | None, mode: str) -> SnapshotResponse:
        return await self._guard(self._snapshot(user, owner, repo, branch, mode))

    async def _snapshot(self, user: User, owner: str, repo: str, branch: str | None, mode: str) -> SnapshotResponse:
        owner, repo = _validated_repo(owner, repo)
        if branch is not None:
            try:
                validate_branch(branch)
            except ValueError as exc:
                raise _validation_error(["query", "branch"], str(exc)) from exc
        base = f"/repos/{path_part(owner)}/{path_part(repo)}"

        async def operation(gh: GitHubClient) -> SnapshotResponse:
            branch_name = branch or (await gh.json("GET", base)).get("default_branch") or "main"
            head = await gh.json("GET", f"{base}/branches/{ref_part(branch_name)}")
            commit_sha = head["commit"]["sha"]
            tree = await gh.json("GET", f"{base}/git/trees/{path_part(commit_sha)}", params={"recursive": "1"})
            walk_skipped: list[SkippedFile] = []
            if tree.get("truncated"):
                # The recursive listing is incomplete: walk directories instead.
                entries, walk_skipped = await _walk_tree(gh, base, tree["sha"])
            else:
                entries = [e for e in (tree.get("tree") or []) if isinstance(e, dict)]
            candidates, skipped = classify_snapshot(entries)
            skipped += walk_skipped

            semaphore = asyncio.Semaphore(GITHUB_CONCURRENCY)

            async def fetch(entry: dict) -> bytes:
                blob = await gh.json("GET", f"{base}/git/blobs/{path_part(entry['sha'])}")
                try:
                    return base64.b64decode(blob.get("content") or "")
                except (ValueError, binascii.Error):
                    return b"\0"

            # Files that turn out to be binary give their budget back; it is
            # refilled with the next candidates in a few more rounds.
            files: list[SnapshotFile] = []
            used_bytes = 0
            for _ in range(1 + MAX_FETCH_ROUNDS):
                batch, candidates = pick_batch(candidates, MAX_FILES - len(files), MAX_TOTAL_BYTES - used_bytes)
                if not batch:
                    break
                contents = await _fetch_all([_bounded(semaphore, fetch, e) for e in batch])
                freed = False
                for entry, data in zip(batch, contents):
                    text = _decode_text(data)
                    if text is None:
                        skipped.append(SkippedFile(path=entry["path"], reason="binary"))
                        freed = True
                        continue
                    files.append(
                        SnapshotFile(path=entry["path"], language=language_for(entry["path"]), content=text, sha=entry["sha"])
                    )
                    used_bytes += len(data)
                if not freed:
                    break
            skipped += [SkippedFile(path=e["path"], reason="limit") for e in candidates]

            files.sort(key=lambda f: f.path)
            resolved_mode = mode if mode != "auto" else guess_mode([(f.path, f.content) for f in files])
            return SnapshotResponse(
                owner=owner,
                repo=repo,
                branch=branch_name,
                commit_sha=commit_sha,
                mode=resolved_mode,
                files=files,
                skipped=sorted(skipped, key=lambda s: s.path)[:MAX_SKIPPED_LISTED],
            )

        return await self._run(user, operation)

    # ------------------------------------------------------------------ #
    # Commits
    # ------------------------------------------------------------------ #
    async def commit(self, user: User, owner: str, repo: str, payload: CommitRequest) -> CommitResponse:
        return await self._guard(self._commit(user, owner, repo, payload))

    async def _commit(self, user: User, owner: str, repo: str, payload: CommitRequest) -> CommitResponse:
        owner, repo = _validated_repo(owner, repo)
        if len(payload.changes) > MAX_CHANGES:
            raise GitHubError(413, "payload_too_large", f"A commit can include at most {MAX_CHANGES} changed files.")
        encoded = {c.path: (c.content or "").encode("utf-8") for c in payload.changes if c.action == "upsert"}
        if sum(len(b) for b in encoded.values()) > MAX_TOTAL_BYTES:
            raise GitHubError(413, "payload_too_large", "A commit can include at most 2 MB of file content.")
        base = f"/repos/{path_part(owner)}/{path_part(repo)}"
        ref_url = f"{base}/git/ref/heads/{ref_part(payload.branch)}"
        committed = [CommittedFile(path=path, sha=git_blob_sha(data)) for path, data in encoded.items()]

        def stale(remote_sha: str) -> GitHubError:
            return GitHubError(
                409, "non_fast_forward", "GitHub has newer commits on this branch. Pull first.", remote_commit_sha=remote_sha
            )

        async def operation(gh: GitHubClient) -> CommitResponse:
            head_sha = (await gh.json("GET", ref_url))["object"]["sha"]
            if head_sha != payload.base_commit_sha:
                raise stale(head_sha)
            base_tree_sha = (await gh.json("GET", f"{base}/git/commits/{path_part(head_sha)}"))["tree"]["sha"]
            base_tree = await gh.json("GET", f"{base}/git/trees/{path_part(base_tree_sha)}", params={"recursive": "1"})
            if base_tree.get("truncated"):
                # Incomplete listing: look up exactly the changed paths.
                existing = await _lookup_paths(gh, base, base_tree_sha, [c.path for c in payload.changes])
            else:
                existing = {e["path"]: e for e in base_tree.get("tree") or [] if isinstance(e, dict)}

            entries = []
            for change in payload.changes:
                current = existing.get(change.path)
                if change.action == "delete":
                    if current is None:
                        continue  # already gone
                    entries.append({"path": change.path, "mode": "100644", "type": "blob", "sha": None})
                else:
                    # Keep an executable bit; anything else becomes a regular file.
                    mode = current["mode"] if current and current.get("mode") in ("100644", "100755") else "100644"
                    entries.append({"path": change.path, "mode": mode, "type": "blob", "content": change.content or ""})

            unchanged = CommitResponse(
                commit_sha=head_sha, html_url=f"https://github.com/{owner}/{repo}/commit/{head_sha}", files=committed
            )
            if not entries:
                return unchanged
            tree_response = await gh.send("POST", f"{base}/git/trees", json={"base_tree": base_tree_sha, "tree": entries})
            if tree_response.status_code == 422:
                raise GitHubError(
                    422, None, "GitHub rejected these changes: a file path clashes with an existing folder (or the reverse)."
                )
            if tree_response.status_code >= 400:
                raise error_for(tree_response)
            new_tree_sha = tree_response.json()["sha"]
            if new_tree_sha == base_tree_sha:
                return unchanged

            new_commit = await gh.json(
                "POST", f"{base}/git/commits", json={"message": payload.message, "tree": new_tree_sha, "parents": [head_sha]}
            )
            update = await gh.send(
                "PATCH", f"{base}/git/refs/heads/{ref_part(payload.branch)}", json={"sha": new_commit["sha"], "force": False}
            )
            if update.status_code == 422:
                # Either someone pushed in between (head moved) or GitHub
                # refuses the update itself (branch protection). Nothing was
                # written to the branch in both cases.
                latest = (await gh.json("GET", ref_url))["object"]["sha"]
                if latest != head_sha:
                    raise stale(latest)
                raise GitHubError(
                    422,
                    None,
                    "GitHub refused to update this branch. It may be protected by branch rules - push to a branch you can write to.",
                )
            if update.status_code >= 400:
                raise error_for(update)
            return CommitResponse(
                commit_sha=new_commit["sha"],
                html_url=new_commit.get("html_url") or f"https://github.com/{owner}/{repo}/commit/{new_commit['sha']}",
                files=committed,
            )

        return await self._run(user, operation)
