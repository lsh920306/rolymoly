"""Ask the active auction fragment for a fresh snapshot when its tab returns.

This component emits a read-refresh signal only. It never submits an auction
action, touches Streamlit's DOM, or changes a server deadline.
"""
from uuid import uuid4

import streamlit as st


HTML = """
<span hidden aria-hidden="true"></span>
"""
CSS = """
:host { display:block; height:0; min-height:0; overflow:hidden; }
"""
JS = """
export default function({parentElement, setTriggerValue}) {
  const doc = parentElement.ownerDocument;
  const view = doc.defaultView;
  if (!view) return;
  const memory = parentElement._rolyAuctionSync ||= {dispose:null, lastSignal:-Infinity, sequence:0};
  // New snapshots can render without an unmount. Replace the previous listeners
  // explicitly so a tab return never accumulates refresh requests.
  memory.dispose?.();
  const signal = (source) => {
    if (!parentElement.isConnected || doc.visibilityState !== 'visible') return;
    const now = view.performance.now();
    if (now - memory.lastSignal < 400) return;
    memory.lastSignal = now;
    setTriggerValue('sync', {sequence:++memory.sequence, source});
  };
  const onVisibility = () => signal('visibility');
  const onFocus = () => signal('focus');
  const onPageShow = () => signal('pageshow');
  doc.addEventListener('visibilitychange', onVisibility);
  view.addEventListener('focus', onFocus);
  view.addEventListener('pageshow', onPageShow);
  const dispose = () => {
    doc.removeEventListener('visibilitychange', onVisibility);
    view.removeEventListener('focus', onFocus);
    view.removeEventListener('pageshow', onPageShow);
    if (memory.dispose === dispose) memory.dispose = null;
  };
  memory.dispose = dispose;
  // Mounting or receiving a snapshot is deliberately not a refresh event.
  return dispose;
}
"""


@st.cache_resource(scope="session", show_spinner=False)
def _register(scope):
    return st.components.v2.component("auction_resume_sync", html=HTML, css=CSS, js=JS, isolate_styles=True)


def _on_sync():
    # The trigger itself reruns the containing fragment. Calling st.rerun here
    # would expand that work to the whole page and can remount the audio controls.
    pass


def render_auction_sync(*, key):
    st.session_state.setdefault("_auction_sync_scope", uuid4().hex)
    return _register(st.session_state["_auction_sync_scope"])(
        data={"kind": "auction_sync"}, key=key, height="content", width="stretch",
        on_sync_change=_on_sync,
    )
