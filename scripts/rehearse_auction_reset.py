"""Rehearse auction resets in temporary SQLite or one newly generated QA schema.

Uses synthetic personal accounts only. The shared rehearsal harness owns schema
creation/cleanup; this wrapper also verifies absence and records source hashes.
Riot HTTP is explicitly disabled and database exception text is never reported.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import secrets
import sys
import threading
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from roly.competition import Competition, ROLES
from roly.live_auction import LiveAuction
from roly.tournament import TournamentService
from scripts import rehearse_postgres as harness
from scripts.rehearse_postgres import Clock, require, rejected
from scripts.test_release import manifest


def exercise(core, report):
    if core.is_postgres:
        harness.require_qa_schema(core.schema)
    comp, clock = Competition(core), Clock()
    tournament = TournamentService(core, comp)
    live = LiveAuction(core, comp, clock=clock)
    try:
        with report.stage("private_fresh_database_and_personal_accounts"):
            require(not core.has_admin() and core.list_members(True) == [])
            if core.is_postgres:
                with core.read_snapshot() as db:
                    for role in (row[0] for row in db.execute("SELECT rolname FROM pg_roles WHERE rolname IN ('anon','authenticated')")):
                        access = db.execute("SELECT has_schema_privilege(CAST(? AS TEXT),CAST(? AS TEXT),'USAGE'),has_table_privilege(CAST(? AS TEXT),CAST(? AS TEXT),'SELECT')",
                                            (role, core.schema, role, core.schema + ".accounts")).fetchone()
                        require(not any(access))
            password = secrets.token_urlsafe(24)
            core.setup_admin("reset_admin", password)
            admin = core.login("reset_admin", password)
            ids, tokens = [], {}
            for index in range(20):
                password = secrets.token_urlsafe(24)
                account = core.register_member(f"reset_member_{index}", password, f"ResetSynthetic{index}#QA",
                                               ROLES[index % 5], ROLES[(index + 1) % 5], request_key=str(uuid.uuid4()))
                member_id = account["member_id"]
                ids.append(member_id)
                core.approve_member(admin, member_id, 100)
                tokens[member_id] = core.login(f"reset_member_{index}", password)
            host, captains = tokens[ids[1]], ids[::5]
            require(core.session(host)["role"] == "member")
            report.data["counts"].update(personal_members=20, captains=4)

        with report.stage("normal_history_and_auction_setup"):
            normal = comp.create_normal(host, [{"member_id": member, "role": ROLES[index % 5]}
                                                for index, member in enumerate(ids[:10])])
            game = comp.get_event(normal)["games"][0]
            comp.record_result(host, normal, game["id"], game["team_a"])
            members_before, games_before = core.list_members(True), core.list_games()
            event_id = tournament.create(host, "Synthetic reset QA", "2030-01-01T20:00:00+09:00",
                                         build_mode="AUCTION", team_count=4, format_name="TOURNAMENT")
            tournament.open_recruitment(host, event_id)
            tournament.set_participants(host, event_id, [{"member_id": member, "role": ROLES[index % 5]}
                                                          for index, member in enumerate(ids)],
                                        expected_roster_token=comp.get_event(event_id)["roster_token"])
            tournament.confirm_participants(host, event_id)
            tournament.set_captains(host, event_id, captains)
            tournament.prepare_auction(host, event_id)
            teams = comp.get_event(event_id)["teams"]
            budgets = {team["id"]: index * 100 for index, team in enumerate(teams)}
            live.configure(host, event_id, bid_seconds=25, team_budgets=budgets,
                           order=[member for member in ids if member not in captains])
            live.start(host, event_id)

        def state():
            return live.get_state(event_id)

        def bid(index, amount, request_id=None, lot_id=None):
            return live.place_bid(tokens[captains[index]], event_id,
                                  lot_id if lot_id is not None else state()["current_lot"]["id"],
                                  amount, request_id or str(uuid.uuid4()))

        def close_and_next():
            clock.value = state()["current_lot"]["closes_at"]
            live.settle_due()
            clock.value += 3
            live.settle_due()

        def request():
            preview = live.preview_reset(host, event_id)
            return {"reason": "Synthetic full reset", "request_id": str(uuid.uuid4()),
                    "expected_fingerprint": preview["fingerprint"]}

        def check_ready(receipt):
            after = state()
            require(after["status"] == "READY" and after["event"]["status"] == "AUCTION_READY")
            require(after["current_lot"] is None and after["next_at"] is None and after["bid_seconds"] == 25)
            require(after["queued_count"] == 16 and set(receipt["lot_ids"]) ==
                    {lot["id"] for lot in after["lots"] if lot["status"] == "QUEUED"})
            require(all(team["remaining"] == budgets[team["id"]] and
                        [player["member_id"] for player in team["players"]] == [team["captain_id"]]
                        for team in after["teams"]))
            require(core.list_members(True) == members_before and core.list_games() == games_before)
            require(live.settle_due() == [])

        with report.stage("free_paid_unsold_and_pending_bid_preview"):
            free_request = str(uuid.uuid4())
            free_receipt = bid(0, 0, free_request)
            close_and_next()
            bid(1, 25)
            close_and_next()
            close_and_next()  # One unsold player.
            bid(2, 30)
            preview = live.preview_reset(host, event_id)
            require((preview["sold_count"], preview["refund_total"], preview["bid_count"]) == (2, 25, 3))
            require(live.preview_reset(admin, event_id) == preview)
            rejected(lambda: live.preview_reset(tokens[captains[0]], event_id), (PermissionError,))
            require(state()["status"] == "RUNNING")

        with report.stage("stale_bid_and_pause_resume_preview_rejected"):
            stale = request()
            bid(3, 31)
            rejected(lambda: live.reset(host, event_id, **stale), (ValueError,))
            live.pause(host, event_id)
            stale = request()
            live.resume(host, event_id)
            rejected(lambda: live.reset(host, event_id, **stale), (ValueError,))

        with report.stage("same_uuid_concurrent_reset_refunds_once_and_retains_history"):
            before, arguments, barrier = state(), request(), threading.Barrier(2)
            def reset_once(_):
                barrier.wait(timeout=20)
                return live.reset(host, event_id, **arguments)
            with ThreadPoolExecutor(max_workers=2) as executor:
                receipts = list(executor.map(reset_once, range(2)))
            require(sum(not receipt["replayed"] for receipt in receipts) == 1)
            require(receipts[0]["lot_ids"] == receipts[1]["lot_ids"])
            check_ready(receipts[0])
            after = state()
            require(after["bids"] == before["bids"] and after["events"][1:] == before["events"])
            old_ids = {lot["id"] for lot in before["lots"]}
            require(all(lot["status"] == "CANCELLED" for lot in after["lots"] if lot["id"] in old_ids))
            detail = json.loads(after["events"][0]["detail"])
            require("before" not in detail and detail["result"]["refund_total"] == 25)
            with core.read_snapshot() as db:
                audit = json.loads(db.execute("SELECT detail FROM competition_audit WHERE event_id=? AND action='RESET'", (event_id,)).fetchone()[0])
                require(len(audit["before"]["lots"]) == 16)
            rejected(lambda: live.reset(admin, event_id, **arguments), (ValueError,))
            rejected(lambda: live.reset(host, event_id, **{**arguments, "reason": "Different payload"}), (ValueError,))

        with report.stage("ready_settings_and_old_bid_receipt_do_not_replay_sale"):
            queue = [lot["id"] for lot in state()["lots"] if lot["status"] == "QUEUED"]
            configured = live.configure(host, event_id, bid_seconds=25)
            require(queue == [lot["id"] for lot in configured["lots"] if lot["status"] == "QUEUED"])
            require(bid(0, 0, free_request, free_receipt["lot_id"])["id"] == free_receipt["id"])
            require(len(state()["bids"]) == 4)
            live.start(host, event_id)
            rejected(lambda: bid(0, 0, lot_id=free_receipt["lot_id"]), (ValueError,))
            require(live.reset(host, event_id, **arguments)["replayed"] and state()["status"] == "RUNNING")

        with report.stage("simultaneous_bid_reset_has_one_serialized_outcome"):
            arguments, lot_id, barrier = request(), state()["current_lot"]["id"], threading.Barrier(2)
            def race(kind):
                barrier.wait(timeout=20)
                try:
                    result = live.reset(host, event_id, **arguments) if kind == "reset" else bid(0, 0, lot_id=lot_id)
                    return kind, True, result
                except ValueError:
                    return kind, False, None
            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(race, ("reset", "bid")))
            require(sum(success for _, success, _ in outcomes) == 1)
            if outcomes[0][1]:
                check_ready(outcomes[0][2])
            else:
                require(state()["status"] == "RUNNING" and state()["current_lot"]["highest_bid"] == 0)
                check_ready(live.reset(host, event_id, **request()))
            report.data["counts"]["concurrent_race_winner"] = next(kind for kind, success, _ in outcomes if success)

        with report.stage("audit_failure_rolls_back_then_same_request_succeeds"):
            arguments, before = request(), state()
            with patch.object(comp, "_audit", side_effect=RuntimeError("Synthetic audit failure")):
                rejected(lambda: live.reset(host, event_id, **arguments), (RuntimeError,))
            require(state() == before)
            check_ready(live.reset(host, event_id, **arguments))

        def sell_all():
            live.start(host, event_id)
            for _ in range(40):
                current = state()
                if current["status"] == "COMPLETED":
                    return
                if current["status"] == "RUNNING":
                    lot = current["current_lot"]
                    team = next(team for team in current["teams"] if
                                lot["role"] not in {player["role"] for player in team["players"]})
                    receipt = live.place_bid(tokens[team["captain_id"]], event_id, lot["id"], 0, str(uuid.uuid4()))
                    clock.value = receipt["closes_at"]
                else:
                    require(current["status"] == "WAITING")
                    clock.value = current["next_at"]
                live.settle_due()
            raise AssertionError("Synthetic auction did not complete")

        with report.stage("completed_before_bracket_resets_all_sixteen_sales"):
            sell_all()
            require(live.preview_reset(host, event_id)["sold_count"] == 16)
            check_ready(live.reset(host, event_id, **request()))

        with report.stage("restarted_auction_completes_and_bracket_blocks_reset"):
            sell_all()
            arguments = request()
            tournament.build_bracket(host, event_id)
            rejected(lambda: live.preview_reset(host, event_id), (ValueError,))
            rejected(lambda: live.reset(host, event_id, **arguments), (ValueError,))
            require(core.list_members(True) == members_before and core.list_games() == games_before)
            report.data["counts"].update(completed_reauctions=2, protected_normal_games=1, reset_queue_size=16)
    finally:
        live.stop_worker()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sqlite", action="store_true")
    mode.add_argument("--remote", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    before, generated = manifest(), []
    original = harness.new_qa_schema
    def new_schema():
        schema = original()
        harness.require_qa_schema(schema)
        generated.append(schema)
        return schema
    with patch.object(harness, "new_qa_schema", side_effect=new_schema), \
            patch.object(harness, "exercise", side_effect=exercise), \
            patch("roly.riot_api._http_get", side_effect=AssertionError("Riot HTTP disabled")), \
            redirect_stdout(StringIO()):
        code = harness.main(["--remote" if args.remote else "--sqlite", "--output", str(args.output)])
    report = json.loads(args.output.read_text(encoding="utf-8"))
    absent = not args.remote
    try:
        if args.remote:
            from roly.postgres import connect
            absent = len(generated) == 1
            for schema in generated:
                harness.require_qa_schema(schema)
                with connect(schema) as db:
                    absent = absent and db.execute("SELECT 1 FROM pg_namespace WHERE nspname=?", (schema,)).fetchone() is None
    except Exception as error:
        absent = False
        report["absence_check_error_type"] = type(error).__name__
    after = manifest()
    changed = [key for key in sorted(before.keys() | after.keys()) if before.get(key) != after.get(key)]
    report.update(qa_schema_absence_verified=absent, qa_schemas_checked=len(generated),
                  source_before_sha256=before, source_sha256=after, changed_during_rehearsal=changed,
                  source_unchanged=not changed, operating_user_data_accessed=False, riot_http_disabled=True)
    report["ok"] = bool(code == 0 and report.get("ok") and report.get("cleaned_up") and absent and not changed)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key not in ("source_before_sha256", "source_sha256")},
                     ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
