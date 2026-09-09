"""Rehearse personal membership and normal matches in a fresh disposable DB.

--sqlite uses a temporary file; --remote generates and removes a private QA
schema. Reports contain only check names, counts, timings and safe failures.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import secrets
import sys
import tempfile
import threading
import traceback
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from roly.core import Core, ROLES
from roly.competition import Competition
from roly.roster_ui import match_roster_text
from scripts.rehearse_postgres import Report, new_qa_schema, require_qa_schema, require, rejected


def together(*actions):
    barrier = threading.Barrier(len(actions))
    def invoke(action):
        barrier.wait(timeout=30)
        return action()
    with ThreadPoolExecutor(max_workers=len(actions)) as workers:
        return list(workers.map(invoke, actions))


def exercise(core, report):
    """Use actual service transactions; never accept an operational PG schema."""
    if core.is_postgres:
        require_qa_schema(core.schema)
    comp = Competition(core)
    def counts():
        with core.read_snapshot() as db:
            return tuple(db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                         for table in ("accounts", "members", "registration_requests"))
    def scores():
        return {member["id"]: member["score"] for member in core.list_members(include_pending=True)}
    def assignments(count, replacement=False):
        return [{"member_id": members[20] if replacement and index == 5 else members[index],
                 "role": ROLES[index % 5]} for index in range(count)]
    def verify_game(game_id, k, revision=1):
        game = core.get_game(game_id)
        require(len(game["players"]) == 10 and game["revision"] == revision)
        require(len(game["settlements"]) == 10 * revision)
        require(all(abs(player["delta"]) == k for player in game["players"]))
        for player in game["players"]:
            require(sum(row["amount"] for row in game["ledger"] if row["member_id"] == player["member_id"]) == player["delta"])
        return game
    def record(token, event_id, fixture, k):
        before = scores()
        latest = comp.get_event(event_id)
        game_id = comp.record_result(token, event_id, fixture["id"], fixture["team_a"], expected_roster_token=latest["roster_token"])
        game = verify_game(game_id, k)
        after, deltas = scores(), {player["member_id"]: player["delta"] for player in game["players"]}
        require(all(after[mid] - value == deltas.get(mid, 0) for mid, value in before.items()))
        require(comp.record_result(token, event_id, fixture["id"], fixture["team_a"], expected_roster_token=latest["roster_token"]) == game_id)
        require(core.get_game(game_id) == game and scores() == after)
        return game_id
    with report.stage("fresh_database_and_private_admins"):
        require(counts() == (0, 0, 0) and comp.list_events() == [])
        password = secrets.token_urlsafe(30)
        core.setup_admin("qa_admin", password)
        admin = core.login("qa_admin", password)
        password = secrets.token_urlsafe(30)
        core.create_account(admin, "qa_second_admin", password, role="admin")
        second_admin = core.login("qa_second_admin", password)
    with report.stage("personal_signup_reject_resubmit_approve_and_identity"):
        members, accounts, tokens, passwords = [], [], [], []
        for index in range(22):
            password, key = secrets.token_urlsafe(30), str(uuid4())
            name = f"MemberNormal{index}#QA"
            receipt = core.register_member(f"qa_member_{index}", password, name, ROLES[index % 5], ROLES[(index + 1) % 5],
                request_key=key, current_tier="실버 2", current_tier_lp=50)
            token = core.login(f"qa_member_{index}", password)
            require(core.session(token)["registration_status"] == "PENDING")
            if index == 0:
                require(core.register_member("qa_member_0", password, name, "TOP", "JG", request_key=key,
                    current_tier="실버 2", current_tier_lp=50) == receipt)
                core.reject_registration(admin, receipt["member_id"], "Synthetic application correction")
                rejected(lambda: core.approve_member(admin, receipt["member_id"], 100), (ValueError,))
                require(core.resubmit_registration(token, "MemberNormal0#FIXED", "TOP", "JG") == receipt["member_id"])
            if index < 21:
                core.approve_member(admin, receipt["member_id"], 100)
                actor = core.session(token)
                require((actor["id"], actor["member_id"], actor["registration_status"]) == (receipt["account_id"], receipt["member_id"], "APPROVED"))
            members.append(receipt["member_id"])
            accounts.append(receipt["account_id"])
            tokens.append(token)
            passwords.append(password)
        require(counts() == (24, 22, 22))
        report.data["counts"].update(accounts=24, members=22, approved_members=21, pending_members=1)
    with report.stage("pending_and_nonadmin_permissions_leave_no_writes"):
        before = scores()
        for operation in (
            lambda: comp.create_normal(tokens[-1], assignments(10)),
            lambda: core.approve_member(tokens[-1], members[-1], 999),
            lambda: core.list_accounts(tokens[0]),
            lambda: core.adjust_score(tokens[0], members[0], 7, "Not an administrator"),
            lambda: core.set_policy(tokens[0], k=25),
        ):
            rejected(operation, (PermissionError,))
        require(scores() == before and counts() == (24, 22, 22) and comp.list_events() == [])
    with report.stage("kakao_match_duplicate_unapproved_ambiguous_and_invalid_rosters"):
        registry = core.list_members(include_pending=True)
        by_id = {member["id"]: member for member in registry}
        name = by_id[members[0]]["riot_id"]
        text = "\n".join([name, name, by_id[members[-1]]["riot_id"], "Missing#QA", "Nickname without tag"])
        matched = match_roster_text(registry, text)
        require([row["status"] for row in matched["rows"]] == ["MATCHED", "DUPLICATE", "UNAPPROVED", "UNMATCHED", "INVALID"])
        require(matched["member_ids"] == [members[0]])
        # Canonical duplicates cannot be inserted. Exercise the matcher's
        # defensive ambiguity branch using a local projection, never bad DB rows.
        ambiguous = match_roster_text(registry + [{**by_id[members[0]], "id": -1}], name)
        require(ambiguous["member_ids"] == [] and ambiguous["rows"][0]["status"] == "AMBIGUOUS")
        invalid = [assignments(10)[:-1], assignments(10) + assignments(10)[:1],
                   [assignments(10)[0]] * 10,
                   [{**item, "role": "TOP"} for item in assignments(10)],
                   [{**item, "role": "UNKNOWN"} if index == 0 else item for index, item in enumerate(assignments(10))],
                   [{**item, "member_id": members[-1]} if index == 0 else item for index, item in enumerate(assignments(10))]]
        for roster in invalid:
            rejected(lambda roster=roster: comp.create_normal(tokens[0], roster), (ValueError,))
        require(comp.list_events() == [] and counts() == (24, 22, 22))
        report.data["counts"].update(invalid_roster_rejections=len(invalid), ambiguous_projection_rejections=1)
    with report.stage("two_member_hosts_create_ten_twenty_and_replace_before_game"):
        events = []
        for count, host in ((10, tokens[1]), (20, tokens[2])):
            pasted = "\n".join(by_id[members[index]]["riot_id"] for index in range(count))
            resolved = match_roster_text(registry, pasted)
            require(resolved["member_ids"] == members[:count])
            event_id = comp.create_normal(host, assignments(count), title=f"Synthetic {count} person normal", balanced=False)
            before = comp.get_event(event_id)
            require(len(before["teams"]) == count // 5 and len(before["games"]) == (1 if count == 10 else 6))
            rejected(lambda: comp.replace_normal_roster(tokens[3], event_id, assignments(count, True), "Other host", before["roster_token"]), (PermissionError,))
            comp.replace_normal_roster(host, event_id, assignments(count, True), "Synthetic Kakao absence replacement", before["roster_token"], balanced=False)
            after = comp.get_event(event_id)
            require(after["id"] == before["id"] and after["policy_snapshot"] == before["policy_snapshot"])
            require({p["member_id"] for p in after["players"]} == {item["member_id"] for item in assignments(count, True)})
            require({game["id"] for game in before["games"]}.isdisjoint(game["id"] for game in after["games"]))
            require(all({p["role"] for p in team["players"]} == set(ROLES) for team in after["teams"]))
            rejected(lambda: comp.replace_normal_roster(admin, event_id, assignments(count), "Stale draft", before["roster_token"]), (ValueError,))
            require(comp.get_event(event_id) == after)
            events.append(event_id)
        report.data["counts"].update(normal_events=2, member_hosts=2, replaced_rosters=2)
    with report.stage("pinned_policy_actual_plus_minus_ten_and_wrong_result_correction"):
        old_policy = core.policy()["id"]
        core.set_policy(admin, k=25)
        event = comp.get_event(events[0])
        fixture = event["games"][0]
        before = scores()
        rejected(lambda: comp.record_result(tokens[2], events[0], fixture["id"], fixture["team_a"]), (PermissionError,))
        first_game = record(tokens[1], events[0], fixture, 10)
        rejected(lambda: comp.record_result(tokens[1], events[0], fixture["id"], fixture["team_b"], reason="Host cannot revise"), (PermissionError,))
        rejected(lambda: comp.record_result(admin, events[0], fixture["id"], fixture["team_b"]), (ValueError,))
        current = comp.get_event(events[0])
        comp.record_result(admin, events[0], fixture["id"], fixture["team_b"], reason="Synthetic winner correction", expected_roster_token=current["roster_token"])
        corrected = verify_game(first_game, 10, 2)
        require(corrected["policy_id"] == old_policy)
        after, deltas = scores(), {p["member_id"]: p["delta"] for p in corrected["players"]}
        require(all(after[mid] - value == deltas.get(mid, 0) for mid, value in before.items()))
        require(comp.record_result(admin, events[0], fixture["id"], fixture["team_b"]) == first_game)
        require(core.get_game(first_game) == corrected)
        rejected(lambda: comp.replace_normal_roster(tokens[1], events[0], assignments(10), "After first game", current["roster_token"]), (ValueError,))
        comp.finalize_event(tokens[1], events[0])
        report.data["counts"].update(corrected_event_games=1, old_policy_delta=10, new_policy_delta=25)
    with report.stage("twenty_player_league_shared_members_finish_without_awards"):
        event = comp.get_event(events[1])
        for fixture in event["games"]:
            game_id = record(tokens[2], events[1], fixture, 10)
            require(core.get_game(game_id)["policy_id"] == old_policy)
        winner = comp.finalize_event(tokens[2], events[1])
        require(comp.finalize_event(tokens[2], events[1]) == winner)
        require(all(comp.get_event(event_id)["status"] == "COMPLETED" for event_id in events))
        require(all(member["award_units"] == 0 for member in core.list_members()))
        require(len(core.list_games()) == 7)
        report.data["counts"].update(completed_normal_events=2, league_games=6)
    with report.stage("admin_manual_score_reason_preserves_game_ledger"):
        previous, game_before = scores(), core.get_game(first_game)
        request_key, reason = str(uuid4()), "Synthetic separate manual correction"
        rejected(lambda: core.adjust_score(admin, members[0], 7, "", request_key=str(uuid4())), (ValueError,))
        def adjust():
            return core.adjust_score(admin, members[0], 7, reason, request_key=request_key)
        receipts = together(adjust, adjust)
        require(receipts[0] == receipts[1] and adjust() == receipts[0])
        for member_id, amount, changed_reason in ((members[0], 8, reason), (members[1], 7, reason), (members[0], 7, "Changed reason")):
            rejected(lambda member_id=member_id, amount=amount, changed_reason=changed_reason:
                core.adjust_score(admin, member_id, amount, changed_reason, request_key=request_key), (ValueError,))
        rejected(lambda: core.adjust_score(second_admin, members[0], 7, reason, request_key=request_key), (ValueError,))
        with core.read_snapshot() as db:
            rows = list(db.execute("SELECT id,member_id,amount FROM score_ledger WHERE source='MANUAL'"))
            require(len(rows) == 1 and (rows[0]["id"], rows[0]["member_id"], rows[0]["amount"]) == (receipts[0], members[0], 7))
            require(db.execute("SELECT COUNT(*) FROM score_adjustment_requests").fetchone()[0] == 1)
            require(db.execute("SELECT COUNT(*) FROM audit WHERE action='SCORE_ADJUST'").fetchone()[0] == 1)
        after = scores()
        require(after[members[0]] == previous[members[0]] + 7 and core.get_game(first_game) == game_before)
        require(all(after[member_id] == value for member_id, value in previous.items() if member_id != members[0]))
        report.data["counts"].update(manual_adjustment=7, concurrent_adjustment_requests=2, manual_ledger_entries=1,
                                     adjustment_payload_rejections=4)
    with report.stage("profile_rank_edit_identity_and_historical_snapshots"):
        snapshots = [comp.get_event(event_id)["players"] for event_id in events]
        require(all((player["current_tier_snapshot"], player["current_tier_lp_snapshot"]) == ("실버 2", 50)
                    for players in snapshots for player in players))
        member, before = core.get_member(members[0]), scores()
        arguments = dict(clan_tier="골드", current_tier="골드 2", current_tier_lp=30, expected_updated_at=member["updated_at"])
        rejected(lambda: core.update_member(tokens[0], members[0], "RenamedNormal#QA", "TOP", "JG", member["base_score"], "Self edit", **arguments), (PermissionError,))
        core.update_member(admin, members[0], "RenamedNormal#QA", "TOP", "JG", member["base_score"], "Synthetic profile correction", **arguments)
        edited, actor = core.get_member(members[0]), core.session(tokens[0])
        require((edited["clan_tier"], edited["current_tier"], edited["current_tier_lp"]) == ("골드", "골드 2", 30))
        require((actor["id"], actor["member_id"], actor["display_name"]) == (accounts[0], members[0], "RenamedNormal#QA"))
        require(scores() == before and [comp.get_event(event_id)["players"] for event_id in events] == snapshots)
        expected_history = {**game_before, "players": [
            {**player, "current_riot_id": "RenamedNormal#QA" if player["member_id"] == members[0] else player["current_riot_id"]}
            for player in game_before["players"]]}
        require(core.get_game(first_game) == expected_history)
        report.data["counts"]["profile_identity_preserved"] = 1
    with report.stage("two_admin_profile_saves_one_winner_and_rank_clear"):
        member = core.get_member(members[0])
        before = scores()
        def save(token, tier, lp):
            try:
                core.update_member(token, members[0], member["riot_id"], "TOP", "JG", member["base_score"], "Concurrent profile change",
                    clan_tier="골드", current_tier=tier, current_tier_lp=lp, expected_updated_at=member["updated_at"])
                return True
            except ValueError:
                return False
        require(sum(together(lambda: save(admin, "골드 1", 40), lambda: save(second_admin, "플래티넘 4", 50))) == 1)
        latest = core.get_member(members[0])
        require((latest["current_tier"], latest["current_tier_lp"]) in (("골드 1", 40), ("플래티넘 4", 50)))
        rejected(lambda: core.update_member(admin, members[0], latest["riot_id"], "TOP", "JG", latest["base_score"], "Invalid LP",
            current_tier="", current_tier_lp=10, expected_updated_at=latest["updated_at"]), (ValueError,))
        require(core.get_member(members[0]) == latest)
        core.update_member(admin, members[0], latest["riot_id"], "TOP", "JG", latest["base_score"], "Clear optional rank",
            clan_tier="", current_tier="", current_tier_lp=None, expected_updated_at=latest["updated_at"])
        cleared = core.get_member(members[0])
        require((cleared["clan_tier"], cleared["current_tier"], cleared["current_tier_lp"]) == ("", "", None) and scores() == before)
        report.data["counts"]["profile_race_winners"] = 1
    with report.stage("new_event_uses_new_policy_and_current_profile"):
        new_event = comp.create_normal(tokens[1], assignments(10), title="Synthetic new policy", balanced=False)
        event = comp.get_event(new_event)
        current_player = next(p for p in event["players"] if p["member_id"] == members[0])
        require((current_player["riot_id"], current_player["clan_tier_snapshot"], current_player["current_tier_snapshot"], current_player["current_tier_lp_snapshot"])
                == ("RenamedNormal#QA", "", "", None))
        game_id = record(tokens[1], new_event, event["games"][0], 25)
        require(core.get_game(game_id)["policy_id"] != old_policy)
        comp.finalize_event(tokens[1], new_event)
        report.data["counts"].update(normal_events=3, completed_normal_events=3)
    with report.stage("standalone_void_and_new_request_rerecord_preserve_history"):
        before = scores()
        key = str(uuid4())
        game_id = core.record_game(admin, key, members[:5], members[5:10], "A")
        verify_game(game_id, 25)
        core.correct_game(admin, game_id, "B", "Synthetic individual winner correction")
        core.void_game(admin, game_id, "Synthetic individual game did not happen")
        core.void_game(admin, game_id, "Identical void retry")
        voided = core.get_game(game_id)
        require(voided["status"] == "VOID" and voided["revision"] == 3 and scores() == before)
        require(sum(row["amount"] for row in voided["ledger"]) == 0)
        replay_id = core.record_game(admin, key, members[:5], members[5:10], "A")
        require(replay_id == game_id and scores() == before)
        replacement = core.record_game(admin, str(uuid4()), members[:5], members[5:10], "B")
        require(replacement != game_id and core.get_game(game_id) == voided)
        verify_game(replacement, 25)
        report.data["counts"].update(voided_standalone_games=1, rerecorded_standalone_games=1)
    with report.stage("kick_revokes_restore_keeps_identity_and_reset_is_single_use"):
        member, before = core.get_member(members[0]), scores()
        core.kick_member(admin, members[0], "Synthetic membership interruption")
        require(core.session(tokens[0]) is None)
        rejected(lambda: core.login("qa_member_0", passwords[0]), (PermissionError,))
        core.restore_member(admin, members[0], "Synthetic membership return")
        token = core.login("qa_member_0", passwords[0])
        actor = core.session(token)
        require((actor["id"], actor["member_id"], actor["display_name"]) == (accounts[0], members[0], "RenamedNormal#QA"))
        require(core.session(tokens[0]) is None and scores() == before and counts() == (24, 22, 22))
        reset = core.issue_password_reset(admin, accounts[0])
        require(core.session(token) is None)
        password = secrets.token_urlsafe(30)
        core.reset_password(reset["token"], password)
        rejected(lambda: core.reset_password(reset["token"], password), (PermissionError,))
        renewed = core.login("qa_member_0", password)
        require(core.session(renewed)["id"] == accounts[0] and scores() == before)
        core.logout(renewed)
        require(core.session(renewed) is None)
        report.data["counts"].update(restored_member_ids=1, reset_consumptions=1)
    with report.stage("final_shared_ledger_consistency"):
        with core.read_snapshot() as db:
            adjustments = {row["member_id"]: row["amount"] for row in db.execute("SELECT member_id,SUM(amount) AS amount FROM score_ledger GROUP BY member_id")}
            projected = core.list_members(include_pending=True)
            require(all(member["score"] == member["base_score"] + adjustments.get(member["id"], 0) for member in projected))
            require(db.execute("SELECT COUNT(*) FROM award_ledger").fetchone()[0] == 0)
        games = core.list_games()
        require(len(games) == 10 and sum(game["status"] == "VOID" for game in games) == 1)
        require(counts() == (24, 22, 22))
        report.data["counts"].update(total_games=10, confirmed_games=9, normal_awards=0)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sqlite", action="store_true")
    mode.add_argument("--remote", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = Report("postgresql" if args.remote else "sqlite")
    schema, temporary, core = None, None, None
    try:
        with report.stage("initialize"):
            if args.remote:
                from roly.postgres import drop_qa_schema
                candidate = new_qa_schema()
                require_qa_schema(candidate)
                schema, target = candidate, "supabase://" + candidate
            else:
                temporary = tempfile.TemporaryDirectory(prefix="roly-member-normal-")
                target = str(Path(temporary.name) / "synthetic.sqlite3")
            core = Core(target)
            if args.remote:
                require(core.is_postgres and core.schema == schema)
        exercise(core, report)
        report.data["ok"] = True
    except Exception as error:
        report.data.update(ok=False, failed_check=report.current, error_type=type(error).__name__)
        state = getattr(error, "sqlstate", None)
        if isinstance(state, str) and re.fullmatch(r"[A-Z0-9]{5}", state):
            report.data["sqlstate"] = state
        report.data["failure_location"] = [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                                            for frame in traceback.extract_tb(error.__traceback__)[-5:]]
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
