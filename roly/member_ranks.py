"""Identity-bound latest ranks; display compatibility is a read projection.

All mutations run inside the caller's transaction. No HTTP or per-member reads
are performed by the projections used on the live auction path.
"""
from datetime import datetime, timezone
import json
import math

from .member_profile import validate_current_tier


TIERS = {"IRON": "아이언", "BRONZE": "브론즈", "SILVER": "실버", "GOLD": "골드",
         "PLATINUM": "플래티넘", "EMERALD": "에메랄드", "DIAMOND": "다이아몬드",
         "MASTER": "마스터", "GRANDMASTER": "그랜드마스터", "CHALLENGER": "챌린저",
         "UNRANKED": "언랭크"}
TOP_TIERS = ("MASTER", "GRANDMASTER", "CHALLENGER", "UNRANKED")
_COLUMNS = ("tier", "division", "league_points", "wins", "losses", "source", "fetched_at", "updated_at")
RANK_SELECT = ",".join(f"{alias}.{column} AS _rank_{queue}_{column}"
                       for queue, alias in (("solo", "rs"), ("flex", "rf")) for column in _COLUMNS)
RANK_JOINS = """ LEFT JOIN member_ranks rs ON rs.member_id=m.id AND rs.queue_type='SOLO' AND rs.canonical_id=m.canonical_id
    LEFT JOIN member_ranks rf ON rf.member_id=m.id AND rf.queue_type='FLEX' AND rf.canonical_id=m.canonical_id """
RANK_FIELDS = frozenset(("current_tier", "lp", "rank_wins", "rank_losses", "flex_current_tier",
                         "flex_lp", "flex_rank_wins", "flex_rank_losses"))
RANK_DDL = """CREATE TABLE IF NOT EXISTS member_ranks(
    member_id INTEGER NOT NULL REFERENCES members(id),queue_type TEXT NOT NULL CHECK(queue_type IN ('SOLO','FLEX')),
    canonical_id TEXT NOT NULL,puuid TEXT,
    tier TEXT NOT NULL CHECK(tier IN ('IRON','BRONZE','SILVER','GOLD','PLATINUM','EMERALD','DIAMOND','MASTER','GRANDMASTER','CHALLENGER','UNRANKED')),
    division INTEGER,league_points INTEGER CHECK(league_points IS NULL OR league_points BETWEEN 0 AND 10000),
    wins INTEGER CHECK(wins IS NULL OR wins BETWEEN 0 AND 10000000),
    losses INTEGER CHECK(losses IS NULL OR losses BETWEEN 0 AND 10000000),
    source TEXT NOT NULL CHECK(source IN ('manual','riot')),fetched_at DOUBLE PRECISION,updated_at TEXT NOT NULL,
    PRIMARY KEY(member_id,queue_type),
    CHECK((tier IN ('MASTER','GRANDMASTER','CHALLENGER','UNRANKED') AND division IS NULL)
       OR (tier NOT IN ('MASTER','GRANDMASTER','CHALLENGER','UNRANKED') AND division BETWEEN 1 AND 4 AND division IS NOT NULL)),
    CHECK(tier<>'UNRANKED' OR league_points IS NULL),
    CHECK((source='manual' AND fetched_at IS NULL) OR (source='riot' AND (fetched_at IS NULL OR fetched_at>=0))))"""


def _rank_values(tier, lp, wins=None, losses=None):
    tier, lp = validate_current_tier(tier, lp)
    if not tier:
        return None
    for value in (wins, losses):
        if value is not None and (type(value) is not int or not 0 <= value <= 10_000_000):
            raise ValueError("랭크 승패는 0 이상의 정수여야 합니다.")
    name, _, division = tier.partition(" ")
    code = next(key for key, label in TIERS.items() if label == name)
    return code, int(division) if division else None, lp, wins, losses


def _put(db, member_id, canonical_id, queue, values, source, updated_at, fetched_at=None, puuid=None):
    if not db.in_transaction:
        raise ValueError("랭크 변경은 진행 중인 트랜잭션 안에서만 가능합니다.")
    if queue not in ("SOLO", "FLEX"):
        raise ValueError("랭크 종류를 확인해주세요.")
    written = db.execute("""INSERT INTO member_ranks(member_id,queue_type,canonical_id,puuid,tier,division,league_points,wins,losses,source,fetched_at,updated_at)
        SELECT ?,?,?,?,?,?,?,?,?,?,?,? FROM members WHERE id=? AND canonical_id=?
        ON CONFLICT(member_id,queue_type) DO UPDATE SET
        canonical_id=excluded.canonical_id,puuid=excluded.puuid,tier=excluded.tier,division=excluded.division,
        league_points=excluded.league_points,wins=excluded.wins,losses=excluded.losses,source=excluded.source,
        fetched_at=excluded.fetched_at,updated_at=excluded.updated_at""",
        (member_id, queue, canonical_id, puuid, *values, source, fetched_at, updated_at, member_id, canonical_id))
    if written.rowcount != 1:
        raise ValueError("회원의 Riot ID가 변경되어 랭크를 저장하지 않았습니다.")


def save_manual_rank(db, member_id, canonical_id, tier, lp, updated_at):
    """Preserve the existing pre-API manual SOLO entry contract."""
    if not db.in_transaction:
        raise ValueError("랭크 변경은 진행 중인 트랜잭션 안에서만 가능합니다.")
    values = _rank_values(tier, lp)
    if values is None:
        db.execute("DELETE FROM member_ranks WHERE member_id=? AND queue_type='SOLO'", (member_id,))
    else:
        _put(db, member_id, canonical_id, "SOLO", values, "manual", updated_at)


