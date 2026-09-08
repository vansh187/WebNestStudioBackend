import asyncio
import hashlib
import logging
import secrets
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from core.exceptions import GenerationUnavailableError, NotFoundError, PayloadTooLargeError, RateLimitedError, ValidationError
from database.base_persistence import BasePersistence
from database.models import CodingExecution, CodingProject, CodingShare, User
from schemas.coding_schemas import CodingFile, ExecuteRequest, ExecuteResponse

logger = logging.getLogger("webnest.coding")

ANON_PER_MINUTE = 20
ANON_PER_DAY = 200
AUTH_PER_MINUTE = 120
SHARES_PER_HOUR = 60

COMPILED_LANGUAGES = {"java", "c", "cpp", "go", "typescript"}

LANGUAGES = [
    {
        "id": "javascript",
        "label": "JavaScript (Node)",
        "version": "20.11.1",
        "monacoId": "javascript",
        "fileExtension": "js",
        "defaultSnippet": 'console.log("Hello, World!");',
    },
    {
        "id": "python",
        "label": "Python 3",
        "version": "3.12.0",
        "monacoId": "python",
        "fileExtension": "py",
        "defaultSnippet": 'print("Hello, World!")',
    },
    {
        "id": "java",
        "label": "Java",
        "version": "21.0.2",
        "monacoId": "java",
        "fileExtension": "java",
        "defaultSnippet": 'public class Main {\n  public static void main(String[] args) {\n    System.out.println("Hello, World!");\n  }\n}',
    },
    {
        "id": "c",
        "label": "C",
        "version": "10.2.0",
        "monacoId": "c",
        "fileExtension": "c",
        "defaultSnippet": '#include <stdio.h>\n\nint main(void) {\n  printf("Hello, World!\\n");\n  return 0;\n}',
    },
    {
        "id": "cpp",
        "label": "C++",
        "version": "10.2.0",
        "monacoId": "cpp",
        "fileExtension": "cpp",
        "defaultSnippet": '#include <iostream>\n\nint main() {\n  std::cout << "Hello, World!" << std::endl;\n  return 0;\n}',
    },
    {
        "id": "typescript",
        "label": "TypeScript",
        "version": "5.0.3",
        "monacoId": "typescript",
        "fileExtension": "ts",
        "defaultSnippet": 'console.log("Hello, World!");',
    },
    {
        "id": "go",
        "label": "Go",
        "version": "1.20.2",
        "monacoId": "go",
        "fileExtension": "go",
        "defaultSnippet": 'package main\n\nimport "fmt"\n\nfunc main() {\n  fmt.Println("Hello, World!")\n}',
    },
    {
        "id": "ruby",
        "label": "Ruby",
        "version": "3.0.1",
        "monacoId": "ruby",
        "fileExtension": "rb",
        "defaultSnippet": 'puts "Hello, World!"',
    },
]

LANGUAGE_BY_ID = {language["id"]: language for language in LANGUAGES}


