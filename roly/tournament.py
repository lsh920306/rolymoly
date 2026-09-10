"""One persistent tournament from recruitment through its final result.

Preparation changes use the same SQLite transaction, ownership checks and audit
trail as Competition. Participant warnings and exclusions preserve member scores.
"""
from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta, timezone

from roly.competition import Competition, ROLES, auction_budget, _now, _table_exists
from roly.core import integer


BUILD_MODES = ("BALANCE", "MANUAL", "AUCTION")
PREPARATION_STATES = ("DRAFT", "RECRUITING", "CAPTAIN_SELECTION", "TEAM_BUILDING", "AUCTION_READY", "BRACKET_SETUP", "READY")


def allowed_team_counts(build_mode="BALANCE"):
    if build_mode not in BUILD_MODES:
        raise ValueError("팀 구성 방식은 자동 밸런스·직접 배정·경매 중에서 선택해 주세요.")
    return (4, 6, 8) if build_mode == "AUCTION" else (2, 4)


def allowed_formats(team_count, build_mode=None):
    if build_mode is not None and team_count not in allowed_team_counts(build_mode):
        raise ValueError("선택한 팀 구성 방식에서 지원하지 않는 팀 수입니다.")
    if team_count == 2:
        return ("SINGLE",)
    if team_count == 4:
        return ("LEAGUE", "TOURNAMENT", "RANKING")
    if team_count in (6, 8):
        return ("LEAGUE", "TOURNAMENT", "GROUP_STAGE")
    raise ValueError("대회 팀 수를 확인해 주세요.")


def _start(value):
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise ValueError("예정 시작 일시를 올바르게 입력해 주세요.") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=9)))
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _details(title, starts_at, description):
    title, description = str(title).strip(), str(description).strip()
    if not title or len(title) > 100:
        raise ValueError("대회 이름은 1~100자로 입력해 주세요.")
    if len(description) > 2000:
        raise ValueError("대회 설명은 2,000자 이내로 입력해 주세요.")
    return title, _start(starts_at), description


def _balance_captains(players, teams, count):
    """Pure search over confirmed snapshots; retain order and tie-breaking."""
    captain_team = {team["captain_id"]: index for index, team in enumerate(teams)}
    choices = []
    for role in ROLES:
        bucket = [player for player in players if player["role"] == role]
        if len(bucket) != count:
            raise ValueError(f"각 포지션에 {count}명씩 배정해 주세요.")
        choices.append([order for order in itertools.permutations(bucket) if all(
            player["member_id"] not in captain_team or captain_team[player["member_id"]] == index
            for index, player in enumerate(order))])
    if any(not options for options in choices):
        raise ValueError("팀장 고정과 포지션 조건을 동시에 만족하는 배정이 없습니다.")
    best_key, best = None, None
    for columns in itertools.product(*choices):
        totals = [sum(column[index]["score"] for column in columns) for index in range(count)]
        key = (max(totals) - min(totals), sum(total * total for total in totals))
        if best_key is None or key < best_key:
            best_key, best = key, columns
        if key[0] == 0:
            break
    return best, best_key


