import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import ui  # noqa: E402
from window36.domain.timeutil import local_str  # noqa: E402
from window36.models import Document, ExtractedFact, FactConflict, Reservation, Site, SiteJob, StockItem  # noqa: E402
from window36.services import documents, inventory, planning  # noqa: E402
from window36.services.common import now  # noqa: E402

actor = ui.page(
    "Procurement and Logistics", "Three sites, spares and reservations, supplier evidence, and multimodal route options"
)
if not ui.require_login(actor):
    st.stop()

with ui.read() as s:
    t = now(s)
    sites = s.scalars(select(Site).order_by(Site.site_role, Site.id)).all()
    items = s.scalars(select(StockItem).order_by(StockItem.id)).all()
    res = s.scalars(select(Reservation).order_by(Reservation.id.desc())).all()
    jobs = {j.stock_item_id: j for j in s.scalars(select(SiteJob))}
    docs = s.scalars(select(Document).order_by(Document.id)).all()
    facts = {
        d.id: s.scalars(select(ExtractedFact).where(ExtractedFact.document_id == d.id).order_by(ExtractedFact.id)).all()
        for d in docs
    }
    conflicts = s.scalars(select(FactConflict).order_by(FactConflict.id)).all()
    plans = planning.current_plans(s)

st.subheader("Sites")
st.dataframe(
    pd.DataFrame(
        [
            {
                "Site": x.id,
                "Name": x.name,
                "Country": x.country,
                "Role": x.site_role,
                "Local time now": local_str(t, x.timezone),
                "Receiving hours (local)": f"{x.receiving_open}–{x.receiving_close}",
            }
            for x in sites
        ]
    ),
    hide_index=True,
    use_container_width=True,
)

st.subheader("Stock and reservations")
active = {r.stock_item_id: r for r in res if r.status == "active"}
st.dataframe(
    pd.DataFrame(
        [
            {
                "Spare": i.id,
                "Site": i.site_id,
                "Part": i.part_number,
                "Rev": i.revision,
                "Revision status": i.revision_status,
                "Status": i.status,
                "Serial": i.serial,
                "Claimed by": (f"{active[i.id].holder_type}: {active[i.id].holder_ref}" if i.id in active else "—"),
                "Donor job": f"{jobs[i.id].id} ({ui.when(jobs[i.id].planned_start, ui.SITE_TZ[i.site_id])})"
                if i.id in jobs
                else "",
            }
            for i in items
        ]
    ),
    hide_index=True,
    use_container_width=True,
)
with st.expander("Reservation history (one active claim per spare is enforced by the database)"):
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Id": r.id,
                    "Spare": r.stock_item_id,
                    "Holder": f"{r.holder_type}: {r.holder_ref}",
                    "Status": r.status,
                    "Reason": r.reason,
                    "By": r.created_by,
                    "Created": ui.when(r.created_at),
                    "Released": ui.when(r.released_at),
                }
                for r in res
            ]
        ),
        hide_index=True,
        use_container_width=True,
    )

st.subheader("Supplier evidence")
st.caption(
    "Uploaded documents are untrusted. A deterministic fixture parser (no AI model) extracts fields with "
    "line provenance; nothing changes operational state until a planner reviews it."
)
open_conflicts = [c for c in conflicts if c.status == "open"]
for c in open_conflicts:
    vals = "; ".join(f"{k}: **{v}**" for k, v in c.values.items())
    st.error(f"Conflict on **{c.field}** for delivery {c.delivery_ref} — {vals}")
for c in conflicts:
    if c.status == "resolved":
        st.success(f"Resolved conflict on {c.field} for {c.delivery_ref}: {c.resolution}")

