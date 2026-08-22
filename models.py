"""Domain models shared by the Tuxun and analysis layers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class GameRound:
    number: int
    pano_id: str
    start_time_ms: int | None = None
    timer_start_ms: int | None = None
    guess_start_ms: int | None = None
    end_time_ms: int | None = None
    guessed_user_ids: tuple[str, ...] = ()

    @property
    def deadline_ms(self) -> int | None:
        return self.end_time_ms or self.guess_start_ms or self.timer_start_ms


@dataclass(frozen=True, slots=True)
class GameState:
    game_id: str
    status: str
    total_rounds: int | None
    rounds: tuple[GameRound, ...]
    mode: str
    current_round: int | None = None
    round_time_ms: int | None = None
    guess_time_ms: int | None = None
    player_count: int = 0

    @property
    def ready_rounds(self) -> tuple[GameRound, ...]:
        return tuple(round_ for round_ in self.rounds if round_.pano_id)

    @property
    def active_round(self) -> GameRound | None:
        if self.current_round is not None:
            match = next((round_ for round_ in self.rounds if round_.number == self.current_round), None)
            if match:
                return match
        return self.rounds[-1] if self.rounds else None

    def deadline_ms(self, round_: GameRound) -> int | None:
        if round_.end_time_ms is not None:
            return round_.end_time_ms
        if round_.guess_start_ms is not None and self.guess_time_ms is not None:
            return round_.guess_start_ms + self.guess_time_ms
        if round_.timer_start_ms is not None and self.round_time_ms is not None:
            return round_.timer_start_ms + self.round_time_ms
        return None


def parse_game_state(game_id: str, payload: dict[str, Any], mode: str) -> GameState:
    def timestamp(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    guesses_by_round: dict[int, set[str]] = {}
    for team in payload.get("teams") or []:
        if not isinstance(team, dict):
            continue
        for member in team.get("teamUsers") or []:
            if not isinstance(member, dict):
                continue
            user = member.get("user") or {}
            user_id = str(user.get("userId") or "")
            for guess in member.get("guesses") or []:
                if not isinstance(guess, dict):
                    continue
                try:
                    guess_round = int(guess.get("round"))
                except (TypeError, ValueError):
                    continue
                guesses_by_round.setdefault(guess_round, set()).add(user_id)

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
        rounds.append(GameRound(
            number=number,
            pano_id=str(pano_id),
            start_time_ms=timestamp(item.get("startTime")),
            timer_start_ms=timestamp(item.get("timerStartTime")),
            guess_start_ms=timestamp(item.get("timerGuessStartTime")),
            end_time_ms=timestamp(item.get("endTime")),
            guessed_user_ids=tuple(sorted(guesses_by_round.get(number, set()))),
        ))
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
        current_round=timestamp(payload.get("currentRound")),
        round_time_ms=timestamp(payload.get("roundTimePeriod")),
        guess_time_ms=timestamp(payload.get("roundTimeGuessPeriod")),
        player_count=len(payload.get("playerIds") or payload.get("players") or []),
    )
