import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

from app.core.config import Settings
from app.core.db import db
from app.core.errors import DomainError
from app.core.time import now
from app.models.entities import AdminUser, LoginAttempt, Session

hasher = PasswordHasher()
dummy_hash = hasher.hash(secrets.token_urlsafe(24))
SESSION_SECONDS = 60 * 60 * 24 * 7


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def status(token: str | None) -> dict:
    session = get_session(token)
    return {
        "setup_required": not AdminUser.select().exists(),
        "authenticated": session is not None,
        "csrf_token": session.csrf if session else None,
        "username": session.admin.username if session else None,
    }


def get_session(token: str | None) -> Session | None:
    if not token:
        return None
    return Session.get_or_none((Session.id == digest(token)) & (Session.expires_at > now()))


def require_session(token: str | None) -> Session:
    session = get_session(token)
    if not session:
        raise DomainError("unauthenticated", "로그인이 필요합니다.", 401)
    return session


def throttle(client: str):
    key = digest(client)
    with db.atomic("IMMEDIATE"):
        attempt, _ = LoginAttempt.get_or_create(
            id=key, defaults={"window_start": now(), "count": 0}
        )
        if now() - attempt.window_start >= 300:
            attempt.window_start, attempt.count = now(), 0
        if attempt.count >= 20:
            raise DomainError("rate_limited", "잠시 후 다시 시도해 주세요.", 429)
        attempt.count += 1
        attempt.save()


def new_session(admin: AdminUser) -> str:
    token = secrets.token_urlsafe(32)
    Session.delete().where(Session.expires_at <= now()).execute()
    Session.create(
        id=digest(token),
        admin=admin,
        csrf=secrets.token_urlsafe(32),
        expires_at=now() + SESSION_SECONDS,
    )
    return token


def setup(settings: Settings, username: str, password: str, setup_token: str) -> str:
    try:
        expected = settings.setup_token_file.read_text().strip()
    except OSError:
        raise DomainError(
            "setup_unavailable", "서버의 초기 설정 키를 확인해 주세요.", 503
        ) from None
    if not expected or not secrets.compare_digest(setup_token, expected):
        raise DomainError("invalid_setup_token", "초기 설정 키가 올바르지 않습니다.", 403)
    password_hash = hasher.hash(password)
    with db.atomic("IMMEDIATE"):
        if AdminUser.select().exists():
            raise DomainError("already_setup", "관리자가 이미 설정되어 있습니다.", 409)
        admin = AdminUser.create(username=username, password_hash=password_hash)
        return new_session(admin)


def login(username: str, password: str) -> str:
    admin = AdminUser.get_or_none(AdminUser.username == username)
    try:
        valid = hasher.verify(admin.password_hash if admin else dummy_hash, password)
    except VerificationError:
        valid = False
    if not admin or not valid:
        raise DomainError("invalid_credentials", "이름 또는 비밀번호를 확인해 주세요.", 401)
    return new_session(admin)


def logout(token: str):
    Session.delete().where(Session.id == digest(token)).execute()
