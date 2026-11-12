"""Window 36 Streamlit entry point (multipage)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st  # noqa: E402

st.set_page_config(page_title="Window 36", page_icon="🛠️", layout="wide")

VIEWS = Path(__file__).resolve().parent / "views"
nav = st.navigation(
    [
        st.Page(str(VIEWS / "help_center.py"), title="Help Center", icon="❓"),
        st.Page(str(VIEWS / "control_room.py"), title="Shutdown Control Room", icon="🕹️", default=True),
        st.Page(str(VIEWS / "procurement_logistics.py"), title="Procurement and Logistics", icon="🚚"),
        st.Page(str(VIEWS / "plans_qa.py"), title="Plans and QA", icon="✅"),
        st.Page(str(VIEWS / "operations_audit.py"), title="Operations and Audit", icon="📋"),
    ]
)
nav.run()
