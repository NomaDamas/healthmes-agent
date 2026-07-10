"""Tests for healthmes.store.session: engine construction and session lifecycles."""

from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.pool import StaticPool

from healthmes.config import Settings
from healthmes.store import (
    Base,
    SessionDep,
    WeeklyGoal,
    create_db_engine,
    dispose_engine,
    get_engine,
    get_session,
    get_session_factory,
    init_engine,
    session_scope,
)
from healthmes.store import session as session_module

MONDAY = date(2026, 7, 6)


class TestCreateDbEngine:
    def test_sqlite_memory_uses_static_pool_and_shared_connection(self):
        engine = create_db_engine("sqlite+pysqlite:///:memory:")
        try:
            assert isinstance(engine.pool, StaticPool)
            # All checkouts share one connection, so DDL survives across them.
            Base.metadata.create_all(engine)
            with engine.connect() as conn:
                assert conn.exec_driver_sql("SELECT count(*) FROM task").scalar() == 0
        finally:
            engine.dispose()

    def test_sqlite_file_does_not_use_static_pool(self, tmp_path):
        engine = create_db_engine(f"sqlite:///{tmp_path / 'file.db'}")
        try:
            assert not isinstance(engine.pool, StaticPool)
        finally:
            engine.dispose()

    def test_sqlite_file_parent_directory_is_created(self, tmp_path):
        db_path = tmp_path / "nested" / "dir" / "healthmes.db"
        engine = create_db_engine(f"sqlite:///{db_path}")
        try:
            assert db_path.parent.is_dir()
            with engine.connect() as conn:
                conn.exec_driver_sql("SELECT 1")
            assert db_path.exists()
        finally:
            engine.dispose()

    def test_sqlite_foreign_keys_pragma_enabled(self, tmp_path):
        engine = create_db_engine(f"sqlite:///{tmp_path / 'fk.db'}")
        try:
            with engine.connect() as conn:
                assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        finally:
            engine.dispose()

    def test_postgres_url_builds_lazily_without_connecting(self):
        # No postgres is running at this address; engine creation must not connect.
        engine = create_db_engine("postgresql+psycopg://u:p@localhost:59999/nope")
        try:
            assert engine.dialect.name == "postgresql"
            assert engine.pool._pre_ping is True
        finally:
            engine.dispose()

    def test_extra_engine_kwargs_pass_through(self, tmp_path):
        engine = create_db_engine(f"sqlite:///{tmp_path / 'echo.db'}", echo=True)
        try:
            assert engine.echo is True
        finally:
            engine.dispose()


@pytest.fixture
def store_settings(tmp_path) -> Settings:
    """Settings pointing the process-wide engine at a per-test sqlite file."""
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'store.db'}",
        data_dir=tmp_path / "data",
        scheduler_enabled=False,
        _env_file=None,
    )


@pytest.fixture
def initialized_store(store_settings: Settings):
    """Process-wide engine initialised from test settings, disposed afterwards."""
    engine = init_engine(store_settings)
    Base.metadata.create_all(engine)
    yield engine
    dispose_engine()


class TestProcessWideEngine:
    def test_init_engine_binds_singletons(self, initialized_store):
        assert get_engine() is initialized_store
        assert get_session_factory().kw["bind"] is initialized_store

    def test_init_engine_replaces_previous_engine(self, initialized_store, store_settings):
        replacement = init_engine(store_settings)
        try:
            assert replacement is not initialized_store
            assert get_engine() is replacement
        finally:
            dispose_engine()

    def test_dispose_engine_clears_singletons(self, store_settings):
        init_engine(store_settings)
        dispose_engine()
        assert session_module._engine is None
        assert session_module._session_factory is None


class TestGetSession:
    def test_yields_working_session_and_closes(self, initialized_store):
        generator = get_session()
        session = next(generator)
        session.add(WeeklyGoal(week_start=MONDAY, title="via dependency"))
        session.commit()
        generator.close()  # runs the finally: session.close()
        with session_scope() as check:
            titles = check.scalars(select(WeeklyGoal.title)).all()
        assert titles == ["via dependency"]

    def test_rolls_back_on_error(self, initialized_store):
        generator = get_session()
        session = next(generator)
        session.add(WeeklyGoal(week_start=MONDAY, title="never committed"))
        with pytest.raises(RuntimeError):
            generator.throw(RuntimeError("handler blew up"))
        with session_scope() as check:
            assert check.scalars(select(WeeklyGoal.id)).all() == []

    def test_works_as_fastapi_dependency(self, initialized_store):
        app = FastAPI()

        @app.get("/goals")
        def list_goals(session: SessionDep) -> list[str]:
            return list(session.scalars(select(WeeklyGoal.title).order_by(WeeklyGoal.title)))

        @app.post("/goals/{title}")
        def add_goal(title: str, session: SessionDep) -> dict[str, str]:
            session.add(WeeklyGoal(week_start=MONDAY, title=title))
            session.commit()
            return {"title": title}

        with TestClient(app) as client:
            assert client.post("/goals/first").status_code == 200
            assert client.post("/goals/second").status_code == 200
            assert client.get("/goals").json() == ["first", "second"]


class TestSessionScope:
    def test_commits_on_success(self, initialized_store):
        with session_scope() as session:
            session.add(WeeklyGoal(week_start=MONDAY, title="committed"))
        with session_scope() as check:
            assert check.scalars(select(WeeklyGoal.title)).all() == ["committed"]

    def test_rolls_back_and_reraises_on_error(self, initialized_store):
        with pytest.raises(ValueError, match="boom"):
            with session_scope() as session:
                session.add(WeeklyGoal(week_start=MONDAY, title="rolled back"))
                raise ValueError("boom")
        with session_scope() as check:
            assert check.scalars(select(WeeklyGoal.id)).all() == []

    def test_accepts_explicit_factory(self, session_factory):
        # Bound to the per-test in-memory engine, not the process-wide one.
        with session_scope(session_factory) as session:
            session.add(WeeklyGoal(week_start=MONDAY, title="explicit factory"))
        with session_factory() as check:
            assert check.scalars(select(WeeklyGoal.title)).all() == ["explicit factory"]
