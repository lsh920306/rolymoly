"""Card payload, trigger boundary and JavaScript DOM behavior (no visual claim)."""
import json
import shutil
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from roly import member_cards


def member(member_id=1, **changes):
    row = {"id": member_id, "riot_id": "카드회원#KR1", "main_role": "TOP", "score": 1234,
           "wins": 3, "losses": 2, "cats": 3, "stars": 1, "medals": 0, "trophies": 0,
           "notes": "PRIVATE-NOTE", "canonical_id": "PRIVATE-IDENTITY"}
    row.update(changes)
    return row


DOM_HARNESS = r"""
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const input = JSON.parse(readFileSync(0, 'utf8'));
const render = (await import('data:text/javascript;base64,' + Buffer.from(input.source).toString('base64'))).default;
const createdTags = [];
class Element {
  constructor(tag, doc) {
    this.tagName = tag;
    this.ownerDocument = doc;
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.scrollTop = 0;
    this.textContent = '';
  }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this.children = children; }
  setAttribute(key, value) { this.attributes[key] = value; }
  focus() { root.activeElement = this; }
  set innerHTML(_) { throw new Error('Untrusted HTML rendering is forbidden'); }
}
const doc = { createElement(tag) { createdTags.push(tag); return new Element(tag, doc); } };
const list = new Element('ul', doc);
const root = { activeElement: null, querySelector(selector) {
  assert.equal(selector, '.member-list');
  return list;
} };
const events = [];
const props = {parentElement: root, data: {members: input.members}, setTriggerValue: (...args) => events.push(args)};
const cleanup = render(props);
assert.equal(list.children.length, input.members.length);
const first = list.children[0].children[0];
const second = list.children[1].children[0];
assert.equal(first.tagName, 'button');
assert.equal(first.type, 'button');
assert.equal(first.title, input.members[0].riot_id);
assert.ok(first.attributes['aria-label'].includes(input.members[0].riot_id));
assert.equal(first.children[0].attributes['aria-hidden'], 'true');
assert.equal(first.children[1].children.length, 3);
assert.equal(first.children[1].children[0].textContent, input.members[0].name);
assert.equal(first.children[1].children[1].textContent, input.members[0].meta);
assert.equal(first.children[1].children[2].textContent, input.members[0].record);
assert.ok(createdTags.every(tag => ['li', 'button', 'span'].includes(tag)));
first.onclick();
assert.deepEqual(events, [['selected', input.members[0].member_id]]);
const last = list.children.at(-1).children[0];
last.onclick();
assert.deepEqual(events.at(-1), ['selected', input.members.at(-1).member_id]);
list.scrollTop = 150;
root.activeElement = second;
cleanup();
assert.equal(first.onclick, null);
const updated = {...props, data: {members: input.members.map(item => ({...item, meta: '갱신된 전력'}))}};
const cleanupNext = render(updated);
assert.equal(list.children.length, input.members.length);
assert.equal(list.scrollTop, 150);
assert.equal(root.activeElement.dataset.memberId, String(input.members[1].member_id));
assert.equal(list.children[0].children[0].children[1].children[1].textContent, '갱신된 전력');
cleanupNext();
render({...props, data: {members: [{...input.members[0], member_id: 9}]}});
assert.equal(list.scrollTop, 0);
assert.equal(list.children.length, 1);
render({...props, data: {members: []}});
assert.equal(list.children[0].className, 'member-empty');
assert.equal(list.children[0].textContent, '조건에 맞는 회원이 없습니다.');
console.log('DOM construction, literal text, accessibility, click, cleanup and rerender checks passed');
"""


class MemberCardTests(unittest.TestCase):
    def test_public_payload_uses_actual_scores_counts_and_plain_names(self):
        row = member(riot_id="<img src=x onerror=alert(1)>#KR1", trophies=12)
        payload = member_cards.card_data([row])[0]
        self.assertEqual(payload["name"], "<img src=x onerror=alert(1)>")
        self.assertEqual(payload["meta"], "탑 · 전력 1,234 P")
        self.assertEqual(payload["record"], "일반내전 5판 · 🏆 × 12 ⭐ 🐱🐱🐱")
        self.assertNotIn("PRIVATE", repr(payload))
        empty = member_cards.card_data([member(wins=0, losses=0, cats=0, stars=0)])[0]
        self.assertEqual(empty["record"], "일반내전 0판 · 업적 없음")
        self.assertEqual(len(member_cards.card_data([member(i) for i in range(1, 41)])), 40)
        with self.assertRaises(ValueError):
            member_cards.card_data([member(), member()])

    def test_selection_only_accepts_current_list_member_ids(self):
        for selected, expected in ((1, 1), (2, None), (True, None), ("1", None), (None, None)):
            render = Mock(return_value=SimpleNamespace(selected=selected))
            with patch.object(member_cards, "_member_cards_renderer", return_value=render):
                self.assertEqual(member_cards.render_member_cards([member()], key="cards"), expected)
                data = render.call_args.kwargs["data"]
                self.assertEqual(data["members"][0]["member_id"], 1)
                self.assertNotIn("PRIVATE", repr(data))
                self.assertIn("on_selected_change", render.call_args.kwargs)

    def test_javascript_syntax_and_safe_dom_interaction(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable; browser rendering is not simulated")
        syntax = subprocess.run([node, "--check", "--input-type=module"], input=member_cards.JS, text=True, encoding="utf-8", capture_output=True, timeout=10)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        payload = {"source": member_cards.JS, "members": member_cards.card_data([
            member(1, riot_id="<img src=x onerror=alert(1)>#KR1"), member(2, riot_id="다른회원#KR1"),
            *[member(index, riot_id=f"회원{index}#KR1") for index in range(3, 41)]])}
        dom = subprocess.run([node, "--input-type=module", "-e", DOM_HARNESS], input=json.dumps(payload, ensure_ascii=False), text=True, encoding="utf-8", capture_output=True, timeout=10)
        self.assertEqual(dom.returncode, 0, dom.stderr)

    def test_streamlit_registers_and_mounts_the_inline_component(self):
        from streamlit.testing.v1 import AppTest
        def script():
            import streamlit as st
            from roly.member_cards import render_member_cards
            selected = render_member_cards([{"id": 7, "riot_id": "표시회원#KR1", "main_role": "SUP", "score": 100, "wins": 1, "losses": 2}], key="member_cards_mount")
            st.write("선택 없음" if selected is None else str(selected))
        # Module import happened in bare mode above. Each independent AppTest
        # has its own registry; both first mount and reruns must work.
        for _ in range(2):
            app = AppTest.from_function(script, default_timeout=10).run()
            for _ in range(2):
                self.assertFalse(app.exception, [error.message for error in app.exception])
                self.assertTrue(any(item.value == "선택 없음" for item in app.markdown))
                app.run()


if __name__ == "__main__":
    unittest.main()
