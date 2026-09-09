"""Resume-event lifecycle and CCv2 trigger wiring; not a browser timing claim."""
import json
import shutil
import subprocess
import unittest

from roly.auction_sync import JS


HARNESS = r"""
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const source=JSON.parse(readFileSync(0,'utf8')).source;
const render=(await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'))).default;
class Events {
  constructor(){this.listeners=new Map();}
  addEventListener(name,fn){if(!this.listeners.has(name))this.listeners.set(name,new Set());this.listeners.get(name).add(fn);}
  removeEventListener(name,fn){this.listeners.get(name)?.delete(fn);}
  emit(name){for(const fn of [...(this.listeners.get(name)||[])])fn();}
  count(name){return this.listeners.get(name)?.size||0;}
}
let clock=0;
const view=new Events();view.performance={now:()=>clock};
const doc=new Events();doc.defaultView=view;doc.visibilityState='visible';
const parent={ownerDocument:doc,isConnected:true};
const events=[];
const props={parentElement:parent,setTriggerValue:(key,value)=>events.push({key,value})};
let cleanup=render(props);
assert.equal(events.length,0,'Initial mount must not request an extra run');
for(let i=0;i<100;i++)cleanup=render(props);
assert.equal(events.length,0,'Snapshots must not trigger an infinite rerun');
assert.equal(doc.count('visibilitychange'),1);
assert.equal(view.count('focus'),1);assert.equal(view.count('pageshow'),1);
doc.visibilityState='hidden';doc.emit('visibilitychange');view.emit('focus');view.emit('pageshow');
assert.equal(events.length,0,'Hidden pages do not send refresh requests');
clock=1000;doc.visibilityState='visible';doc.emit('visibilitychange');view.emit('focus');view.emit('pageshow');
assert.equal(events.length,1,'Visibility/focus/pageshow burst must be coalesced');
assert.equal(events[0].key,'sync');assert.equal(events[0].value.source,'visibility');
const olderCleanup=cleanup;cleanup=render(props);olderCleanup();
clock=1200;view.emit('focus');assert.equal(events.length,1,'Debounce must survive snapshots');
clock=1500;view.emit('focus');assert.equal(events.length,2);
clock=2000;view.emit('pageshow');assert.equal(events.length,3);
assert.equal(events[2].value.sequence,3,'Each accepted signal has a new payload');
parent.isConnected=false;clock=2500;view.emit('focus');assert.equal(events.length,3);
cleanup();assert.equal(doc.count('visibilitychange'),0);assert.equal(view.count('focus'),0);assert.equal(view.count('pageshow'),0);
parent.isConnected=true;clock=3000;view.emit('focus');assert.equal(events.length,3,'Unmount must release all listeners');
const other={ownerDocument:doc,isConnected:true};
const otherCleanup=render({...props,parentElement:other});
assert.equal(events.length,3,'A new component mount remains quiet');
view.emit('focus');assert.equal(events.length,4,'A fresh instance has independent debounce');otherCleanup();
process.stdout.write('startup, snapshot replacement, hidden state, debounce, focus/pageshow and cleanup passed');
"""


class AuctionSyncTests(unittest.TestCase):
    def test_frontend_startup_visibility_debounce_and_cleanup(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required for this component lifecycle check")
        result = subprocess.run([node, "--input-type=module", "-e", HARNESS],
            input=json.dumps({"source": JS}), text=True, encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_trigger_is_consumed_once_without_an_extra_rerun(self):
        from streamlit.testing.v1 import AppTest
        from streamlit.components.v2.bidi_component.main import _make_trigger_id
        source = '''
import streamlit as st
from roly.auction_sync import render_auction_sync
st.session_state["runs"] = st.session_state.get("runs", 0) + 1
@st.fragment
def fragment():
    result = render_auction_sync(key="resume-test")
    st.session_state["sync_seen"] = bool(result.sync)
fragment()
st.text_input("Draft", value="Keep this text", key="draft")
'''
        for _ in range(2):
            app = AppTest.from_string(source).run()
            self.assertFalse(app.exception, [error.message for error in app.exception])
            self.assertEqual(app.session_state["runs"], 1)
            self.assertFalse(app.session_state["sync_seen"])
            states = app._tree.get_widget_states()
            component = app.get("bidi_component")[0]
            signal = states.widgets.add()
            signal.id = _make_trigger_id(component.proto.id, "events")
            signal.json_trigger_value = json.dumps([{"event": "sync", "value": {"sequence": 1, "source": "focus"}}])
            app._run(states)
            self.assertFalse(app.exception, [error.message for error in app.exception])
            self.assertEqual(app.session_state["runs"], 2, "No callback-triggered full rerun")
            self.assertTrue(app.session_state["sync_seen"])
            self.assertEqual(app.text_input(key="draft").value, "Keep this text")
            app.run()
            self.assertEqual(app.session_state["runs"], 3)
            self.assertFalse(app.session_state["sync_seen"], "Trigger must not stay latched")


if __name__ == "__main__":
    unittest.main()
