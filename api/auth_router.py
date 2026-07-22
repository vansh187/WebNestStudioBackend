from fastapi import APIRouter, BackgroundTasks, Depends, Request, status

from core.dependencies import get_auth_service, get_client_ip, get_current_user, get_email_service
from database.models import User
from schemas.auth_schemas import (
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    SignupRequest,
    TokenResponse,
    UserResponse,
    VerifyOtpRequest,
)
from services.auth_service import AuthService
from services.email_service import EmailService

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/signup", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def signup(
    payload: SignupRequest,
    background_tasks: BackgroundTasks,
    auth_service: AuthService = Depends(get_auth_service),
    email_service: EmailService = Depends(get_email_service),
) -> User:
    user, otp_code = await auth_service.signup(payload.full_name, payload.email, payload.phone_number, payload.password)
    # Sending the OTP email is a slow, best-effort network call - it must never
    # block this response (Render's outbound SMTP can hang for tens of seconds).
    background_tasks.add_task(email_service.send_otp_email, user.email, otp_code, "signup")
    return user


@router.post("/verify-otp", response_model=UserResponse)
async def verify_otp(payload: VerifyOtpRequest, auth_service: AuthService = Depends(get_auth_service)) -> User:
    return await auth_service.verify_otp(payload.email, payload.otp_code, payload.purpose)


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    auth_service: AuthService = Depends(get_auth_service),
    client_ip: str | None = Depends(get_client_ip),
) -> TokenResponse:
    access_token, refresh_token, _ = await auth_service.login(
        payload.email, payload.password, request.headers.get("user-agent"), client_ip
    )
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(payload: RefreshRequest, auth_service: AuthService = Depends(get_auth_service)) -> TokenResponse:
    access_token, new_refresh_token = await auth_service.refresh(payload.refresh_token)
    return TokenResponse(access_token=access_token, refresh_token=new_refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(payload: LogoutRequest, auth_service: AuthService = Depends(get_auth_service)) -> None:
    await auth_service.logout(payload.refresh_token)


@router.get("/me", response_model=UserResponse)
async def me(current_user: User = Depends(get_current_user)) -> User:
    return current_user
