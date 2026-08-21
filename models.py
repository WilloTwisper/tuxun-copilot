"""Domain models shared by the Tuxun and analysis layers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class GameRound:
    number: int
    pano_id: str


@dataclass(frozen=True, slots=True)
class GameState:
    game_id: str
    status: str
    total_rounds: int | None
    rounds: tuple[GameRound, ...]
    mode: str

    @property
    def ready_rounds(self) -> tuple[GameRound, ...]:
        return tuple(round_ for round_ in self.rounds if round_.pano_id)


def parse_game_state(game_id: str, payload: dict[str, Any], mode: str) -> GameState:
    rounds: list[GameRound] = []
    for index, item in enumerate(payload.get("rounds") or [], start=1):
        if not isinstance(item, dict):
            continue
        pano_id = item.get("panoId") or item.get("pano_id")
        if not pano_id:
            continue
        number = item.get("round") or index
        try:
            number = int(number)
        except (TypeError, ValueError):
            number = index
        rounds.append(GameRound(number=number, pano_id=str(pano_id)))
    total = payload.get("roundNumber") or payload.get("roundsCount")
    try:
        total_rounds = int(total) if total is not None else None
    except (TypeError, ValueError):
        total_rounds = None
    return GameState(
        game_id=game_id,
        status=str(payload.get("status") or ""),
        total_rounds=total_rounds,
        rounds=tuple(rounds),
        mode=mode,
    )
