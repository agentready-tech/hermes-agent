"""Owner-side Linear receipt and native FIFO input share a single commit."""

from dataclasses import dataclass
import uuid

from hermes_state_runtime import (
    RuntimeStoreError, _admit_input_in_transaction, _epoch, _json, admission_fingerprint,
)

_RECEIPT_RETENTION_S = 24 * 60 * 60
_CLEANUP_LIMIT = 128


@dataclass(frozen=True)
class LinearKey:
    subscription_id: str
    delivery_id: str
    body_sha256: str
    stale: bool


def _duplicate(conn, key):
    row = conn.execute('SELECT body_sha256,admission_id FROM linear_main_receipts '
                       'WHERE subscription_id=? AND delivery_id=?',
                       (key.subscription_id, key.delivery_id)).fetchone()
    if row is not None:
        if row[0] != key.body_sha256:
            raise RuntimeStoreError('admission_conflict')
        return row[1]
    if key.stale:
        raise RuntimeStoreError('stale_delivery')
    row = conn.execute('SELECT admission_id FROM linear_main_receipts '
                       'WHERE subscription_id=? AND body_sha256=?',
                       (key.subscription_id, key.body_sha256)).fetchone()
    return row[0] if row is not None else None


def read_linear_receipt(db, key):
    """Read the committed receipt under the same scoped replay rules as the writer."""
    with db._read_ctx() as conn:
        return _duplicate(conn, key)


def _insert_receipt(conn, key, admission_id):
    conn.execute('INSERT INTO linear_main_receipts(subscription_id,delivery_id,body_sha256,admission_id) '
                 'VALUES(?,?,?,?)', (key.subscription_id, key.delivery_id, key.body_sha256, admission_id))
    conn.execute("DELETE FROM linear_main_receipts WHERE rowid IN ("
                 "SELECT rowid FROM linear_main_receipts WHERE terminal_at <= CAST(strftime('%s','now') AS INTEGER)-? "
                 "ORDER BY terminal_at LIMIT ?)", (_RECEIPT_RETENTION_S, _CLEANUP_LIMIT))


def _admit_linear_input(db, *, epoch, key, principal_id, session_id, request_id, payload, authorize):
    """Return the original admission ID and, for a new commit, its native row."""
    encoded = _json(payload)
    digest = admission_fingerprint(canonical_target=session_id, payload={'input': payload, 'intent': 'queue'})

    def write(conn):
        _epoch(conn, epoch)
        duplicate = _duplicate(conn, key)
        if duplicate is not None:
            return duplicate, None
        row = _admit_input_in_transaction(conn, epoch=epoch, principal_id=principal_id, session_id=session_id,
                                         request_id=request_id, encoded=encoded, digest=digest,
                                         intent='queue', authorize=authorize)
        _insert_receipt(conn, key, row['admission_id'])
        return row['admission_id'], row

    return db._execute_write(write)


def _linear_request_id():
    return 'linear:' + uuid.uuid4().hex
