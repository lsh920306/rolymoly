"""Riot integration rehearsal with fake HTTP and a disposable real database.

--remote creates and removes only a generated rolymoly_qa_<32hex> schema.
No real Riot key, real Riot request, operating schema, or browser is used.
"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from hashlib import sha256
import argparse
import json
from pathlib import Path
import re
import secrets
import sys
import tempfile
import threading
import time
import traceback
from urllib.parse import unquote, urlsplit
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from roly.core import Core
from roly.competition import Competition, ROLES
from roly.live_auction import LiveAuction
from roly.member_profile import validate_current_tier
from roly.postgres import SCHEMA_VERSION
from roly.tournament import TournamentService
from roly.riot_api import DataDragonClient, HTTPResponse, RiotAPIError, RiotClient, RiotConfig
from roly.riot_sync import DatabaseRateLimiter, RiotSync, _metadata_transaction
from scripts.rehearse_postgres import Clock, Report, new_qa_schema, require_qa_schema, require, rejected

RIOT_TABLES = ("riot_profiles", "riot_jobs", "riot_rate_hits", "riot_rate_cooldowns")
PRESERVED_TABLES = ("members", "accounts", "sessions", "registration_requests", "policies", "base_history",
    "games", "game_players", "game_revisions", "game_settlements", "score_ledger", "award_batches", "award_ledger",
    "competition_events", "competition_players", "competition_teams", "competition_games", "competition_audit",
    "competition_creation_requests", "live_sessions", "live_lots", "live_bids", "live_events")


class FakeHTTP:
    """Use real endpoint/response parsers, with no outbound HTTP transport."""
    def __init__(self):
        self.calls = Counter()
        self.failures = {}
        self.hook = None
        self.keys = set()

    def __call__(self, url, headers, timeout, max_bytes):
        parsed = urlsplit(url)
        require(parsed.scheme == "https" and timeout == 5 and max_bytes > 0)
        path = parsed.path
        if parsed.hostname == "ddragon.leagueoflegends.com":
            require("X-Riot-Token" not in headers)
            if path == "/api/versions.json":
                stage, identity, body = "dd_versions", "static", ["16.18.1"]
            else:
                require(path == "/cdn/16.18.1/data/ko_KR/champion.json")
                stage, identity = "dd_champions", "static"
                body = {"data": {name: {"key": str(champ_id), "name": korean, "image": {"full": name + ".png"}}
                    for champ_id, name, korean in ((22, "Ashe", "애쉬"), (103, "Ahri", "아리"), (64, "LeeSin", "리 신"),
                                                  (99, "Lux", "럭스"), (86, "Garen", "가렌"))}}
        else:
            require(parsed.hostname in ("asia.api.riotgames.com", "kr.api.riotgames.com"))
            require(headers.get("X-Riot-Token") in self.keys)
            if "/accounts/by-riot-id/" in path:
                stage, identity = "account", unquote(path.rsplit("/", 2)[1])
                body = {"puuid": sha256(identity.encode()).hexdigest(), "gameName": identity, "tagLine": "QA"}
                require(parsed.hostname == "asia.api.riotgames.com")
            elif "/summoners/by-puuid/" in path:
                stage, identity = "summoner", path.rsplit("/", 1)[1]
                body = {"puuid": identity, "profileIconId": 29, "summonerLevel": 155}
            elif "/entries/by-puuid/" in path:
                stage, identity = "rank", path.rsplit("/", 1)[1]
                body = [{"queueType": "RANKED_SOLO_5x5", "tier": "GOLD", "rank": "II", "leaguePoints": 37,
                         "wins": 50, "losses": 40},
                        {"queueType": "RANKED_FLEX_SR", "tier": "PLATINUM", "rank": "III", "leaguePoints": 64,
                         "wins": 12, "losses": 7}]
            else:
                require("/champion-masteries/by-puuid/" in path and parsed.query == "count=5")
                stage, identity = "masteries", path.rsplit("/", 2)[1]
                body = [{"championId": champion, "championPoints": points, "championLevel": level}
                        for champion, points, level in ((22, 123456, 15), (103, 54321, 9), (64, 12000, 6),
                                                       (99, 9000, 5), (86, 7000, 4))]
        self.calls[(stage, identity)] += 1
        if self.hook:
            self.hook(stage, identity)
        failures = self.failures.get(stage, [])
        if failures:
            status, retry_after = failures.pop(0)
            return HTTPResponse(status, {"Retry-After": str(retry_after)} if retry_after else {}, b"synthetic ignored failure")
        return HTTPResponse(200, {}, json.dumps(body, ensure_ascii=False).encode())

    def factory(self, config, *, before_request):
        self.keys.add(config.api_key)
        return RiotClient(config, before_request=before_request, transport=self)

    def stage_count(self, stage):
        return sum(count for (name, _), count in self.calls.items() if name == stage)


def table_rows(db, tables=PRESERVED_TABLES):
    # Names are a source-controlled allowlist; values never enter reports.
    require(all(name in PRESERVED_TABLES or name in RIOT_TABLES for name in tables))
    return {name: [dict(row) for row in db.execute(f"SELECT * FROM {name} ORDER BY 1,2")]
            for name in tables}


def job(core, member_id):
    with core.read_snapshot() as db:
        row = db.execute("SELECT * FROM riot_jobs WHERE member_id=?", (member_id,)).fetchone()
        return dict(row) if row else None


def finish(sync, count=4):
    for _ in range(count):
        require(sync.process_one())


def exercise(core, report):
    comp, clock = Competition(core), Clock()
    tournament, live = TournamentService(core, comp), LiveAuction(core, comp, clock=clock)
    with report.stage("personal_accounts_normal_scores_and_running_auction_fixture"):
        require(not core.has_admin() and not core.list_members(True))
        password = secrets.token_urlsafe(30)
        core.setup_admin("qa_riot_admin", password)
        admin = core.login("qa_riot_admin", password)
        members, tokens = [], []
        for index in range(20):
            username, password = f"qa_riot_{index}", secrets.token_urlsafe(30)
            registration = core.register_member(username, password, f"SyntheticRiot{index}#QA",
                ROLES[index % 5], ROLES[(index + 1) % 5], request_key=str(uuid4()), current_tier="실버 2", current_tier_lp=25)
            members.append(registration["member_id"])
            tokens.append(core.login(username, password))
            if index == 0:
                pending = tokens[0]
                blocked = RiotSync(core, RiotConfig("synthetic-riot-disabled-key"), client_factory=FakeHTTP().factory, clock=clock)
                rejected(lambda: blocked.enqueue(pending, [members[0]]), (PermissionError,))
            core.approve_member(admin, members[-1], 100)
        core.update_member(admin, members[1], "SyntheticRiot1#QA", "JG", "MID", 100,
                           "synthetic clan evaluation", clan_tier="QA 클랜 티어")
        assignments = [{"member_id": member, "role": ROLES[index % 5]} for index, member in enumerate(members)]
        normal = comp.create_normal(tokens[2], assignments[:10], "Synthetic pre-upgrade normal", balanced=False, request_key=str(uuid4()))
        game = comp.get_event(normal)["games"][0]
        comp.record_result(tokens[2], normal, game["id"], game["team_a"])
        game = comp.get_event(normal)["games"][0]
        event_id = tournament.create(tokens[3], "Synthetic Riot integration auction", "2030-01-01T20:00:00+09:00",
            "synthetic HTTP only", "AUCTION", 4, "TOURNAMENT", request_key=str(uuid4()))
        tournament.open_recruitment(tokens[3], event_id)
        tournament.set_participants(tokens[3], event_id, assignments)
        tournament.confirm_participants(tokens[3], event_id, expected_roster_token=comp.get_event(event_id)["roster_token"])
        tournament.set_captains(tokens[3], event_id, members[::5])
        tournament.prepare_auction(tokens[3], event_id)
        live.configure(tokens[3], event_id, bid_seconds=30, order=[mid for mid in members if mid not in members[::5]])
        state = live.start(tokens[3], event_id)
        lot_id = state["current_lot"]["id"]
        require(state["current_lot"]["member_id"] == members[1])
        live.place_bid(tokens[0], event_id, lot_id, 5, str(uuid4()))
        scores_before = {member["id"]: member["score"] for member in core.list_members()}
        require(sorted(scores_before.values()) == [90] * 5 + [100] * 10 + [110] * 5)
        report.data["counts"].update(members=20, accounts=21, normal_games=1, score_entries=10, preserved_live_bids=1)
    with report.stage("rebuild_owned_v5_shape_then_upgrade_to_current_without_row_changes"):
        with core.transaction() as db:
            for table in RIOT_TABLES:
                db.execute(f"DROP TABLE {table}")
            db.execute("ALTER TABLE members DROP COLUMN current_tier_source")
            if core.is_postgres:
                db.execute("DELETE FROM _schema_migrations WHERE version>=6")
                db.execute("INSERT INTO _schema_migrations(version,applied_at) VALUES(5,?) ON CONFLICT(version) DO NOTHING",
                    (datetime.now(timezone.utc).isoformat(),))
            before = table_rows(db)
        core.initialize()
        core.initialize()
        with core.read_snapshot() as db:
            after = table_rows(db)
            for table in PRESERVED_TABLES:
                require(len(before[table]) == len(after[table]))
                for old, new in zip(before[table], after[table]):
                    require(all(new.get(key) == value for key, value in old.items()))
            require(all(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0 for table in RIOT_TABLES))
            require(all(row[0] == "manual" for row in db.execute("SELECT current_tier_source FROM members")))
            if core.is_postgres:
                require(db.execute("SELECT MAX(version) FROM _schema_migrations").fetchone()[0] == SCHEMA_VERSION)
        require(all(core.session(token)["member_id"] == mid for mid, token in zip(members, tokens)))
        require(core.session(admin)["role"] == "admin")
        require({member["id"]: member["score"] for member in core.list_members()} == scores_before)
        require(live.get_state(event_id)["current_lot"]["highest_bid"] == 5)
        report.data["counts"].update(upgrade_preserved_tables=len(PRESERVED_TABLES), preserved_sessions=21, schema_version=SCHEMA_VERSION)
    with report.stage("new_api_tables_are_private_and_metadata_locks_are_independent"):
        if core.is_postgres:
            from roly.postgres import advisory_key
            with core.read_snapshot() as db:
                roles = [row[0] for row in db.execute("SELECT rolname FROM pg_roles WHERE rolname IN ('anon','authenticated')")]
                for role in roles:
                    require(not db.execute("SELECT has_schema_privilege(CAST(? AS TEXT),CAST(? AS TEXT),'USAGE')", (role, core.schema)).fetchone()[0])
                    for table in RIOT_TABLES:
                        require(not db.execute("SELECT has_table_privilege(CAST(? AS TEXT),CAST(? AS TEXT),'SELECT,INSERT,UPDATE,DELETE')", (role, core.schema + "." + table)).fetchone()[0])
            for purpose in ("jobs", "rate:synthetic-independent"):
                with _metadata_transaction(core, purpose):
                    with core.read_snapshot() as independent:
                        require(independent.execute("SELECT pg_try_advisory_xact_lock(?)", (advisory_key(core.schema),)).fetchone()[0])
            report.data["counts"].update(denied_api_roles=len(roles), independent_metadata_locks=2)
        else:
            report.data["counts"]["independent_metadata_locks"] = "PostgreSQL-only; SQLite uses its single writer"
    http = FakeHTTP()
    sync = RiotSync(core, RiotConfig("synthetic-riot-key-one"), client_factory=http.factory, clock=clock)
    with patch("roly.riot_api._http_get", side_effect=AssertionError("Outbound Riot HTTP is forbidden in this rehearsal")), \
         patch("roly.riot_sync.DataDragonClient", side_effect=lambda: DataDragonClient(transport=http)), \
         patch.dict("roly.riot_sync._static_cache", {"until": 0.0, "version": "", "champions": {}}, clear=True):
        with report.stage("approved_members_enqueue_and_anonymous_requests_are_rejected"):
            rejected(lambda: sync.enqueue(None, [members[1]]), (PermissionError,))
            rejected(lambda: sync.enqueue("not-a-session", [members[1]]), (PermissionError,))
            require(sync.enqueue(tokens[4], [members[1], members[1]]) == 1)
            require(sync.enqueue(tokens[4], [members[1]]) == 0)
            require(http.stage_count("account") == 0)
        with report.stage("queued_http_refresh_updates_api_tier_and_mastery_not_power_or_frozen_roster"):
            member_before = core.get_member(members[1])
            frozen_before = comp.get_event(event_id)["players"]
            normal_before = core.get_game(game["core_game_id"]) if game.get("core_game_id") else None
            finish(sync)
            member_after = core.get_member(members[1])
            require((member_after["current_tier"], member_after["current_tier_lp"], member_after["current_tier_source"]) == ("골드 2", 37, "riot"))
            for field in ("score", "base_score", "wins", "losses", "award_units", "clan_tier", "main_role", "sub_role", "notes", "application_notes"):
                require(member_after[field] == member_before[field])
            require(comp.get_event(event_id)["players"] == frozen_before)
            if normal_before:
                require(core.get_game(game["core_game_id"]) == normal_before)
            profile = sync.get_profiles([members[1]])[members[1]]
            require(profile["status"] == "DONE" and [champion["points"] for champion in profile["champions"]] == [123456, 54321, 12000, 9000, 7000])
            require([champion["name"] for champion in profile["champions"]] == ["애쉬", "아리", "리 신", "럭스", "가렌"])
            require(profile["champions"][0]["name"] == "애쉬" and "puuid" not in profile)
            require((profile["flex_current_tier"], profile["flex_lp"], profile["flex_rank_wins"], profile["flex_rank_losses"]) ==
                    ("플래티넘 3", 64, 12, 7))
            require(http.stage_count("rank") == 1)
            require(http.stage_count("dd_versions") == http.stage_count("dd_champions") == 1)
            require(sync.enqueue(tokens[4], [members[1]]) == 0)
        with report.stage("auction_snapshot_exposes_cached_public_profile_without_http"):
            calls = sum(http.calls.values())
            actor, state = live.get_view(tokens[9], event_id)
            require(actor["member_status"] == "APPROVED" and state["current_lot"]["riot_profile"]["current_tier"] == "골드 2")
            require(state["current_lot"]["current_tier_snapshot"] == "실버 2")
            require(state["current_lot"]["clan_tier_snapshot"] == "QA 클랜 티어")
            public = state["current_lot"]["riot_profile"]
            required_fields = {"current_tier", "lp", "champions", "profile_icon_url", "updated_at"}
            numeric_fields = {"summoner_level", "rank_wins", "rank_losses", "flex_rank_wins", "flex_rank_losses"}
            optional_fields = numeric_fields | {"flex_current_tier", "flex_lp"}
            require(required_fields <= set(public) <= required_fields | optional_fields)
            for field in numeric_fields & set(public):
                require(type(public[field]) is int and 0 <= public[field] <= 10_000_000)
            if "flex_current_tier" in public or "flex_lp" in public:
                require({"flex_current_tier", "flex_lp"} <= set(public))
                require(validate_current_tier(public["flex_current_tier"], public["flex_lp"]) ==
                        (public["flex_current_tier"], public["flex_lp"]))
            require(sum(http.calls.values()) == calls)
        with report.stage("http_429_preserves_partial_stage_and_resumes_after_retry_after"):
            require(sync.enqueue(tokens[4], [members[2]]) == 1)
            account_calls = http.stage_count("account")
            finish(sync, 2)
            http.failures["rank"] = [(429, 10)]
            require(sync.process_one())
            old = job(core, members[2])
            require(old["stage"] == 2 and old["last_error"] == "rate_limited")
            require("puuid" in json.loads(old["partial_payload"]))
            calls = sum(http.calls.values())
            require(not sync.process_one())
            resume_at = clock.value + 10
            clock.value = resume_at - 0.001
            require(not sync.process_one() and sum(http.calls.values()) == calls)
            clock.value = resume_at
            finish(sync, 2)
            require(job(core, members[2])["status"] == "DONE")
            require(http.stage_count("account") == account_calls + 1)
        with report.stage("auth_failure_blocks_old_key_and_new_key_resumes_same_partial_job"):
            require(sync.enqueue(tokens[4], [members[3]]) == 1)
            finish(sync, 2)
            http.failures["rank"] = [(403, None)]
            require(sync.process_one())
            require(job(core, members[3])["stage"] == 2)
            calls, accounts = sum(http.calls.values()), http.stage_count("account")
            require(not sync.process_one() and sum(http.calls.values()) == calls)
            replacement = RiotSync(core, RiotConfig("synthetic-riot-key-two"), client_factory=http.factory, clock=clock)
            finish(replacement, 2)
            require(job(core, members[3])["status"] == "DONE" and http.stage_count("account") == accounts)
            require(not sync.process_one())
        with report.stage("slow_http_holds_no_database_writer_lock_and_other_captain_bid_commits"):
            require(replacement.enqueue(tokens[4], [members[4]]) == 1)
            entered, release = threading.Event(), threading.Event()
            def gate(stage, identity):
                if stage == "account":
                    entered.set()
                    require(release.wait(15))
            http.hook = gate
            try:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    response = pool.submit(replacement.process_one)
                    try:
                        require(entered.wait(8))
                        require(not response.done())
                        started = time.monotonic()
                        accepted = pool.submit(live.place_bid, tokens[5], event_id, lot_id, 10, str(uuid4())).result(timeout=8)
                        report.data["timings_seconds"]["bid_while_http_blocked"] = round(time.monotonic() - started, 3)
                        require(accepted["accepted"] and not accepted["replayed"] and not response.done())
                        require(replacement._claim() is None)
                    finally:
                        release.set()
                    require(response.result(timeout=8))
            finally:
                http.hook = None
            require(job(core, members[4])["stage"] == 1)
            finish(replacement, 3)
            require(job(core, members[4])["status"] == "DONE")
        with report.stage("canonical_rename_invalidates_api_cache_but_preserves_historical_roster"):
            member = core.get_member(members[1])
            rejected(lambda: core.update_member(admin, members[1], member["riot_id"], "JG", "MID", member["base_score"],
                "synthetic blocked manual API override", current_tier="실버 1"))
            core.update_member(admin, members[1], "RenamedSynthetic#QA", "JG", "MID", member["base_score"], "synthetic verified Riot identity change")
            renamed = core.get_member(members[1])
            require((renamed["current_tier"], renamed["current_tier_source"]) == ("", "manual"))
            require(live.get_state(event_id)["current_lot"]["riot_profile"] is None)
            require(comp.get_event(event_id)["players"] == frozen_before)
            require(core.session(tokens[1])["member_id"] == members[1])
        report.data["counts"].update(fake_riot_http_requests=sum(value for (stage, _), value in http.calls.items() if not stage.startswith("dd_")),
            fake_dd_http_requests=http.stage_count("dd_versions") + http.stage_count("dd_champions"), api_refreshes_completed=4)
    with report.stage("global_twenty_per_second_shared_across_scopes_and_connections"):
        rate_key = sha256(b"synthetic-isolated-rate-probe").hexdigest()
        limiter = DatabaseRateLimiter(core, rate_key, clock)
        def reserve(index):
            try:
                DatabaseRateLimiter(core, rate_key, clock).reserve("asia" if index % 2 else "kr")
                return "accepted"
            except RiotAPIError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(reserve, range(24)))
        require(results.count("accepted") == 20 and results.count("rate_limited") == 4)
        report.data["counts"].update(rate_concurrent_requests=24, rate_one_second_accepted=20, rate_one_second_rejected=4)
    with report.stage("global_hundred_per_two_minutes_rejects_then_recovers_at_boundary"):
        for _ in range(4):
            clock.value += 1
            for index in range(20):
                limiter.reserve("asia" if index % 2 else "kr")
        clock.value += 1
        try:
            limiter.reserve("asia")
            require(False)
        except RiotAPIError as error:
            require(error.code == "rate_limited" and error.retry_after > 100)
        clock.value += 120
        limiter.reserve("kr")
        report.data["counts"].update(rate_two_minute_accepted=100, rate_recovery_accepted=1)
    with report.stage("final_scores_games_rosters_and_bid_ledger_remain_intact"):
        require({member["id"]: member["score"] for member in core.list_members()} == scores_before)
        require(comp.get_event(event_id)["players"] == frozen_before)
        with core.read_snapshot() as db:
            require(db.execute("SELECT COUNT(*) FROM score_ledger").fetchone()[0] == 10)
            require(db.execute("SELECT COUNT(*) FROM live_bids").fetchone()[0] == 2)
            require(db.execute("SELECT COUNT(*) FROM award_ledger").fetchone()[0] == 0)
        comp.cancel_event(tokens[3], event_id, "synthetic rehearsal complete")
        live.settle_due()
        state = live.get_state(event_id)
        require(state["status"] == "CANCELLED" and all(player["price"] == 0 for player in state["event"]["players"]))
        report.data["counts"].update(final_bids=2, auction_charged_points=0, award_entries=0, outbound_riot_requests=0)


def source_hashes():
    return {name: sha256((ROOT / name).read_bytes()).hexdigest() for name in
        ("roly/core.py", "roly/postgres.py", "roly/riot_api.py", "roly/riot_sync.py", "roly/riot_profile.py", "roly/live_auction.py")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sqlite", action="store_true")
    mode.add_argument("--remote", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    output = args.output or ROOT / "test-artifacts" / ("riot-rehearsal-remote.json" if args.remote else "riot-rehearsal-sqlite.json")
    report = Report("postgresql-riot-fake-http" if args.remote else "sqlite-riot-fake-http")
    report.data["limitations"] = ["Riot and Data Dragon HTTP are synthetic; no real Riot key or account verification",
        "Database connections and overlapping requests are real; auction/rate/lease time is controlled",
        "The v5 fixture is constructed by removing only new v6 fields/tables in this fresh QA database",
        "No browser/UI validation, operating schema access or latency guarantee"]
    report.data["source_before"] = source_hashes()
    created, temporary = None, None
    try:
        with report.stage("create_new_owned_qa_database"):
            if args.remote:
                from roly.postgres import _driver, connect, drop_qa_schema
                schema = new_qa_schema()
                require_qa_schema(schema)
                with connect(schema) as db:
                    db.execute("BEGIN IMMEDIATE")
                    require(not db.execute("SELECT 1 FROM pg_namespace WHERE nspname=?", (schema,)).fetchone())
                    db._raw.execute(_driver().sql.SQL("CREATE SCHEMA {}").format(_driver().sql.Identifier(schema)))
                created = schema
                report.data["qa_schema"] = schema
                core = Core("supabase://" + schema)
            else:
                temporary = tempfile.TemporaryDirectory(prefix="roly-riot-rehearsal-")
                core = Core(Path(temporary.name) / "synthetic.sqlite3")
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
            with report.stage("drop_only_owned_qa_database_and_verify_absence"):
                if created:
                    drop_qa_schema(created)
                    with connect(created) as db:
                        require(not db.execute("SELECT 1 FROM pg_namespace WHERE nspname=?", (created,)).fetchone())
                if temporary:
                    path = Path(temporary.name)
                    temporary.cleanup()
                    require(not path.exists())
            report.data["cleaned_up"] = True
        except Exception as error:
            report.data.update(ok=False, cleaned_up=False, cleanup_error_type=type(error).__name__)
    report.data["source_unchanged"] = source_hashes() == report.data["source_before"]
    document = json.dumps(report.finish(), ensure_ascii=False, indent=2)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document + "\n", encoding="utf-8")
    print(document)
    return 0 if report.data.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
