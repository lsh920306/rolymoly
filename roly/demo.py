"""Session-isolated rehearsal using the supplied roster and synthetic results."""
import json
from pathlib import Path
import secrets
from datetime import datetime, timedelta, timezone

from roly.competition import Competition, ROLES
from roly.core import identity, now
from roly.member_profile import CURRENT_TIERS
from roly.riot_profile import public_profile
from roly.tournament import TournamentService


DEMO_DATA_VERSION = 5
DEMO_RIOT_SNAPSHOT = Path(__file__).resolve().parents[1] / "static" / "demo-riot-profiles.json"
DEMO_RIOT_IDS = (
    "겨울#kr99",
    "Kging#kr1",
    "슬모띵#kr1",
    "마아먕고로룡#123",
    "야동초등학교#kr1",
    "메이쥐#kr0",
    "경 먀#kr1",
    "남자는티오피#kr1",
    "콩이바람이아빠#KR1",
    "화려한솔로#외로운청년",
    "평화파밍사랑#kr4",
    "야수#테토",
    "부리부리대만왕#kr2",
    "Siat#kr1",
    "수 빈#kr111",
    "치원#kr1",
    "들기름무빙#kr01",
    "홍시먹다체함#kr0",
    "라라루루#kr0",
    "정 현#kr2",
    "오도봉구#kr1",
    "건동김#KR1",
)


def _load_riot_snapshot():
    """The packaged snapshot needs no API key and never initiates a request."""
    try:
        with DEMO_RIOT_SNAPSHOT.open("rb") as source:
            raw = source.read(524289)
        if len(raw) > 524288:
            return {}
        document = json.loads(raw)
        return document if isinstance(document, dict) and isinstance(document.get("profiles"), dict) else {}
    except (OSError, UnicodeError, ValueError, RecursionError):
        return {}


def _demo_clan_tier(current_tier):
    ladder = CURRENT_TIERS[2:]
    if current_tier in ("", "언랭크"):
        return secrets.choice(ladder)
    index = ladder.index(current_tier) + secrets.choice((1, -2))
    return ladder[max(0, min(index, len(ladder) - 1))]


def _seed_riot_profiles(core, member_ids):
    from .member_ranks import save_riot_profile
    document = _load_riot_snapshot()
    with core.transaction() as db:
        for member_id in member_ids:
            member = core.get_member(member_id, db)
            canonical = identity(member["riot_id"])[1]
            profile = public_profile(document.get("profiles", {}).get(canonical))
            if profile:
                try:
                    captured = datetime.fromisoformat(profile["updated_at"] or document.get("generated_at", ""))
                    if captured.tzinfo is None:
                        raise ValueError("Snapshot time needs a timezone")
                    captured = captured.astimezone(timezone.utc)
                    fetched_at = captured.timestamp()
                except (TypeError, ValueError, OverflowError):
                    profile = None
            clan_tier = _demo_clan_tier(profile["current_tier"] if profile else "")
            if not profile:
                db.execute("UPDATE members SET clan_tier=?,updated_at=? WHERE id=? AND canonical_id=?",
                           (clan_tier, now(), member_id, canonical))
                continue
            profile["updated_at"] = captured.isoformat(timespec="microseconds")
            db.execute("UPDATE members SET clan_tier=?,updated_at=? WHERE id=? AND canonical_id=?",
                       (clan_tier, now(), member_id, canonical))
            save_riot_profile(db, member_id, canonical, profile, fetched_at)


def seed(core, *, credentials=None):
    if core.is_postgres:
        raise ValueError("체험 데이터는 별도의 로컬 체험 저장소에만 만들 수 있습니다.")
    password = secrets.token_urlsafe(24)
    core.setup_admin("demo", password, "체험 운영진")
    token = core.login("demo", password)
    ids = []
    for index, riot_id in enumerate(DEMO_RIOT_IDS):
        role_index = index % len(ROLES)
        member = core.join_member(riot_id, ROLES[role_index], ROLES[(role_index + 1) % len(ROLES)])
        core.approve_member(token, member, [220, 300, 390, 150, 480][index // 5] + role_index * 5)
        ids.append(member)
    # Apply captured API tiers before any player snapshot is created. Clan
    # tiers are sample values drawn once; scores and positions stay unchanged.
    _seed_riot_profiles(core, ids)
    competition = Competition(core)
    # The last two members replace the first TOP/JG pair in the third sample,
    # keeping ten unique players and exactly two players in each position.
    normal_rosters = (ids[:10], ids[10:20], ids[20:22] + ids[2:10])
    for number, roster in enumerate(normal_rosters):
        assignment = [{"member_id": i, "role": ROLES[k % 5]} for k, i in enumerate(roster)]
        event_id = competition.create_normal(token, assignment, title=f"예시 내전 {number + 1}")
        event = competition.get_event(event_id)
        game = event["games"][0]
        competition.record_result(token, event_id, game["id"], game["team_a"] if number % 2 == 0 else game["team_b"])
        competition.finalize_event(token, event_id)
    competition.create_normal(token, [{"member_id": i, "role": ROLES[k % 5]} for k, i in enumerate(ids[:10])], title="오늘의 10인 내전")
    preparation = TournamentService(core, competition)
    event_id = preparation.create(token, "주말 경매 · 4팀", (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "체험 계정을 바꾸어 4팀의 입찰과 우승 업적을 확인할 수 있습니다.", "AUCTION", 4, "TOURNAMENT")
    preparation.open_recruitment(token, event_id)
    preparation.set_participants(token, event_id, [{"member_id": member_id, "role": ROLES[index % 5]} for index, member_id in enumerate(ids[:20])])
    preparation.confirm_participants(token, event_id)
    preparation.set_captains(token, event_id, ids[:20:5])
    preparation.prepare_auction(token, event_id)
    logins = [{"username": "demo", "password": password, "label": "체험 운영진 · 관리자", "member_id": None}]
    for number, captain_id in enumerate(ids[:20:5], 1):
        captain_password = secrets.token_urlsafe(24)
        username = f"demo_captain_{number}"
        name = core.get_member(captain_id)["riot_id"].split("#")[0]
        core.create_account(token, username, captain_password, f"{number}팀 팀장 · {name}", role="member", member_id=captain_id)
        logins.append({"username": username, "password": captain_password, "label": f"{number}팀 팀장 · {name}", "member_id": captain_id})
    participant_password = secrets.token_urlsafe(24)
    participant_name = core.get_member(ids[1])["riot_id"].split("#")[0]
    core.create_account(token, "demo_participant", participant_password, f"일반 참가자 · {participant_name}", role="member", member_id=ids[1])
    logins.append({"username": "demo_participant", "password": participant_password, "label": f"일반 참가자 · {participant_name}", "member_id": ids[1]})
    if credentials is not None:
        credentials.extend(logins)
    return token
