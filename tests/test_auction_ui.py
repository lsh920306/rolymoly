"""Structural regressions for the periodically refreshed auction fragment.

AppTest verifies emitted server element paths. It cannot reproduce React's
concurrent delta application; separate multi-browser rehearsal covers that.
"""
import json
import unittest
from unittest.mock import patch

import test_live_auction_ui as live_ui_fixture


class AuctionLayoutTests(unittest.TestCase):
    def setUp(self):
        # Reuse the real 20-player, four-captain fixture without inheriting and
        # accidentally collecting its complete test suite a second time.
        self.fixture = live_ui_fixture.LiveAuctionUITests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def control_paths(self, app):
        found = {}

        def walk(node, path=()):
            node_type = getattr(node, "type", None)
            if node_type == "bidi_component":
                kind = json.loads(node.proto.json).get("kind")
                if kind in ("team", "stage", "history"):
                    found[kind] = path
            elif node_type == "expander" and node.label == "진행 기록":
                found["events"] = path
            for index, child in getattr(node, "children", {}).items():
                walk(child, (*path, index))

        walk(app._tree)
        self.assertEqual(set(found), {"team", "stage", "history", "events"})
        return found

    def test_bid_error_does_not_move_fragment_controls(self):
        fixture = self.fixture
        fixture.start()
        captain = fixture.app(fixture.tokens[0])
        too_high = fixture.state()["teams"][0]["remaining"] + 1
        fixture.widget(captain, "number_input", "입찰할 포인트").set_value(too_high).run()
        before = self.control_paths(captain)
        fixture.click(captain, "입찰하기")
        self.assertTrue(any("예산" in error.value for error in captain.error))
        self.assertEqual(fixture.state()["bids"], [])
        self.assertEqual(self.control_paths(captain), before)

        # Repeated polling must retain both the readable rejection and the
        # draft, while keeping the following block hierarchy at the same path.
        for _ in range(2):
            captain.run()
            fixture.healthy(captain)
            self.assertTrue(any("예산" in error.value for error in captain.error))
            self.assertEqual(fixture.widget(captain, "number_input", "입찰할 포인트").value, too_high)
            self.assertEqual(self.control_paths(captain), before)

        fixture.widget(captain, "number_input", "입찰할 포인트").set_value(10).run()
        fixture.click(captain, "입찰하기")
        self.assertFalse(captain.error)
        self.assertEqual(fixture.state()["current_lot"]["highest_bid"], 10)
        self.assertEqual(self.control_paths(captain), before)

        # A server settlement warning is another transient alert ahead of the
        # same blocks; the first accepted bid also removes the empty-log text.
        read_state = fixture.live.get_state
        with patch.object(fixture.live, "get_state", side_effect=lambda event_id, **kwargs: {
            **read_state(event_id, **kwargs), "worker_error": "temporary settlement failure",
        }):
            captain.run()
            fixture.healthy(captain)
            self.assertTrue(any("마감 처리를 다시 시도" in error.value for error in captain.error))
            self.assertEqual(self.control_paths(captain), before)
        captain.run()
        self.assertFalse(captain.error)
        self.assertEqual(self.control_paths(captain), before)


if __name__ == "__main__":
    unittest.main()
