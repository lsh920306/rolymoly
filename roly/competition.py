"""Competition rules and persistent state for the new Rolymoly application.

All mutations run in a Core transaction. A competition stores the roster and
valuation used when it was created, while Core owns scored games and awards.
"""
from __future__ import annotations

import itertools
import json
import random
from hashlib import sha256
from contextlib import closing
from datetime import datetime, timezone
from uuid import UUID

from .member_ranks import RANK_SELECT, RANK_JOINS, project_member


ROLES = ("TOP", "JG", "MID", "AD", "SUP")
CREATION_REQUESTS_DDL = """
CREATE TABLE IF NOT EXISTS competition_creation_requests (
    request_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
    actor_id INTEGER NOT NULL REFERENCES accounts(id),
    event_id INTEGER NOT NULL UNIQUE REFERENCES competition_events(id),
    kind TEXT NOT NULL, created_at TEXT NOT NULL
);
"""
AUCTION_BUDGETS = (
    (700, 690), (600, 790), (550, 840), (480, 910), (450, 940),
    (420, 970), (390, 1000), (340, 1050), (320, 1070), (300, 1090),
    (280, 1110), (230, 1160), (220, 1170), (210, 1180), (200, 1190),
    (150, 1240), (140, 1250), (130, 1260), (120, 1270), (90, 1300),
    (80, 1310), (70, 1320),
)


def auction_budget(score: float) -> int:
    return next((budget for lower, budget in AUCTION_BUDGETS if score >= lower), 1330)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _table_exists(conn, name):
    if hasattr(conn, "table_exists"):
        return conn.table_exists(name)
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def balance_teams(players: list[dict]) -> list[list[dict]]:
    """Return 2 or 4 teams, minimizing the largest team-total difference."""
    if len(players) not in (10, 20):
        raise ValueError("일반내전은 10명 또는 20명이 필요합니다.")
    count = len(players) // 5
    buckets = [[p for p in players if p["role"] == role] for role in ROLES]
    if any(len(bucket) != count for bucket in buckets):
        raise ValueError(f"각 포지션에 {count}명씩 배정해 주세요.")
    if len({p["member_id"] for p in players}) != len(players):
        raise ValueError("같은 선수가 두 번 참가할 수 없습니다.")
    # Fixing the first role removes permutations that merely rename teams.
    best = None
    best_key = None
    permutations = [list(itertools.permutations(bucket)) for bucket in buckets[1:]]
    for other_roles in itertools.product(*permutations):
        columns = (tuple(buckets[0]),) + other_roles
        totals = [sum(column[i]["score"] for column in columns) for i in range(count)]
        key = (max(totals) - min(totals), sum(total * total for total in totals))
        if best_key is None or key < best_key:
            best_key = key
            best = [[column[i] for column in columns] for i in range(count)]
        if key[0] == 0:
            break
    return best


