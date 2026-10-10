CREATE INDEX artwork_snapshot_history ON artwork_snapshot(artwork_id, fetched_at DESC, id DESC);
