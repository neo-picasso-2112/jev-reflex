from __future__ import annotations

import json
import os
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

INPUT_USD_PER_MILLION = 0.042
MODEL = "jev-1.13.0"


class JevAction(StrEnum):
    SEEK_TARGET = "seek_target"
    ORBIT_CLOCKWISE = "orbit_clockwise"
    ORBIT_COUNTERCLOCKWISE = "orbit_counterclockwise"
    RETREAT = "retreat"
    BANK_LEFT = "bank_left"
    BANK_RIGHT = "bank_right"
    INTERCEPT_THREAT = "intercept_threat"
    DASH_SAFE = "dash_safe"


ACTION_DESCRIPTIONS: dict[JevAction, str] = {
    JevAction.SEEK_TARGET: "Approach the best enemy target and aim directly at it.",
    JevAction.ORBIT_CLOCKWISE: "Strafe clockwise around the target while aiming and dodging.",
    JevAction.ORBIT_COUNTERCLOCKWISE: (
        "Strafe counterclockwise around the target while aiming and dodging."
    ),
    JevAction.RETREAT: "Move away from the closest threat while keeping aim on the target.",
    JevAction.BANK_LEFT: "Aim a calculated bank shot from the left wall while moving safely.",
    JevAction.BANK_RIGHT: "Aim a calculated bank shot from the right wall while moving safely.",
    JevAction.INTERCEPT_THREAT: (
        "Aim at the closest hostile bullet while moving toward the safest open direction."
    ),
    JevAction.DASH_SAFE: "Dash immediately toward the safest open direction.",
}


class WeaponAction(StrEnum):
    FIRE = "fire"
    HOLD = "hold"
    RELOAD = "reload"


WEAPON_DESCRIPTIONS: dict[WeaponAction, str] = {
    WeaponAction.FIRE: "Spend one round now because the current shot is useful.",
    WeaponAction.HOLD: "Conserve ammunition because the current shot is low value.",
    WeaponAction.RELOAD: "Reload now because ammunition is low and the danger window allows it.",
}


class JevConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class JevDecision:
    action: JevAction
    weapon_action: WeaponAction
    confidence: float
    weapon_confidence: float
    probabilities: Mapping[str, float]
    weapon_probabilities: Mapping[str, float]
    latency_ms: float
    input_tokens: int
    output_tokens: int
    model: str
    request_id: str
    state: Mapping[str, Any]

    @property
    def selected_probability(self) -> float:
        return float(self.probabilities.get(self.action.value, 0.0))

    @property
    def selected_weapon_probability(self) -> float:
        return float(self.weapon_probabilities.get(self.weapon_action.value, 0.0))


@dataclass
class JevTelemetry:
    status: str = "READY"
    action: JevAction | None = None
    weapon_action: WeaponAction | None = None
    confidence: float = 0.0
    weapon_confidence: float = 0.0
    probabilities: dict[str, float] = field(default_factory=dict)
    weapon_probabilities: dict[str, float] = field(default_factory=dict)
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0
    errors: int = 0
    model: str = MODEL
    last_error: str = ""
    recent_decisions: deque[float] = field(default_factory=lambda: deque(maxlen=64))

    @property
    def input_cost(self) -> float:
        return self.input_tokens * INPUT_USD_PER_MILLION / 1_000_000

    @property
    def output_cost(self) -> float:
        return 0.0

    @property
    def total_cost(self) -> float:
        return self.input_cost + self.output_cost

    def decisions_per_second(self, now: float | None = None) -> float:
        current = now if now is not None else time.monotonic()
        while self.recent_decisions and current - self.recent_decisions[0] > 5.0:
            self.recent_decisions.popleft()
        if len(self.recent_decisions) < 2:
            return 0.0
        span = max(0.001, current - self.recent_decisions[0])
        return (len(self.recent_decisions) - 1) / span

    def record(self, decision: JevDecision) -> None:
        self.action = decision.action
        self.weapon_action = decision.weapon_action
        self.confidence = decision.confidence
        self.weapon_confidence = decision.weapon_confidence
        self.probabilities = dict(decision.probabilities)
        self.weapon_probabilities = dict(decision.weapon_probabilities)
        self.latency_ms = decision.latency_ms
        self.input_tokens += decision.input_tokens
        self.output_tokens += decision.output_tokens
        self.requests += 1
        self.model = decision.model
        self.last_error = ""
        self.recent_decisions.append(time.monotonic())


