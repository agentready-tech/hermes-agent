"""Adopt the managed home as native Bot Mode's exact-title conversation."""

import logging

from hermes_runtime.sessions import HOME_SESSION_ID, HOME_SESSION_TITLE

logger = logging.getLogger(__name__)


def adopt_home_as_bot_chat(db):
    """Transfer the title atomically; retain every transcript and compression edge."""
    from hermes_state_titles import next_title_in_lineage

    def adopt(conn):
        canonical = conn.execute("SELECT id FROM sessions WHERE title = ?", (HOME_SESSION_TITLE,)).fetchone()
        previous = None
        in_home = canonical is not None and (
            canonical["id"] == HOME_SESSION_ID or db._is_compression_ancestor(
                conn, ancestor_id=HOME_SESSION_ID, descendant_id=canonical["id"]))
        if canonical is not None and not in_home:
            previous = canonical["id"]
            # Explicit migration, rather than a normal rename: the old Bot Chat
            # becomes an ordinary visible conversation with its history intact.
            db._set_lineage_column_in_transaction(conn, "hidden", previous, 0)
            saved_title = next_title_in_lineage(conn, f"Previous Bot Chat ({previous})")
            db._set_session_title_in_transaction(conn, previous, saved_title, source=db.TITLE_SOURCE_USER)
        if not in_home:
            db._set_session_title_in_transaction(conn, HOME_SESSION_ID, HOME_SESSION_TITLE,
                                                 source=db.TITLE_SOURCE_USER)
        # Native Bot Chats are hidden from ordinary session lists and protected
        # from accidental renames. Native lineage selection leaves branches and
        # delegated workers independent; an existing canonical tip stays put.
        db._set_lineage_column_in_transaction(conn, "hidden", HOME_SESSION_ID, 1)
        return previous

    previous = db._execute_write(adopt)
    if previous:
        logger.info("Preserved former Bot Chat %s separately; adopted managed home %s", previous, HOME_SESSION_ID)
