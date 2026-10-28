from fastapi import APIRouter, Response, status

from app.deps import SessionDep
from app.schemas import LoginIn, RefreshIn, RegisterIn, TokenPair, UserOut
from app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterIn, session: SessionDep) -> UserOut:
    user = await auth_service.register(session, body.email, body.password, body.base_currency)
    return UserOut.model_validate(user)


@router.post("/login", response_model=TokenPair)
async def login(body: LoginIn, session: SessionDep) -> TokenPair:
    return await auth_service.login(session, body.email, body.password)


@router.post("/refresh", response_model=TokenPair)
async def refresh(body: RefreshIn, session: SessionDep) -> TokenPair:
    return await auth_service.refresh(session, body.refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: RefreshIn, session: SessionDep) -> Response:
    await auth_service.logout(session, body.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
