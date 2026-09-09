"""PostgreSQL storage for the existing transactional domain services.

Only the application's SQLite DB-API conventions are adapted: qmark parameters,
dual-access rows, generated IDs, and its two explicit BEGIN modes. Schema DDL is
PostgreSQL-specific and versioned; arbitrary SQLite SQL is not translated.
Importing this module never connects, reads credentials, or changes a database.
"""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
import atexit
import hashlib
import os
import re
import sqlite3
import threading
from time import monotonic


SCHEMA_VERSION = 6
POOL_MAX_SIZE = 6
POOL_TIMEOUT = 5.0
_pool_lock = threading.Lock()
_active_pool = None
_SCHEMA = re.compile(r"rolymoly(?:_qa_[a-f0-9]{8,40})?\Z")
_QA_SCHEMA = re.compile(r"rolymoly_qa_[a-f0-9]{8,40}\Z")
_ID_TABLES = frozenset({
    "members", "accounts", "base_history", "policies", "games", "game_revisions",
    "game_settlements", "score_ledger", "award_batches", "award_ledger", "audit",
    "competition_events", "competition_teams", "competition_players",
    "competition_games", "competition_audit", "live_lots", "live_bids",
    "live_events", "competition_game_archives",
})


def validate_schema(schema):
    if not isinstance(schema, str) or not _SCHEMA.fullmatch(schema):
        raise ValueError("허용된 운영 또는 임시 검증 스키마 이름이 필요합니다.")
    return schema


def parse_uri(uri):
    if not isinstance(uri, str) or not uri.startswith("supabase://"):
        raise ValueError("Supabase 저장소 주소 형식을 확인해 주세요.")
    return validate_schema(uri[len("supabase://"):])


