"""Gemini model/key failover with explicit, testable state."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any


class GeminiCapacityError(RuntimeError):
    """All configured model/key combinations are unavailable or rate limited."""


class GeminiRouter:
    def __init__(self, clients: list[Any], models: tuple[str, ...], sleeper: Callable[[float], None] = time.sleep):
        if not clients:
            raise ValueError("至少需要一个 Gemini 客户端")
        if not models:
            raise ValueError("至少需要一个 Gemini 模型")
        self.clients = clients
        self.models = models
        self._sleep = sleeper
        self._cursor = 0
        self.dead_keys: set[int] = set()
        self.dead_combinations: set[tuple[int, int]] = set()
        self.last_model: str | None = None

    @staticmethod
    def _kind(error: Exception) -> str:
        text = str(error)
        if "PERMISSION_DENIED" in text or "403" in text:
            return "key"
        if "NOT_FOUND" in text and "404" in text:
            return "model"
        if "RESOURCE_EXHAUSTED" in text or "429" in text:
            return "quota"
        if any(marker in text for marker in ("503", "504", "UNAVAILABLE", "DEADLINE_EXCEEDED")):
            return "service"
        return "other"

    def generate(self, contents: Any, config: Any, max_attempts: int | None = None) -> str | None:
        combinations = [(model_index, key_index)
                        for model_index in range(len(self.models))
                        for key_index in range(len(self.clients))]
        attempt_limit = max_attempts or max(1, len(combinations) * 2)
        for _ in range(attempt_limit):
            combination = combinations[self._cursor % len(combinations)]
            model_index, key_index = combination
            if key_index in self.dead_keys or combination in self.dead_combinations:
                self._cursor += 1
                continue
            model = self.models[model_index]
            try:
                response = self.clients[key_index].models.generate_content(
                    model=model, contents=contents, config=config
                )
                self.last_model = model
                return response.text
            except Exception as error:
                kind = self._kind(error)
                if kind == "key":
                    self.dead_keys.add(key_index)
                    self._cursor += 1
                    continue
                if kind == "model":
                    self.dead_combinations.add(combination)
                    self._cursor += 1
                    continue
                if kind == "quota":
                    self._cursor += 1
                    self._sleep(1)
                    continue
                if kind == "service":
                    # 服务端容量/截止时间问题与本地代理无关，直接尝试下一个模型/Key。
                    self._cursor += 1
                    continue
                raise
        raise GeminiCapacityError("所有可用 Gemini 模型和 Key 均不可用或被限流")
