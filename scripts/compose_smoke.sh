#!/usr/bin/env bash
# Compose smoke test: clean start, migrations + seed, health, least-privilege role,
# idempotent restart and persistence across a full stop/start. Leaves nothing behind.
set -euo pipefail

PORT="${W36_UI_PORT:-8501}"
export W36_UI_PORT="$PORT"
cleanup() { docker compose down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT

wait_healthy() {
  for _ in $(seq 1 60); do
    status=$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q app)" 2>/dev/null || echo none)
    [ "$status" = "healthy" ] && return 0
    sleep 3
  done
  docker compose logs --tail 80
  echo "app did not become healthy" >&2
  return 1
}

echo "== clean start"
docker compose down -v --remove-orphans >/dev/null 2>&1 || true
docker compose up -d --build
wait_healthy
test "$(docker inspect -f '{{.State.ExitCode}}' "$(docker compose ps -aq migrate)")" = "0"

echo "== UI health endpoint"
curl -fsS "http://127.0.0.1:${PORT}/_stcore/health" | grep -q ok

echo "== application + database smoke (runs as the least-privilege app role)"
docker compose exec -T app python -m window36.cli smoke

echo "== app role cannot run DDL"
if docker compose exec -T app python -c "
from sqlalchemy import text
from window36.db import get_engine
with get_engine().begin() as c:
    c.execute(text('CREATE TABLE should_fail (id int)'))
" >/dev/null 2>&1; then echo "app role was able to run DDL" >&2; exit 1; fi

echo "== database is not published on the host"
if [ -n "$(docker port "$(docker compose ps -q db)")" ]; then echo "db port is published" >&2; exit 1; fi

echo "== write state, full stop/start, verify persistence and idempotent startup"
docker compose exec -T app python - <<'EOF'
from window36.db import session_scope
from window36.services import scenario
from window36.services.common import Ctx, load_actor
with session_scope() as s:
    scenario.advance_clock(Ctx(s, load_actor(s, "planner_fr")), 1)
EOF
docker compose stop
docker compose up -d
wait_healthy
docker compose exec -T app python - <<'EOF'
from window36.db import session_scope
from window36.services import scenario
from window36.models import AppUser
from sqlalchemy import func, select
with session_scope() as s:
    now = scenario.sim_now(s)
    assert now.isoformat() == "2026-02-20T03:00:00+00:00", now
    assert s.scalar(select(func.count()).select_from(AppUser)) == 8
print("persistence OK", now.isoformat())
EOF
echo "SMOKE PASSED"
