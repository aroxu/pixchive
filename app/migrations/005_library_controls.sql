-- Explicit local tag catalog and download policy accounting.
ALTER TABLE tag ADD COLUMN local_created INTEGER NOT NULL DEFAULT 0;
UPDATE tag SET local_created = 1 WHERE id IN (
 SELECT tag_id FROM artwork_tag WHERE source = 'local'
);
ALTER TABLE collection_item ADD COLUMN download_skipped INTEGER NOT NULL DEFAULT 0;
CREATE TABLE integrity_issue (
 id INTEGER PRIMARY KEY,
 job_id TEXT NOT NULL REFERENCES job(id),
 asset_id TEXT NOT NULL REFERENCES asset(id),
 code TEXT NOT NULL,
 UNIQUE(job_id, asset_id)
);
