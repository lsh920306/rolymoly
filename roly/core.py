"""Transactional local domain services. No network or UI dependencies."""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import secrets
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from uuid import UUID

from .auth import AUTH_DDL, PersonalAuth, password_digest, password_material, username_value
from .member_profile import UNSET, validate_clan_tier, validate_current_tier

ROLES = ("TOP", "JG", "MID", "AD", "SUP")
ROLE_ALIASES = {"탑": "TOP", "정글": "JG", "미드": "MID", "원딜": "AD", "서폿": "SUP", "바텀": "AD", "ADC": "AD", "SUPPORT": "SUP", "JUNGLE": "JG"}

SCORE_ADJUSTMENT_DDL = """CREATE TABLE IF NOT EXISTS score_adjustment_requests(
    request_key TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,
    ledger_id INTEGER NOT NULL UNIQUE REFERENCES score_ledger(id),
    actor_id INTEGER NOT NULL REFERENCES accounts(id))"""

# These indexed aggregates run on the database server in one statement. Keeping
# them together avoids per-member network round trips without changing ledger
# semantics: corrections/voids already contribute compensating ledger entries.
_MEMBER_SELECT = """SELECT m.*,
    m.base_score + COALESCE((SELECT SUM(s.amount) FROM score_ledger s WHERE s.member_id=m.id),0) AS score,
    (SELECT COUNT(*) FROM game_players p JOIN games g ON p.game_id=g.id
        WHERE p.member_id=m.id AND g.status='CONFIRMED' AND g.kind='NORMAL' AND p.team=g.winner) AS wins,
    (SELECT COUNT(*) FROM game_players p JOIN games g ON p.game_id=g.id
        WHERE p.member_id=m.id AND g.status='CONFIRMED' AND g.kind='NORMAL' AND p.team<>g.winner) AS losses,
    COALESCE((SELECT SUM(a.units) FROM award_ledger a WHERE a.member_id=m.id),0) AS award_units
    ,(SELECT r.status FROM registration_requests r WHERE r.member_id=m.id) AS registration_status
    ,COALESCE((SELECT r.rejection_reason FROM registration_requests r WHERE r.member_id=m.id),'') AS rejection_reason
    FROM members m"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def role(value):
    value = str(value).strip().upper()
    value = ROLE_ALIASES.get(value, value)
    if value not in ROLES:
        raise ValueError("포지션은 TOP/JG/MID/AD/SUP 중 하나여야 합니다.")
    return value


def identity(value):
    value = str(value).strip()
    if value.count("#") != 1:
        raise ValueError("Riot ID는 닉네임#태그 형식으로 입력해주세요.")
    nickname, tag = [part.strip() for part in value.split("#")]
    if not nickname or not tag or len(nickname) > 40 or len(tag) > 12:
        raise ValueError("닉네임과 태그를 확인해주세요.")
    display = f"{nickname}#{tag}"
    return display, display.casefold()


def integer(value, name="값"):
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not re.fullmatch(r"[+-]?\d+", str(value)):
        raise ValueError(f"{name}은 정수여야 합니다.")
    return int(value)


class Core(PersonalAuth):
    def __init__(self, db_path):
        self.db_path = str(db_path)
        self.is_postgres = self.db_path.startswith("supabase://")
        self.backend = "postgres" if self.is_postgres else "sqlite"
        self.schema = None
        if self.is_postgres:
            from .postgres import parse_uri
            self.schema = parse_uri(self.db_path)
        elif self.db_path == ":memory:":
            self.db_path = f"file:roly-{secrets.token_hex(12)}?mode=memory&cache=shared"
            self._keeper = self.connect()
        elif not self.db_path.startswith("file:"):
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self):
        if self.is_postgres:
            from .postgres import connect
            return connect(self.schema)
        conn = sqlite3.connect(self.db_path, timeout=15, isolation_level=None, uri=self.db_path.startswith("file:"))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    @contextmanager
    def transaction(self):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def _using(self, conn=None):
        if conn is not None:
            if not conn.in_transaction:
                raise ValueError("외부 연결은 진행 중인 트랜잭션 안에서만 사용할 수 있습니다.")
            savepoint = "core_operation_" + secrets.token_hex(12)
            conn.execute(f"SAVEPOINT {savepoint}")
            try:
                yield conn
            except BaseException:
                if conn.in_transaction:
                    conn.execute(f"ROLLBACK TO {savepoint}")
                    conn.execute(f"RELEASE {savepoint}")
                raise
            else:
                conn.execute(f"RELEASE {savepoint}")
        else:
            with self.transaction() as db:
                yield db

    @contextmanager
    def read_snapshot(self, conn=None):
        """Read a consistent view without reserving SQLite's single writer.

        Mutation services pass their existing transaction so authorization and
        policy reads remain atomic with the protected write.
        """
        if conn is not None:
            yield conn
        else:
            with closing(self.connect()) as db:
                if not self.is_postgres:
                    db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
                yield db

    def initialize(self):
        if self.is_postgres:
            from .postgres import initialize
            initialize(self.schema)
            return
        with closing(self.connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS accounts(id INTEGER PRIMARY KEY,username TEXT NOT NULL UNIQUE,display_name TEXT NOT NULL,password_hash TEXT NOT NULL,salt TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('admin','organizer','member')),active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,member_id INTEGER REFERENCES members(id));
            CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,account_id INTEGER NOT NULL REFERENCES accounts(id),expires_at TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS login_failures(username TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS members(id INTEGER PRIMARY KEY,riot_id TEXT NOT NULL,canonical_id TEXT NOT NULL UNIQUE,main_role TEXT NOT NULL,sub_role TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('PENDING','APPROVED','KICKED')),base_score INTEGER NOT NULL DEFAULT 0,notes TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS base_history(id INTEGER PRIMARY KEY,member_id INTEGER NOT NULL REFERENCES members(id),base_score INTEGER NOT NULL,effective_at TEXT NOT NULL,reason TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS policies(id INTEGER PRIMARY KEY,mode TEXT NOT NULL CHECK(mode IN ('fixed','bracket')),k INTEGER NOT NULL CHECK(k>0),threshold INTEGER NOT NULL,high_k INTEGER NOT NULL CHECK(high_k>0),effective_at TEXT NOT NULL,actor_id INTEGER REFERENCES accounts(id));
            CREATE TABLE IF NOT EXISTS games(id INTEGER PRIMARY KEY,request_key TEXT NOT NULL UNIQUE,fingerprint TEXT NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('NORMAL','AUCTION')),tournament_id TEXT,played_at TEXT NOT NULL,created_at TEXT NOT NULL,policy_id INTEGER NOT NULL REFERENCES policies(id),winner TEXT NOT NULL CHECK(winner IN ('A','B')),status TEXT NOT NULL DEFAULT 'CONFIRMED' CHECK(status IN ('CONFIRMED','VOID')),revision INTEGER NOT NULL DEFAULT 1,notes TEXT NOT NULL DEFAULT '',actor_id INTEGER NOT NULL REFERENCES accounts(id));
            CREATE TABLE IF NOT EXISTS game_players(game_id INTEGER NOT NULL REFERENCES games(id),member_id INTEGER NOT NULL REFERENCES members(id),team TEXT NOT NULL CHECK(team IN ('A','B')),role TEXT NOT NULL,score_before INTEGER NOT NULL,delta INTEGER NOT NULL,k INTEGER NOT NULL,PRIMARY KEY(game_id,member_id),UNIQUE(game_id,team,role));
            CREATE TABLE IF NOT EXISTS game_revisions(id INTEGER PRIMARY KEY,game_id INTEGER NOT NULL REFERENCES games(id),revision INTEGER NOT NULL,winner TEXT,status TEXT NOT NULL,reason TEXT NOT NULL,actor_id INTEGER NOT NULL REFERENCES accounts(id),created_at TEXT NOT NULL,UNIQUE(game_id,revision));
            CREATE TABLE IF NOT EXISTS game_settlements(id INTEGER PRIMARY KEY,game_id INTEGER NOT NULL REFERENCES games(id),revision INTEGER NOT NULL,member_id INTEGER NOT NULL REFERENCES members(id),score_before INTEGER NOT NULL,delta INTEGER NOT NULL,created_at TEXT NOT NULL,UNIQUE(game_id,revision,member_id));
            CREATE TABLE IF NOT EXISTS score_ledger(id INTEGER PRIMARY KEY,member_id INTEGER NOT NULL REFERENCES members(id),amount INTEGER NOT NULL,source TEXT NOT NULL,game_id INTEGER REFERENCES games(id),revision INTEGER,reason TEXT NOT NULL,actor_id INTEGER NOT NULL REFERENCES accounts(id),created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS award_batches(id INTEGER PRIMARY KEY,request_key TEXT NOT NULL UNIQUE,fingerprint TEXT NOT NULL,event_id TEXT,reason TEXT NOT NULL,actor_id INTEGER NOT NULL REFERENCES accounts(id),created_at TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS award_one_per_event ON award_batches(event_id) WHERE event_id IS NOT NULL;
            CREATE TABLE IF NOT EXISTS award_ledger(id INTEGER PRIMARY KEY,batch_id INTEGER NOT NULL REFERENCES award_batches(id),member_id INTEGER NOT NULL REFERENCES members(id),units INTEGER NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(batch_id,member_id));
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,actor_id INTEGER REFERENCES accounts(id),action TEXT NOT NULL,target TEXT NOT NULL,details TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS score_member ON score_ledger(member_id);
            CREATE INDEX IF NOT EXISTS game_member ON game_players(member_id);
            CREATE INDEX IF NOT EXISTS login_window ON login_failures(username,created_at);
            """)
            self._migrate_accounts(db)
            db.executescript(AUTH_DDL)
            db.execute("BEGIN IMMEDIATE")
            try:
                from .member_profile import initialize_sqlite
                initialize_sqlite(db)
                from .riot_sync import RIOT_DDL
                for statement in RIOT_DDL.split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute(SCORE_ADJUSTMENT_DDL)
                if not db.execute("SELECT 1 FROM policies").fetchone():
                    db.execute("INSERT INTO policies(mode,k,threshold,high_k,effective_at) VALUES('fixed',10,280,15,?)", ("1970-01-01T00:00:00.000000+00:00",))
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _migrate_accounts(db):
        """Rebuild the old role constraint while preserving IDs and session references."""
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN IMMEDIATE")
        try:
            columns = {r[1] for r in db.execute("PRAGMA table_info(accounts)")}
            definition = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='accounts'").fetchone()[0]
            if "member_id" not in columns or "'member'" not in definition:
                db.execute("CREATE TABLE accounts_updated(id INTEGER PRIMARY KEY,username TEXT NOT NULL UNIQUE,display_name TEXT NOT NULL,password_hash TEXT NOT NULL,salt TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('admin','organizer','member')),active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,member_id INTEGER REFERENCES members(id))")
                member_expression = "member_id" if "member_id" in columns else "NULL"
                db.execute("INSERT INTO accounts_updated SELECT id,username,display_name,password_hash,salt,role,active,created_at," + member_expression + " FROM accounts")
                db.execute("DROP TABLE accounts")
                db.execute("ALTER TABLE accounts_updated RENAME TO accounts")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS account_one_member ON accounts(member_id) WHERE member_id IS NOT NULL")
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("계정 데이터 연결을 확인하지 못해 변경을 중단했습니다.")
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.execute("PRAGMA foreign_keys=ON")

    def _audit(self, db, actor, action, target, details):
        actor_id = actor.get("id") if isinstance(actor, dict) else actor
        db.execute("INSERT INTO audit(actor_id,action,target,details,created_at) VALUES(?,?,?,?,?)", (actor_id, action, str(target), json.dumps(details, ensure_ascii=False, default=str), now()))

    def has_admin(self):
        with closing(self.connect()) as db:
            return bool(self._active_admins(db))

    @staticmethod
    def _active_admins(db):
        return [dict(row) for row in db.execute("SELECT a.id,a.member_id FROM accounts a LEFT JOIN members m ON m.id=a.member_id WHERE a.role='admin' AND a.active=1 AND (a.member_id IS NULL OR m.status='APPROVED')")]

    def _account_member(self, db, member_id, account_role, account_id=None):
        if member_id is None:
            if account_role == "member":
                raise ValueError("회원 로그인 계정에는 승인된 회원을 연결해주세요.")
            return None
        member_id = integer(member_id, "회원 번호")
        member = self.get_member(member_id, db)
        if member["status"] != "APPROVED":
            raise ValueError("승인된 회원만 로그인 계정에 연결할 수 있습니다.")
        if db.execute("SELECT 1 FROM accounts WHERE member_id=? AND (CAST(? AS BIGINT) IS NULL OR id<>?)", (member_id, account_id, account_id)).fetchone():
            raise ValueError("이미 다른 로그인 계정에 연결된 회원입니다.")
        return member_id

    def _new_account(self, db, username, material, display_name, account_role, member_id=None):
        username = username_value(username)
        if account_role not in ("admin", "organizer", "member"):
            raise ValueError("허용되지 않은 계정 역할입니다.")
        member_id = self._account_member(db, member_id, account_role)
        salt, digest = material
        try:
            return db.execute("INSERT INTO accounts(username,display_name,password_hash,salt,role,created_at,member_id) VALUES(?,?,?,?,?,?,?)", (username, str(display_name or username).strip(), digest, salt, account_role, now(), member_id)).lastrowid
        except sqlite3.IntegrityError as exc:
            raise ValueError("이미 사용 중인 로그인 아이디입니다.") from exc

    def setup_admin(self, username, password, display_name="운영진"):
        username = username_value(username)
        material = password_material(password)
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM accounts").fetchone():
                raise PermissionError("최초 관리자 설정은 이미 완료되었습니다.")
            account_id = self._new_account(db, username, material, display_name, "admin")
            self._audit(db, account_id, "ADMIN_SETUP", account_id, {})
            return account_id

    def login(self, username, password):
        username = str(username).strip().casefold()
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        with self.read_snapshot() as db:
            attempts = db.execute("SELECT count(*) FROM login_failures WHERE username=? AND created_at>?", (username, cutoff)).fetchone()[0]
            if attempts >= 5:
                raise PermissionError("로그인 시도가 많습니다. 5분 후 다시 시도해주세요.")
            found = db.execute("SELECT * FROM accounts WHERE username=? AND active=1", (username,)).fetchone()
            snapshot = dict(found) if found else None
        salt = snapshot["salt"] if snapshot else "00" * 16
        supplied = str(password)
        digest = password_digest(supplied if len(supplied) <= 256 else "invalid-password", salt)
        with self.transaction() as db:
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
            attempts = db.execute("SELECT count(*) FROM login_failures WHERE username=? AND created_at>?", (username, cutoff)).fetchone()[0]
            if attempts >= 5:
                raise PermissionError("로그인 시도가 많습니다. 5분 후 다시 시도해주세요.")
            account = db.execute("SELECT * FROM accounts WHERE username=? AND active=1", (username,)).fetchone()
            valid = bool(account and snapshot and account["id"] == snapshot["id"] and
                         account["salt"] == salt and account["password_hash"] == snapshot["password_hash"] and
                         len(supplied) <= 256 and hmac.compare_digest(digest, account["password_hash"]))
            if valid and (account["role"] == "member" or account["member_id"] is not None):
                member = db.execute("SELECT status FROM members WHERE id=?", (account["member_id"],)).fetchone()
                valid = bool(member and (member["status"] == "APPROVED" or (account["role"] == "member" and member["status"] == "PENDING")))
            if not valid:
                db.execute("INSERT INTO login_failures VALUES(?,?)", (username, now()))
            else:
                db.execute("DELETE FROM login_failures WHERE username=?", (username,))
                token = secrets.token_urlsafe(32)
                expires = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
                db.execute("INSERT INTO sessions VALUES(?,?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), account["id"], expires, now()))
                self._audit(db, account["id"], "LOGIN", account["id"], {})
        if not valid:
            raise PermissionError("로그인 아이디 또는 비밀번호를 확인해주세요.")
        return token

    def session(self, token, conn=None):
        if not token:
            return None
        with self.read_snapshot(conn) as db:
            result = db.execute("SELECT a.id,a.username,a.display_name,a.role,a.member_id,m.status AS member_status,r.status AS registration_status,COALESCE(r.rejection_reason,'') AS rejection_reason,s.expires_at FROM sessions s JOIN accounts a ON a.id=s.account_id LEFT JOIN members m ON m.id=a.member_id LEFT JOIN registration_requests r ON r.account_id=a.id WHERE s.token_hash=? AND s.expires_at>? AND a.active=1 AND ((a.member_id IS NULL AND a.role<>'member') OR m.status='APPROVED' OR (a.role='member' AND m.status='PENDING'))", (hashlib.sha256(str(token).encode()).hexdigest(), now())).fetchone()
            return dict(result) if result else None

    def require_staff(self, conn, token):
        actor = self.session(token, conn)
        if not actor or actor["role"] not in ("admin", "organizer"):
            raise PermissionError("운영 계정 로그인이 필요합니다.")
        return actor

    def require_admin(self, conn, token):
        actor = self.require_staff(conn, token)
        if actor["role"] != "admin":
            raise PermissionError("관리자 권한이 필요합니다.")
        return actor

    def logout(self, token):
        with self.transaction() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(str(token).encode()).hexdigest(),))

    def create_account(self, token, username, password, display_name="", role="organizer", member_id=None):
        with self.read_snapshot() as db:
            self.require_admin(db, token)
        username = username_value(username)
        material = password_material(password)
        with self.transaction() as db:
            actor = self.require_admin(db, token)
            account_id = self._new_account(db, username, material, display_name, role, member_id)
            self._audit(db, actor, "ACCOUNT_CREATE", account_id, {"role": role, "member_id": member_id})
            return account_id

    def list_accounts(self, token):
        with self.read_snapshot() as db:
            self.require_admin(db, token)
            return [dict(r) for r in db.execute("SELECT a.id,a.username,a.display_name,a.role,a.active,a.created_at,a.member_id,m.riot_id,m.status AS member_status,r.status AS registration_status,COALESCE(r.rejection_reason,'') AS rejection_reason FROM accounts a LEFT JOIN members m ON m.id=a.member_id LEFT JOIN registration_requests r ON r.account_id=a.id ORDER BY a.id")]

    def link_account_member(self, token, account_id, member_id, reason=""):
        if not str(reason).strip():
            raise ValueError("계정과 회원의 연결 변경 사유를 입력해 주세요.")
        with self.transaction() as db:
            actor = self.require_admin(db, token)
            account = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if not account:
                raise ValueError("계정을 찾을 수 없습니다.")
            member_id = integer(member_id, "회원 번호") if member_id is not None else None
            if member_id == account["member_id"]:
                return
            if db.execute("SELECT 1 FROM registration_requests WHERE account_id=?", (account_id,)).fetchone():
                raise ValueError("직접 가입한 개인 계정의 회원 연결은 변경할 수 없습니다.")
            events_exist = db.table_exists("competition_events") if self.is_postgres else bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='competition_events'").fetchone())
            if events_exist:
                for linked_id in (account["member_id"], member_id):
                    if linked_id is not None and db.execute("SELECT 1 FROM competition_events e WHERE e.status NOT IN ('COMPLETED','CANCELLED') AND (EXISTS(SELECT 1 FROM competition_teams t WHERE t.event_id=e.id AND t.captain_id=?) OR EXISTS(SELECT 1 FROM competition_players p WHERE p.event_id=e.id AND p.member_id=? AND p.participation_status='SELECTED')) LIMIT 1", (linked_id, linked_id)).fetchone():
                        raise ValueError("진행 중인 내전·경매의 참가자 또는 팀장은 계정 연결을 변경할 수 없습니다.")
            member_id = self._account_member(db, member_id, account["role"], account_id)
            display_name = self.get_member(member_id, db)["riot_id"] if member_id is not None else account["username"]
            db.execute("UPDATE accounts SET member_id=?,display_name=? WHERE id=?", (member_id, display_name, account_id))
            db.execute("DELETE FROM sessions WHERE account_id=?", (account_id,))
            db.execute("UPDATE password_resets SET used_at=? WHERE account_id=? AND used_at IS NULL", (now(), account_id))
            self._audit(db, actor, "ACCOUNT_MEMBER", account_id, {"before": account["member_id"], "member_id": member_id, "reason": str(reason).strip()})

    def set_account_role(self, token, account_id, role, active=True):
        with self.transaction() as db:
            actor = self.require_admin(db, token)
            if role not in ("admin", "organizer", "member"):
                raise ValueError("허용되지 않은 역할입니다.")
            old = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if not old:
                raise ValueError("계정을 찾을 수 없습니다.")
            personal = db.execute("SELECT 1 FROM registration_requests WHERE account_id=?", (account_id,)).fetchone()
            if role != "member" and old["member_id"] is not None and (active or role != old["role"]):
                self._account_member(db, old["member_id"], role, account_id)
            if role == "member" and active and not personal:
                self._account_member(db, old["member_id"], role, account_id)
            if old["role"] == "admin" and old["active"] and (role != "admin" or not active):
                if not any(row["id"] != old["id"] for row in self._active_admins(db)):
                    raise ValueError("마지막 관리자는 해제할 수 없습니다.")
            db.execute("UPDATE accounts SET role=?,active=? WHERE id=?", (role, int(bool(active)), account_id))
            db.execute("DELETE FROM sessions WHERE account_id=?", (account_id,))
            db.execute("UPDATE password_resets SET used_at=? WHERE account_id=? AND used_at IS NULL", (now(), account_id))
            self._audit(db, actor, "ACCOUNT_ROLE", account_id, {"role": role, "active": bool(active)})

    def join_member(self, riot_id, main_role, sub_role, base_score=0, notes=""):
        display, canonical = identity(riot_id)
        main_role, sub_role = role(main_role), role(sub_role)
        if main_role == sub_role:
            raise ValueError("주 포지션과 부 포지션은 달라야 합니다.")
        with self.transaction() as db:
            try:
                member_id = db.execute("INSERT INTO members(riot_id,canonical_id,main_role,sub_role,status,base_score,application_notes,created_at,updated_at) VALUES(?,?,?,?,'PENDING',0,?,?,?)", (display, canonical, main_role, sub_role, str(notes)[:2000], now(), now())).lastrowid
            except sqlite3.IntegrityError as exc:
                raise ValueError("이미 신청했거나 등록된 Riot ID입니다. 운영진에게 문의해주세요.") from exc
            self._audit(db, None, "MEMBER_JOIN", member_id, {"riot_id": display})
            return member_id

    def member_score(self, conn, member_id):
        row = conn.execute("SELECT base_score + COALESCE((SELECT SUM(amount) FROM score_ledger WHERE member_id=m.id),0) AS score FROM members m WHERE id=?", (member_id,)).fetchone()
        if not row:
            raise ValueError("회원을 찾을 수 없습니다.")
        return row["score"]

    def get_member(self, member_id, conn=None):
        with self.read_snapshot(conn) as db:
            row = db.execute(_MEMBER_SELECT + " WHERE m.id=?", (member_id,)).fetchone()
            if not row:
                raise ValueError("회원을 찾을 수 없습니다.")
            return self._member_record(row)

    @staticmethod
    def _member_record(row):
        result = dict(row)
        score, wins, losses, units = (result.pop(key) for key in ("score", "wins", "losses", "award_units"))
        result["score"] = score
        result["primary_role"], result["secondary_role"] = result["main_role"], result["sub_role"]
        result["nickname"], result["riot_tag"] = result["riot_id"].split("#", 1)
        result["wins"], result["losses"] = wins, losses
        result.update(award_units=units, cats=units % 5, stars=(units // 5) % 5, medals=(units // 25) % 5, trophies=units // 125)
        return result

    def list_members(self, include_pending=False):
        with self.read_snapshot() as db:
            order = ' ORDER BY m.riot_id COLLATE "C"' if self.is_postgres else " ORDER BY m.riot_id"
            rows = db.execute(_MEMBER_SELECT + " WHERE (?=1 OR m.status='APPROVED')" + order, (int(bool(include_pending)),))
            return [self._member_record(row) for row in rows]

    def approve_member(self, token, member_id, base_score, notes="", *, expected_updated_at=None):
        base_score = integer(base_score, "기본점수")
        if base_score < 0 or base_score > 10000:
            raise ValueError("승인 기본점수는 0~10,000 사이여야 합니다.")
        with self.transaction() as db:
            actor = self.require_admin(db, token)
            member = self.get_member(member_id, db)
            if expected_updated_at is not None and expected_updated_at != member["updated_at"]:
                raise ValueError("신청 내용이 변경되었습니다. 최신 신청을 불러온 뒤 다시 확인해주세요.")
            if member["status"] != "PENDING":
                raise ValueError("승인 대기 회원만 최초 승인할 수 있습니다. 탈퇴 회원은 복귀 처리를 이용해주세요.")
            if member["registration_status"] == "REJECTED":
                raise ValueError("거절된 신청은 본인이 다시 제출한 뒤 승인해주세요.")
            updated_at = max(now(), (datetime.fromisoformat(member["updated_at"]) + timedelta(microseconds=1)).isoformat(timespec="microseconds"))
            db.execute("UPDATE members SET status='APPROVED',base_score=?,notes=?,updated_at=? WHERE id=?", (base_score, notes or member["notes"], updated_at, member_id))
            db.execute("UPDATE registration_requests SET status='APPROVED',rejection_reason='',updated_at=? WHERE member_id=?", (updated_at, member_id))
            db.execute("INSERT INTO base_history(member_id,base_score,effective_at,reason) VALUES(?,?,?,?)", (member_id, base_score, now(), "회원 승인"))
            self._audit(db, actor, "MEMBER_APPROVE", member_id, {"base_score": base_score})

    def restore_member(self, token, member_id, reason, *, expected_updated_at=None):
        if not str(reason).strip():
            raise ValueError("복귀 사유를 입력해주세요.")
        with self.transaction() as db:
            actor = self.require_admin(db, token)
            member = self.get_member(member_id, db)
            member_id = member["id"]
            if expected_updated_at is not None and expected_updated_at != member["updated_at"]:
                raise ValueError("회원 상태가 변경되었습니다. 최신 정보를 확인해주세요.")
            if member["status"] != "KICKED":
                raise ValueError("탈퇴 처리된 회원만 복귀할 수 있습니다.")
            previously_approved = member["registration_status"] == "APPROVED"
            if member["registration_status"] is None:
                previously_approved = bool(db.execute("SELECT 1 FROM audit WHERE target=? AND action='MEMBER_APPROVE'", (str(member_id),)).fetchone())
            status = "APPROVED" if previously_approved else "PENDING"
            updated_at = max(now(), (datetime.fromisoformat(member["updated_at"]) + timedelta(microseconds=1)).isoformat(timespec="microseconds"))
            db.execute("UPDATE members SET status=?,updated_at=? WHERE id=?", (status, updated_at, member_id))
            db.execute("UPDATE registration_requests SET updated_at=? WHERE member_id=?", (updated_at, member_id))
            self._audit(db, actor, "MEMBER_RESTORE", member_id, {"before": "KICKED", "status": status, "reason": str(reason).strip()})
            return self.get_member(member_id, db)

    def _member_active_events(self, db, member_id):
        exists = db.table_exists("competition_events") if self.is_postgres else bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='competition_events'").fetchone())
        if not exists:
            return []
        return [dict(row) for row in db.execute("SELECT e.id,e.title,e.kind,e.status,CASE WHEN EXISTS(SELECT 1 FROM competition_teams t WHERE t.event_id=e.id AND t.captain_id=?) THEN 1 ELSE 0 END AS is_captain FROM competition_events e WHERE e.status NOT IN ('COMPLETED','CANCELLED') AND (EXISTS(SELECT 1 FROM competition_players p WHERE p.event_id=e.id AND p.member_id=? AND p.participation_status='SELECTED') OR EXISTS(SELECT 1 FROM competition_teams t WHERE t.event_id=e.id AND t.captain_id=?) OR e.created_by IN (SELECT id FROM accounts WHERE member_id=?)) ORDER BY e.id", (member_id, member_id, member_id, member_id))]

    def member_active_events(self, token, member_id):
        with self.read_snapshot() as db:
            self.require_admin(db, token)
            self.get_member(member_id, db)
            return self._member_active_events(db, member_id)

    def kick_member(self, token, member_id, reason, *, expected_updated_at=None):
        if not str(reason).strip():
            raise ValueError("탈퇴·거절 사유가 필요합니다.")
        with self.transaction() as db:
            actor = self.require_admin(db, token)
            member = self.get_member(member_id, db)
            member_id = member["id"]
            if expected_updated_at is not None and expected_updated_at != member["updated_at"]:
                raise ValueError("회원 상태가 변경되었습니다. 최신 정보를 확인해주세요.")
            if member["status"] == "KICKED":
                raise ValueError("이미 탈퇴 처리된 회원입니다.")
            admins = self._active_admins(db)
            if any(row["member_id"] == member_id for row in admins) and not any(row["member_id"] != member_id for row in admins):
                raise ValueError("마지막 관리자는 탈퇴 처리할 수 없습니다. 다른 관리자를 먼저 지정해주세요.")
            updated_at = max(now(), (datetime.fromisoformat(member["updated_at"]) + timedelta(microseconds=1)).isoformat(timespec="microseconds"))
            db.execute("UPDATE members SET status='KICKED',updated_at=? WHERE id=?", (updated_at, member_id))
            db.execute("DELETE FROM sessions WHERE account_id IN (SELECT id FROM accounts WHERE member_id=?)", (member_id,))
            db.execute("UPDATE password_resets SET used_at=? WHERE account_id IN (SELECT id FROM accounts WHERE member_id=?) AND used_at IS NULL", (updated_at, member_id))
            self._audit(db, actor, "MEMBER_KICK", member_id, {"before": member["status"], "reason": reason, "active_events": self._member_active_events(db, member_id)})

    def _invalidate_riot_identity(self, db, member_id):
        # Invalidate the identity's generation, including an A→B→A rename
        # while an earlier HTTP request is still in flight.
        from .riot_sync import _job_lock
        _job_lock(self, db)
        db.execute("DELETE FROM riot_jobs WHERE member_id=?", (member_id,))
        db.execute("DELETE FROM riot_profiles WHERE member_id=?", (member_id,))

    def update_own_riot_id(self, token, riot_id, *, expected_updated_at):
        """Rename the approved session's member without changing account identity.

        The version is mandatory: stale profile forms cannot overwrite an admin
        edit or a second rename. Historical rosters and all scoring remain intact.
        """
        display, canonical = identity(riot_id)
        if not isinstance(expected_updated_at, str) or not expected_updated_at.strip():
            raise ValueError("최신 회원 정보를 불러온 뒤 다시 확인해주세요.")
        with self.transaction() as db:
            actor = self.require_member(db, token)
            member_id = actor["member_id"]
            old = self.get_member(member_id, db)
            if expected_updated_at != old["updated_at"]:
                raise ValueError("회원 정보가 변경되었습니다. 최신 정보를 불러온 뒤 다시 확인해주세요.")
            if display == old["riot_id"]:
                return member_id
            identity_changed = canonical != old["canonical_id"]
            updated_at = max(now(), (datetime.fromisoformat(old["updated_at"]) + timedelta(microseconds=1)).isoformat(timespec="microseconds"))
            try:
                db.execute("UPDATE members SET riot_id=?,canonical_id=?,updated_at=? WHERE id=?",
                           (display, canonical, updated_at, member_id))
            except sqlite3.IntegrityError:
                raise ValueError("이미 등록된 Riot ID입니다.") from None
            if identity_changed:
                self._invalidate_riot_identity(db, member_id)
                db.execute("UPDATE members SET current_tier='',current_tier_lp=NULL,current_tier_source='manual',current_tier_updated_at=NULL WHERE id=?", (member_id,))
            db.execute("UPDATE accounts SET display_name=? WHERE id=? AND member_id=?", (display, actor["id"], member_id))
            self._audit(db, actor, "MEMBER_SELF_RENAME", member_id,
                        {"riot_id_before": old["riot_id"], "riot_id": display,
                         "canonical_id_before": old["canonical_id"], "canonical_id": canonical,
                         "riot_profile_invalidated": identity_changed})
            return member_id

    def update_member(self, token, member_id, riot_id, main_role, sub_role, base_score, reason, notes=None, *, clan_tier=UNSET, current_tier=UNSET, current_tier_lp=UNSET, expected_updated_at=None):
        if not str(reason).strip():
            raise ValueError("변경 사유가 필요합니다.")
        display, canonical = identity(riot_id)
        main_role, sub_role = role(main_role), role(sub_role)
        base_score = integer(base_score, "기본점수")
        if main_role == sub_role or not 0 <= base_score <= 10000:
            raise ValueError("서로 다른 주·부 포지션과 0~10,000의 기본점수를 입력해주세요.")
        with self.transaction() as db:
            actor = self.require_admin(db, token)
            old = self.get_member(member_id, db)
            if expected_updated_at is not None and expected_updated_at != old["updated_at"]:
                raise ValueError("회원 정보가 변경되었습니다. 최신 정보를 불러온 뒤 다시 확인해주세요.")
            clan_tier = validate_clan_tier(old["clan_tier"] if clan_tier is UNSET else clan_tier)
            current_tier, current_tier_lp = validate_current_tier(
                old["current_tier"] if current_tier is UNSET else current_tier,
                old["current_tier_lp"] if current_tier_lp is UNSET else current_tier_lp)
            api_identity_changed = old["current_tier_source"] == "riot" and canonical != old["canonical_id"]
            if old["current_tier_source"] == "riot" and not api_identity_changed and (current_tier, current_tier_lp) != (old["current_tier"], old["current_tier_lp"]):
                raise ValueError("Riot API로 연결된 현재 티어는 직접 수정할 수 없습니다. Riot 정보를 갱신해주세요.")
            if api_identity_changed:
                current_tier, current_tier_lp = "", None
            updated_at = max(now(), (datetime.fromisoformat(old["updated_at"]) + timedelta(microseconds=1)).isoformat(timespec="microseconds"))
            tier_changed = (current_tier, current_tier_lp) != (old["current_tier"], old["current_tier_lp"])
            tier_updated_at = updated_at if tier_changed else old["current_tier_updated_at"]
            try:
                db.execute("UPDATE members SET riot_id=?,canonical_id=?,main_role=?,sub_role=?,base_score=?,notes=?,clan_tier=?,current_tier=?,current_tier_lp=?,current_tier_updated_at=?,updated_at=? WHERE id=?", (display, canonical, main_role, sub_role, base_score, old["notes"] if notes is None else notes, clan_tier, current_tier, current_tier_lp, tier_updated_at, updated_at, member_id))
            except sqlite3.IntegrityError as exc:
                raise ValueError("이미 등록된 Riot ID입니다.") from exc
            if api_identity_changed:
                db.execute("UPDATE members SET current_tier_source='manual',current_tier_updated_at=NULL WHERE id=?", (member_id,))
            if canonical != old["canonical_id"]:
                self._invalidate_riot_identity(db, member_id)
            db.execute("UPDATE accounts SET display_name=? WHERE member_id=?", (display, member_id))
            if old["base_score"] != base_score:
                db.execute("INSERT INTO base_history(member_id,base_score,effective_at,reason) VALUES(?,?,?,?)", (member_id, base_score, now(), reason))
            self._audit(db, actor, "MEMBER_UPDATE", member_id, {"before": old, "after": self.get_member(member_id, db), "riot_id": display, "base_score": base_score, "reason": reason})

    def adjust_score(self, token, member_id, amount, reason, *, request_key=None):
        amount = integer(amount, "보정량")
        if not amount or abs(amount) > 10000 or not str(reason).strip():
            raise ValueError("0이 아닌 보정량(절댓값 10,000 이하)과 사유를 입력해주세요.")
        member_id = integer(member_id, "회원 번호")
        reason = str(reason).strip()
        if request_key is not None:
            try:
                parsed = UUID(str(request_key))
                if parsed.int == 0:
                    raise ValueError()
                request_key = str(parsed)
            except (ValueError, AttributeError, TypeError):
                raise ValueError("수동 점수 보정 요청의 고유 UUID가 필요합니다.") from None
        with self.transaction() as db:
            actor = self.require_admin(db, token)
            if request_key is not None:
                fingerprint = hashlib.sha256(json.dumps([actor["id"], member_id, amount, reason], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
                existing = db.execute("SELECT ledger_id,fingerprint FROM score_adjustment_requests WHERE request_key=?", (request_key,)).fetchone()
                if existing:
                    if not hmac.compare_digest(existing["fingerprint"], fingerprint):
                        raise ValueError("같은 요청 번호에 다른 점수 보정 내용 또는 처리자가 전달되었습니다.")
                    return existing["ledger_id"]
            self.get_member(member_id, db)
            result = db.execute("INSERT INTO score_ledger(member_id,amount,source,reason,actor_id,created_at) VALUES(?,?,'MANUAL',?,?,?)", (member_id, amount, reason, actor["id"], now())).lastrowid
            if request_key is not None:
                db.execute("INSERT INTO score_adjustment_requests(request_key,fingerprint,ledger_id,actor_id) VALUES(?,?,?,?)", (request_key, fingerprint, result, actor["id"]))
            self._audit(db, actor, "SCORE_ADJUST", member_id, {"amount": amount, "reason": reason})
            return result

    def policy(self, conn=None, at=None):
        with self.read_snapshot(conn) as db:
            return dict(db.execute("SELECT * FROM policies WHERE effective_at<=? ORDER BY effective_at DESC,id DESC LIMIT 1", (at or now(),)).fetchone())

    def policy_history(self):
        with self.read_snapshot() as db:
            return [dict(r) for r in db.execute("SELECT * FROM policies ORDER BY id DESC")]

    def set_policy(self, token, mode="fixed", k=10, threshold=280, high_k=15):
        k, threshold, high_k = integer(k), integer(threshold), integer(high_k)
        if mode not in ("fixed", "bracket") or not (1 <= k <= 100 and 1 <= high_k <= 100):
            raise ValueError("정책 모드와 1~100의 증감량을 확인해주세요.")
        with self.transaction() as db:
            actor = self.require_admin(db, token)
            result = db.execute("INSERT INTO policies(mode,k,threshold,high_k,effective_at,actor_id) VALUES(?,?,?,?,?,?)", (mode, k, threshold, high_k, now(), actor["id"])).lastrowid
            self._audit(db, actor, "POLICY_CREATE", result, {"mode": mode, "k": k, "threshold": threshold, "high_k": high_k})
            return result

    def _team(self, db, entries):
        if len(entries) != 5:
            raise ValueError("각 팀은 정확히 5명이어야 합니다.")
        output = []
        for entry in entries:
            member_id = entry.get("member_id", entry.get("id")) if isinstance(entry, dict) else entry
            member = self.get_member(integer(member_id), db)
            if member["status"] != "APPROVED":
                raise ValueError("승인된 회원만 출전할 수 있습니다.")
            assigned = role(entry.get("role", member["main_role"])) if isinstance(entry, dict) else member["main_role"]
            output.append({"member_id": member["id"], "role": assigned, "score_before": member["score"],
                           "riot_id_snapshot": member["riot_id"], "clan_tier_snapshot": member["clan_tier"],
                           "current_tier_snapshot": member["current_tier"], "current_tier_lp_snapshot": member["current_tier_lp"]})
        if set(p["role"] for p in output) != set(ROLES):
            raise ValueError("각 팀은 5개 포지션에 한 명씩 배정해야 합니다.")
        return output

    def _require_event_game_access(self, db, actor, event_id, kind):
        """An outer transaction does not waive event ownership or finality."""
        event_id = integer(event_id, "대회 번호")
        exists = db.table_exists("competition_events") if self.is_postgres else bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='competition_events'").fetchone())
        if not exists:
            raise ValueError("경기 대상 내전·경매를 찾을 수 없습니다.")
        event = db.execute("SELECT * FROM competition_events WHERE id=?", (event_id,)).fetchone()
        if not event or event["kind"] != kind:
            raise ValueError("경기와 내전·경매 종류가 일치하지 않습니다.")
        if actor["role"] != "admin" and event["created_by"] != actor["id"]:
            raise PermissionError("본인이 진행하는 내전·경매만 변경할 수 있습니다.")
        if event["status"] not in ("READY", "PLAYING"):
            raise ValueError("진행 중인 내전·경매의 경기만 등록하거나 정정할 수 있습니다.")
        if db.execute("SELECT 1 FROM award_batches WHERE event_id=?", (str(event_id),)).fetchone():
            raise ValueError("이미 우승 보상이 지급된 경매의 경기 기록은 변경할 수 없습니다.")

    def record_game(self, token, request_key, team_a_ids, team_b_ids, winner, played_at=None, tournament_id=None, kind="NORMAL", notes="", conn=None, event_id=None, policy_id=None, fixture_id=None):
        if not request_key or len(str(request_key)) > 200:
            raise ValueError("경기의 고유 요청 키가 필요합니다.")
        if winner not in ("A", "B") or kind not in ("NORMAL", "AUCTION"):
            raise ValueError("경기 종류와 승리팀을 확인해주세요.")
        timestamp = now()
        if played_at:
            dt = datetime.fromisoformat(str(played_at).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone(timedelta(hours=9)))
            timestamp = dt.astimezone(timezone.utc).isoformat(timespec="microseconds")
            if dt.astimezone(timezone.utc) > datetime.now(timezone.utc) + timedelta(minutes=5):
                raise ValueError("미래 경기는 확정할 수 없습니다.")
        tournament_id = tournament_id if tournament_id is not None else event_id
        if event_id is not None and str(tournament_id) != str(event_id):
            raise ValueError("대회 번호가 일치하지 않습니다.")
        if tournament_id is not None and conn is None:
            raise ValueError("대회 경기는 대회 진행 서비스를 통해 등록해주세요.")
        if policy_id is not None and (conn is None or tournament_id is None):
            raise ValueError("대회에 고정된 정책만 대회 진행 서비스에서 지정할 수 있습니다.")
        with self._using(conn) as db:
            actor = self.require_event_manager(db, token, tournament_id) if tournament_id is not None else self.require_staff(db, token)
            if tournament_id is not None:
                self._require_event_game_access(db, actor, tournament_id, kind)
            # Fingerprint only submitted identity, never changing current score.
            def signature(entries):
                return sorted((integer(e.get("member_id", e.get("id"))), str(e.get("role", ""))) if isinstance(e, dict) else (integer(e), "") for e in entries)
            payload = [signature(team_a_ids), signature(team_b_ids), winner, kind, str(tournament_id), str(played_at or ""), str(notes), policy_id]
            fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()
            existing = db.execute("SELECT id,fingerprint FROM games WHERE request_key=?", (str(request_key),)).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise ValueError("같은 요청 키에 다른 경기 내용이 전달되었습니다.")
                if tournament_id is not None:
                    fixture = db.execute("SELECT core_game_id FROM competition_games WHERE id=? AND event_id=?", (fixture_id, tournament_id)).fetchone()
                    if not fixture or fixture["core_game_id"] != existing["id"]:
                        raise ValueError("경기 요청 번호가 해당 대진과 일치하지 않습니다.")
                return existing["id"]
            teams = {"A": self._team(db, team_a_ids), "B": self._team(db, team_b_ids)}
            all_ids = [p["member_id"] for players in teams.values() for p in players]
            if len(set(all_ids)) != 10:
                raise ValueError("한 경기에서 같은 회원이 중복 출전할 수 없습니다.")
            if tournament_id is not None:
                self._validate_event_fixture(db, tournament_id, fixture_id, teams, policy_id)
                snapshots = {saved["member_id"]: saved for saved in db.execute("SELECT member_id,riot_id,clan_tier_snapshot,current_tier_snapshot,current_tier_lp_snapshot FROM competition_players WHERE event_id=?", (tournament_id,))}
                for players in teams.values():
                    for player in players:
                        saved = snapshots[player["member_id"]]
                        player["riot_id_snapshot"] = saved["riot_id"]
                        # Unknown historical metadata stays unknown; neither
                        # NULL nor a recorded empty tier takes today's profile.
                        player["clan_tier_snapshot"] = saved["clan_tier_snapshot"]
                        player["current_tier_snapshot"] = saved["current_tier_snapshot"]
                        player["current_tier_lp_snapshot"] = saved["current_tier_lp_snapshot"]
            if policy_id is None:
                policy = self.policy(db, timestamp)
            else:
                found = db.execute("SELECT * FROM policies WHERE id=? AND effective_at<=?", (policy_id, now())).fetchone()
                if not found:
                    raise ValueError("대회 적용 정책을 찾을 수 없습니다.")
                policy = dict(found)
            game_id = db.execute("INSERT INTO games(request_key,fingerprint,kind,tournament_id,played_at,created_at,policy_id,winner,notes,actor_id) VALUES(?,?,?,?,?,?,?,?,?,?)", (str(request_key), fingerprint, kind, None if tournament_id is None else str(tournament_id), timestamp, now(), policy["id"], winner, notes, actor["id"])).lastrowid
            db.execute("INSERT INTO game_revisions(game_id,revision,winner,status,reason,actor_id,created_at) VALUES(?,1,?,'CONFIRMED',?,?,?)", (game_id, winner, "최초 결과 확정", actor["id"], now()))
            for team, players in teams.items():
                for player in players:
                    k = 0 if kind == "AUCTION" else policy["high_k"] if policy["mode"] == "bracket" and player["score_before"] >= policy["threshold"] else policy["k"]
                    delta = k if winner == team else -k
                    db.execute("INSERT INTO game_players(game_id,member_id,team,role,score_before,delta,k,riot_id_snapshot,clan_tier_snapshot,current_tier_snapshot,current_tier_lp_snapshot) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (game_id, player["member_id"], team, player["role"], player["score_before"], delta, k, player["riot_id_snapshot"], player["clan_tier_snapshot"], player["current_tier_snapshot"], player["current_tier_lp_snapshot"]))
                    db.execute("INSERT INTO game_settlements(game_id,revision,member_id,score_before,delta,created_at) VALUES(?,1,?,?,?,?)", (game_id, player["member_id"], player["score_before"], delta, now()))
                    db.execute("INSERT INTO score_ledger(member_id,amount,source,game_id,revision,reason,actor_id,created_at) VALUES(?,?,'GAME',?,1,?,?,?)", (player["member_id"], delta, game_id, "경기 결과 확정", actor["id"], now()))
            if tournament_id is not None:
                # Reserve this fixture with its ledger in the same savepoint.
                # Competition completes the result and propagation in the outer
                # transaction; another request cannot charge this fixture twice.
                db.execute("UPDATE competition_games SET core_game_id=? WHERE id=? AND event_id=?", (game_id, fixture_id, tournament_id))
            self._audit(db, actor, "GAME_RECORD", game_id, {"winner": winner, "policy_id": policy["id"]})
            return game_id

    def _validate_event_fixture(self, db, event_id, fixture_id, teams, policy_id):
        if fixture_id is None:
            raise ValueError("대회 경기의 대진 번호가 필요합니다.")
        fixture = db.execute("SELECT * FROM competition_games WHERE id=? AND event_id=?", (integer(fixture_id, "대진 번호"), event_id)).fetchone()
        if not fixture or fixture["status"] != "PENDING" or fixture["core_game_id"] is not None:
            raise ValueError("확정되지 않은 해당 대회의 대진만 기록할 수 있습니다.")
        if not fixture["team_a"] or not fixture["team_b"] or fixture["team_a"] == fixture["team_b"]:
            raise ValueError("경기에 출전할 두 팀이 확정되지 않았습니다.")
        for side, column in (("A", "team_a"), ("B", "team_b")):
            team = db.execute("SELECT event_id FROM competition_teams WHERE id=?", (fixture[column],)).fetchone()
            expected = sorted((r["member_id"], r["role"]) for r in db.execute("SELECT member_id,role FROM competition_players WHERE event_id=? AND team_id=?", (event_id, fixture[column])))
            submitted = sorted((p["member_id"], p["role"]) for p in teams[side])
            if not team or team["event_id"] != integer(event_id) or submitted != expected:
                raise ValueError("경기 선수와 포지션이 해당 대진의 팀 명단과 일치하지 않습니다.")
        event = db.execute("SELECT policy_snapshot FROM competition_events WHERE id=?", (event_id,)).fetchone()
        expected_policy = json.loads(event["policy_snapshot"])["score_policy"]["id"]
        if policy_id != expected_policy:
            raise ValueError("대회에 고정된 점수 정책으로만 경기를 기록할 수 있습니다.")

    def _revise_game(self, token, game_id, winner, reason, void, conn=None):
        if not str(reason).strip():
            raise ValueError("정정·무효 사유가 필요합니다.")
        with self._using(conn) as db:
            actor = self.require_admin(db, token)
            game = db.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
            if not game:
                raise ValueError("경기를 찾을 수 없습니다.")
            if game["tournament_id"] is not None and conn is None:
                raise ValueError("대회 경기 정정은 대회 진행 서비스를 이용해주세요.")
            if game["tournament_id"] is not None:
                self._require_event_game_access(db, actor, game["tournament_id"], game["kind"])
            if game["status"] == "VOID":
                if void:
                    return game_id
                raise ValueError("무효 경기는 승리팀을 수정할 수 없습니다.")
            if not void and winner not in ("A", "B"):
                raise ValueError("승리팀은 A 또는 B여야 합니다.")
            if not void and winner == game["winner"]:
                return game_id
            revision = game["revision"] + 1
            status = "VOID" if void else "CONFIRMED"
            winner = game["winner"] if void else winner
            for player in db.execute("SELECT * FROM game_players WHERE game_id=?", (game_id,)).fetchall():
                delta = 0 if void else player["k"] if player["team"] == winner else -player["k"]
                difference = delta - player["delta"]
                db.execute("INSERT INTO game_settlements(game_id,revision,member_id,score_before,delta,created_at) VALUES(?,?,?,?,?,?)", (game_id, revision, player["member_id"], player["score_before"], delta, now()))
                db.execute("INSERT INTO score_ledger(member_id,amount,source,game_id,revision,reason,actor_id,created_at) VALUES(?,?,'CORRECTION',?,?,?,?,?)", (player["member_id"], difference, game_id, revision, reason, actor["id"], now()))
                db.execute("UPDATE game_players SET delta=? WHERE game_id=? AND member_id=?", (delta, game_id, player["member_id"]))
            db.execute("INSERT INTO game_revisions(game_id,revision,winner,status,reason,actor_id,created_at) VALUES(?,?,?,?,?,?,?)", (game_id, revision, winner, status, reason, actor["id"], now()))
            db.execute("UPDATE games SET winner=?,status=?,revision=? WHERE id=?", (winner, status, revision, game_id))
            self._audit(db, actor, "GAME_VOID" if void else "GAME_CORRECT", game_id, {"winner": winner, "reason": reason})
            return game_id

    def correct_game(self, token, game_id, winner, reason, conn=None):
        return self._revise_game(token, game_id, winner, reason, False, conn)

    def void_game(self, token, game_id, reason, conn=None):
        return self._revise_game(token, game_id, None, reason, True, conn)

    def list_games(self, kind=None):
        with self.read_snapshot() as db:
            return [dict(r) for r in db.execute("SELECT g.*,a.display_name AS actor_name FROM games g JOIN accounts a ON a.id=g.actor_id WHERE (CAST(? AS TEXT) IS NULL OR kind=?) ORDER BY g.id DESC", (kind, kind))]

    def get_game(self, game_id, conn=None):
        with self.read_snapshot(conn) as db:
            row = db.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
            if not row:
                raise ValueError("경기를 찾을 수 없습니다.")
            result = dict(row)
            result["players"] = [dict(r) for r in db.execute("SELECT p.*,COALESCE(p.riot_id_snapshot,m.riot_id) AS riot_id,m.riot_id AS current_riot_id,p.clan_tier_snapshot AS clan_tier,p.current_tier_snapshot AS current_tier,p.current_tier_lp_snapshot AS current_tier_lp FROM game_players p JOIN members m ON m.id=p.member_id WHERE game_id=? ORDER BY team,role", (game_id,))]
            result["ledger"] = [dict(r) for r in db.execute("SELECT * FROM score_ledger WHERE game_id=? ORDER BY id", (game_id,))]
            result["revisions"] = [dict(r) for r in db.execute("SELECT * FROM game_revisions WHERE game_id=? ORDER BY revision", (game_id,))]
            result["settlements"] = [dict(r) for r in db.execute("SELECT * FROM game_settlements WHERE game_id=? ORDER BY revision,member_id", (game_id,))]
            return result

    def _validate_event_award(self, db, actor, event_id, member_ids, units):
        """A supplied transaction is not an authorization capability."""
        exists = db.table_exists("competition_events") if self.is_postgres else bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='competition_events'").fetchone())
        if not exists:
            raise ValueError("보상 대상 대회를 찾을 수 없습니다.")
        event = db.execute("SELECT * FROM competition_events WHERE id=?", (event_id,)).fetchone()
        if not event:
            raise ValueError("보상 대상 대회를 찾을 수 없습니다.")
        if actor["role"] != "admin" and event["created_by"] != actor["id"]:
            raise PermissionError("본인이 진행하는 대회의 우승 보상만 확정할 수 있습니다.")
        if event["kind"] != "AUCTION" or event["status"] not in ("PLAYING", "COMPLETED"):
            raise ValueError("경기가 완료된 경매 대회만 우승 보상을 받을 수 있습니다.")
        games = db.execute("SELECT * FROM competition_games WHERE event_id=?", (event_id,)).fetchall()
        if not games or any(g["status"] not in ("COMPLETED", "BYE") for g in games):
            raise ValueError("모든 대회 경기를 완료한 뒤 보상을 확정해주세요.")
        for game in games:
            if game["status"] == "BYE":
                continue
            result = db.execute("SELECT * FROM games WHERE id=?", (game["core_game_id"],)).fetchone()
            if not result or result["status"] != "CONFIRMED" or result["kind"] != "AUCTION" or str(result["tournament_id"]) != str(event_id):
                raise ValueError("대회 경기와 확정된 경기 기록이 일치하지 않습니다.")
            actual_winner = game["team_a"] if result["winner"] == "A" else game["team_b"]
            if actual_winner != game["winner_team_id"]:
                raise ValueError("대회 승리팀과 확정 경기의 승리팀이 일치하지 않습니다.")
        from .competition import Competition
        winner = Competition.resolve_award_winner(db, event_id)
        if winner is None:
            raise ValueError("우승팀이 확정되지 않았습니다.")
        if event["status"] == "COMPLETED" and event["winner_team_id"] != winner:
            raise ValueError("대회 우승 기록을 먼저 확인해주세요.")
        expected_members = sorted(r[0] for r in db.execute("SELECT member_id FROM competition_players WHERE event_id=? AND team_id=?", (event_id, winner)))
        count = db.execute("SELECT count(*) FROM competition_teams WHERE event_id=?", (event_id,)).fetchone()[0]
        if count not in (4, 6, 8) or len(expected_members) != 5 or expected_members != member_ids:
            raise ValueError("해당 대회의 실제 우승팀 선수 5명에게만 보상할 수 있습니다.")
        if units != {4: 1, 6: 5, 8: 25}[count]:
            raise ValueError("대회 규모에 정해진 우승 보상량과 일치하지 않습니다.")

    def grant_award(self, token, member_ids, units, reason, request_key, event_id=None, conn=None):
        units = integer(units, "보상량")
        if not units or not str(reason).strip() or not request_key:
            raise ValueError("0이 아닌 보상량, 사유, 고유 요청 키가 필요합니다.")
        member_ids = sorted(integer(i) for i in member_ids)
        if not member_ids or len(set(member_ids)) != len(member_ids):
            raise ValueError("보상 회원이 비어 있거나 중복되었습니다.")
        with self._using(conn) as db:
            actor = self.require_event_manager(db, token, event_id) if event_id is not None and conn is not None else self.require_admin(db, token)
            if event_id is not None:
                event_id = integer(event_id, "대회 번호")
                self._validate_event_award(db, actor, event_id, member_ids, units)
                request_key = f"tournament:{event_id}"
                reason = "경매 대회 우승"
            fingerprint = hashlib.sha256(json.dumps([member_ids, units, reason, str(event_id)]).encode()).hexdigest()
            existing = db.execute("SELECT id,fingerprint FROM award_batches WHERE request_key=?", (str(request_key),)).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise ValueError("같은 보상 키에 다른 내용이 전달되었습니다.")
                return existing["id"]
            for member_id in member_ids:
                self.get_member(member_id, db)
                balance = db.execute("SELECT COALESCE(sum(units),0) FROM award_ledger WHERE member_id=?", (member_id,)).fetchone()[0]
                if balance + units < 0:
                    raise ValueError("보상을 보유량보다 많이 회수할 수 없습니다.")
            batch_id = db.execute("INSERT INTO award_batches(request_key,fingerprint,event_id,reason,actor_id,created_at) VALUES(?,?,?,?,?,?)", (str(request_key), fingerprint, None if event_id is None else str(event_id), reason, actor["id"], now())).lastrowid
            for member_id in member_ids:
                db.execute("INSERT INTO award_ledger(batch_id,member_id,units,reason,created_at) VALUES(?,?,?,?,?)", (batch_id, member_id, units, reason, now()))
            self._audit(db, actor, "AWARD_GRANT", batch_id, {"member_ids": member_ids, "units": units, "event_id": event_id, "reason": reason})
            return batch_id

    def award_tournament(self, token, eventid, winner_ids, team_count, request_key, conn=None):
        team_count = integer(team_count, "팀 수")
        if eventid is None or not request_key or team_count not in (4, 6, 8) or len(winner_ids) != 5:
            raise ValueError("정식 4·6·8팀 경매 우승팀 5명에게만 지급할 수 있습니다.")
        # A tournament has one original award even when a UI retry obtains a new key.
        return self.grant_award(token, winner_ids, {4: 1, 6: 5, 8: 25}[int(team_count)], "경매 대회 우승", f"tournament:{eventid}", event_id=eventid, conn=conn)

    def award_summary(self):
        return [{k: m[k] for k in ("id", "riot_id", "award_units", "cats", "stars", "medals", "trophies")} for m in self.list_members(True)]

    def list_adjustments(self, token=None):
        with self.read_snapshot() as db:
            self.require_admin(db, token)
            return [dict(r) for r in db.execute("SELECT l.*,m.riot_id,a.display_name AS actor_name FROM score_ledger l JOIN members m ON m.id=l.member_id JOIN accounts a ON a.id=l.actor_id WHERE source='MANUAL' ORDER BY l.id DESC")]

    def audit_log(self, limit=100, token=None):
        with self.read_snapshot() as db:
            self.require_admin(db, token)
            return [dict(r) for r in db.execute("SELECT q.*,a.display_name AS actor_name FROM audit q LEFT JOIN accounts a ON a.id=q.actor_id ORDER BY q.id DESC LIMIT ?", (min(1000, max(1, int(limit))),))]

    @staticmethod
    def csv_bytes(rows):
        rows = list(rows)
        output = io.StringIO(newline="")
        if rows:
            writer = csv.DictWriter(output, fieldnames=list(rows[0]), extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                safe = {k: ("'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else value) for k, value in row.items()}
                writer.writerow(safe)
        return output.getvalue().encode("utf-8-sig")

    def export_members_csv(self):
        public_fields = ("riot_id", "main_role", "sub_role", "score", "wins", "losses", "cats", "stars", "medals", "trophies")
        return self.csv_bytes([{key: member[key] for key in public_fields} for member in self.list_members()])

    def export_games_csv(self):
        return self.csv_bytes(self.list_games())


Service = Core
