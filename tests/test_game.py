import os
import time
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame

from jev_reflex.game import MAGAZINE_SIZE, RELOAD_SECONDS, Game, reflect_velocity
from jev_reflex.jev import JevAction, JevController, JevDecision, WeaponAction


def test_reflect_velocity_off_vertical_surface() -> None:
    reflected = reflect_velocity(pygame.Vector2(10, 4), pygame.Vector2(-1, 0))
    assert reflected == pygame.Vector2(-10, 4)


def test_game_can_start_and_advance() -> None:
    game = Game(seed=7)
    game.start()
    game.update(1 / 60)

    assert game.mode == "play"
    assert game.health == 5
    assert game.wave == 1
    game.sound.play("bounce", 2)
    game.sound.play("kill", 4)
    game.sound.play("overdrive")
    pygame.quit()


def test_fake_jev_pilot_drives_complete_game_loop(tmp_path: Path) -> None:
    def choose(state, legal_actions):
        action = JevAction.ORBIT_CLOCKWISE
        probabilities = {candidate.value: 0.06 for candidate in legal_actions}
        probabilities[action.value] = 0.64
        return JevDecision(
            action=action,
            weapon_action=WeaponAction.FIRE,
            confidence=0.53,
            weapon_confidence=0.61,
            probabilities=probabilities,
            weapon_probabilities={"fire": 0.71, "hold": 0.2, "reload": 0.09},
            latency_ms=9.0,
            input_tokens=200,
            output_tokens=25,
            model="jev-test",
            request_id="req-integration",
            state=state,
        )

    game = Game(seed=11)
    game.start("human")
    game.pilot_mode = "jev"
    game.configure_display()
    game.jev_controller = JevController(
        decision_function=choose,
        artifacts_dir=tmp_path,
        min_interval=0,
    )

    deadline = time.monotonic() + 1
    while game.jev_controller.telemetry.requests < 2 and time.monotonic() < deadline:
        game.update(1 / 60)
        time.sleep(0.001)

    assert game.jev_action is JevAction.ORBIT_CLOCKWISE
    assert game.jev_controller.telemetry.requests >= 2
    assert game.jev_controller.telemetry.input_tokens >= 400
    assert game.jev_controller.telemetry.weapon_action is WeaponAction.FIRE
    assert any(bullet.friendly for bullet in game.bullets)
    game.jev_controller.close()
    pygame.quit()


def test_magazine_automatically_reloads_after_two_seconds() -> None:
    game = Game(seed=19)
    game.start("human")

    for _ in range(MAGAZINE_SIZE):
        game.fire_cooldown = 0
        game.shoot()

    assert game.ammo == 0
    assert game.reload_timer == RELOAD_SECONDS

    game.update(RELOAD_SECONDS - 0.01)
    assert game.ammo == 0
    assert game.reload_timer > 0
    game.update(0.02)
    assert game.ammo == MAGAZINE_SIZE
    assert game.reload_timer == 0
    pygame.quit()


def test_jev_state_exposes_authoritative_reload_data() -> None:
    game = Game(seed=23)
    game.start("human")
    game.ammo = 2
    game.start_reload()

    weapon = game.build_jev_state()["weapon"]

    assert weapon == {
        "ammo": 2,
        "capacity": MAGAZINE_SIZE,
        "ammo_state": "low",
        "reloading": True,
        "reload_seconds_total": RELOAD_SECONDS,
        "reload_seconds_remaining": RELOAD_SECONDS,
        "target_available": False,
    }
    pygame.quit()


def test_jev_mode_runs_at_three_quarter_simulation_speed() -> None:
    game = Game(seed=29)
    game.start("human")
    game.pilot_mode = "jev"
    game.configure_display()
    before = game.elapsed

    game.update(1.0)

    assert game.elapsed - before == 0.75
    pygame.quit()
