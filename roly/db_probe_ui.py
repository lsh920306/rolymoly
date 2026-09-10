"""Candidate UI: call only inside admin.py's existing authenticated accounts tab.

No route or menu is registered here. The backend independently checks current
admin access on the same read-only lease when the button is pressed.
"""
import json
from time import monotonic

import streamlit as st
from .db_probe import measure_db


def render_db_probe(core, token):
    panel = st.expander("저장소 응답 확인", key="admin_db_probe_panel", on_change="rerun")
    if not panel.open:
        return
    with panel:
        st.caption("기본 조회 3회와 전송 방식 4회를 비교합니다. 1분에 한 번 실행할 수 있습니다.")
        if st.button("DB 응답 비교 실행", key="admin_db_probe_run", disabled=not core.is_postgres):
            st.session_state["admin_db_probe_result"] = {"stored_at": monotonic(), "report": measure_db(core, token)}
        saved = st.session_state.get("admin_db_probe_result")
        if saved and monotonic() - saved["stored_at"] > 600:
            st.session_state.pop("admin_db_probe_result", None)
            saved = None
        if saved:
            report = saved["report"]
            if report["ok"]:
                st.success("DB 응답 비교를 완료했습니다.")
            elif report["status"] in ("rate_limited", "busy", "capacity_limited"):
                st.warning("다른 확인이 진행 중이거나 아직 1분이 지나지 않았습니다.")
            elif report["status"] == "auth_denied":
                st.warning("현재 관리자 로그인을 확인해 주세요.")
            else:
                st.warning("일부 확인을 마치지 못했습니다. 단계별 결과를 확인해 주세요.")
            rows = [{"단계": name, "상태": row["status"], "시간 (ms)": row["elapsed_ms"]}
                    for name, row in report["stages"].items()]
            rows.extend({"단계": f"SELECT 1 · {row['index']}", "상태": row["status"], "시간 (ms)": row["elapsed_ms"]}
                        for row in report["samples"])
            rows.extend({"단계": f"묶음 {case['index']} {case['mode']} · {name}", "상태": case[name]["status"], "시간 (ms)": case[name]["elapsed_ms"]}
                        for case in report["pipeline_controls"] for name in ("queue", "pipeline_exit", "materialization", "rollback"))
            st.dataframe(rows, hide_index=True)
            st.code(json.dumps(report, ensure_ascii=False, indent=2), language="json")