def advisory_key(schema):
    """The same signed int64 key is shared by writers, migrations, and workers."""
    schema = validate_schema(schema)
    digest = hashlib.sha256(("rolymoly:postgres:writer:" + schema).encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class Row:
    """sqlite3.Row's int/name access with a normal mapping conversion."""
    __slots__ = ("_names", "_values", "_indexes")

    def __init__(self, names, values):
        self._names = tuple(names)
        self._values = tuple(values)
        self._indexes = {}
        for index, name in enumerate(self._names):
            self._indexes.setdefault(name, index)

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        return self._values[self._indexes[key]]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def keys(self):
        return list(self._names)


def _row_factory(cursor):
    names = tuple(column.name for column in cursor.description or ())
    return lambda values: Row(names, values)


def _regions(query):
    """Yield SQL code, quoted tokens and comments without changing their text."""
    index, size, start = 0, len(query), 0
    while index < size:
        opening, kind, end = query[index], None, index
        if query.startswith("--", index):
            kind = "comment"
            found = query.find("\n", index + 2)
            end = size if found < 0 else found + 1
        elif query.startswith("/*", index):
            kind, depth, end = "comment", 1, index + 2
            while end < size and depth:
                if query.startswith("/*", end):
                    depth += 1
                    end += 2
                elif query.startswith("*/", end):
                    depth -= 1
                    end += 2
                else:
                    end += 1
            if depth:
                raise sqlite3.ProgrammingError("닫히지 않은 SQL 주석입니다.")
        elif opening in ("'", '"'):
            kind, end = "quoted", index + 1
            escape = opening == "'" and index > 0 and query[index - 1] in "eE" and (index < 2 or not (query[index - 2].isalnum() or query[index - 2] == "_"))
            while end < size:
                if escape and query[end] == "\\":
                    end += 2
                elif query[end] == opening:
                    if end + 1 < size and query[end + 1] == opening:
                        end += 2
                    else:
                        end += 1
                        break
                else:
                    end += 1
            else:
                raise sqlite3.ProgrammingError("닫히지 않은 SQL 문자열입니다.")
        elif opening == "$":
            tag = re.match(r"\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$", query[index:])
            if tag:
                kind = "quoted"
                marker = tag.group()
                found = query.find(marker, index + len(marker))
                if found < 0:
                    raise sqlite3.ProgrammingError("닫히지 않은 SQL 문자열입니다.")
                end = found + len(marker)
        if kind:
            if index > start:
                yield "code", query[start:index]
            yield kind, query[index:end]
            index = start = end
        else:
            index += 1
    if start < size:
        yield "code", query[start:]


def _bind_query(query, bind=True):
    output = []
    for kind, text in _regions(query):
        if bind:
            text = text.replace("%", "%%")
            if kind == "code":
                text = text.replace("?", "%s")
        output.append(text)
    return "".join(output)


def _script_statements(script):
    current = []
    for kind, text in _regions(script):
        if kind != "code":
            current.append(text)
            continue
        parts = text.split(";")
        for index, part in enumerate(parts):
            current.append(part)
            if index < len(parts) - 1:
                statement = "".join(current).strip()
                if statement:
                    yield statement
                current = []
    statement = "".join(current).strip()
    if statement:
        yield statement


def _with_generated_id(query):
    regions = list(_regions(query))
    code = "".join(text if kind == "code" else " " * len(text) for kind, text in regions)
    match = re.match(r"\s*INSERT\s+INTO\s+([a-z_][a-z_0-9]*)\b", code, re.IGNORECASE)
    if not match or match.group(1).lower() not in _ID_TABLES or re.search(r"\bRETURNING\b", code, re.IGNORECASE):
        return query, False
    offset, last = 0, 0
    for kind, text in regions:
        if kind == "quoted":
            last = offset + len(text)
        elif kind == "code":
            meaningful = text.rstrip().rstrip(";").rstrip()
            if meaningful:
                last = offset + len(meaningful)
        offset += len(text)
    return query[:last] + " RETURNING id" + query[last:], True


def _driver():
    try:
        import psycopg
    except ImportError:
        raise RuntimeError("Supabase 저장소를 사용하려면 psycopg 패키지를 설치해 주세요.") from None
    return psycopg


def _database_error(error):
    """Preserve domain catch contracts without exposing credentials or row data."""
    psycopg = _driver()
    state = getattr(error, "sqlstate", None)
    suffix = f" (SQLSTATE {state})" if state and re.fullmatch(r"[A-Z0-9]{5}", state) else ""
    if isinstance(error, psycopg.IntegrityError):
        translated = sqlite3.IntegrityError("데이터 제약조건을 확인해 주세요." + suffix)
    elif isinstance(error, (psycopg.OperationalError, psycopg.InterfaceError)):
        translated = sqlite3.OperationalError("Supabase 데이터베이스 연결 또는 작업 상태를 확인해 주세요." + suffix)
    else:
        translated = sqlite3.DatabaseError("PostgreSQL 저장소 작업을 완료하지 못했습니다." + suffix)
    translated.sqlstate = state
    return translated


def _batch_select(query, parameters):
    """Accept one plain SELECT, never a write, lock or SQL function call.

    This internal batch API only needs the auction's independent row queries.
    Values remain bound parameters; it is not a general SQL execution API.
    """
    if not isinstance(query, str):
        raise TypeError("SQL은 문자열로 전달해 주세요.")
    statements = list(_script_statements(query))
    if len(statements) != 1:
        raise sqlite3.ProgrammingError("조회 묶음에는 한 문장씩 전달해 주세요.")
    statement = statements[0]
    # Preserve quoted identifiers as tokens, while ignoring quoted values and
    # comments, so a quoted function name cannot bypass the no-calls rule.
    code = " ".join(text if kind == "code" else "__quoted_identifier__"
                    if kind == "quoted" and text.startswith('"') else " "
                    for kind, text in _regions(statement))
    if (not re.match(r"\s*SELECT\b", code, re.IGNORECASE)
            or re.search(r"\b(?:WITH|INSERT|UPDATE|DELETE|MERGE|INTO|FOR|LOCK|CALL|DO|COPY|TRUNCATE|CREATE|ALTER|DROP|GRANT|REVOKE|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE|SET|RESET)\b", code, re.IGNORECASE)):
        raise sqlite3.ProgrammingError("조회 묶음에는 읽기 전용 SELECT만 사용할 수 있습니다.")
    for match in re.finditer(r"\b([A-Za-z_][A-Za-z_0-9]*)\s*\(", code):
        if match.group(1).upper() not in {"SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "IN", "EXISTS"}:
            raise sqlite3.ProgrammingError("조회 묶음에서는 SQL 함수 호출을 사용할 수 없습니다.")
    return _bind_query(statement, parameters is not None), parameters


class Cursor:
    def __init__(self, cursor, *, generated_id=False):
        self._cursor = cursor
        self.lastrowid = None
        if generated_id:
            result = cursor.fetchone()
            self.lastrowid = result[0] if result else None

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchmany(self, size=1):
        return self._cursor.fetchmany(size)

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def description(self):
        return self._cursor.description

    def __iter__(self):
        return iter(self._cursor)

    def close(self):
        self._cursor.close()


class PostgresConnection:
    """One exclusive connection lease; each transaction has a local schema.

    SET LOCAL is performed inside each transaction, including one-statement
    operations. No session-level search_path or advisory locks are required,
    so transaction-pooler reuse cannot redirect the next statement's schema.
    """
    is_postgres = True

    def __init__(self, schema, raw, *, pool=None):
        self.schema = validate_schema(schema)
        self._raw = raw
        self._pool = pool
        self._closed = False

    def _require_open(self):
        if self._closed:
            raise sqlite3.ProgrammingError("이미 반환한 저장소 연결입니다.")

    @property
    def in_transaction(self):
        if self._closed or self._raw.closed:
            return False
        return self._raw.info.transaction_status != _driver().pq.TransactionStatus.IDLE

    def _pipeline(self):
        capability = getattr(getattr(_driver(), "capabilities", None), "has_pipeline", None)
        if callable(capability) and capability():
            return self._raw.pipeline()
        return nullcontext()

    def _begin(self, *, writer):
        if self.in_transaction:
            raise sqlite3.ProgrammingError("이미 진행 중인 트랜잭션입니다.")
        psycopg = _driver()
        mode = "BEGIN ISOLATION LEVEL READ COMMITTED" if writer else "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"
        with self._pipeline():
            result = self._raw.execute(mode)
            self._raw.execute(psycopg.sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(psycopg.sql.Identifier(self.schema)))
            self._raw.execute("SET LOCAL lock_timeout = '15s'")
            if writer:
                self._raw.execute("SELECT pg_advisory_xact_lock(%s)", (advisory_key(self.schema),))
        # Do not return before the pipeline has confirmed setup and the lock.
        return Cursor(result)

    def fetch_batches(self, statements):
        """Fetch plain SELECTs together inside the caller's existing transaction.

        All statements are checked before sending any SQL. Fetching starts only
        after pipeline synchronization. The caller retains commit/rollback
        ownership, including when a driver error is reported at synchronization.
        Without libpq pipeline support the same queries execute sequentially.
        """
        self._require_open()
        if not self.in_transaction:
            raise sqlite3.ProgrammingError("조회 묶음에는 진행 중인 트랜잭션이 필요합니다.")
        prepared = [_batch_select(query, parameters) for query, parameters in statements]
        if not prepared:
            return []
        psycopg = _driver()
        try:
            with self._pipeline():
                cursors = [self._raw.execute(query, parameters) for query, parameters in prepared]
            return [cursor.fetchall() for cursor in cursors]
        except psycopg.Error as error:
            raise _database_error(error) from None

    def execute(self, query, parameters=None):
        self._require_open()
        if not isinstance(query, str):
            raise TypeError("SQL은 문자열로 전달해 주세요.")
        control = query.strip().rstrip(";").strip().upper()
        psycopg = _driver()
        owned = False
        try:
            if control in ("BEGIN", "BEGIN TRANSACTION", "BEGIN IMMEDIATE"):
                return self._begin(writer=control == "BEGIN IMMEDIATE")
            if control.startswith("PRAGMA ") or "sqlite_master" in control.lower() or "sqlite_sequence" in control.lower():
                raise sqlite3.ProgrammingError("PostgreSQL에서는 저장소 메타데이터 API를 사용해 주세요.")
            if not self.in_transaction:
                if re.match(r"\s*(SAVEPOINT|RELEASE|ROLLBACK\s+TO)\b", query, re.IGNORECASE):
                    raise sqlite3.ProgrammingError("SAVEPOINT에는 진행 중인 트랜잭션이 필요합니다.")
                if control in ("COMMIT", "ROLLBACK"):
                    return Cursor(self._raw.execute(control))
                first_code = " ".join(text for kind, text in _regions(query) if kind == "code").lstrip()
                owned = True
                self._begin(writer=not bool(re.match(r"(?:SELECT|SHOW)\b", first_code, re.IGNORECASE)))
            query, generated = _with_generated_id(query)
            cursor = Cursor(self._raw.execute(_bind_query(query, parameters is not None), parameters), generated_id=generated)
            if owned:
                self._raw.commit()
            return cursor
        except psycopg.Error as error:
            if owned:
                self._raw.rollback()
            raise _database_error(error) from None
        except BaseException:
            if owned:
                self._raw.rollback()
            raise

    def executemany(self, query, parameters):
        self._require_open()
        psycopg = _driver()
        owned = not self.in_transaction
        try:
            if owned:
                self._begin(writer=True)
            cursor = self._raw.cursor()
            cursor.executemany(_bind_query(query), parameters)
            if owned:
                self._raw.commit()
            return Cursor(cursor)
        except psycopg.Error as error:
            if owned:
                self._raw.rollback()
            raise _database_error(error) from None
        except BaseException:
            if owned:
                self._raw.rollback()
            raise

    def executescript(self, script):
        """Execute PostgreSQL DDL/script statements in one existing/new write txn."""
        self._require_open()
        owned = not self.in_transaction
        try:
            if owned:
                self.execute("BEGIN IMMEDIATE")
            result = None
            for statement in _script_statements(script):
                result = self.execute(statement)
            if owned:
                self.commit()
            return result
        except BaseException:
            if owned:
                self.rollback()
            raise

    def table_exists(self, name):
        return bool(self.execute("SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_schema=? AND table_name=? AND table_type='BASE TABLE')", (self.schema, name)).fetchone()[0])

    def table_columns(self, name):
        return {row[0] for row in self.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=? AND table_name=? ORDER BY ordinal_position", (self.schema, name))}

    def commit(self):
        self._require_open()
        try:
            self._raw.commit()
        except _driver().Error as error:
            raise _database_error(error) from None

    def rollback(self):
        self._require_open()
        try:
            self._raw.rollback()
        except _driver().Error as error:
            raise _database_error(error) from None

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._pool is None:
            self._raw.close()
            return
        # Core.read_snapshot deliberately exits without committing. Finish that
        # transaction before returning the lease; never commit from close(). A
        # failed rollback discards this connection instead of reusing its state.
        try:
            if not self._raw.closed and self._raw.info.transaction_status != _driver().pq.TransactionStatus.IDLE:
                self._raw.rollback()
        except _driver().Error:
            self._raw.close()
        finally:
            self._pool.putconn(self._raw)

    def __enter__(self):
        return self

    def __exit__(self, error_type, error, traceback):
        try:
            self.rollback() if error_type else self.commit()
        finally:
            self.close()


def _pooled_connection_class():
    """Keep background pool diagnostics free of hosts, users and driver text."""
    driver = _driver()

    class ApplicationConnection(driver.Connection):
        @classmethod
        def connect(cls, *args, **kwargs):
            try:
                return super().connect(*args, **kwargs)
            except driver.Error:
                raise driver.OperationalError("저장소 연결을 준비하지 못했습니다.") from None

        def __repr__(self):
            return "<Rolymoly PostgreSQL connection>"

    return ApplicationConnection


def _reset_pool_connection(raw):
    """Reset SQL session state and client defaults before another checkout.

    The pool performs this in its worker before making the connection available.
    DISCARD ALL also clears temporary objects and session advisory locks; SET
    LOCAL still chooses the schema for every individual transaction.
    """
    driver = _driver()
    try:
        raw.autocommit = True
        raw.read_only = None
        raw.isolation_level = None
        raw.deferrable = None
        raw.prepare_threshold = None
        raw.row_factory = _row_factory
        raw.execute("DISCARD ALL")
        raw._roly_released_at = monotonic()
    except driver.Error:
        raise driver.OperationalError("저장소 연결을 정리하지 못했습니다.") from None


def _check_pool_connection(raw):
    """Check long-idle sockets without an extra network query on every poll."""
    driver = _driver()
    try:
        if raw.closed:
            raise driver.OperationalError("저장소 연결이 종료되었습니다.")
        released = getattr(raw, "_roly_released_at", None)
        if released is not None and monotonic() - released >= 30:
            raw.execute("SELECT 1")
    except driver.Error:
        raise driver.OperationalError("저장소 연결을 다시 준비하고 있습니다.") from None


def _connection_pool(kwargs):
    global _active_pool
    try:
        from psycopg_pool import ConnectionPool
    except ImportError:
        raise RuntimeError("Supabase 저장소를 사용하려면 psycopg_pool 패키지를 설치해 주세요.") from None
    # Credentials are only used in this opaque process-local identity, never
    # in the pool name or logs. QA schemas share the same small connection cap.
    identity = (os.getpid(), hashlib.sha256(repr(sorted(kwargs.items())).encode()).digest())
    with _pool_lock:
        previous = _active_pool
        if previous and previous[0] == identity and not previous[1].closed:
            return previous[1]
        pool = ConnectionPool(connection_class=_pooled_connection_class(), kwargs=kwargs,
                              name="rolymoly", min_size=1, max_size=POOL_MAX_SIZE,
                              timeout=POOL_TIMEOUT, max_waiting=64, num_workers=2,
                              max_idle=60, max_lifetime=600, reconnect_timeout=10,
                              check=_check_pool_connection, reset=_reset_pool_connection, open=True)
        _active_pool = (identity, pool)
    if previous and previous[0][0] == identity[0]:
        # In-flight leases keep their own pool reference; a retired pool closes
        # those connections on return rather than lending them to new settings.
        previous[1].close()
    return pool


def close_pools():
    """Release this process's idle connections (also used by QA/CLI shutdown)."""
    global _active_pool
    with _pool_lock:
        previous, _active_pool = _active_pool, None
    if previous and previous[0][0] == os.getpid():
        previous[1].close()


atexit.register(close_pools)


def connect(schema="rolymoly"):
    schema = validate_schema(schema)
    from .storage_config import postgres_kwargs
    psycopg = _driver()
    kwargs = dict(postgres_kwargs())
    kwargs.update(autocommit=True, row_factory=_row_factory)
    try:
        pool = _connection_pool(kwargs)
        return PostgresConnection(schema, pool.getconn(), pool=pool)
    except psycopg.Error as error:
        raise _database_error(error) from None


# Preserve the domain's TEXT/INTEGER return contracts. In particular, epoch
# seconds MUST be double precision: PostgreSQL REAL cannot distinguish 3/5s at
# contemporary Unix timestamps. Tables are ordered before their FK consumers.
_SCHEMA_DDL = """
CREATE TABLE members(id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,riot_id TEXT NOT NULL,canonical_id TEXT NOT NULL UNIQUE,main_role TEXT NOT NULL,sub_role TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('PENDING','APPROVED','KICKED')),base_score INTEGER NOT NULL DEFAULT 0,notes TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE accounts(id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,username TEXT NOT NULL UNIQUE,display_name TEXT NOT NULL,password_hash TEXT NOT NULL,salt TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('admin','organizer','member')),active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,member_id BIGINT REFERENCES members(id));
CREATE TABLE sessions(token_hash TEXT PRIMARY KEY,account_id BIGINT NOT NULL REFERENCES accounts(id),expires_at TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE login_failures(username TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE base_history(id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,member_id BIGINT NOT NULL REFERENCES members(id),base_score INTEGER NOT NULL,effective_at TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE policies(id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,mode TEXT NOT NULL CHECK(mode IN ('fixed','bracket')),k INTEGER NOT NULL CHECK(k>0),threshold INTEGER NOT NULL,high_k INTEGER NOT NULL CHECK(high_k>0),effective_at TEXT NOT NULL,actor_id BIGINT REFERENCES accounts(id));
CREATE TABLE games(id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,request_key TEXT NOT NULL UNIQUE,fingerprint TEXT NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('NORMAL','AUCTION')),tournament_id TEXT,played_at TEXT NOT NULL,created_at TEXT NOT NULL,policy_id BIGINT NOT NULL REFERENCES policies(id),winner TEXT NOT NULL CHECK(winner IN ('A','B')),status TEXT NOT NULL DEFAULT 'CONFIRMED' CHECK(status IN ('CONFIRMED','VOID')),revision INTEGER NOT NULL DEFAULT 1,notes TEXT NOT NULL DEFAULT '',actor_id BIGINT NOT NULL REFERENCES accounts(id));
CREATE TABLE game_players(game_id BIGINT NOT NULL REFERENCES games(id),member_id BIGINT NOT NULL REFERENCES members(id),team TEXT NOT NULL CHECK(team IN ('A','B')),role TEXT NOT NULL,score_before INTEGER NOT NULL,delta INTEGER NOT NULL,k INTEGER NOT NULL,PRIMARY KEY(game_id,member_id),UNIQUE(game_id,team,role));
CREATE TABLE game_revisions(id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,game_id BIGINT NOT NULL REFERENCES games(id),revision INTEGER NOT NULL,winner TEXT,status TEXT NOT NULL,reason TEXT NOT NULL,actor_id BIGINT NOT NULL REFERENCES accounts(id),created_at TEXT NOT NULL,UNIQUE(game_id,revision));
CREATE TABLE game_settlements(id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,game_id BIGINT NOT NULL REFERENCES games(id),revision INTEGER NOT NULL,member_id BIGINT NOT NULL REFERENCES members(id),score_before INTEGER NOT NULL,delta INTEGER NOT NULL,created_at TEXT NOT NULL,UNIQUE(game_id,revision,member_id));
CREATE TABLE score_ledger(id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,member_id BIGINT NOT NULL REFERENCES members(id),amount INTEGER NOT NULL,source TEXT NOT NULL,game_id BIGINT REFERENCES games(id),revision INTEGER,reason TEXT NOT NULL,actor_id BIGINT NOT NULL REFERENCES accounts(id),created_at TEXT NOT NULL);
CREATE TABLE award_batches(id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,request_key TEXT NOT NULL UNIQUE,fingerprint TEXT NOT NULL,event_id TEXT,reason TEXT NOT NULL,actor_id BIGINT NOT NULL REFERENCES accounts(id),created_at TEXT NOT NULL);
CREATE TABLE award_ledger(id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,batch_id BIGINT NOT NULL REFERENCES award_batches(id),member_id BIGINT NOT NULL REFERENCES members(id),units INTEGER NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(batch_id,member_id));
CREATE TABLE audit(id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,actor_id BIGINT REFERENCES accounts(id),action TEXT NOT NULL,target TEXT NOT NULL,details TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE competition_events(id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,title TEXT NOT NULL,kind TEXT NOT NULL,format TEXT NOT NULL,status TEXT NOT NULL,created_by BIGINT NOT NULL,created_at TEXT NOT NULL,current_player_id BIGINT,winner_team_id BIGINT,policy_snapshot TEXT NOT NULL,starts_at TEXT,description TEXT NOT NULL DEFAULT '',build_mode TEXT NOT NULL DEFAULT '',team_count INTEGER NOT NULL DEFAULT 0,workflow_version INTEGER NOT NULL DEFAULT 0,participants_confirmed_at TEXT);
CREATE TABLE competition_teams(id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,event_id BIGINT NOT NULL REFERENCES competition_events(id),name TEXT NOT NULL,captain_id BIGINT,budget INTEGER NOT NULL DEFAULT 0);
CREATE TABLE competition_players(id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,event_id BIGINT NOT NULL REFERENCES competition_events(id),member_id BIGINT NOT NULL,riot_id TEXT NOT NULL,role TEXT NOT NULL,score DOUBLE PRECISION NOT NULL,team_id BIGINT REFERENCES competition_teams(id),price INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL DEFAULT 'AVAILABLE',participation_status TEXT NOT NULL DEFAULT 'SELECTED',attendance_status TEXT NOT NULL DEFAULT 'NOT_REQUESTED',attendance_requested_at TEXT,attendance_confirmed_at TEXT,warning_count INTEGER NOT NULL DEFAULT 0,exclusion_reason TEXT NOT NULL DEFAULT '',tier_snapshot TEXT,main_role_snapshot TEXT,sub_role_snapshot TEXT,UNIQUE(event_id,member_id));
CREATE TABLE competition_games(id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,event_id BIGINT NOT NULL REFERENCES competition_events(id),round INTEGER NOT NULL,position INTEGER NOT NULL,group_key TEXT NOT NULL DEFAULT '',stage TEXT NOT NULL DEFAULT 'MAIN',team_a BIGINT REFERENCES competition_teams(id),team_b BIGINT REFERENCES competition_teams(id),source_a BIGINT REFERENCES competition_games(id),source_b BIGINT REFERENCES competition_games(id),winner_team_id BIGINT REFERENCES competition_teams(id),status TEXT NOT NULL DEFAULT 'PENDING',core_game_id BIGINT,source_a_result TEXT NOT NULL DEFAULT 'WINNER',source_b_result TEXT NOT NULL DEFAULT 'WINNER',attempt INTEGER NOT NULL DEFAULT 1,UNIQUE(event_id,stage,group_key,round,position));
CREATE TABLE competition_audit(id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,event_id BIGINT NOT NULL,actor_id BIGINT NOT NULL,action TEXT NOT NULL,detail TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE live_sessions(event_id BIGINT PRIMARY KEY REFERENCES competition_events(id),status TEXT NOT NULL CHECK(status IN ('READY','RUNNING','WAITING','PAUSED','COMPLETED','CANCELLED')),bid_seconds INTEGER NOT NULL,reset_on_bid INTEGER NOT NULL,transition_seconds INTEGER NOT NULL DEFAULT 3,current_lot_id BIGINT,next_at DOUBLE PRECISION,paused_phase TEXT,pause_remaining DOUBLE PRECISION,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE live_lots(id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,event_id BIGINT NOT NULL REFERENCES live_sessions(event_id),member_id BIGINT NOT NULL REFERENCES members(id),sequence INTEGER NOT NULL,attempt INTEGER NOT NULL DEFAULT 1,status TEXT NOT NULL CHECK(status IN ('QUEUED','OPEN','SOLD','UNSOLD','CANCELLED')),highest_team_id BIGINT REFERENCES competition_teams(id),highest_bid INTEGER,opened_at DOUBLE PRECISION,closes_at DOUBLE PRECISION,closed_at DOUBLE PRECISION,UNIQUE(event_id,sequence),UNIQUE(event_id,member_id,attempt));
CREATE TABLE live_bids(id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,request_id TEXT NOT NULL UNIQUE,fingerprint TEXT NOT NULL,event_id BIGINT NOT NULL REFERENCES live_sessions(event_id),lot_id BIGINT NOT NULL REFERENCES live_lots(id),team_id BIGINT NOT NULL REFERENCES competition_teams(id),account_id BIGINT NOT NULL REFERENCES accounts(id),member_id BIGINT NOT NULL REFERENCES members(id),amount INTEGER NOT NULL CHECK(amount>=0),closes_at DOUBLE PRECISION NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE live_events(id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,event_id BIGINT NOT NULL REFERENCES live_sessions(event_id),lot_id BIGINT REFERENCES live_lots(id),actor_id BIGINT REFERENCES accounts(id),type TEXT NOT NULL,detail TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE result_revision_previews(token_hash TEXT PRIMARY KEY,actor_id BIGINT NOT NULL REFERENCES accounts(id),event_id BIGINT NOT NULL REFERENCES competition_events(id),game_id BIGINT NOT NULL,winner_team_id BIGINT NOT NULL,reason TEXT NOT NULL,fingerprint TEXT NOT NULL,plan_json TEXT NOT NULL,created_at TEXT NOT NULL,expires_at TEXT NOT NULL,applied_at TEXT,result_json TEXT);
CREATE TABLE competition_game_archives(id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,event_id BIGINT NOT NULL REFERENCES competition_events(id),fixture_id BIGINT NOT NULL,core_game_id BIGINT,revision_token_hash TEXT NOT NULL REFERENCES result_revision_previews(token_hash),snapshot TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE clan_profile(id INTEGER PRIMARY KEY CHECK(id=1),name TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',founded_on TEXT NOT NULL DEFAULT '',capacity INTEGER,contact_url TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL,poster TEXT NOT NULL DEFAULT 'rolymoly');
CREATE UNIQUE INDEX award_one_per_event ON award_batches(event_id) WHERE event_id IS NOT NULL;
CREATE UNIQUE INDEX account_one_member ON accounts(member_id) WHERE member_id IS NOT NULL;
CREATE UNIQUE INDEX live_one_open ON live_lots(event_id) WHERE status='OPEN';
CREATE INDEX score_member ON score_ledger(member_id);
CREATE INDEX game_member ON game_players(member_id);
CREATE INDEX login_window ON login_failures(username,created_at);
CREATE INDEX live_bid_event ON live_bids(event_id,id);
CREATE INDEX competition_archive_event ON competition_game_archives(event_id,id);
CREATE INDEX game_event ON games(tournament_id,id);
CREATE INDEX team_event ON competition_teams(event_id,id);
CREATE INDEX competition_audit_event ON competition_audit(event_id,id);
CREATE INDEX live_event_event ON live_events(event_id,id);
CREATE INDEX award_member ON award_ledger(member_id);
"""


def initialize(schema="rolymoly"):
    """Atomic, versioned schema creation. Does not import or read SQLite data."""
    schema = validate_schema(schema)
    psycopg = _driver()
    with connect(schema) as connection:
        connection.execute("BEGIN IMMEDIATE")
        raw = connection._raw
        try:
            raw.execute(psycopg.sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(psycopg.sql.Identifier(schema)))
            raw.execute("CREATE TABLE IF NOT EXISTS _schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)")
            row = connection.execute("SELECT COALESCE(MAX(version),0) FROM _schema_migrations").fetchone()
            version = row[0]
            if version > SCHEMA_VERSION:
                raise ValueError("저장소 버전이 앱보다 최신입니다. 앱을 업데이트해 주세요.")
            if version == SCHEMA_VERSION:
                return
            if version not in (0, 1, 2, 3, 4, 5):
                raise ValueError("지원하지 않는 저장소 마이그레이션 버전입니다.")
            stamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            if version == 0:
                connection.executescript(_SCHEMA_DDL)
                connection.execute("INSERT INTO policies(mode,k,threshold,high_k,effective_at) VALUES('fixed',10,280,15,?)", ("1970-01-01T00:00:00.000000+00:00",))
                connection.execute("INSERT INTO clan_profile(id,name,updated_at) VALUES(1,'롤리몰리',?) ON CONFLICT(id) DO NOTHING", (stamp,))
                connection.execute("INSERT INTO _schema_migrations(version,applied_at) VALUES(1,?)", (stamp,))
            # v2 is additive: existing members, credentials, sessions and events
            # retain their identity and existing CHECK constraints.
            if version < 2:
                from .auth import AUTH_DDL
                connection.executescript(AUTH_DDL.replace(" INTEGER", " BIGINT"))
                connection.execute("INSERT INTO _schema_migrations(version,applied_at) VALUES(2,?)", (stamp,))
            if version < 3:
                from .member_profile import initialize_postgres
                initialize_postgres(connection)
                connection.execute("INSERT INTO _schema_migrations(version,applied_at) VALUES(3,?)", (stamp,))
            from .core import SCORE_ADJUSTMENT_DDL
            connection.execute(SCORE_ADJUSTMENT_DDL.replace(" INTEGER", " BIGINT"))
            if version < 4:
                connection.execute("INSERT INTO _schema_migrations(version,applied_at) VALUES(4,?)", (stamp,))
            # Existing mixed notes are private. Never copy them to the new
            # applicant-authored field during an additive upgrade.
            connection.execute("ALTER TABLE members ADD COLUMN IF NOT EXISTS application_notes TEXT NOT NULL DEFAULT ''")
            from .competition import CREATION_REQUESTS_DDL
            connection.execute(CREATION_REQUESTS_DDL.replace(" INTEGER", " BIGINT"))
            connection.execute("ALTER TABLE members ADD COLUMN IF NOT EXISTS current_tier_source TEXT NOT NULL DEFAULT 'manual' CHECK(current_tier_source IN ('manual','riot'))")
            from .riot_sync import RIOT_DDL
            connection.executescript(RIOT_DDL.replace(" INTEGER", " BIGINT"))
            raw.execute(psycopg.sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(psycopg.sql.Identifier(schema)))
            raw.execute(psycopg.sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM PUBLIC").format(psycopg.sql.Identifier(schema)))
            raw.execute(psycopg.sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA {} FROM PUBLIC").format(psycopg.sql.Identifier(schema)))
            # Supabase's public API roles must not gain access to passwords,
            # sessions or ledger tables through broad project default grants.
            roles = [row[0] for row in raw.execute("SELECT rolname FROM pg_roles WHERE rolname IN ('anon','authenticated')")]
            for role in roles:
                role_sql = psycopg.sql.Identifier(role)
                schema_sql = psycopg.sql.Identifier(schema)
                raw.execute(psycopg.sql.SQL("REVOKE ALL ON SCHEMA {} FROM {}").format(schema_sql, role_sql))
                raw.execute(psycopg.sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}").format(schema_sql, role_sql))
                raw.execute(psycopg.sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA {} FROM {}").format(schema_sql, role_sql))
            connection.execute("INSERT INTO _schema_migrations(version,applied_at) VALUES(?,?)", (SCHEMA_VERSION, stamp))
        except psycopg.Error as error:
            raise _database_error(error) from None


def drop_qa_schema(schema):
    """Explicit test cleanup; never accepts the operational rolymoly schema."""
    if not isinstance(schema, str) or not _QA_SCHEMA.fullmatch(schema):
        raise ValueError("임시 검증 스키마만 삭제할 수 있습니다. 운영 저장소는 삭제할 수 없습니다.")
    psycopg = _driver()
    with connect(schema) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection._raw.execute(psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(psycopg.sql.Identifier(schema)))
        except psycopg.Error as error:
            raise _database_error(error) from None
