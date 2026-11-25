from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Role, User


@dataclass(frozen=True)
class Principal:
    id: int
    name: str
    role: str


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_user(session: Session, name: str, role: str, token: str | None = None) -> str:
    """Create a user and return the plaintext token once. Only the hash is stored."""
    if role not in (Role.VIEWER, Role.ANALYST, Role.APPROVER):
        raise ValueError(f"Unknown role: {role}")
    token = token or secrets.token_urlsafe(32)
    session.add(User(name=name, role=role, token_hash=hash_token(token)))
    session.commit()
    return token


def authenticate(session: Session, token: str) -> Principal | None:
    user = session.scalar(
        select(User).where(User.token_hash == hash_token(token), User.is_active.is_(True))
    )
    return Principal(user.id, user.name, user.role) if user else None
