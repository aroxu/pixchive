ALTER TABLE asset ADD COLUMN thumbnail_id TEXT REFERENCES asset(id) ON DELETE SET NULL;
CREATE INDEX asset_thumbnail_id ON asset(thumbnail_id);
