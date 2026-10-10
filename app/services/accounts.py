import re

from cryptography.fernet import Fernet, InvalidToken

from app.adapters.pixiv.web import COOKIE_PATTERN
from app.core.config import Settings
from app.core.db import db
from app.core.errors import DomainError
from app.core.time import now
from app.models.entities import Credential, PixivAccount


def list_accounts() -> list[dict]:
    credentials = {c.account_id: c for c in Credential.select()}
    return [
        {
            "id": a.id,
            "pixiv_id": a.pixiv_id,
            "status": a.status,
            "validated_at": a.validated_at,
            "credential_present": a.id in credentials,
            "credential_kind": credentials[a.id].kind if a.id in credentials else None,
        }
        for a in PixivAccount.select()
    ]


def store_credential(settings: Settings, cookie: str) -> dict:
    if not re.fullmatch(COOKIE_PATTERN, cookie):
        raise DomainError("invalid_cookie", "PHPSESSID 쿠키의 값만 입력해 주세요.", 422)
    try:
        cipher = Fernet(settings.key_file.read_bytes().strip())
    except (OSError, ValueError):
        raise DomainError(
            "key_unavailable", "서버의 자격 증명 보관 키를 확인해 주세요.", 503
        ) from None
    encrypted = cipher.encrypt(cookie.encode()).decode()
    with db.atomic("IMMEDIATE"):
        account, _ = PixivAccount.get_or_create(id=1)
        account.status = "unverified"
        account.validated_at = None
        account.save()
        Credential.insert(account=account, ciphertext=encrypted, kind="pixiv_cookie").on_conflict(
            conflict_target=[Credential.account],
            update={Credential.ciphertext: encrypted, Credential.kind: "pixiv_cookie"},
        ).execute()
    return list_accounts()[0]


def disconnect():
    with db.atomic("IMMEDIATE"):
        Credential.delete().execute()
        PixivAccount.update(status="disconnected", validated_at=None).execute()


def load_credential(settings: Settings) -> tuple[str, str]:
    credential = Credential.get_or_none(Credential.account == 1)
    if not credential:
        raise DomainError("credential_missing", "먼저 Pixiv 쿠키를 등록해 주세요.", 409)
    if credential.kind != "pixiv_cookie":
        raise DomainError("cookie_required", "PHPSESSID 쿠키를 새로 등록해 주세요.", 409)
    try:
        cookie = (
            Fernet(settings.key_file.read_bytes().strip())
            .decrypt(credential.ciphertext.encode())
            .decode()
        )
    except (OSError, ValueError, InvalidToken):
        raise DomainError("key_unavailable", "자격 증명 보관 키를 확인해 주세요.", 503) from None
    return cookie, credential.ciphertext


def credential_current(expected: str) -> bool:
    return (
        Credential.select()
        .where(
            (Credential.account == 1)
            & (Credential.ciphertext == expected)
            & (Credential.kind == "pixiv_cookie")
        )
        .exists()
    )


def validated(expected: str, pixiv_id: str):
    mismatch = False
    with db.atomic("IMMEDIATE"):
        if not credential_current(expected):
            raise DomainError("credential_changed", "쿠키가 변경되어 작업을 중단했습니다.", 409)
        account = PixivAccount.get_by_id(1)
        if account.pixiv_id and account.pixiv_id != pixiv_id:
            account.status = "account_mismatch"
            account.save()
            mismatch = True
        else:
            account.pixiv_id, account.status, account.validated_at = pixiv_id, "connected", now()
            account.save()
    if mismatch:
        raise DomainError("account_mismatch", "기존과 같은 Pixiv 계정의 쿠키를 등록해 주세요.", 409)


def authentication_failed(expected: str):
    with db.atomic("IMMEDIATE"):
        if credential_current(expected):
            PixivAccount.update(status="auth_expired").where(PixivAccount.id == 1).execute()
