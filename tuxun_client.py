"""Authenticated client for the tuxun.fun game API."""

from __future__ import annotations

import re
from http.cookies import SimpleCookie

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from models import GameState, parse_game_state


class TuxunClient:
    API_ROOT = "https://tuxun.fun/api/v0/tuxun"
    GAME_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{6,64}$")

    def __init__(self, cookie: str, timeout: float = 15.0):
        if not cookie:
            raise ValueError("图寻 Cookie 不能为空")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
            "Accept": "application/json",
            "Origin": "https://tuxun.fun",
            "Referer": "https://tuxun.fun/",
        })
        parsed_cookie = SimpleCookie()
        parsed_cookie.load(cookie)
        if not parsed_cookie:
            raise ValueError("图寻 Cookie 格式无效")
        for name, morsel in parsed_cookie.items():
            self.session.cookies.set(name, morsel.value, domain="tuxun.fun", path="/")
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    @classmethod
    def parse_game_id(cls, value: str) -> str:
        text = value.strip()
        path_match = re.search(r"/(?:solo|party|main_game|match|challenge)/([A-Za-z0-9_-]+)", text)
        query_match = re.search(r"[?&](?:id|gameId|game_id|challengeId)=([A-Za-z0-9_-]+)", text)
        game_id = path_match.group(1) if path_match else query_match.group(1) if query_match else text
        if not cls.GAME_ID_PATTERN.fullmatch(game_id):
            raise ValueError("无法识别游戏 ID")
        return game_id

    def current_user_id(self) -> str | None:
        response = self.session.get(
            f"{self.API_ROOT}/user/getSelfProfile", timeout=self.timeout, allow_redirects=False
        )
        response.raise_for_status()
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        user_id = data.get("userId") if isinstance(data, dict) else None
        return str(user_id) if user_id is not None else None

    def get_game(self, game_id: str) -> GameState | None:
        is_uuid = re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", game_id)
        endpoints = (
            (("challenge/getGameInfo", "challengeId", "precise"), ("solo/get", "gameId", "fast"))
            if is_uuid else (("solo/get", "gameId", "fast"),)
        )
        for path, parameter, mode in endpoints:
            try:
                response = self.session.get(
                    f"{self.API_ROOT}/{path}", params={parameter: game_id},
                    timeout=self.timeout, allow_redirects=False,
                )
                response.raise_for_status()
                body = response.json()
                payload = body.get("data") if isinstance(body, dict) and body.get("success") else None
                if isinstance(payload, dict) and payload:
                    return parse_game_state(game_id, payload, mode)
            except (requests.RequestException, ValueError):
                continue
        return None
