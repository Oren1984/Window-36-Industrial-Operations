"""Local demo authentication with bcrypt-hashed passwords."""

from __future__ import annotations

import logging

import bcrypt
from sqlalchemy import select

from window36.logging_setup import log
from window36.models import AppUser
from window36.services.common import Actor

logger = logging.getLogger("window36.auth")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=10)).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("ascii"))
    except ValueError:
        return False


def authenticate(session, username: str, password: str) -> Actor | None:
    user = session.scalar(select(AppUser).where(AppUser.username == username.strip()))
    if user is None or not verify_password(password, user.password_hash):
        log(logger, logging.WARNING, "login failed", user=username[:60], status="denied")
        return None
    log(logger, logging.INFO, "login", user=user.username, role=user.role, status="ok")
    return Actor.from_user(user)
