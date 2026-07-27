from __future__ import annotations

import os
from collections.abc import Generator, Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


settings = get_settings()

if settings.database_url.startswith("sqlite:///"):
    sqlite_path = settings.database_url.replace("sqlite:///", "", 1)
    if sqlite_path not in (":memory:", ""):
        db_dir = os.path.dirname(os.path.abspath(sqlite_path))
        os.makedirs(db_dir, exist_ok=True)

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Session that commits on clean exit and rolls back on any exception.

    Used by the sync queue so a batch either fully lands or leaves no trace —
    a half-applied batch would strand records in an ambiguous synced state.
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
