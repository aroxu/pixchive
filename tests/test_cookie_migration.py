from pathlib import Path

import pytest

from app.core.db import db, initialize
from app.core.errors import DomainError
from app.migrations.runner import migrate
from app.models.entities import CollectionRun, Credential, Job, PixivAccount
from app.services import accounts


def test_v2_upgrade_preserves_legacy_secret_and_resets_incompatible_cursor(settings):
    if not db.is_closed():
        db.close()
    initialize(settings.data_dir)
    try:
        db.execute_sql("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        for version, name in ((1, "001_initial.sql"), (2, "002_collection.sql")):
            for statement in (Path("app/migrations") / name).read_text().split(";"):
                if statement.strip():
                    db.execute_sql(statement)
            db.execute_sql("INSERT INTO schema_version VALUES (?)", (version,))
        PixivAccount.create(id=1, pixiv_id="42", status="connected", validated_at=123)
        db.execute_sql(
            "INSERT INTO credential (account_id, ciphertext) VALUES (1, ?)",
            ("preserve-encrypted-legacy-value",),
        )
        # Raw v2 rows: the current model has columns that the old schema does not.
        db.execute_sql(
            """INSERT INTO job (id, kind, scope, state, idempotency_key, active_key, lease_key,
            processed, failed, created_at, updated_at, phase)
            VALUES ('old', 'sync', 'public', 'paused', 'old-job', 'sync:1:public',
            'account:1', 0, 0, 0, 0, 'queued')"""
        )
        run = CollectionRun.create(
            job="old", scope="public", generation="old-generation", cursor="987654"
        )
        migrate()
        assert Credential.get().kind == "refresh_token"
        assert Credential.get().ciphertext == "preserve-encrypted-legacy-value"
        assert PixivAccount.get().status == "cookie_required"
        assert PixivAccount.get().pixiv_id == "42"
        assert PixivAccount.get().validated_at is None
        assert Job.get_by_id("old").state == "paused"
        assert Job.get_by_id("old").mode == "full"
        assert CollectionRun.get_by_id(run.id).cursor is None
        assert CollectionRun.get_by_id(run.id).generation != "old-generation"
        with pytest.raises(DomainError, match="cookie_required"):
            accounts.load_credential(settings)
        # Storing a cookie replaces the old secret and still requires validation.
        accounts.store_credential(settings, "42_new_synthetic_cookie")
        assert accounts.list_accounts()[0]["credential_kind"] == "pixiv_cookie"
        migrate()
        assert accounts.load_credential(settings)[0] == "42_new_synthetic_cookie"
        assert PixivAccount.get().status == "unverified"
    finally:
        db.close()
