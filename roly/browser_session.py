"""Restore a revocable login after a same-tab reload; never store passwords.

The token is confined to sessionStorage, not a URL or persistent localStorage.
The server validates it on every action. This is not browser-close remember-me.
"""
from hashlib import sha256
from uuid import uuid4

import streamlit as st

JS = """
export default function({parentElement, data, setStateValue}) {
  const signature = JSON.stringify(data);
  if (parentElement.__loginSignature === signature) return;
  parentElement.__loginSignature = signature;
  let token = null, unavailable = false;
  try {
    const storage = parentElement.ownerDocument.defaultView.sessionStorage;
    if (data.operation === 'save') storage.setItem(data.storageKey, data.token);
    else if (data.operation === 'clear') storage.removeItem(data.storageKey);
    else token = storage.getItem(data.storageKey);
  } catch (_) { unavailable = true; }
  if (data.operation === 'load') {
    setStateValue('loaded', {checked:true, token, unavailable, nonce:data.nonce});
  }
}
"""


@st.cache_resource(scope="session", show_spinner=False)
def _component(scope):
    return st.components.v2.component("roly_tab_login", html="<span hidden></span>", js=JS)


def clear_login():
    st.session_state.token = None
    st.session_state.auth_storage_checked = True
    st.session_state.pop("admin_reset_receipt", None)
    st.session_state.pop("admin_reset_checked", None)
    st.session_state.pop("admin_reset_account", None)
    st.session_state.pop("sale_dialog_event", None)
    st.session_state.pop("normal_creation_draft", None)
    st.session_state.pop("t_creation_draft", None)
    st.session_state.pop("t_preparation_dialog", None)
    st.session_state.pop("t_auction_settings_review", None)
    st.session_state.pop("_clan_profile_editor", None)
    for key in list(st.session_state):
        if key.startswith(("live_amount_", "live_request_", "live_pending_", "live_notice_", "sale_preview", "member_edit_context_", "registration_edit_", "admin_adjust_request_", "admin_membership_context_", "t_confirm_roster_")):
            st.session_state.pop(key, None)


def sync_login(core):
    st.session_state.setdefault("auth_storage_nonce", uuid4().hex)
    token = st.session_state.get("token")
    operation = "save" if token else "clear" if st.session_state.get("auth_storage_checked") else "load"
    result = _component(st.session_state.auth_storage_nonce)(key="tab_login", height="content", width="stretch", data={
        "storageKey": "roly-login-" + sha256(core.db_path.encode()).hexdigest()[:24],
        "operation": operation, "token": token, "nonce": st.session_state.auth_storage_nonce,
    }, on_loaded_change=lambda: None)
    loaded = result.loaded
    if operation == "load" and isinstance(loaded, dict) and loaded.get("checked") and loaded.get("nonce") == st.session_state.auth_storage_nonce:
        candidate = loaded.get("token")
        if isinstance(candidate, str) and 20 <= len(candidate) <= 256 and core.session(candidate):
            st.session_state.token = candidate
        st.session_state.auth_storage_checked = True
        st.session_state.auth_storage_unavailable = bool(loaded.get("unavailable"))
        st.rerun()
