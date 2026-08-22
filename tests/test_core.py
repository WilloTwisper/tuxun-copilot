import unittest
from unittest.mock import Mock

import requests
from config import AppConfig
from gemini_router import GeminiRouter
from models import parse_game_state
from streetview import StreetViewClient
from tuxun_client import TuxunClient


class FakeResponse:
    def __init__(self, text=None, error=None):
        self.text = text
        self.error = error

    @property
    def models(self):
        return self

    def generate_content(self, **kwargs):
        if self.error:
            raise self.error
        return self


class RouterTests(unittest.TestCase):
    def test_rotates_model_after_quota_error(self):
        first = FakeResponse(error=RuntimeError("429 RESOURCE_EXHAUSTED"))
        second = FakeResponse(text="ok")
        router = GeminiRouter([first, second], ("primary", "fallback"), sleeper=lambda _: None)
        self.assertEqual(router.generate("prompt", {}), "ok")
        self.assertEqual(router.last_model, "primary")
        second.text = "again"
        self.assertEqual(router.generate("prompt", {}), "again")
        self.assertEqual(router.last_model, "primary")

    def test_rotates_to_next_model_when_only_one_key_exists(self):
        client = FakeResponse(error=RuntimeError("429 RESOURCE_EXHAUSTED"))
        router = GeminiRouter([client], ("primary", "fallback"), sleeper=lambda _: None)
        calls = 0

        def generate_content(**kwargs):
            nonlocal calls
            calls += 1
            if kwargs["model"] == "primary":
                raise RuntimeError("429 RESOURCE_EXHAUSTED")
            return FakeResponse(text="fallback-ok")

        client.generate_content = generate_content
        self.assertEqual(router.generate("prompt", {}), "fallback-ok")
        self.assertEqual(router.last_model, "fallback")

    def test_skips_dead_key(self):
        clients = [FakeResponse(error=RuntimeError("403 PERMISSION_DENIED")), FakeResponse(text="ok")]
        router = GeminiRouter(clients, ("model",), sleeper=lambda _: None)
        self.assertEqual(router.generate("prompt", {}), "ok")
        self.assertEqual(router.dead_keys, {0})

    def test_model_not_found_only_disables_one_key_model_pair(self):
        clients = [FakeResponse(error=RuntimeError("404 NOT_FOUND")), FakeResponse(text="ok")]
        router = GeminiRouter(clients, ("model",), sleeper=lambda _: None)
        self.assertEqual(router.generate("prompt", {}), "ok")
        self.assertEqual(router.dead_combinations, {(0, 0)})


class ParsingTests(unittest.TestCase):
    def test_game_id_parser_accepts_paths_and_query_strings(self):
        self.assertEqual(TuxunClient.parse_game_id("https://tuxun.fun/solo/ABC123"), "ABC123")
        self.assertEqual(TuxunClient.parse_game_id("https://tuxun.fun/?gameId=ABC123"), "ABC123")

    def test_game_state_normalizes_rounds(self):
        state = parse_game_state("ABC123", {
            "status": "ongoing",
            "roundNumber": 5,
            "rounds": [{"round": 2, "panoId": "pano-2"}],
        }, "fast")
        self.assertEqual(state.ready_rounds[0].pano_id, "pano-2")
        self.assertEqual(state.total_rounds, 5)

    def test_game_state_exposes_deadline_and_guesses(self):
        state = parse_game_state("ABC123", {
            "status": "ongoing",
            "currentRound": 2,
            "roundNumber": 5,
            "roundTimePeriod": 15000,
            "playerIds": ["a", "b"],
            "teams": [{"teamUsers": [{"user": {"userId": "a"}, "guesses": [{"round": 2}]}]}],
            "rounds": [{"round": 2, "panoId": "pano-2", "timerStartTime": 1000}],
        }, "fast")
        self.assertEqual(state.current_round, 2)
        self.assertEqual(state.deadline_ms(state.active_round), 16000)
        self.assertEqual(state.active_round.guessed_user_ids, ("a",))
        self.assertEqual(state.player_count, 2)

    def test_streetview_urls_have_four_yaws(self):
        urls = StreetViewClient().image_urls("pano")
        self.assertEqual(tuple(urls), ("前", "右", "后", "左"))
        self.assertTrue(all("panoid=pano" in url for url in urls.values()))

    def test_uuid_challenge_failure_falls_back_to_solo(self):
        client = TuxunClient("fun_ticket=value")
        challenge = Mock()
        challenge.raise_for_status.side_effect = requests.HTTPError("challenge unavailable")
        solo = Mock()
        solo.raise_for_status.return_value = None
        solo.json.return_value = {
            "success": True,
            "data": {"status": "ongoing", "rounds": [{"round": 1, "panoId": "pano"}]},
        }
        client.session.get = Mock(side_effect=(challenge, solo))
        state = client.get_game("00000000-0000-0000-0000-000000000000")
        self.assertIsNotNone(state)
        self.assertEqual(state.mode, "fast")
        self.assertEqual(state.rounds[0].pano_id, "pano")


class ConfigTests(unittest.TestCase):
    def test_config_is_immutable(self):
        config = AppConfig(("key",), ("model",), "cookie", None, None, "")
        with self.assertRaises(AttributeError):
            config.proxy = "unexpected"


if __name__ == "__main__":
    unittest.main()
