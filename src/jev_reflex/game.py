from __future__ import annotations

import math
import random
from array import array
from dataclasses import dataclass
from typing import Any

import pygame
from dotenv import load_dotenv

from .jev import (
    JevAction,
    JevConfigurationError,
    JevController,
    WeaponAction,
    api_key_available,
)

Vec = pygame.Vector2

WIDTH, HEIGHT = 480, 270
JEV_RAIL_WIDTH = 220
JEV_WIDTH = WIDTH + JEV_RAIL_WIDTH
ARENA = pygame.Rect(12, 12, WIDTH - 24, HEIGHT - 24)
REACTOR_POS = Vec(WIDTH / 2, HEIGHT / 2)
REACTOR_RADIUS = 22
FPS = 60
JEV_TIME_SCALE = 0.75
MAGAZINE_SIZE = 9
RELOAD_SECONDS = 2.0
AUDIO_GAIN = 0.70

INK = (5, 7, 18)
PANEL = (11, 15, 35)
CYAN = (42, 245, 255)
PINK = (255, 48, 156)
VIOLET = (153, 82, 255)
LIME = (151, 255, 87)
GOLD = (255, 211, 76)
WHITE = (225, 244, 255)
RED = (255, 67, 84)

JEV_ACTION_LABELS = {
    JevAction.SEEK_TARGET.value: "SEEK TARGET",
    JevAction.ORBIT_CLOCKWISE.value: "ORBIT CW",
    JevAction.ORBIT_COUNTERCLOCKWISE.value: "ORBIT CCW",
    JevAction.RETREAT.value: "RETREAT",
    JevAction.BANK_LEFT.value: "BANK LEFT",
    JevAction.BANK_RIGHT.value: "BANK RIGHT",
    JevAction.INTERCEPT_THREAT.value: "INTERCEPT",
    JevAction.DASH_SAFE.value: "DASH SAFE",
}

JEV_WEAPON_LABELS = {
    WeaponAction.FIRE.value: "FIRE NOW",
    WeaponAction.HOLD.value: "HOLD FIRE",
    WeaponAction.RELOAD.value: "RELOAD",
}


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def safe_normalize(vector: Vec, fallback: Vec | None = None) -> Vec:
    if vector.length_squared() > 0.0001:
        return vector.normalize()
    return Vec(fallback or (1, 0))


def reflect_velocity(velocity: Vec, normal: Vec) -> Vec:
    """Reflect a velocity around a unit surface normal."""
    normal = safe_normalize(normal)
    return velocity - 2 * velocity.dot(normal) * normal


def direction_name(vector: Vec) -> str:
    if vector.length_squared() < 0.001:
        return "here"
    directions = (
        "east",
        "southeast",
        "south",
        "southwest",
        "west",
        "northwest",
        "north",
        "northeast",
    )
    index = round((math.degrees(math.atan2(vector.y, vector.x)) % 360) / 45) % 8
    return directions[index]


def distance_band(distance: float) -> str:
    if distance < 32:
        return "immediate"
    if distance < 70:
        return "near"
    if distance < 135:
        return "medium"
    return "far"


