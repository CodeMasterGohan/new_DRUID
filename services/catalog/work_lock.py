"""Per-document execution ownership across commits.

Production holds a PostgreSQL session advisory lock on the SAME connection as
catalog writes. It cannot expire while an old worker is still using that DB
session. SQLite's process lock is only a unit-test/development substitute.
"""
from contextlib import contextmanager
import hashlib
import threading
from sqlalchemy import text, event
from .errors import ConflictError

_locks = [threading.RLock() for _ in range(256)]


@contextmanager
def document_work_lock(session, document_id, *, wait=False):
    key = int.from_bytes(hashlib.sha256(str(document_id).encode()).digest()[:8], 'big', signed=True)
    bind = session.get_bind()
    if bind.dialect.name != 'postgresql':
        lock = _locks[key % len(_locks)]
        if not lock.acquire(blocking=wait):
            raise ConflictError('document work is already running')
        try:
            session.expire_all()
            yield
        finally:
            lock.release()
        return
    # Call only at an operation boundary, before any mutations.
    session.rollback()
    with bind.connect() as connection:
        fn = 'pg_advisory_lock' if wait else 'pg_try_advisory_lock'
        acquired = connection.scalar(text(f'SELECT {fn}(:key)'), {'key': key})
        connection.commit()
        if not wait and not acquired:
            raise ConflictError('document work is already running')
        original_driver = connection.connection.driver_connection
        def fence(conn, cursor, statement, parameters, context, executemany):
            if conn.connection.driver_connection is not original_driver:
                raise RuntimeError("work lock connection lost; refusing stale worker writes")
        event.listen(connection, "before_cursor_execute", fence)
        session.bind = connection
        session.expire_all()
        try:
            yield
        finally:
            try:
                session.rollback()
                # Never reconnect after losing the lock-owning connection.
                if not connection.invalidated:
                    connection.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': key})
                    connection.commit()
            except Exception:
                # A failed unlock must not return a locked session to the pool.
                connection.invalidate()
                raise
            finally:
                event.remove(connection, "before_cursor_execute", fence)
                session.bind = bind
