"""Temporary AppTest compatibility for Streamlit's stateful native blocks.

Only fills state omitted by ElementTree.get_widget_states. Existing widget
payloads, explicit saved/replayed requests and normal widget inputs are intact.
Install with a context manager so unrelated tests keep their own semantics.
"""
from contextlib import contextmanager
from unittest.mock import patch

from streamlit.testing.v1.element_tree import ElementTree


def append_native_blocks(tree, wire):
    existing = {item.id for item in wire.widgets}
    for node in tree:
        proto = getattr(node, "proto", None)
        if proto is None:
            continue
        if getattr(node, "type", None) in ("expander", "status"):
            identifier = getattr(proto, "id", "")
            field, value_type = "bool_value", bool
        elif getattr(proto, "HasField", None) and "tab_container" in proto.DESCRIPTOR.fields_by_name and proto.HasField("tab_container"):
            identifier = proto.tab_container.id
            field, value_type = "string_value", str
        else:
            continue
        if not identifier or identifier in existing:
            continue
        try:
            value = tree.session_state[identifier]
        except KeyError:
            continue
        # Stateful layouts only use these native scalar types. Do not invent a
        # value for unmounted blocks or copy private/session objects into wire.
        if type(value) is not value_type:
            continue
        item = wire.widgets.add(id=identifier)
        setattr(item, field, value)
        existing.add(identifier)
    return wire


@contextmanager
def native_blocks():
    original = ElementTree.get_widget_states

    def with_native_blocks(tree):
        return append_native_blocks(tree, original(tree))

    with patch.object(ElementTree, "get_widget_states", with_native_blocks):
        yield


import unittest

from streamlit.testing.v1 import AppTest


APP = '''
import streamlit as st
one,two = st.tabs(["One", "Two"],key="sections",on_change="rerun")
if one.open:
    with one:
        st.caption("First section")
if two.open:
    with two:
        panel=st.expander("Details",key="details",on_change="rerun")
        if panel.open:
            with panel:
                st.selectbox("Choice",[1,2],key="choice")
                if st.button("Apply",key="apply"):
                    st.session_state.applied=st.session_state.get("applied",0)+1
'''


class NativeBlockAdapterTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(native_blocks())
        self.app = AppTest.from_string(APP, default_timeout=10).run()

    def open_details(self):
        self.app.session_state.sections = "Two"
        self.app.run()
        self.app.session_state.details = True
        self.app.run()

    def test_native_state_survives_multiple_normal_widget_runs_and_intentional_close(self):
        self.open_details()
        self.app.selectbox(key="choice").select(2).run()
        self.app.button(key="apply").click().run()
        self.assertEqual(self.app.session_state.applied, 1)
        self.assertEqual(self.app.session_state.sections, "Two")
        self.assertTrue(self.app.session_state.details)
        self.assertEqual(self.app.selectbox(key="choice").value, 2)
        self.app.session_state.details = False
        self.app.run()
        self.assertFalse(self.app.selectbox)
        self.app.session_state.sections = "One"
        self.app.run()
        self.assertFalse(self.app.expander)

    def test_existing_wire_is_never_overwritten_and_saved_request_remains_authoritative(self):
        self.open_details()
        wire = self.app._tree.get_widget_states()
        original_bytes = wire.SerializeToString()
        append_native_blocks(self.app._tree, wire)
        self.assertEqual(wire.SerializeToString(), original_bytes)
        tab = next(item for item in wire.widgets if item.id.endswith("-sections"))
        panel = next(item for item in wire.widgets if item.id.endswith("-details"))
        tab.string_value, panel.bool_value = "One", False
        explicit = wire.SerializeToString()
        append_native_blocks(self.app._tree, wire)
        self.assertEqual(wire.SerializeToString(), explicit)
        self.app._run(wire)
        self.assertEqual(self.app.session_state.sections, "One")
        self.open_details()
        old_request = self.app._tree.get_widget_states()
        self.app.session_state.sections = "One"
        self.app.run()
        self.app._run(old_request)
        self.assertEqual(self.app.session_state.sections, "Two")
        self.assertTrue(self.app.session_state.details)
        self.assertTrue(self.app.selectbox)

    def test_unmounted_blocks_are_not_reintroduced_into_new_requests(self):
        self.open_details()
        old_panel_id = self.app.expander[0].proto.id
        self.app.session_state.sections = "One"
        self.app.run()
        self.assertNotIn(old_panel_id, {item.id for item in self.app._tree.get_widget_states().widgets})


if __name__ == "__main__":
    unittest.main()
