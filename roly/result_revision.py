"""Reviewed, atomic correction of an active auction tournament's result.

Core game rows are revised or voided, never deleted. Superseded tiebreak
fixtures are archived before removal from the current bracket.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import secrets

from roly.competition import Competition
from roly.core import integer


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ResultRevisionService:
    def __init__(self, core, competition=None, *, clock=None):
        self.core = core
        self.competition = competition or Competition(core)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if core.is_postgres:
            return
        with closing(core.connect()) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS result_revision_previews(
                    token_hash TEXT PRIMARY KEY, actor_id INTEGER NOT NULL REFERENCES accounts(id),
                    event_id INTEGER NOT NULL REFERENCES competition_events(id),
                    game_id INTEGER NOT NULL, winner_team_id INTEGER NOT NULL,
                    reason TEXT NOT NULL, fingerprint TEXT NOT NULL, plan_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                    applied_at TEXT, result_json TEXT);
                CREATE TABLE IF NOT EXISTS competition_game_archives(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER NOT NULL REFERENCES competition_events(id),
                    fixture_id INTEGER NOT NULL, core_game_id INTEGER,
                    revision_token_hash TEXT NOT NULL REFERENCES result_revision_previews(token_hash),
                    snapshot TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS competition_archive_event ON competition_game_archives(event_id,id);
            """)

    def _validate(self, conn, token, event_id, game_id, winner_team_id):
        actor = self.core.require_admin(conn, token)
        event = self.competition._event(conn, event_id)
        if event["kind"] != "AUCTION":
            raise ValueError("후속 경기 일괄 정정은 경매 대회에서만 사용할 수 있습니다. 일반내전은 기존 결과 정정을 이용해 주세요.")
        if event["status"] != "PLAYING":
            raise ValueError("진행 중인 경매 대회만 정정할 수 있습니다. 종료된 대회의 기록과 보상은 변경할 수 없습니다.")
        if conn.execute("SELECT 1 FROM award_batches WHERE event_id=?", (str(event_id),)).fetchone():
            raise ValueError("이미 우승 보상이 지급된 대회입니다. 결과를 일괄 정정할 수 없습니다.")
        games = [dict(r) for r in conn.execute("SELECT * FROM competition_games WHERE event_id=? ORDER BY id", (event_id,))]
        game = next((g for g in games if g["id"] == game_id), None)
        if not game or game["status"] != "COMPLETED" or not game["core_game_id"]:
            raise ValueError("실제 결과가 확정된 경기를 선택해 주세요.")
        if winner_team_id not in (game["team_a"], game["team_b"]) or winner_team_id == game["winner_team_id"]:
            raise ValueError("현재 결과와 다른 실제 참가 팀을 정정 후 승리팀으로 선택해 주세요.")
        for fixture in games:
            if fixture["status"] != "COMPLETED":
                continue
            result = conn.execute("SELECT * FROM games WHERE id=?", (fixture["core_game_id"],)).fetchone()
            if (not result or result["status"] != "CONFIRMED" or result["kind"] != "AUCTION"
                    or str(result["tournament_id"]) != str(event_id)
                    or fixture["winner_team_id"] != fixture["team_a" if result["winner"] == "A" else "team_b"]):
                raise ValueError("대진과 실제 경기 원장이 일치하지 않습니다. 원장 상태를 먼저 확인해 주세요.")
        return actor, event, games, game

    def _fingerprint(self, conn, event, games):
        core_games = [dict(r) for r in conn.execute("SELECT * FROM games WHERE tournament_id=? ORDER BY id", (str(event["id"]),))]
        players = [dict(r) for r in conn.execute("SELECT * FROM competition_players WHERE event_id=? ORDER BY id", (event["id"],))]
        teams = [dict(r) for r in conn.execute("SELECT * FROM competition_teams WHERE event_id=? ORDER BY id", (event["id"],))]
        awards = [dict(r) for r in conn.execute("SELECT * FROM award_batches WHERE event_id=? ORDER BY id", (str(event["id"]),))]
        ledger = [dict(r) for r in conn.execute("SELECT p.* FROM game_players p JOIN games g ON g.id=p.game_id WHERE g.tournament_id=? ORDER BY p.game_id,p.member_id", (str(event["id"]),))]
        payload = [event, games, core_games, players, teams, awards, ledger]
        return hashlib.sha256(_json(payload).encode()).hexdigest()

    def _affected(self, conn, event, games, game):
        affected = {g["id"]: {**g, "action": "RESET"} for g in self.competition._descendants(conn, event["id"], game["id"])}
        if event["format"] in ("LEAGUE", "GROUP_STAGE"):
            for other in games:
                if other["stage"] != "TIEBREAK" or other["group_key"] != game["group_key"]:
                    continue
                # Same-batch games are independent; only later batches depend
                # on this tiebreak result. A MAIN change invalidates every batch.
                if game["stage"] == "MAIN" or (game["stage"] == "TIEBREAK" and other["round"] // 1000 > game["round"] // 1000):
                    affected[other["id"]] = {**other, "action": "REMOVE_TIEBREAK"}
            if event["format"] == "GROUP_STAGE" and game["stage"] != "FINAL":
                for other in games:
                    if other["stage"] == "FINAL":
                        affected[other["id"]] = {**other, "action": "RESET"}
        return sorted(affected.values(), key=lambda g: (g["round"], g["id"]))

    def _apply_fixtures(self, conn, event, game, winner_team_id, affected):
        for other in affected:
            if other["action"] == "REMOVE_TIEBREAK":
                conn.execute("DELETE FROM competition_games WHERE id=?", (other["id"],))
            else:
                conn.execute("UPDATE competition_games SET status='PENDING',winner_team_id=NULL,core_game_id=NULL,attempt=attempt+1 WHERE id=?", (other["id"],))
        conn.execute("UPDATE competition_games SET winner_team_id=? WHERE id=?", (winner_team_id, game["id"]))
        self.competition._propagate(conn, event["id"])
        self.competition._refresh_group_final(conn, event["id"])

    def preview(self, token, event_id, game_id, winner_team_id, reason):
        event_id, game_id, winner_team_id = (integer(v, "대회·경기·팀 번호") for v in (event_id, game_id, winner_team_id))
        reason = str(reason).strip()
        if not reason or len(reason) > 1000:
            raise ValueError("정정 사유를 1~1,000자로 입력해 주세요.")
        with self.core.transaction() as conn:
            actor, event, games, game = self._validate(conn, token, event_id, game_id, winner_team_id)
            affected = self._affected(conn, event, games, game)
            fingerprint = self._fingerprint(conn, event, games)
            names = {r["id"]: r["name"] for r in conn.execute("SELECT id,name FROM competition_teams WHERE event_id=?", (event_id,))}
            # Simulate only derived fixtures inside a savepoint. The simulation
            # cannot leak any changed results, ledger entries or attempts.
            conn.execute("SAVEPOINT result_preview")
            try:
                self._apply_fixtures(conn, event, game, winner_team_id, affected)
                projected = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM competition_games WHERE event_id=?", (event_id,))}
            finally:
                conn.execute("ROLLBACK TO result_preview")
                conn.execute("RELEASE result_preview")
            def label(fixture):
                return {**fixture, "team_a_name": names.get(fixture["team_a"], "미정"),
                        "team_b_name": names.get(fixture["team_b"], "미정"),
                        "winner_name": names.get(fixture["winner_team_id"], "")}
            impacts = []
            for other in affected:
                item = label(other)
                future = projected.get(other["id"])
                item["after_team_a_name"] = names.get(future["team_a"], "미정") if future else "대진 제외"
                item["after_team_b_name"] = names.get(future["team_b"], "미정") if future else "대진 제외"
                impacts.append(item)
            plan = {"event_id": event_id, "title": event["title"], "source_game": label(game),
                    "winner_team_id": winner_team_id, "winner_name": names[winner_team_id], "reason": reason,
                    "affected_games": impacts, "void_count": sum(g["status"] == "COMPLETED" for g in affected),
                    "removed_tiebreak_count": sum(g["action"] == "REMOVE_TIEBREAK" for g in affected),
                    "replay_count": sum(g["action"] == "RESET" for g in affected), "score_change": 0}
            preview_token = secrets.token_urlsafe(32)
            stamp = self.clock()
            expires = stamp + timedelta(minutes=10)
            conn.execute("INSERT INTO result_revision_previews(token_hash,actor_id,event_id,game_id,winner_team_id,reason,fingerprint,plan_json,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (hashlib.sha256(preview_token.encode()).hexdigest(), actor["id"], event_id, game_id, winner_team_id, reason, fingerprint, _json(plan), stamp.isoformat(), expires.isoformat()))
            return {**plan, "preview_token": preview_token, "expires_at": expires.isoformat()}

    def apply(self, token, preview_token, *, confirmed=False):
        if confirmed is not True:
            raise ValueError("미리보기의 무효 경기와 대진 변경을 확인한 뒤 정정을 확정해 주세요.")
        token_hash = hashlib.sha256(str(preview_token).encode()).hexdigest()
        with self.core.transaction() as conn:
            actor = self.core.require_admin(conn, token)
            preview = conn.execute("SELECT * FROM result_revision_previews WHERE token_hash=?", (token_hash,)).fetchone()
            if not preview or preview["actor_id"] != actor["id"]:
                raise ValueError("본인이 확인한 정정 미리보기를 다시 열어 주세요.")
            if preview["applied_at"]:
                return json.loads(preview["result_json"])
            if self.clock() >= datetime.fromisoformat(preview["expires_at"]):
                raise ValueError("미리보기 확인 시간이 지났습니다. 최신 영향 범위를 다시 확인해 주세요.")
            actor, event, games, game = self._validate(conn, token, preview["event_id"], preview["game_id"], preview["winner_team_id"])
            if not hmac.compare_digest(preview["fingerprint"], self._fingerprint(conn, event, games)):
                raise ValueError("미리보기 이후 경기 또는 대회 상태가 바뀌었습니다. 최신 영향 범위를 다시 확인해 주세요.")
            affected = self._affected(conn, event, games, game)
            stamp = self.clock().isoformat()
            team_names = {r["id"]: r["name"] for r in conn.execute("SELECT id,name FROM competition_teams WHERE event_id=?", (event["id"],))}
            # Archive the source too, so its old winner remains identifiable.
            for original in [game, *affected]:
                snapshot = {**original, "team_a_name": team_names.get(original["team_a"], "미정"),
                            "team_b_name": team_names.get(original["team_b"], "미정"),
                            "winner_name": team_names.get(original["winner_team_id"], ""),
                            "players": [dict(r) for r in conn.execute("SELECT member_id,riot_id,role,score,team_id FROM competition_players WHERE event_id=? AND team_id IN (?,?) ORDER BY id", (event["id"], original["team_a"], original["team_b"]))]}
                conn.execute("INSERT INTO competition_game_archives(event_id,fixture_id,core_game_id,revision_token_hash,snapshot,created_at) VALUES(?,?,?,?,?,?)", (event["id"], original["id"], original["core_game_id"], token_hash, _json(snapshot), stamp))
            for other in reversed(affected):
                if other["status"] == "COMPLETED":
                    self.core.void_game(token, other["core_game_id"], f"원경기 {game['id']} 정정에 따른 후속 경기 무효: {preview['reason']}", conn=conn)
            self.core.correct_game(token, game["core_game_id"], "A" if preview["winner_team_id"] == game["team_a"] else "B", preview["reason"], conn=conn)
            self._apply_fixtures(conn, event, game, preview["winner_team_id"], affected)
            result = {"event_id": event["id"], "game_id": game["id"], "winner_team_id": preview["winner_team_id"],
                      "voided_core_game_ids": [g["core_game_id"] for g in affected if g["status"] == "COMPLETED"],
                      "reset_fixture_ids": [g["id"] for g in affected if g["action"] == "RESET"],
                      "removed_tiebreak_ids": [g["id"] for g in affected if g["action"] == "REMOVE_TIEBREAK"]}
            self.competition._audit(conn, event["id"], actor, "RESULT_CASCADE",
                f"경기 {game['id']}: {team_names[game['winner_team_id']]} → {team_names[preview['winner_team_id']]}; "
                f"완료 후속 경기 {len(result['voided_core_game_ids'])}개 무효, 대진 {len(result['reset_fixture_ids'])}개 재계산, "
                f"추가 대진 {len(result['removed_tiebreak_ids'])}개 제외. 사유: {preview['reason']}")
            conn.execute("UPDATE result_revision_previews SET applied_at=?,result_json=? WHERE token_hash=?", (stamp, _json(result), token_hash))
            return result

    preview_result_correction = preview
    apply_result_correction = apply