class TournamentService:
    def __init__(self, core, competition=None):
        self.core = core
        self.competition = competition or Competition(core)

    def get_event(self, event_id):
        return self.competition.get_event(event_id)

    def list_events(self):
        return self.competition.list_events()

    def _edit(self, conn, token, event_id, states):
        actor = self.competition._authorize(conn, token, event_id)
        event = self.competition._event(conn, event_id)
        if event["status"] not in states:
            raise ValueError("현재 대회 단계에서는 이 작업을 할 수 없습니다.")
        return actor, event

    def _audit(self, conn, event_id, actor, action, detail):
        self.competition._audit(conn, event_id, actor, action, detail)

    def _transition(self, conn, event, actor, status, detail):
        conn.execute("UPDATE competition_events SET status=? WHERE id=?", (status, event["id"]))
        self._audit(conn, event["id"], actor, "STAGE", f"{event['status']} → {status}: {detail}")

    def _players(self, conn, event_id):
        return [dict(r) for r in conn.execute("SELECT * FROM competition_players WHERE event_id=? AND participation_status='SELECTED' ORDER BY id", (event_id,))]

    def _selected(self, conn, event_id, member_id):
        player = self.competition._player(conn, event_id, member_id)
        if player["participation_status"] != "SELECTED":
            raise ValueError("현재 대회 참가 명단에 없는 선수입니다.")
        return player

    def _format(self, event, format_name):
        if format_name not in allowed_formats(event["team_count"], event["build_mode"]):
            raise ValueError("현재 팀 수에서 사용할 수 없는 대회 방식입니다.")
        return format_name

    def create(self, token, title, starts_at, description="", build_mode="BALANCE", team_count=4, format_name="LEAGUE", request_key=None):
        if isinstance(team_count, bool) or not isinstance(team_count, int) or team_count not in allowed_team_counts(build_mode):
            raise ValueError("일반 대회는 2·4팀, 경매 대회는 4·6·8팀으로 구성해 주세요.")
        if format_name not in allowed_formats(team_count, build_mode):
            raise ValueError("팀 수에 맞는 대회 방식을 선택해 주세요.")
        title, starts_at, description = _details(title, starts_at, description)
        with self.core.transaction() as conn:
            actor = self.competition._authorize(conn, token)
            kind = "AUCTION" if build_mode == "AUCTION" else "NORMAL"
            key, fingerprint, receipt = self.competition._creation_request(conn, actor, kind,
                {"flow": "preparation", "title": title, "starts_at": starts_at, "description": description,
                 "build_mode": build_mode, "team_count": team_count, "format": format_name}, request_key)
            if receipt is not None:
                return receipt
            event_id = self.competition._new_event(conn, actor, title, kind, format_name, "DRAFT")
            conn.execute("UPDATE competition_events SET starts_at=?,description=?,build_mode=?,team_count=?,workflow_version=1 WHERE id=?", (starts_at, description, build_mode, team_count, event_id))
            self._audit(conn, event_id, actor, "CREATE_DRAFT", f"{build_mode}, {team_count}팀, {format_name}, 예정 {starts_at}")
            self.competition._save_creation_request(conn, actor, event_id, kind, key, fingerprint)
            return event_id

    def update_details(self, token, event_id, title, starts_at, description="", format_name=None):
        title, starts_at, description = _details(title, starts_at, description)
        with self.core.transaction() as conn:
            actor, event = self._edit(conn, token, event_id, ("DRAFT", "RECRUITING", "CAPTAIN_SELECTION", "TEAM_BUILDING"))
            self.competition._guard_live(conn, event_id)
            format_name = self._format(event, format_name or event["format"])
            conn.execute("UPDATE competition_events SET title=?,starts_at=?,description=?,format=? WHERE id=?", (title, starts_at, description, format_name, event_id))
            self._audit(conn, event_id, actor, "DETAILS", f"이름 {event['title']} → {title}; 예정 {event['starts_at']} → {starts_at}; 방식 {event['format']} → {format_name}; 설명 {event['description']} → {description}")

    def open_recruitment(self, token, event_id):
        with self.core.transaction() as conn:
            actor, event = self._edit(conn, token, event_id, ("DRAFT",))
            self._transition(conn, event, actor, "RECRUITING", "참가자 모집 시작")

    def set_participants(self, token, event_id, assignments, expected_roster_token=None):
        with self.core.transaction() as conn:
            actor, event = self._edit(conn, token, event_id, ("RECRUITING",))
            self.competition._check_roster_token(conn, event_id, expected_roster_token)
            self.competition._guard_live(conn, event_id)
            players = self.competition._player_snapshots(conn, assignments)
            ids = [p["member_id"] for p in players]
            if len(ids) > event["team_count"] * 5 or len(set(ids)) != len(ids):
                raise ValueError("참가자는 중복 없이 대회 정원 이내로 선택해 주세요.")
            existing = {row["member_id"]: dict(row) for row in conn.execute("SELECT member_id,participation_status FROM competition_players WHERE event_id=?", (event_id,))}
            previous = {member_id for member_id, row in existing.items() if row["participation_status"] == "SELECTED"}
            statements = []
            removed = sorted(previous - set(ids))
            if removed:
                statements.append(("UPDATE competition_players SET participation_status='EXCLUDED',state='EXCLUDED',exclusion_reason='참가 명단 변경' WHERE event_id=? AND member_id IN (" + ",".join("?" for _ in removed) + ")", (event_id, *removed)))
            for player in players:
                if player["member_id"] not in existing:
                    statements.append(self.competition._insert_player_statement(event_id, player))
                    continue
                statements.append(("UPDATE competition_players SET riot_id=?,role=?,score=?,main_role_snapshot=?,sub_role_snapshot=?,clan_tier_snapshot=?,current_tier_snapshot=?,current_tier_lp_snapshot=?,participation_status='SELECTED',state='AVAILABLE',team_id=NULL,price=0,exclusion_reason='' WHERE event_id=? AND member_id=?",
                    (player["riot_id"], player["role"], player["score"], player["main_role_snapshot"], player["sub_role_snapshot"],
                     player["clan_tier_snapshot"], player["current_tier_snapshot"], player["current_tier_lp_snapshot"], event_id, player["member_id"])))
            self.competition._write_statements(conn, statements)
            self._audit(conn, event_id, actor, "PARTICIPANTS", f"참가 명단 {sorted(previous)} → {ids}; 포지션 {[(p['member_id'], p['role']) for p in players]}")

    def confirm_participants(self, token, event_id, expected_roster_token=None):
        with self.core.transaction() as conn:
            actor, event = self._edit(conn, token, event_id, ("RECRUITING",))
            self.competition._check_roster_token(conn, event_id, expected_roster_token)
            players = self._players(conn, event_id)
            if len(players) != event["team_count"] * 5:
                raise ValueError(f"참가자 {event['team_count'] * 5}명을 채운 뒤 확정해 주세요.")
            if event["build_mode"] != "AUCTION" and any(sum(p["role"] == r for p in players) != event["team_count"] for r in ROLES):
                raise ValueError(f"각 포지션에 {event['team_count']}명씩 배정해 주세요.")
            refreshed = self.competition._player_snapshots(conn, players)
            statements = []
            for p, fresh in zip(players, refreshed):
                statements.append(("UPDATE competition_players SET riot_id=?,score=?,main_role_snapshot=?,sub_role_snapshot=?,clan_tier_snapshot=?,current_tier_snapshot=?,current_tier_lp_snapshot=? WHERE id=?",
                    (fresh["riot_id"], fresh["score"], fresh["main_role_snapshot"], fresh["sub_role_snapshot"],
                     fresh["clan_tier_snapshot"], fresh["current_tier_snapshot"], fresh["current_tier_lp_snapshot"], p["id"])))
            self.competition._write_statements(conn, statements)
            conn.execute("UPDATE competition_events SET participants_confirmed_at=? WHERE id=?", (_now(), event_id))
            self._transition(conn, event, actor, "CAPTAIN_SELECTION", "참가자 및 전력 스냅샷 확정")

    def set_captains(self, token, event_id, member_ids):
        with self.core.transaction() as conn:
            actor, event = self._edit(conn, token, event_id, ("CAPTAIN_SELECTION", "TEAM_BUILDING"))
            self.competition._guard_live(conn, event_id)
            ids = [int(m) for m in member_ids]
            if len(ids) != event["team_count"] or len(set(ids)) != len(ids):
                raise ValueError("팀 수에 맞는 서로 다른 팀장을 선택해 주세요.")
            captains = [self._selected(conn, event_id, m) for m in ids]
            conn.execute("UPDATE competition_players SET team_id=NULL,price=0,state='AVAILABLE' WHERE event_id=? AND participation_status='SELECTED'", (event_id,))
            conn.execute("DELETE FROM competition_teams WHERE event_id=?", (event_id,))
            for index, captain in enumerate(captains):
                budget = auction_budget(captain["score"]) if event["build_mode"] == "AUCTION" else 0
                team_id = conn.execute("INSERT INTO competition_teams(event_id,name,captain_id,budget) VALUES(?,?,?,?)", (event_id, f"{index + 1}팀", captain["member_id"], budget)).lastrowid
                self.competition._assign(conn, event_id, captain["member_id"], team_id, 0)
            self._transition(conn, event, actor, "TEAM_BUILDING", f"팀장 {ids}; 팀장 고정, 팀 배정 초기화")

    def auto_balance(self, token, event_id):
        with self.core.read_snapshot() as conn:
            actor, event, snapshot, teams, players = self._balance_inputs(conn, token, event_id)
        best, best_key = _balance_captains(players, teams, event["team_count"])
        with self.core.transaction() as conn:
            actor, event, latest, teams, players = self._balance_inputs(conn, token, event_id)
            self.competition._check_balance_inputs(snapshot["token"], latest["token"])
            statements = [("UPDATE competition_players SET team_id=NULL,price=0,state='AVAILABLE' WHERE event_id=? AND participation_status='SELECTED'", (event_id,))]
            counts = [0] * len(teams)
            for column in best:
                for index, player in enumerate(column):
                    # Every selected player is reset above. Zero-price assignment
                    # has known destination totals; validate without N rereads.
                    writes = self.competition._assignment_statements(teams[index], player, 0, counts[index], 0)
                    statements.append(writes[0])
                    counts[index] += 1
            ids = [player["member_id"] for player in players]
            statements.append(("UPDATE competition_events SET current_player_id=NULL WHERE id=? AND current_player_id IN (" + ",".join("?" for _ in ids) + ")", (event_id, *ids)))
            self.competition._write_statements(conn, statements)
            self._audit(conn, event_id, actor, "AUTO_BALANCE", f"팀장 고정, 팀 전력 최대 차이 {best_key[0]:g}; 배정 {[[p['member_id'] for p in col] for col in best]}")

    def _balance_inputs(self, conn, token, event_id):
        actor, event = self._edit(conn, token, event_id, ("TEAM_BUILDING",))
        if event["build_mode"] != "BALANCE":
            raise ValueError("자동 밸런스 방식으로 만든 대회에서 사용해 주세요.")
        self.competition._guard_live(conn, event_id)
        snapshot = self.competition._roster_snapshot(conn, event_id, event)
        teams = snapshot["teams"]
        players = [player for player in snapshot["players"] if player["participation_status"] == "SELECTED"]
        count = event["team_count"]
        if count not in (2, 4) or len(teams) != count or len(players) != 5 * count:
            raise ValueError("팀장과 참가 정원을 먼저 확정해 주세요.")
        # Preparation deliberately uses the previously confirmed score/role
        # snapshots. Live membership approval must still hold at persistence.
        ids = [player["member_id"] for player in players]
        statuses = list(conn.execute("SELECT id,status FROM members WHERE id IN (" + ",".join("?" for _ in ids) + ")", tuple(ids)))
        if len(statuses) != len(ids) or any(row["status"] != "APPROVED" for row in statuses):
            raise ValueError("승인된 회원만 참가할 수 있습니다.")
        return actor, event, snapshot, teams, players

    def assign_player(self, token, event_id, member_id, team_id, role=None):
        with self.core.transaction() as conn:
            actor, event = self._edit(conn, token, event_id, ("TEAM_BUILDING",))
            self.competition._guard_live(conn, event_id)
            if event["build_mode"] == "AUCTION":
                raise ValueError("경매 대회 선수는 실시간 경매에서 배정해 주세요.")
            player = self._selected(conn, event_id, member_id)
            target = self.competition._team(conn, event_id, team_id)
            captain = conn.execute("SELECT id FROM competition_teams WHERE event_id=? AND captain_id=?", (event_id, member_id)).fetchone()
            if captain and captain["id"] != team_id:
                raise ValueError("팀장은 자신의 팀에 고정됩니다.")
            role = role or player["role"]
            if role not in ROLES:
                raise ValueError("올바른 포지션을 선택해 주세요.")
            if conn.execute("SELECT 1 FROM competition_players WHERE team_id=? AND member_id<>? AND role=?", (target["id"], member_id, role)).fetchone():
                raise ValueError("해당 팀에는 같은 포지션의 선수가 이미 있습니다.")
            conn.execute("UPDATE competition_players SET role=? WHERE id=?", (role, player["id"]))
            self.competition._assign(conn, event_id, member_id, team_id, 0)
            self._audit(conn, event_id, actor, "ASSIGN", f"선수 {member_id}: {player['team_id']} → {team_id}, {player['role']} → {role}")

    def unassign_player(self, token, event_id, member_id):
        with self.core.transaction() as conn:
            actor, event = self._edit(conn, token, event_id, ("TEAM_BUILDING",))
            if event["build_mode"] == "AUCTION":
                raise ValueError("경매 대회 선수는 실시간 경매에서 배정해 주세요.")
            self.competition._guard_live(conn, event_id)
            player = self._selected(conn, event_id, member_id)
            if conn.execute("SELECT 1 FROM competition_teams WHERE event_id=? AND captain_id=?", (event_id, member_id)).fetchone():
                raise ValueError("팀장은 배정을 해제할 수 없습니다.")
            conn.execute("UPDATE competition_players SET team_id=NULL,price=0,state='AVAILABLE' WHERE id=?", (player["id"],))
            self._audit(conn, event_id, actor, "UNASSIGN", f"선수 {member_id}, 이전 팀 {player['team_id']}")

    def set_player_role(self, token, event_id, member_id, role):
        if role not in ROLES:
            raise ValueError("올바른 포지션을 선택해 주세요.")
        with self.core.transaction() as conn:
            actor, event = self._edit(conn, token, event_id, ("CAPTAIN_SELECTION", "TEAM_BUILDING", "BRACKET_SETUP"))
            self.competition._guard_live(conn, event_id, allow_completed=True)
            player = self._selected(conn, event_id, member_id)
            conn.execute("UPDATE competition_players SET role=? WHERE id=?", (role, player["id"]))
            self._audit(conn, event_id, actor, "ROLE", f"선수 {member_id}: {player['role']} → {role}")

    def set_team_roles(self, token, event_id, team_id, assignments):
        """Save one complete auction-team form and its audit entry atomically."""
        with self.core.transaction() as conn:
            actor, event = self._edit(conn, token, event_id, ("BRACKET_SETUP",))
            if event["build_mode"] != "AUCTION":
                raise ValueError("경매를 마친 팀의 포지션 확인에서 사용해 주세요.")
            self.competition._guard_live(conn, event_id, allow_completed=True)
            team = self.competition._team(conn, event_id, team_id)
            if not isinstance(assignments, dict) or len(assignments) != 5:
                raise ValueError("팀원 5명의 포지션을 모두 선택해 주세요.")
            selected = [(integer(member_id, "회원 번호"), role) for member_id, role in assignments.items()]
            if any(role not in ROLES for _, role in selected) or len({role for _, role in selected}) != 5:
                raise ValueError("탑·정글·미드·원딜·서포터를 팀에 한 명씩 배정해 주세요.")
            players = [dict(row) for row in conn.execute("SELECT member_id,role FROM competition_players WHERE event_id=? AND team_id=? AND participation_status='SELECTED' ORDER BY id", (event_id, team_id))]
            if len(players) != 5 or {member_id for member_id, _ in selected} != {player["member_id"] for player in players}:
                raise ValueError("팀 명단이 변경되었습니다. 현재 팀원 5명을 다시 확인해 주세요.")
            before = {player["member_id"]: player["role"] for player in players}
            after = dict(selected)
            if before == after:
                return
            conn.executemany("UPDATE competition_players SET role=? WHERE event_id=? AND team_id=? AND member_id=? AND participation_status='SELECTED'", [(role, event_id, team_id, member_id) for member_id, role in selected])
            self._audit(conn, event_id, actor, "ROLE", f"{team['name']} 전체 포지션: {before} → {after}")

    def prepare_auction(self, token, event_id):
        with self.core.transaction() as conn:
            actor, event = self._edit(conn, token, event_id, ("TEAM_BUILDING",))
            self.competition._guard_live(conn, event_id)
            if event["build_mode"] != "AUCTION":
                raise ValueError("경매 방식으로 만든 대회에서 사용해 주세요.")
            teams = list(conn.execute("SELECT * FROM competition_teams WHERE event_id=?", (event_id,)))
            players = self._players(conn, event_id)
            if len(teams) != event["team_count"] or len(players) != event["team_count"] * 5:
                raise ValueError("팀장과 참가 정원을 먼저 확정해 주세요.")
            for team in teams:
                assigned = [p for p in players if p["team_id"] == team["id"]]
                if len(assigned) != 1 or assigned[0]["member_id"] != team["captain_id"]:
                    raise ValueError("경매 시작 전에는 각 팀에 팀장만 배정되어야 합니다.")
            self._transition(conn, event, actor, "AUCTION_READY", "팀장·예산·경매 참가 명단 확정")

    def reopen_preparation(self, token, event_id, reason):
        """Reopen an unstarted auction without discarding its preparation history."""
        reason = str(reason).strip()
        if not reason or len(reason) > 1000:
            raise ValueError("다시 준비하는 사유를 1~1,000자로 입력해 주세요.")
        with self.core.transaction() as conn:
            actor, event = self._edit(conn, token, event_id, ("AUCTION_READY",))
            if event["kind"] != "AUCTION":
                raise ValueError("시작 전 경매만 참가 명단을 다시 준비할 수 있습니다.")
            teams = [dict(row) for row in conn.execute("SELECT * FROM competition_teams WHERE event_id=? ORDER BY id", (event_id,))]
            players = [dict(row) for row in conn.execute("SELECT * FROM competition_players WHERE event_id=? ORDER BY id", (event_id,))]
            captains = {team["captain_id"]: team["id"] for team in teams}
            if any(player["price"] or (player["team_id"] is not None and captains.get(player["member_id"]) != player["team_id"]) for player in players):
                raise ValueError("선수 배정·낙찰 이력이 있는 경매는 준비 단계로 되돌릴 수 없습니다.")
            if conn.execute("SELECT 1 FROM competition_games WHERE event_id=?", (event_id,)).fetchone():
                raise ValueError("대진이 생성된 경매는 준비 단계로 되돌릴 수 없습니다.")
            session, lots, live_history = None, [], []
            has_live = _table_exists(conn, "live_sessions")
            if has_live:
                row = conn.execute("SELECT * FROM live_sessions WHERE event_id=?", (event_id,)).fetchone()
                session = dict(row) if row else None
                lots = [dict(row) for row in conn.execute("SELECT * FROM live_lots WHERE event_id=? ORDER BY sequence", (event_id,))]
                live_history = [dict(row) for row in conn.execute("SELECT * FROM live_events WHERE event_id=? ORDER BY id", (event_id,))]
                began = conn.execute("SELECT 1 FROM live_events WHERE event_id=? AND type<>'CONFIGURE'", (event_id,)).fetchone()
                bids = conn.execute("SELECT 1 FROM live_bids WHERE event_id=?", (event_id,)).fetchone()
                if ((session and session["status"] != "READY") or began or bids
                        or any(lot["status"] != "QUEUED" or lot["opened_at"] is not None or lot["highest_team_id"] is not None for lot in lots)):
                    raise ValueError("이미 시작하거나 입찰·낙찰 이력이 있는 경매는 되돌릴 수 없습니다.")
            snapshot = {"reason": reason, "event": event, "teams": teams, "players": players,
                        "live_settings": session, "auction_order": lots, "live_history": live_history}
            self._audit(conn, event_id, actor, "PREPARATION_REOPENED", json.dumps(snapshot, ensure_ascii=False))
            if has_live:
                # Preserve original CONFIGURE rows in the durable audit snapshot
                # before releasing their FK to the disposable, unstarted session.
                conn.execute("DELETE FROM live_events WHERE event_id=?", (event_id,))
                conn.execute("DELETE FROM live_lots WHERE event_id=?", (event_id,))
                conn.execute("DELETE FROM live_sessions WHERE event_id=?", (event_id,))
            conn.execute("UPDATE competition_players SET team_id=NULL,price=0,state=CASE participation_status WHEN 'SELECTED' THEN 'AVAILABLE' ELSE 'EXCLUDED' END WHERE event_id=?", (event_id,))
            conn.execute("DELETE FROM competition_teams WHERE event_id=?", (event_id,))
            conn.execute("UPDATE competition_events SET current_player_id=NULL,participants_confirmed_at=NULL WHERE id=?", (event_id,))
            self._transition(conn, event, actor, "RECRUITING", f"참가 명단 보존, 팀장·팀·경매 설정을 다시 준비: {reason}")

    def _validate_roster(self, conn, event):
        teams = [dict(r) for r in conn.execute("SELECT * FROM competition_teams WHERE event_id=? ORDER BY id", (event["id"],))]
        players = self._players(conn, event["id"])
        if len(teams) != event["team_count"] or len(players) != len(teams) * 5 or any(p["team_id"] is None for p in players):
            raise ValueError("참가자 전원을 팀당 5명씩 배정해 주세요.")
        for team in teams:
            roster = [p for p in players if p["team_id"] == team["id"]]
            if len(roster) != 5 or {p["role"] for p in roster} != set(ROLES):
                raise ValueError(f"{team['name']}: 5명과 TOP/JG/MID/AD/SUP 포지션을 확인해 주세요.")
            if team["captain_id"] not in {p["member_id"] for p in roster}:
                raise ValueError("팀장은 자신의 팀에 포함되어야 합니다.")
        # Recheck current eligibility at both build and confirmation without
        # replacing the score/tier snapshots already frozen on participants.
        self.competition._player_snapshots(conn, players)
        return teams

    def build_bracket(self, token, event_id, format_name=None):
        with self.core.transaction() as conn:
            actor, event = self._edit(conn, token, event_id, ("TEAM_BUILDING", "BRACKET_SETUP"))
            self.competition._guard_live(conn, event_id, allow_completed=True)
            if event["build_mode"] == "AUCTION" and event["status"] != "BRACKET_SETUP":
                raise ValueError("실시간 경매를 완료한 뒤 대진을 생성해 주세요.")
            format_name = self._format(event, format_name or event["format"])
            teams = self._validate_roster(conn, event)
            if conn.execute("SELECT 1 FROM competition_games WHERE event_id=?", (event_id,)).fetchone():
                if format_name == event["format"]:
                    return
                raise ValueError("이미 생성한 대진의 방식은 변경할 수 없습니다.")
            conn.execute("UPDATE competition_events SET format=? WHERE id=?", (format_name, event_id))
            self.competition._make_schedule(conn, event_id, [t["id"] for t in teams], format_name)
            self._transition(conn, event, actor, "BRACKET_SETUP", f"{format_name} 대진 생성; 최종 확인 대기")

    def confirm_bracket(self, token, event_id):
        with self.core.transaction() as conn:
            actor, event = self._edit(conn, token, event_id, ("BRACKET_SETUP",))
            self.competition._guard_live(conn, event_id, allow_completed=True)
            self._validate_roster(conn, event)
            if not conn.execute("SELECT 1 FROM competition_games WHERE event_id=?", (event_id,)).fetchone():
                raise ValueError("대진을 먼저 생성해 주세요.")
            self._transition(conn, event, actor, "READY", "대진과 팀 명단 최종 확정")

    def warn_participant(self, token, event_id, member_id, reason):
        reason = str(reason).strip()
        if not reason:
            raise ValueError("경고 사유를 입력해 주세요.")
        with self.core.transaction() as conn:
            actor, event = self._edit(conn, token, event_id, PREPARATION_STATES[1:])
            self.competition._guard_live(conn, event_id, allow_completed=True)
            player = self._selected(conn, event_id, member_id)
            conn.execute("UPDATE competition_players SET warning_count=warning_count+1 WHERE id=?", (player["id"],))
            self._audit(conn, event_id, actor, "PARTICIPANT_WARNING", f"선수 {member_id}, 경고 {player['warning_count'] + 1}회: {reason}")

    def exclude_participant(self, token, event_id, member_id, reason):
        reason = str(reason).strip()
        if not reason:
            raise ValueError("대회 명단 제외 사유를 입력해 주세요.")
        with self.core.transaction() as conn:
            actor, event = self._edit(conn, token, event_id, PREPARATION_STATES[1:])
            self.competition._guard_live(conn, event_id)
            player = self._selected(conn, event_id, member_id)
            if conn.execute("SELECT 1 FROM competition_games WHERE event_id=? AND core_game_id IS NOT NULL", (event_id,)).fetchone():
                raise ValueError("실제 경기가 기록된 대회에서는 참가자를 제외할 수 없습니다.")
            conn.execute("UPDATE competition_games SET source_a=NULL,source_b=NULL WHERE event_id=?", (event_id,))
            conn.execute("DELETE FROM competition_games WHERE event_id=?", (event_id,))
            conn.execute("UPDATE competition_players SET team_id=NULL,price=0,state=CASE participation_status WHEN 'SELECTED' THEN 'AVAILABLE' ELSE 'EXCLUDED' END WHERE event_id=?", (event_id,))
            conn.execute("DELETE FROM competition_teams WHERE event_id=?", (event_id,))
            conn.execute("UPDATE competition_players SET participation_status='EXCLUDED',state='EXCLUDED',exclusion_reason=? WHERE id=?", (reason, player["id"]))
            conn.execute("UPDATE competition_events SET current_player_id=NULL,participants_confirmed_at=NULL WHERE id=?", (event_id,))
            self._transition(conn, event, actor, "RECRUITING", f"선수 {member_id} 명단 제외: {reason}; 미진행 팀·대진 초기화 후 충원")

    def upgrade_auction(self, token, event_id):
        """Opt in untouched legacy drafts; never reset settled prices or rosters."""
        with self.core.transaction() as conn:
            actor, event = self._edit(conn, token, event_id, ("AUCTION",))
            self.competition._guard_live(conn, event_id)
            if event["kind"] != "AUCTION" or event["workflow_version"] or event["current_player_id"]:
                raise ValueError("추첨·입찰 전의 이전 경매만 새 준비 흐름으로 전환할 수 있습니다.")
            if conn.execute("SELECT 1 FROM competition_games WHERE event_id=?", (event_id,)).fetchone():
                raise ValueError("대진이 있는 경매는 전환할 수 없습니다.")
            teams = list(conn.execute("SELECT * FROM competition_teams WHERE event_id=?", (event_id,)))
            players = self._players(conn, event_id)
            if len(teams) not in (4, 6, 8) or len(players) != len(teams) * 5:
                raise ValueError("경매 팀 수와 참가 정원을 확인해 주세요.")
            captains = {t["captain_id"]: t["id"] for t in teams}
            for p in players:
                expected = captains.get(p["member_id"])
                if p["price"] or p["team_id"] != expected or p["state"] not in ("AVAILABLE", "ASSIGNED"):
                    raise ValueError("입찰·유찰·배정 이력이 있는 경매는 전환할 수 없습니다.")
            conn.execute("UPDATE competition_events SET workflow_version=1,build_mode='AUCTION',team_count=?,participants_confirmed_at=? WHERE id=?", (len(teams), _now(), event_id))
            self._transition(conn, event, actor, "AUCTION_READY", "입찰 전 경매를 실시간 경매 준비 흐름으로 전환; 명단·예산 유지")
