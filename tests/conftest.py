"""Test fixtures.

Unit tests need no database. Integration and UI tests need W36_TEST_DATABASE_URL pointing at a
disposable PostgreSQL database (its public schema is dropped and recreated).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

TEST_URL = os.environ.get("W36_TEST_DATABASE_URL")
os.environ["W36_DATABASE_URL"] = TEST_URL or "postgresql+psycopg://unused:unused@127.0.0.1:1/unused"
os.environ.setdefault("W36_DEMO_PASSWORD", "test-demo-password")

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def engine():
    if not TEST_URL:
        if os.environ.get("CI"):
            pytest.fail("W36_TEST_DATABASE_URL must be set in CI")
        pytest.skip("W36_TEST_DATABASE_URL not set")
    from alembic import command
    from alembic.config import Config

    from window36.db import session_scope
    from window36.services import scenario

    eng = create_engine(TEST_URL)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    with eng.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    with session_scope(eng) as s:
        scenario.seed_reference(s)
        scenario.seed_users(s)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    """Fresh seeded scenario for each test (users and sites are kept)."""
    from window36.db import session_scope
    from window36.services import scenario

    tables = ", ".join(m.__tablename__ for m in scenario.SCENARIO_TABLES) + ", audit_event"
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    with session_scope(engine) as s:
        scenario.seed_scenario(s)
    return engine


@contextmanager
def as_user(engine, username: str):
    from window36.db import session_scope
    from window36.services.common import Ctx, load_actor

    with session_scope(engine) as s:
        yield Ctx(s, load_actor(s, username))


@pytest.fixture
def act(db):
    """act('planner_fr', fn, *args) runs fn(ctx, *args) in its own transaction."""

    def run(username, fn, *args, **kwargs):
        with as_user(db, username) as ctx:
            return fn(ctx, *args, **kwargs)

    return run
