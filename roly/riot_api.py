"""Small, server-only Riot client. Scheduling and persistent caching live elsewhere.

Each authenticated request must reserve capacity through ``before_request``.
That callback must finish its transaction before returning: no DB lock should
span network I/O. This module never retries, sleeps, logs URLs, or exposes an
upstream error body. The only configured routing is Korea (KR / ASIA).
"""
from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import math
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from roly.storage_config import ConfigError, _runtime_document


API_HOSTS = {"asia": "asia.api.riotgames.com", "kr": "kr.api.riotgames.com"}
CDN_HOST = "ddragon.leagueoflegends.com"
SOLO_QUEUE = "RANKED_SOLO_5x5"
FLEX_QUEUE = "RANKED_FLEX_SR"
MASTERY_LIMIT = 5
TIERS = frozenset(("IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD",
                   "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER"))
MESSAGES = {
    "disabled": "Riot API 키가 아직 설정되지 않았습니다.",
    "invalid_request": "Riot 조회에 필요한 계정 정보를 확인해 주세요.",
    "auth": "Riot API 키가 만료되었거나 접근이 허용되지 않았습니다.",
    "not_found": "Riot에서 해당 계정 또는 한국 서버 정보를 찾지 못했습니다.",
    "rate_limited": "Riot 요청 한도에 도달했습니다. 잠시 후 다시 갱신합니다.",
    "unavailable": "Riot 서버에 일시적으로 연결할 수 없습니다.",
    "invalid_response": "Riot 응답 형식을 확인할 수 없습니다. 기존 정보를 유지합니다.",
}


class RiotAPIError(RuntimeError):
    """Only a known code and numeric metadata leave the HTTP boundary."""

    def __init__(self, code, *, retry_after=None, status=None, scope=None):
        self.code = code if isinstance(code, str) and code in MESSAGES else "unavailable"
        self.retry_after = retry_after if (type(retry_after) in (float, int)
                                           and math.isfinite(retry_after)
                                           and retry_after >= 0) else None
        self.status = status if type(status) is int and 100 <= status <= 599 else None
        self.scope = scope if scope in API_HOSTS else None
        super().__init__(MESSAGES[self.code])


@dataclass(frozen=True)
class RiotConfig:
    api_key: str = field(default="", repr=False)
    allow_demo: bool = False

    def __post_init__(self):
        if type(self.allow_demo) is not bool:
            raise ConfigError("[riot] allow_demo에는 true 또는 false를 입력해 주세요.")
        if not isinstance(self.api_key, str) or (self.api_key and not re.fullmatch(
                r"[A-Za-z0-9_-]{8,256}", self.api_key)):
            raise ConfigError("[riot] api_key 형식을 확인해 주세요.")

    @property
    def enabled(self):
        return bool(self.api_key)


def load_riot_config(document=None):
    """An absent/blank optional key disables Riot only, never DB connection."""
    document = _runtime_document() if document is None else document
    if not isinstance(document, Mapping):
        raise ConfigError("Riot 설정은 [riot] 항목으로 입력해 주세요.")
    values = document.get("riot", {})
    if not isinstance(values, Mapping):
        raise ConfigError("Riot 설정은 [riot] 항목으로 입력해 주세요.")
    key = values.get("api_key", "")
    if not isinstance(key, str):
        raise ConfigError("[riot] api_key에는 문자열을 입력해 주세요.")
    return RiotConfig(key.strip(), allow_demo=values.get("allow_demo", False))


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: Mapping = field(default_factory=dict, repr=False)
    body: bytes = field(default=b"", repr=False)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _allowed_url(url):
    try:
        parts = urlsplit(url)
        return (parts.scheme == "https" and parts.hostname in {*API_HOSTS.values(), CDN_HOST}
                and parts.port in (None, 443) and not parts.username and not parts.password
                and not parts.fragment)
    except (ValueError, TypeError):
        return False


def _http_get(url, headers, timeout, max_bytes):
    """Bounded read; redirects cannot forward a key to another destination."""
    if not _allowed_url(url):
        raise RiotAPIError("invalid_request")
    request = Request(url, headers=headers, method="GET")
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    deadline = time.monotonic() + timeout
    try:
        with opener.open(request, timeout=timeout) as response:
            chunks, length = [], 0
            while True:
                if time.monotonic() >= deadline:
                    raise RiotAPIError("unavailable")
                chunk = response.read1(min(65536, max_bytes + 1 - length))
                if not chunk:
                    break
                chunks.append(chunk)
                length += len(chunk)
                if length > max_bytes:
                    raise RiotAPIError("invalid_response")
            return HTTPResponse(response.status, dict(response.headers), b"".join(chunks))
    except HTTPError as error:
        # Error body may echo a key or identity. Do not read it.
        status, response_headers = error.code, dict(error.headers or {})
        error.close()
        return HTTPResponse(status, response_headers)
    except RiotAPIError:
        raise
    except (URLError, OSError, ValueError):
        raise RiotAPIError("unavailable") from None


