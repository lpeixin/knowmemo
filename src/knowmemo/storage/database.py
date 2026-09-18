"""Engine and session management for KnowMemo's metadata database."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from knowmemo.config.settings import Settings
from knowmemo.storage.models import Base


def _enable_sqlite_pragmas(engine: Engine) -> None:
    """Apply the pragmas SQLite needs to behave under concurrent readers.

    ``foreign_keys`` is off by default in SQLite and the schema relies on
    ``ON DELETE CASCADE`` for the segment/knowledge mapping.
    """

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection: object, _record: object) -> None:
        if not isinstance(dbapi_connection, sqlite3.Connection):
            return
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


def create_db_engine(settings: Settings, *, echo: bool = False) -> Engine:
    """Build an engine from settings, creating the SQLite parent directory."""
    url = settings.storage.database_url
    sqlite_path = settings.storage.sqlite_path
    if sqlite_path is not None:
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(url, echo=echo, future=True)
    if url.startswith("sqlite"):
        _enable_sqlite_pragmas(engine)
    return engine


def init_db(engine: Engine) -> None:
    """Create any missing tables. Safe to call on every start."""
    Base.metadata.create_all(engine)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on any exception."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def data_root(settings: Settings) -> Path:
    """Return the root of the local data tree (§7).

    Derived from ``storage.database_url`` when it is SQLite, so a user who
    relocates the database relocates the whole data tree with it.
    """
    sqlite_path = settings.storage.sqlite_path
    if sqlite_path is not None:
        return sqlite_path.parent
    return Path("data")


def ensure_data_dirs(settings: Settings) -> dict[str, Path]:
    """Create ``raw/``, ``normalized/``, ``knowledge/`` and ``logs/`` (§7)."""
    root = data_root(settings)
    created: dict[str, Path] = {}
    for name in ("raw", "normalized", "knowledge", "logs"):
        directory = root / name
        directory.mkdir(parents=True, exist_ok=True)
        created[name] = directory
    return created
