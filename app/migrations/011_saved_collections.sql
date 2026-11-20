ALTER TABLE artwork ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0 CHECK (favorite IN (0, 1));
CREATE INDEX artwork_favorite ON artwork(favorite);
CREATE TABLE saved_collection (
 id TEXT NOT NULL PRIMARY KEY,
 name TEXT NOT NULL,
 name_key TEXT NOT NULL UNIQUE,
 created_at INTEGER NOT NULL
);
CREATE TABLE saved_collection_artwork (
 id INTEGER NOT NULL PRIMARY KEY,
 collection_id TEXT NOT NULL REFERENCES saved_collection(id) ON DELETE CASCADE,
 artwork_id TEXT NOT NULL REFERENCES artwork(id) ON DELETE CASCADE,
 added_at INTEGER NOT NULL,
 UNIQUE(collection_id, artwork_id)
);
CREATE INDEX saved_collection_artwork_artwork ON saved_collection_artwork(artwork_id);
