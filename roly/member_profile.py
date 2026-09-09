"""Stored member profile values; tier labels never imply a power-score change."""
import re

UNSET = object()
CURRENT_TIERS = ("", "언랭크") + tuple(
    f"{tier} {division}"
    for tier in ("아이언", "브론즈", "실버", "골드", "플래티넘", "에메랄드", "다이아몬드")
    for division in (4, 3, 2, 1)
) + ("마스터", "그랜드마스터", "챌린저")


def validate_clan_tier(value):
    value = "" if value is None else str(value).strip()
    if len(value) > 32:
        raise ValueError("클랜 티어는 32자 이내로 입력해주세요.")
    return value


def validate_current_tier(tier, lp=None):
    tier = "" if tier is None else str(tier).strip()
    if tier not in CURRENT_TIERS:
        raise ValueError("현재 티어는 제공된 티어 목록에서 선택해주세요.")
    if lp is not None:
        if isinstance(lp, bool) or not isinstance(lp, (int, str)) or not re.fullmatch(r"\d+", str(lp)) or not 0 <= int(lp) <= 10000:
            raise ValueError("현재 LP는 0~10,000의 정수로 입력해주세요.")
        lp = int(lp)
        if tier in ("", "언랭크"):
            raise ValueError("미입력·언랭크에는 LP를 지정할 수 없습니다.")
    return tier, lp


_TIER_VALUES = ",".join("'" + tier + "'" for tier in CURRENT_TIERS)
PROFILE_COLUMNS = {
    "members": {
        "application_notes": "TEXT NOT NULL DEFAULT ''",
        "clan_tier": "TEXT NOT NULL DEFAULT '' CHECK(length(clan_tier)<=32)",
        "current_tier": f"TEXT NOT NULL DEFAULT '' CHECK(current_tier IN ({_TIER_VALUES}))",
        "current_tier_lp": "INTEGER CHECK(current_tier_lp IS NULL OR (current_tier NOT IN ('','언랭크') AND current_tier_lp BETWEEN 0 AND 10000))",
        "current_tier_updated_at": "TEXT",
        "current_tier_source": "TEXT NOT NULL DEFAULT 'manual' CHECK(current_tier_source IN ('manual','riot'))",
    },
    "game_players": {
        "riot_id_snapshot": "TEXT", "clan_tier_snapshot": "TEXT",
        "current_tier_snapshot": "TEXT", "current_tier_lp_snapshot": "INTEGER",
    },
}
COMPETITION_PROFILE_COLUMNS = {
    "clan_tier_snapshot": "TEXT", "current_tier_snapshot": "TEXT", "current_tier_lp_snapshot": "INTEGER",
}


def initialize_sqlite(db):
    for table, additions in PROFILE_COLUMNS.items():
        existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        for column, declaration in additions.items():
            if column not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def initialize_postgres(db):
    for table, additions in {**PROFILE_COLUMNS, "competition_players": COMPETITION_PROFILE_COLUMNS}.items():
        for column, declaration in additions.items():
            db.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {declaration}")
