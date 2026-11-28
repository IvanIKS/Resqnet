"""Authentication routes: register, login, and current user."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.schemas import AuthTokenOut, LoginIn, RegisterIn, UserOut
from database import get_session
from models import User, UserRole
from services.auth import create_access_token, get_current_user, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=AuthTokenOut, status_code=201)
def register(payload: RegisterIn, session: Session = Depends(get_session)):
    existing = session.scalar(select(User).where(User.username == payload.username))
    if existing is not None:
        raise HTTPException(status_code=409, detail="username already exists")

    role_row = session.scalar(select(UserRole).where(UserRole.role == payload.role))
    if role_row is None:
        raise HTTPException(status_code=400, detail=f"unknown role: {payload.role}")

    user = User(
        username=payload.username,
        password=hash_password(payload.password),
        role=role_row.id,
    )
    session.add(user)
    session.commit()
    session.refresh(user)

    token = create_access_token(user.id, user.username, role_row.role)
    return AuthTokenOut(
        access_token=token,
        user=UserOut(id=user.id, username=user.username, role=role_row.role),
    )


@router.post("/login", response_model=AuthTokenOut)
def login(payload: LoginIn, session: Session = Depends(get_session)):
    row = session.execute(
        select(User, UserRole.role)
        .join(UserRole, User.role == UserRole.id)
        .where(User.username == payload.username)
    ).first()
    if row is None or not verify_password(payload.password, row[0].password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid username or password"
        )

    user, role_name = row
    token = create_access_token(user.id, user.username, role_name)
    return AuthTokenOut(
        access_token=token,
        user=UserOut(id=user.id, username=user.username, role=role_name),
    )


@router.get("/me", response_model=UserOut)
def me(current: tuple[User, str] = Depends(get_current_user)):
    user, role_name = current
    return UserOut(id=user.id, username=user.username, role=role_name)
