"""CodeLab FastAPI workspaces ("backend") as saved projects and shares.

Offline: CodingService against a fake session. A backend project keeps every file
with its path, can never be switched to or from another language, and is never offered
to or run by the compiler. Every bad request is a 4xx, never a 500."""

import asyncio
import os
import re
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

os.environ.setdefault("SUPABASE_URL", "postgresql://user:pass@localhost:5432/test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.coding_router import router
from core.dependencies import get_coding_service, get_current_user
from core.error_handlers import register_error_handlers
from core.exceptions import RateLimitedError, ValidationError
from pydantic import ValidationError as PydanticValidationError

from schemas.codelab_schemas import CodeFile
from schemas.coding_schemas import (
    MAX_PROJECT_FILES,
    CodingFile,
    ExecuteRequest,
    ProjectCreateRequest,
    ProjectResponse,
    ProjectUpdateRequest,
    ShareCreateRequest,
    ShareResponse,
    check_file_tree,
    clean_file_path,
)
from services.coding_service import CodingService

SETTINGS = SimpleNamespace(
    coding_project_source_limit_bytes=262144,
    coding_project_limit_per_user=100,
    compiler_source_limit_bytes=65536,
)
USER_ID = uuid.uuid4()
FILES = [
    CodingFile(name="app/main.py", content="from fastapi import FastAPI\napp = FastAPI()\n"),
    CodingFile(name="schema.sql", content="CREATE TABLE notes (id INTEGER PRIMARY KEY);\n"),
    CodingFile(name="requirements.txt", content="httpx\n"),
    CodingFile(name="frontend/index.html", content="<h1>Notes</h1>\n"),
]


class FakeSession:
    def __init__(self, project=None):
        self.added = []
        self.project = project

    async def execute(self, statement):
        project = self.project
        return SimpleNamespace(scalar_one=lambda: 0, scalar_one_or_none=lambda: project)

    def add(self, instance):
        self.added.append(instance)

    async def commit(self):
        pass

    async def refresh(self, instance):
        # What Postgres fills in on insert, so response models validate as in production.
        now = datetime.now(timezone.utc)
        for field, value in {"id": uuid.uuid4(), "created_at": now, "updated_at": now, "is_public": False}.items():
            if getattr(instance, field, None) is None:
                setattr(instance, field, value)

    async def rollback(self):
        pass


def run(coro):
    return asyncio.run(coro)


def test_backend_project_keeps_every_file_and_path():
    session = FakeSession()
    project = run(CodingService(session, SETTINGS).create_project(USER_ID, "Notes API", "backend", FILES, None, "", ""))
    assert project.language == "backend"
    assert [file["name"] for file in project.files] == ["app/main.py", "schema.sql", "requirements.txt", "frontend/index.html"]
    assert session.added == [project]


def test_backend_project_without_files_gets_a_python_main_file():
    project = run(CodingService(FakeSession(), SETTINGS).create_project(USER_ID, "Empty", "backend", None, "print(1)", "", ""))
    assert project.files == [{"name": "main.py", "content": "print(1)"}]


def stored(language):
    return SimpleNamespace(language=language, files=[], title="t", description="", stdin="", updated_at=None)


@pytest.mark.parametrize(("current", "new"), [("python", "backend"), ("java", "backend"), ("backend", "python")])
def test_projects_cannot_switch_to_or_from_backend(current, new):
    service = CodingService(FakeSession(stored(current)), SETTINGS)
    with pytest.raises(ValidationError, match="FastAPI projects cannot be switched"):
        run(service.update_project(USER_ID, uuid.uuid4(), {"language": new}))


def test_backend_project_update_keeping_its_language_is_allowed():
    project = stored("backend")
    updated = run(CodingService(FakeSession(project), SETTINGS).update_project(USER_ID, uuid.uuid4(), {"language": "backend", "files": FILES}))
    assert updated.language == "backend"
    assert len(updated.files) == 4


def test_compiler_projects_still_switch_between_compiler_languages():
    updated = run(CodingService(FakeSession(stored("python")), SETTINGS).update_project(USER_ID, uuid.uuid4(), {"language": "java"}))
    assert updated.language == "java"


def test_backend_share_is_accepted():
    user = SimpleNamespace(id=USER_ID, full_name="Learner")
    share = run(CodingService(FakeSession(), SETTINGS).create_share(user, None, "Notes API", "backend", FILES, None, "", ""))
    assert share.language == "backend"
    assert len(share.files) == 4


def test_backend_is_not_a_compiler_language():
    service = CodingService(FakeSession(), SETTINGS)
    assert "backend" not in {language["id"] for language in service.list_languages()}
    with pytest.raises(ValidationError, match="run in your browser"):
        run(service.execute(ExecuteRequest(language="backend", files=FILES), None, "127.0.0.1"))


def test_compiler_entry_point_matches_main_in_any_case(monkeypatch):
    sent = {}

    class FakeClient:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, url, json):
            sent.update(json)
            return SimpleNamespace(status_code=200, json=lambda: {"output": "", "statusCode": 200, "cpuTime": "0"}, text="")

    monkeypatch.setattr("services.coding_service.httpx.AsyncClient", FakeClient)
    settings = SimpleNamespace(**vars(SETTINGS), jdoodle_base_url="https://jdoodle.test", jdoodle_client_id="id", jdoodle_client_secret="secret", compiler_request_timeout_seconds=5)
    service = CodingService(FakeSession(), settings)
    files = [CodingFile(name="helpers.py", content="HELPERS"), CodingFile(name="main.py", content="MAIN")]
    run(service._call_jdoodle("python", None, files, ""))
    assert sent["script"] == "MAIN"
    files = [CodingFile(name="Helper.java", content="HELPER"), CodingFile(name="Main.java", content="JAVA MAIN")]
    run(service._call_jdoodle("java", None, files, ""))
    assert sent["script"] == "JAVA MAIN"


