from __future__ import annotations

import time

from jev_reflex.jev import (
    INPUT_USD_PER_MILLION,
    JevAction,
    JevController,
    JevDecision,
    JevTelemetry,
    WeaponAction,
)


def fake_decision(state, legal_actions):
    action = legal_actions[0]
    probabilities = {candidate.value: 0.0 for candidate in legal_actions}
    probabilities[action.value] = 1.0
    return JevDecision(
        action=action,
        weapon_action=WeaponAction.FIRE,
        confidence=1.0,
        weapon_confidence=0.9,
        probabilities=probabilities,
        weapon_probabilities={"fire": 0.8, "hold": 0.15, "reload": 0.05},
        latency_ms=4.2,
        input_tokens=250,
        output_tokens=30,
        model="jev-test",
        request_id="req-test",
        state=state,
    )


def wait_for_decision(controller: JevController, run_id: int) -> JevDecision:
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        decision = controller.poll(run_id=run_id)
        if decision:
            return decision
        time.sleep(0.001)
    raise AssertionError("controller did not return a decision")


def test_controller_tracks_usage_and_cost(tmp_path) -> None:
    controller = JevController(
        decision_function=fake_decision,
        artifacts_dir=tmp_path,
        min_interval=0,
    )
    assert controller.request({"player": {"health": "safe"}}, [JevAction.SEEK_TARGET], run_id=3)
    decision = wait_for_decision(controller, 3)

    assert decision.action is JevAction.SEEK_TARGET
    assert decision.weapon_action is WeaponAction.FIRE
    assert controller.telemetry.weapon_probabilities["fire"] == 0.8
    assert controller.telemetry.input_tokens == 250
    assert controller.telemetry.output_tokens == 30
    assert controller.telemetry.input_cost == 250 * INPUT_USD_PER_MILLION / 1_000_000
    assert controller.telemetry.output_cost == 0
    assert controller.log_path.read_text().count("\n") == 1
    controller.close()


def test_stale_run_decision_is_discarded(tmp_path) -> None:
    controller = JevController(
        decision_function=fake_decision,
        artifacts_dir=tmp_path,
        min_interval=0,
    )
    controller.request({}, [JevAction.SEEK_TARGET], run_id=1)
    deadline = time.monotonic() + 1
    while controller.pending and time.monotonic() < deadline:
        controller.poll(run_id=2)
        time.sleep(0.001)

    assert controller.telemetry.requests == 0
    controller.close()


def test_error_is_visible_and_backed_off(tmp_path) -> None:
    def fail(_state, _actions):
        raise RuntimeError("network unavailable")

    controller = JevController(
        decision_function=fail,
        artifacts_dir=tmp_path,
        min_interval=0,
    )
    controller.request({}, [JevAction.SEEK_TARGET], run_id=1)
    deadline = time.monotonic() + 1
    while controller.pending and time.monotonic() < deadline:
        controller.poll(run_id=1)
        time.sleep(0.001)

    assert controller.telemetry.status == "ERROR"
    assert controller.telemetry.errors == 1
    assert "network unavailable" in controller.telemetry.last_error
    assert not controller.request({}, [JevAction.SEEK_TARGET], run_id=1)
    controller.close()


def test_decision_rate_uses_recent_window() -> None:
    telemetry = JevTelemetry()
    telemetry.recent_decisions.extend((10.0, 10.2, 10.4, 10.6))
    assert 4.9 < telemetry.decisions_per_second(10.6) < 5.1
