"""Engine and session management for the healthmes database.

Built from ``Settings.database_url`` and working on both run targets:

- **sqlite** (zero-setup mac-native dev / unit tests): ``check_same_thread``
  disabled so FastAPI worker threads can share connections; ``StaticPool``
  only for ``:memory:`` URLs (one shared connection, otherwise every checkout
  would see a fresh empty database); ``PRAGMA foreign_keys=ON`` so FK
  constraints (CASCADE/SET NULL) actually apply; parent directory of file
  databases is created on demand.
- **postgres** (full stack): plain pooled engine with ``pool_pre_ping``.

Session access mirrors ``vendor/open-wearables/backend/app/database.py``:
``get_session`` is the FastAPI dependency (no auto-commit — request handlers
commit explicitly) and ``session_scope`` is the commit-on-success context
manager for background code (APScheduler jobs, CLI tooling).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from healthmes.config import Settings, get_settings

_SQLITE_MEMORY_DATABASES = (None, "", ":memory:")

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def create_db_engine(database_url: str, **engine_kwargs: Any) -> Engine:
    """Create an Engine for ``database_url`` with per-backend safety settings.

    Extra ``engine_kwargs`` are passed through to ``sqlalchemy.create_engine``
    (e.g. ``poolclass=NullPool`` for migration runs).
    """
    url = make_url(database_url)

    if url.get_backend_name() == "sqlite":
        connect_args = dict(engine_kwargs.pop("connect_args", {}))
        connect_args.setdefault("check_same_thread", False)
        engine_kwargs["connect_args"] = connect_args
        if url.database in _SQLITE_MEMORY_DATABASES:
            engine_kwargs.setdefault("poolclass", StaticPool)
        else:
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, **engine_kwargs)

        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine

    engine_kwargs.setdefault("pool_pre_ping", True)
    return create_engine(url, **engine_kwargs)


def init_engine(settings: Settings | None = None) -> Engine:
    """(Re)build the process-wide engine + session factory from settings.

    Intended to be called once from the app lifespan; ``get_engine`` calls it
    lazily from the env-derived settings singleton when nothing did.
    """
    global _engine, _session_factory
    settings = settings if settings is not None else get_settings()
    if _engine is not None:
        _engine.dispose()
    _engine = create_db_engine(settings.database_url)
    _session_factory = sessionmaker(bind=_engine, autocommit=False, autoflush=False)
    return _engine


def dispose_engine() -> None:
    """Dispose the process-wide engine and clear the cached factory (tests/shutdown)."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def get_engine() -> Engine:
    """Return the process-wide engine, lazily initialised from settings."""
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """Return the process-wide session factory, lazily initialised from settings."""
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None
    return _session_factory


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session (no auto-commit).

    Semantics match open-wearables' ``_get_db_dependency``: rollback on error,
    always close; handlers commit explicitly.
    """
    session = get_session_factory()()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def session_scope(
    factory: sessionmaker[Session] | None = None,
) -> Iterator[Session]:
    """Transactional scope for background code: commit on success, rollback on error.

    ``factory`` overrides the process-wide session factory (tests, tooling).
    """
    session = (factory or get_session_factory())()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# FastAPI handler parameter alias, open-wearables ``DbSession`` style.
SessionDep = Annotated[Session, Depends(get_session)]