def _retry_after(headers):
    if not isinstance(headers, Mapping):
        return 120.0
    raw = next((v for k, v in headers.items() if isinstance(k, str)
                and k.lower() == "retry-after"), None)
    if isinstance(raw, str) and re.fullmatch(r"\d{1,9}(?:\.\d{1,3})?", raw.strip()):
        return max(1.0, float(raw))
    return 120.0


def _json_response(response, max_bytes, scope=None):
    if not isinstance(response, HTTPResponse) or type(response.status) is not int:
        raise RiotAPIError("invalid_response", scope=scope)
    status = response.status
    if status != 200:
        code = ("auth" if status in (401, 403) else "not_found" if status == 404
                else "rate_limited" if status == 429 else "invalid_request"
                if status in (400, 405, 415) else "unavailable")
        raise RiotAPIError(code, status=status, scope=scope,
                           retry_after=_retry_after(response.headers) if status == 429 else None)
    if not isinstance(response.body, bytes) or len(response.body) > max_bytes:
        raise RiotAPIError("invalid_response", scope=scope)
    try:
        def invalid_constant(_value):
            raise ValueError
        return json.loads(response.body.decode("utf-8"), parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise RiotAPIError("invalid_response", scope=scope) from None


def _text(value, maximum=128, *, required=True):
    if value is None and not required:
        return ""
    if (not isinstance(value, str) or len(value) > maximum or (required and not value.strip())
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise RiotAPIError("invalid_response")
    return value.strip()


def _integer(value, *, maximum=2**63 - 1, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        raise RiotAPIError("invalid_response")
    return value


def _segment(value, maximum=128):
    try:
        value = _text(value, maximum)
    except RiotAPIError:
        raise RiotAPIError("invalid_request") from None
    return quote(value, safe="")


class RiotClient:
    def __init__(self, config, *, before_request, transport=None, timeout=5.0):
        if not isinstance(config, RiotConfig) or not callable(before_request):
            raise ConfigError("Riot 클라이언트 설정과 요청 제한 처리를 확인해 주세요.")
        if type(timeout) not in (float, int) or not math.isfinite(timeout) or not 1 <= timeout <= 10:
            raise ConfigError("Riot 요청 제한 시간은 1~10초 범위여야 합니다.")
        self._config = config
        self._before_request = before_request
        self._transport = transport or _http_get
        self._timeout = float(timeout)

    def _get(self, scope, path):
        if not self._config.enabled:
            raise RiotAPIError("disabled", scope=scope)
        self._before_request(scope)
        headers = {"X-Riot-Token": self._config.api_key, "Accept": "application/json",
                   "User-Agent": "Rolymoly/1.0"}
        try:
            response = self._transport("https://" + API_HOSTS[scope] + path, headers,
                                       self._timeout, 1024 * 1024)
        except RiotAPIError as error:
            raise RiotAPIError(error.code, retry_after=error.retry_after,
                               status=error.status, scope=scope) from None
        except Exception:
            raise RiotAPIError("unavailable", scope=scope) from None
        return _json_response(response, 1024 * 1024, scope)

    def account_by_riot_id(self, game_name, tag_line):
        data = self._get("asia", "/riot/account/v1/accounts/by-riot-id/"
                         + _segment(game_name, 100) + "/" + _segment(tag_line, 32))
        if not isinstance(data, dict):
            raise RiotAPIError("invalid_response")
        puuid = _text(data.get("puuid"))
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", puuid):
            raise RiotAPIError("invalid_response")
        return {"puuid": puuid, "game_name": _text(data.get("gameName"), 100, required=False),
                "tag_line": _text(data.get("tagLine"), 32, required=False)}

    def summoner_by_puuid(self, puuid):
        data = self._get("kr", "/lol/summoner/v4/summoners/by-puuid/" + _segment(puuid))
        if not isinstance(data, dict):
            raise RiotAPIError("invalid_response")
        if "puuid" in data and data["puuid"] != puuid:
            raise RiotAPIError("invalid_response")
        return {"profile_icon_id": _integer(data.get("profileIconId", 0), maximum=1000000),
                "summoner_level": _integer(data.get("summonerLevel", 0), maximum=1000000)}

    def solo_rank_by_puuid(self, puuid):
        """Keep the solo result contract and attach flex from the same request."""
        data = self._get("kr", "/lol/league/v4/entries/by-puuid/" + _segment(puuid))
        if not isinstance(data, list) or len(data) > 20 or any(not isinstance(v, dict) for v in data):
            raise RiotAPIError("invalid_response")
        for entry in data:
            _text(entry.get("queueType"), 64)
        def rank_for(queue):
            entries = [value for value in data if value["queueType"] == queue]
            if not entries:
                return {"tier": "UNRANKED", "division": "", "lp": 0, "wins": 0,
                        "losses": 0, "queue": queue}
            if len(entries) != 1:
                raise RiotAPIError("invalid_response")
            entry = entries[0]
            tier, division = entry.get("tier"), entry.get("rank")
            if not isinstance(tier, str) or tier not in TIERS or division not in ("I", "II", "III", "IV"):
                raise RiotAPIError("invalid_response")
            return {"tier": tier, "division": division,
                    "lp": _integer(entry.get("leaguePoints", 0), maximum=1000000),
                    "wins": _integer(entry.get("wins", 0), maximum=1000000),
                    "losses": _integer(entry.get("losses", 0), maximum=1000000), "queue": queue}
        solo = rank_for(SOLO_QUEUE)
        solo["flex"] = rank_for(FLEX_QUEUE)
        return solo

    def top_masteries(self, puuid):
        data = self._get("kr", "/lol/champion-mastery/v4/champion-masteries/by-puuid/"
                         + _segment(puuid) + f"/top?count={MASTERY_LIMIT}")
        if not isinstance(data, list) or len(data) > MASTERY_LIMIT:
            raise RiotAPIError("invalid_response")
        entries, seen = [], set()
        for entry in data:
            if not isinstance(entry, dict):
                raise RiotAPIError("invalid_response")
            champion_id = _integer(entry.get("championId"), minimum=1, maximum=1000000)
            if champion_id in seen:
                raise RiotAPIError("invalid_response")
            seen.add(champion_id)
            entries.append({"champion_id": champion_id,
                            "points": _integer(entry.get("championPoints", 0)),
                            "level": _integer(entry.get("championLevel", 0), maximum=1000000)})
        return sorted(entries, key=lambda row: (-row["points"], row["champion_id"]))


def _version(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}", value):
        raise RiotAPIError("invalid_response")
    return value


def profile_icon_url(version, icon_id):
    return (f"https://{CDN_HOST}/cdn/{_version(version)}/img/profileicon/"
            f"{_integer(icon_id, maximum=1000000)}.png")


class DataDragonClient:
    """Key-free static metadata; caller caches by version across all members."""

    def __init__(self, *, transport=None, timeout=5.0):
        if type(timeout) not in (float, int) or not math.isfinite(timeout) or not 1 <= timeout <= 10:
            raise ConfigError("Data Dragon 요청 제한 시간은 1~10초 범위여야 합니다.")
        self._transport = transport or _http_get
        self._timeout = float(timeout)

    def _get(self, path):
        try:
            response = self._transport(f"https://{CDN_HOST}" + path,
                                       {"Accept": "application/json", "User-Agent": "Rolymoly/1.0"},
                                       self._timeout, 4 * 1024 * 1024)
        except RiotAPIError:
            raise
        except Exception:
            raise RiotAPIError("unavailable") from None
        return _json_response(response, 4 * 1024 * 1024)

    def latest_version(self):
        data = self._get("/api/versions.json")
        if not isinstance(data, list) or not data or len(data) > 5000:
            raise RiotAPIError("invalid_response")
        return _version(data[0])

    def champions(self, version):
        version = _version(version)
        data = self._get(f"/cdn/{version}/data/ko_KR/champion.json")
        if not isinstance(data, dict) or not isinstance(data.get("data"), dict) or not data["data"]:
            raise RiotAPIError("invalid_response")
        if len(data["data"]) > 2000:
            raise RiotAPIError("invalid_response")
        champions = {}
        for value in data["data"].values():
            if not isinstance(value, dict):
                raise RiotAPIError("invalid_response")
            key = value.get("key")
            image = value.get("image", {}).get("full") if isinstance(value.get("image"), dict) else None
            if (not isinstance(key, str) or not re.fullmatch(r"[1-9]\d{0,6}", key)
                    or not isinstance(image, str) or not re.fullmatch(r"[A-Za-z0-9_]+\.png", image)
                    or key in champions):
                raise RiotAPIError("invalid_response")
            champions[key] = {"name": _text(value.get("name"), 100),
                              "image": f"https://{CDN_HOST}/cdn/{version}/img/champion/{image}"}
        return champions
