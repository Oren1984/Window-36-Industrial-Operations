"""Operational entry points: `python -m window36.cli <command>`.

migrate-and-seed  Apply migrations, grant the least-privilege app role, seed users and scenario (idempotent).
smoke             Verify DB connectivity, seeded data and a planning round-trip (read-mostly).
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text

from window36.config import get_settings
from window36.db import get_engine, session_scope
from window36.logging_setup import configure_logging, log, new_correlation_id

logger = logging.getLogger("window36.cli")
ROOT = Path(__file__).resolve().parent.parent


def _wait_for_db(engine, attempts: int = 30) -> None:
    for i in range(attempts):
        try:
            with engine.connect() as conn:
                conn.execute(text("select 1"))
            return
        except Exception:  # noqa: BLE001
            log(logger, logging.INFO, "waiting for database", attempt=i + 1, status="waiting")
            time.sleep(2)
    raise SystemExit("database not reachable")


def migrate_and_seed() -> None:
    settings = get_settings()
    engine = get_engine(settings.database_url)
    _wait_for_db(engine)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    with engine.begin() as conn:
        # Serialize concurrent starters; migrations + seed run once.
        conn.execute(text("select pg_advisory_lock(36036)"))
        try:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
            app_role = os.environ.get("W36_APP_DB_ROLE")
            if app_role:
                if not app_role.replace("_", "").isalnum():
                    raise SystemExit("invalid W36_APP_DB_ROLE")
                conn.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {app_role}"))
                conn.execute(text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {app_role}"))
                conn.execute(text(f"REVOKE INSERT, UPDATE, DELETE ON alembic_version FROM {app_role}"))
        finally:
            conn.execute(text("select pg_advisory_unlock(36036)"))
    from window36.services import scenario

    with session_scope(engine) as s:
        scenario.seed_reference(s)
        created = scenario.seed_users(s)
        seeded = scenario.is_seeded(s)
    if not seeded:
        with session_scope(engine) as s:
            scenario.seed_scenario(s)
    log(
        logger,
        logging.INFO,
        "migrate-and-seed complete",
        users_created=created,
        scenario_seeded=not seeded,
        status="ok",
    )


def smoke() -> int:
    from window36.models import AppUser, Plan, Site, StockItem
    from window36.services import health, planning

    engine = get_engine()
    db = health.db_health(engine)
    checks = {"database": db["ok"]}
    with session_scope(engine) as s:
        checks["sites=3"] = s.scalar(select(func.count()).select_from(Site)) == 3
        checks["users>=8"] = s.scalar(select(func.count()).select_from(AppUser)) >= 8
        checks["stock>=3"] = s.scalar(select(func.count()).select_from(StockItem)) >= 3
        checks["plans>0"] = s.scalar(select(func.count()).select_from(Plan)) > 0
        cands = {c.plan_key: c for c in planning.compute_candidates(s, health.now(s))}
        checks["sea misses window"] = not cands["FULL-IE-FR-SEA"].feasible
        checks["counters"] = isinstance(health.counters(s), dict)
    ok = all(checks.values())
    log(
        logger,
        logging.INFO if ok else logging.ERROR,
        "smoke",
        **{k: str(v) for k, v in checks.items()},
        status="ok" if ok else "failed",
    )
    print("SMOKE", "PASS" if ok else "FAIL", checks)
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    configure_logging(os.environ.get("W36_LOG_LEVEL", "INFO"))
    new_correlation_id()
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "migrate-and-seed":
        migrate_and_seed()
        return 0
    if cmd == "smoke":
        return smoke()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
