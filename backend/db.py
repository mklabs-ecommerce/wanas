"""Engine, session factory, and the transaction helper everything writes through."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from backend.config import settings

_connect_args = {}
if settings.database_url.startswith("sqlite"):
    # check_same_thread is a SQLite driver setting, not a schema dependency:
    # FastAPI runs sync endpoints in a threadpool, so a connection may be
    # touched from a different thread than it was created on.
    _connect_args["check_same_thread"] = False

engine = create_engine(
    settings.database_url,
    connect_args=_connect_args,
    future=True,
    pool_pre_ping=True,
)

if engine.dialect.name == "sqlite":

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - driver glue
        cursor = dbapi_connection.cursor()
        # Foreign keys are off by default in SQLite. Postgres enforces them
        # always, and the local database has to behave the same way or a
        # constraint bug only shows up in production.
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _sqlite_begin_immediate(conn):  # pragma: no cover - driver glue
        # SQLite defaults to a deferred transaction, which takes a write lock
        # only at the first write -- two concurrent order transactions can both
        # read, then one dies with "database is locked" instead of losing the
        # conditional decrement cleanly. BEGIN IMMEDIATE takes the lock up
        # front so the loser waits its turn and then sees the real stock.
        conn.exec_driver_sql("BEGIN IMMEDIATE")


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """One unit of work. Commits on success, rolls back on any exception.

    The order transaction depends on this being the only way writes reach the
    database: stock decremented but no order written is an inventory count
    that is permanently wrong with nothing to show for it.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
