from pathlib import Path

from app.core.db import db


def migrate():
    # The version check and migration share a reserved write lock across web/worker starts.
    with db.atomic("IMMEDIATE"):
        db.execute_sql("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        installed = {row[0] for row in db.execute_sql("SELECT version FROM schema_version")}
        migrations = sorted(Path(__file__).parent.glob("[0-9]*.sql"))
        known = {int(path.stem.split("_", 1)[0]) for path in migrations}
        if installed - known:
            raise RuntimeError("Database schema is newer than this application")
        for path in migrations:
            version = int(path.stem.split("_", 1)[0])
            if version in installed:
                continue
            for statement in path.read_text().split(";"):
                if statement.strip():
                    db.execute_sql(statement)
            db.execute_sql("INSERT INTO schema_version (version) VALUES (?)", (version,))
