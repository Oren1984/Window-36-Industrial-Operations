"""Shared Streamlit helpers. UI state stays here; business rules live in window36.services."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from window36 import CREDIT  # noqa: E402
from window36.config import get_settings  # noqa: E402
from window36.db import get_engine, session_scope  # noqa: E402
from window36.domain.timeutil import local_str, utc_str  # noqa: E402
from window36.logging_setup import configure_logging, log, new_correlation_id  # noqa: E402
from window36.services import auth  # noqa: E402
from window36.services.common import (  # noqa: E402
    ROLE_LABELS,
    ROLE_PERMISSIONS,
    Actor,
    Ctx,
    DomainError,
    PolicyBlocked,
    load_actor,
    now,
)

logger = logging.getLogger("window36.ui")
MAIN_TZ = "Europe/Paris"
SITE_TZ = {"FR-VAL": "Europe/Paris", "DE-HSF": "Europe/Berlin", "IE-SHM": "Europe/Dublin"}
USER_KEY = "w36_user"


def engine():
    configure_logging(get_settings().log_level)
    return get_engine()


@contextmanager
def read():
    with session_scope(engine()) as s:
        yield s


def current_actor() -> Actor | None:
    username = st.session_state.get(USER_KEY)
    if not username:
        return None
    with read() as s:
        try:
            return load_actor(s, username)  # role re-read from the database on every run
        except DomainError:
            st.session_state.pop(USER_KEY, None)
            return None


def can(actor: Actor | None, permission: str, site_id: str | None = None) -> bool:
    if actor is None or permission not in ROLE_PERMISSIONS.get(actor.role, set()):
        return False
    return not (site_id and actor.site_id and actor.site_id != site_id)


def run_action(label: str, fn: Callable, *args, **kwargs):
    """Execute one service call as one transaction and report the outcome."""
    actor = current_actor()
    if actor is None:
        st.error("Sign in first.")
        return None
    cid = new_correlation_id()
    try:
        with session_scope(engine()) as s:
            result = fn(Ctx(s, load_actor(s, actor.username), cid), *args, **kwargs)
        st.session_state["w36_flash"] = ("success", f"{label}: done")
        log(logger, logging.INFO, "ui action", action=label, user=actor.username, status="ok")
        return result if result is not None else True
    except PolicyBlocked as exc:
        st.session_state["w36_flash"] = ("error", f"{label} blocked — {exc}")
    except DomainError as exc:
        st.session_state["w36_flash"] = ("error", f"{label} not allowed — {exc}")
    except Exception:  # noqa: BLE001 - never leak internals to the page
        logger.exception("ui action failed")
        st.session_state["w36_flash"] = ("error", f"{label} failed unexpectedly (correlation id {cid})")
    return None


def flash() -> None:
    msg = st.session_state.pop("w36_flash", None)
    if msg:
        kind, text = msg
        (st.success if kind == "success" else st.error)(text)


def when(dt: datetime | None, site_tz: str = MAIN_TZ) -> str:
    if dt is None:
        return "—"
    return f"{local_str(dt, site_tz)} ({utc_str(dt).split(' ', 3)[-1]})"


def iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def sidebar() -> Actor | None:
    with st.sidebar:
        st.markdown("**Window 36** · shutdown decision simulator")
        st.caption("SIMULATION — synthetic data, fictional sites. No real orders, bookings or machine control.")
        try:
            with read() as s:
                t = now(s)
            st.metric("Simulated clock (Valmont)", local_str(t, MAIN_TZ, with_tz=False), utc_str(t), delta_color="off")
        except Exception:  # noqa: BLE001
            st.warning("Scenario not available — check database health.")
        actor = current_actor()
        if actor is None:
            with st.form("login", clear_on_submit=True):
                st.markdown("**Sign in** (local demo accounts)")
                username = st.text_input("Username", key="login_user")
                password = st.text_input("Password", type="password", key="login_pass")
                if st.form_submit_button("Sign in"):
                    with read() as s:
                        who = auth.authenticate(s, username, password)
                    if who:
                        st.session_state[USER_KEY] = who.username
                        st.rerun()
                    else:
                        st.error("Invalid username or password")
        else:
            site = f" · {actor.site_id}" if actor.site_id else " · all sites"
            st.success(f"{actor.username}\n\n{ROLE_LABELS[actor.role]}{site}")
            if st.button("Sign out", key="logout"):
                st.session_state.pop(USER_KEY, None)
                st.rerun()
        st.divider()
        st.caption(CREDIT)
    return actor


def page(title: str, intro: str) -> Actor | None:
    actor = sidebar()
    st.title(title)
    st.caption(intro)
    flash()
    return actor


def require_login(actor: Actor | None) -> bool:
    if actor is None:
        st.info("Sign in from the sidebar to view this page. New here? Open the Help Center in the sidebar.")
        return False
    return True


def footer() -> None:
    st.divider()
    st.caption(f"{CREDIT} · Demo data is fictional · Times shown in site-local time with UTC in brackets")
