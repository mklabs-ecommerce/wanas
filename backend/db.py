"""Engine, session factory, and the transaction helper everything writes through."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from backend.config import settings

log = logging.getLogger("wanas.db")

#: Environment variables that exist only in a real deployment. Railway injects
#: all three into every service it runs (RAILWAY_PUBLIC_DOMAIN once a domain is
#: attached), and a laptop checkout has none of them -- `railway run` included,
#: deliberately: that shell mirrors a deploy closely enough that refusing there
#: too is the honest reading. Any one of them set means "deployed".
_DEPLOY_MARKERS = (
    "RAILWAY_ENVIRONMENT",
    "RAILWAY_PROJECT_ID",
    "RAILWAY_PUBLIC_DOMAIN",
)


def _deployed() -> bool:
    return any(os.getenv(marker, "").strip() for marker in _DEPLOY_MARKERS)


def normalise_database_url(url: str) -> str:
    """Rewrite bare postgres schemes onto the psycopg 3 dialect this repo ships.

    Railway hands out `postgres://...` and `postgresql://...`; SQLAlchemy maps
    both onto the psycopg2 dialect, which requirements.txt deliberately does
    not install -- without this rewrite either spelling crash-loops the deploy
    on ImportError before a single request. Already-dialect-ed URLs and every
    non-postgres scheme pass through untouched. Only the scheme changes: the
    credentials inside the URL are not moved, logged, or rewritten.
    """
    scheme, sep, rest = url.partition("://")
    if sep and scheme in ("postgres", "postgresql"):
        return f"postgresql+psycopg://{rest}"
    return url


def resolve_database_url(raw_url: str) -> str:
    """Normalise the URL, then refuse the one combination that eats data.

    A deployed container's filesystem is ephemeral: SQLite there means every
    session, client, order and queue row is wiped on the next redeploy while
    the startup seed refills catalog and fees, so the shop looks alive with
    nobody's history in it -- that is Bug 2 exactly. So sqlite + deployed
    refuses, unless ALLOW_SQLITE_IN_DEPLOY=1 says someone genuinely means it.
    Local development (no deploy markers at all) boots on SQLite as before,
    unchanged.
    """
    url = normalise_database_url(raw_url)
    if not url.startswith("sqlite") or not _deployed():
        return url
    if os.getenv("ALLOW_SQLITE_IN_DEPLOY", "").strip().lower() in {"1", "true", "yes", "on"}:
        log.warning(
            "ALLOW_SQLITE_IN_DEPLOY=1: starting on SQLite inside a deployment; "
            "everything written here is wiped on the next redeploy"
        )
        return url
    raise RuntimeError(
        "Refusing to start: DATABASE_URL resolves to SQLite while running in a "
        "deployed environment, where the container filesystem is ephemeral -- chat "
        "sessions, clients and orders would be silently wiped on every redeploy. "
        "Set DATABASE_URL to PostgreSQL "
        "(postgresql+psycopg://user:password@host:5432/db). If you really do mean "
        "to run SQLite here, set ALLOW_SQLITE_IN_DEPLOY=1."
    )


_resolved_database_url = resolve_database_url(settings.database_url)

_connect_args = {}
if _resolved_database_url.startswith("sqlite"):
    # Once per process, here at engine creation -- the warning that was missing
    # while deploys quietly ran on ./wanas.db. The scheme is what gets named,
    # never the URL itself: it can carry credentials.
    log.warning(
        "Database is SQLite: data written here is NOT durable across redeploys "
        "or container restarts. Production must point DATABASE_URL at PostgreSQL."
    )
    # check_same_thread is a SQLite driver setting, not a schema dependency:
    # FastAPI runs sync endpoints in a threadpool, so a connection may be
    # touched from a different thread than it was created on.
    _connect_args["check_same_thread"] = False

engine = create_engine(
    _resolved_database_url,
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
