# Jev Reflex

Jev Reflex is a small neon arena shooter that demonstrates fast, typed AI decisions in a
practical game. Bullets bounce from walls, bank shots cause extra damage, and the centre reactor
changes bullet direction.

You can play in two ways:

- **Human Pilot** — you control the ship.
- **Jev Pilot** — TypeSafe's Jev model controls the ship while you watch its decisions live.

The game is also a practical demonstration of fast, low-cost AI decision-making. It does not
use Jev to generate dialogue or game assets. Jev repeatedly reads a small description of the
current situation and selects the next tactical action.

## Quick start

This is a uv project. Python packages, the virtual environment, and the uv cache stay inside
this project.

```bash
uv sync --extra dev
uv run jev-reflex
```

Use the up and down keys on the title screen to select a pilot, then press Enter.

## Enable Jev Pilot

Open the project-local `.env` file and add your TypeSafe API key:

```dotenv
TYPESAFE_API_KEY="your-key"
```

The game loads this file automatically. You do not need to put the API key in the launch
command. `.env` is listed in `.gitignore`, so Git will not include the key in a commit. Restart
the game after adding or changing the key.

Start the game normally:

```bash
uv run jev-reflex
```

If the key is missing, Human Pilot remains available and the menu explains why Jev Pilot
cannot start.

## What is Jev?

Jev is TypeSafe's System One model. A System One model makes fast, focused judgments that
software can use directly.

Jev does not return a paragraph telling the game what it might do. The game gives Jev a fixed
list of legal actions using a TypeSafe `Choice` question. Jev returns:

- The selected action
- A probability for every available action
- A confidence value for the complete probability distribution
- The model version and token usage

This typed response is useful in real-time software because the game does not need to parse
generated text.

