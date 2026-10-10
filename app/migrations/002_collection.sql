ALTER TABLE job ADD COLUMN scope TEXT;
ALTER TABLE job ADD COLUMN phase TEXT NOT NULL DEFAULT 'queued';
ALTER TABLE job ADD COLUMN retry_at INTEGER;
CREATE TABLE collection_run (
 id INTEGER PRIMARY KEY,
 job_id TEXT NOT NULL REFERENCES job(id),
 scope TEXT NOT NULL,
 generation TEXT NOT NULL,
 cursor TEXT,
 state TEXT NOT NULL DEFAULT 'scanning',
 UNIQUE(job_id, scope)
);
CREATE TABLE collection_item (
 id INTEGER PRIMARY KEY,
 run_id INTEGER NOT NULL REFERENCES collection_run(id),
 artwork_id TEXT NOT NULL,
 error_code TEXT,
 UNIQUE(run_id, artwork_id)
);
CREATE TABLE download_task (
 id INTEGER PRIMARY KEY,
 job_id TEXT NOT NULL REFERENCES job(id),
 artwork_id TEXT NOT NULL REFERENCES artwork(id),
 page INTEGER NOT NULL,
 url TEXT NOT NULL,
 frames TEXT,
 asset_id TEXT REFERENCES asset(id),
 state TEXT NOT NULL DEFAULT 'pending',
 error_code TEXT,
 UNIQUE(job_id, artwork_id, page)
);
CREATE INDEX download_task_source ON download_task(artwork_id, page, url, state);
CREATE TABLE ugoira_bundle (
 id TEXT PRIMARY KEY,
 artwork_id TEXT NOT NULL REFERENCES artwork(id),
 revision TEXT NOT NULL,
 archive_id TEXT NOT NULL REFERENCES asset(id),
 timing_id TEXT NOT NULL REFERENCES asset(id),
 thumbnail_id TEXT REFERENCES asset(id),
 video_id TEXT REFERENCES asset(id),
 state TEXT NOT NULL DEFAULT 'pending',
 error_code TEXT,
 UNIQUE(artwork_id, revision)
);
CREATE TABLE job_dependency (
 id INTEGER PRIMARY KEY,
 parent_id TEXT NOT NULL REFERENCES job(id),
 child_id TEXT NOT NULL REFERENCES job(id),
 UNIQUE(parent_id, child_id)
);
