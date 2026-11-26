"""Rules the database itself enforces, so they hold even if the app has a bug or someone edits tables directly."""

# The activity log can only grow: any attempt to change or delete an entry is refused by PostgreSQL.
ACTIVITY_LOG_APPEND_ONLY = """
CREATE OR REPLACE FUNCTION activity_log_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'The activity log can only be added to; entries cannot be changed or deleted.';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS activity_log_append_only ON activity_log;
CREATE TRIGGER activity_log_append_only BEFORE UPDATE OR DELETE ON activity_log
    FOR EACH ROW EXECUTE FUNCTION activity_log_append_only();
"""

REMOVE_ACTIVITY_LOG_APPEND_ONLY = """
DROP TRIGGER IF EXISTS activity_log_append_only ON activity_log;
DROP FUNCTION IF EXISTS activity_log_append_only();
"""
