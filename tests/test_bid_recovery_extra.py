"""Independent recovery boundaries using real local transactions and app widgets."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import sqlite3
from threading import Event, local
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_live_auction_ui as live_fixture


class BidRecoveryBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.fx = live_fixture.LiveAuctionUITests()
        self.addCleanup(self.fx.doCleanups)
        self.fx.setUp()
        self.fx.start()

    def test_resolution_waits_for_inflight_commit_or_rollback(self):
        fx = self.fx
        lot = fx.state()['current_lot']
        connect = fx.core.connect
        role = local()
        for committed in (True, False):
            with self.subTest(committed=committed):
                reached_commit, release_commit, attempted_resolve, resolved = (Event() for _ in range(4))
                request = uuid4().hex

                class HeldConnection:
                    def __init__(self):
                        self.db = connect()
                        self.role = getattr(role, 'name', None)

                    def execute(self, sql, *args):
                        if self.role == 'resolve' and sql == 'BEGIN IMMEDIATE':
                            attempted_resolve.set()
                        return self.db.execute(sql, *args)

                    def commit(self):
                        if self.role == 'bid':
                            reached_commit.set()
                            if not release_commit.wait(10):
                                raise RuntimeError('Synthetic commit gate timed out')
                            if not committed:
                                self.db.rollback()
                                raise sqlite3.OperationalError('Synthetic rollback')
                        return self.db.commit()

                    def __getattr__(self, name):
                        return getattr(self.db, name)

                def bid():
                    role.name = 'bid'
                    try:
                        return fx.live.place_bid(fx.tokens[0], fx.event_id, lot['id'], 10 if committed else 20, request)
                    except sqlite3.OperationalError:
                        return None

                def resolve():
                    role.name = 'resolve'
                    try:
                        return fx.live.resolve_bid(fx.tokens[0], fx.event_id, lot['id'], 10 if committed else 20, request)
                    finally:
                        resolved.set()

                with patch.object(fx.core, 'connect', side_effect=HeldConnection), ThreadPoolExecutor(max_workers=2) as pool:
                    writer = pool.submit(bid)
                    try:
                        self.assertTrue(reached_commit.wait(10))
                        reader = pool.submit(resolve)
                        self.assertTrue(attempted_resolve.wait(10))
                        self.assertFalse(resolved.wait(.1), 'Receipt absence cannot be concluded while the old writer is uncommitted')
                    finally:
                        release_commit.set()
                    receipt, resolution = writer.result(timeout=10), reader.result(timeout=10)
                if committed:
                    self.assertEqual(resolution['id'], receipt['id'])
                else:
                    self.assertIsNone(receipt)
                    self.assertIsNone(resolution)
        self.assertEqual(len(fx.state()['bids']), 1)

    def test_old_receipt_outside_display_limit_resolves_after_sale_without_mutation(self):
        fx = self.fx
        lot = fx.state()['current_lot']
        request = uuid4().hex
        old = fx.live.place_bid(fx.tokens[0], fx.event_id, lot['id'], 1, request)
        for amount in range(2, 302):
            fx.live.place_bid(fx.tokens[0], fx.event_id, lot['id'], amount, uuid4().hex)
        fx.clock_value = fx.state()['current_lot']['closes_at']
        fx.live.settle_due()
        before = deepcopy(fx.state())
        self.assertEqual(len(before['bids']), 300)
        self.assertNotIn(old['id'], [bid['id'] for bid in before['bids']])
        receipt = fx.live.resolve_bid(fx.tokens[0], fx.event_id, lot['id'], 1, request)
        self.assertEqual(receipt['id'], old['id'])
        self.assertTrue(receipt['replayed'])
        self.assertEqual(before['current_lot']['status'], 'SOLD')
        self.assertEqual(fx.state(), before)

    def test_kick_during_pending_clears_identity_before_another_captain_logs_in(self):
        fx = self.fx
        app = fx.app(fx.tokens[0])
        fx.click(app, '+10')
        lot = fx.state()['current_lot']
        client = fx.client(app)
        original_request = client.request_id
        original_context = client.sync()["transport"]["context"]
        original_bid = fx.live.place_bid

        def disconnect_after_commit(*args, **kwargs):
            original_bid(*args, **kwargs)
            raise sqlite3.OperationalError('Synthetic private disconnect')

        with patch.object(fx.live, 'place_bid', side_effect=disconnect_after_commit), patch.object(
            fx.live, 'resolve_bid', side_effect=sqlite3.OperationalError('Synthetic private offline')
        ):
            fx.click(app, '입찰하기')
        self.assertIn(f'live_command_pending_{fx.event_id}', app.session_state)
        fx.core.kick_member(fx.admin, fx.captains[0], 'Synthetic permission revocation')
        with self.assertRaises(PermissionError):
            fx.live.resolve_bid(fx.tokens[0], fx.event_id, lot['id'], 10, original_request)
        before = deepcopy(fx.state())
        app.run()
        fx.healthy(app)
        self.assertIsNone(app.session_state['token'])
        self.assertNotIn(f'live_command_pending_{fx.event_id}', app.session_state)
        self.assertNotIn(f"live_command_ack_{fx.event_id}", app.session_state)
        self.assertTrue(all(stage.get("control") is None for stage in fx.presentation(app, "stage")))
        app.text_input(key='login_username').set_value('captain1')
        app.text_input(key='login_password').set_value('ui-captain-password')
        fx.click(app, '로그인')
        app.switch_page('app_pages/auction.py').run()
        fx.healthy(app)
        self.assertEqual(fx.core.session(app.session_state['token'])['member_id'], fx.captains[1])
        self.assertNotEqual(client.sync()["control"]["context"], original_context)
        self.assertIsNone(client.pending)
        self.assertNotEqual(client.request_id, original_request)
        self.assertEqual(fx.state(), before)
        fx.click(app, '+10')
        fx.click(app, '입찰하기')
        after = fx.state()
        self.assertEqual(after['current_lot']['highest_bid'], 20)
        self.assertEqual(after['current_lot']['highest_team_id'], after['teams'][1]['id'])
        self.assertEqual(len(after['bids']), 2)

    def test_pending_general_database_error_preserves_request_and_recovers(self):
        import psycopg
        from roly.postgres import _database_error

        fx = self.fx
        app = fx.app(fx.tokens[0])
        fx.click(app, '+10')
        lot = fx.state()['current_lot']
        client = fx.client(app)
        request = client.request_id
        with patch.object(fx.live, 'place_bid', side_effect=sqlite3.OperationalError('Synthetic offline')), patch.object(
            fx.live, 'resolve_bid', side_effect=sqlite3.OperationalError('Synthetic offline')
        ):
            fx.click(app, '입찰하기')
        # PG privilege/schema errors map to DatabaseError, outside the narrower
        # OperationalError branch used for transport failures.
        mapped = _database_error(psycopg.errors.InsufficientPrivilege('Synthetic private error'))
        self.assertIs(type(mapped), sqlite3.DatabaseError)
        with patch.object(fx.live, 'resolve_bid', side_effect=mapped):
            client.poll()
        fx.healthy(app)
        self.assertIn(f'live_command_pending_{fx.event_id}', app.session_state)
        self.assertEqual(client.request_id, request)
        self.assertTrue(fx.widget(app, 'button', '입찰하기').disabled)
        self.assertTrue(app.error)
        self.assertFalse(any('Synthetic' in error.value for error in app.error))
        client.poll()
        fx.healthy(app)
        self.assertNotIn(f'live_command_pending_{fx.event_id}', app.session_state)
        self.assertNotEqual(client.request_id, request)
        self.assertEqual(fx.state()['bids'], [])
        fx.click(app, '입찰하기')
        self.assertEqual(len(fx.state()['bids']), 1)


if __name__ == '__main__':
    unittest.main()