class Competition:
    def __init__(self, service):
        self.service = service
        if service.is_postgres:
            # PostgreSQL has one versioned migration for the whole schema.
            return
        with service.transaction() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS competition_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL, kind TEXT NOT NULL,
                    format TEXT NOT NULL, status TEXT NOT NULL,
                    created_by INTEGER NOT NULL, created_at TEXT NOT NULL,
                    current_player_id INTEGER, winner_team_id INTEGER,
                    policy_snapshot TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS competition_teams (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER NOT NULL REFERENCES competition_events(id),
                    name TEXT NOT NULL, captain_id INTEGER,
                    budget INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS competition_players (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER NOT NULL REFERENCES competition_events(id),
                    member_id INTEGER NOT NULL, riot_id TEXT NOT NULL,
                    role TEXT NOT NULL, score REAL NOT NULL,
                    team_id INTEGER REFERENCES competition_teams(id),
                    price INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'AVAILABLE',
                    UNIQUE(event_id, member_id)
                );
                CREATE TABLE IF NOT EXISTS competition_games (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER NOT NULL REFERENCES competition_events(id),
                    round INTEGER NOT NULL, position INTEGER NOT NULL,
                    group_key TEXT NOT NULL DEFAULT '',
                    stage TEXT NOT NULL DEFAULT 'MAIN',
                    team_a INTEGER REFERENCES competition_teams(id),
                    team_b INTEGER REFERENCES competition_teams(id),
                    source_a INTEGER REFERENCES competition_games(id),
                    source_b INTEGER REFERENCES competition_games(id),
                    winner_team_id INTEGER REFERENCES competition_teams(id),
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    core_game_id INTEGER,
                    UNIQUE(event_id, stage, group_key, round, position)
                );
                CREATE TABLE IF NOT EXISTS competition_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER NOT NULL, actor_id INTEGER NOT NULL,
                    action TEXT NOT NULL, detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """ + CREATION_REQUESTS_DDL)
            # Additive migrations preserve existing local databases and legacy APIs.
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            additions = {
                "competition_events": {"starts_at": "TEXT", "description": "TEXT NOT NULL DEFAULT ''",
                    "build_mode": "TEXT NOT NULL DEFAULT ''", "team_count": "INTEGER NOT NULL DEFAULT 0",
                    "workflow_version": "INTEGER NOT NULL DEFAULT 0", "participants_confirmed_at": "TEXT"},
                "competition_players": {"participation_status": "TEXT NOT NULL DEFAULT 'SELECTED'",
                    "attendance_status": "TEXT NOT NULL DEFAULT 'NOT_REQUESTED'", "attendance_requested_at": "TEXT",
                    "attendance_confirmed_at": "TEXT", "warning_count": "INTEGER NOT NULL DEFAULT 0",
                    "exclusion_reason": "TEXT NOT NULL DEFAULT ''", "tier_snapshot": "TEXT",
                    "main_role_snapshot": "TEXT", "sub_role_snapshot": "TEXT",
                    "clan_tier_snapshot": "TEXT", "current_tier_snapshot": "TEXT",
                    "current_tier_lp_snapshot": "INTEGER"},
                "competition_games": {"source_a_result": "TEXT NOT NULL DEFAULT 'WINNER'",
                    "source_b_result": "TEXT NOT NULL DEFAULT 'WINNER'", "attempt": "INTEGER NOT NULL DEFAULT 1"},
            }
            for table, columns in additions.items():
                existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                for name, definition in columns.items():
                    if name not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            conn.execute("UPDATE competition_events SET build_mode=CASE kind WHEN 'AUCTION' THEN 'AUCTION' ELSE 'LEGACY' END WHERE build_mode=''")
            conn.execute("UPDATE competition_events SET team_count=(SELECT COUNT(*) FROM competition_teams WHERE event_id=competition_events.id) WHERE team_count=0 AND workflow_version=0")

    @staticmethod
    def _event(conn, event_id):
        row = conn.execute("SELECT e.*,a.display_name AS created_by_name FROM competition_events e LEFT JOIN accounts a ON a.id=e.created_by WHERE e.id=?", (event_id,)).fetchone()
        if row is None:
            raise ValueError("대회를 찾을 수 없습니다.")
        return dict(row)

    def _authorize(self, conn, token, event_id=None):
        return self.service.require_event_manager(conn, token, event_id)

    @staticmethod
    def _creation_request(conn, actor, kind, body, request_key):
        """Check only submitted intent, so a receipt survives later profile changes."""
        if request_key is None:
            return None, None, None
        try:
            key = UUID(str(request_key))
            if key.int == 0:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ValueError("내전·경매 생성 요청의 고유 UUID가 필요합니다.") from None
        key = str(key)
        fingerprint = sha256(json.dumps([actor["id"], kind, body], ensure_ascii=False,
                                       sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        receipt = conn.execute("SELECT event_id,fingerprint FROM competition_creation_requests WHERE request_key=?", (key,)).fetchone()
        if receipt and receipt["fingerprint"] != fingerprint:
            raise ValueError("같은 생성 요청 번호에 다른 내용·주최자·종류가 전달되었습니다. 새 생성을 시작해 주세요.")
        return key, fingerprint, receipt["event_id"] if receipt else None

    @staticmethod
    def _save_creation_request(conn, actor, event_id, kind, key, fingerprint):
        if key is not None:
            conn.execute("INSERT INTO competition_creation_requests(request_key,fingerprint,actor_id,event_id,kind,created_at) VALUES(?,?,?,?,?,?)",
                         (key, fingerprint, actor["id"], event_id, kind, _now()))

    @staticmethod
    def _roster_digest(event, players, teams, games, audit_id):
        # Include the last operation ID so changing a roster away and back does
        # not make an old browser draft valid again. No schema migration needed.
        fields = ("id", "kind", "status", "format", "build_mode", "team_count", "participants_confirmed_at")
        payload = [{key: event.get(key) for key in fields},
                   [dict(player) for player in players],
                   [{key: team[key] for key in ("id", "event_id", "name", "captain_id", "budget")} for team in teams],
                   [{key: game.get(key) for key in ("id", "status", "team_a", "team_b", "core_game_id", "attempt")} for game in games], audit_id]
        return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()

    def _roster_token(self, conn, event_id):
        return self._roster_snapshot(conn, event_id)["token"]

    def _roster_snapshot(self, conn, event_id, event=None):
        event = event if event is not None else self._event(conn, event_id)
        players = [dict(row) for row in conn.execute("SELECT * FROM competition_players WHERE event_id=? ORDER BY id", (event_id,))]
        teams = [dict(row) for row in conn.execute("SELECT * FROM competition_teams WHERE event_id=? ORDER BY id", (event_id,))]
        games = [dict(row) for row in conn.execute("SELECT * FROM competition_games WHERE event_id=? ORDER BY id", (event_id,))]
        audit_id = conn.execute("SELECT COALESCE(MAX(id),0) FROM competition_audit WHERE event_id=?", (event_id,)).fetchone()[0]
        return {"players": players, "teams": teams, "games": games,
                "token": self._roster_digest(event, players, teams, games, audit_id)}

    def _check_roster_token(self, conn, event_id, expected):
        if expected is not None and (not isinstance(expected, str) or expected != self._roster_token(conn, event_id)):
            raise ValueError("다른 화면에서 명단이나 진행 상태가 변경되었습니다. 최신 명단을 불러온 뒤 다시 확인해 주세요.")

    def _audit(self, conn, event_id, actor, action, detail):
        conn.execute("INSERT INTO competition_audit(event_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
                     (event_id, actor["id"], action, detail, _now()))

    def _player_snapshot(self, conn, member_id, role=None):
        member = self.service.get_member(int(member_id), conn=conn)
        return self._snapshot_member(member, member_id, role)

    @staticmethod
    def _snapshot_member(member, member_id, role=None):
        if not member or str(member["status"]).upper() != "APPROVED":
            raise ValueError("승인된 회원만 참가할 수 있습니다.")
        selected_role = role or member.get("main_role") or member.get("primary_role")
        if selected_role not in ROLES:
            raise ValueError("참가자의 포지션을 TOP/JG/MID/AD/SUP 중에서 선택해 주세요.")
        return {"member_id": int(member_id), "riot_id": member["riot_id"],
                "role": selected_role, "score": float(member["score"]),
                "main_role_snapshot": member.get("main_role"), "sub_role_snapshot": member.get("sub_role"),
                "clan_tier_snapshot": member.get("clan_tier", ""),
                "current_tier_snapshot": member.get("current_tier", ""),
                "current_tier_lp_snapshot": member.get("current_tier_lp")}

    def _player_snapshots(self, conn, assignments):
        """Read only roster fields in one statement, preserving submitted order."""
        assignments = list(assignments)
        ids = list(dict.fromkeys(int(item["member_id"]) for item in assignments))
        if not ids:
            return []
        query = """SELECT m.id,m.riot_id,m.status,m.main_role,m.sub_role,m.clan_tier,
            m.base_score+COALESCE((SELECT SUM(s.amount) FROM score_ledger s WHERE s.member_id=m.id),0) AS score,
            """ + RANK_SELECT + " FROM members m" + RANK_JOINS + " WHERE m.id IN (" + ",".join("?" for _ in ids) + ")"
        members = {row["id"]: project_member(row) for row in conn.execute(query, tuple(ids))}
        if any(member_id not in members for member_id in ids):
            raise ValueError("회원을 찾을 수 없습니다.")
        return [self._snapshot_member(members[int(item["member_id"])], item["member_id"], item["role"]) for item in assignments]

    @staticmethod
    def _check_balance_inputs(expected, actual):
        if expected != actual:
            raise ValueError("팀 편성 계산 중 참가자·전력·배정 상태가 변경되었습니다. 최신 명단에서 다시 편성해 주세요.")

    @staticmethod
    def _write_statements(conn, statements):
        # Reuse the caller's transaction; never commit a partial roster.
        if hasattr(conn, "execute_batch"):
            conn.execute_batch(statements)
        else:
            for query, parameters in statements:
                conn.execute(query, parameters)

    def _new_event(self, conn, actor, title, kind, format_name, status):
        if format_name not in ("SINGLE", "LEAGUE", "TOURNAMENT", "GROUP_STAGE", "RANKING"):
            raise ValueError("지원하지 않는 대회 방식입니다.")
        title = str(title).strip() or ("일반내전" if kind == "NORMAL" else "경매내전")
        if len(title) > 100:
            raise ValueError("대회 이름은 100자 이내로 입력해 주세요.")
        score_policy = self.service.policy(conn=conn)
        snapshot = {"auction_budgets": list(AUCTION_BUDGETS), "auction_default": 1330,
                    "score_policy": score_policy,
                    "award_cats": {"4": 1, "6": 5, "8": 25},
                    "ties": "승점, 동률팀 승점, 추가 경기"}
        return conn.execute("INSERT INTO competition_events(title,kind,format,status,created_by,created_at,policy_snapshot) VALUES(?,?,?,?,?,?,?)",
                            (title, kind, format_name, status, actor["id"], _now(), json.dumps(snapshot, ensure_ascii=False))).lastrowid

    def _insert_player(self, conn, event_id, player, team_id=None, price=0):
        conn.execute(*self._insert_player_statement(event_id, player, team_id, price))

    @staticmethod
    def _insert_player_statement(event_id, player, team_id=None, price=0):
        return ("INSERT INTO competition_players(event_id,member_id,riot_id,role,score,team_id,price,state,main_role_snapshot,sub_role_snapshot,clan_tier_snapshot,current_tier_snapshot,current_tier_lp_snapshot) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, player["member_id"], player["riot_id"], player["role"], player["score"],
                      team_id, price, "ASSIGNED" if team_id else "AVAILABLE",
                      player.get("main_role_snapshot"), player.get("sub_role_snapshot"),
                      player.get("clan_tier_snapshot"), player.get("current_tier_snapshot"), player.get("current_tier_lp_snapshot")))

    def create_normal(self, token, assignments, title="", balanced=True, format_name="LEAGUE", request_key=None):
        assignments = [{"member_id": int(p["member_id"]), "role": p["role"]} for p in assignments]
        body = {"flow": "normal", "assignments": assignments, "title": str(title).strip(),
                "balanced": bool(balanced), "format": format_name}
        with self.service.read_snapshot() as conn:
            actor = self._authorize(conn, token)
            key, fingerprint, receipt = self._creation_request(conn, actor, "NORMAL", body, request_key)
            if receipt is not None:
                return receipt
            players = self._player_snapshots(conn, assignments)
            if len(players) not in (10, 20):
                raise ValueError("일반내전은 10명 또는 20명을 선택해 주세요.")
            if len({p["member_id"] for p in players}) != len(players):
                raise ValueError("중복 참가자가 있습니다.")
            count = len(players) // 5
            buckets = [[p for p in players if p["role"] == role] for role in ROLES]
            if any(len(bucket) != count for bucket in buckets):
                raise ValueError(f"각 포지션에 {count}명씩 선택해 주세요.")
            if count == 2:
                format_name = "SINGLE"
            elif format_name not in ("LEAGUE", "TOURNAMENT"):
                raise ValueError("20인 일반내전은 풀리그 또는 토너먼트를 선택해 주세요.")
        # Close the read transaction and return its connection before CPU work.
        teams = balance_teams(players) if balanced else [[bucket[i] for bucket in buckets] for i in range(count)]
        with self.service.transaction() as conn:
            actor = self._authorize(conn, token)
            key, fingerprint, receipt = self._creation_request(conn, actor, "NORMAL", body, request_key)
            if receipt is not None:
                return receipt
            self._check_balance_inputs(players, self._player_snapshots(conn, assignments))
            event_id = self._new_event(conn, actor, title, "NORMAL", format_name, "READY")
            conn.execute("UPDATE competition_events SET build_mode=?,team_count=? WHERE id=?", ("BALANCE" if balanced else "MANUAL", count, event_id))
            team_ids = []
            statements = []
            for i, members in enumerate(teams):
                team_id = conn.execute("INSERT INTO competition_teams(event_id,name) VALUES(?,?)", (event_id, f"{i + 1}팀")).lastrowid
                team_ids.append(team_id)
                for player in members:
                    statements.append(self._insert_player_statement(event_id, player, team_id))
            self._write_statements(conn, statements)
            self._make_schedule(conn, event_id, team_ids, format_name)
            self._audit(conn, event_id, actor, "CREATE", f"{len(players)}인 일반내전, {format_name}")
            self._save_creation_request(conn, actor, event_id, "NORMAL", key, fingerprint)
            return event_id

    def replace_normal_roster(self, token, event_id, assignments, reason, expected_roster_token, balanced=None):
        """Replace an unplayed normal roster without changing event identity."""
        reason = str(reason).strip()
        if not reason or len(reason) > 1000:
            raise ValueError("명단 변경 사유를 1~1,000자로 입력해 주세요.")
        if not expected_roster_token:
            raise ValueError("최신 명단을 불러온 뒤 다시 저장해 주세요.")
        assignments = [{"member_id": int(item["member_id"]), "role": item["role"]} for item in assignments]
        with self.service.read_snapshot() as conn:
            actor, event, snapshot = self._replacement_inputs(conn, token, event_id, expected_roster_token)
            players = self._player_snapshots(conn, assignments)
            count = event["team_count"]
            if count not in (2, 4) or len(players) != count * 5 or len({p["member_id"] for p in players}) != len(players):
                raise ValueError(f"기존 정원 {count * 5}명에 맞게 중복 없이 참가자를 선택해 주세요.")
            buckets = [[p for p in players if p["role"] == role] for role in ROLES]
            if any(len(bucket) != count for bucket in buckets):
                raise ValueError(f"각 포지션에 {count}명씩 선택해 주세요.")
            if balanced is None:
                balanced = event["build_mode"] == "BALANCE"
            if not isinstance(balanced, bool):
                raise ValueError("팀 균형 적용 여부를 확인해 주세요.")
        teams = balance_teams(players) if balanced else [[bucket[index] for bucket in buckets] for index in range(count)]
        with self.service.transaction() as conn:
            actor, event, snapshot = self._replacement_inputs(conn, token, event_id, expected_roster_token)
            self._check_balance_inputs(players, self._player_snapshots(conn, assignments))
            before = {"players": snapshot["players"], "teams": snapshot["teams"], "games": snapshot["games"],
                      "build_mode": event["build_mode"]}
            # Pending bracket links reference one another. Clear links before
            # deleting only this unplayed event's fixtures and team snapshots.
            self._write_statements(conn, [
                ("UPDATE competition_games SET source_a=NULL,source_b=NULL WHERE event_id=?", (event_id,)),
                ("DELETE FROM competition_games WHERE event_id=?", (event_id,)),
                ("DELETE FROM competition_players WHERE event_id=?", (event_id,)),
                ("DELETE FROM competition_teams WHERE event_id=?", (event_id,))])
            team_ids = []
            statements = []
            for index, roster in enumerate(teams):
                team_id = conn.execute("INSERT INTO competition_teams(event_id,name) VALUES(?,?)", (event_id, f"{index + 1}팀")).lastrowid
                team_ids.append(team_id)
                for player in roster:
                    statements.append(self._insert_player_statement(event_id, player, team_id))
            self._write_statements(conn, statements)
            self._make_schedule(conn, event_id, team_ids, event["format"])
            conn.execute("UPDATE competition_events SET build_mode=?,current_player_id=NULL,winner_team_id=NULL WHERE id=?", ("BALANCE" if balanced else "MANUAL", event_id))
            self._audit(conn, event_id, actor, "NORMAL_ROSTER_REPLACE", json.dumps({"reason": reason, "before": before, "after": players, "balanced": balanced}, ensure_ascii=False))
        return event_id

    def _replacement_inputs(self, conn, token, event_id, expected_roster_token):
        actor = self._authorize(conn, token, event_id)
        event = self._event(conn, event_id)
        if event["kind"] != "NORMAL" or event["status"] != "READY":
            raise ValueError("첫 경기 전의 일반내전 명단만 다시 편성할 수 있습니다.")
        snapshot = self._roster_snapshot(conn, event_id, event)
        if expected_roster_token != snapshot["token"]:
            raise ValueError("다른 화면에서 명단이나 진행 상태가 변경되었습니다. 최신 명단을 불러온 뒤 다시 확인해 주세요.")
        self._guard_live(conn, event_id)
        if conn.execute("SELECT 1 FROM games WHERE tournament_id=? LIMIT 1", (str(event_id),)).fetchone() or any(
                game["core_game_id"] is not None or game["status"] not in ("PENDING", "BYE") for game in snapshot["games"]):
            raise ValueError("실제 경기 이력이 있는 일반내전은 명단을 다시 편성할 수 없습니다.")
        if conn.execute("SELECT 1 FROM award_batches WHERE event_id=?", (str(event_id),)).fetchone():
            raise ValueError("보상이 확정된 행사 명단은 변경할 수 없습니다.")
        return actor, event, snapshot

    def create_auction(self, token, member_ids, captain_ids, title="", format_name="LEAGUE"):
        with self.service.transaction() as conn:
            actor = self._authorize(conn, token)
            member_ids = [int(i) for i in member_ids]
            if len(captain_ids) not in (4, 6, 8) or any(i is None for i in captain_ids):
                raise ValueError("경매는 4·6·8팀이며 모든 팀장을 지정해야 합니다.")
            captain_ids = [int(i) for i in captain_ids]
            if len(set(captain_ids)) != len(captain_ids):
                raise ValueError("팀장을 중복 지정할 수 없습니다.")
            if len(member_ids) != 5 * len(captain_ids) or len(set(member_ids)) != len(member_ids):
                raise ValueError("팀당 5명에 맞는 서로 다른 참가자를 선택해 주세요.")
            if not set(captain_ids).issubset(member_ids):
                raise ValueError("모든 팀장은 참가자 명단에 포함되어야 합니다.")
            if format_name not in ("LEAGUE", "TOURNAMENT", "GROUP_STAGE", "RANKING"):
                raise ValueError("경매 대회 방식을 선택해 주세요.")
            if format_name == "GROUP_STAGE" and len(captain_ids) not in (6, 8):
                raise ValueError("조별리그는 6팀 또는 8팀에서 사용할 수 있습니다.")
            if format_name == "RANKING" and len(captain_ids) != 4:
                raise ValueError("순위 결정전은 4팀에서만 진행할 수 있습니다.")
            players = {i: self._player_snapshot(conn, i) for i in member_ids}
            event_id = self._new_event(conn, actor, title, "AUCTION", format_name, "AUCTION")
            conn.execute("UPDATE competition_events SET build_mode='AUCTION',team_count=? WHERE id=?", (len(captain_ids), event_id))
            captain_teams = {}
            for i, captain in enumerate(captain_ids):
                player = players[captain]
                team_id = conn.execute("INSERT INTO competition_teams(event_id,name,captain_id,budget) VALUES(?,?,?,?)",
                                       (event_id, f"{player['riot_id'].split('#')[0]} 팀", captain, auction_budget(player["score"]))).lastrowid
                captain_teams[captain] = team_id
            for member_id, player in players.items():
                self._insert_player(conn, event_id, player, captain_teams.get(member_id))
            self._audit(conn, event_id, actor, "CREATE", f"{len(captain_ids)}팀 경매, {format_name}")
            return event_id

    def swap_players(self, token, event_id, first_member_id, second_member_id, reason="팀 균형 조정",
                     *, expected_roster_token=None, request_id=None):
        """Swap once for a reviewed UI request; retain the legacy internal API."""
        guarded = expected_roster_token is not None or request_id is not None
        reason = str(reason).strip()
        if guarded:
            from roly.core import integer
            event_id, first_member_id, second_member_id = (integer(value) for value in
                (event_id, first_member_id, second_member_id))
            try:
                parsed = UUID(str(request_id))
                if not parsed.int:
                    raise ValueError()
                request_id = str(parsed)
            except (ValueError, AttributeError, TypeError):
                raise ValueError("유효한 선수 교환 요청 번호(UUID)가 필요합니다.") from None
            if (not isinstance(expected_roster_token, str) or len(expected_roster_token) != 64
                    or not reason or len(reason) > 1000):
                raise ValueError("최신 교환 명단과 1~1,000자의 교환 사유를 확인해 주세요.")
        with self.service.transaction() as conn:
            actor = self._authorize(conn, token, event_id)
            if guarded:
                payload_hash = sha256(json.dumps([actor["id"], event_id, first_member_id,
                    second_member_id, reason, expected_roster_token], ensure_ascii=False).encode()).hexdigest()
                # Like sale corrections, low-volume swap receipts belong to the
                # append-only audit. Replay precedes the changed-roster check.
                pattern = '%"request_id": "' + request_id + '"%'
                for row in conn.execute("SELECT detail FROM competition_audit WHERE action='SWAP' AND detail LIKE ?", (pattern,)):
                    try:
                        previous = json.loads(row["detail"])
                    except (ValueError, TypeError):
                        continue  # Historical swaps contain plain text.
                    if isinstance(previous, dict) and previous.get("request_id") == request_id:
                        if previous.get("payload_hash") != payload_hash:
                            raise ValueError("같은 요청 번호로 다른 선수 교환을 전송할 수 없습니다.")
                        return {**previous["result"], "replayed": True}
            event = self._event(conn, event_id)
            if event["kind"] != "NORMAL" or event["status"] != "READY":
                raise ValueError("일반내전 첫 경기 시작 전에만 선수를 교환할 수 있습니다.")
            if guarded:
                self._check_roster_token(conn, event_id, expected_roster_token)
            first = self._player(conn, event_id, first_member_id)
            second = self._player(conn, event_id, second_member_id)
            if conn.execute("SELECT 1 FROM competition_teams WHERE event_id=? AND captain_id IN (?,?)", (event_id, first_member_id, second_member_id)).fetchone():
                raise ValueError("팀장은 자신의 팀에 고정됩니다. 일반 선수끼리 교환해 주세요.")
            if first["team_id"] == second["team_id"] or first["role"] != second["role"]:
                raise ValueError("다른 팀의 같은 포지션 선수끼리 교환해 주세요.")
            conn.execute("UPDATE competition_players SET team_id=? WHERE id=?", (second["team_id"], first["id"]))
            conn.execute("UPDATE competition_players SET team_id=? WHERE id=?", (first["team_id"], second["id"]))
            summary = f"{first['riot_id']} ↔ {second['riot_id']}: {reason}"
            if guarded:
                result = {"event_id": event_id, "first_member_id": first_member_id,
                          "second_member_id": second_member_id, "first_team_id": second["team_id"],
                          "second_team_id": first["team_id"], "replayed": False}
                self._audit(conn, event_id, actor, "SWAP", json.dumps({"request_id": request_id,
                    "payload_hash": payload_hash, "summary": summary, "result": result}, ensure_ascii=False))
                return result
            self._audit(conn, event_id, actor, "SWAP", summary)

    def _auction_open(self, conn, event_id):
        self._guard_live(conn, event_id)
        if self._event(conn, event_id)["status"] != "AUCTION":
            raise ValueError("진행 중인 경매에서만 변경할 수 있습니다.")

    @staticmethod
    def _guard_live(conn, event_id, allow_completed=False):
        if _table_exists(conn, "live_sessions"):
            row = conn.execute("SELECT status FROM live_sessions WHERE event_id=? LIMIT 1", (event_id,)).fetchone()
            if row and not (allow_completed and row["status"] == "COMPLETED"):
                raise ValueError("실시간 경매가 연결된 대회입니다. 실시간 경매 화면에서 진행해 주세요.")

    def draw_player(self, token, event_id):
        with self.service.transaction() as conn:
            self._authorize(conn, token, event_id)
            self._auction_open(conn, event_id)
            event = self._event(conn, event_id)
            if event["current_player_id"]:
                return dict(conn.execute("SELECT * FROM competition_players WHERE event_id=? AND member_id=?", (event_id, event["current_player_id"])).fetchone())
            pool = conn.execute("SELECT * FROM competition_players WHERE event_id=? AND state='AVAILABLE' ORDER BY id", (event_id,)).fetchall()
            if not pool:
                raise ValueError("추첨할 선수가 없습니다. 유찰 선수와 팀 명단을 확인해 주세요.")
            player = dict(random.SystemRandom().choice(pool))
            conn.execute("UPDATE competition_events SET current_player_id=? WHERE id=?", (player["member_id"], event_id))
            return player

    def mark_unsold(self, token, event_id, member_id):
        with self.service.transaction() as conn:
            actor = self._authorize(conn, token, event_id)
            self._auction_open(conn, event_id)
            player = self._player(conn, event_id, member_id)
            if player["team_id"] is not None:
                raise ValueError("이미 배정된 선수를 유찰 처리할 수 없습니다.")
            conn.execute("UPDATE competition_players SET state='UNSOLD' WHERE id=?", (player["id"],))
            conn.execute("UPDATE competition_events SET current_player_id=NULL WHERE id=? AND current_player_id=?", (event_id, member_id))
            self._audit(conn, event_id, actor, "UNSOLD", str(member_id))

    def _player(self, conn, event_id, member_id):
        row = conn.execute("SELECT * FROM competition_players WHERE event_id=? AND member_id=?", (event_id, member_id)).fetchone()
        if row is None:
            raise ValueError("대회 참가자를 찾을 수 없습니다.")
        return dict(row)

    def _team(self, conn, event_id, team_id):
        row = conn.execute("SELECT * FROM competition_teams WHERE event_id=? AND id=?", (event_id, team_id)).fetchone()
        if row is None:
            raise ValueError("해당 대회의 팀을 선택해 주세요.")
        return dict(row)

    def _assign(self, conn, event_id, member_id, team_id, amount):
        team = self._team(conn, event_id, team_id)
        player = self._player(conn, event_id, member_id)
        count, spent = conn.execute("SELECT COUNT(*),COALESCE(SUM(price),0) FROM competition_players WHERE team_id=? AND member_id<>?", (team_id, member_id)).fetchone()
        statements = self._assignment_statements(team, player, amount, count, spent)
        batch = getattr(conn, "execute_batch", None)
        if callable(batch):
            batch(statements)
        else:
            for query, parameters in statements:
                conn.execute(query, parameters)

    @staticmethod
    def _assignment_statements(team, player, amount, count, spent):
        """Validate rows read under the caller's writer lock, without re-reading.

        Both ordinary assignment and live settlement keep the same rules. The
        caller must supply the destination totals excluding this player, which
        also preserves reassignment to a player's existing team.
        """
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise ValueError("낙찰가는 0 이상의 정수로 입력해 주세요.")
        if player["participation_status"] != "SELECTED":
            raise ValueError("대회 명단에서 제외된 선수는 배정할 수 없습니다.")
        if team["event_id"] != player["event_id"]:
            raise ValueError("해당 대회의 팀을 선택해 주세요.")
        if count >= 5:
            raise ValueError("팀 정원 5명을 초과할 수 없습니다.")
        if amount > team["budget"] - spent:
            raise ValueError("팀의 남은 예산을 초과했습니다.")
        return [
            ("UPDATE competition_players SET team_id=?,price=?,state='ASSIGNED' WHERE event_id=? AND member_id=?", (team["id"], amount, player["event_id"], player["member_id"])),
            ("UPDATE competition_events SET current_player_id=NULL WHERE id=? AND current_player_id=?", (player["event_id"], player["member_id"])),
        ]

    def bid(self, token, event_id, member_id, team_id, amount):
        with self.service.transaction() as conn:
            actor = self._authorize(conn, token, event_id)
            self._auction_open(conn, event_id)
            if self._player(conn, event_id, member_id)["team_id"] is not None:
                raise ValueError("이미 낙찰된 선수입니다.")
            self._assign(conn, event_id, member_id, team_id, amount)
            self._audit(conn, event_id, actor, "BID", f"선수 {member_id}, 팀 {team_id}, {amount} 포인트")

    def move_player(self, token, event_id, member_id, target_team_id, amount, reason):
        if not str(reason).strip():
            raise ValueError("재배정 사유를 입력해 주세요.")
        with self.service.transaction() as conn:
            actor = self._authorize(conn, token, event_id)
            self._auction_open(conn, event_id)
            player = self._player(conn, event_id, member_id)
            if conn.execute("SELECT 1 FROM competition_teams WHERE event_id=? AND captain_id=?", (event_id, member_id)).fetchone():
                raise ValueError("팀장은 경매 도중 이동할 수 없습니다.")
            self._assign(conn, event_id, member_id, target_team_id, amount)
            self._audit(conn, event_id, actor, "MOVE", f"선수 {member_id}: {player['team_id']}팀/{player['price']} 환불 → {target_team_id}팀/{amount}. {reason.strip()}")

    def set_player_role(self, token, event_id, member_id, role):
        if role not in ROLES:
            raise ValueError("올바른 포지션을 선택해 주세요.")
        with self.service.transaction() as conn:
            actor = self._authorize(conn, token, event_id)
            self._auction_open(conn, event_id)
            self._player(conn, event_id, member_id)
            conn.execute("UPDATE competition_players SET role=? WHERE event_id=? AND member_id=?", (role, event_id, member_id))
            self._audit(conn, event_id, actor, "ROLE", f"선수 {member_id}: {role}")

    def finalize_auction(self, token, event_id):
        with self.service.transaction() as conn:
            actor = self._authorize(conn, token, event_id)
            self._auction_open(conn, event_id)
            if conn.execute("SELECT 1 FROM competition_players WHERE event_id=? AND participation_status='SELECTED' AND team_id IS NULL", (event_id,)).fetchone():
                raise ValueError("미배정·유찰 선수를 모두 배정한 뒤 경매를 확정해 주세요.")
            teams = conn.execute("SELECT * FROM competition_teams WHERE event_id=? ORDER BY id", (event_id,)).fetchall()
            for team in teams:
                players = conn.execute("SELECT role FROM competition_players WHERE team_id=?", (team["id"],)).fetchall()
                if len(players) != 5 or {p["role"] for p in players} != set(ROLES):
                    raise ValueError(f"{team['name']}: 5명과 TOP/JG/MID/AD/SUP 배정을 확인해 주세요.")
            event = self._event(conn, event_id)
            self._make_schedule(conn, event_id, [t["id"] for t in teams], event["format"])
            conn.execute("UPDATE competition_events SET status='READY',current_player_id=NULL WHERE id=?", (event_id,))
            self._audit(conn, event_id, actor, "AUCTION_FINALIZE", "경매 명단·예산 확정")

    def _game(self, conn, event_id, round_num, position, a=None, b=None, group="", stage="MAIN", source_a=None, source_b=None):
        if self.service.is_postgres:
            return conn.execute("INSERT INTO competition_games(event_id,round,position,group_key,stage,team_a,team_b,source_a,source_b) VALUES(?,?,?,?,?,?,?,?,?)",
                                (event_id, round_num, position, group, stage, a, b, source_a, source_b)).lastrowid
        # Legacy tables lack AUTOINCREMENT. A removed tiebreak still owns its
        # fixture ID in the archive and Core request keys, so never reuse it.
        highest = conn.execute("SELECT COALESCE(MAX(id),0) FROM competition_games").fetchone()[0]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('competition_game_archives','sqlite_sequence')")}
        if "competition_game_archives" in tables:
            highest = max(highest, conn.execute("SELECT COALESCE(MAX(fixture_id),0) FROM competition_game_archives").fetchone()[0])
        if "sqlite_sequence" in tables:
            highest = max(highest, conn.execute("SELECT COALESCE(MAX(seq),0) FROM sqlite_sequence WHERE name='competition_games'").fetchone()[0])
        return conn.execute("INSERT INTO competition_games(id,event_id,round,position,group_key,stage,team_a,team_b,source_a,source_b) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (highest + 1, event_id, round_num, position, group, stage, a, b, source_a, source_b)).lastrowid

    def _round_robin(self, conn, event_id, teams, group="", stage="MAIN", round_offset=0):
        rotation = list(teams)
        if len(rotation) % 2:
            rotation.append(None)
        for r in range(len(rotation) - 1):
            for p in range(len(rotation) // 2):
                a, b = rotation[p], rotation[-p - 1]
                if a is not None and b is not None:
                    self._game(conn, event_id, r + 1 + round_offset, p + 1, a, b, group, stage)
            rotation = [rotation[0], rotation[-1]] + rotation[1:-1]

    def _make_schedule(self, conn, event_id, teams, format_name):
        if conn.execute("SELECT 1 FROM competition_games WHERE event_id=?", (event_id,)).fetchone():
            raise ValueError("대진표가 이미 생성되어 있습니다.")
        if format_name == "RANKING":
            if len(teams) != 4:
                raise ValueError("순위 결정전은 4팀에서만 진행할 수 있습니다.")
            teams = list(teams)
            random.SystemRandom().shuffle(teams)
            first = self._game(conn, event_id, 1, 1, teams[0], teams[1])
            second = self._game(conn, event_id, 1, 2, teams[2], teams[3])
            self._game(conn, event_id, 2, 1, stage="FINAL", source_a=first, source_b=second)
            third = self._game(conn, event_id, 2, 2, stage="THIRD_PLACE", source_a=first, source_b=second)
            conn.execute("UPDATE competition_games SET source_a_result='LOSER',source_b_result='LOSER' WHERE id=?", (third,))
        elif format_name == "SINGLE":
            self._game(conn, event_id, 1, 1, *teams)
        elif format_name == "LEAGUE":
            self._round_robin(conn, event_id, teams)
        elif format_name == "GROUP_STAGE":
            teams = list(teams)
            random.SystemRandom().shuffle(teams)
            half = len(teams) // 2
            self._round_robin(conn, event_id, teams[:half], "A")
            self._round_robin(conn, event_id, teams[half:], "B")
            self._game(conn, event_id, 100, 1, stage="FINAL")
        elif format_name == "TOURNAMENT":
            teams = list(teams)
            random.SystemRandom().shuffle(teams)
            size = 1 << (len(teams) - 1).bit_length()
            bye_count = size - len(teams)
            pairs = [(teams[i], None) for i in range(bye_count)]
            rest = teams[bye_count:]
            pairs.extend(zip(rest[::2], rest[1::2]))
            # Distribute the two six-team byes to opposite semifinals.
            if len(teams) == 6:
                pairs = [pairs[0], pairs[2], pairs[1], pairs[3]]
            ids = []
            for position, (a, b) in enumerate(pairs, 1):
                game_id = self._game(conn, event_id, 1, position, a, b)
                if b is None:
                    conn.execute("UPDATE competition_games SET status='BYE',winner_team_id=? WHERE id=?", (a, game_id))
                ids.append(game_id)
            round_num = 2
            while len(ids) > 1:
                ids = [self._game(conn, event_id, round_num, i // 2 + 1, source_a=ids[i], source_b=ids[i + 1])
                       for i in range(0, len(ids), 2)]
                round_num += 1
            self._propagate(conn, event_id)

    def _descendants(self, conn, event_id, game_id):
        result = []
        pending = [game_id]
        while pending:
            current = pending.pop()
            rows = conn.execute("SELECT * FROM competition_games WHERE event_id=? AND (source_a=? OR source_b=?)", (event_id, current, current)).fetchall()
            for row in rows:
                result.append(dict(row))
                pending.append(row["id"])
        return result

    def _propagate(self, conn, event_id):
        games = conn.execute("SELECT * FROM competition_games WHERE event_id=? ORDER BY round,position", (event_id,)).fetchall()
        by_id = {g["id"]: g for g in games}
        def source_team(source_id, result):
            source = by_id.get(source_id)
            if source is None or source["winner_team_id"] is None:
                return None
            if result == "LOSER":
                return source["team_b"] if source["winner_team_id"] == source["team_a"] else source["team_a"]
            return source["winner_team_id"]
        for game in games:
            if game["status"] != "PENDING":
                continue
            a = source_team(game["source_a"], game["source_a_result"]) if game["source_a"] else game["team_a"]
            b = source_team(game["source_b"], game["source_b_result"]) if game["source_b"] else game["team_b"]
            conn.execute("UPDATE competition_games SET team_a=?,team_b=? WHERE id=?", (a, b, game["id"]))

    @staticmethod
    def _standings(conn, event_id, group=""):
        games = [dict(r) for r in conn.execute("SELECT * FROM competition_games WHERE event_id=? AND group_key=? AND stage IN ('MAIN','TIEBREAK') ORDER BY round,id", (event_id, group))]
        team_ids = {g[k] for g in games for k in ("team_a", "team_b") if g[k] is not None}
        names = {r["id"]: r["name"] for r in conn.execute("SELECT id,name FROM competition_teams WHERE event_id=?", (event_id,))}
        stats = {t: {"team_id": t, "team_name": names[t], "group": group, "played": 0, "wins": 0, "losses": 0, "points": 0, "h2h": 0, "tiebreak_wins": 0} for t in team_ids}
        for game in games:
            if game["status"] != "COMPLETED" or game["stage"] != "MAIN":
                continue
            for team in (game["team_a"], game["team_b"]):
                stats[team]["played"] += 1
                if team == game["winner_team_id"]:
                    stats[team]["wins"] += 1
                    stats[team]["points"] += 3
                else:
                    stats[team]["losses"] += 1
        for team, stat in stats.items():
            tied = {other for other in stats if stats[other]["points"] == stat["points"]}
            stat["h2h"] = sum(3 for g in games if g["stage"] == "MAIN" and g["status"] == "COMPLETED" and g["winner_team_id"] == team and g["team_a"] in tied and g["team_b"] in tied)
        extras = [g for g in games if g["stage"] == "TIEBREAK"]
        if extras and all(g["status"] == "COMPLETED" for g in extras):
            latest_round = max(g["round"] for g in extras)
            # Each tiebreak batch has its own stage round (one round robin).
            batch = latest_round // 1000
            for game in extras:
                if game["round"] // 1000 == batch:
                    stats[game["winner_team_id"]]["tiebreak_wins"] += 1
        rows = sorted(stats.values(), key=lambda s: (-s["points"], -s["h2h"], -s["tiebreak_wins"], s["team_id"]))
        previous, rank = None, 0
        for i, row in enumerate(rows, 1):
            key = (row["points"], row["h2h"], row["tiebreak_wins"])
            if key != previous:
                rank = i
            row["rank"] = rank
            previous = key
        return rows

    @classmethod
    def _group_winner(cls, conn, event_id, group=""):
        if conn.execute("SELECT 1 FROM competition_games WHERE event_id=? AND group_key=? AND stage IN ('MAIN','TIEBREAK') AND status='PENDING'", (event_id, group)).fetchone():
            return None
        rows = cls._standings(conn, event_id, group)
        leaders = [row for row in rows if row["rank"] == 1]
        return leaders[0]["team_id"] if len(leaders) == 1 else None

    @classmethod
    def resolve_award_winner(cls, conn, event_id):
        """Read and validate a finished auction's winner without opening a connection."""
        event = cls._event(conn, event_id)
        if event["kind"] != "AUCTION" or event["status"] not in ("PLAYING", "COMPLETED"):
            raise ValueError("실제 경기가 진행된 경매 대회만 우승 보상을 받을 수 있습니다.")
        games = conn.execute("SELECT * FROM competition_games WHERE event_id=?", (event_id,)).fetchall()
        if not games or not any(g["status"] == "COMPLETED" for g in games) or any(g["status"] not in ("COMPLETED", "BYE") for g in games):
            raise ValueError("남은 실제 경기를 모두 완료한 뒤 우승 보상을 확정해 주세요.")
        if event["format"] == "LEAGUE":
            winner = cls._group_winner(conn, event_id)
        elif event["format"] in ("GROUP_STAGE", "RANKING"):
            finals = [g for g in games if g["stage"] == "FINAL"]
            winner = finals[0]["winner_team_id"] if len(finals) == 1 else None
        else:
            winner = max(games, key=lambda g: (g["round"], g["position"]))["winner_team_id"]
        if winner is None:
            raise ValueError("우승팀이 확정되지 않았습니다. 동률 추가 경기를 확인해 주세요.")
        if event["status"] == "COMPLETED" and event["winner_team_id"] != winner:
            raise ValueError("대회 우승팀과 실제 경기 결과가 일치하지 않습니다.")
        return winner

    def _refresh_group_final(self, conn, event_id):
        event = self._event(conn, event_id)
        if event["format"] == "GROUP_STAGE":
            a = self._group_winner(conn, event_id, "A")
            b = self._group_winner(conn, event_id, "B")
            conn.execute("UPDATE competition_games SET team_a=?,team_b=? WHERE event_id=? AND stage='FINAL' AND status='PENDING'", (a, b, event_id))

    def create_tiebreakers(self, token, event_id, group_key=None):
        group = group_key or ""
        with self.service.transaction() as conn:
            actor = self._authorize(conn, token, event_id)
            event = self._event(conn, event_id)
            if event["status"] not in ("READY", "PLAYING") or event["format"] not in ("LEAGUE", "GROUP_STAGE"):
                raise ValueError("진행 중인 리그의 동률만 추가 경기로 결정할 수 있습니다.")
            if event["format"] == "GROUP_STAGE" and group not in ("A", "B"):
                raise ValueError("추가 경기를 진행할 조를 선택해 주세요.")
            if conn.execute("SELECT 1 FROM competition_games WHERE event_id=? AND group_key=? AND stage IN ('MAIN','TIEBREAK') AND status='PENDING'", (event_id, group)).fetchone():
                raise ValueError("해당 리그의 남은 경기를 먼저 완료해 주세요.")
            leaders = [s["team_id"] for s in self._standings(conn, event_id, group) if s["rank"] == 1]
            if len(leaders) < 2:
                raise ValueError("1위 동률이 없습니다.")
            last = conn.execute("SELECT COALESCE(MAX(round),0) FROM competition_games WHERE event_id=? AND group_key=? AND stage='TIEBREAK'", (event_id, group)).fetchone()[0]
            offset = (last // 1000 + 1) * 1000
            random.SystemRandom().shuffle(leaders)
            self._round_robin(conn, event_id, leaders, group, "TIEBREAK", offset)
            self._refresh_group_final(conn, event_id)
            self._audit(conn, event_id, actor, "TIEBREAK", f"{group or '전체'} 리그 동률팀 추가 경기")

    def record_result(self, token, event_id, game_id, winner_team_id, request_key=None, reason="", expected_roster_token=None):
        with self.service.transaction() as conn:
            actor = self._authorize(conn, token, event_id)
            event = self._event(conn, event_id)
            if event["status"] not in ("READY", "PLAYING"):
                raise ValueError("진행 중인 대회에서 결과를 입력해 주세요. 종료 대회 수정은 별도 분쟁 처리 대상입니다.")
            row = conn.execute("SELECT * FROM competition_games WHERE event_id=? AND id=?", (event_id, game_id)).fetchone()
            if row is None:
                raise ValueError("경기를 찾을 수 없습니다.")
            game = dict(row)
            if game["status"] == "BYE":
                raise ValueError("부전승에는 실경기 결과를 입력하지 않습니다.")
            if game["team_a"] is None or game["team_b"] is None:
                raise ValueError("앞선 경기 또는 조별리그 결과를 먼저 확정해 주세요.")
            if winner_team_id not in (game["team_a"], game["team_b"]):
                raise ValueError("해당 경기의 참가 팀 중 승리팀을 선택해 주세요.")
            # A retried receipt does not write anything and remains valid after
            # other fixtures advance. New writes must use the reviewed roster.
            if game["status"] == "COMPLETED" and game["winner_team_id"] == winner_team_id:
                return game["core_game_id"]
            self._check_roster_token(conn, event_id, expected_roster_token)
            if game["status"] == "COMPLETED":
                self.service.require_admin(conn, token)
                if event["kind"] == "AUCTION" and conn.execute("SELECT 1 FROM award_batches WHERE event_id=?", (str(event["id"]),)).fetchone():
                    raise ValueError("이미 우승 보상이 지급된 대회입니다. 결과와 보상을 변경할 수 없습니다.")
                if not str(reason).strip():
                    raise ValueError("결과 정정 사유를 입력해 주세요.")
                descendants = self._descendants(conn, event_id, game_id)
                if event["format"] == "GROUP_STAGE" and game["stage"] != "FINAL":
                    descendants += [dict(r) for r in conn.execute("SELECT * FROM competition_games WHERE event_id=? AND stage='FINAL'", (event_id,))]
                if any(g["status"] == "COMPLETED" for g in descendants):
                    raise ValueError("이 결과를 사용한 후속 경기가 이미 끝났습니다. 자동 변경할 수 없으며 운영진의 분쟁 처리가 필요합니다.")
                if game["stage"] == "MAIN" and conn.execute("SELECT 1 FROM competition_games WHERE event_id=? AND group_key=? AND stage='TIEBREAK'", (event_id, game["group_key"])).fetchone():
                    raise ValueError("이미 추가 경기 대진이 생성되었습니다. 원경기 정정은 운영진의 분쟁 처리가 필요합니다.")
                if game["stage"] == "TIEBREAK" and conn.execute("SELECT 1 FROM competition_games WHERE event_id=? AND group_key=? AND stage='TIEBREAK' AND round>=?", (event_id, game["group_key"], (game["round"] // 1000 + 1) * 1000)).fetchone():
                    guidance = "경기 결과 정정 영향 미리보기에서 후속 경기를 확인해 주세요." if event["kind"] == "AUCTION" else "일반내전의 후속 경기 기록을 보존한 상태에서 운영진의 분쟁 처리가 필요합니다."
                    raise ValueError(f"이미 후속 동률 추가 경기 대진이 생성되었습니다. {guidance}")
                self.service.correct_game(token, game["core_game_id"], "A" if winner_team_id == game["team_a"] else "B", reason.strip(), conn=conn)
                core_game_id = game["core_game_id"]
            else:
                rosters = []
                for team_id in (game["team_a"], game["team_b"]):
                    rosters.append([{"member_id": r["member_id"], "role": r["role"]} for r in conn.execute("SELECT member_id,role FROM competition_players WHERE event_id=? AND team_id=? ORDER BY id", (event_id, team_id))])
                default_key = request_key or f"competition:{event_id}:game:{game_id}"
                if game["attempt"] > 1:
                    default_key += f":attempt:{game['attempt']}"
                core_game_id = self.service.record_game(token, default_key,
                    rosters[0], rosters[1], "A" if winner_team_id == game["team_a"] else "B",
                    tournament_id=event_id, kind=event["kind"], notes=f"{event['title']} 경기 {game_id}", conn=conn,
                    policy_id=json.loads(event["policy_snapshot"])["score_policy"]["id"], fixture_id=game_id)
                if conn.execute("SELECT status FROM games WHERE id=?", (core_game_id,)).fetchone()["status"] != "CONFIRMED":
                    raise ValueError("무효 처리된 경기 요청 번호는 다시 사용할 수 없습니다.")
            conn.execute("UPDATE competition_games SET winner_team_id=?,status='COMPLETED',core_game_id=? WHERE id=?", (winner_team_id, core_game_id, game_id))
            conn.execute("UPDATE competition_events SET status='PLAYING' WHERE id=?", (event_id,))
            self._propagate(conn, event_id)
            self._refresh_group_final(conn, event_id)
            self._audit(conn, event_id, actor, "RESULT", f"경기 {game_id}, 승리팀 {winner_team_id}" + (f", 정정: {reason.strip()}" if reason else ""))
            return core_game_id

    def finalize_event(self, token, event_id):
        with self.service.transaction() as conn:
            actor = self._authorize(conn, token, event_id)
            event = self._event(conn, event_id)
            if event["status"] == "COMPLETED":
                return event["winner_team_id"]
            if event["status"] != "PLAYING":
                raise ValueError("실제 경기가 완료된 대회만 종료할 수 있습니다.")
            games = conn.execute("SELECT * FROM competition_games WHERE event_id=?", (event_id,)).fetchall()
            if not games or any(g["status"] == "PENDING" for g in games):
                raise ValueError("남은 경기를 모두 완료한 뒤 대회를 종료해 주세요.")
            if event["format"] == "LEAGUE":
                winner = self._group_winner(conn, event_id)
            else:
                final = max(games, key=lambda g: (g["round"], g["position"])) if event["format"] not in ("GROUP_STAGE", "RANKING") else next(g for g in games if g["stage"] == "FINAL")
                winner = final["winner_team_id"]
            if winner is None:
                raise ValueError("1위가 동률입니다. 추가 경기로 우승팀을 결정해 주세요.")
            if event["kind"] == "AUCTION":
                winner = self.resolve_award_winner(conn, event_id)
                team_count = conn.execute("SELECT COUNT(*) FROM competition_teams WHERE event_id=?", (event_id,)).fetchone()[0]
                winner_ids = [r[0] for r in conn.execute("SELECT member_id FROM competition_players WHERE event_id=? AND team_id=?", (event_id, winner))]
                self.service.award_tournament(token, event_id, winner_ids, team_count, f"competition:{event_id}:award", conn=conn)
            conn.execute("UPDATE competition_events SET status='COMPLETED',winner_team_id=? WHERE id=?", (winner, event_id))
            self._audit(conn, event_id, actor, "FINALIZE", f"우승팀 {winner}")
            return winner

    def close_unfinished(self, token, event_id, reason):
        """End an interrupted event without erasing played games or awarding a winner."""
        reason = str(reason).strip()
        if not reason or len(reason) > 1000:
            raise ValueError("중단 종료 사유를 1~1,000자로 입력해 주세요.")
        with self.service.transaction() as conn:
            actor = self.service.require_admin(conn, token)
            event = self._event(conn, event_id)
            if event["kind"] not in ("NORMAL", "AUCTION") or event["status"] != "PLAYING":
                raise ValueError("실제 경기가 진행 중인 일반내전·경매만 중단 종료할 수 있습니다.")
            if conn.execute("SELECT 1 FROM award_batches WHERE event_id=?", (str(event_id),)).fetchone():
                raise ValueError("이미 우승 보상이 지급된 경매는 중단 종료할 수 없습니다.")
            games = [dict(row) for row in conn.execute("SELECT * FROM competition_games WHERE event_id=? ORDER BY id", (event_id,))]
            core_games = [dict(row) for row in conn.execute("SELECT * FROM games WHERE tournament_id=? AND kind=? ORDER BY id", (str(event_id), event["kind"]))]
            confirmed = {game["id"] for game in core_games if game["status"] == "CONFIRMED"}
            if not any(game["status"] == "COMPLETED" and game["core_game_id"] in confirmed for game in games):
                raise ValueError("확정된 실제 경기가 없으면 기존 일반내전·경매 취소를 이용해 주세요.")
            snapshot = {"reason": reason, "event": event, "fixtures": games, "core_games": core_games,
                        "teams": [dict(row) for row in conn.execute("SELECT * FROM competition_teams WHERE event_id=? ORDER BY id", (event_id,))],
                        "players": [dict(row) for row in conn.execute("SELECT * FROM competition_players WHERE event_id=? ORDER BY id", (event_id,))]}
            self._audit(conn, event_id, actor, "CLOSE_UNFINISHED", json.dumps(snapshot, ensure_ascii=False))
            conn.execute("UPDATE competition_events SET status='CANCELLED',winner_team_id=NULL,current_player_id=NULL WHERE id=?", (event_id,))

    def cancel_event(self, token, event_id, reason):
        if not str(reason).strip():
            raise ValueError("취소 사유를 입력해 주세요.")
        with self.service.transaction() as conn:
            actor = self._authorize(conn, token, event_id)
            event = self._event(conn, event_id)
            if event["status"] in ("COMPLETED", "CANCELLED"):
                raise ValueError("이미 종료 또는 취소된 대회입니다.")
            if conn.execute("SELECT 1 FROM competition_games WHERE event_id=? AND status='COMPLETED'", (event_id,)).fetchone():
                raise ValueError("이미 실경기 결과가 있습니다. 기록을 보존한 상태에서 운영진의 분쟁 처리가 필요합니다.")
            conn.execute("UPDATE competition_events SET status='CANCELLED' WHERE id=?", (event_id,))
            self._audit(conn, event_id, actor, "CANCEL", reason.strip())

    def list_events(self):
        with closing(self.service.connect()) as conn:
            return [dict(r) for r in conn.execute("SELECT e.*,a.display_name AS created_by_name,(SELECT COUNT(*) FROM competition_players p WHERE p.event_id=e.id AND p.participation_status='SELECTED') AS participant_count FROM competition_events e LEFT JOIN accounts a ON a.id=e.created_by ORDER BY e.id DESC")]

    def game_labels(self):
        """Return real competition team names for the shared Core game history."""
        with closing(self.service.connect()) as conn:
            labels = {}
            if _table_exists(conn, "competition_game_archives"):
                for row in conn.execute("SELECT a.*,e.title FROM competition_game_archives a JOIN competition_events e ON e.id=a.event_id WHERE a.core_game_id IS NOT NULL ORDER BY a.id"):
                    snapshot = json.loads(row["snapshot"])
                    labels[row["core_game_id"]] = {"core_game_id": row["core_game_id"], "event_id": row["event_id"], "title": row["title"],
                        "team_a_name": snapshot["team_a_name"], "team_b_name": snapshot["team_b_name"], "winner_name": snapshot["winner_name"]}
            rows = conn.execute("""SELECT g.core_game_id,g.event_id,e.title,
                a.name AS team_a_name,b.name AS team_b_name,w.name AS winner_name
                FROM competition_games g JOIN competition_events e ON e.id=g.event_id
                LEFT JOIN competition_teams a ON a.id=g.team_a
                LEFT JOIN competition_teams b ON b.id=g.team_b
                LEFT JOIN competition_teams w ON w.id=g.winner_team_id
                WHERE g.core_game_id IS NOT NULL""")
            labels.update({row["core_game_id"]: dict(row) for row in rows})
            return labels

    def get_event(self, event_id):
        with closing(self.service.connect()) as conn:
            conn.execute("BEGIN")
            event = self._event(conn, event_id)
            event["policy_snapshot"] = json.loads(event["policy_snapshot"])
            participants = [dict(r) for r in conn.execute("SELECT * FROM competition_players WHERE event_id=? ORDER BY id", (event_id,))]
            players = [p for p in participants if p["participation_status"] == "SELECTED"]
            teams = [dict(r) for r in conn.execute("SELECT * FROM competition_teams WHERE event_id=? ORDER BY id", (event_id,))]
            for team in teams:
                team["players"] = [p for p in players if p["team_id"] == team["id"]]
                team["remaining"] = team["budget"] - sum(p["price"] for p in team["players"])
                team["total_score"] = sum(p["score"] for p in team["players"])
            names = {t["id"]: t["name"] for t in teams}
            games = [dict(r) for r in conn.execute("SELECT * FROM competition_games WHERE event_id=? ORDER BY round,group_key,position,id", (event_id,))]
            for game in games:
                game["team_a_name"] = names.get(game["team_a"], "미정")
                game["team_b_name"] = names.get(game["team_b"], "미정")
                game["winner_name"] = names.get(game["winner_team_id"], "")
            event["teams"] = teams
            event["pool"] = [p for p in players if p["team_id"] is None]
            event["players"] = players
            event["participants"] = participants
            event["excluded_players"] = [p for p in participants if p["participation_status"] == "EXCLUDED"]
            event["participant_count"] = len(players)
            event["games"] = games
            event["archived_games"] = []
            if _table_exists(conn, "competition_game_archives"):
                event["archived_games"] = [{**dict(row), "snapshot": json.loads(row["snapshot"])} for row in conn.execute("SELECT * FROM competition_game_archives WHERE event_id=? ORDER BY id DESC", (event_id,))]
            event["standings"] = []
            event["final_rankings"] = []
            if event["format"] == "RANKING":
                for stage, first_rank in (("FINAL", 1), ("THIRD_PLACE", 3)):
                    match = next((g for g in games if g["stage"] == stage and g["status"] == "COMPLETED"), None)
                    if match:
                        loser = match["team_b"] if match["winner_team_id"] == match["team_a"] else match["team_a"]
                        event["final_rankings"].extend([{"rank": first_rank, "team_id": match["winner_team_id"], "team_name": names[match["winner_team_id"]]},
                            {"rank": first_rank + 1, "team_id": loser, "team_name": names[loser]}])
            if event["format"] in ("LEAGUE", "GROUP_STAGE") and games:
                for group in (("A", "B") if event["format"] == "GROUP_STAGE" else ("",)):
                    event["standings"].extend(self._standings(conn, event_id, group))
            event["audit"] = [dict(r) for r in conn.execute("SELECT * FROM competition_audit WHERE event_id=? ORDER BY id DESC LIMIT 30", (event_id,))]
            event["roster_token"] = self._roster_digest(event, participants, teams,
                sorted(games, key=lambda game: game["id"]), event["audit"][0]["id"] if event["audit"] else 0)
            return event
