"""Exercise real services in a disposable database, emitting sanitized JSON.

Choose --sqlite for a temporary local rehearsal or --remote for a new private
Supabase QA schema. The remote mode never accepts an existing schema name.
Credentials and driver exception text are never included in the report.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import re
import secrets
import sys
import tempfile
import threading
import time
import traceback
import uuid


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from roly.competition import Competition, ROLES
from roly.core import Core
from roly.live_auction import LiveAuction
from roly.tournament import TournamentService


class Clock:
    """Boundary tests control one clock shared by independent DB connections."""
    def __init__(self):
        self.value = 2_000_000_000.0

    def __call__(self):
        return self.value


class Report:
    def __init__(self, backend):
        self.data = {"backend": backend, "passed": [], "counts": {}, "timings_seconds": {}}
        self.current = "initialize"
        self.started = time.monotonic()

    @contextmanager
    def stage(self, name):
        self.current = name
        started = time.monotonic()
        try:
            yield
        finally:
            self.data["timings_seconds"][name] = round(time.monotonic() - started, 3)
        self.data["passed"].append(name)

    def finish(self):
        self.data["timings_seconds"]["total"] = round(time.monotonic() - self.started, 3)
        return self.data


def require(condition):
    if not condition:
        raise AssertionError("Rehearsal invariant failed")


def rejected(operation, errors=(PermissionError, ValueError)):
    try:
        operation()
    except errors:
        return
    raise AssertionError("Restricted operation unexpectedly succeeded")


def new_qa_schema():
    return "rolymoly_qa_" + uuid.uuid4().hex


def require_qa_schema(schema):
    if not isinstance(schema, str) or not re.fullmatch(r"rolymoly_qa_[a-f0-9]{32}", schema):
        raise ValueError("Rehearsal requires a generated private QA schema")


def exercise(core, report):
    """Run bounded workflows; callers must supply a fresh disposable database."""
    if getattr(core, "is_postgres", False):
        require_qa_schema(core.schema)
    with report.stage("initialize_services"):
        comp = Competition(core)
        service = TournamentService(core, comp)
        clock = Clock()
        live = LiveAuction(core, comp, clock=clock)
        second = LiveAuction(core, comp, clock=clock)
    try:
        if core.is_postgres:
            with report.stage("postgres_private_schema_and_server_clock"):
                with core.read_snapshot() as db:
                    roles = [row[0] for row in db.execute("SELECT rolname FROM pg_roles WHERE rolname IN ('anon','authenticated')")]
                    for role in roles:
                        allowed = db.execute("SELECT has_schema_privilege(CAST(? AS TEXT),CAST(? AS TEXT),'USAGE'),has_table_privilege(CAST(? AS TEXT),CAST(? AS TEXT),'SELECT')",
                                             (role, core.schema, role, core.schema + ".accounts")).fetchone()
                        require(not allowed[0] and not allowed[1])
                    fields = dict(db.execute("SELECT column_name,data_type FROM information_schema.columns WHERE table_schema=? AND table_name='live_lots' AND column_name IN ('opened_at','closes_at','closed_at')", (core.schema,)))
                    require(set(fields.values()) == {"double precision"} and len(fields) == 3)
                    realtime = LiveAuction(core, comp)
                    realtime._clock = lambda: 0.0  # A wrong host clock must not affect PG deadlines.
                    before = float(db.execute("SELECT EXTRACT(EPOCH FROM clock_timestamp())::double precision").fetchone()[0])
                    actual = realtime._now(db)
                    after = float(db.execute("SELECT EXTRACT(EPOCH FROM clock_timestamp())::double precision").fetchone()[0])
                    require(before <= actual <= after and actual > 0)
                report.data["counts"]["api_roles_denied"] = len(roles)
        with report.stage("fresh_database_and_synthetic_accounts"):
            require(not core.has_admin())
            require(core.list_members() == [])
            require(comp.list_events() == [])
            password = secrets.token_urlsafe(30)
            core.setup_admin("qa_admin", password)
            admin = core.login("qa_admin", password)
            members = []
            tokens, account_ids = {}, {}
            for index in range(20):
                password = secrets.token_urlsafe(30)
                username = f"qa_member_{index}"
                registration = core.register_member(
                    username, password, f"SyntheticQA{index}#TEST",
                    ROLES[index % 5], ROLES[(index + 1) % 5], request_key=str(uuid.uuid4())
                )
                member = registration["member_id"]
                members.append(member)
                account_ids[member] = registration["account_id"]
                tokens[member] = core.login(username, password)
                actor = core.session(tokens[member])
                require(actor["id"] == account_ids[member] and actor["member_id"] == member)
                require(actor["role"] == "member" and actor["member_status"] == "PENDING"
                        and actor["registration_status"] == "PENDING")
            require(core.list_members() == [] and len(core.list_members(include_pending=True)) == 20)
            require(len(core.list_accounts(admin)) == 21)
            report.data["counts"].update(members=20, accounts=21, pending_logins=20)

        with report.stage("pending_permissions_and_approval_reuse_personal_accounts"):
            pending = tokens[members[0]]
            pending_roster = [{"member_id": member, "role": ROLES[index % 5]}
                              for index, member in enumerate(members[:10])]
            rejected(lambda: comp.create_normal(pending, pending_roster), (PermissionError,))
            rejected(lambda: service.create(pending, "Pending cannot host", "2030-01-01T20:00:00+09:00",
                                           build_mode="AUCTION", team_count=4), (PermissionError,))
            rejected(lambda: core.approve_member(pending, members[0], 100), (PermissionError,))
            rejected(lambda: core.list_accounts(pending), (PermissionError,))
            require(comp.list_events() == [])
            for member in members:
                core.approve_member(admin, member, 100)
                actor = core.session(tokens[member])
                require(actor["id"] == account_ids[member] and actor["member_id"] == member)
                require(actor["role"] == "member" and actor["member_status"] == "APPROVED"
                        and actor["registration_status"] == "APPROVED")
            require(len(core.list_members()) == 20 and len(core.list_accounts(admin)) == 21)
            captains = members[::5]
            normal_host, auction_host, participant = (tokens[members[index]] for index in (1, 2, 3))
            require(members[1] not in captains and members[2] not in captains)
            report.data["counts"].update(approved_personal_accounts=20, linked_captains=4,
                                          distinct_member_hosts=2, noncaptain_hosts=2)

        with report.stage("normal_game_plus_minus_ten_and_replay"):
            assignments = [{"member_id": member, "role": ROLES[index % 5]}
                           for index, member in enumerate(members[:10])]
            event_id = comp.create_normal(normal_host, assignments, title="Synthetic normal QA")
            event = comp.get_event(event_id)
            require(event["created_by"] == account_ids[members[1]])
            game = event["games"][0]
            winner_members = {p["member_id"] for team in event["teams"]
                              if team["id"] == game["team_a"] for p in team["players"]}
            rejected(lambda: comp.record_result(participant, event_id, game["id"], game["team_a"]),
                     (PermissionError,))
            rejected(lambda: comp.record_result(auction_host, event_id, game["id"], game["team_a"]),
                     (PermissionError,))
            result_id = comp.record_result(normal_host, event_id, game["id"], game["team_a"])
            require(comp.record_result(normal_host, event_id, game["id"], game["team_a"]) == result_id)
            scores = {row["id"]: row["score"] for row in core.list_members()}
            require(all(scores[member] == (110 if member in winner_members else 90)
                        for member in members[:10]))
            require(len(core.list_games()) == 1)

        with report.stage("normal_result_correction_preserves_ledger"):
            rejected(lambda: comp.record_result(
                participant, event_id, game["id"], game["team_b"], reason="Synthetic correction"
            ), (PermissionError,))
            rejected(lambda: comp.record_result(
                normal_host, event_id, game["id"], game["team_b"], reason="Host cannot correct"
            ), (PermissionError,))
            require(comp.record_result(admin, event_id, game["id"], game["team_b"],
                                       reason="Synthetic result correction") == result_id)
            scores = {row["id"]: row["score"] for row in core.list_members()}
            require(all(scores[member] == (90 if member in winner_members else 110)
                        for member in members[:10]))
            recorded = core.get_game(result_id)
            require(recorded["revision"] == 2)
            require(len(recorded["revisions"]) == 2)
            require(len(recorded["settlements"]) == 20)
            require(len(recorded["ledger"]) == 20)
            comp.finalize_event(normal_host, event_id)
            require(sum(row["award_units"] for row in core.list_members()) == 0)
            report.data["counts"].update(normal_games=1, normal_revisions=2)

        with report.stage("auction_preparation_and_captain_permissions"):
            event_id = service.create(auction_host, "Synthetic auction QA", "2030-01-01T20:00:00+09:00",
                                      build_mode="AUCTION", team_count=4, format_name="TOURNAMENT")
            require(comp.get_event(event_id)["created_by"] == account_ids[members[2]])
            rejected(lambda: service.open_recruitment(normal_host, event_id), (PermissionError,))
            service.open_recruitment(auction_host, event_id)
            roster_token = comp.get_event(event_id)["roster_token"]
            service.set_participants(auction_host, event_id, [
                {"member_id": member, "role": ROLES[index % 5]} for index, member in enumerate(members)
            ], expected_roster_token=roster_token)
            service.confirm_participants(auction_host, event_id)
            service.set_captains(auction_host, event_id, captains)
            service.prepare_auction(auction_host, event_id)
            pool_members = [member for member in members if member not in captains]
            configured = live.configure(auction_host, event_id, bid_seconds=10)
            random_queue = [lot["member_id"] for lot in configured["lots"]]
            require(len(random_queue) == 16 and set(random_queue) == set(pool_members))
            configured = live.configure(auction_host, event_id, bid_seconds=10)
            require([lot["member_id"] for lot in configured["lots"]] == random_queue)
            state = live.start(auction_host, event_id)
            first_lot = state["current_lot"]
            for token in (participant, auction_host, admin):
                rejected(lambda token=token: live.place_bid(
                    token, event_id, first_lot["id"], 10, str(uuid.uuid4())
                ), (PermissionError,))
            rejected(lambda: live.pause(tokens[captains[0]], event_id), (PermissionError,))
            require(live.get_state(event_id)["bids"] == [])
            report.data["counts"]["persisted_random_lots"] = len(random_queue)

        with report.stage("concurrent_equal_price_bid_has_one_winner"):
            barrier = threading.Barrier(4)
            requests = [str(uuid.uuid4()) for _ in captains]

            def attempt(index):
                barrier.wait(timeout=20)
                try:
                    receipt = live.place_bid(tokens[captains[index]], event_id,
                                             first_lot["id"], 10, requests[index])
                    return index, receipt
                except ValueError:
                    return index, None

            with ThreadPoolExecutor(max_workers=4) as executor:
                receipts = list(executor.map(attempt, range(4)))
            accepted = [(index, receipt) for index, receipt in receipts if receipt is not None]
            require(len(accepted) == 1)
            winner_index, first_receipt = accepted[0]
            state = live.get_state(event_id)
            require(len(state["bids"]) == 1)
            require(state["current_lot"]["highest_bid"] == 10)
            require(state["current_lot"]["closes_at"] == first_lot["closes_at"])
            report.data["counts"].update(concurrent_bid_attempts=4, concurrent_bid_accepts=1)

        with report.stage("bid_replay_does_not_extend_deadline"):
            clock.value += 3
            replay = live.place_bid(tokens[captains[winner_index]], event_id,
                                   first_lot["id"], 10, requests[winner_index])
            require(replay["replayed"] and replay["id"] == first_receipt["id"])
            require(live.get_state(event_id)["current_lot"]["closes_at"] == first_lot["closes_at"])
            rejected(lambda: live.place_bid(tokens[captains[winner_index]], event_id,
                                           first_lot["id"], 11, requests[winner_index]), (ValueError,))

        with report.stage("five_second_extension_and_selected_duration_cap"):
            capped = live.place_bid(tokens[captains[winner_index]], event_id,
                                    first_lot["id"], 15, str(uuid.uuid4()))
            require(capped["closes_at"] == clock.value + 10)  # Seven seconds becomes ten.
            clock.value = capped["closes_at"] - 2
            extended = live.place_bid(tokens[captains[winner_index]], event_id,
                                      first_lot["id"], 20, str(uuid.uuid4()))
            require(extended["closes_at"] == capped["closes_at"] + 5)
            require(extended["closes_at"] - clock.value == 7)

        with report.stage("exact_deadline_rejects_new_bid"):
            clock.value = extended["closes_at"]
            rejected(lambda: live.place_bid(tokens[captains[(winner_index + 1) % 4]], event_id,
                                           first_lot["id"], 25, str(uuid.uuid4())), (ValueError,))
            require(len(live.get_state(event_id)["bids"]) == 3)

        with report.stage("two_workers_settle_once_and_charge_once"):
            barrier = threading.Barrier(2)

            def tick(worker):
                barrier.wait(timeout=20)
                return worker.settle_due()

            with ThreadPoolExecutor(max_workers=2) as executor:
                settlements = list(executor.map(tick, (live, second)))
            require(sum(event_id in settled for settled in settlements) == 1)
            require(live.settle_due() == [])
            state = live.get_state(event_id)
            require(state["status"] == "WAITING")
            require(state["current_lot"]["status"] == "SOLD")
            require(sum(row["type"] == "SOLD" for row in state["events"]) == 1)
            team = next(team for team in state["teams"] if team["id"] == first_receipt["team_id"])
            require(len(team["players"]) == 2 and team["budget"] - team["remaining"] == 20)
            report.data["counts"]["concurrent_settlements"] = 1

        with report.stage("next_player_waits_three_seconds"):
            closed_at = clock.value
            clock.value = closed_at + 2.99
            require(live.settle_due() == [])
            require(live.get_state(event_id)["current_lot"]["id"] == first_lot["id"])
            clock.value = closed_at + 3
            require(live.settle_due() == [event_id])
            require(live.get_state(event_id)["current_lot"]["id"] != first_lot["id"])

        with report.stage("all_sixteen_players_sold_with_valid_rosters"):
            for _ in range(32):
                state = live.get_state(event_id)
                if state["status"] == "COMPLETED":
                    break
                lot = state["current_lot"]
                if state["status"] == "RUNNING":
                    team = next(team for team in state["teams"]
                                if lot["role"] not in {player["role"] for player in team["players"]})
                    receipt = live.place_bid(tokens[team["captain_id"]], event_id,
                                             lot["id"], 10, str(uuid.uuid4()))
                    clock.value = receipt["closes_at"]
                else:
                    require(state["status"] == "WAITING")
                    clock.value = state["next_at"]
                live.settle_due()
            state = live.get_state(event_id)
            require(state["status"] == "COMPLETED")
            require(len(state["lots"]) == 16 and all(lot["status"] == "SOLD" for lot in state["lots"]))
            require(sum(row["type"] == "SOLD" for row in state["events"]) == 16)
            require(all(len(team["players"]) == 5 and
                        {player["role"] for player in team["players"]} == set(ROLES)
                        for team in state["teams"]))
            require(sum(team["budget"] - team["remaining"] for team in state["teams"]) == 170)
            require(comp.get_event(event_id)["status"] == "BRACKET_SETUP")
            require(comp.get_event(event_id)["games"] == [])
            report.data["counts"].update(auction_sales=16, auction_charged_points=170, complete_teams=4)

        with report.stage("tournament_completion_and_idempotent_awards"):
            scores_before = {row["id"]: row["score"] for row in core.list_members()}
            for team in comp.get_event(event_id)["teams"]:
                service.set_team_roles(auction_host, event_id, team["id"],
                                       {player["member_id"]: player["role"] for player in team["players"]})
            service.build_bracket(auction_host, event_id)
            service.confirm_bracket(auction_host, event_id)
            played = 0
            for _ in range(3):
                ready = [game for game in comp.get_event(event_id)["games"]
                         if game["status"] == "PENDING" and game["team_a"] and game["team_b"]]
                if not ready:
                    break
                for game in ready:
                    comp.record_result(auction_host, event_id, game["id"], game["team_a"])
                    played += 1
            require(played == 3)
            rejected(lambda: comp.finalize_event(normal_host, event_id), (PermissionError,))
            winner = comp.finalize_event(auction_host, event_id)
            require(comp.finalize_event(auction_host, event_id) == winner)
            final = comp.get_event(event_id)
            require(final["status"] == "COMPLETED")
            winner_members = {player["member_id"] for team in final["teams"]
                              if team["id"] == winner for player in team["players"]}
            final_members = core.list_members()
            require(all(row["award_units"] == (1 if row["id"] in winner_members else 0)
                        for row in final_members))
            require({row["id"]: row["score"] for row in final_members} == scores_before)
            require(len(core.list_games()) == 4)
            report.data["counts"].update(auction_games=3, award_recipients=5, award_units=5,
                                         completed_events=2, total_games=4)
    finally:
        live.stop_worker()
        second.stop_worker()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sqlite", action="store_true", help="Use a disposable local SQLite database")
    mode.add_argument("--remote", action="store_true", help="Create, exercise and remove a private Supabase QA schema")
    parser.add_argument("--output", type=Path, help="Also write the sanitized JSON report to this file")
    args = parser.parse_args(argv)
    report = Report("postgresql" if args.remote else "sqlite")
    schema, temporary, core = None, None, None
    try:
        with report.stage("initialize"):
            if args.remote:
                from roly.postgres import drop_qa_schema
                schema = new_qa_schema()
                require_qa_schema(schema)
                target = "supabase://" + schema
            else:
                temporary = tempfile.TemporaryDirectory(prefix="roly-db-rehearsal-")
                target = str(Path(temporary.name) / "synthetic.sqlite3")
            core = Core(target)
            if args.remote:
                require(getattr(core, "is_postgres", False) and core.schema == schema)
        exercise(core, report)
        report.data["ok"] = True
    except Exception as error:
        report.data.update(ok=False, failed_check=report.current, error_type=type(error).__name__)
        state = getattr(error, "sqlstate", None)
        if isinstance(state, str) and re.fullmatch(r"[A-Z0-9]{5}", state):
            report.data["sqlstate"] = state
        report.data["failure_location"] = [
            {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
            for frame in traceback.extract_tb(error.__traceback__)[-5:]
        ]
    finally:
        try:
            with report.stage("cleanup_disposable_database"):
                if core is not None and getattr(core, "_keeper", None) is not None:
                    core._keeper.close()
                if schema is not None:
                    require_qa_schema(schema)
                    drop_qa_schema(schema)
                if temporary is not None:
                    temporary.cleanup()
            report.data["cleaned_up"] = True
        except Exception as error:
            report.data.update(ok=False, cleaned_up=False, cleanup_error_type=type(error).__name__)
    document = json.dumps(report.finish(), ensure_ascii=False, indent=2)
    if args.output:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(document + "\n", encoding="utf-8")
        except OSError:
            print(json.dumps({"ok": False, "failed_check": "write_sanitized_report"}))
            return 2
    print(document)
    return 0 if report.data["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
