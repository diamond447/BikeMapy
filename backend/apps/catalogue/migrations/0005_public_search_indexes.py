from django.db import migrations

POSTGRESQL_SQL = """
CREATE EXTENSION IF NOT EXISTS pg_trgm;
-- The three SearchVector expressions below intentionally mirror the runtime
-- ``config='simple'`` vectors in apps.api.views_routes.filter_routes.
CREATE INDEX IF NOT EXISTS catalogue_route_titles_fts_idx ON catalogue_route USING gin ((
    to_tsvector('simple', coalesce(display_title, '')) ||
    to_tsvector('simple', coalesce(generated_title, '')) ||
    to_tsvector('simple', coalesce(thread_title, ''))
));
CREATE INDEX IF NOT EXISTS catalogue_route_titles_trgm_idx
    ON catalogue_route USING gin (display_title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS catalogue_route_generated_title_trgm_idx
    ON catalogue_route USING gin (generated_title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS catalogue_thread_title_fts_idx ON catalogue_forumthread USING gin ((
    to_tsvector('simple', coalesce(title, '')) ||
    to_tsvector('simple', coalesce(locality, ''))
));
CREATE INDEX IF NOT EXISTS catalogue_thread_title_trgm_idx
    ON catalogue_forumthread USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS catalogue_thread_locality_trgm_idx
    ON catalogue_forumthread USING gin (locality gin_trgm_ops);
CREATE INDEX IF NOT EXISTS catalogue_author_username_fts_idx ON catalogue_forumauthor USING gin (
    to_tsvector('simple', coalesce(username, ''))
);
CREATE INDEX IF NOT EXISTS catalogue_author_username_trgm_idx
    ON catalogue_forumauthor USING gin (username gin_trgm_ops);
"""

POSTGRESQL_REVERSE_SQL = """
DROP INDEX IF EXISTS catalogue_route_titles_fts_idx;
DROP INDEX IF EXISTS catalogue_route_titles_trgm_idx;
DROP INDEX IF EXISTS catalogue_route_generated_title_trgm_idx;
DROP INDEX IF EXISTS catalogue_thread_title_fts_idx;
DROP INDEX IF EXISTS catalogue_thread_title_trgm_idx;
DROP INDEX IF EXISTS catalogue_thread_locality_trgm_idx;
DROP INDEX IF EXISTS catalogue_author_username_fts_idx;
DROP INDEX IF EXISTS catalogue_author_username_trgm_idx;
"""


def install_search_indexes(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(POSTGRESQL_SQL)


def uninstall_search_indexes(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(POSTGRESQL_REVERSE_SQL)


class Migration(migrations.Migration):
    dependencies = [("catalogue", "0004_routeheatmapcell_routebrowsegeometry_and_more")]

    operations = [migrations.RunPython(install_search_indexes, uninstall_search_indexes)]