def test_compiler_files_cannot_be_in_folders():
    # The compiler runs one file, so a folder tree would be silently dropped.
    service = CodingService(FakeSession(), SETTINGS)
    files = [CodingFile(name="main.py", content=""), CodingFile(name="app/utils.py", content="")]
    with pytest.raises(ValidationError, match="cannot be in folders"):
        run(service.execute(ExecuteRequest(language="python", files=files), None, "127.0.0.1"))
    with pytest.raises(ValidationError, match="cannot be in folders"):
        run(service.create_project(USER_ID, "t", "python", files, None, "", ""))


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("../secret.py", "'..' segments"),
        ("/etc/passwd", "must be relative"),
        ("app//main.py", "empty"),
        ("app/./main.py", "'.'"),
        ("app\\main.py", "must be relative"),
        ("app/", "empty"),
        (".", "'.'"),
        ("app /main.py", "spaces"),
        ("app/ /main.py", "spaces"),
        (" x/ main.py", "spaces"),
        ("a\x00b.py", "control characters"),
        ("app/ma\nin.py", "control characters"),
        ("x\x7f.py", "control characters"),
        (".git/config", ".git"),
        ("app/.GIT/hooks/pre-commit", ".git"),
        ("x\ud800.py", "UTF-8"),
        ("a" * 121, "at most 120"),
        ("ma\x1fin.py", "control characters"),
        ("ma\x85in.py", "control characters"),
        ("a:b.py", "cannot contain"),
        ("x?.py", "cannot contain"),
        ("a<b>.py", "cannot contain"),
        ("con.py", "reserved name on Windows"),
        ("app/NUL", "reserved name on Windows"),
        ("app/lpt1.txt", "reserved name on Windows"),
        ("CONIN$.txt", "reserved name on Windows"),
        ("conout$", "reserved name on Windows"),
        ("COM¹.py", "reserved name on Windows"),
        ("lpt³.txt", "reserved name on Windows"),
        ("nul .py", "reserved name on Windows"),
        ("app/con  .txt", "reserved name on Windows"),
        ("app./m.py", "end with a dot"),
        ("app/m.", "end with a dot"),
    ],
)
def test_unsafe_file_paths_are_rejected(name, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        clean_file_path(name)


@pytest.mark.parametrize(
    "name",
    [
        "main.py", ".env", ".gitignore", "app/main.py", "frontend/css/style.css", "  app/main.py  ", "  " + "a" * 120,
        "app/console.py", "app/config.py", "Straße.py", "main.py\n", "\tmain.py", "\x1fmain.py", "main.py\x85", "com0.py",
    ],
)
def test_relative_file_paths_are_accepted(name):
    # Whitespace at either end is trimmed, as before folders were allowed.
    assert clean_file_path(name) == name.strip()
    assert CodingFile(name=name, content="").name == name.strip()


def test_codelab_files_keep_their_plain_name_rule():
    # CodeLab problems are single files; the project path rule does not apply to them.
    assert CodeFile(name="aux.py", language="python", content="").name == "aux.py"
    for name in ("app/main.py", "../main.py", ".."):
        with pytest.raises(ValueError, match="plain file name"):
            CodeFile(name=name, language="python", content="")


def files_of(*names):
    return [{"name": name, "content": ""} for name in names]


@pytest.mark.parametrize(
    ("names", "message"),
    [
        (("app/main.py", "app/main.py"), "Duplicate file: app/main.py"),
        (("main.py", "main.py"), "Duplicate file: main.py"),
        (("app", "app/main.py"), "cannot share the name app"),
        (("app/api", "app/api/routes/notes.py"), "cannot share the name app/api"),
        (("App/x.py", "app/x.py"), "same name on Windows and macOS"),
        (("README.md", "readme.md"), "same name on Windows and macOS"),
        (("App", "app/main.py"), "cannot share the name App"),
        (("App/a.py", "app/b.py"), "Folders app and App are the same name"),
        (("src/App/a.py", "src/app/b.py"), "Folders src/app and src/App are the same name"),
        (("café.py", "café.py"), "same name on Windows and macOS"),
    ],
)
def test_file_lists_must_form_a_file_tree(names, message):
    files = [CodingFile.model_validate(file) for file in files_of(*names)]
    user = SimpleNamespace(id=uuid.uuid4(), full_name="L")
    with pytest.raises(ValidationError, match=message):
        run(CodingService(FakeSession(), SETTINGS).create_project(USER_ID, "t", "backend", files, None, "", ""))
    with pytest.raises(ValidationError, match=message):
        run(CodingService(FakeSession(), SETTINGS).create_share(user, None, "t", "backend", files, None, "", ""))
    with pytest.raises(ValidationError, match=message):
        run(CodingService(FakeSession(stored("backend")), SETTINGS).update_project(USER_ID, uuid.uuid4(), {"files": files}))


def test_names_that_only_casefold_alike_are_different_files():
    # ß folds to "ss" but no file system treats Straße.py and strasse.py as one name.
    check_file_tree([CodingFile(name="Straße.py", content=""), CodingFile(name="strasse.py", content="")])


def test_names_alike_under_simple_case_folding_clash():
    # Final sigma and sigma both upper-case to Σ, so NTFS and APFS see one name.
    with pytest.raises(ValueError, match="same name on Windows and macOS"):
        check_file_tree([CodingFile(name="λόγος.py", content=""), CodingFile(name="λόγοσ.py", content="")])


@pytest.mark.parametrize("model", [ProjectCreateRequest, ShareCreateRequest, ProjectUpdateRequest, ExecuteRequest])
def test_file_lists_are_capped_while_parsing(model):
    extra = {"title": "t", "language": "backend"} if model is ProjectCreateRequest else {"language": "python"} if model in (ShareCreateRequest, ExecuteRequest) else {}
    model(files=files_of(*(f"app/m{i}.py" for i in range(MAX_PROJECT_FILES))), **extra)
    with pytest.raises(PydanticValidationError) as caught:
        model(files=files_of(*(f"app/m{i}.py" for i in range(MAX_PROJECT_FILES + 1))), **extra)
    assert caught.value.errors()[0]["type"] == "too_long"


def test_service_checks_the_file_tree_for_direct_callers():
    service = CodingService(FakeSession(), SETTINGS)
    twice = [CodingFile(name="main.py", content=""), CodingFile(name="main.py", content="")]
    with pytest.raises(ValidationError, match="Duplicate file"):
        run(service.create_project(USER_ID, "t", "python", twice, None, "", ""))
    with pytest.raises(ValidationError, match="Duplicate file"):
        run(service.create_share(SimpleNamespace(id=uuid.uuid4(), full_name="L"), None, "t", "python", twice, None, "", ""))
    project = stored("python")
    with pytest.raises(ValidationError, match="Duplicate file"):
        run(CodingService(FakeSession(project), SETTINGS).update_project(USER_ID, uuid.uuid4(), {"files": twice}))
    with pytest.raises(ValidationError) as caught:
        run(CodingService(FakeSession(project), SETTINGS).update_project(USER_ID, uuid.uuid4(), {"files": [CodingFile(name="../x.py", content="")]}))
    assert str(caught.value) == "Invalid file name ../x.py: Path must not contain empty, '.' or '..' segments."


def test_service_accepts_files_as_dicts():
    # model_dump() output, as a caller other than the router may pass.
    updated = run(CodingService(FakeSession(stored("backend")), SETTINGS).update_project(USER_ID, uuid.uuid4(), {"files": [file.model_dump() for file in FILES]}))
    assert len(updated.files) == 4
    with pytest.raises(ValidationError, match="Invalid file"):
        run(CodingService(FakeSession(stored("python")), SETTINGS).update_project(USER_ID, uuid.uuid4(), {"files": [{"name": "main.py"}]}))


LEGACY_FILES = files_of("Main.py", "main.py", "aux.py", "notes.", "a?.py", "dup.py", "dup.py")


def test_projects_saved_under_the_old_name_rule_can_still_be_edited_and_shared():
    project = stored("python")
    project.files = LEGACY_FILES
    project.share_id = None
    edited = [CodingFile.model_validate({**file, "content": "edited"}) for file in LEGACY_FILES]
    updated = run(CodingService(FakeSession(project), SETTINGS).update_project(USER_ID, uuid.uuid4(), {"files": edited}))
    assert [file["content"] for file in updated.files] == ["edited"] * 7
    share = run(CodingService(FakeSession(project), SETTINGS).create_share(SimpleNamespace(id=uuid.uuid4(), full_name="L"), uuid.uuid4(), "t", "python", edited, None, "", ""))
    assert len(share.files) == 7
    # A new file still has to meet the rule, and a saved name cannot gain more copies.
    for extra, message in (("con.py", "reserved name on Windows"), ("MAIN.py", "same name on Windows and macOS"), ("dup.py", "Duplicate file: dup.py")):
        with pytest.raises(ValidationError, match=message):
            run(CodingService(FakeSession(project), SETTINGS).update_project(USER_ID, uuid.uuid4(), {"files": [*edited, CodingFile(name=extra, content="")]}))


def test_a_rejected_update_leaves_the_project_untouched():
    project = stored("python")
    with pytest.raises(ValidationError, match="Duplicate file"):
        run(CodingService(FakeSession(project), SETTINGS).update_project(USER_ID, uuid.uuid4(), {"language": "java", "files": [CodingFile(name="Main.java", content="")] * 2}))
    assert project.language == "python"
    assert project.files == []


def test_a_rate_limited_share_does_no_database_work(monkeypatch):
    class NoDatabase(FakeSession):
        async def execute(self, statement):
            raise AssertionError("the database was queried")

    async def full(*_):
        return False

    service = CodingService(NoDatabase(), SETTINGS)
    monkeypatch.setattr(service._share_limiter, "consume", full)
    with pytest.raises(RateLimitedError):
        run(service.create_share(SimpleNamespace(id=uuid.uuid4(), full_name="L"), uuid.uuid4(), "t", "python", FILES[:1], None, "", ""))


def test_rejected_shares_do_not_use_up_the_hourly_limit():
    user = SimpleNamespace(id=uuid.uuid4(), full_name="L")
    service = CodingService(FakeSession(stored("python")), SETTINGS)

    def slots_used():
        return len(service._share_limiter._events.get(str(user.id), ()))

    with pytest.raises(ValidationError):
        run(service.create_share(user, uuid.uuid4(), "t", "backend", FILES, None, "", ""))
    with pytest.raises(ValidationError):
        run(service.create_share(user, None, "t", "cobol", FILES, None, "", ""))
    with pytest.raises(ValidationError):
        run(service.create_share(user, None, "t", "python", files_of("main.py", "main.py"), None, "", ""))
    assert slots_used() == 0
    run(service.create_share(user, None, "t", "backend", FILES, None, "", ""))
    assert slots_used() == 1


@pytest.mark.parametrize(("project_language", "share_language"), [("python", "backend"), ("backend", "python")])
def test_share_must_match_its_projects_kind(project_language, share_language):
    project = stored(project_language)
    project.share_id = None
    service = CodingService(FakeSession(project), SETTINGS)
    with pytest.raises(ValidationError, match="FastAPI projects cannot be switched"):
        run(service.create_share(SimpleNamespace(id=uuid.uuid4(), full_name="L"), uuid.uuid4(), "t", share_language, FILES, None, "", ""))
    assert project.share_id is None


def test_stored_rows_load_whatever_their_names():
    # The write rules apply to new writes only; reading an older row must never fail.
    now = datetime.now(timezone.utc)
    files = files_of("main.py", "main.py", ".git", "a\tb.py")
    row = SimpleNamespace(id=uuid.uuid4(), title="t", description="", language="python", files=files, stdin="", is_public=False, share_id=None, created_at=now, updated_at=now)
    assert len(ProjectResponse.model_validate(row).files) == 4
    share = SimpleNamespace(share_id="s", title="t", language="python", files=files, stdin="", stdout="", created_at=now, author_display_name=None)
    assert len(ShareResponse.model_validate(share).files) == 4


def test_unknown_project_language_is_still_rejected():
    with pytest.raises(ValidationError, match="Unknown language"):
        run(CodingService(FakeSession(), SETTINGS).create_project(USER_ID, "x", "cobol", FILES, None, "", ""))


# ---- through the real router and error handlers: no request may end in a 500 ----------


def client_for(session):
    app = FastAPI()
    app.include_router(router)
    register_error_handlers(app)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=USER_ID, full_name="Learner", role="client")
    app.dependency_overrides[get_coding_service] = lambda: CodingService(session, SETTINGS)
    return TestClient(app, raise_server_exceptions=False)