class SlidingWindowLimiter:
    _SWEEP_INTERVAL_SECONDS = 300

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()
        self._window_seconds = 0
        self._next_sweep = time.monotonic() + self._SWEEP_INTERVAL_SECONDS

    async def consume(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        cutoff = now - window_seconds
        async with self._lock:
            self._window_seconds = max(self._window_seconds, window_seconds)
            events = self._events.get(key)
            if events is None:
                events = deque()
                self._events[key] = events
            while events and events[0] <= cutoff:
                events.popleft()
            allowed = len(events) < limit
            if allowed:
                events.append(now)
            if not events:
                # Never retain an empty deque: an idle client would otherwise
                # leak its key for the life of the process.
                self._events.pop(key, None)
            if now >= self._next_sweep:
                self._sweep(now)
                self._next_sweep = now + self._SWEEP_INTERVAL_SECONDS
            return allowed

    def _sweep(self, now: float) -> None:
        expiry = now - self._window_seconds
        stale = [key for key, events in self._events.items() if not events or events[-1] <= expiry]
        for key in stale:
            del self._events[key]


class CodingService(BasePersistence):
    _minute_limiter = SlidingWindowLimiter()
    _day_limiter = SlidingWindowLimiter()
    _share_limiter = SlidingWindowLimiter()

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        super().__init__(session)
        self._settings = settings

    def list_languages(self) -> list[dict]:
        return LANGUAGES

    async def execute(self, payload: ExecuteRequest, user: User | None, client_ip: str | None) -> ExecuteResponse:
        files = self._normalize_files(payload.language, payload.files, payload.source)
        language = self._get_language(payload.language)
        self._validate_total_size(files, self._settings.compiler_source_limit_bytes)
        await self._check_execute_rate_limit(user, client_ip)

        if not self._settings.piston_base_url.strip():
            raise GenerationUnavailableError("Compiler engine is not configured. Set PISTON_BASE_URL to enable execution.")

        started = time.perf_counter()
        status = "internal_error"
        time_ms = 0
        try:
            response_data = await self._call_piston(language, payload.version, files, payload.stdin, payload.args)
            wall_time_ms = int((time.perf_counter() - started) * 1000)
            result = self._map_piston_response(payload.language, response_data, wall_time_ms)
            status = result.status
            time_ms = result.time_ms
            return result
        finally:
            await self._record_execution(
                user_id=user.id if user else None,
                ip_hash=self._hash_ip(client_ip),
                language=payload.language,
                status=status,
                time_ms=time_ms,
            )

    async def list_projects(self, user_id: uuid.UUID, limit: int, cursor: str | None) -> tuple[list[CodingProject], str | None]:
        limit = min(max(limit, 1), 100)
        statement = select(CodingProject).where(CodingProject.owner_user_id == user_id)
        if cursor:
            cursor_dt = self._parse_cursor(cursor)
            statement = statement.where(CodingProject.updated_at < cursor_dt)
        result = await self._execute(statement.order_by(CodingProject.updated_at.desc()).limit(limit + 1))
        rows = list(result.scalars().all())
        has_more = len(rows) > limit
        page = rows[:limit]
        # Cursor must be the last row we actually return; the strict "<" filter
        # on the next page would otherwise skip the probe row.
        next_cursor = page[-1].updated_at.isoformat() if has_more and page else None
        return page, next_cursor

    async def create_project(
        self, user_id: uuid.UUID, title: str, language: str, files: list[CodingFile] | None, source: str | None, stdin: str, description: str
    ) -> CodingProject:
        normalized_files = self._normalize_files(language, files, source)
        self._get_language(language)
        self._validate_total_size(normalized_files, self._settings.coding_project_source_limit_bytes)
        result = await self._execute(select(func.count(CodingProject.id)).where(CodingProject.owner_user_id == user_id))
        if (result.scalar_one() or 0) >= self._settings.coding_project_limit_per_user:
            raise ValidationError(f"Project limit reached. You can save up to {self._settings.coding_project_limit_per_user} projects.")

        project = CodingProject(
            owner_user_id=user_id,
            title=title.strip(),
            description=description.strip(),
            language=language,
            files=[file.model_dump() for file in normalized_files],
            stdin=stdin,
        )
        self._session.add(project)
        await self._commit()
        await self._refresh(project)
        return project

    async def get_project(self, user_id: uuid.UUID, project_id: uuid.UUID) -> CodingProject:
        project = await self._get_owned_project(user_id, project_id)
        if project is None:
            raise NotFoundError("Project not found.")
        return project

    async def update_project(self, user_id: uuid.UUID, project_id: uuid.UUID, updates: dict) -> CodingProject:
        project = await self.get_project(user_id, project_id)
        if "language" in updates and updates["language"] is not None:
            self._get_language(updates["language"])
            project.language = updates["language"]
        if "files" in updates and updates["files"] is not None:
            # The router passes updates straight from model_dump(), so files arrive
            # as plain dicts; coerce them back to CodingFile before validating.
            files = [item if isinstance(item, CodingFile) else CodingFile.model_validate(item) for item in updates["files"]]
            self._validate_total_size(files, self._settings.coding_project_source_limit_bytes)
            project.files = [file.model_dump() for file in files]
        if "title" in updates and updates["title"] is not None:
            project.title = updates["title"].strip()
        if "description" in updates and updates["description"] is not None:
            project.description = updates["description"].strip()
        if "stdin" in updates and updates["stdin"] is not None:
            project.stdin = updates["stdin"]
        project.updated_at = datetime.now(timezone.utc)
        await self._commit()
        await self._refresh(project)
        return project

    async def delete_project(self, user_id: uuid.UUID, project_id: uuid.UUID) -> None:
        project = await self.get_project(user_id, project_id)
        await self._delete(project)

    async def create_share(
        self,
        user: User,
        project_id: uuid.UUID | None,
        title: str | None,
        language: str,
        files: list[CodingFile] | None,
        source: str | None,
        stdin: str,
        stdout: str,
    ) -> CodingShare:
        if not await self._share_limiter.consume(str(user.id), SHARES_PER_HOUR, 3600):
            raise RateLimitedError("Too many share links created. Try again in an hour.")
        normalized_files = self._normalize_files(language, files, source)
        self._get_language(language)
        self._validate_total_size(normalized_files, self._settings.coding_project_source_limit_bytes)
        project = await self._get_owned_project(user.id, project_id) if project_id else None
        if project_id and project is None:
            raise NotFoundError("Project not found.")

        share = CodingShare(
            share_id=await self._new_share_id(),
            project_id=project_id,
            owner_user_id=user.id,
            title=(title or (project.title if project else "Untitled")).strip() or "Untitled",
            language=language,
            files=[file.model_dump() for file in normalized_files],
            stdin=stdin,
            stdout=stdout,
            author_display_name=user.full_name,
        )
        self._session.add(share)
        if project is not None:
            project.share_id = share.share_id
            project.is_public = True
            project.updated_at = datetime.now(timezone.utc)
        await self._commit(conflict_message="Could not create a unique share link. Please try again.")
        await self._refresh(share)
        return share

    async def get_share(self, share_id: str) -> CodingShare:
        result = await self._execute(select(CodingShare).where(CodingShare.share_id == share_id))
        share = result.scalar_one_or_none()
        if share is None:
            raise NotFoundError("Share not found.")
        return share

    async def get_stats(self, user_id: uuid.UUID) -> tuple[int, datetime | None]:
        result = await self._execute(
            select(func.count(CodingProject.id), func.max(CodingProject.updated_at)).where(CodingProject.owner_user_id == user_id)
        )
        count, last_activity_at = result.one()
        return count or 0, last_activity_at

    async def _call_piston(
        self, language: dict, version: str | None, files: list[CodingFile], stdin: str, args: list[str]
    ) -> dict:
        url = self._settings.piston_base_url.rstrip("/") + "/api/v2/execute"
        payload = {
            "language": language["id"],
            "version": version or language["version"],
            "files": [file.model_dump() for file in files],
            "stdin": stdin,
            "args": args,
            "compile_timeout": self._settings.compiler_compile_timeout_ms,
            "run_timeout": self._settings.compiler_run_timeout_ms,
            "compile_memory_limit": self._settings.compiler_memory_limit_bytes,
            "run_memory_limit": self._settings.compiler_memory_limit_bytes,
        }
        try:
            async with httpx.AsyncClient(timeout=self._settings.compiler_request_timeout_seconds) as client:
                response = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise GenerationUnavailableError("Compiler engine is temporarily unavailable.") from exc
        if response.status_code >= 500:
            raise GenerationUnavailableError("Compiler engine is temporarily unavailable.")
        if response.status_code >= 400:
            raise ValidationError("Compiler engine rejected this execution request.")
        try:
            return response.json()
        except ValueError as exc:
            raise GenerationUnavailableError("Compiler engine returned an invalid response.") from exc

    def _map_piston_response(self, language_id: str, data: dict, wall_time_ms: int) -> ExecuteResponse:
        compile_data = data.get("compile") or {}
        run_data = data.get("run") or {}
        compile_code = compile_data.get("code")
        run_code = run_data.get("code")
        signal = run_data.get("signal")
        stdout, stdout_truncated = self._cap_output(run_data.get("stdout") or "")
        stderr, stderr_truncated = self._cap_output(run_data.get("stderr") or "")
        compile_stdout, compile_stdout_truncated = self._cap_output(compile_data.get("stdout") or "")
        compile_stderr, compile_stderr_truncated = self._cap_output(compile_data.get("stderr") or "")

        run_message = str(run_data.get("message") or "").lower()
        compile_message = str(compile_data.get("message") or "").lower()
        timed_out = signal == "SIGKILL" or "timed out" in run_message or "timed out" in compile_message

        if compile_code not in (None, 0):
            status = "compile_error"
        elif timed_out:
            # Classify from the sandbox signal/message, not HTTP wall-clock time:
            # a slow-but-successful Piston call is not a timeout.
            status = "timeout"
        elif run_code not in (None, 0) or signal:
            status = "runtime_error"
        else:
            status = "success"

        return ExecuteResponse(
            status=status,
            stdout="" if status == "compile_error" else stdout,
            stderr="" if status == "compile_error" else stderr,
            exit_code=run_code,
            signal=signal,
            compile={
                "stdout": compile_stdout,
                "stderr": compile_stderr,
                "exit_code": compile_code,
            }
            if language_id in COMPILED_LANGUAGES or compile_data
            else None,
            time_ms=int(run_data.get("time") or 0),
            wall_time_ms=wall_time_ms,
            truncated=stdout_truncated or stderr_truncated or compile_stdout_truncated or compile_stderr_truncated,
        )

    async def _check_execute_rate_limit(self, user: User | None, client_ip: str | None) -> None:
        if user is not None:
            allowed = await self._minute_limiter.consume(f"user:{user.id}", AUTH_PER_MINUTE, 60)
            if not allowed:
                raise RateLimitedError("Too many runs, try again in a minute.")
            return

        anon_key = f"ip:{self._hash_ip(client_ip) or 'unknown'}"
        minute_allowed = await self._minute_limiter.consume(anon_key, ANON_PER_MINUTE, 60)
        day_allowed = await self._day_limiter.consume(anon_key, ANON_PER_DAY, int(timedelta(days=1).total_seconds()))
        if not minute_allowed or not day_allowed:
            raise RateLimitedError("Too many runs, try again in a minute.")

    async def _record_execution(
        self, user_id: uuid.UUID | None, ip_hash: str | None, language: str, status: str, time_ms: int | None
    ) -> None:
        self._session.add(
            CodingExecution(user_id=user_id, ip_hash=ip_hash, language=language, status=status, time_ms=time_ms)
        )
        try:
            await self._commit()
        except Exception:
            logger.warning("Could not write coding execution audit log", exc_info=True)

    async def _get_owned_project(self, user_id: uuid.UUID, project_id: uuid.UUID | None) -> CodingProject | None:
        if project_id is None:
            return None
        result = await self._execute(
            select(CodingProject).where(CodingProject.id == project_id, CodingProject.owner_user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def _new_share_id(self) -> str:
        for _ in range(5):
            share_id = secrets.token_urlsafe(9).rstrip("_-")[:12]
            result = await self._execute(select(CodingShare.id).where(CodingShare.share_id == share_id))
            if result.scalar_one_or_none() is None:
                return share_id
        return secrets.token_urlsafe(18).rstrip("_-")[:24]

    def _normalize_files(self, language_id: str, files: list[CodingFile] | None, source: str | None) -> list[CodingFile]:
        if files is not None:
            return files
        language = self._get_language(language_id)
        return [CodingFile(name=f"main.{language['fileExtension']}", content=source or "")]

    def _get_language(self, language_id: str) -> dict:
        language = LANGUAGE_BY_ID.get(language_id)
        if language is None:
            raise ValidationError("Unknown language.")
        return language

    def _validate_total_size(self, files: list[CodingFile], limit_bytes: int) -> None:
        total = sum(len(file.content.encode("utf-8")) for file in files)
        if total > limit_bytes:
            raise PayloadTooLargeError("Submitted code is too large.")

    def _cap_output(self, value: str) -> tuple[str, bool]:
        encoded = value.encode("utf-8")
        if len(encoded) <= self._settings.compiler_output_limit_bytes:
            return value, False
        capped = encoded[: self._settings.compiler_output_limit_bytes].decode("utf-8", errors="ignore")
        return capped, True

    def _hash_ip(self, client_ip: str | None) -> str | None:
        if not client_ip:
            return None
        return hashlib.sha256(f"{self._settings.jwt_secret_key}:{client_ip}".encode("utf-8")).hexdigest()

    def _parse_cursor(self, cursor: str) -> datetime:
        try:
            return datetime.fromisoformat(cursor.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("Invalid cursor.") from exc
