import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request

from roly.riot_api import (
    ConfigError, DataDragonClient, HTTPResponse, RiotAPIError, RiotClient,
    RiotConfig, SOLO_QUEUE, FLEX_QUEUE, _NoRedirect, _allowed_url, _http_get,
    load_riot_config, profile_icon_url,
)


FAKE_KEY = "RGAPI-test-not-a-real-key"
PUUID = "a-test-encrypted-puuid-1234567890"


def response(data, status=200, headers=None):
    return HTTPResponse(status, headers or {}, json.dumps(data).encode())


class RiotAPITests(unittest.TestCase):
    def client(self, *responses):
        transport = Mock(side_effect=responses)
        limiter = Mock()
        return RiotClient(RiotConfig(FAKE_KEY), before_request=limiter,
                          transport=transport), transport, limiter

    def test_optional_configuration_is_disabled_and_never_contains_key_in_repr(self):
        for document in ({}, {"riot": {}}, {"riot": {"api_key": "   "}}):
            self.assertFalse(load_riot_config(document).enabled)
        config = load_riot_config({"riot": {"api_key": " " + FAKE_KEY + " "}})
        self.assertTrue(config.enabled)
        self.assertEqual(config.api_key, FAKE_KEY)
        self.assertNotIn(FAKE_KEY, repr(config))
        with patch("roly.riot_api._runtime_document", return_value={"riot": {"api_key": FAKE_KEY}}):
            self.assertEqual(load_riot_config().api_key, FAKE_KEY)

    def test_configuration_rejects_wrong_shapes_and_header_injection_without_echo(self):
        for value in (42, None, True, [], "secret\r\nHeader: value", "한글비밀문자", "https://secret"):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(ConfigError) as caught:
                    load_riot_config({"riot": {"api_key": value}})
                self.assertNotIn("secret", str(caught.exception))
        for document in ([], {"riot": "secret"}):
            with self.assertRaises(ConfigError):
                load_riot_config(document)

    def test_demo_access_defaults_off_and_requires_a_boolean(self):
        self.assertFalse(load_riot_config({"riot": {"api_key": FAKE_KEY}}).allow_demo)
        self.assertTrue(load_riot_config({"riot": {"api_key": FAKE_KEY, "allow_demo": True}}).allow_demo)
        for value in ("true", "false", 0, 1, None, []):
            with self.subTest(kind=type(value).__name__), self.assertRaises(ConfigError):
                load_riot_config({"riot": {"api_key": FAKE_KEY, "allow_demo": value}})

    def test_disabled_or_limited_client_never_starts_network(self):
        transport, limiter = Mock(), Mock()
        client = RiotClient(RiotConfig(), before_request=limiter, transport=transport)
        with self.assertRaises(RiotAPIError) as caught:
            client.account_by_riot_id("겨울", "KR99")
        self.assertEqual(caught.exception.code, "disabled")
        limiter.assert_not_called()
        transport.assert_not_called()
        limiter.side_effect = RiotAPIError("rate_limited", retry_after=1.2)
        client = RiotClient(RiotConfig(FAKE_KEY), before_request=limiter, transport=transport)
        with self.assertRaises(RiotAPIError) as caught:
            client.account_by_riot_id("겨울", "KR99")
        self.assertEqual(caught.exception.retry_after, 1.2)
        transport.assert_not_called()

    def test_all_four_endpoints_reserve_each_request_and_key_only_in_header(self):
        client, transport, limiter = self.client(
            response({"puuid": PUUID, "gameName": "겨울 / ?", "tagLine": "KR99"}),
            response({"puuid": PUUID, "profileIconId": 123, "summonerLevel": 200}),
            response([{ "queueType": SOLO_QUEUE, "tier": "GOLD", "rank": "II", "leaguePoints": 20}]),
            response([{"championId": 22, "championPoints": 500, "championLevel": 8}]),
        )
        self.assertEqual(client.account_by_riot_id("겨울 / ?", "KR99")["puuid"], PUUID)
        self.assertEqual(client.summoner_by_puuid(PUUID)["profile_icon_id"], 123)
        self.assertEqual(client.solo_rank_by_puuid(PUUID)["lp"], 20)
        self.assertEqual(client.top_masteries(PUUID)[0]["champion_id"], 22)
        self.assertEqual([call.args[0] for call in limiter.call_args_list], ["asia", "kr", "kr", "kr"])
        urls = [call.args[0] for call in transport.call_args_list]
        self.assertIn("%EA%B2%A8%EC%9A%B8%20%2F%20%3F/KR99", urls[0])
        self.assertEqual(urlsplit(urls[0]).netloc, "asia.api.riotgames.com")
        self.assertTrue(urls[2].endswith("/lol/league/v4/entries/by-puuid/" + PUUID))
        self.assertTrue(urls[3].endswith("/top?count=5"))
        for call in transport.call_args_list:
            url, headers, timeout, limit = call.args
            self.assertNotIn(FAKE_KEY, url)
            self.assertEqual(headers["X-Riot-Token"], FAKE_KEY)
            self.assertEqual(timeout, 5)
            self.assertEqual(limit, 1024 * 1024)

    def test_solo_rank_separates_flex_and_legitimately_unranked(self):
        flex = {"queueType": FLEX_QUEUE, "tier": "DIAMOND", "rank": "I", "leaguePoints": 12,
                "wins": 8, "losses": 2}
        solo = {"queueType": SOLO_QUEUE, "tier": "MASTER", "rank": "I", "leaguePoints": 222,
                "wins": 100, "losses": 80}
        client, transport, limiter = self.client(response([flex, solo]), response([flex]), response([]), response([solo]))
        self.assertEqual(client.solo_rank_by_puuid(PUUID), {
            "tier": "MASTER", "division": "I", "lp": 222, "wins": 100, "losses": 80, "queue": SOLO_QUEUE,
            "flex": {"tier": "DIAMOND", "division": "I", "lp": 12, "wins": 8, "losses": 2, "queue": FLEX_QUEUE}})
        only_flex = client.solo_rank_by_puuid(PUUID)
        self.assertEqual((only_flex["tier"], only_flex["flex"]["tier"]), ("UNRANKED", "DIAMOND"))
        empty = client.solo_rank_by_puuid(PUUID)
        self.assertEqual((empty["tier"], empty["flex"]["tier"]), ("UNRANKED", "UNRANKED"))
        self.assertEqual((empty["flex"]["wins"], empty["flex"]["losses"]), (0, 0))
        only_solo = client.solo_rank_by_puuid(PUUID)
        self.assertEqual((only_solo["tier"], only_solo["flex"]["tier"]), ("MASTER", "UNRANKED"))
        self.assertEqual((transport.call_count, limiter.call_count), (4, 4))

    def test_duplicate_or_invalid_flex_cannot_silently_replace_cached_ranks(self):
        solo = {"queueType": SOLO_QUEUE, "tier": "GOLD", "rank": "II"}
        flex = {"queueType": FLEX_QUEUE, "tier": "DIAMOND", "rank": "I", "leaguePoints": 12}
        invalid = [[solo, flex, flex], [solo, dict(flex, tier="UNKNOWN")],
                   [solo, dict(flex, rank="V")], [solo, dict(flex, wins=True)],
                   [solo, dict(flex, losses=-1)], [solo, dict(flex, leaguePoints="12")]]
        for data in invalid:
            with self.subTest(fields=[set(row) for row in data]):
                client, transport, limiter = self.client(response(data))
                with self.assertRaises(RiotAPIError) as caught:
                    client.solo_rank_by_puuid(PUUID)
                self.assertEqual(caught.exception.code, "invalid_response")
                self.assertEqual((transport.call_count, limiter.call_count), (1, 1))

    def test_omitted_zero_values_follow_riot_documentation(self):
        client, _, _ = self.client(response({}), response([{"queueType": SOLO_QUEUE,
            "tier": "BRONZE", "rank": "IV"}]), response([{"championId": 22}]), response([]))
        self.assertEqual(client.summoner_by_puuid(PUUID), {"profile_icon_id": 0, "summoner_level": 0})
        self.assertEqual(client.solo_rank_by_puuid(PUUID)["lp"], 0)
        self.assertEqual(client.top_masteries(PUUID), [{"champion_id": 22, "points": 0, "level": 0}])
        self.assertEqual(client.top_masteries(PUUID), [])

    def test_malformed_rank_cannot_turn_an_existing_rank_into_unranked(self):
        good = {"queueType": SOLO_QUEUE, "tier": "GOLD", "rank": "II", "leaguePoints": 10}
        invalid = [{}, [None], [{}], [dict(good, tier=[])], [dict(good, rank="V")], [good, good]]
        invalid += [[dict(good, leaguePoints=value)] for value in (True, "20", -1, 1e309, float("nan"))]
        for data in invalid:
            with self.subTest(data_type=type(data).__name__):
                client, _, _ = self.client(response(data))
                with self.assertRaises(RiotAPIError) as caught:
                    client.solo_rank_by_puuid(PUUID)
                self.assertEqual(caught.exception.code, "invalid_response")

    def test_mastery_returns_only_valid_unique_champions_sorted_by_points(self):
        client, _, _ = self.client(response([
            {"championId": 22, "championPoints": 500, "championLevel": 7},
            {"championId": 103, "championPoints": 900, "championLevel": 10},
            {"championId": 1, "championPoints": 500, "championLevel": 7},
        ]))
        self.assertEqual([c["champion_id"] for c in client.top_masteries(PUUID)], [103, 1, 22])
        for invalid in ([{"championId": 22}] * 2, [{"championId": 22}] * 4,
                        [{"championId": index} for index in range(1, 7)],
                        [{"championId": "22"}], [{"championId": 22, "championPoints": -1}]):
            client, _, _ = self.client(response(invalid))
            with self.assertRaises(RiotAPIError):
                client.top_masteries(PUUID)

    def test_five_masteries_are_sorted_and_fetched_in_one_request(self):
        rows = [{"championId": index, "championPoints": index * 100, "championLevel": index}
                for index in range(1, 6)]
        client, transport, limiter = self.client(response(rows))
        result = client.top_masteries(PUUID)
        self.assertEqual([row["champion_id"] for row in result], [5, 4, 3, 2, 1])
        self.assertEqual([row["points"] for row in result], [500, 400, 300, 200, 100])
        self.assertEqual([row["level"] for row in result], [5, 4, 3, 2, 1])
        self.assertEqual((transport.call_count, limiter.call_count), (1, 1))
        self.assertTrue(transport.call_args.args[0].endswith("/top?count=5"))

    def test_short_mastery_history_is_not_padded_to_five(self):
        for count in (0, 1, 3, 4):
            with self.subTest(count=count):
                rows = [{"championId": index, "championPoints": index * 100} for index in range(1, count + 1)]
                client, transport, limiter = self.client(response(rows))
                result = client.top_masteries(PUUID)
                self.assertEqual(len(result), count)
                self.assertEqual({row["champion_id"] for row in result}, set(range(1, count + 1)))
                self.assertEqual((transport.call_count, limiter.call_count), (1, 1))

    def test_account_and_summoner_identity_validation(self):
        for data in ({}, [], {"puuid": "bad value"}, {"puuid": PUUID, "gameName": "bad\nname"}):
            client, _, _ = self.client(response(data))
            with self.assertRaises(RiotAPIError):
                client.account_by_riot_id("겨울", "KR99")
        client, _, _ = self.client(response({"puuid": PUUID}), response({"puuid": "different"}))
        self.assertEqual(client.account_by_riot_id("겨울", "KR99")["game_name"], "")
        with self.assertRaises(RiotAPIError):
            client.summoner_by_puuid(PUUID)

    def test_error_bodies_are_ignored_and_statuses_distinguish_absence_from_unranked(self):
        for status, expected in ((401, "auth"), (403, "auth"), (404, "not_found"), (429, "rate_limited"),
                                 (400, "invalid_request"), (301, "unavailable"), (503, "unavailable")):
            client, _, _ = self.client(HTTPResponse(status, {}, (FAKE_KEY + " private account").encode()))
            with self.assertRaises(RiotAPIError) as caught:
                client.solo_rank_by_puuid(PUUID)
            error = caught.exception
            self.assertEqual((error.code, error.status, error.scope), (expected, status, "kr"))
            self.assertNotIn(FAKE_KEY, repr(error))
            self.assertNotIn("private account", str(error))

    def test_retry_after_honors_server_delay_and_malformed_header_uses_conservative_default(self):
        for value, expected in (("2", 2), ("0", 1), ("2.500", 2.5), ("7200", 7200),
                                 ("NaN", 120), ("-3", 120), (FAKE_KEY, 120), (None, 120)):
            client, _, _ = self.client(HTTPResponse(429, {"rEtRy-AfTeR": value}))
            with self.assertRaises(RiotAPIError) as caught:
                client.top_masteries(PUUID)
            self.assertEqual(caught.exception.retry_after, expected)

    def test_transport_failures_and_invalid_json_do_not_leak_credentials(self):
        client, transport, _ = self.client()
        transport.side_effect = RuntimeError(FAKE_KEY + " request headers")
        with self.assertRaises(RiotAPIError) as caught:
            client.summoner_by_puuid(PUUID)
        self.assertEqual(caught.exception.code, "unavailable")
        self.assertNotIn(FAKE_KEY, str(caught.exception))
        for body in (b"<html>error</html>", b"[NaN]", b"\xff", b"x" * (1024 * 1024 + 1)):
            client, _, _ = self.client(HTTPResponse(200, {}, body))
            with self.assertRaises(RiotAPIError) as caught:
                client.top_masteries(PUUID)
            self.assertEqual(caught.exception.code, "invalid_response")

    def test_invalid_path_inputs_are_rejected_before_rate_reservation(self):
        client, transport, limiter = self.client()
        for name in (None, "", "bad\nname", "x" * 101):
            with self.assertRaises(RiotAPIError) as caught:
                client.account_by_riot_id(name, "KR1")
            self.assertEqual(caught.exception.code, "invalid_request")
        transport.assert_not_called()
        limiter.assert_not_called()

    def test_http_boundary_allows_only_https_fixed_hosts_and_blocks_redirects(self):
        for url in ("http://kr.api.riotgames.com/test", "https://kr.api.riotgames.com.attacker.test/",
                    "https://attacker.test/", "https://secret@kr.api.riotgames.com/",
                    "https://kr.api.riotgames.com:444/test", "https://kr.api.riotgames.com/#fragment"):
            self.assertFalse(_allowed_url(url))
            with self.assertRaises(RiotAPIError):
                _http_get(url, {}, 5, 100)
        self.assertTrue(_allowed_url("https://kr.api.riotgames.com/test"))
        self.assertIsNone(_NoRedirect().redirect_request(Request("https://kr.api.riotgames.com/test"),
                                                        None, 302, "redirect", {}, "https://attacker.test/"))

    def test_http_error_body_is_not_read_and_success_body_is_bounded(self):
        body = Mock(wraps=io.BytesIO(FAKE_KEY.encode()))
        upstream = HTTPError("https://kr.api.riotgames.com/test", 403, FAKE_KEY, {}, body)
        opener = Mock()
        opener.open.side_effect = upstream
        with patch("roly.riot_api.build_opener", return_value=opener):
            self.assertEqual(_http_get("https://kr.api.riotgames.com/test", {}, 5, 100).status, 403)
        body.read.assert_not_called()
        body.close.assert_called_once()
        stream = Mock(status=200, headers={})
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=False)
        stream.read1.side_effect = [b"x" * 101]
        opener.open.side_effect = None
        opener.open.return_value = stream
        with patch("roly.riot_api.build_opener", return_value=opener):
            with self.assertRaises(RiotAPIError) as caught:
                _http_get("https://kr.api.riotgames.com/test", {}, 5, 100)
        self.assertEqual(caught.exception.code, "invalid_response")

    def test_data_dragon_metadata_is_key_free_and_builds_allowlisted_asset_urls(self):
        transport = Mock(side_effect=[response(["16.17.1", "16.16.1"]), response({"data": {
            "Ashe": {"key": "22", "name": "애쉬", "image": {"full": "Ashe.png"}},
            "Ahri": {"key": "103", "name": "아리", "image": {"full": "Ahri.png"}},
        }})])
        client = DataDragonClient(transport=transport)
        version = client.latest_version()
        champions = client.champions(version)
        self.assertEqual(champions["22"], {"name": "애쉬", "image":
            "https://ddragon.leagueoflegends.com/cdn/16.17.1/img/champion/Ashe.png"})
        self.assertEqual(profile_icon_url(version, 0),
                         "https://ddragon.leagueoflegends.com/cdn/16.17.1/img/profileicon/0.png")
        for call in transport.call_args_list:
            self.assertNotIn("X-Riot-Token", call.args[1])
            self.assertEqual(urlsplit(call.args[0]).hostname, "ddragon.leagueoflegends.com")

    def test_data_dragon_rejects_paths_and_ambiguous_metadata(self):
        for version in ("../../secret", "16.17.1/?secret", None):
            transport = Mock()
            with self.assertRaises(RiotAPIError):
                DataDragonClient(transport=transport).champions(version)
            transport.assert_not_called()
        for data in ({}, {"data": {}}, {"data": {"X": {"key": "22", "name": "X", "image": {
                "full": "../../secret.png"}}}}, {"data": {"X": {"key": "22", "name": "X",
                "image": {"full": "X.png"}}, "Y": {"key": "22", "name": "Y", "image": {"full": "Y.png"}}}}):
            with self.assertRaises(RiotAPIError):
                DataDragonClient(transport=Mock(return_value=response(data))).champions("16.17.1")

    def test_timeouts_are_bounded_not_nan_and_limiter_is_required(self):
        for timeout in (float("nan"), float("inf"), 0, 11, True, "5"):
            with self.assertRaises(ConfigError):
                RiotClient(RiotConfig(FAKE_KEY), before_request=Mock(), timeout=timeout)
            with self.assertRaises(ConfigError):
                DataDragonClient(timeout=timeout)
        with self.assertRaises(ConfigError):
            RiotClient(RiotConfig(FAKE_KEY), before_request=None)


if __name__ == "__main__":
    unittest.main()
