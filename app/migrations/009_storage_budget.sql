ALTER TABLE job ADD COLUMN storage_budget TEXT;
CREATE TABLE storage_reservation (
    job_id TEXT NOT NULL PRIMARY KEY REFERENCES job(id) ON DELETE CASCADE,
    owner TEXT NOT NULL,
    bytes INTEGER NOT NULL CHECK (bytes >= 0)
);
