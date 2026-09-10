"""Server-clock competitive bidding with durable, transactional settlement.

The worker is process-local; deadlines and every decision are durable database
records. Multiple workers use the same transactional write lock, so restarting the app
or opening another browser never creates a second sale.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import secrets
import threading
import time
import uuid

from .core import identity, integer, session_query

BID_INCREMENTS = (5, 10, 20, 30, 50, 70, 100)
BID_EXTENSION_SECONDS = 5
BID_SECONDS_OPTIONS = (5, 10, 15, 20, 25, 30)
DEFAULT_BID_SECONDS = 10
MAX_BID_SECONDS = BID_SECONDS_OPTIONS[-1]
TRANSITION_SECONDS = 3
_LOG = logging.getLogger(__name__)


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="microseconds")


def _effective_bid_seconds(seconds):
    """Map earlier saved durations up to the next supported step, within 5..30."""
    return min(MAX_BID_SECONDS, max(BID_SECONDS_OPTIONS[0], ((int(seconds) + 4) // 5) * 5))


class LiveAuction:
    _workers = {}
    _worker_lock = threading.Lock()

    def __init__(self, core, competition, *, clock=None):
        self.core = core
        self.competition = competition
        self._clock = clock or time.time
        self._injected_clock = clock is not None
        if core.is_postgres:
            return
        with closing(core.connect()) as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS live_sessions(
                event_id INTEGER PRIMARY KEY REFERENCES competition_events(id),
                status TEXT NOT NULL CHECK(status IN ('READY','RUNNING','WAITING','PAUSED','COMPLETED','CANCELLED')),
                bid_seconds INTEGER NOT NULL,reset_on_bid INTEGER NOT NULL,
                transition_seconds INTEGER NOT NULL DEFAULT 3,
                current_lot_id INTEGER,next_at REAL,
                paused_phase TEXT,pause_remaining REAL,
                created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS live_lots(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL REFERENCES live_sessions(event_id),
                member_id INTEGER NOT NULL REFERENCES members(id),
                sequence INTEGER NOT NULL,attempt INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL CHECK(status IN ('QUEUED','OPEN','SOLD','UNSOLD','CANCELLED')),
                highest_team_id INTEGER REFERENCES competition_teams(id),highest_bid INTEGER,
                opened_at REAL,closes_at REAL,closed_at REAL,
                UNIQUE(event_id,sequence),UNIQUE(event_id,member_id,attempt));
            CREATE UNIQUE INDEX IF NOT EXISTS live_one_open ON live_lots(event_id) WHERE status='OPEN';
            CREATE TABLE IF NOT EXISTS live_bids(
                id INTEGER PRIMARY KEY AUTOINCREMENT,request_id TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL,event_id INTEGER NOT NULL REFERENCES live_sessions(event_id),
                lot_id INTEGER NOT NULL REFERENCES live_lots(id),team_id INTEGER NOT NULL REFERENCES competition_teams(id),
                account_id INTEGER NOT NULL REFERENCES accounts(id),member_id INTEGER NOT NULL REFERENCES members(id),
                amount INTEGER NOT NULL CHECK(amount>=0),closes_at REAL NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS live_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,event_id INTEGER NOT NULL REFERENCES live_sessions(event_id),
                lot_id INTEGER REFERENCES live_lots(id),actor_id INTEGER REFERENCES accounts(id),
                type TEXT NOT NULL,detail TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS live_bid_event ON live_bids(event_id,id);
            """)
            from .auction_state import initialize_changes
            db.execute("BEGIN IMMEDIATE")
            try:
                initialize_changes(db)
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def _now(self, db):
        if self.core.is_postgres and not self._injected_clock:
            # clock_timestamp(), unlike now(), advances while a writer waits.
            return float(db.execute("SELECT EXTRACT(EPOCH FROM clock_timestamp())::double precision").fetchone()[0])
        return self._clock()

    def _session(self, db, event_id):
        row = db.execute("SELECT * FROM live_sessions WHERE event_id=?", (event_id,)).fetchone()
        if not row:
            raise ValueError("실시간 경매 설정을 먼저 저장해 주세요.")
        return dict(row)

    def get_event_status(self, event_id):
        """Read only the workflow status for waiting-room polling."""
        with self.core.read_snapshot() as db:
            row = db.execute("SELECT status FROM competition_events WHERE id=?", (event_id,)).fetchone()
            return row["status"] if row else None

    def _log(self, db, event_id, kind, detail, timestamp, actor=None, lot_id=None):
        db.execute("INSERT INTO live_events(event_id,lot_id,actor_id,type,detail,created_at) VALUES(?,?,?,?,?,?)",
                   (event_id, lot_id, actor["id"] if actor else None, kind,
                    json.dumps(detail, ensure_ascii=False), _iso(timestamp)))

    def configure(self, token, event_id, bid_seconds=DEFAULT_BID_SECONDS, reset_on_bid=True, order=None, team_budgets=None, *, expected_settings=None):
        bid_seconds = integer(bid_seconds, "입찰 시간")
        if bid_seconds not in BID_SECONDS_OPTIONS:
            raise ValueError("입찰 시간은 5·10·15·20·25·30초 중에서 선택해 주세요.")
        if reset_on_bid is not True:
            raise ValueError("유효 입찰마다 남은 시간에 5초를 더하는 규칙은 해제할 수 없습니다.")
        with self.core.transaction() as db:
            actor = self.competition._authorize(db, token, event_id)
            event = self.competition._event(db, event_id)
            if event["kind"] != "AUCTION" or event["status"] != "AUCTION_READY":
                raise ValueError("경매 준비 단계에서만 설정할 수 있습니다.")
            previous = db.execute("SELECT status,updated_at FROM live_sessions WHERE event_id=?", (event_id,)).fetchone()
            if previous and previous["status"] != "READY":
                raise ValueError("시작한 경매의 설정은 변경할 수 없습니다.")
            if expected_settings is not None:
                version = (self.competition._roster_token(db, event_id), previous["updated_at"] if previous else None)
                if not isinstance(expected_settings, tuple) or expected_settings != version:
                    raise ValueError("경매 설정이 변경되었습니다. 최신 설정을 불러온 뒤 다시 저장해주세요.")
            teams = list(db.execute("SELECT * FROM competition_teams WHERE event_id=?", (event_id,)))
            players = list(db.execute("SELECT p.*,m.status AS member_status FROM competition_players p JOIN members m ON m.id=p.member_id WHERE p.event_id=? AND p.participation_status='SELECTED' ORDER BY p.id", (event_id,)))
            if len(teams) not in (4, 6, 8) or len(players) != len(teams) * 5:
                raise ValueError("4·6·8개 팀과 팀당 5명의 확정 참가자가 필요합니다.")
            if any(p["member_status"] != "APPROVED" for p in players):
                raise ValueError("승인된 회원만 경매에 참가할 수 있습니다.")
            for team in teams:
                assigned = [p for p in players if p["team_id"] == team["id"]]
                if len(assigned) != 1 or assigned[0]["member_id"] != team["captain_id"]:
                    raise ValueError("경매 시작 전 각 팀에는 팀장 한 명만 배정되어야 합니다.")
            previous_budgets = {team["id"]: team["budget"] for team in teams}
            budgets = previous_budgets
            if team_budgets is not None:
                if not isinstance(team_budgets, Mapping):
                    raise ValueError("팀별 시작 포인트는 팀 번호와 포인트로 지정해 주세요.")
                budgets = {integer(team_id, "팀 번호"): integer(amount, "시작 포인트") for team_id, amount in team_budgets.items()}
                if len(budgets) != len(team_budgets) or set(budgets) != set(previous_budgets):
                    raise ValueError("이 경매의 모든 팀에 시작 포인트를 한 번씩 지정해 주세요.")
                if any(amount < 0 or amount > 9_223_372_036_854_775_807 for amount in budgets.values()):
                    raise ValueError("시작 포인트는 저장 가능한 범위의 0 이상 정수로 입력해 주세요.")
            pool = [p["member_id"] for p in players if p["team_id"] is None]
            if order is None:
                chosen = [row[0] for row in db.execute(
                    "SELECT member_id FROM live_lots WHERE event_id=? AND status='QUEUED' ORDER BY sequence", (event_id,)
                )] if previous else list(pool)
                if not previous:
                    # One uniform permutation is equivalent to drawing without
                    # replacement. Persist it so settings saves/reconnects do not
                    # give another chance to draw a preferred next player.
                    secrets.SystemRandom().shuffle(chosen)
            else:
                # Explicit order remains available for deterministic service
                # fixtures; the application uses the random default.
                chosen = [integer(mid, "선수 번호") for mid in order]
            if len(chosen) != len(pool) or set(chosen) != set(pool):
                raise ValueError("경매 순서에는 미배정 선수 전원을 한 번씩 지정해 주세요.")
            stamp = self._now(db)
            settings_updated = _iso(stamp)
            if previous:
                settings_updated = max(settings_updated, (datetime.fromisoformat(previous["updated_at"]) + timedelta(microseconds=1)).isoformat(timespec="microseconds"))
            if team_budgets is not None:
                db.executemany("UPDATE competition_teams SET budget=? WHERE id=? AND event_id=?",
                               [(amount, team_id, event_id) for team_id, amount in budgets.items()])
            if previous:
                # A reset returns to READY while earlier bids still reference
                # cancelled lots. Preserve every ID and only reorder the queue
                # when an explicit deterministic service order is supplied.
                if order is not None:
                    queue = {row["member_id"]: row["id"] for row in db.execute(
                        "SELECT id,member_id FROM live_lots WHERE event_id=? AND status='QUEUED'", (event_id,))}
                    sequence = db.execute("SELECT COALESCE(MAX(sequence),-1)+1 FROM live_lots WHERE event_id=?", (event_id,)).fetchone()[0]
                    db.executemany("UPDATE live_lots SET sequence=? WHERE id=? AND event_id=?",
                                   [(sequence + index, queue[mid], event_id) for index, mid in enumerate(chosen)])
                db.execute("UPDATE live_sessions SET bid_seconds=?,reset_on_bid=?,transition_seconds=?,updated_at=? WHERE event_id=?", (bid_seconds, int(reset_on_bid), TRANSITION_SECONDS, settings_updated, event_id))
            else:
                db.execute("INSERT INTO live_sessions(event_id,status,bid_seconds,reset_on_bid,created_at,updated_at) VALUES(?,'READY',?,?,?,?)", (event_id, bid_seconds, int(reset_on_bid), _iso(stamp), _iso(stamp)))
                db.executemany("INSERT INTO live_lots(event_id,member_id,sequence,status) VALUES(?,?,?,'QUEUED')", [(event_id, mid, index) for index, mid in enumerate(chosen)])
            self._log(db, event_id, "CONFIGURE", {"bid_seconds": bid_seconds, "extension_seconds": BID_EXTENSION_SECONDS,
                "max_remaining_seconds": bid_seconds, "transition_seconds": TRANSITION_SECONDS,
                "order": chosen, "team_budgets_before": previous_budgets, "team_budgets": budgets}, stamp, actor)
        return self.get_state(event_id)

    def _open_next(self, db, session, stamp):
        event_id = session["event_id"]
        lot = db.execute("SELECT * FROM live_lots WHERE event_id=? AND status='QUEUED' ORDER BY sequence LIMIT 1", (event_id,)).fetchone()
        if not lot:
            return False
        seconds = _effective_bid_seconds(session["bid_seconds"])
        db.execute("UPDATE live_lots SET status='OPEN',opened_at=?,closes_at=? WHERE id=?", (stamp, stamp + seconds, lot["id"]))
        db.execute("UPDATE live_sessions SET status='RUNNING',current_lot_id=?,next_at=NULL,bid_seconds=?,transition_seconds=?,updated_at=? WHERE event_id=?", (lot["id"], seconds, TRANSITION_SECONDS, _iso(stamp), event_id))
        db.execute("UPDATE competition_events SET current_player_id=? WHERE id=?", (lot["member_id"], event_id))
        self._log(db, event_id, "LOT_OPEN", {"member_id": lot["member_id"], "attempt": lot["attempt"]}, stamp, lot_id=lot["id"])
        return True

    def start(self, token, event_id, *, return_state=True):
        with self.core.transaction() as db:
            actor = self.competition._authorize(db, token, event_id)
            session = self._session(db, event_id)
            event = self.competition._event(db, event_id)
            if session["status"] != "READY" or event["status"] != "AUCTION_READY":
                raise ValueError("준비가 완료된 경매만 시작할 수 있습니다.")
            if db.execute("SELECT 1 FROM competition_players p JOIN members m ON m.id=p.member_id WHERE p.event_id=? AND p.participation_status='SELECTED' AND m.status<>'APPROVED'", (event_id,)).fetchone():
                raise ValueError("참가자의 승인 및 참가 상태를 확인해 주세요.")
            stamp = self._now(db)
            db.execute("UPDATE competition_events SET status='AUCTION' WHERE id=?", (event_id,))
            if not self._open_next(db, session, stamp):
                raise ValueError("경매에 등록된 선수가 없습니다.")
            self._log(db, event_id, "START", {}, stamp, actor)
        return self.get_state(event_id) if return_state else None

    def _bid_actor(self, db, token):
        actor = self.core.session(token, db)
        return self._validate_bid_actor(actor)

    @staticmethod
    def _validate_bid_actor(actor):
        if not actor or actor["member_id"] is None:
            raise PermissionError("승인 회원과 연결된 팀장 계정으로 로그인해 주세요.")
        # The fresh session query already joins this member inside the writer
        # transaction. A score/history aggregation is unnecessary for bidding.
        if actor["member_status"] != "APPROVED":
            raise PermissionError("승인된 팀장만 입찰할 수 있습니다.")
        return actor

    def _bidder(self, db, token, event_id):
        actor = self._bid_actor(db, token)
        teams = list(db.execute("SELECT t.* FROM competition_teams t JOIN competition_players p ON p.event_id=t.event_id AND p.member_id=t.captain_id WHERE t.event_id=? AND t.captain_id=? AND p.participation_status='SELECTED' AND p.team_id=t.id", (event_id, actor["member_id"])))
        if len(teams) != 1:
            raise PermissionError("이 대회의 팀장만 본인 팀으로 입찰할 수 있습니다.")
        return actor, dict(teams[0])

    def _capacity(self, db, team, amount):
        count, spent = db.execute("SELECT COUNT(*),COALESCE(SUM(price),0) FROM competition_players WHERE team_id=?", (team["id"],)).fetchone()
        self._check_capacity(team, amount, count, spent)

    @staticmethod
    def _check_capacity(team, amount, count, spent):
        if count >= 5:
            raise ValueError("팀 정원 5명을 모두 채웠습니다.")
        if amount > team["budget"] - spent:
            raise ValueError("팀의 남은 예산을 초과했습니다.")

    @contextmanager
    def _bid_transaction(self, token, event_id, lot_id, request_id, *, receipt_only=False):
        if not self.core.is_postgres:
            with self.core.transaction() as db:
                yield db, None
            return
        # This method owns the connection just as Core.transaction does. The
        # explicit adapter API starts BEGIN + lock + reads without an earlier
        # synchronization. Never commit before Python validation and all writes.
        with closing(self.core.connect()) as db:
            try:
                snapshot = self._bid_snapshot(db, token, event_id, lot_id, request_id,
                                              receipt_only=receipt_only)
                yield db, snapshot
                # Normal PG bids finalize their writes and COMMIT together.
                # Replay/resolve paths still own an open read transaction.
                if db.in_transaction:
                    db.commit()
            except BaseException:
                if db.in_transaction:
                    db.rollback()
                raise

    def _bid_snapshot(self, db, token, event_id, lot_id, request_id, *, receipt_only=False):
        """Read fresh authorization and validation after the writer lock.

        Team lookup uses the same token's account in SQL, removing the need to
        wait for an actor ID before queuing reads. Authenticate before using any
        returned data. The final DB clock and receipts keep their original
        authority; no display cache or pre-lock timestamp participates.
        """
        if not token:
            self._validate_bid_actor(None)
        actor_query = session_query(token)
        token_hash = actor_query[1][0]
        statements = [
            actor_query,
            ("""SELECT t.* FROM competition_teams t
                JOIN competition_players p ON p.event_id=t.event_id AND p.member_id=t.captain_id
                JOIN accounts a ON a.member_id=t.captain_id JOIN sessions s ON s.account_id=a.id
                WHERE t.event_id=? AND s.token_hash=?
                AND p.participation_status='SELECTED' AND p.team_id=t.id""", (event_id, token_hash)),
            ("SELECT * FROM live_bids WHERE request_id=?", (request_id,)),
        ]
        if not receipt_only:
            statements.extend([
            ("SELECT * FROM live_sessions WHERE event_id=?", (event_id,)),
            ("SELECT status FROM competition_events WHERE id=?", (event_id,)),
            ("SELECT * FROM live_lots WHERE id=? AND event_id=?", (lot_id, event_id)),
            ("""SELECT p.team_id,p.participation_status,m.status AS member_status
                FROM competition_players p JOIN members m ON m.id=p.member_id
                JOIN live_lots l ON l.event_id=p.event_id AND l.member_id=p.member_id
                WHERE l.id=? AND l.event_id=?""", (lot_id, event_id)),
            ("""SELECT p.team_id,p.price FROM competition_players p
                JOIN competition_teams t ON t.id=p.team_id
                JOIN accounts a ON a.member_id=t.captain_id JOIN sessions s ON s.account_id=a.id
                WHERE t.event_id=? AND s.token_hash=?""", (event_id, token_hash)),
            ])
            if not self._injected_clock:
                statements.append(("SELECT EXTRACT(EPOCH FROM pg_catalog.clock_timestamp())::double precision", None))
        batches = db.begin_writer_batches(statements)
        actor_rows, teams, receipts = batches[:3]
        actor = self._validate_bid_actor(dict(actor_rows[0]) if actor_rows else None)
        # The bound session cutoff was prepared before a possibly long lock
        # wait. Recheck expiry after synchronization, retaining Core.session's
        # UTC timestamp semantics without an extra authentication round trip.
        if actor["expires_at"] <= datetime.now(timezone.utc).isoformat():
            self._validate_bid_actor(None)
        if len(teams) != 1:
            raise PermissionError("이 대회의 팀장만 본인 팀으로 입찰할 수 있습니다.")
        team = dict(teams[0])
        first = lambda rows: dict(rows[0]) if rows else None
        result = {"actor": actor, "team": team, "receipt": first(receipts)}
        if not receipt_only:
            stamp = self._clock() if self._injected_clock else float(batches.pop()[0][0])
            sessions, events, lots, players, roster = batches[3:]
            roster = [row for row in roster if row["team_id"] == team["id"]]
            result.update(session=first(sessions), event=first(events), lot=first(lots),
                          player=first(players), count=len(roster),
                          spent=sum(row["price"] for row in roster), stamp=stamp)
        return result

    @staticmethod
    def _receipt(row, replayed):
        return {"id": row["id"], "accepted": True, "event_id": row["event_id"], "lot_id": row["lot_id"],
                "team_id": row["team_id"], "amount": row["amount"], "highest_bid": row["amount"],
                "closes_at": row["closes_at"], "created_at": row["created_at"], "replayed": replayed}

    def place_bid(self, token, event_id, lot_id, amount, request_id):
        amount = integer(amount, "입찰가")
        if amount < 0:
            raise ValueError("입찰가는 0 이상의 정수로 입력해 주세요.")
        event_id, lot_id = integer(event_id, "대회 번호"), integer(lot_id, "경매 번호")
        try:
            request_id = str(uuid.UUID(str(request_id)))
        except (ValueError, AttributeError) as exc:
            raise ValueError("유효한 입찰 요청 번호(UUID)가 필요합니다.") from exc
        with self._bid_transaction(token, event_id, lot_id, request_id) as (db, snapshot):
            actor, team = (snapshot["actor"], snapshot["team"]) if snapshot is not None else self._bidder(db, token, event_id)
            payload = [event_id, lot_id, amount, actor["id"], actor["member_id"], team["id"]]
            fingerprint = hashlib.sha256(json.dumps(payload).encode()).hexdigest()
            existing = snapshot["receipt"] if snapshot is not None else db.execute("SELECT * FROM live_bids WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise ValueError("같은 요청 번호로 다른 입찰을 전송할 수 없습니다.")
                return self._receipt(existing, True)
            if snapshot is None:
                session = self._session(db, event_id)
                event = self.competition._event(db, event_id)
                lot = db.execute("SELECT * FROM live_lots WHERE id=? AND event_id=?", (lot_id, event_id)).fetchone()
                stamp = self._now(db)  # Read after obtaining the write lock, never before waiting.
            else:
                session, event, lot, stamp = (snapshot[key] for key in ("session", "event", "lot", "stamp"))
                if session is None:
                    raise ValueError("실시간 경매 설정을 먼저 저장해 주세요.")
                if event is None:
                    raise ValueError("대회를 찾을 수 없습니다.")
            if event["status"] != "AUCTION" or session["status"] != "RUNNING" or session["current_lot_id"] != lot_id or not lot or lot["status"] != "OPEN":
                raise ValueError("현재 진행 중인 선수에게만 입찰할 수 있습니다.")
            if stamp >= lot["closes_at"]:
                raise ValueError("입찰 시간이 마감되었습니다.")
            if lot["highest_bid"] is not None and amount <= lot["highest_bid"]:
                raise ValueError("현재 최고 입찰가보다 높은 금액을 입력해 주세요.")
            player = snapshot["player"] if snapshot is not None else db.execute("""SELECT p.team_id,p.participation_status,m.status AS member_status
                FROM competition_players p JOIN members m ON m.id=p.member_id
                WHERE p.event_id=? AND p.member_id=?""", (event_id, lot["member_id"])).fetchone()
            if not player or player["team_id"] is not None or player["participation_status"] != "SELECTED" or player["member_status"] != "APPROVED":
                raise ValueError("현재 선수의 참가 상태를 확인해 주세요.")
            if snapshot is None:
                self._capacity(db, team, amount)
            else:
                self._check_capacity(team, amount, snapshot["count"], snapshot["spent"])
            # Add five seconds, capped at the selected initial duration.
            # Old saved reset_on_bid=False values do not disable this rule.
            # All validation and replay checks above run before this extension.
            deadline = min(lot["closes_at"] + BID_EXTENSION_SECONDS, stamp + _effective_bid_seconds(session["bid_seconds"]))
            writes = [
                ("UPDATE live_lots SET highest_team_id=?,highest_bid=?,closes_at=? WHERE id=?", (team["id"], amount, deadline, lot_id)),
                ("INSERT INTO live_bids(request_id,fingerprint,event_id,lot_id,team_id,account_id,member_id,amount,closes_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (request_id, fingerprint, event_id, lot_id, team["id"], actor["id"], actor["member_id"], amount, deadline, _iso(stamp))),
                ("UPDATE live_sessions SET updated_at=? WHERE event_id=?", (_iso(stamp), event_id)),
            ]
            detail = {"team_id": team["id"], "team_name": team["name"], "amount": amount}
            # Prepare the receipt before submitting COMMIT; a construction error
            # must not be reported as a rejected bid after it has been saved.
            receipt = self._receipt({"id": None, "event_id": event_id, "lot_id": lot_id,
                "team_id": team["id"], "amount": amount, "closes_at": deadline,
                "created_at": _iso(stamp)}, False)
            if self.core.is_postgres:
                writes.append(("INSERT INTO live_events(event_id,lot_id,actor_id,type,detail,created_at) VALUES(?,?,?,?,?,?)",
                               (event_id, lot_id, actor["id"], "BID", json.dumps(detail, ensure_ascii=False), _iso(stamp))))
                # No write depends on the generated bid ID. Return it only
                # after the final batch has confirmed a successful COMMIT.
                bid_id = db.commit_bid_batch(writes)[1].lastrowid
            else:
                db.execute(*writes[0])
                bid_id = db.execute(*writes[1]).lastrowid
                db.execute(*writes[2])
                self._log(db, event_id, "BID", detail, stamp, actor, lot_id)
            receipt["id"] = bid_id
            return receipt

    def resolve_bid(self, token, event_id, lot_id, amount, request_id):
        """Resolve an uncertain submission without submitting or extending a bid.

        Use the writer lock so a missing receipt is conclusive even when the
        previous connection was lost during COMMIT. This is used only while a
        client has an unresolved request, never for ordinary polling.
        """
        event_id, lot_id, amount = (integer(event_id), integer(lot_id), integer(amount))
        try:
            request_id = str(uuid.UUID(str(request_id)))
        except (ValueError, AttributeError) as exc:
            raise ValueError("유효한 입찰 요청 번호(UUID)가 필요합니다.") from exc
        with self._bid_transaction(token, event_id, lot_id, request_id, receipt_only=True) as (db, snapshot):
            actor, team = (snapshot["actor"], snapshot["team"]) if snapshot is not None else self._bidder(db, token, event_id)
            row = snapshot["receipt"] if snapshot is not None else db.execute("SELECT * FROM live_bids WHERE request_id=?", (request_id,)).fetchone()
            if row is None:
                return None
            fingerprint = hashlib.sha256(json.dumps(
                [event_id, lot_id, amount, actor["id"], actor["member_id"], team["id"]]
            ).encode()).hexdigest()
            if row["fingerprint"] != fingerprint:
                raise ValueError("접수 확인 요청이 원래 입찰과 일치하지 않습니다.")
            return self._receipt(row, True)

    def _complete_or_wait(self, db, session, stamp):
        event_id = session["event_id"]
        remaining = db.execute("SELECT COUNT(*) FROM competition_players WHERE event_id=? AND team_id IS NULL AND participation_status='SELECTED'", (event_id,)).fetchone()[0]
        if not remaining:
            counts = [r[0] for r in db.execute("SELECT COUNT(p.id) FROM competition_teams t LEFT JOIN competition_players p ON p.team_id=t.id AND p.participation_status='SELECTED' WHERE t.event_id=? GROUP BY t.id", (event_id,))]
            if counts and all(count == 5 for count in counts):
                db.execute("UPDATE live_sessions SET status='COMPLETED',next_at=NULL,updated_at=? WHERE event_id=?", (_iso(stamp), event_id))
                db.execute("UPDATE competition_events SET status='BRACKET_SETUP',current_player_id=NULL WHERE id=?", (event_id,))
                self._log(db, event_id, "COMPLETED", {"next_stage": "BRACKET_SETUP"}, stamp)
                return
        queued = db.execute("SELECT 1 FROM live_lots WHERE event_id=? AND status='QUEUED'", (event_id,)).fetchone()
        db.execute("UPDATE live_sessions SET status='WAITING',next_at=?,updated_at=? WHERE event_id=?", (stamp + TRANSITION_SECONDS if queued else None, _iso(stamp), event_id))
        db.execute("UPDATE competition_events SET current_player_id=NULL WHERE id=?", (event_id,))

    def _close_lot(self, db, session, stamp):
        lot = db.execute("SELECT * FROM live_lots WHERE id=? AND status='OPEN'", (session["current_lot_id"],)).fetchone()
        if not lot:
            raise ValueError("진행 중인 경매 기록이 일치하지 않습니다.")
        event_id = session["event_id"]
        sold, failure = False, None
        if lot["highest_team_id"] is not None:
            try:
                team = self.competition._team(db, event_id, lot["highest_team_id"])
                player = self.competition._player(db, event_id, lot["member_id"])
                captain = self.competition._player(db, event_id, team["captain_id"])
                if player["team_id"] is not None or player["participation_status"] != "SELECTED" or captain["participation_status"] != "SELECTED" or captain["team_id"] != team["id"]:
                    raise ValueError("선수 또는 팀장의 참가 상태가 변경되었습니다.")
                if any(self.core.get_member(mid, db)["status"] != "APPROVED" for mid in (player["member_id"], captain["member_id"])):
                    raise ValueError("선수 또는 팀장의 회원 승인이 해제되었습니다.")
                self._capacity(db, team, lot["highest_bid"])
                self.competition._assign(db, event_id, lot["member_id"], team["id"], lot["highest_bid"])
                sold = True
            except ValueError as exc:
                failure = str(exc)
        state = "SOLD" if sold else "UNSOLD"
        db.execute("UPDATE live_lots SET status=?,closed_at=? WHERE id=?", (state, stamp, lot["id"]))
        if not sold:
            db.execute("UPDATE competition_players SET state='UNSOLD' WHERE event_id=? AND member_id=? AND team_id IS NULL", (event_id, lot["member_id"]))
        self._log(db, event_id, state, {"member_id": lot["member_id"], "team_id": lot["highest_team_id"] if sold else None, "amount": lot["highest_bid"] if sold else None, "reason": failure}, stamp, lot_id=lot["id"])
        self._complete_or_wait(db, session, stamp)

    def _advance(self, db, session, stamp):
        event = self.competition._event(db, session["event_id"])
        if event["status"] == "CANCELLED":
            db.execute("UPDATE live_lots SET status='CANCELLED',closed_at=? WHERE event_id=? AND status IN ('OPEN','QUEUED')", (stamp, session["event_id"]))
            db.execute("UPDATE live_sessions SET status='CANCELLED',next_at=NULL,updated_at=? WHERE event_id=?", (_iso(stamp), session["event_id"]))
            self._log(db, session["event_id"], "CANCELLED", {}, stamp)
            return True
        if event["status"] != "AUCTION":
            return False
        if session["status"] == "RUNNING":
            lot = db.execute("SELECT closes_at FROM live_lots WHERE id=?", (session["current_lot_id"],)).fetchone()
            if lot and lot["closes_at"] <= stamp:
                self._close_lot(db, session, stamp)
                return True
        elif session["status"] == "WAITING" and session["next_at"] is not None and session["next_at"] <= stamp:
            return self._open_next(db, session, stamp)
        return False

    def _worker_read(self, query, parameters=None):
        # Two independent lifecycle reads retain their existing ordering. Each
        # owns one short read-only pipeline instead of BEGIN/SELECT/close trips.
        if self.core.is_postgres:
            with closing(self.core.connect()) as db:
                return db.fetch_snapshot_batches([(query, parameters)])[0]
        with self.core.read_snapshot() as db:
            return db.execute(query, parameters or ()).fetchall()

    def _due_candidates(self):
        """Skip idle writer transactions; these IDs are hints, never authority."""
        if self.core.is_postgres:
            statements = [("""SELECT s.event_id,s.status,e.status AS event_status,l.closes_at,s.next_at
                FROM live_sessions s JOIN competition_events e ON e.id=s.event_id
                LEFT JOIN live_lots l ON l.id=s.current_lot_id AND l.event_id=s.event_id
                WHERE s.status IN ('READY','RUNNING','WAITING','PAUSED') AND (
                    e.status='CANCELLED' OR (e.status='AUCTION' AND (
                        (s.status='RUNNING' AND l.closes_at IS NOT NULL) OR
                        (s.status='WAITING' AND s.next_at IS NOT NULL))))""", None)]
            if not self._injected_clock:
                statements.append(("SELECT EXTRACT(EPOCH FROM pg_catalog.clock_timestamp())::double precision", None))
            with closing(self.core.connect()) as db:
                batches = db.fetch_snapshot_batches(statements)
            stamp = self._clock() if self._injected_clock else float(batches[1][0][0])
            return [row["event_id"] for row in batches[0] if row["event_status"] == "CANCELLED"
                    or (row["status"] == "RUNNING" and row["closes_at"] <= stamp)
                    or (row["status"] == "WAITING" and row["next_at"] <= stamp)]
        return [row[0] for row in self._worker_read("""SELECT s.event_id
                FROM live_sessions s JOIN competition_events e ON e.id=s.event_id
                LEFT JOIN live_lots l ON l.id=s.current_lot_id AND l.event_id=s.event_id
                CROSS JOIN (SELECT ? AS stamp) c
                WHERE s.status IN ('READY','RUNNING','WAITING','PAUSED') AND (
                    e.status='CANCELLED' OR (e.status='AUCTION' AND (
                        (s.status='RUNNING' AND l.closes_at<=c.stamp) OR
                        (s.status='WAITING' AND s.next_at<=c.stamp))))""", (self._clock(),))]

    def settle_due(self):
        """Apply only server-determined deadlines; callers cannot select a winner."""
        candidates = self._due_candidates()
        if not candidates:
            return []
        changed = []
        with self.core.transaction() as db:
            # A bid, pause or reset may have changed a candidate while the lock
            # was pending. Re-read its complete state and the DB clock here.
            placeholders = ",".join("?" for _ in candidates)
            sessions = [dict(r) for r in db.execute(f"SELECT * FROM live_sessions WHERE event_id IN ({placeholders}) AND status IN ('READY','RUNNING','WAITING','PAUSED') ORDER BY event_id", candidates)]
            for session in sessions:
                if self._advance(db, session, self._now(db)):
                    changed.append(session["event_id"])
        return changed

    def pause(self, token, event_id, *, return_state=True):
        with self.core.transaction() as db:
            actor = self.competition._authorize(db, token, event_id)
            session = self._session(db, event_id)
            if session["status"] not in ("RUNNING", "WAITING"):
                raise ValueError("진행 중인 경매만 일시정지할 수 있습니다.")
            stamp = self._now(db)
            self._advance(db, session, stamp)
            session = self._session(db, event_id)
            if session["status"] in ("RUNNING", "WAITING"):
                deadline = db.execute("SELECT closes_at FROM live_lots WHERE id=?", (session["current_lot_id"],)).fetchone()[0] if session["status"] == "RUNNING" else session["next_at"]
                limit = _effective_bid_seconds(session["bid_seconds"]) if session["status"] == "RUNNING" else TRANSITION_SECONDS
                remaining = min(limit, max(0.0, deadline - stamp)) if deadline is not None else None
                db.execute("UPDATE live_sessions SET status='PAUSED',paused_phase=?,pause_remaining=?,updated_at=? WHERE event_id=?", (session["status"], remaining, _iso(stamp), event_id))
                self._log(db, event_id, "PAUSE", {"phase": session["status"], "remaining_seconds": remaining}, stamp, actor)
        return self.get_state(event_id) if return_state else None

    def resume(self, token, event_id, *, return_state=True):
        with self.core.transaction() as db:
            actor = self.competition._authorize(db, token, event_id)
            session = self._session(db, event_id)
            if session["status"] != "PAUSED" or self.competition._event(db, event_id)["status"] != "AUCTION":
                raise ValueError("일시정지한 경매만 재개할 수 있습니다.")
            stamp = self._now(db)
            limit = _effective_bid_seconds(session["bid_seconds"]) if session["paused_phase"] == "RUNNING" else TRANSITION_SECONDS
            deadline = stamp + min(limit, max(0, session["pause_remaining"])) if session["pause_remaining"] is not None else None
            if session["paused_phase"] == "RUNNING":
                db.execute("UPDATE live_lots SET closes_at=? WHERE id=? AND status='OPEN'", (deadline, session["current_lot_id"]))
            db.execute("UPDATE live_sessions SET status=?,next_at=?,paused_phase=NULL,pause_remaining=NULL,updated_at=? WHERE event_id=?", (session["paused_phase"], deadline if session["paused_phase"] == "WAITING" else None, _iso(stamp), event_id))
            self._log(db, event_id, "RESUME", {"phase": session["paused_phase"], "deadline": deadline}, stamp, actor)
        return self.get_state(event_id) if return_state else None

    def retry_unsold(self, token, event_id, member_ids=None):
        with self.core.transaction() as db:
            actor = self.competition._authorize(db, token, event_id)
            session = self._session(db, event_id)
            round_finished = (session["status"] == "WAITING" and session["next_at"] is None) or (
                session["status"] == "PAUSED" and session["paused_phase"] == "WAITING" and session["pause_remaining"] is None)
            has_remaining = db.execute("SELECT 1 FROM live_lots WHERE event_id=? AND status IN ('QUEUED','OPEN') LIMIT 1", (event_id,)).fetchone()
            if not round_finished or has_remaining or self.competition._event(db, event_id)["status"] != "AUCTION":
                raise ValueError("등록된 순서를 모두 진행한 뒤 유찰 선수를 재경매할 수 있습니다.")
            available = [r[0] for r in db.execute("SELECT member_id FROM competition_players WHERE event_id=? AND team_id IS NULL AND state='UNSOLD' AND participation_status='SELECTED' ORDER BY id", (event_id,))]
            chosen = available if member_ids is None else [integer(mid, "선수 번호") for mid in member_ids]
            if not chosen or len(chosen) != len(set(chosen)) or not set(chosen).issubset(available):
                raise ValueError("미배정 유찰 선수를 한 번씩 선택해 주세요.")
            secrets.SystemRandom().shuffle(chosen)
            stamp = self._now(db)
            sequence = db.execute("SELECT COALESCE(MAX(sequence),-1)+1 FROM live_lots WHERE event_id=?", (event_id,)).fetchone()[0]
            for index, member_id in enumerate(chosen):
                if self.core.get_member(member_id, db)["status"] != "APPROVED":
                    raise ValueError("승인된 회원만 재경매할 수 있습니다.")
                attempt = db.execute("SELECT COALESCE(MAX(attempt),0)+1 FROM live_lots WHERE event_id=? AND member_id=?", (event_id, member_id)).fetchone()[0]
                db.execute("INSERT INTO live_lots(event_id,member_id,sequence,attempt,status) VALUES(?,?,?,?,'QUEUED')", (event_id, member_id, sequence + index, attempt))
                db.execute("UPDATE competition_players SET state='AVAILABLE' WHERE event_id=? AND member_id=?", (event_id, member_id))
            db.execute("UPDATE live_sessions SET status='WAITING',next_at=?,paused_phase=NULL,pause_remaining=NULL,updated_at=? WHERE event_id=?", (stamp + TRANSITION_SECONDS, _iso(stamp), event_id))
            self._log(db, event_id, "RETRY_UNSOLD", {"member_ids": chosen}, stamp, actor)
        return self.get_state(event_id)

    def _reset_preview(self, db, event_id):
        session = self._session(db, event_id)
        event = self.competition._event(db, event_id)
        allowed = ((event["status"] == "AUCTION_READY" and session["status"] == "READY")
                   or (event["status"] == "AUCTION" and session["status"] in ("RUNNING", "WAITING", "PAUSED"))
                   or (event["status"] == "BRACKET_SETUP" and session["status"] == "COMPLETED"))
        if event["kind"] != "AUCTION" or not allowed:
            raise ValueError("대진 생성 전의 준비·진행 경매만 초기화할 수 있습니다.")
        if (db.execute("SELECT 1 FROM competition_games WHERE event_id=?", (event_id,)).fetchone()
                or db.execute("SELECT 1 FROM games WHERE tournament_id=?", (str(event_id),)).fetchone()
                or db.execute("SELECT 1 FROM award_batches WHERE event_id=?", (str(event_id),)).fetchone()):
            raise ValueError("대진·경기·우승 보상이 있는 경매는 초기화할 수 없습니다.")
        teams = [dict(row) for row in db.execute("SELECT * FROM competition_teams WHERE event_id=? ORDER BY id", (event_id,))]
        roster = [dict(row) for row in db.execute(
            "SELECT p.*,m.status AS member_status FROM competition_players p JOIN members m ON m.id=p.member_id WHERE p.event_id=? ORDER BY p.id", (event_id,))]
        selected = [player for player in roster if player["participation_status"] == "SELECTED"]
        if (len(teams) not in (4, 6, 8) or len(selected) != len(teams) * 5
                or any(player["member_status"] != "APPROVED" for player in selected)):
            raise ValueError("팀당 5명의 승인된 확정 참가자와 팀장을 확인해 주세요.")
        captains = {team["captain_id"]: team["id"] for team in teams}
        if len(captains) != len(teams):
            raise ValueError("서로 다른 팀장이 필요합니다.")
        for member_id, team_id in captains.items():
            captain = next((player for player in selected if player["member_id"] == member_id), None)
            if not captain or captain["team_id"] != team_id or captain["price"] != 0:
                raise ValueError("팀장 배정과 시작 금액을 확인해 주세요.")
        team_ids = {team["id"] for team in teams}
        if any(player["price"] < 0 or (player["team_id"] is not None and player["team_id"] not in team_ids) for player in roster):
            raise ValueError("선수 배정과 낙찰 금액이 이 경매와 일치하지 않습니다.")
        if any(player["participation_status"] != "SELECTED" and (player["team_id"] is not None or player["price"]) for player in roster):
            raise ValueError("참가 명단 밖의 선수 배정이 남아 있습니다. 명단을 먼저 확인해 주세요.")
        lots = [dict(row) for row in db.execute("SELECT * FROM live_lots WHERE event_id=? ORDER BY sequence", (event_id,))]
        bid_count, last_bid_id = db.execute("SELECT COUNT(*),COALESCE(MAX(id),0) FROM live_bids WHERE event_id=?", (event_id,)).fetchone()
        last_event_id = db.execute("SELECT COALESCE(MAX(id),0) FROM live_events WHERE event_id=?", (event_id,)).fetchone()[0]
        balances = []
        for team in teams:
            assigned = [player for player in selected if player["team_id"] == team["id"]]
            balances.append({"team_id": team["id"], "team_name": team["name"],
                             "before": team["budget"] - sum(player["price"] for player in assigned),
                             "after": team["budget"], "players_before": len(assigned), "players_after": 1})
        snapshot = {"event": event, "session": session, "teams": teams, "roster": roster,
                    "lots": lots, "bid_count": bid_count, "last_bid_id": last_bid_id, "last_event_id": last_event_id}
        fingerprint = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
        return {"event_id": event_id, "title": event["title"], "status": session["status"],
                "workflow_status": event["status"], "bid_seconds": _effective_bid_seconds(session["bid_seconds"]),
                "participant_count": len(selected), "captain_count": len(captains),
                "reset_player_count": len(selected) - len(captains),
                "sold_count": sum(player["team_id"] is not None and player["member_id"] not in captains for player in selected),
                "bid_count": bid_count, "refund_total": sum(player["price"] for player in selected),
                "team_balances": balances, "fingerprint": fingerprint}, snapshot

    def preview_reset(self, token, event_id):
        """Read the exact state an authorized host will return to READY."""
        event_id = integer(event_id, "경매 번호")
        with self.core.read_snapshot() as db:
            self.competition._authorize(db, token, event_id)
            return self._reset_preview(db, event_id)[0]

    def reset(self, token, event_id, *, reason, request_id, expected_fingerprint):
        """Refund all sales, preserve history and append one shuffled queue."""
        event_id = integer(event_id, "경매 번호")
        reason = str(reason).strip()
        if not reason or len(reason) > 1000:
            raise ValueError("경매 초기화 사유를 1~1,000자로 입력해 주세요.")
        try:
            parsed = uuid.UUID(str(request_id))
            if not parsed.int:
                raise ValueError()
            request_id = str(parsed)
        except (ValueError, AttributeError):
            raise ValueError("유효한 초기화 요청 번호(UUID)가 필요합니다.") from None
        if (not isinstance(expected_fingerprint, str) or len(expected_fingerprint) != 64
                or any(char not in "0123456789abcdef" for char in expected_fingerprint)):
            raise ValueError("초기화할 경매를 다시 미리보기해 주세요.")
        with self.core.transaction() as db:
            actor = self.competition._authorize(db, token, event_id)
            payload_hash = hashlib.sha256(json.dumps(
                [actor["id"], event_id, reason, expected_fingerprint]).encode()).hexdigest()
            pattern = '%"request_id": "' + request_id + '"%'
            for row in db.execute("SELECT detail FROM live_events WHERE type='RESET' AND detail LIKE ?", (pattern,)):
                previous = json.loads(row["detail"])
                if previous.get("request_id") == request_id:
                    if previous.get("payload_hash") != payload_hash:
                        raise ValueError("같은 요청 번호로 다른 경매 초기화를 전송할 수 없습니다.")
                    return {**previous["result"], "replayed": True}
            preview, snapshot = self._reset_preview(db, event_id)
            if preview["fingerprint"] != expected_fingerprint:
                raise ValueError("미리보기 이후 경매 상태가 바뀌었습니다. 다시 확인해 주세요.")
            stamp = self._now(db)
            captains = {team["captain_id"]: team["id"] for team in snapshot["teams"]}
            pool = [player["member_id"] for player in snapshot["roster"]
                    if player["participation_status"] == "SELECTED" and player["member_id"] not in captains]
            secrets.SystemRandom().shuffle(pool)
            sequence = max((lot["sequence"] for lot in snapshot["lots"]), default=-1) + 1
            attempts = {}
            for lot in snapshot["lots"]:
                attempts[lot["member_id"]] = max(attempts.get(lot["member_id"], 0), lot["attempt"])
            db.execute("UPDATE live_lots SET status='CANCELLED',closed_at=COALESCE(closed_at,?) WHERE event_id=? AND status<>'CANCELLED'", (stamp, event_id))
            db.executemany("UPDATE competition_players SET team_id=?,price=0,state=? WHERE event_id=? AND member_id=? AND participation_status='SELECTED'",
                           [(captains.get(player["member_id"]), "ASSIGNED" if player["member_id"] in captains else "AVAILABLE", event_id, player["member_id"])
                            for player in snapshot["roster"] if player["participation_status"] == "SELECTED"])
            lot_ids = [db.execute("INSERT INTO live_lots(event_id,member_id,sequence,attempt,status) VALUES(?,?,?,?,'QUEUED')",
                                 (event_id, member_id, sequence + index, attempts.get(member_id, 0) + 1)).lastrowid
                       for index, member_id in enumerate(pool)]
            updated = max(_iso(stamp), (datetime.fromisoformat(snapshot["session"]["updated_at"]) + timedelta(microseconds=1)).isoformat(timespec="microseconds"))
            db.execute("UPDATE live_sessions SET status='READY',current_lot_id=NULL,next_at=NULL,paused_phase=NULL,pause_remaining=NULL,updated_at=? WHERE event_id=?", (updated, event_id))
            db.execute("UPDATE competition_events SET status='AUCTION_READY',current_player_id=NULL WHERE id=?", (event_id,))
            result = {"event_id": event_id, "status": "READY", "workflow_status": "AUCTION_READY",
                      "member_ids": pool, "lot_ids": lot_ids, "refund_total": preview["refund_total"], "replayed": False}
            # Bids and their FK targets survive unchanged. The audit also keeps
            # each lot's original outcome before its state becomes CANCELLED.
            # The frequently polled live feed keeps only a bounded receipt;
            # the full historical state belongs in the durable domain audit.
            detail = {"request_id": request_id, "payload_hash": payload_hash, "reason": reason,
                      "team_balances": preview["team_balances"], "sold_count": preview["sold_count"],
                      "bid_count": preview["bid_count"], "result": result}
            self._log(db, event_id, "RESET", detail, stamp, actor)
            self.competition._audit(db, event_id, actor, "RESET", json.dumps(
                {**detail, "before": snapshot}, ensure_ascii=False))
            return result

    def _sale_correction_preview(self, db, event_id, lot_id, team_id, amount):
        session = self._session(db, event_id)
        event = self.competition._event(db, event_id)
        paused = session["status"] == "PAUSED" and event["status"] == "AUCTION"
        completed = session["status"] == "COMPLETED" and event["status"] == "BRACKET_SETUP"
        if event["kind"] != "AUCTION" or not (paused or completed):
            raise ValueError("경매를 일시정지한 뒤 정정해 주세요. 완료된 경매는 대진 생성 전까지만 정정할 수 있습니다.")
        if paused and session["paused_phase"] not in ("RUNNING", "WAITING"):
            raise ValueError("일시정지된 경매의 진행 단계가 일치하지 않습니다.")
        if (db.execute("SELECT 1 FROM competition_games WHERE event_id=?", (event_id,)).fetchone()
                or db.execute("SELECT 1 FROM games WHERE tournament_id=?", (str(event_id),)).fetchone()
                or db.execute("SELECT 1 FROM award_batches WHERE event_id=?", (str(event_id),)).fetchone()):
            raise ValueError("대진·경기·우승 보상이 있는 경매의 낙찰은 변경할 수 없습니다.")
        row = db.execute("SELECT * FROM live_lots WHERE event_id=? AND id=?", (event_id, lot_id)).fetchone()
        if row is None or row["status"] != "SOLD":
            raise ValueError("이 경매에서 낙찰이 완료된 선수를 선택해 주세요.")
        lot = dict(row)
        player = self.competition._player(db, event_id, lot["member_id"])
        if db.execute("SELECT 1 FROM competition_teams WHERE event_id=? AND captain_id=?", (event_id, player["member_id"])).fetchone():
            raise ValueError("고정된 팀장은 낙찰 정정으로 이동할 수 없습니다.")
        if (player["participation_status"] != "SELECTED"
                or self.core.get_member(player["member_id"], db)["status"] != "APPROVED"):
            raise ValueError("참가 명단에 있는 승인 회원만 낙찰 정정·재경매할 수 있습니다.")
        if (player["team_id"] is None or player["team_id"] != lot["highest_team_id"]
                or player["price"] != lot["highest_bid"]):
            raise ValueError("현재 선수 배정과 낙찰 기록이 일치하지 않습니다. 먼저 기록을 확인해 주세요.")
        if db.execute("SELECT 1 FROM live_lots WHERE event_id=? AND member_id=? AND id<>? AND status IN ('OPEN','QUEUED','SOLD')",
                      (event_id, player["member_id"], lot_id)).fetchone():
            raise ValueError("같은 선수의 다른 진행·낙찰 기록이 있어 정정할 수 없습니다.")
        teams = [dict(row) for row in db.execute("SELECT * FROM competition_teams WHERE event_id=? ORDER BY id", (event_id,))]
        team_map = {team["id"]: team for team in teams}
        if player["team_id"] not in team_map:
            raise ValueError("기존 낙찰 팀이 이 경매에 없습니다.")
        roster = [dict(row) for row in db.execute(
            "SELECT p.member_id,p.team_id,p.price,p.participation_status,m.status AS member_status "
            "FROM competition_players p JOIN members m ON m.id=p.member_id WHERE p.event_id=? ORDER BY p.id", (event_id,)
        )]
        if team_id is not None:
            target = team_map.get(team_id)
            if target is None:
                raise ValueError("이 경매의 팀을 선택해 주세요.")
            captain = next((member for member in roster if member["member_id"] == target["captain_id"]), None)
            if (not captain or captain["member_status"] != "APPROVED"
                    or captain["participation_status"] != "SELECTED" or captain["team_id"] != team_id):
                raise ValueError("새 낙찰 팀장의 승인과 참가 상태를 확인해 주세요.")
            others = [member for member in roster if member["team_id"] == team_id and member["member_id"] != player["member_id"]]
            if len(others) >= 5 or sum(member["price"] for member in others) + amount > target["budget"]:
                raise ValueError("정정 후 팀 정원 또는 시작 예산을 초과합니다.")
            if team_id == player["team_id"] and amount == player["price"]:
                raise ValueError("변경할 낙찰 팀이나 금액을 입력해 주세요.")
        elif amount != 0:
            raise ValueError("낙찰 취소·재경매의 금액은 0으로 입력해 주세요.")

        balances = []
        for team in teams:
            before = [member for member in roster if member["team_id"] == team["id"]]
            after = [member for member in before if member["member_id"] != player["member_id"]]
            if team_id == team["id"]:
                after.append({"price": amount})
            balances.append({"team_id": team["id"], "team_name": team["name"],
                             "before": team["budget"] - sum(member["price"] for member in before),
                             "after": team["budget"] - sum(member["price"] for member in after),
                             "players_before": len(before), "players_after": len(after)})

        open_lots = [dict(row) for row in db.execute("SELECT * FROM live_lots WHERE event_id=? AND status='OPEN'", (event_id,))]
        if paused and session["paused_phase"] == "RUNNING":
            if len(open_lots) != 1 or open_lots[0]["id"] != session["current_lot_id"]:
                raise ValueError("일시정지된 현재 선수 기록이 일치하지 않습니다.")
        elif open_lots:
            raise ValueError("진행 중인 선수가 남아 있어 낙찰 정정을 확인할 수 없습니다.")
        for current in open_lots:
            if current["highest_team_id"] is not None:
                pending = next((balance for balance in balances if balance["team_id"] == current["highest_team_id"]), None)
                if (pending is None or pending["players_after"] >= 5
                        or current["highest_bid"] > pending["after"]):
                    raise ValueError("현재 선수의 최고 입찰이 정정 후 정원·예산을 초과합니다. 현재 경매 결과를 먼저 정리해 주세요.")
        before = {"team_id": player["team_id"], "team_name": team_map[player["team_id"]]["name"], "amount": player["price"]}
        after = {"team_id": team_id, "team_name": team_map[team_id]["name"] if team_id is not None else None, "amount": amount}
        last_event_id = db.execute("SELECT COALESCE(MAX(id),0) FROM live_events WHERE event_id=?", (event_id,)).fetchone()[0]
        snapshot = {"session": session, "event_status": event["status"], "last_event_id": last_event_id, "lot": lot,
                    "roster": roster, "teams": teams, "open_lots": open_lots, "after": after}
        fingerprint = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
        return {"event_id": event_id, "lot_id": lot_id, "member_id": player["member_id"], "riot_id": player["riot_id"],
                "before": before, "after": after, "team_balances": balances,
                "requeue": team_id is None, "fingerprint": fingerprint}

    @staticmethod
    def _correction_values(event_id, lot_id, team_id, amount):
        event_id, lot_id = integer(event_id, "경매 번호"), integer(lot_id, "낙찰 번호")
        team_id = integer(team_id, "팀 번호") if team_id is not None else None
        amount = integer(amount, "정정 낙찰가")
        if amount < 0 or amount > 9_223_372_036_854_775_807:
            raise ValueError("정정 낙찰가는 저장 가능한 범위의 0 이상 정수로 입력해 주세요.")
        return event_id, lot_id, team_id, amount

    def preview_sale_correction(self, token, event_id, lot_id, *, team_id=None, amount=0):
        """Preview a paused/pre-bracket correction without changing any records."""
        event_id, lot_id, team_id, amount = self._correction_values(event_id, lot_id, team_id, amount)
        with self.core.read_snapshot() as db:
            self.core.require_admin(db, token)
            return self._sale_correction_preview(db, event_id, lot_id, team_id, amount)

    def correct_sale(self, token, event_id, lot_id, *, team_id=None, amount=0, reason,
                     request_id, expected_fingerprint):
        """Refund/reassign a SOLD player atomically; preserve bids and audit once.

        team_id=None cancels the sale and appends one fresh QUEUED attempt. A
        finished auction returns to PAUSED/WAITING and needs explicit resume.
        Preview fingerprints reject stale dialogs; a successful UUID retry
        returns its original receipt even after the auction has moved on.
        """
        event_id, lot_id, team_id, amount = self._correction_values(event_id, lot_id, team_id, amount)
        reason = str(reason).strip()
        if not reason or len(reason) > 1000:
            raise ValueError("낙찰 정정 사유를 1~1,000자로 입력해 주세요.")
        try:
            request_id = str(uuid.UUID(str(request_id)))
        except (ValueError, AttributeError):
            raise ValueError("유효한 낙찰 정정 요청 번호(UUID)가 필요합니다.") from None
        if not isinstance(expected_fingerprint, str) or len(expected_fingerprint) != 64:
            raise ValueError("정정할 낙찰을 다시 미리보기해 주세요.")
        with self.core.transaction() as db:
            actor = self.core.require_admin(db, token)
            payload = [actor["id"], event_id, lot_id, team_id, amount, reason, expected_fingerprint]
            payload_hash = hashlib.sha256(json.dumps(payload).encode()).hexdigest()
            # Correction volume is small. Keeping UUID receipts in the existing
            # append-only event audit avoids a second monetary source of truth.
            pattern = '%"request_id": "' + request_id + '"%'
            for row in db.execute("SELECT detail FROM live_events WHERE type='SALE_CORRECTION' AND detail LIKE ?", (pattern,)):
                previous = json.loads(row["detail"])
                if previous.get("request_id") == request_id:
                    if previous.get("payload_hash") != payload_hash:
                        raise ValueError("같은 요청 번호로 다른 낙찰 정정을 전송할 수 없습니다.")
                    return {**previous["result"], "replayed": True}
            preview = self._sale_correction_preview(db, event_id, lot_id, team_id, amount)
            if preview["fingerprint"] != expected_fingerprint:
                raise ValueError("미리보기 이후 경매 상태가 바뀌었습니다. 다시 확인해 주세요.")
            session = self._session(db, event_id)
            stamp = self._now(db)
            new_lot_id = None
            if team_id is None:
                db.execute("UPDATE competition_players SET team_id=NULL,price=0,state='AVAILABLE' WHERE event_id=? AND member_id=?",
                           (event_id, preview["member_id"]))
                db.execute("UPDATE live_lots SET status='CANCELLED',highest_team_id=NULL,highest_bid=NULL WHERE id=? AND event_id=?", (lot_id, event_id))
                sequence = db.execute("SELECT COALESCE(MAX(sequence),-1)+1 FROM live_lots WHERE event_id=?", (event_id,)).fetchone()[0]
                attempt = db.execute("SELECT COALESCE(MAX(attempt),0)+1 FROM live_lots WHERE event_id=? AND member_id=?", (event_id, preview["member_id"])).fetchone()[0]
                new_lot_id = db.execute("INSERT INTO live_lots(event_id,member_id,sequence,attempt,status) VALUES(?,?,?,?,'QUEUED')",
                                        (event_id, preview["member_id"], sequence, attempt)).lastrowid
                if session["status"] == "COMPLETED":
                    db.execute("UPDATE competition_events SET status='AUCTION',current_player_id=NULL WHERE id=?", (event_id,))
                    db.execute("UPDATE live_sessions SET status='PAUSED',paused_phase='WAITING',pause_remaining=?,next_at=NULL,updated_at=? WHERE event_id=?",
                               (TRANSITION_SECONDS, _iso(stamp), event_id))
                elif session["paused_phase"] == "WAITING" and session["pause_remaining"] is None:
                    db.execute("UPDATE live_sessions SET pause_remaining=? WHERE event_id=?", (TRANSITION_SECONDS, event_id))
            else:
                self.competition._assign(db, event_id, preview["member_id"], team_id, amount)
                db.execute("UPDATE live_lots SET highest_team_id=?,highest_bid=? WHERE id=? AND event_id=?", (team_id, amount, lot_id, event_id))
            db.execute("UPDATE live_sessions SET updated_at=? WHERE event_id=?", (_iso(stamp), event_id))
            result = {"event_id": event_id, "lot_id": lot_id, "new_lot_id": new_lot_id,
                      "member_id": preview["member_id"], "team_id": team_id, "amount": amount,
                      "old_team_id": preview["before"]["team_id"], "old_amount": preview["before"]["amount"], "replayed": False}
            detail = {"request_id": request_id, "payload_hash": payload_hash, "reason": reason,
                      "before": preview["before"], "after": preview["after"], "result": result}
            self._log(db, event_id, "SALE_CORRECTION", detail, stamp, actor, lot_id)
            self.competition._audit(db, event_id, actor, "SALE_CORRECTION", json.dumps(detail, ensure_ascii=False))
            return result

    def get_view(self, token, event_id):
        """Read identity and auction display from one consistent DB snapshot."""
        reader = closing(self.core.connect()) if self.core.is_postgres else self.core.read_snapshot()
        with reader as db:
            if self.core.is_postgres:
                actor, state = self._fetch_view(event_id, db, actor_query=session_query(token) if token else None,
                                                owned_snapshot=True)
            else:
                actor = self.core.session(token, db)
                state = self.get_state(event_id, conn=db)
            read_finished = time.monotonic()
        self._account_for_read_close(state, read_finished)
        return actor, state

    def _account_for_read_close(self, state, read_finished):
        """Age the DB sample by observed cleanup time, without guessing transit.

        A pooled read closes with a synchronous ROLLBACK. Its elapsed time is
        real clock age even though it occurs after the display was assembled.
        The unmeasured database-to-app one-way transit is not estimated here.
        """
        if not state or not self.core.is_postgres or self._injected_clock:
            return
        state["server_now"] += max(0, time.monotonic() - read_finished)
        if state["status"] == "PAUSED":
            return
        for lot in state["lots"]:
            if lot["status"] == "OPEN":
                lot["remaining_seconds"] = max(0.0, lot["closes_at"] - state["server_now"])
        if state.get("next_at") is not None:
            state["next_in_seconds"] = max(0.0, state["next_at"] - state["server_now"])

    @staticmethod
    def _view_statements(event_id):
        # Historical lots remain available, but their repeated member IDs must
        # not multiply Riot payload transfer or public-profile validation work.
        from .member_ranks import RANK_JOINS, RANK_SELECT
        return [
            ("""SELECT s.*,e.status AS workflow_status,
                e.created_by AS host_id,e.kind AS event_kind,e.title AS event_title,
                COALESCE(a.display_name,a.username,'') AS host_name,
                EXISTS(SELECT 1 FROM competition_games g WHERE g.event_id=e.id) AS has_games
                FROM live_sessions s JOIN competition_events e ON e.id=s.event_id
                LEFT JOIN accounts a ON a.id=e.created_by WHERE s.event_id=?""", (event_id,)),
            ("SELECT l.*,p.riot_id,p.role,p.score,p.clan_tier_snapshot,p.current_tier_snapshot,p.current_tier_lp_snapshot,t.name AS highest_team_name FROM live_lots l JOIN competition_players p ON p.event_id=l.event_id AND p.member_id=l.member_id LEFT JOIN competition_teams t ON t.id=l.highest_team_id WHERE l.event_id=? ORDER BY l.sequence", (event_id,)),
            ("SELECT b.id,b.request_id,b.event_id,b.lot_id,b.team_id,b.member_id,b.amount,b.closes_at,b.created_at,t.name AS team_name,p.riot_id FROM live_bids b JOIN competition_teams t ON t.id=b.team_id JOIN competition_players p ON p.event_id=b.event_id AND p.member_id=b.member_id WHERE b.event_id=? ORDER BY b.id DESC LIMIT 300", (event_id,)),
            ("SELECT id,lot_id,type,detail,created_at FROM live_events WHERE event_id=? ORDER BY id DESC LIMIT 300", (event_id,)),
            ("SELECT * FROM competition_teams WHERE event_id=? ORDER BY id", (event_id,)),
            ("SELECT p.member_id,p.riot_id,p.role,p.score,p.price,p.team_id,p.state,p.clan_tier_snapshot,p.current_tier_snapshot,p.current_tier_lp_snapshot FROM competition_players p WHERE p.event_id=? AND p.participation_status='SELECTED' ORDER BY p.id", (event_id,)),
            (f"SELECT m.id AS member_id,m.riot_id,m.canonical_id AS profile_canonical_id,rp.payload AS riot_cache_payload,rp.canonical_id AS riot_cache_canonical,{RANK_SELECT} FROM competition_players p JOIN members m ON m.id=p.member_id LEFT JOIN riot_profiles rp ON rp.member_id=m.id AND rp.canonical_id=m.canonical_id {RANK_JOINS} WHERE p.event_id=? ORDER BY p.id", (event_id,)),
        ]

    def get_state(self, event_id, *, conn=None):
        with self.core.read_snapshot(conn) as db:
            state = self._fetch_view(event_id, db)[1]
            read_finished = time.monotonic()
        if conn is None:
            self._account_for_read_close(state, read_finished)
        return state

    def _fetch_view(self, event_id, db, *, actor_query=None, owned_snapshot=False):
        statements = self._view_statements(event_id)
        if actor_query is not None:
            statements.insert(0, actor_query)
        clock_in_batch = self.core.is_postgres and not self._injected_clock
        if clock_in_batch:
            # This is last so all display SELECTs precede its DB-clock
            # sample. No host wall clock participates in PG deadlines.
            statements.append(("SELECT EXTRACT(EPOCH FROM pg_catalog.clock_timestamp())::double precision", None))
        if self.core.is_postgres:
            batches = db.fetch_snapshot_batches(statements) if owned_snapshot else db.fetch_batches(statements)
        else:
            batches = [db.execute(query, parameters).fetchall() for query, parameters in statements]
        received = time.monotonic()
        sampled_clock = float(batches.pop()[0][0]) if clock_in_batch else None
        actor_rows = batches.pop(0) if actor_query is not None else []
        actor = dict(actor_rows[0]) if actor_rows else None
        return actor, self._assemble_view(event_id, batches, db, sampled_clock=sampled_clock, received=received)

    def _assemble_view(self, event_id, batches, db=None, *, sampled_clock=None, received=None):
        """Project already fetched rows for both ordinary and shared reads."""
        header, lots, bids, events, teams, players, profiles = batches
        if not header:
            return None
        result = dict(header[0])
        event = {"id": event_id, "status": result.pop("workflow_status"),
                 "created_by": result.pop("host_id"), "kind": result.pop("event_kind"),
                 "title": result.pop("event_title"), "host_name": result.pop("host_name"),
                 "has_games": bool(result.pop("has_games"))}
        result["event"] = event
        # Compatibility field now reports the mandatory effective rule.
        result["reset_on_bid"] = True
        result["extension_seconds"] = BID_EXTENSION_SECONDS
        result["bid_seconds"] = _effective_bid_seconds(result["bid_seconds"])
        result["max_remaining_seconds"] = result["bid_seconds"]
        result["transition_seconds"] = TRANSITION_SECONDS
        result["lots"], result["bids"], result["events"], result["teams"], players = [
            [dict(row) for row in rows] for rows in (lots, bids, events, teams, players)]
        from .riot_profile import attach_profile
        public_profiles = {}
        for row in profiles:
            profile = dict(row)
            attach_profile(profile)
            public_profiles[profile["member_id"]] = (profile["profile_canonical_id"], profile["riot_profile"])
        for player in [*result["lots"], *players]:
            canonical, profile = public_profiles.get(player["member_id"], (None, None))
            try:
                matches = canonical is not None and identity(player["riot_id"])[1] == canonical
            except ValueError:
                matches = False
            player["riot_profile"] = deepcopy(profile) if matches else None
        # Account for local conversion time after the final database clock
        # sample. This duration uses a monotonic clock, never wall time.
        stamp = sampled_clock + max(0, time.monotonic()-received) if sampled_clock is not None else self._now(db)
        result["server_now"] = stamp
        result["current_lot"] = next((lot for lot in result["lots"] if lot["id"] == result["current_lot_id"]), None)
        for lot in result["lots"]:
            if lot["status"] == "OPEN":
                lot["remaining_seconds"] = result["pause_remaining"] if result["status"] == "PAUSED" else max(0.0, lot["closes_at"] - stamp)
            else:
                lot["remaining_seconds"] = 0.0
        result["next_in_seconds"] = max(0.0, result["next_at"] - stamp) if result["next_at"] is not None else None
        if result["status"] == "PAUSED" and result["paused_phase"] == "WAITING":
            result["next_in_seconds"] = result["pause_remaining"]
        event["players"] = players
        for team in result["teams"]:
            team["players"] = [p for p in players if p["team_id"] == team["id"]]
            team["remaining"] = team["budget"] - sum(p["price"] for p in team["players"])
        result["unsold_count"] = sum(p["team_id"] is None and p["state"] == "UNSOLD" for p in players)
        result["queued_count"] = sum(lot["status"] == "QUEUED" for lot in result["lots"])
        result["worker_error"] = self._worker_error()
        return result

    def has_active_sessions(self):
        return bool(self._worker_read("SELECT 1 FROM live_sessions WHERE status IN ('READY','RUNNING','WAITING','PAUSED') LIMIT 1"))

    def _worker_error(self):
        with self._worker_lock:
            worker = self._workers.get(self.core.db_path)
            return worker["error"] if worker else None

    def ensure_worker(self, interval=0.25, *, persistent=False):
        interval = float(interval)
        if not 0.01 <= interval <= 10:
            raise ValueError("처리 주기는 0.01~10초로 지정해 주세요.")
        if not isinstance(persistent, bool):
            raise ValueError("상시 처리 여부는 참 또는 거짓으로 지정해 주세요.")
        with self._worker_lock:
            existing = self._workers.get(self.core.db_path)
            if existing and existing["thread"].is_alive() and not existing["stop"].is_set():
                existing["persistent"] |= persistent
                existing["generation"] += 1
                return existing["thread"]
            stop = threading.Event()
            state = {"stop": stop, "error": None, "persistent": persistent, "generation": 0}

            def work():
                try:
                    while not stop.is_set():
                        try:
                            self.settle_due()
                            state["error"] = None
                            with self._worker_lock:
                                generation = state["generation"]
                            # Do not retain a polling thread for every discarded
                            # demo DB. READY, paused and unsold sessions still
                            # need a worker; the launcher explicitly stays on.
                            if not state["persistent"] and not self.has_active_sessions():
                                with self._worker_lock:
                                    # An ensure call during the read must get a
                                    # further check instead of a retiring worker.
                                    if not state["persistent"] and state["generation"] == generation:
                                        if self._workers.get(self.core.db_path) is state:
                                            self._workers.pop(self.core.db_path, None)
                                        return
                        except Exception as exc:
                            state["error"] = str(exc)
                            _LOG.exception("Live auction deadline processing failed")
                        stop.wait(interval)
                finally:
                    with self._worker_lock:
                        if self._workers.get(self.core.db_path) is state:
                            self._workers.pop(self.core.db_path, None)

            thread = threading.Thread(target=work, name="roly-auction-worker", daemon=True)
            state["thread"] = thread
            self._workers[self.core.db_path] = state
            thread.start()
            return thread

    def stop_worker(self):
        with self._worker_lock:
            worker = self._workers.get(self.core.db_path)
            if not worker:
                return
            worker["stop"].set()
        worker["thread"].join(timeout=16)
        with self._worker_lock:
            if not worker["thread"].is_alive() and self._workers.get(self.core.db_path) is worker:
                self._workers.pop(self.core.db_path, None)
