"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


def _csv(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))


@dataclass(frozen=True, slots=True)
class AppConfig:
    gemini_keys: tuple[str, ...]
    gemini_models: tuple[str, ...]
    tuxun_cookie: str
    proxy: str | None
    clash_controller: str | None
    clash_secret: str
    request_timeout: float = 15.0
    monitor_interval: float = 2.0

    @classmethod
    def from_env(cls, dotenv_path: str | None = None) -> "AppConfig":
        load_dotenv(dotenv_path)
        keys = tuple(dict.fromkeys(
            key for key in (os.getenv("API_KEY"), os.getenv("API_KEY2")) if key
        ))
        primary_model = os.getenv("GEMINI_MODEL", "").strip() or "gemini-3.6-flash"
        models = tuple(dict.fromkeys((primary_model, *_csv(os.getenv("GEMINI_MODEL_FALLBACKS", "")))))
        cookie = os.getenv("TUXUN_COOKIE", "")
        if not keys:
            raise ValueError("未配置 API_KEY。请复制 .env.example 并填入 Gemini API Key。")
        if not cookie:
            raise ValueError("未配置 TUXUN_COOKIE。请复制 .env.example 并填入图寻 Cookie。")
        proxy = os.getenv("PROXY") or None
        controller = os.getenv("CLASH_CONTROLLER") or None
        if controller and not proxy:
            raise ValueError("配置 CLASH_CONTROLLER 时必须同时配置 PROXY（Clash 混合端口）")
        return cls(
            gemini_keys=keys,
            gemini_models=models,
            tuxun_cookie=cookie,
            proxy=proxy,
            clash_controller=controller,
            clash_secret=os.getenv("CLASH_SECRET", ""),
        )
