from concurrent.futures import ThreadPoolExecutor

from app.core.db import db
from app.models.entities import Artwork, ArtworkTag, Bookmark, Job, Outbox
from app.services import jobs, library
from tests.test_library import remote


def test_wal_concurrent_metadata_and_job_creation(client):
    def write(index):
        with db.connection_context():
            library.ingest(remote(), "public")
            return jobs.enqueue_integrity(f"parallel-{index}").id

    with ThreadPoolExecutor(max_workers=6) as pool:
        ids = list(pool.map(write, range(18)))
    assert len(set(ids)) == 1
    assert Job.select().count() == Outbox.select().count() == 1
    assert Artwork.select().count() == Bookmark.select().count() == 1
    assert ArtworkTag.select().count() == 1
