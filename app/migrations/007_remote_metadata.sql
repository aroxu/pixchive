ALTER TABLE artist ADD COLUMN account TEXT;
ALTER TABLE artwork ADD COLUMN rating_evidence TEXT;
ALTER TABLE asset ADD COLUMN provenance TEXT;
ALTER TABLE download_task ADD COLUMN provenance TEXT;
CREATE INDEX asset_inventory_role ON asset(artwork_id, role);