Learn more in the [TypeSafe System One documentation](https://docs.typesafe.ai/concepts/system-one)
and [Choice documentation](https://docs.typesafe.ai/primitives/choice).

## How Jev controls the ship

The game runs at 60 frames per second. It never waits for a network response inside the frame
loop.

1. Python reads the current positions, velocities, enemies, and bullets.
2. Python calculates exact facts such as the safest direction and whether a collision is close.
3. The game sends Jev a compact JSON state.
4. One request asks Jev two typed `Choice` questions: movement and weapon intent.
5. Jev chooses a movement tactic plus `fire`, `hold`, or `reload`.
6. Python converts those choices into smooth movement, aiming, firing, and dashing.
7. The next request starts after the previous request finishes.

Only one Jev request can be active at a time. The ship continues its current action while the
next decision is being made. If a request fails, a clearly labelled local fallback keeps the
ship moving. The display never claims that a fallback action came from Jev.

The movement question includes actions such as:

- Seek and aim at the nearest enemy
- Orbit clockwise or counterclockwise
- Retreat while keeping aim on the target
- Calculate a bank shot from the left or right wall
- Intercept an enemy bullet
- Dash toward the safest open direction

Jev chooses the tactic. Python owns arithmetic, physics, collision detection, bank-shot
geometry, and control execution. This separation keeps the model input small and the controls
reliable.

Python could move directly toward its calculated safest direction, and the local fallback does
exactly that when Jev is unavailable. In Jev Pilot mode, however, the safe direction is one
fact Jev may use when comparing tactics. Jev still decides whether the situation calls for an
attack, orbit, retreat, interception, bank shot, or safe dash.

The ship has a nine-round magazine and takes two game seconds to reload. The weapon question
asks Jev whether to fire, hold a round, or begin reloading. Movement and weapon intent are sent
as two questions in the same request, so TypeSafe evaluates them together instead of adding a
second network round trip. Additional questions still add a small number of input and output
tokens.

Each `Choice` receives its complete option set. The state says whether a dash is ready, a hostile
bullet exists, the magazine is full, or a reload is already active. This lets Jev assign low
probability to an unsuitable choice without Python silently hiding options from the decision.

### Questions sent in each request

The game asks both questions together:

```python
response = client.system_one(
    state=state,
    questions={
        "movement": movement_question,
        "weapon": weapon_question,
    },
)
```

The **movement** question asks:

> Choose the ship's next short control macro for about 200 milliseconds. Survive first,
> destroy threats second, and use bank shots when they are safe. Trust Python's precomputed
> danger, direction, distance, and safe-direction labels. React immediately to an immediate
> threat and avoid repeating an action when the recent result says it failed.

Its options are `seek_target`, `orbit_clockwise`, `orbit_counterclockwise`, `retreat`,
`bank_left`, `bank_right`, `intercept_threat`, and `dash_safe`.

The **weapon** question asks:

> Choose the weapon commitment for the same next 200 milliseconds. Fire only for a useful
> target or interception, conserve rounds when shot quality is poor, and reload when ammunition
> is low and danger allows it. Use the ammunition and threat state. Do not choose fire while
> reloading or reload when the magazine is full.

Its options are `fire`, `hold`, and `reload`. Jev returns a selected option, probability for
every option, and confidence for each question.

Python owns the exact ammunition count and reload timer. Every request includes the magazine
capacity, remaining rounds, reload status, reload duration, remaining reload time, and whether
a target is available. Jev judges the tactical tradeoff; it does not count frames or control
the timer itself.

> The important Jev judgment is the tradeoff between survival, direct attack, interception, and risky bank-shot scoring

## What Jev receives

Jev does not receive screenshots. The state contains short semantic fields such as:

```json
{
  "player": {
    "health": "damaged",
    "near_wall": false,
    "dash_ready": true
  },
  "weapon": {
    "ammo": 2,
    "capacity": 9,
    "ammo_state": "low",
    "reloading": false,
    "reload_seconds_total": 2.0,
    "reload_seconds_remaining": 0.0,
    "target_available": true
  },
  "threat": {
    "immediate": true,
    "nearest_enemy_type": "gunner",
    "nearest_bullet_direction": "east",
    "nearest_bullet_distance": "near"
  },
  "arena": {
    "safest_direction": "southwest",
    "crowding": "high"
  }
}
```

Python turns raw measurements into named directions, distance bands, and danger facts before
sending the state. Jev therefore decides between tactics instead of doing coordinate maths.

## Reading the Jev display

Jev Pilot adds a dedicated side rail, so telemetry never covers the arena. The simulation runs
at 0.75x speed in this mode to make the rapid decisions easier to observe. Network latency and
decisions per second remain real wall-clock measurements.

- **Move / Weapon** — the two choices returned by the same request.
- **Selected probabilities** — the probability assigned to each selected choice.
- **Alternative bars** — the other actions Jev considered most likely.
- **Confidence** — how concentrated the complete Choice distribution is.
- **Decisions/s** — completed Jev decisions per second over a recent rolling window.
- **Latency** — time taken by the latest request.
- **Calls** — completed Jev requests in this run.
- **IN / OUT** — cumulative input and output tokens.
- **Cost** — running input cost, output cost, and total cost in USD.
- **Ammo** — nine individual round markers, or a timed progress bar during reload.

Probability and confidence are related but different. The selected probability belongs to one
action. Confidence describes how strongly the complete distribution favours one action over
the alternatives.

The integration pins `jev-1.13.0` so a moving model alias cannot silently change gameplay.
Direct TypeSafe pricing currently charges $0.042 per million input tokens. Output tokens are
returned by the API and displayed, but currently cost $0. See the current
[TypeSafe model and pricing page](https://docs.typesafe.ai/models) before using these figures
for another application.

## Run records

Each Jev decision is appended to a JSONL file under `artifacts/`. A record contains:

- The compact state sent to Jev
- Selected action and all probabilities
- Confidence and latency
- Input and output tokens
- Running cost
- Model and request identifiers

`artifacts/` is ignored by Git. API keys are never written to these records.

## Human controls

- `WASD`: move
- Mouse: aim; hold left click to fire
- Arrow keys: keyboard-only twin-stick aim and fire
- `R`: reload early
- `Space` or right click: dash
- `P` or `Esc`: pause
- `M`: mute
- `F`: fullscreen
- `Enter`: start or restart

## Development checks

```bash
uv run ruff format --check src tests
uv run ruff check src tests
uv run pytest -q
```

The game uses generated geometry and sound. It has no downloaded art or audio assets. Jev
Pilot is the only feature that makes network requests.