def compact_number(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def polygon_points(center: Vec, radius: float, sides: int, rotation: float = 0) -> list[Vec]:
    return [
        center
        + Vec(math.cos(rotation + i * math.tau / sides), math.sin(rotation + i * math.tau / sides))
        * radius
        for i in range(sides)
    ]


@dataclass
class Particle:
    pos: Vec
    vel: Vec
    color: tuple[int, int, int]
    life: float
    size: float
    drag: float = 0.94

    def update(self, dt: float) -> bool:
        self.pos += self.vel * dt
        self.vel *= self.drag ** (dt * 60)
        self.life -= dt
        return self.life > 0

    def draw(self, surface: pygame.Surface) -> None:
        radius = max(1, int(self.size * min(1, self.life * 4)))
        pygame.draw.circle(surface, self.color, self.pos, radius)


@dataclass
class Floater:
    text: str
    pos: Vec
    color: tuple[int, int, int]
    life: float = 0.75

    def update(self, dt: float) -> bool:
        self.pos.y -= 20 * dt
        self.life -= dt
        return self.life > 0


@dataclass
class Bullet:
    pos: Vec
    vel: Vec
    friendly: bool
    bounces: int = 0
    charged: bool = False
    age: float = 0
    reactor_cooldown: float = 0

    @property
    def radius(self) -> int:
        return 3 if self.charged else 2


@dataclass
class Enemy:
    pos: Vec
    kind: str
    hp: int
    vel: Vec
    cooldown: float
    phase: float
    flash: float = 0

    @property
    def radius(self) -> int:
        return {"rammer": 8, "gunner": 10, "orbiter": 11}[self.kind]


class SoundBank:
    def __init__(self) -> None:
        self.enabled = pygame.mixer.get_init() is not None
        self.muted = False
        self.sounds: dict[str, pygame.mixer.Sound] = {}
        self.last_played: dict[str, int] = {}
        if not self.enabled:
            return
        for name, frequency, duration, volume, fall in (
            ("shoot", 680, 0.065, 0.10, True),
            ("hit", 170, 0.07, 0.12, True),
            ("dash", 130, 0.14, 0.16, False),
            ("hurt", 90, 0.22, 0.22, True),
        ):
            self.sounds[name] = self._tone(frequency, duration, volume, fall)

        # Player-authored ricochets and kills climb a pentatonic scale. The notes
        # communicate a growing combo without adding another visual meter.
        for index, frequency in enumerate((330, 392, 494, 587, 659)):
            self.sounds[f"bounce_{index}"] = self._tone(frequency, 0.052, 0.045, False)
        for index, frequency in enumerate((196, 247, 294, 392, 494)):
            self.sounds[f"kill_{index}"] = self._tone(frequency, 0.13, 0.115, False)
        self.sounds["overdrive"] = self._chord((262, 330, 392, 523), 0.42, 0.17)

    @staticmethod
    def _tone(frequency: int, duration: float, volume: float, fall: bool) -> pygame.mixer.Sound:
        sample_rate, _, channels = pygame.mixer.get_init()
        count = int(sample_rate * duration)
        samples = array("h")
        phase = 0.0
        for i in range(count):
            progress = i / max(1, count - 1)
            freq = frequency * (1 - 0.38 * progress) if fall else frequency
            phase += math.tau * freq / sample_rate
            attack = min(1.0, i / max(1, sample_rate * 0.006))
            envelope = attack * (1 - progress) ** 1.8
            wave = math.sin(phase) + math.sin(phase * 2) * 0.12
            value = int(32767 * volume * AUDIO_GAIN * envelope * wave)
            for _ in range(channels):
                samples.append(value)
        return pygame.mixer.Sound(buffer=samples)

    @staticmethod
    def _chord(frequencies: tuple[int, ...], duration: float, volume: float) -> pygame.mixer.Sound:
        sample_rate, _, channels = pygame.mixer.get_init()
        count = int(sample_rate * duration)
        samples = array("h")
        phases = [0.0 for _ in frequencies]
        for i in range(count):
            progress = i / max(1, count - 1)
            attack = min(1.0, i / max(1, sample_rate * 0.012))
            envelope = attack * (1 - progress) ** 1.35
            wave = 0.0
            for index, frequency in enumerate(frequencies):
                phases[index] += math.tau * frequency / sample_rate
                wave += math.sin(phases[index])
            value = int(32767 * volume * AUDIO_GAIN * envelope * wave / len(frequencies))
            for _ in range(channels):
                samples.append(value)
        return pygame.mixer.Sound(buffer=samples)

    def play(self, name: str, step: int = 0) -> None:
        if not self.enabled or self.muted:
            return
        now = pygame.time.get_ticks()
        if name == "bounce" and now - self.last_played.get(name, -1000) < 55:
            return
        self.last_played[name] = now
        variant = f"{name}_{min(4, max(0, step))}"
        sound = self.sounds.get(variant) or self.sounds.get(name)
        if sound is not None:
            sound.play()


class Game:
    def __init__(self, *, seed: int | None = None) -> None:
        if seed is not None:
            random.seed(seed)
        pygame.init()
        try:
            if pygame.mixer.get_init() is None:
                pygame.mixer.init(frequency=22050, size=-16, channels=1, buffer=256)
        except pygame.error:
            pass

        self.windowed_size = (WIDTH * 3, HEIGHT * 3)
        self.screen = pygame.display.set_mode(self.windowed_size, pygame.RESIZABLE)
        pygame.display.set_caption("JEV REFLEX")
        self.canvas = pygame.Surface((WIDTH, HEIGHT))
        self.glow = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        self.clock = pygame.time.Clock()
        self.font_small = pygame.font.Font(None, 16)
        self.font = pygame.font.Font(None, 22)
        self.font_big = pygame.font.Font(None, 52)
        self.font_huge = pygame.font.Font(None, 72)
        self.sound = SoundBank()
        self.fullscreen = False
        self.running = True
        self.mode = "title"
        self.pilot_mode = "human"
        self.menu_selection = 0
        self.menu_notice = ""
        self.jev_controller: JevController | None = None
        self.run_id = 0
        self.best_score = 0
        self.viewport = pygame.Rect(0, 0, *self.screen.get_size())
        self.stars = [
            (random.randrange(WIDTH), random.randrange(HEIGHT), random.choice((1, 1, 1, 2)))
            for _ in range(75)
        ]
        self.reset()

    @property
    def render_width(self) -> int:
        return JEV_WIDTH if self.pilot_mode == "jev" and self.mode != "title" else WIDTH

    def configure_display(self) -> None:
        width = self.render_width
        self.canvas = pygame.Surface((width, HEIGHT))
        self.glow = pygame.Surface((width, HEIGHT), pygame.SRCALPHA)
        scale = 2 if width == JEV_WIDTH else 3
        self.windowed_size = (width * scale, HEIGHT * scale)
        if not self.fullscreen:
            self.screen = pygame.display.set_mode(self.windowed_size, pygame.RESIZABLE)

    def reset(self) -> None:
        self.player = Vec(WIDTH / 2, HEIGHT - 52)
        self.player_vel = Vec()
        self.aim = Vec(0, -1)
        self.health = 5
        self.max_health = 5
        self.invulnerable = 0.0
        self.fire_cooldown = 0.0
        self.ammo = MAGAZINE_SIZE
        self.reload_timer = 0.0
        self.dash_cooldown = 0.0
        self.dash_time = 0.0
        self.bullets: list[Bullet] = []
        self.enemies: list[Enemy] = []
        self.particles: list[Particle] = []
        self.floaters: list[Floater] = []
        self.score = 0
        self.combo = 1
        self.combo_timer = 0.0
        self.charge = 0.0
        self.overdrive = 0.0
        self.wave = 1
        self.wave_timer = 0.8
        self.wave_banner = 1.8
        self.spawn_left = self.wave_budget()
        self.spawn_cooldown = 0.5
        self.shake = 0.0
        self.flash = 0.0
        self.elapsed = 0.0
        self.jev_action = JevAction.SEEK_TARGET
        self.jev_weapon_action = WeaponAction.HOLD
        self.jev_last_score = 0
        self.jev_last_health = self.health
        self.jev_fallback = False
        self.jev_display_move_probabilities: dict[str, float] = {}
        self.jev_display_weapon_probabilities: dict[str, float] = {}

    def wave_budget(self) -> int:
        return 4 + self.wave * 2

    def start(self, pilot_mode: str | None = None) -> bool:
        chosen_mode = pilot_mode or ("jev" if self.menu_selection else "human")
        if chosen_mode == "jev" and not api_key_available():
            self.menu_notice = "SET TYPESAFE_API_KEY TO START JEV"
            return False
        if self.jev_controller is not None:
            self.jev_controller.close()
            self.jev_controller = None
        self.reset()
        self.run_id += 1
        self.pilot_mode = chosen_mode
        self.mode = "play"
        self.configure_display()
        if chosen_mode == "jev":
            try:
                self.jev_controller = JevController()
            except JevConfigurationError as exc:
                self.menu_notice = str(exc).upper()
                self.mode = "title"
                self.pilot_mode = "human"
                self.configure_display()
                return False
        self.menu_notice = ""
        return True

    def toggle_fullscreen(self) -> None:
        self.fullscreen = not self.fullscreen
        if self.fullscreen:
            self.screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        else:
            self.screen = pygame.display.set_mode(self.windowed_size, pygame.RESIZABLE)

    def mouse_in_canvas(self) -> Vec:
        mx, my = pygame.mouse.get_pos()
        if not self.viewport.width or not self.viewport.height:
            return Vec(WIDTH / 2, HEIGHT / 2)
        x = (mx - self.viewport.x) * self.render_width / self.viewport.width
        y = (my - self.viewport.y) * HEIGHT / self.viewport.height
        return Vec(clamp(x, 0, WIDTH), clamp(y, 0, HEIGHT))

    def handle_event(self, event: pygame.event.Event) -> None:
        if event.type == pygame.QUIT:
            self.running = False
        elif event.type == pygame.KEYDOWN:
            if event.key == pygame.K_f:
                self.toggle_fullscreen()
            elif event.key == pygame.K_m:
                self.sound.muted = not self.sound.muted
            elif self.mode == "title" and event.key in (
                pygame.K_UP,
                pygame.K_DOWN,
                pygame.K_w,
                pygame.K_s,
            ):
                self.menu_selection = 1 - self.menu_selection
                self.menu_notice = ""
            elif event.key in (pygame.K_p, pygame.K_ESCAPE) and self.mode in ("play", "pause"):
                self.mode = "pause" if self.mode == "play" else "play"
            elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER) and self.mode == "title":
                self.start()
            elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER) and self.mode == "gameover":
                self.start(self.pilot_mode)
            elif event.key == pygame.K_SPACE and self.mode == "play" and self.pilot_mode == "human":
                self.try_dash()
            elif event.key == pygame.K_r and self.mode == "play" and self.pilot_mode == "human":
                self.start_reload()
        elif event.type == pygame.MOUSEBUTTONDOWN:
            if event.button == 1 and self.mode == "title":
                mouse = self.mouse_in_canvas()
                if 170 <= mouse.y <= 218:
                    self.menu_selection = 0 if mouse.y < 194 else 1
                self.start()
            elif event.button == 1 and self.mode == "gameover":
                self.start(self.pilot_mode)
            elif event.button == 3 and self.mode == "play" and self.pilot_mode == "human":
                self.try_dash()

    def try_dash(self, direction: Vec | None = None) -> None:
        if self.dash_cooldown > 0:
            return
        if direction is None:
            keys = pygame.key.get_pressed()
            move = Vec(keys[pygame.K_d] - keys[pygame.K_a], keys[pygame.K_s] - keys[pygame.K_w])
            direction = safe_normalize(move, self.aim)
        else:
            direction = safe_normalize(direction, self.aim)
        self.player_vel = direction * 340
        self.dash_time = 0.15
        self.dash_cooldown = 0.85
        self.invulnerable = max(self.invulnerable, 0.2)
        self.burst(self.player, CYAN, 18, 75)
        self.shake = max(self.shake, 4)
        self.sound.play("dash")

    def shoot(self) -> None:
        rate = 0.085 if self.overdrive > 0 else 0.15
        if self.fire_cooldown > 0 or self.reload_timer > 0:
            return
        if self.ammo <= 0:
            self.start_reload()
            return
        angles = (-0.12, 0, 0.12) if self.overdrive > 0 else (0,)
        for angle in angles:
            direction = self.aim.rotate_rad(angle)
            self.bullets.append(Bullet(self.player + direction * 10, direction * 245, True))
        self.player_vel -= self.aim * 8
        self.ammo -= 1
        self.fire_cooldown = rate
        self.burst(self.player + self.aim * 10, CYAN, 4, 28)
        self.sound.play("shoot")
        if self.ammo == 0:
            self.start_reload()

    def start_reload(self) -> None:
        if self.reload_timer > 0 or self.ammo >= MAGAZINE_SIZE:
            return
        self.reload_timer = RELOAD_SECONDS
        self.fire_cooldown = 0.0

    def spawn_enemy(self) -> None:
        side = random.randrange(4)
        if side == 0:
            pos = Vec(random.uniform(25, WIDTH - 25), ARENA.top + 2)
        elif side == 1:
            pos = Vec(ARENA.right - 2, random.uniform(25, HEIGHT - 25))
        elif side == 2:
            pos = Vec(random.uniform(25, WIDTH - 25), ARENA.bottom - 2)
        else:
            pos = Vec(ARENA.left + 2, random.uniform(25, HEIGHT - 25))
        roll = random.random()
        if self.wave >= 4 and roll < 0.2:
            kind, hp = "orbiter", 4
        elif self.wave >= 2 and roll < 0.5:
            kind, hp = "gunner", 2
        else:
            kind, hp = "rammer", 1
        self.enemies.append(
            Enemy(pos, kind, hp, Vec(), random.uniform(0.5, 1.5), random.random() * math.tau)
        )
        self.burst(pos, PINK if kind == "rammer" else VIOLET, 12, 45)

    def burst(
        self,
        pos: Vec,
        color: tuple[int, int, int],
        count: int,
        speed: float,
        *,
        size: float = 2,
    ) -> None:
        for _ in range(count):
            velocity = Vec(1, 0).rotate(random.uniform(0, 360)) * random.uniform(
                speed * 0.25, speed
            )
            self.particles.append(
                Particle(
                    Vec(pos), velocity, color, random.uniform(0.25, 0.65), random.uniform(1, size)
                )
            )

    def update_player(self, dt: float) -> None:
        keys = pygame.key.get_pressed()
        move = Vec(keys[pygame.K_d] - keys[pygame.K_a], keys[pygame.K_s] - keys[pygame.K_w])
        keyboard_aim = Vec(
            keys[pygame.K_RIGHT] - keys[pygame.K_LEFT],
            keys[pygame.K_DOWN] - keys[pygame.K_UP],
        )
        mouse_fire = pygame.mouse.get_pressed()[0]
        if keyboard_aim.length_squared() > 0:
            self.aim = keyboard_aim.normalize()
            wants_fire = True
        else:
            mouse_delta = self.mouse_in_canvas() - self.player
            if mouse_delta.length_squared() > 9:
                self.aim = mouse_delta.normalize()
            wants_fire = mouse_fire

        self.apply_player_controls(dt, move, wants_fire)

    def apply_player_controls(self, dt: float, move: Vec, wants_fire: bool) -> None:
        """Apply already-decided controls; shared by the human and Jev pilots."""

        if self.dash_time > 0:
            self.dash_time -= dt
            self.burst(self.player, CYAN, 1, 12, size=3)
        else:
            target = safe_normalize(move) * 150 if move.length_squared() else Vec()
            self.player_vel = self.player_vel.lerp(target, min(1, dt * 17))
        self.player += self.player_vel * dt
        self.player.x = clamp(self.player.x, ARENA.left + 8, ARENA.right - 8)
        self.player.y = clamp(self.player.y, ARENA.top + 8, ARENA.bottom - 8)
        if wants_fire:
            self.shoot()

    def nearest_enemy(self) -> Enemy | None:
        return min(
            self.enemies,
            key=lambda enemy: enemy.pos.distance_squared_to(self.player),
            default=None,
        )

    def nearest_hostile_bullet(self) -> Bullet | None:
        return min(
            (bullet for bullet in self.bullets if not bullet.friendly),
            key=lambda bullet: bullet.pos.distance_squared_to(self.player),
            default=None,
        )

    def safest_direction(self) -> Vec:
        candidates = [Vec(1, 0).rotate(index * 45) for index in range(8)]
        hostile = [bullet for bullet in self.bullets if not bullet.friendly]

        def safety(direction: Vec) -> float:
            point = self.player + direction * 52
            point.x = clamp(point.x, ARENA.left + 8, ARENA.right - 8)
            point.y = clamp(point.y, ARENA.top + 8, ARENA.bottom - 8)
            enemy_clearance = min(
                (point.distance_to(enemy.pos) for enemy in self.enemies), default=160
            )
            bullet_clearance = min(
                (point.distance_to(bullet.pos + bullet.vel * 0.35) for bullet in hostile),
                default=160,
            )
            wall_clearance = min(
                point.x - ARENA.left,
                ARENA.right - point.x,
                point.y - ARENA.top,
                ARENA.bottom - point.y,
            )
            return enemy_clearance + bullet_clearance * 1.6 + min(45, wall_clearance)

        return max(candidates, key=safety)

    def legal_jev_actions(self) -> tuple[JevAction, ...]:
        return tuple(JevAction)

    def build_jev_state(self) -> dict[str, Any]:
        enemy = self.nearest_enemy()
        hostile = self.nearest_hostile_bullet()
        safe = self.safest_direction()
        enemy_offset = enemy.pos - self.player if enemy else Vec()
        bullet_offset = hostile.pos - self.player if hostile else Vec()
        enemy_distance = enemy_offset.length() if enemy else 999
        bullet_distance = bullet_offset.length() if hostile else 999
        near_wall = (
            min(
                self.player.x - ARENA.left,
                ARENA.right - self.player.x,
                self.player.y - ARENA.top,
                ARENA.bottom - self.player.y,
            )
            < 28
        )
        health_state = "safe" if self.health >= 4 else "damaged" if self.health >= 2 else "critical"
        score_gain = self.score - self.jev_last_score
        health_lost = max(0, self.jev_last_health - self.health)
        if self.ammo == 0:
            ammo_state = "empty"
        elif self.ammo <= 3:
            ammo_state = "low"
        elif self.ammo == MAGAZINE_SIZE:
            ammo_state = "full"
        else:
            ammo_state = "ready"
        if health_lost:
            result = "took_damage"
        elif score_gain:
            result = "scored_points"
        else:
            result = "no_score_or_damage"
        return {
            "goal": "Survive as long as possible while destroying enemies efficiently.",
            "player": {
                "health": health_state,
                "near_wall": near_wall,
                "moving": direction_name(self.player_vel),
                "dash_ready": self.dash_cooldown <= 0,
                "overdrive": self.overdrive > 0,
            },
            "weapon": {
                "ammo": self.ammo,
                "capacity": MAGAZINE_SIZE,
                "ammo_state": ammo_state,
                "reloading": self.reload_timer > 0,
                "reload_seconds_total": RELOAD_SECONDS,
                "reload_seconds_remaining": round(self.reload_timer, 1),
                "target_available": enemy is not None,
            },
            "threat": {
                "immediate": bullet_distance < 42 or enemy_distance < 30,
                "enemy_count": len(self.enemies),
                "hostile_bullet_count": sum(not bullet.friendly for bullet in self.bullets),
                "nearest_enemy_type": enemy.kind if enemy else "none",
                "nearest_enemy_direction": direction_name(enemy_offset),
                "nearest_enemy_distance": distance_band(enemy_distance),
                "nearest_bullet_direction": direction_name(bullet_offset),
                "nearest_bullet_distance": distance_band(bullet_distance),
            },
            "arena": {
                "safest_direction": direction_name(safe),
                "player_zone": direction_name(self.player - REACTOR_POS),
                "crowding": "high"
                if len(self.enemies) >= 6
                else "medium"
                if len(self.enemies) >= 3
                else "low",
            },
            "recent_control": {
                "action": self.jev_action.value,
                "weapon_action": self.jev_weapon_action.value,
                "result": result,
                "score_gained": score_gain > 0,
                "health_lost": health_lost > 0,
            },
            "episode": {
                "wave": self.wave,
                "combo": self.combo,
                "reactor_charge": "high" if self.charge >= 70 else "building",
            },
        }

    def update_jev_player(self, dt: float) -> None:
        controller = self.jev_controller
        if controller is None:
            self.apply_player_controls(dt, self.safest_direction(), self.reload_timer <= 0)
            return

        decision = controller.poll(run_id=self.run_id)
        if decision is not None:
            self.jev_action = decision.action
            self.jev_weapon_action = decision.weapon_action
            self.jev_fallback = False

        if not controller.pending:
            state = self.build_jev_state()
            if controller.request(state, self.legal_jev_actions(), run_id=self.run_id):
                self.jev_last_score = self.score
                self.jev_last_health = self.health

        telemetry = controller.telemetry
        if telemetry.requests == 0 or telemetry.status == "ERROR":
            self.jev_fallback = True

        enemy = self.nearest_enemy()
        hostile = self.nearest_hostile_bullet()
        safe = self.safest_direction()
        target_offset = enemy.pos - self.player if enemy else Vec(0, -1)
        target_direction = safe_normalize(target_offset, Vec(0, -1))
        action = self.jev_action if not self.jev_fallback else JevAction.RETREAT
        weapon_action = self.jev_weapon_action if not self.jev_fallback else WeaponAction.HOLD

        if action == JevAction.SEEK_TARGET:
            move = (
                target_direction
                if target_offset.length() > 82
                else Vec(-target_direction.y, target_direction.x)
            )
            self.aim = target_direction
        elif action == JevAction.ORBIT_CLOCKWISE:
            move = Vec(-target_direction.y, target_direction.x)
            self.aim = target_direction
        elif action == JevAction.ORBIT_COUNTERCLOCKWISE:
            move = Vec(target_direction.y, -target_direction.x)
            self.aim = target_direction
        elif action == JevAction.RETREAT:
            threat_offset = (hostile.pos - self.player) if hostile else target_offset
            move = -safe_normalize(threat_offset, -target_direction)
            self.aim = target_direction
        elif action in (JevAction.BANK_LEFT, JevAction.BANK_RIGHT):
            if enemy:
                wall_x = ARENA.left if action == JevAction.BANK_LEFT else ARENA.right
                mirrored_target = Vec(2 * wall_x - enemy.pos.x, enemy.pos.y)
                self.aim = safe_normalize(mirrored_target - self.player)
            move = safe
        elif action == JevAction.INTERCEPT_THREAT:
            self.aim = safe_normalize(hostile.pos - self.player) if hostile else target_direction
            move = safe
        else:
            self.aim = target_direction
            move = safe
            self.try_dash(safe)
        if weapon_action == WeaponAction.RELOAD:
            self.start_reload()
        wants_fire = weapon_action == WeaponAction.FIRE
        self.apply_player_controls(dt, move, wants_fire)

    def update_bullets(self, dt: float) -> None:
        survivors: list[Bullet] = []
        for bullet in self.bullets:
            bullet.age += dt
            bullet.reactor_cooldown = max(0, bullet.reactor_cooldown - dt)
            bullet.pos += bullet.vel * dt
            bounced = False
            if bullet.pos.x < ARENA.left + bullet.radius:
                bullet.pos.x = ARENA.left + bullet.radius
                bullet.vel.x = abs(bullet.vel.x)
                bounced = True
            elif bullet.pos.x > ARENA.right - bullet.radius:
                bullet.pos.x = ARENA.right - bullet.radius
                bullet.vel.x = -abs(bullet.vel.x)
                bounced = True
            if bullet.pos.y < ARENA.top + bullet.radius:
                bullet.pos.y = ARENA.top + bullet.radius
                bullet.vel.y = abs(bullet.vel.y)
                bounced = True
            elif bullet.pos.y > ARENA.bottom - bullet.radius:
                bullet.pos.y = ARENA.bottom - bullet.radius
                bullet.vel.y = -abs(bullet.vel.y)
                bounced = True
            if bounced:
                bullet.bounces += 1
                if bullet.friendly:
                    bullet.charged = True
                self.burst(bullet.pos, GOLD if bullet.friendly else PINK, 5, 35)
                if bullet.friendly:
                    self.sound.play("bounce", bullet.bounces - 1)

            offset = bullet.pos - REACTOR_POS
            if offset.length() < REACTOR_RADIUS + bullet.radius and bullet.reactor_cooldown <= 0:
                normal = safe_normalize(offset, -bullet.vel)
                bullet.pos = REACTOR_POS + normal * (REACTOR_RADIUS + bullet.radius + 1)
                bullet.vel = reflect_velocity(bullet.vel, normal) * 1.08
                bullet.bounces += 1
                bullet.charged = True
                bullet.reactor_cooldown = 0.16
                self.burst(bullet.pos, LIME, 7, 50)
                self.shake = max(self.shake, 1.5)
                if bullet.friendly:
                    self.sound.play("bounce", bullet.bounces - 1)

            max_bounces = 7 if bullet.friendly else 2
            max_age = 5.5 if bullet.friendly else 4.0
            if bullet.bounces <= max_bounces and bullet.age <= max_age:
                survivors.append(bullet)
        self.bullets = survivors

    def update_enemies(self, dt: float) -> None:
        for enemy in self.enemies:
            enemy.cooldown -= dt
            enemy.flash = max(0, enemy.flash - dt)
            enemy.phase += dt
            to_player = self.player - enemy.pos
            distance = max(1, to_player.length())
            direction = to_player / distance
            if enemy.kind == "rammer":
                target_vel = direction * (48 + min(32, self.wave * 2.5))
            elif enemy.kind == "gunner":
                tangent = Vec(-direction.y, direction.x) * math.sin(enemy.phase * 1.7)
                approach = direction * clamp((distance - 100) / 30, -1, 1)
                target_vel = (approach + tangent * 0.75) * 42
                if enemy.cooldown <= 0 and distance < 220:
                    aim = safe_normalize(self.player + self.player_vel * 0.2 - enemy.pos)
                    self.bullets.append(Bullet(enemy.pos + aim * 11, aim * 92, False))
                    enemy.cooldown = max(0.65, 1.5 - self.wave * 0.035)
                    self.burst(enemy.pos, VIOLET, 4, 25)
            else:
                radial = safe_normalize(enemy.pos - REACTOR_POS)
                tangent = Vec(-radial.y, radial.x)
                desired_radius = 65
                correction = radial * clamp(
                    (desired_radius - (enemy.pos - REACTOR_POS).length()) / 18, -1, 1
                )
                target_vel = tangent * 52 + correction * 36
                if enemy.cooldown <= 0:
                    aim = safe_normalize(self.player - enemy.pos)
                    for angle in (-13, 0, 13):
                        self.bullets.append(Bullet(enemy.pos, aim.rotate(angle) * 82, False))
                    enemy.cooldown = max(1.0, 2.0 - self.wave * 0.03)
                    self.burst(enemy.pos, GOLD, 5, 30)
            enemy.vel = enemy.vel.lerp(target_vel, min(1, dt * 4))
            enemy.pos += enemy.vel * dt
            enemy.pos.x = clamp(enemy.pos.x, ARENA.left + enemy.radius, ARENA.right - enemy.radius)
            enemy.pos.y = clamp(enemy.pos.y, ARENA.top + enemy.radius, ARENA.bottom - enemy.radius)

    def resolve_collisions(self) -> None:
        dead_bullets: set[int] = set()
        dead_enemies: set[int] = set()

        friendly = [(i, bullet) for i, bullet in enumerate(self.bullets) if bullet.friendly]
        hostile = [(i, bullet) for i, bullet in enumerate(self.bullets) if not bullet.friendly]
        for friendly_index, player_bullet in friendly:
            for hostile_index, enemy_bullet in hostile:
                radii = player_bullet.radius + enemy_bullet.radius + 1
                if player_bullet.pos.distance_squared_to(enemy_bullet.pos) < radii * radii:
                    dead_bullets.update((friendly_index, hostile_index))
                    points = 15 * self.combo
                    self.score += points
                    self.charge = min(100, self.charge + 4)
                    self.floaters.append(Floater(f"INTERCEPT +{points}", player_bullet.pos, LIME))
                    self.burst(player_bullet.pos, LIME, 12, 65)
                    break

        for bullet_index, bullet in friendly:
            if bullet_index in dead_bullets:
                continue
            for enemy_index, enemy in enumerate(self.enemies):
                if enemy_index in dead_enemies:
                    continue
                radii = bullet.radius + enemy.radius
                if bullet.pos.distance_squared_to(enemy.pos) < radii * radii:
                    dead_bullets.add(bullet_index)
                    damage = 2 if bullet.charged else 1
                    enemy.hp -= damage
                    enemy.flash = 0.09
                    self.burst(bullet.pos, GOLD if bullet.charged else CYAN, 9, 55)
                    self.sound.play("hit")
                    if enemy.hp <= 0:
                        dead_enemies.add(enemy_index)
                        self.kill_enemy(enemy, bullet)
                    break

        if self.invulnerable <= 0:
            for bullet_index, bullet in hostile:
                radii = bullet.radius + 7
                if bullet.pos.distance_squared_to(self.player) < radii * radii:
                    dead_bullets.add(bullet_index)
                    self.hurt_player()
                    break
            if self.invulnerable <= 0:
                for enemy_index, enemy in enumerate(self.enemies):
                    radii = enemy.radius + 7
                    if enemy.pos.distance_squared_to(self.player) < radii * radii:
                        if self.dash_time > 0:
                            dead_enemies.add(enemy_index)
                            self.kill_enemy(enemy, None, dash=True)
                        else:
                            self.hurt_player()
                        break

        self.bullets = [b for i, b in enumerate(self.bullets) if i not in dead_bullets]
        self.enemies = [e for i, e in enumerate(self.enemies) if i not in dead_enemies]

    def kill_enemy(self, enemy: Enemy, bullet: Bullet | None, *, dash: bool = False) -> None:
        stylish = dash or (bullet is not None and bullet.charged)
        self.combo = min(9, self.combo + (1 if stylish else 0))
        self.combo_timer = 2.4
        base = {"rammer": 100, "gunner": 180, "orbiter": 260}[enemy.kind]
        points = base * self.combo
        self.score += points
        gain = 18 if stylish else 8
        self.charge = min(100, self.charge + gain)
        label = f"BANK x{self.combo} +{points}" if stylish else f"+{points}"
        self.floaters.append(Floater(label, Vec(enemy.pos), GOLD if stylish else WHITE))
        self.burst(enemy.pos, PINK, 24 if stylish else 16, 95, size=3)
        self.shake = max(self.shake, 5 if stylish else 3)
        self.sound.play("kill", self.combo - 1)

    def hurt_player(self) -> None:
        self.health -= 1
        self.invulnerable = 1.25
        self.combo = 1
        self.combo_timer = 0
        self.player_vel = safe_normalize(self.player - REACTOR_POS) * 115
        self.burst(self.player, RED, 28, 110, size=3)
        self.shake = 9
        self.flash = 0.18
        self.sound.play("hurt")
        if self.health <= 0:
            self.best_score = max(self.best_score, self.score)
            self.mode = "gameover"

    def update_wave(self, dt: float) -> None:
        if self.spawn_left > 0:
            self.spawn_cooldown -= dt
            cap = min(4 + self.wave // 2, 10)
            if self.spawn_cooldown <= 0 and len(self.enemies) < cap:
                self.spawn_enemy()
                self.spawn_left -= 1
                self.spawn_cooldown = max(0.22, 0.62 - self.wave * 0.025)
        elif not self.enemies:
            self.wave_timer -= dt
            if self.wave_timer <= 0:
                self.wave += 1
                self.spawn_left = self.wave_budget()
                self.spawn_cooldown = 0.35
                self.wave_timer = 1.0
                self.wave_banner = 1.5
                if self.wave % 3 == 0 and self.health < self.max_health:
                    self.health += 1
                    self.floaters.append(Floater("REACTOR REPAIR +1", REACTOR_POS, LIME, 1.2))

    def update(self, dt: float) -> None:
        if self.mode != "play":
            self.particles = [particle for particle in self.particles if particle.update(dt)]
            self.floaters = [floater for floater in self.floaters if floater.update(dt)]
            return
        if self.pilot_mode == "jev":
            dt *= JEV_TIME_SCALE
        self.elapsed += dt
        self.fire_cooldown = max(0, self.fire_cooldown - dt)
        if self.reload_timer > 0:
            self.reload_timer = max(0, self.reload_timer - dt)
            if self.reload_timer == 0:
                self.ammo = MAGAZINE_SIZE
                self.floaters.append(Floater("RELOADED", Vec(self.player), LIME, 0.55))
        self.dash_cooldown = max(0, self.dash_cooldown - dt)
        self.invulnerable = max(0, self.invulnerable - dt)
        self.overdrive = max(0, self.overdrive - dt)
        self.wave_banner = max(0, self.wave_banner - dt)
        self.shake = max(0, self.shake - dt * 18)
        self.flash = max(0, self.flash - dt)
        if self.combo_timer > 0:
            self.combo_timer -= dt
            if self.combo_timer <= 0:
                self.combo = 1
        if self.charge >= 100 and self.overdrive <= 0:
            self.charge = 0
            self.overdrive = 6.0
            self.invulnerable = max(self.invulnerable, 0.5)
            self.floaters.append(Floater("OVERDRIVE", self.player - Vec(0, 18), LIME, 1.1))
            self.burst(self.player, LIME, 40, 140, size=4)
            self.shake = 8
            self.sound.play("overdrive")

        if self.pilot_mode == "jev":
            self.update_jev_player(dt)
        else:
            self.update_player(dt)
        self.update_bullets(dt)
        self.update_enemies(dt)
        self.resolve_collisions()
        self.update_wave(dt)
        self.particles = [particle for particle in self.particles if particle.update(dt)]
        self.floaters = [floater for floater in self.floaters if floater.update(dt)]

    def draw_background(self) -> None:
        self.canvas.fill(INK)
        pulse = (math.sin(self.elapsed * 2) + 1) * 0.5
        for x, y, size in self.stars:
            color = (22 + size * 14, 32 + size * 16, 62 + size * 18)
            self.canvas.fill(color, (x, y, size, size))
        if self.mode != "title":
            grid_color = (10, 21 + int(pulse * 3), 39)
            for x in range(ARENA.left, ARENA.right, 48):
                pygame.draw.line(self.canvas, grid_color, (x, ARENA.top), (x, ARENA.bottom))
            for y in range(ARENA.top, ARENA.bottom, 48):
                pygame.draw.line(self.canvas, grid_color, (ARENA.left, y), (ARENA.right, y))
        pygame.draw.rect(self.canvas, (18, 45, 67), ARENA, 1)
        for corner in (ARENA.topleft, ARENA.topright, ARENA.bottomleft, ARENA.bottomright):
            pygame.draw.circle(self.canvas, CYAN, corner, 2)

    def draw_reactor(self) -> None:
        pulse = math.sin(self.elapsed * 5) * 2
        pygame.draw.circle(self.glow, (*LIME, 24), REACTOR_POS, int(35 + pulse))
        pygame.draw.circle(self.canvas, (14, 40, 50), REACTOR_POS, REACTOR_RADIUS)
        pygame.draw.circle(self.canvas, LIME, REACTOR_POS, int(REACTOR_RADIUS + pulse), 2)
        rotation = self.elapsed * 0.9
        for index in range(3):
            angle = rotation + index * math.tau / 3
            point = REACTOR_POS + Vec(math.cos(angle), math.sin(angle)) * 14
            pygame.draw.line(self.canvas, CYAN, REACTOR_POS, point, 2)
        pygame.draw.circle(self.canvas, WHITE, REACTOR_POS, 4)

    def draw_bullet(self, bullet: Bullet) -> None:
        color = GOLD if bullet.charged else (CYAN if bullet.friendly else PINK)
        tail = bullet.pos - safe_normalize(bullet.vel) * (8 if bullet.charged else 5)
        pygame.draw.line(self.canvas, color, tail, bullet.pos, 2)
        pygame.draw.circle(
            self.canvas, WHITE if bullet.charged else color, bullet.pos, bullet.radius
        )
        pygame.draw.circle(self.glow, (*color, 50), bullet.pos, 7 if bullet.charged else 5)

    def draw_enemy(self, enemy: Enemy) -> None:
        color = (
            WHITE
            if enemy.flash > 0
            else {"rammer": PINK, "gunner": VIOLET, "orbiter": GOLD}[enemy.kind]
        )
        angle = math.atan2(enemy.vel.y, enemy.vel.x)
        if enemy.kind == "rammer":
            points = polygon_points(enemy.pos, 9, 3, angle)
            pygame.draw.polygon(self.canvas, color, points, 2)
        elif enemy.kind == "gunner":
            points = polygon_points(enemy.pos, 10, 4, angle + math.pi / 4)
            pygame.draw.polygon(self.canvas, color, points, 2)
            pygame.draw.circle(self.canvas, color, enemy.pos, 3)
        else:
            points = polygon_points(enemy.pos, 11, 6, enemy.phase)
            pygame.draw.polygon(self.canvas, color, points, 2)
            pygame.draw.circle(self.canvas, PINK, enemy.pos, 3)
        pygame.draw.circle(self.glow, (*color, 35), enemy.pos, enemy.radius + 5)

    def draw_player(self) -> None:
        if self.invulnerable > 0 and int(self.invulnerable * 14) % 2 == 0:
            return
        side = Vec(-self.aim.y, self.aim.x)
        nose = self.player + self.aim * 10
        back = self.player - self.aim * 7
        points = [nose, back + side * 6, self.player - self.aim * 3, back - side * 6]
        color = LIME if self.overdrive > 0 else CYAN
        pygame.draw.polygon(self.canvas, color, points, 2)
        pygame.draw.circle(self.canvas, WHITE, self.player, 2)
        pygame.draw.line(self.canvas, PINK, back, back - self.aim * random.uniform(3, 7), 2)
        pygame.draw.circle(self.glow, (*color, 50), self.player, 13)

    def text(
        self,
        value: str,
        pos: tuple[float, float] | Vec,
        color: tuple[int, int, int] = WHITE,
        *,
        font: pygame.font.Font | None = None,
        center: bool = False,
    ) -> None:
        image = (font or self.font).render(value, True, color)
        rect = image.get_rect(center=pos) if center else image.get_rect(topleft=pos)
        self.canvas.blit(image, rect)

    def draw_ammo_meter(self, x: int, y: int, width: int) -> None:
        if self.reload_timer > 0:
            progress = 1 - self.reload_timer / RELOAD_SECONDS
            self.text(f"RELOADING  {self.reload_timer:.1f}s", (x, y), GOLD, font=self.font_small)
            pygame.draw.rect(self.canvas, (25, 35, 49), (x, y + 13, width, 5))
            pygame.draw.rect(self.canvas, GOLD, (x, y + 13, int(width * progress), 5))
            return

        color = CYAN if self.ammo > 3 else PINK
        self.text(f"AMMO  {self.ammo}/{MAGAZINE_SIZE}", (x, y), color, font=self.font_small)
        gap = 2
        pip_width = (width - gap * (MAGAZINE_SIZE - 1)) // MAGAZINE_SIZE
        for index in range(MAGAZINE_SIZE):
            pip_x = x + index * (pip_width + gap)
            pip_color = color if index < self.ammo else (25, 35, 49)
            pygame.draw.rect(self.canvas, pip_color, (pip_x, y + 13, pip_width, 5))

    @staticmethod
    def ease_probabilities(
        shown: dict[str, float], target: dict[str, float], amount: float = 0.16
    ) -> None:
        for name in shown.keys() | target.keys():
            current = shown.get(name, 0.0)
            shown[name] = current + (target.get(name, 0.0) - current) * amount
        for name in [name for name, value in shown.items() if value < 0.001 and name not in target]:
            del shown[name]

    def draw_probability_rows(
        self,
        *,
        title: str,
        probabilities: dict[str, float],
        selected: str,
        y: int,
        rows: int,
        color: tuple[int, int, int],
        labels: dict[str, str] | None = None,
    ) -> None:
        x = WIDTH + 13
        right = WIDTH + JEV_RAIL_WIDTH - 13
        self.text(title, (x, y), (91, 115, 139), font=self.font_small)
        ranked = sorted(probabilities.items(), key=lambda item: item[1], reverse=True)[:rows]
        if not ranked:
            self.text("AWAITING RESPONSE", (x, y + 15), (125, 151, 177), font=self.font_small)
            return
        for index, (name, value) in enumerate(ranked):
            row_y = y + 15 + index * 14
            label = (labels or {}).get(name, name.replace("_", " ").upper())[:13]
            row_color = color if name == selected else (125, 151, 177)
            label_x = x + 9
            if name == selected:
                pygame.draw.polygon(
                    self.canvas,
                    color,
                    [(x, row_y + 2), (x, row_y + 10), (x + 6, row_y + 6)],
                )
            self.text(label, (label_x, row_y), row_color, font=self.font_small)
            pygame.draw.rect(self.canvas, (24, 35, 53), (x + 96, row_y + 3, 65, 5))
            pygame.draw.rect(self.canvas, row_color, (x + 96, row_y + 3, int(65 * value), 5))
            self.text(f"{value * 100:02.0f}%", (right - 23, row_y), WHITE, font=self.font_small)

    def draw_hud(self) -> None:
        self.text(f"SCORE {self.score:07d}", (19, 17), CYAN, font=self.font_small)
        self.text(
            f"WAVE {self.wave:02d}", (WIDTH / 2, 22), WHITE, font=self.font_small, center=True
        )
        if self.pilot_mode == "human":
            self.text(
                f"BEST {self.best_score:07d}", (WIDTH - 110, 17), VIOLET, font=self.font_small
            )
        self.text("HULL", (19, 34), (105, 133, 158), font=self.font_small)
        for index in range(self.max_health):
            color = PINK if index < self.health else (47, 25, 51)
            x = 55 + index * 11
            pygame.draw.polygon(
                self.canvas, color, [(x, 37), (x + 4, 33), (x + 8, 37), (x + 4, 42)]
            )
        if self.pilot_mode == "human":
            bar = pygame.Rect(WIDTH - 112, 34, 94, 7)
            pygame.draw.rect(self.canvas, (25, 35, 49), bar)
            amount = self.overdrive / 6 if self.overdrive > 0 else self.charge / 100
            fill_color = LIME if self.overdrive > 0 else GOLD
            pygame.draw.rect(self.canvas, fill_color, (bar.x, bar.y, int(bar.w * amount), bar.h))
            label = "OVERDRIVE" if self.overdrive > 0 else "REACTOR"
            self.text(label, (bar.x, bar.y - 10), fill_color, font=self.font_small)
        if self.combo > 1:
            self.text(
                f"x{self.combo} BANK COMBO",
                (WIDTH / 2, HEIGHT - 19),
                GOLD,
                font=self.font,
                center=True,
            )
        dash_ready = "DASH READY" if self.dash_cooldown <= 0 else f"DASH {self.dash_cooldown:.1f}"
        self.text(
            dash_ready,
            (18, HEIGHT - 25),
            CYAN if self.dash_cooldown <= 0 else (75, 91, 115),
            font=self.font_small,
        )
        if self.pilot_mode == "human":
            self.draw_ammo_meter(WIDTH - 112, HEIGHT - 31, 94)
        else:
            self.draw_ammo_meter(WIDTH - 112, 18, 94)

    def draw_title(self) -> None:
        # A deliberately sparse cabinet-style attract screen.
        pygame.draw.line(self.canvas, CYAN, (68, 35), (174, 35), 2)
        pygame.draw.line(self.canvas, PINK, (306, 35), (412, 35), 2)
        pygame.draw.circle(self.canvas, GOLD, (240, 35), 3)
        pygame.draw.circle(self.canvas, LIME, (240, 35), 8, 1)
        self.text("JEV", (WIDTH / 2, 63), CYAN, font=self.font_huge, center=True)
        self.text("REFLEX", (WIDTH / 2, 103), PINK, font=self.font_huge, center=True)
        self.text("THINK FAST. MOVE FASTER.", (WIDTH / 2, 130), GOLD, font=self.font, center=True)

        pygame.draw.rect(self.canvas, PANEL, (70, 146, WIDTH - 140, 82))
        pygame.draw.line(self.canvas, (31, 69, 94), (70, 146), (410, 146))
        pygame.draw.line(self.canvas, (31, 69, 94), (70, 228), (410, 228))
        pygame.draw.rect(self.canvas, CYAN, (70, 146, 3, 12))
        pygame.draw.rect(self.canvas, PINK, (407, 216, 3, 12))
        self.text(
            "CHOOSE PILOT", (WIDTH / 2, 158), (118, 143, 170), font=self.font_small, center=True
        )
        options = (
            ("HUMAN PILOT", "YOU FLY", CYAN),
            ("JEV PILOT", "FAST TYPED DECISIONS", LIME),
        )
        for index, (label, detail, color) in enumerate(options):
            y = 179 + index * 25
            selected = index == self.menu_selection
            if selected:
                pygame.draw.rect(self.canvas, (18, 30, 51), (83, y - 10, 314, 21))
                self.text(">", (91, y), color, font=self.font, center=True)
            self.text(label, (111, y - 8), color if selected else (112, 133, 157), font=self.font)
            self.text(
                detail,
                (280, y),
                WHITE if selected else (86, 103, 124),
                font=self.font_small,
                center=True,
            )
        if self.menu_selection == 1:
            key_status = "API KEY READY" if api_key_available() else "TYPESAFE_API_KEY REQUIRED"
            self.text(
                self.menu_notice or key_status,
                (WIDTH / 2, 225),
                LIME if api_key_available() and not self.menu_notice else GOLD,
                font=self.font_small,
                center=True,
            )
        blink = CYAN if int(pygame.time.get_ticks() / 450) % 2 else WHITE
        self.text("ENTER TO START", (WIDTH / 2, 246), blink, font=self.font, center=True)

    def draw_jev_panel(self) -> None:
        controller = self.jev_controller
        if controller is None:
            return
        telemetry = controller.telemetry
        x = WIDTH
        right = x + JEV_RAIL_WIDTH - 13
        self.canvas.fill((7, 10, 22), (x, 0, JEV_RAIL_WIDTH, HEIGHT))
        pygame.draw.line(self.canvas, (31, 66, 82), (x, 0), (x, HEIGHT), 1)
        pygame.draw.line(self.canvas, CYAN, (x, 0), (x, 48), 2)

        if telemetry.status == "ERROR":
            status, status_color = "ERROR", RED
        elif self.jev_fallback:
            status, status_color = "FALLBACK", GOLD
        else:
            status, status_color = "LIVE", LIME
        self.text("JEV / SYSTEM ONE", (x + 13, 13), WHITE, font=self.font)
        self.text(status, (right - 27, 18), status_color, font=self.font_small)
        self.text(
            "> CHOSEN  ·  BARS = PROBABILITY", (x + 13, 35), (105, 133, 158), font=self.font_small
        )

        action = telemetry.action.value if telemetry.action else self.jev_action.value
        weapon = (
            telemetry.weapon_action.value
            if telemetry.weapon_action is not None
            else self.jev_weapon_action.value
        )
        if self.jev_fallback:
            self.jev_display_move_probabilities.clear()
            self.jev_display_weapon_probabilities.clear()
        else:
            self.ease_probabilities(
                self.jev_display_move_probabilities, dict(telemetry.probabilities)
            )
            self.ease_probabilities(
                self.jev_display_weapon_probabilities, dict(telemetry.weapon_probabilities)
            )

        self.draw_probability_rows(
            title="NEXT MANEUVER",
            probabilities=self.jev_display_move_probabilities,
            selected=action,
            y=55,
            rows=4,
            color=CYAN,
            labels=JEV_ACTION_LABELS,
        )
        self.draw_probability_rows(
            title="FIRE CONTROL",
            probabilities=self.jev_display_weapon_probabilities,
            selected=weapon,
            y=128,
            rows=3,
            color=PINK,
            labels=JEV_WEAPON_LABELS,
        )

        pygame.draw.line(self.canvas, (31, 54, 72), (x + 13, 191), (right, 191))
        self.text("LIVE LOOP  ·  0.75x SIM", (x + 13, 197), (91, 115, 139), font=self.font_small)
        dps = telemetry.decisions_per_second()
        self.text(f"{dps:.1f}/S", (x + 13, 210), LIME, font=self.font_small)
        self.text(f"{telemetry.latency_ms:.0f} MS", (x + 82, 210), GOLD, font=self.font_small)
        self.text(f"{telemetry.requests} CALLS", (x + 155, 210), WHITE, font=self.font_small)

        pygame.draw.line(self.canvas, (31, 54, 72), (x + 13, 226), (right, 226))
        self.text("RUN COST", (x + 13, 232), (91, 115, 139), font=self.font_small)
        self.text(f"${telemetry.total_cost:.6f}", (x + 83, 230), WHITE, font=self.font)
        self.text(
            f"IN {compact_number(telemetry.input_tokens)} / ${telemetry.input_cost:.6f}",
            (x + 13, 247),
            CYAN,
            font=self.font_small,
        )
        self.text(
            f"OUT {compact_number(telemetry.output_tokens)} / ${telemetry.output_cost:.6f}",
            (x + 13, 259),
            VIOLET,
            font=self.font_small,
        )

    def draw_overlay(self) -> None:
        shade = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        shade.fill((3, 5, 15, 195))
        self.canvas.blit(shade, (0, 0))
        if self.mode == "pause":
            self.text("PAUSED", (WIDTH / 2, 108), CYAN, font=self.font_big, center=True)
            self.text("P / ESC TO RESUME", (WIDTH / 2, 147), WHITE, font=self.font, center=True)
        elif self.mode == "gameover":
            self.text("SIGNAL LOST", (WIDTH / 2, 82), PINK, font=self.font_big, center=True)
            self.text(
                f"SCORE  {self.score:07d}", (WIDTH / 2, 128), WHITE, font=self.font, center=True
            )
            self.text(
                f"WAVE   {self.wave:02d}", (WIDTH / 2, 151), GOLD, font=self.font, center=True
            )
            self.text(
                "CLICK OR PRESS ENTER TO REBOOT",
                (WIDTH / 2, 195),
                CYAN,
                font=self.font,
                center=True,
            )

    def draw(self) -> None:
        self.glow.fill((0, 0, 0, 0))
        self.draw_background()
        if self.mode == "title":
            self.draw_title()
        else:
            self.draw_reactor()
            for bullet in self.bullets:
                self.draw_bullet(bullet)
            for enemy in self.enemies:
                self.draw_enemy(enemy)
            for particle in self.particles:
                particle.draw(self.canvas)
            self.draw_player()
            # Standard alpha compositing gives a controlled bloom without turning
            # every sprite into a large flat neon disc.
            self.canvas.blit(self.glow, (0, 0))
            for floater in self.floaters:
                self.text(
                    floater.text, floater.pos, floater.color, font=self.font_small, center=True
                )
            self.draw_hud()
            if self.wave_banner > 0 and self.mode == "play":
                self.text(
                    f"WAVE {self.wave}", (WIDTH / 2, 61), PINK, font=self.font_big, center=True
                )
            if self.mode in ("pause", "gameover"):
                self.draw_overlay()
            if self.pilot_mode == "jev":
                self.draw_jev_panel()
        if self.sound.muted:
            self.text("MUTED", (WIDTH - 53, HEIGHT - 24), (95, 105, 125), font=self.font_small)
        if self.flash > 0:
            overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
            overlay.fill((*RED, int(110 * self.flash / 0.18)))
            self.canvas.blit(overlay, (0, 0))
        screen_width, screen_height = self.screen.get_size()
        scale = min(screen_width / self.render_width, screen_height / HEIGHT)
        out_size = (max(1, int(self.render_width * scale)), max(1, int(HEIGHT * scale)))
        self.viewport = pygame.Rect(
            (screen_width - out_size[0]) // 2,
            (screen_height - out_size[1]) // 2,
            *out_size,
        )
        self.screen.fill((1, 2, 7))
        scaled = pygame.transform.scale(self.canvas, out_size)
        shake = Vec()
        if self.shake > 0 and self.pilot_mode != "jev":
            shake = Vec(
                random.uniform(-self.shake, self.shake), random.uniform(-self.shake, self.shake)
            )
        self.screen.blit(scaled, self.viewport.move(round(shake.x), round(shake.y)))
        pygame.display.flip()

    def run(self) -> None:
        try:
            while self.running:
                dt = min(self.clock.tick(FPS) / 1000, 0.05)
                for event in pygame.event.get():
                    self.handle_event(event)
                self.update(dt)
                self.draw()
        finally:
            if self.jev_controller is not None:
                self.jev_controller.close()
            pygame.quit()


def main() -> None:
    load_dotenv()
    Game().run()


if __name__ == "__main__":
    main()
