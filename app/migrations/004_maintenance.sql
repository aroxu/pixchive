-- P4: refresh policy, scheduled sync, local deletion records, and artist avatars.
ALTER TABLE artwork ADD COLUMN metadata_fetched_at INTEGER;
ALTER TABLE artwork ADD COLUMN listing_fingerprint TEXT;
UPDATE artwork SET metadata_fetched_at = (
 SELECT max(s.fetched_at) FROM artwork_snapshot s WHERE s.artwork_id = artwork.id
);
ALTER TABLE artist ADD COLUMN avatar_url TEXT;
ALTER TABLE artist ADD COLUMN avatar_fetched_url TEXT;
ALTER TABLE artist ADD COLUMN avatar_path TEXT;
ALTER TABLE artist ADD COLUMN avatar_media_type TEXT;
ALTER TABLE artist_name ADD COLUMN first_seen INTEGER;
ALTER TABLE artist_name ADD COLUMN last_seen INTEGER;
ALTER TABLE collection_item ADD COLUMN outcome TEXT;
ALTER TABLE job ADD COLUMN mode TEXT;
ALTER TABLE job ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE job ADD COLUMN summary TEXT;
UPDATE job SET mode = 'full' WHERE kind = 'sync';
ALTER TABLE tombstone ADD COLUMN state TEXT NOT NULL DEFAULT 'deleted';
ALTER TABLE tombstone ADD COLUMN job_id TEXT;
ALTER TABLE tombstone ADD COLUMN file_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tombstone ADD COLUMN freed_bytes INTEGER NOT NULL DEFAULT 0;
CREATE INDEX collection_item_artwork_id ON collection_item (artwork_id);
CREATE INDEX job_kind_scope ON job (kind, scope);
CREATE INDEX asset_artwork_role_state ON asset (artwork_id, role, state)
