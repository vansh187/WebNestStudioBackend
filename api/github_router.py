from typing import Literal

from fastapi import APIRouter, Depends, Query, Response, status

from core.dependencies import get_current_user, get_github_service
from database.models import User
from schemas.github_schemas import (
    BranchListResponse,
    CommitRequest,
    CommitResponse,
    GitHubConnectionResponse,
    OAuthCallbackRequest,
    OAuthCallbackResponse,
    OAuthStartRequest,
    OAuthStartResponse,
    RepoCreateRequest,
    RepoCreateResponse,
    RepoListResponse,
    SnapshotResponse,
)
from services.github_service import GitHubService

# CodeLab GitHub sync - contract in GITHUB_INTEGRATION_BACKEND.md section 2.
# Every route needs a WebNest login; GitHub-side failures never answer 401.
router = APIRouter(prefix="/api/github", tags=["github"])


@router.get("/connection", response_model=GitHubConnectionResponse)
async def get_connection(
    current_user: User = Depends(get_current_user),
    github: GitHubService = Depends(get_github_service),
) -> GitHubConnectionResponse:
    return await github.get_connection(current_user)


@router.delete("/connection", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect(
    current_user: User = Depends(get_current_user),
    github: GitHubService = Depends(get_github_service),
) -> Response:
    await github.disconnect(current_user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/oauth/start", response_model=OAuthStartResponse)
async def start_oauth(
    payload: OAuthStartRequest,
    current_user: User = Depends(get_current_user),
    github: GitHubService = Depends(get_github_service),
) -> OAuthStartResponse:
    return await github.start_oauth(current_user, payload.return_to)


@router.post("/oauth/callback", response_model=OAuthCallbackResponse)
async def complete_oauth(
    payload: OAuthCallbackRequest,
    current_user: User = Depends(get_current_user),
    github: GitHubService = Depends(get_github_service),
) -> OAuthCallbackResponse:
    return await github.complete_oauth(current_user, payload.code, payload.state)


@router.get("/repos", response_model=RepoListResponse)
async def list_repos(
    page: int = Query(default=1, ge=1, le=1000),
    per_page: int = Query(default=30, ge=1, le=100),
    q: str | None = Query(default=None, max_length=100),
    current_user: User = Depends(get_current_user),
    github: GitHubService = Depends(get_github_service),
) -> RepoListResponse:
    return await github.list_repos(current_user, page, per_page, q)


@router.post("/repos", response_model=RepoCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_repo(
    payload: RepoCreateRequest,
    current_user: User = Depends(get_current_user),
    github: GitHubService = Depends(get_github_service),
) -> RepoCreateResponse:
    return await github.create_repo(current_user, payload)


@router.get("/repos/{owner}/{repo}/branches", response_model=BranchListResponse)
async def list_branches(
    owner: str,
    repo: str,
    current_user: User = Depends(get_current_user),
    github: GitHubService = Depends(get_github_service),
) -> BranchListResponse:
    return await github.list_branches(current_user, owner, repo)


@router.get("/repos/{owner}/{repo}/snapshot", response_model=SnapshotResponse)
async def get_snapshot(
    owner: str,
    repo: str,
    branch: str | None = Query(default=None, max_length=255),
    mode: Literal["auto", "backend", "web", "python", "java"] = Query(default="auto"),
    current_user: User = Depends(get_current_user),
    github: GitHubService = Depends(get_github_service),
) -> SnapshotResponse:
    return await github.snapshot(current_user, owner, repo, branch, mode)


@router.post("/repos/{owner}/{repo}/commits", response_model=CommitResponse, status_code=status.HTTP_201_CREATED)
async def create_commit(
    owner: str,
    repo: str,
    payload: CommitRequest,
    current_user: User = Depends(get_current_user),
    github: GitHubService = Depends(get_github_service),
) -> CommitResponse:
    return await github.commit(current_user, owner, repo, payload)