class TypeSafePolicy:
    """Small synchronous TypeSafe caller intended to run off the pygame thread."""

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY")
        if not self.api_key:
            raise JevConfigurationError("TYPESAFE_API_KEY is not set")
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            from typesafe_sdk import RetryPolicy, TypeSafeClient

            self._client = TypeSafeClient(
                api_key=self.api_key,
                model=MODEL,
                retry=RetryPolicy(max_retries=1, backoff_max=0.1, timeout=1.2),
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def choose(self, state: Mapping[str, Any], legal_actions: Sequence[JevAction]) -> JevDecision:
        from typesafe_sdk import Choice

        criteria = {action.value: ACTION_DESCRIPTIONS[action] for action in legal_actions}
        movement_question = Choice(
            instructions={
                "task": "Choose the ship's next short control macro for about 200 milliseconds.",
                "priority": (
                    "Survive first, destroy threats second, and use bank shots when they are safe."
                ),
                "state_guidance": (
                    "Trust the precomputed danger, direction, distance, and safe-direction labels. "
                    "React immediately when `threat.immediate` is true. Avoid repeating an action "
                    "when `recent_control.result` says it failed."
                ),
            },
            criteria=criteria,
        )
        weapon_question = Choice(
            instructions={
                "task": "Choose the weapon commitment for the same next 200 milliseconds.",
                "priority": (
                    "Fire only for a useful target or interception, conserve rounds when shot "
                    "quality is poor, and reload when ammunition is low and danger allows it."
                ),
                "state_guidance": (
                    "Use weapon.ammo_state, reloading, target_available, and threat.immediate. "
                    "Do not choose fire while reloading or reload when the magazine is full."
                ),
            },
            criteria={
                action.value: description for action, description in WEAPON_DESCRIPTIONS.items()
            },
        )
        started = time.perf_counter()
        response = self._get_client().system_one(
            state=state,
            questions={"movement": movement_question, "weapon": weapon_question},
        )
        latency_ms = (time.perf_counter() - started) * 1000
        choices = getattr(response, "choices", None)
        answers = choices or response.answers
        movement_answer = answers["movement"]
        weapon_answer = answers["weapon"]
        usage = response.usage
        return JevDecision(
            action=JevAction(str(movement_answer.choice)),
            weapon_action=WeaponAction(str(weapon_answer.choice)),
            confidence=float(movement_answer.confidence),
            weapon_confidence=float(weapon_answer.confidence),
            probabilities={
                str(key): float(value) for key, value in movement_answer.probabilities.items()
            },
            weapon_probabilities={
                str(key): float(value) for key, value in weapon_answer.probabilities.items()
            },
            latency_ms=latency_ms,
            input_tokens=int(usage.input_tokens or 0),
            output_tokens=int(usage.output_tokens or 0),
            model=str(response.model),
            request_id=str(response.request_id),
            state=state,
        )


DecisionFunction = Callable[[Mapping[str, Any], Sequence[JevAction]], JevDecision]


class JevController:
    """Owns one in-flight request and exposes non-blocking polling to the game loop."""

    def __init__(
        self,
        *,
        decision_function: DecisionFunction | None = None,
        api_key: str | None = None,
        artifacts_dir: Path | None = None,
        min_interval: float = 0.1,
    ) -> None:
        self.telemetry = JevTelemetry()
        self.min_interval = min_interval
        self._policy = None if decision_function else TypeSafePolicy(api_key)
        self._choose = decision_function or self._policy.choose
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jev-pilot")
        self._pending: Future[JevDecision] | None = None
        self._pending_run = 0
        self._next_request_at = 0.0
        self._backoff_until = 0.0
        self._closed = False
        root = artifacts_dir or Path("artifacts")
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        self.log_path = root / f"jev-run-{stamp}.jsonl"

    @property
    def pending(self) -> bool:
        return self._pending is not None

    def request(
        self,
        state: Mapping[str, Any],
        legal_actions: Sequence[JevAction],
        *,
        run_id: int,
        now: float | None = None,
    ) -> bool:
        current = now if now is not None else time.monotonic()
        if (
            self._closed
            or self._pending is not None
            or current < self._next_request_at
            or current < self._backoff_until
        ):
            return False
        self._pending_run = run_id
        self._pending = self._executor.submit(self._choose, state, tuple(legal_actions))
        self._next_request_at = current + self.min_interval
        self.telemetry.status = "THINKING"
        return True

    def poll(self, *, run_id: int, now: float | None = None) -> JevDecision | None:
        if self._pending is None or not self._pending.done():
            return None
        current = now if now is not None else time.monotonic()
        future = self._pending
        pending_run = self._pending_run
        self._pending = None
        try:
            decision = future.result()
        except Exception as exc:  # noqa: BLE001 - surface worker failures in the HUD.
            self.telemetry.errors += 1
            self.telemetry.status = "ERROR"
            self.telemetry.last_error = str(exc)[:90]
            self._backoff_until = current + 1.0
            return None
        if pending_run != run_id:
            return None
        self.telemetry.record(decision)
        self.telemetry.status = "ACTIVE"
        self._write_record(decision)
        return decision

    def _write_record(self, decision: JevDecision) -> None:
        record = {
            "request": self.telemetry.requests,
            "request_id": decision.request_id,
            "model": decision.model,
            "action": decision.action.value,
            "weapon_action": decision.weapon_action.value,
            "selected_probability": decision.selected_probability,
            "selected_weapon_probability": decision.selected_weapon_probability,
            "confidence": decision.confidence,
            "weapon_confidence": decision.weapon_confidence,
            "probabilities": dict(decision.probabilities),
            "weapon_probabilities": dict(decision.weapon_probabilities),
            "latency_ms": decision.latency_ms,
            "usage": {
                "input_tokens": decision.input_tokens,
                "output_tokens": decision.output_tokens,
            },
            "running": {
                "input_tokens": self.telemetry.input_tokens,
                "output_tokens": self.telemetry.output_tokens,
                "estimated_cost_usd": self.telemetry.total_cost,
            },
            "state": decision.state,
        }
        with self.log_path.open("a", encoding="utf-8") as log:
            log.write(json.dumps(record, separators=(",", ":")) + "\n")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        pending = self._pending
        self._executor.shutdown(wait=False, cancel_futures=True)
        if self._policy is not None:
            if pending is not None and not pending.done():
                pending.add_done_callback(lambda _future: self._policy.close())
            else:
                self._policy.close()


def api_key_available() -> bool:
    return bool(os.environ.get("TYPESAFE_API_KEY"))