for d in docs:
    badge = {"staged": "🟡", "accepted": "🟢", "rejected": "⚪", "failed": "🔴"}[d.status]
    with st.expander(
        f"{badge} #{d.id} {d.kind} · {d.source_ref or d.filename} · {d.status}", expanded=d.status == "staged"
    ):
        st.caption(
            f"File `{d.filename}` · {d.size_bytes} bytes · sha256 {d.sha256[:12]}… · extractor: {d.extractor} · "
            f"uploaded by {d.uploaded_by} at {ui.when(d.uploaded_at)}"
        )
        if d.error:
            st.error(d.error)
        if facts[d.id]:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Field": f.field,
                            "Value": f.value,
                            "Provenance": f.provenance,
                            "Valid": "✓" if f.valid else "✗",
                            "Validation": f.validation_error or "",
                        }
                        for f in facts[d.id]
                    ]
                ),
                hide_index=True,
                use_container_width=True,
            )
        if d.reviewed_by:
            st.caption(f"Reviewed by {d.reviewed_by} at {ui.when(d.reviewed_at)}: {d.review_note}")
        if d.status == "staged":
            if ui.can(actor, "document.review", "FR-VAL"):
                note = st.text_input(
                    "Review note", key=f"note_{d.id}", placeholder="e.g. Compared with goods-in paperwork"
                )
                a, b = st.columns(2)
                if a.button("Accept facts", key=f"accept_{d.id}", type="primary"):
                    ui.run_action(f"Review document #{d.id}", documents.review_document, d.id, True, note)
                    st.rerun()
                if b.button("Reject document", key=f"reject_{d.id}"):
                    ui.run_action(f"Review document #{d.id}", documents.review_document, d.id, False, note)
                    st.rerun()
            else:
                st.caption("Review requires the planner role at Valmont.")

c1, c2 = st.columns(2)
with c1:
    st.markdown("**Import a supplier document** (.txt fixture format, ≤ 64 KB)")
    if ui.can(actor, "document.import"):
        up = st.file_uploader("Document", type=["txt"], key="upload")
        if up is not None and st.button("Stage for review", key="stage"):
            ui.run_action("Import document", documents.import_document, up.name, up.getvalue())
            st.rerun()
    else:
        st.caption("Import requires the planner or site operator role.")
with c2:
    st.markdown("**Receiving inspection** (goods-in evidence)")
    inbound = [i.id for i in items if i.status in ("inbound", "quarantine")]
    if ui.can(actor, "receiving.record") and inbound:
        with st.form("receiving"):
            item = st.selectbox("Spare", inbound)
            rev = st.text_input("Observed revision", max_chars=1)
            ref = st.text_input("Evidence reference", placeholder="RCV-VAL-0220-02")
            if st.form_submit_button("Record inspection"):
                ui.run_action("Receiving inspection", inventory.record_receiving_inspection, item, rev, ref)
                st.rerun()
    else:
        st.caption("No inbound items, or the site operator role is required.")

st.subheader("Multimodal routes to Valmont")
st.caption(
    "Earliest dispatch is one hour after authorization. Each leg departs at the later of shipment-ready time "
    "and its scheduled departure plus delay; a leg with a cutoff needs the shipment ready by the cutoff."
)
for p in plans:
    rd = p.route_detail
    if not rd:
        continue
    ok = p.feasible
    icon = "🟢" if ok else "🔴"
    st.markdown(f"#### {icon} {rd['name']} · {rd['mode']} · €{p.cost_breakdown.get('Transport', 0):,.0f}")
    st.caption(f"{rd['carrier']} · plan {p.plan_key} r{p.revision} · status {p.status}")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Leg": leg["seq"],
                    "Mode": leg["mode"],
                    "From": leg["from"],
                    "To": leg["to"],
                    "Ready": ui.when(ui.iso(leg["ready"]), leg["from_tz"]),
                    "Departs": ui.when(ui.iso(leg["departs"]), leg["from_tz"]),
                    "Arrives": ui.when(ui.iso(leg["arrives"]), leg["to_tz"]),
                    "Cutoff": ui.when(ui.iso(leg["cutoff"]), leg["from_tz"]) if leg["cutoff"] else "",
                    "Delay h": leg["delay_h"] or "",
                    "Leg status": leg["status"],
                    "Reason": leg["reason"],
                }
                for leg in rd["legs"]
            ]
        ),
        hide_index=True,
        use_container_width=True,
    )
    if ok:
        st.success(
            f"Part available at Valmont {ui.when(p.part_available_at)} · expected finish "
            f"{ui.when(p.expected_finish)} · slack {p.slack_h:.1f} h"
        )
    else:
        for r in p.reasons:
            st.error(r)
    if p.donor_impact:
        st.info("Donor-site impact: " + p.donor_impact["summary"])

ui.footer()