BODY = {"title": "Notes API", "language": "backend", "files": [file.model_dump() for file in FILES], "stdin": ""}


def test_http_create_backend_project_returns_every_file():
    response = client_for(FakeSession()).post("/api/projects", json=BODY)
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["language"] == "backend"
    assert [file["name"] for file in data["files"]] == ["app/main.py", "schema.sql", "requirements.txt", "frontend/index.html"]


def test_http_get_and_update_backend_project():
    stored = SimpleNamespace(
        id=uuid.uuid4(), title="Notes API", description="", language="backend", files=BODY["files"], stdin="",
        is_public=False, share_id=None, created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    )
    client = client_for(FakeSession(stored))
    got = client.get(f"/api/projects/{stored.id}")
    assert got.status_code == 200, got.text
    assert len(got.json()["files"]) == 4
    files = [*BODY["files"], {"name": "app/routes/notes.py", "content": "# routes\n"}]
    put = client.put(f"/api/projects/{stored.id}", json={"files": files})
    assert put.status_code == 200, put.text
    assert put.json()["files"][-1]["name"] == "app/routes/notes.py"


def test_http_backend_share_is_created():
    response = client_for(FakeSession()).post("/api/shares", json={"language": "backend", "files": BODY["files"]})
    assert response.status_code == 200, response.text
    assert response.json()["url"].startswith("/s/")


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({**BODY, "files": [{"name": "../escape.py", "content": ""}]}, 422),
        ({**BODY, "language": "cobol"}, 422),
        ({**BODY, "files": [{"name": "app/main.py", "content": "x" * 262145}]}, 413),
        ({**BODY, "files": []}, 422),
        ({**BODY, "files": files_of("app/main.py", "app/main.py")}, 422),
        ({**BODY, "files": files_of("app", "app/main.py")}, 422),
        ({**BODY, "files": files_of("app /main.py")}, 422),
    ],
)
def test_http_bad_backend_projects_are_client_errors_not_500(body, status):
    response = client_for(FakeSession()).post("/api/projects", json=body)
    assert response.status_code == status, response.text
    assert "detail" in response.json()