def save_riot_profile(db, member_id, canonical_id, payload, fetched_at, *, rank_fetched_at=None):
    """Split a validated internal profile into latest ranks and non-rank cache.

    The old payload contract is accepted at this write boundary only. A legacy
    job without FLEX leaves that queue unqueried, never inventing UNRANKED.
    """
    if not db.in_transaction:
        raise ValueError("Riot 정보 저장은 진행 중인 트랜잭션 안에서만 가능합니다.")
    fetched_at = float(fetched_at)
    rank_fetched_at = fetched_at if rank_fetched_at is None else float(rank_fetched_at)
    if not all(math.isfinite(value) and value >= 0 for value in (fetched_at, rank_fetched_at)):
        raise ValueError("Riot 조회 시각을 확인해주세요.")
    updated_at = str(payload.get("updated_at") or datetime.fromtimestamp(fetched_at, timezone.utc).isoformat(timespec="microseconds"))
    # Validate both before writing either queue.
    ranks = []
    for queue, prefix in (("SOLO", ""), ("FLEX", "flex_")):
        if payload.get(prefix + "current_tier") is None:
            continue
        values = _rank_values(payload[prefix + "current_tier"], payload.get(prefix + "lp"),
                              payload.get(prefix + "rank_wins"), payload.get(prefix + "rank_losses"))
        if values:
            ranks.append((queue, values))
    if not any(queue == "SOLO" for queue, _ in ranks):
        raise ValueError("조회한 솔로랭크 정보가 필요합니다.")
    for queue, values in ranks:
        _put(db, member_id, canonical_id, queue, values, "riot", updated_at, rank_fetched_at, payload.get("puuid"))
    metadata = {key: value for key, value in payload.items() if key not in RANK_FIELDS}
    db.execute("""INSERT INTO riot_profiles(member_id,canonical_id,payload,fetched_at) VALUES(?,?,?,?)
        ON CONFLICT(member_id) DO UPDATE SET canonical_id=excluded.canonical_id,payload=excluded.payload,fetched_at=excluded.fetched_at""",
        (member_id, canonical_id, json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), fetched_at))


def rank_projection(row, *, riot_only=False):
    """Project the two unique joined rows without reading a DB or raw JSON."""
    result = {}
    for queue, prefix in (("solo", ""), ("flex", "flex_")):
        key = "_rank_" + queue + "_"
        code = row.get(key + "tier")
        if not code or (riot_only and row.get(key + "source") != "riot"):
            continue
        tier = TIERS[code]
        if row.get(key + "division") is not None:
            tier += " " + str(row[key + "division"])
        result[prefix + "current_tier"] = tier
        result[prefix + "lp"] = row.get(key + "league_points")
        for field in ("wins", "losses"):
            if row.get(key + field) is not None:
                result[prefix + "rank_" + field] = row[key + field]
    return result


def strip_rank_columns(row):
    for key in list(row):
        if key.startswith("_rank_"):
            row.pop(key)


def project_member(row):
    """Overwrite deprecated member columns with normalized SOLO compatibility."""
    row = dict(row)
    rank = rank_projection(row)
    row.update(current_tier=rank.get("current_tier", ""), current_tier_lp=rank.get("lp"),
               current_tier_source=row.get("_rank_solo_source") or "manual",
               current_tier_updated_at=row.get("_rank_solo_updated_at"))
    strip_rank_columns(row)
    return row


def profile_projection(payload, row):
    """Ranks come exclusively from joined rows, even if old JSON contains them."""
    try:
        if isinstance(payload, str):
            if len(payload) > 32768:
                return None
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            return None
        result = {key: value for key, value in payload.items() if key not in RANK_FIELDS}
        result.update(rank_projection(dict(row), riot_only=True))
        return result
    except (ValueError, TypeError):
        return None


def initialize_ranks(db, *, postgres=False):
    """Transactional v7 upgrade: import existing values once, without HTTP."""
    existing = db.execute("SELECT to_regclass('member_ranks')" if postgres else
                          "SELECT name FROM sqlite_master WHERE type='table' AND name='member_ranks'").fetchone()
    present = bool(existing and existing[0])
    db.execute(RANK_DDL.replace(" INTEGER", " BIGINT") if postgres else RANK_DDL)
    if present:
        return
    rows = db.execute("SELECT m.*,rp.canonical_id AS cache_identity,rp.payload,rp.fetched_at FROM members m LEFT JOIN riot_profiles rp ON rp.member_id=m.id").fetchall()
    for entry in rows:
        member = dict(entry)
        payload = None
        if member["cache_identity"] == member["canonical_id"] and member["payload"]:
            try:
                payload = json.loads(member["payload"])
                if not isinstance(payload, dict):
                    payload = None
                elif _rank_values(payload.get("current_tier"), payload.get("lp")):
                    save_riot_profile(db, member["id"], member["canonical_id"], payload, member["fetched_at"])
                else:
                    payload = None
            except (ValueError, TypeError, KeyError, StopIteration):
                payload = None
        if payload is None:
            if member["current_tier_source"] == "manual":
                save_manual_rank(db, member["id"], member["canonical_id"], member["current_tier"], member["current_tier_lp"],
                                 member["current_tier_updated_at"] or member["updated_at"])
            else:
                # A damaged/absent rich cache must not erase a valid existing
                # API SOLO value or turn it into an editable manual rank.
                values = _rank_values(member["current_tier"], member["current_tier_lp"])
                if values:
                    observed = None
                    try:
                        captured = datetime.fromisoformat(member["current_tier_updated_at"])
                        if captured.tzinfo is not None and captured.timestamp() >= 0:
                            observed = captured.timestamp()
                    except (ValueError, TypeError, OverflowError):
                        pass
                    _put(db, member["id"], member["canonical_id"], "SOLO", values, "riot",
                         member["current_tier_updated_at"] or member["updated_at"], observed)
