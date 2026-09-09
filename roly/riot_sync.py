"""Shared Riot work queue, rate budget and identity-bound profile cache.

HTTP is always outside database transactions. Queue/rate transactions use
separate PostgreSQL advisory locks; only final member changes use Core's writer.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import sqlite3
import threading
import time
from uuid import uuid4

from .riot_api import DataDragonClient, MASTERY_LIMIT, RiotAPIError, RiotClient, load_riot_config, profile_icon_url
from .member_profile import validate_current_tier
from .member_ranks import RANK_SELECT, RANK_JOINS, project_member, profile_projection, save_riot_profile

RIOT_DDL = """
CREATE TABLE IF NOT EXISTS riot_profiles(
    member_id INTEGER PRIMARY KEY REFERENCES members(id),canonical_id TEXT NOT NULL,
    payload TEXT NOT NULL,fetched_at DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS riot_jobs(
    member_id INTEGER PRIMARY KEY REFERENCES members(id),canonical_id TEXT NOT NULL,
    lease_id TEXT,lease_until DOUBLE PRECISION NOT NULL DEFAULT 0,
    status TEXT NOT NULL,next_attempt DOUBLE PRECISION NOT NULL,
    partial_payload TEXT NOT NULL DEFAULT '{}',stage INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',requested_at DOUBLE PRECISION NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS riot_jobs_due ON riot_jobs(status,next_attempt,lease_until);
CREATE TABLE IF NOT EXISTS riot_rate_hits(
    id TEXT PRIMARY KEY,key_hash TEXT NOT NULL,requested_at DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS riot_rate_window ON riot_rate_hits(key_hash,requested_at);
CREATE TABLE IF NOT EXISTS riot_rate_cooldowns(
    key_hash TEXT PRIMARY KEY,until_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    blocked INTEGER NOT NULL DEFAULT 0,last_error TEXT NOT NULL DEFAULT '');
"""

TTL_SECONDS = 6 * 60 * 60
FORCE_SECONDS = 5 * 60
LEASE_SECONDS = 60
_ERRORS = {
    "disabled": "Riot API 설정이 필요합니다.", "auth": "Riot API 키를 확인해 주세요.",
    "not_found": "Riot 계정을 찾지 못했습니다.", "rate_limited": "Riot 요청 제한을 기다리고 있습니다.",
    "unavailable": "Riot 정보를 잠시 확인하지 못했습니다.",
    "invalid_response": "Riot 응답을 확인하지 못했습니다.",
    "invalid_request": "Riot ID를 확인해 주세요.",
    "profile_changed": "회원 정보가 변경되어 갱신하지 않았습니다. 다시 요청해 주세요.",
}
_TIERS = {"IRON": "아이언", "BRONZE": "브론즈", "SILVER": "실버", "GOLD": "골드",
          "PLATINUM": "플래티넘", "EMERALD": "에메랄드", "DIAMOND": "다이아몬드",
          "MASTER": "마스터", "GRANDMASTER": "그랜드마스터", "CHALLENGER": "챌린저"}
_DIVISIONS = {"I": 1, "II": 2, "III": 3, "IV": 4}


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _rank_tier(rank):
    if rank["tier"] == "UNRANKED":
        return "언랭크", None
    tier = _TIERS[rank["tier"]]
    if rank["tier"] not in ("MASTER", "GRANDMASTER", "CHALLENGER"):
        tier += " " + str(_DIVISIONS[rank["division"]])
    return validate_current_tier(tier, rank["lp"])


def _lock_key(core, purpose):
    value = f"rolymoly:riot:{core.schema}:{purpose}".encode()
    return int.from_bytes(hashlib.sha256(value).digest()[:8], "big", signed=True)


def _job_lock(core, db):
    if core.is_postgres:
        db.execute("SELECT pg_advisory_xact_lock(?)", (_lock_key(core, "jobs"),))


@contextmanager
def _metadata_transaction(core, purpose):
    """Metadata writes never reserve the auction/domain advisory lock."""
    db = core.connect()
    try:
        if core.is_postgres:
            from .postgres import _driver, _database_error
            driver = _driver()
            try:
                with db._pipeline():
                    db._raw.execute("BEGIN ISOLATION LEVEL READ COMMITTED")
                    db._raw.execute(driver.sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(driver.sql.Identifier(core.schema)))
                    db._raw.execute("SET LOCAL lock_timeout = '5s'")
                    db._raw.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_key(core, purpose),))
            except driver.Error as error:
                raise _database_error(error) from None
        else:
            db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def _time(core, db, clock):
    if clock is not None:
        return float(clock())
    if core.is_postgres:
        return float(db.execute("SELECT EXTRACT(EPOCH FROM clock_timestamp())::double precision").fetchone()[0])
    return time.time()


class DatabaseRateLimiter:
    """One shared application-key budget across all members and DB workers."""
    def __init__(self, core, key_hash, clock=None):
        self.core, self.key_hash, self.clock = core, key_hash, clock

    def status(self):
        """A scheduling hint; reserve() rechecks under the shared rate lock."""
        with self.core.read_snapshot() as db:
            stamp = _time(self.core, db, self.clock)
            row = db.execute("SELECT until_at,blocked,last_error FROM riot_rate_cooldowns WHERE key_hash=?", (self.key_hash,)).fetchone()
            return {"blocked": bool(row and row["blocked"]),
                    "retry_after": max(0.0, row["until_at"] - stamp) if row else 0.0,
                    "last_error": row["last_error"] if row and row["last_error"] in _ERRORS else ""}

    def reset_auth(self):
        """An explicit refresh can retry authentication without clearing 429s."""
        with _metadata_transaction(self.core, "rate:" + self.key_hash) as db:
            stamp = _time(self.core, db, self.clock)
            db.execute("UPDATE riot_rate_cooldowns SET blocked=0,until_at=?,last_error='' WHERE key_hash=? AND blocked=1", (stamp, self.key_hash))

    def reserve(self, scope):
        if scope not in ("asia", "kr"):
            raise RiotAPIError("invalid_request")
        with _metadata_transaction(self.core, "rate:" + self.key_hash) as db:
            stamp = _time(self.core, db, self.clock)
            cooldown = db.execute("SELECT * FROM riot_rate_cooldowns WHERE key_hash=?", (self.key_hash,)).fetchone()
            if cooldown and cooldown["blocked"]:
                raise RiotAPIError("auth")
            if cooldown and cooldown["until_at"] > stamp:
                raise RiotAPIError("rate_limited", retry_after=cooldown["until_at"] - stamp)
            db.execute("DELETE FROM riot_rate_hits WHERE key_hash=? AND requested_at<=?", (self.key_hash, stamp - 120))
            hits = [row[0] for row in db.execute("SELECT requested_at FROM riot_rate_hits WHERE key_hash=? ORDER BY requested_at", (self.key_hash,))]
            recent = [hit for hit in hits if hit > stamp - 1]
            delays = []
            if len(recent) >= 20:
                delays.append(recent[-20] + 1 - stamp)
            if len(hits) >= 100:
                delays.append(hits[-100] + 120 - stamp)
            if delays:
                raise RiotAPIError("rate_limited", retry_after=max(delays) + 0.001)
            db.execute("INSERT INTO riot_rate_hits(id,key_hash,requested_at) VALUES(?,?,?)", (str(uuid4()), self.key_hash, stamp))

    def backoff(self, error):
        blocked = error.code == "auth"
        delay = getattr(error, "retry_after", None)
        delay = float(delay) if isinstance(delay, (float, int)) and math.isfinite(delay) else 60.0
        delay = max(0.001, delay)
        with _metadata_transaction(self.core, "rate:" + self.key_hash) as db:
            stamp = _time(self.core, db, self.clock)
            current = db.execute("SELECT until_at,blocked FROM riot_rate_cooldowns WHERE key_hash=?", (self.key_hash,)).fetchone()
            until = max(stamp + delay if not blocked else stamp, current["until_at"] if current else 0)
            db.execute("INSERT INTO riot_rate_cooldowns(key_hash,until_at,blocked,last_error) VALUES(?,?,?,?) ON CONFLICT(key_hash) DO UPDATE SET until_at=excluded.until_at,blocked=excluded.blocked,last_error=excluded.last_error", (self.key_hash, until, int(blocked or bool(current and current["blocked"])), error.code))


_static_lock = threading.Lock()
_static_cache = {"until": 0.0, "version": "", "champions": {}}


def _static_data():
    """Public Data Dragon metadata is independent of the Riot API-key budget."""
    with _static_lock:
        if _static_cache["until"] > time.time():
            return _static_cache["version"], _static_cache["champions"]
    try:
        client = DataDragonClient()
        version = client.latest_version()
        champions = client.champions(version)
    except Exception:
        with _static_lock:
            _static_cache["until"] = time.time() + 300
            return _static_cache["version"], _static_cache["champions"]
    with _static_lock:
        _static_cache.update(until=time.time() + 86400, version=version, champions=champions)
    return version, champions


class RiotSync:
    def __init__(self, core, config=None, client_factory=None, clock=None, *, rate_core=None):
        self.core = core
        self.rate_core = core if rate_core is None else rate_core
        self.config = load_riot_config() if config is None else config
        self.clock = clock
        self.key_hash = hashlib.sha256(self.config.api_key.encode()).hexdigest()
        self.limiter = DatabaseRateLimiter(self.rate_core, self.key_hash, clock)
        factory = client_factory or RiotClient
        self.client = factory(self.config, before_request=self.limiter.reserve)

    @staticmethod
    def _ids(member_ids):
        from .core import integer
        if isinstance(member_ids, (str, bytes)):
            raise ValueError("회원 번호 목록을 확인해 주세요.")
        ids = list(dict.fromkeys(integer(value, "회원 번호") for value in member_ids))
        if len(ids) > 40 or any(value <= 0 for value in ids):
            raise ValueError("한 번에 최대 40명의 회원을 선택해 주세요.")
        return ids

    def enqueue(self, token, member_ids, force=False):
        if not isinstance(force, bool):
            raise ValueError("갱신 요청을 확인해 주세요.")
        ids = self._ids(member_ids)
        queued = 0
        with self.core.transaction() as db:
            self.core.require_event_manager(db, token)
            _job_lock(self.core, db)
            if not ids:
                return 0
            placeholders = ",".join("?" for _ in ids)
            members = [dict(row) for row in db.execute(f"""SELECT m.id,m.riot_id,m.canonical_id,m.status,rs.updated_at AS current_tier_updated_at,
                j.canonical_id AS job_identity,j.status AS job_status,j.next_attempt,j.requested_at,
                p.canonical_id AS cache_identity,p.fetched_at
                FROM members m LEFT JOIN riot_jobs j ON j.member_id=m.id
                LEFT JOIN riot_profiles p ON p.member_id=m.id {RANK_JOINS} WHERE m.id IN ({placeholders})""", ids)]
            if len(members) != len(ids) or any(member["status"] != "APPROVED" for member in members):
                raise ValueError("승인된 회원만 Riot 정보를 갱신할 수 있습니다.")
            if not self.config.enabled:
                return 0
            stamp = _time(self.core, db, self.clock)
            reset_auth = False
            updates, inserts = [], []
            for member in members:
                same = member["job_identity"] == member["canonical_id"]
                if same and force and member["requested_at"] > stamp - FORCE_SECONDS:
                    continue
                if same and member["job_status"] in ("QUEUED", "RUNNING"):
                    if force:
                        reset_auth = True
                        updates.append((stamp, stamp, member["id"]))
                    continue
                if not force and member["cache_identity"] == member["canonical_id"] and member["fetched_at"] > stamp - TTL_SECONDS:
                    continue
                if same and not force and member["job_status"] == "FAILED" and member["next_attempt"] > stamp:
                    continue
                partial = {"riot_id": member["riot_id"], "tier_version": member["current_tier_updated_at"], "key_hash": self.key_hash}
                inserts.append((member["id"], member["canonical_id"], stamp, _json(partial), stamp))
                reset_auth |= force
                queued += 1
            if inserts:
                db.executemany("INSERT INTO riot_jobs(member_id,canonical_id,lease_id,lease_until,status,next_attempt,partial_payload,stage,last_error,requested_at,attempts) VALUES(?,?,NULL,0,'QUEUED',?,?,0,'',?,0) ON CONFLICT(member_id) DO UPDATE SET canonical_id=excluded.canonical_id,lease_id=NULL,lease_until=0,status='QUEUED',next_attempt=excluded.next_attempt,partial_payload=excluded.partial_payload,stage=0,last_error='',requested_at=excluded.requested_at,attempts=0", inserts)
            if updates:
                db.executemany("UPDATE riot_jobs SET next_attempt=?,requested_at=? WHERE member_id=?", updates)
        if reset_auth:
            # A shared remote budget must not be contacted while a demo's
            # local writer is held. Unauthorized/failed enqueues never reset it.
            self.limiter.reset_auth()
        return queued

    def get_profiles(self, member_ids):
        from .riot_profile import public_profile
        ids = self._ids(member_ids)
        results = {}
        if not ids:
            return results
        placeholders = ",".join("?" for _ in ids)
        with self.core.read_snapshot() as db:
            rows = db.execute(f"SELECT m.id,m.canonical_id,{RANK_SELECT},p.payload,p.fetched_at,j.status,j.last_error,j.next_attempt FROM members m LEFT JOIN riot_profiles p ON p.member_id=m.id AND p.canonical_id=m.canonical_id LEFT JOIN riot_jobs j ON j.member_id=m.id AND j.canonical_id=m.canonical_id {RANK_JOINS} WHERE m.id IN ({placeholders}) AND m.status='APPROVED'", ids)
            for row in rows:
                payload = public_profile(profile_projection(row["payload"], row)) or {"current_tier": "", "lp": None,
                    "champions": [], "profile_icon_url": "", "updated_at": ""}
                error_code = row["last_error"] if row["last_error"] in _ERRORS else ""
                payload.update(status=row["status"] or "EMPTY", last_error=error_code, error=_ERRORS.get(error_code, ""),
                               fetched_at=row["fetched_at"], next_attempt=row["next_attempt"])
                results[row["id"]] = payload
        return results

    def _claim(self):
        if not self.config.enabled:
            return None
        cooldown = self.limiter.status()
        if cooldown["blocked"] or cooldown["retry_after"] > 0:
            return None
        with _metadata_transaction(self.core, "jobs") as db:
            stamp = _time(self.core, db, self.clock)
            # Stale identity/status work can never be published or keep the
            # background queue alive indefinitely.
            db.execute("UPDATE riot_jobs SET status='FAILED',lease_id=NULL,lease_until=0,last_error='profile_changed' WHERE status IN ('QUEUED','RUNNING') AND NOT EXISTS(SELECT 1 FROM members m WHERE m.id=riot_jobs.member_id AND m.canonical_id=riot_jobs.canonical_id AND m.status='APPROVED')")
            row = db.execute("SELECT * FROM riot_jobs WHERE (status='QUEUED' OR (status='RUNNING' AND lease_until<=?)) AND next_attempt<=? ORDER BY next_attempt,requested_at,member_id LIMIT 1", (stamp, stamp)).fetchone()
            if not row:
                return None
            job = dict(row)
            job["lease_id"] = str(uuid4())
            db.execute("UPDATE riot_jobs SET status='RUNNING',lease_id=?,lease_until=? WHERE member_id=?", (job["lease_id"], stamp + LEASE_SECONDS, job["member_id"]))
            return job

    def _progress(self, job, partial):
        with _metadata_transaction(self.core, "jobs") as db:
            stamp = _time(self.core, db, self.clock)
            if job["stage"] == 2:
                partial["rank_fetched_at"] = stamp
            db.execute("UPDATE riot_jobs SET partial_payload=?,stage=?,status='QUEUED',lease_id=NULL,lease_until=0,next_attempt=?,attempts=0,last_error='' WHERE member_id=? AND canonical_id=? AND lease_id=? AND lease_until>?", (_json(partial), job["stage"] + 1, stamp, job["member_id"], job["canonical_id"], job["lease_id"], stamp))

    def _failure(self, job, error):
        code = error.code if error.code in _ERRORS else "unavailable"
        if code in ("rate_limited", "auth"):
            self.limiter.backoff(error)
        with _metadata_transaction(self.core, "jobs") as db:
            stamp = _time(self.core, db, self.clock)
            attempts = job["attempts"] + (code not in ("auth", "rate_limited"))
            terminal = code in ("not_found", "invalid_request", "disabled") or attempts >= 5
            delay = min(300, 5 * 2 ** min(attempts, 6))
            if code in ("auth", "rate_limited"):
                delay = 0
            db.execute("UPDATE riot_jobs SET status=?,lease_id=NULL,lease_until=0,next_attempt=?,attempts=?,last_error=? WHERE member_id=? AND canonical_id=? AND lease_id=?", ("FAILED" if terminal else "QUEUED", stamp + (TTL_SECONDS if terminal else delay), attempts, code, job["member_id"], job["canonical_id"], job["lease_id"]))

    def _finish(self, job, partial):
        rank = partial["rank"]
        tier, lp = _rank_tier(rank)
        flex_payload = {}
        # Missing flex in old partial jobs means it was never queried. It must
        # remain distinct from a successful response with no flex entry.
        flex = rank.get("flex")
        if flex is not None:
            flex_tier, flex_lp = _rank_tier(flex)
            flex_payload = {"flex_current_tier": flex_tier, "flex_lp": flex_lp,
                            "flex_rank_wins": flex.get("wins"), "flex_rank_losses": flex.get("losses")}
        version, champions = _static_data()
        mastery = []
        for item in partial["masteries"][:MASTERY_LIMIT]:
            metadata = champions.get(str(item["champion_id"]), {})
            mastery.append({"id": item["champion_id"], "name": metadata.get("name", str(item["champion_id"])),
                            "icon_url": metadata.get("image", ""), "points": item["points"], "level": item["level"]})
        icon = profile_icon_url(version, partial["summoner"]["profile_icon_id"]) if version else ""
        with self.core.transaction() as db:
            _job_lock(self.core, db)
            stamp = _time(self.core, db, self.clock)
            current = db.execute("SELECT * FROM riot_jobs WHERE member_id=?", (job["member_id"],)).fetchone()
            member_row = db.execute("SELECT m.*," + RANK_SELECT + " FROM members m " + RANK_JOINS + " WHERE m.id=?", (job["member_id"],)).fetchone()
            member = project_member(member_row) if member_row else None
            if not current or current["lease_id"] != job["lease_id"] or current["lease_until"] <= stamp:
                return
            if (not member or member["status"] != "APPROVED" or member["canonical_id"] != job["canonical_id"]
                    or member["current_tier_updated_at"] != partial["tier_version"]):
                db.execute("UPDATE riot_jobs SET status='FAILED',last_error='profile_changed',lease_id=NULL,lease_until=0,next_attempt=? WHERE member_id=? AND lease_id=?", (stamp + FORCE_SECONDS, job["member_id"], job["lease_id"]))
                return
            updated = max(datetime.fromtimestamp(stamp, timezone.utc), datetime.fromisoformat(member["updated_at"]) + timedelta(microseconds=1)).isoformat(timespec="microseconds")
            payload = {"puuid": partial["puuid"], "current_tier": tier, "lp": lp,
                       "champions": mastery, "profile_icon_url": icon, "updated_at": updated,
                       "summoner_level": partial["summoner"].get("summoner_level"),
                       "rank_wins": rank.get("wins"), "rank_losses": rank.get("losses")}
            payload.update(flex_payload)
            db.execute("UPDATE members SET updated_at=? WHERE id=? AND canonical_id=?", (updated, job["member_id"], job["canonical_id"]))
            save_riot_profile(db, job["member_id"], job["canonical_id"], payload, stamp,
                              rank_fetched_at=partial.get("rank_fetched_at"))
            db.execute("UPDATE riot_jobs SET status='DONE',stage=4,partial_payload='{}',last_error='',lease_id=NULL,lease_until=0,attempts=0 WHERE member_id=? AND lease_id=?", (job["member_id"], job["lease_id"]))

    def process_one(self):
        job = self._claim()
        if not job:
            return False
        try:
            partial = json.loads(job["partial_payload"])
            if job["stage"] == 0:
                name, tag = partial["riot_id"].split("#")
                partial["puuid"] = self.client.account_by_riot_id(name, tag)["puuid"]
            elif job["stage"] == 1:
                partial["summoner"] = self.client.summoner_by_puuid(partial["puuid"])
            elif job["stage"] == 2:
                partial["rank"] = self.client.solo_rank_by_puuid(partial["puuid"])
            else:
                partial["masteries"] = self.client.top_masteries(partial["puuid"])
                self._finish(job, partial)
                return True
            self._progress(job, partial)
        except RiotAPIError as error:
            self._failure(job, error)
        except (ValueError, KeyError, TypeError):
            self._failure(job, RiotAPIError("invalid_response"))
        return True

    def _pending_work(self):
        if self.limiter.status()["blocked"]:
            return False
        with self.core.read_snapshot() as db:
            return bool(db.execute("SELECT 1 FROM riot_jobs WHERE status IN ('QUEUED','RUNNING') LIMIT 1").fetchone())

    def ensure_worker(self):
        return start_worker(self.core, self.config, sync=self)


_workers = {}
_worker_lock = threading.Lock()


def stop_demo_workers():
    """Disable already-running disposable workers when the demo gate closes."""
    with _worker_lock:
        stopped = 0
        for path, state in _workers.items():
            if not str(path).startswith("supabase://") and not state["stop"].is_set():
                state["stop"].set()
                stopped += 1
        return stopped


def stop_workers_except_key(config):
    """Retire this process's workers after the one configured key changes."""
    current = hashlib.sha256(config.api_key.encode()).hexdigest() if config.enabled else None
    stopped = 0
    with _worker_lock:
        for state in _workers.values():
            if state["key_hash"] != current and not state["stop"].is_set():
                state["stop"].set()
                stopped += 1
    return stopped


def start_worker(core, config=None, *, sync=None):
    service = sync or RiotSync(core, config)
    if not service.config.enabled:
        with _worker_lock:
            previous = _workers.get(core.db_path)
            if previous:
                previous["stop"].set()
        return None
    with _worker_lock:
        previous = _workers.get(core.db_path)
        rate_path = service.rate_core.db_path
        if (previous and previous["key_hash"] == service.key_hash
                and previous.get("rate_path", core.db_path) == rate_path
                and previous["thread"].is_alive() and not previous["stop"].is_set()):
            previous["generation"] += 1
            return previous["thread"]
        if previous:
            previous["stop"].set()
        stop = threading.Event()
        state = {"key_hash": service.key_hash, "rate_path": rate_path, "stop": stop, "generation": 0}

        def work():
            idle_since = time.monotonic()
            try:
                while not stop.is_set():
                    try:
                        with _worker_lock:
                            generation = state["generation"]
                        worked = service.process_one()
                        if worked or service._pending_work():
                            idle_since = time.monotonic()
                        elif time.monotonic() - idle_since >= 30:
                            with _worker_lock:
                                if state["generation"] == generation:
                                    if _workers.get(core.db_path) is state:
                                        _workers.pop(core.db_path, None)
                                    return
                            idle_since = time.monotonic()
                    except Exception:
                        # Worker errors never include credentials, HTTP URLs,
                        # private Riot responses or driver details in logs.
                        worked = False
                    stop.wait(0.05 if worked else 0.5)
            finally:
                with _worker_lock:
                    if _workers.get(core.db_path) is state:
                        _workers.pop(core.db_path, None)

        thread = threading.Thread(target=work, name="roly-riot-sync", daemon=True)
        state["thread"] = thread
        _workers[core.db_path] = state
        thread.start()
        return thread
