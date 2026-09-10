"""Public, bounded Riot cache projection. No HTTP or credentials in render paths."""
from collections.abc import Mapping
from contextlib import closing
import json
import re

from .core import identity
from .member_profile import validate_current_tier
from .riot_api import MASTERY_LIMIT
from .member_ranks import RANK_SELECT, RANK_JOINS, profile_projection, strip_rank_columns


def image_url(value):
    if isinstance(value, str) and re.fullmatch(
        r"https://ddragon\.leagueoflegends\.com/cdn/\d+\.\d+\.\d+/img/(?:champion/[A-Za-z0-9]+|profileicon/\d+)\.png", value
    ):
        return value
    return ""


def public_profile(payload):
    """Drop internal PUUID, malformed caches and untrusted image destinations."""
    try:
        if isinstance(payload, str):
            if len(payload) > 32768:
                return None
            payload = json.loads(payload)
        if not isinstance(payload, Mapping):
            return None
        tier, lp = validate_current_tier(payload.get("current_tier"), payload.get("lp"))
        if not tier:
            return None
        champions, seen = [], set()
        for item in (payload.get("champions") or [])[:MASTERY_LIMIT]:
            if not isinstance(item, Mapping):
                continue
            champ_id, points, level = item.get("id"), item.get("points"), item.get("level")
            if any(type(value) is not int or value < 0 for value in (champ_id, points, level)) or champ_id in seen:
                continue
            seen.add(champ_id)
            champions.append({"id": champ_id, "name": str(item.get("name") or f"챔피언 {champ_id}")[:80],
                              "icon_url": image_url(item.get("icon_url")), "points": points, "level": level})
        profile = {"current_tier": tier, "lp": lp, "champions": champions,
                "profile_icon_url": image_url(payload.get("profile_icon_url")),
                "updated_at": str(payload.get("updated_at") or "")[:40]}
        for field in ("summoner_level", "rank_wins", "rank_losses"):
            value = payload.get(field)
            if type(value) is int and 0 <= value <= 10_000_000:
                profile[field] = value
        if payload.get("flex_current_tier") is not None:
            try:
                flex_tier, flex_lp = validate_current_tier(payload["flex_current_tier"], payload.get("flex_lp"))
            except (ValueError, TypeError):
                flex_tier = ""
            if flex_tier:
                profile.update(flex_current_tier=flex_tier, flex_lp=flex_lp)
                for field in ("flex_rank_wins", "flex_rank_losses"):
                    value = payload.get(field)
                    if type(value) is int and 0 <= value <= 10_000_000:
                        profile[field] = value
        return profile
    except (ValueError, TypeError, KeyError):
        return None


def attach_profile(row):
    """Match both live member identity and the historical roster's Riot ID."""
    payload = row.pop("riot_cache_payload", None)
    canonical = row.pop("riot_cache_canonical", None)
    row["riot_profile"] = None
    try:
        if canonical and identity(row["riot_id"])[1] == canonical:
            row["riot_profile"] = public_profile(profile_projection(payload, row))
    except (ValueError, KeyError):
        pass
    strip_rank_columns(row)
    return row


def member_profiles(core, member_ids):
    ids = list(dict.fromkeys(member_ids))
    if not ids:
        return {}
    results = {}
    statements = []
    for offset in range(0, len(ids), 100):
        batch = ids[offset:offset + 100]
        marks = ",".join("?" for _ in batch)
        statements.append((f"SELECT m.id,rp.payload,{RANK_SELECT} FROM members m JOIN riot_profiles rp ON rp.member_id=m.id AND rp.canonical_id=m.canonical_id {RANK_JOINS} WHERE m.id IN ({marks}) AND m.status='APPROVED'", batch))
    rows = [row for batch in core.read_batches(statements) for row in batch]
    for row in rows:
        profile = public_profile(profile_projection(row["payload"], row))
        if profile:
            results[row["id"]] = profile
    return results