@pytest.mark.parametrize(
    ("url", "body"),
    [
        ("/api/projects", {**BODY, "files": [{"name": "main.py", "content": "\ud800"}]}),
        ("/api/projects", {**BODY, "files": [{"name": "m\ud800.py", "content": ""}]}),
        ("/api/projects", {"title": "t", "language": "python", "source": "\ud800"}),
        ("/api/projects", {**BODY, "description": "\ud800"}),
        ("/api/shares", {"language": "python", "files": [{"name": "main.py", "content": ""}], "stdout": "\ud800"}),
        ("/api/compiler/execute", {"language": "python", "files": [{"name": "main.py", "content": "\ud800"}]}),
    ],
)
def test_http_text_that_is_not_utf8_is_a_422_not_500(url, body):
    # A lone surrogate from a JSON "\ud800" escape cannot be encoded or stored.
    response = client_for(FakeSession()).post(url, json=body)
    assert response.status_code == 422, response.text


def test_http_executing_a_backend_workspace_is_a_422():
    response = client_for(FakeSession()).post("/api/compiler/execute", json={"language": "backend", "files": BODY["files"]})
    assert response.status_code == 422, response.text
    assert "run in your browser" in response.json()["detail"]


def test_http_switching_a_backend_project_language_is_a_422():
    project = SimpleNamespace(
        id=uuid.uuid4(), title="Notes API", description="", language="backend", files=BODY["files"], stdin="",
        is_public=False, share_id=None, created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    )
    response = client_for(FakeSession(project)).put(f"/api/projects/{project.id}", json={"language": "python"})
    assert response.status_code == 422, response.text
    assert project.language == "backend"
