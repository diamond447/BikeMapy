-- The postgis image ships the extension, but enabling it explicitly keeps a
-- freshly-created local database deterministic.
CREATE EXTENSION IF NOT EXISTS postgis;
