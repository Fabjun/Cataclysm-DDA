#!/usr/bin/env python3
"""Debug patcher for the NEMESIS mod.

Patches can be stacked: apply as many --mode flags as needed simultaneously.
All patches write .json.bak backups on first apply and are fully reversible via restore.
Never ship with the patch applied — always restore before committing or distributing.

USAGE
-----
Apply one or more modes (default mode is 'spawn' if none specified):
    python3 nemesis_debug_patch.py apply
    python3 nemesis_debug_patch.py apply --mode spawn --mode hunt --mode state
    python3 nemesis_debug_patch.py apply --mode hunt --nemesis alice_smith_0042

Restore all patched files:
    python3 nemesis_debug_patch.py restore
    python3 nemesis_debug_patch.py restore --verify

MODES
-----
spawn       Remove spawn delay gates and location conditions from every spawn-check EOC;
            remove 1-hour cooldown from EOC_NEMESIS_MASTER; add [NEMESIS DEBUG] messages;
            inject EOC_NEMESIS_DEBUG_GAME_START so one Nemesis spawns the moment the game
            loads (game_begin event), without requiring any OMT crossing.  Subsequent
            spawns still fire on each OMT crossing.
            (This is the default mode applied when no --mode flag is given.)

hunt        Inject NEMESIS_HUNTING + NEMESIS_OVR_DIST (intensity 30) into every
            EOC_NEMESIS_EFFECTS_* so the Nemesis enters active hunt mode at spawn
            without the player needing to attack it first.

ambush      Inject NEMESIS_AMBUSH + NEMESIS_OVR_DIST (intensity 5) into every
            EOC_NEMESIS_EFFECTS_* and remove NEMESIS_HUNTING.  The Nemesis spawns
            already in ambush mode, skipping the active hunt phase entirely.
            Useful for testing the proximity re-trigger and ambush → hunt transition.
            If hunt is also applied, ambush runs after and overrides it.

state       Add EOC_NEMESIS_DEBUG_STATE to nemesis_master.json — a RECURRING (1 min)
            EOC that caches the hunting Nemesis's state (state / OMT distance / OVR_DIST
            / FRUSTRATION) into globals each tick and shows it to the player.
            Shows the state of the last Nemesis processed per tick; works correctly
            when testing a single Nemesis at a time.

dread       Add EOC_NEMESIS_DEBUG_DREAD to nemesis_master.json — a RECURRING (1 min)
            EOC that queues all three dread message tiers every minute while any Nemesis
            is hunting.  Use this to verify all three message texts display correctly
            without needing a specific player PER + survival build.

death-echo  Inject EOC_NEMESIS_DEATH_ECHO_* into each seed file with NO coordinate
            check.  The echo fires once on any OMT crossing after the Nemesis is armed,
            letting you verify the atmospheric message without traveling to the actual
            death location.  (Real-character Nemesis files already have the echo EOC
            with correct coordinates and are not modified by this mode.)

OPTIONS
-------
--nemesis SAFE_NAME     Apply seed-file patches only to the file matching that safe name.
                        Example: --nemesis alice_smith_0042
                        If omitted, all seed files are patched.

--verify (restore only) After restoring, check that no [NEMESIS DEBUG] strings remain in
                        any restored file.  Exits with an error if any are found.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

MOD_DIR    = Path(__file__).parent
GEN_DIR    = MOD_DIR / "generated"
MASTER     = MOD_DIR / "nemesis_master.json"

# OVR_DIST values imported to match nemesis_eoc.py constants exactly.
_OVR_DIST_START   = 30   # initial hunt OVR_DIST (matches nemesis_eoc._OVR_DIST_START)
_AMBUSH_OVR_DIST  = 5    # ambush OVR_DIST (matches nemesis_eoc._AMBUSH_OVR_DIST)


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def _load(path: Path) -> list:
    """Load and return a JSON list from the given file path."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save(path: Path, data: list) -> None:
    """Write a JSON list to the given file path with 2-space indentation."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _backup(path: Path) -> None:
    """Create a .json.bak copy of path if one does not already exist.

    Only creates the backup on the first call, so a repeated apply does not
    overwrite the original with an already-patched version.
    """
    bak = path.with_suffix(".json.bak")
    if not bak.exists():
        shutil.copy2(path, bak)


def _restore_from_bak(path: Path) -> bool:
    """Replace path with its .json.bak backup and delete the backup.

    Returns True if the backup existed and was restored, False if none was found.
    """
    bak = path.with_suffix(".json.bak")
    if bak.exists():
        shutil.copy2(bak, path)
        bak.unlink()
        return True
    return False


def _target_seed_files(nemesis_filter: str | None) -> list[Path]:
    """Return all nemesis_seed_*.json files, optionally filtered by safe name.

    When nemesis_filter is set, only files whose name contains that string are returned.
    """
    seeds = sorted(GEN_DIR.glob("nemesis_*.json"))
    if nemesis_filter:
        seeds = [s for s in seeds if nemesis_filter in s.name]
    return seeds


# ---------------------------------------------------------------------------
# Mode: spawn
# Removes delay conditions and location gates from spawn-check EOCs.
# Removes the 1-hour cooldown from EOC_NEMESIS_MASTER.
# Adds visible [NEMESIS DEBUG] messages so the tester can see the spawn pipeline.
# ---------------------------------------------------------------------------

def _patch_master_spawn(data: list) -> list:
    """Patch EOC_NEMESIS_MASTER: drop the time_since cooldown, add a debug message."""
    for obj in data:
        if obj.get("id") == "EOC_NEMESIS_MASTER":
            obj["condition"] = {"one_in_chance": 1}
            debug_msg = {
                "u_message": (
                    "<color_yellow>[NEMESIS DEBUG] Master EOC fired — running spawn checks.</color>"
                ),
                "type": "neutral",
            }
            obj["effect"] = [debug_msg] + obj.get("effect", [])
    return data


def _patch_spawn_check(obj: dict) -> dict:
    """Patch one EOC_NEMESIS_SPAWN_CHECK_* to fire immediately on any OMT crossing.

    Strips time_since delay and u_is_outside location conditions from the "and" list,
    keeping only the spawned_var == 1 check so the Nemesis can still only spawn once.
    Prepends green/red debug messages to effect and false_effect.
    """
    eid  = obj.get("id", "")
    safe = eid.replace("EOC_NEMESIS_SPAWN_CHECK_", "")

    old_cond   = obj.get("condition", {})
    conditions = old_cond.get("and", [])
    kept = [
        c for c in conditions
        if "u_is_outside" not in json.dumps(c)
        and "time_since"  not in json.dumps(c)
        and "time('now')" not in json.dumps(c)
    ]
    obj["condition"] = {"and": kept} if len(kept) > 1 else (kept[0] if kept else {})

    pass_msg = {
        "u_message": (
            f"<color_green>[NEMESIS DEBUG] {safe}: conditions MET — attempting spawn.</color>"
        ),
        "type": "good",
    }
    obj["effect"] = [pass_msg] + obj.get("effect", [])

    fail_msg = {
        "u_message": (
            f"<color_red>[NEMESIS DEBUG] {safe}: not armed yet — arming now.</color>"
        ),
        "type": "bad",
    }
    obj["false_effect"] = [fail_msg] + obj.get("false_effect", [])

    return obj


def _build_game_start_eoc(hub_id: str) -> dict:
    """Build EOC_NEMESIS_DEBUG_GAME_START — fires on game_begin for immediate spawning.

    Runs the hub twice in sequence so a Nemesis spawns at the moment the game loads,
    without requiring any OMT crossing.  The two-pass design is necessary because the
    spawn check is a two-step state machine: on the first hub pass, every spawn check's
    false_effect fires (spawned_var 0 → 1, char_start recorded).  On the second hub
    pass, the conditions are immediately met (no delay, no location gate — both stripped
    by the spawn patch) and the first eligible Nemesis spawns.  The lock reset between
    the two passes ensures the second pass can produce a new spawn.
    """
    return {
        "type":           "effect_on_condition",
        "id":             "EOC_NEMESIS_DEBUG_GAME_START",
        "eoc_type":       "EVENT",
        "required_event": "game_begin",
        "effect": [
            {
                "u_message": (
                    "<color_yellow>[NEMESIS DEBUG] game_begin — "
                    "forcing first Nemesis spawn now.</color>"
                ),
                "type": "neutral",
            },
            # Pass 1: arm all spawn checks (spawned 0 → 1 via false_effect).
            {"math": ["global_nemesis_spawn_lock = 0"]},
            {"run_eocs": [hub_id]},
            # Pass 2: conditions now met (spawned=1, delay stripped, location stripped).
            # First eligible Nemesis spawns; lock is then set to 1, blocking the rest.
            {"math": ["global_nemesis_spawn_lock = 0"]},
            {"run_eocs": [hub_id]},
        ],
    }


def _apply_game_start(master_data: list) -> None:
    """Inject EOC_NEMESIS_DEBUG_GAME_START into master so one Nemesis spawns at game load.

    Finds the hub EOC ID from the master data (it starts with 'EOC_HUB_') and builds
    a game_begin EVENT EOC that runs the hub twice.  This eliminates the requirement
    to cross an OMT boundary before the first spawn occurs.
    """
    hub_id: str | None = None
    for obj in master_data:
        if isinstance(obj, dict) and obj.get("id", "").startswith("EOC_HUB_"):
            hub_id = obj["id"]
            break
    if hub_id is None:
        print("  [spawn] WARNING: no hub EOC found — game_start injection skipped")
        return
    # Remove any stale version before appending.
    master_data[:] = [o for o in master_data if o.get("id") != "EOC_NEMESIS_DEBUG_GAME_START"]
    master_data.append(_build_game_start_eoc(hub_id))
    print(f"  [spawn] injected EOC_NEMESIS_DEBUG_GAME_START → {hub_id}")


def _apply_spawn(master_data: list, seed_paths: list[Path], seed_data: dict[Path, list]) -> None:
    """Apply spawn-gate removal to master and all targeted seed files."""
    _patch_master_spawn(master_data)
    _apply_game_start(master_data)
    for path in seed_paths:
        data = seed_data[path]
        for obj in data:
            if isinstance(obj, dict) and obj.get("id", "").startswith("EOC_NEMESIS_SPAWN_CHECK_"):
                _patch_spawn_check(obj)
        print(f"  [spawn] patched {path.name}")


# ---------------------------------------------------------------------------
# Mode: hunt
# Appends NEMESIS_HUNTING + OVR_DIST to the NPC-context EFFECTS EOC so the
# Nemesis enters hunt mode immediately at spawn without the player attacking.
# ---------------------------------------------------------------------------

_HUNT_INJECT: list[dict] = [
    # Applied in NPC context (EOC_NEMESIS_EFFECTS runs via u_run_npc_eocs).
    # Mirrors EOC_NEMESIS_REVENGE_TRIGGER's arm sequence.
    {"u_add_effect": "NEMESIS_HUNTING",    "duration": "PERMANENT"},
    {"u_add_effect": "NEMESIS_OVR_DIST",   "duration": "PERMANENT", "intensity": _OVR_DIST_START},
    {"u_lose_effect": "NEMESIS_FRUSTRATION"},
]


def _apply_hunt(seed_paths: list[Path], seed_data: dict[Path, list]) -> None:
    """Append NEMESIS_HUNTING effects to the EFFECTS EOC in each targeted seed file."""
    for path in seed_paths:
        data = seed_data[path]
        for obj in data:
            if isinstance(obj, dict) and obj.get("id", "").startswith("EOC_NEMESIS_EFFECTS_"):
                obj["effect"] = obj.get("effect", []) + _HUNT_INJECT
                print(f"  [hunt] injected NEMESIS_HUNTING into {path.name}")
                break


# ---------------------------------------------------------------------------
# Mode: ambush
# Appends NEMESIS_AMBUSH + OVR_DIST=5 to the EFFECTS EOC; removes HUNTING.
# Overrides hunt mode if both are applied.
# ---------------------------------------------------------------------------

_AMBUSH_INJECT: list[dict] = [
    {"u_lose_effect": "NEMESIS_HUNTING"},
    {"u_lose_effect": "NEMESIS_FRUSTRATION"},
    {"u_add_effect": "NEMESIS_AMBUSH",   "duration": "PERMANENT", "intensity": 1},
    {"u_add_effect": "NEMESIS_OVR_DIST", "duration": "PERMANENT", "intensity": _AMBUSH_OVR_DIST},
]


def _apply_ambush(seed_paths: list[Path], seed_data: dict[Path, list]) -> None:
    """Append NEMESIS_AMBUSH effects to the EFFECTS EOC in each targeted seed file."""
    for path in seed_paths:
        data = seed_data[path]
        for obj in data:
            if isinstance(obj, dict) and obj.get("id", "").startswith("EOC_NEMESIS_EFFECTS_"):
                obj["effect"] = obj.get("effect", []) + _AMBUSH_INJECT
                print(f"  [ambush] injected NEMESIS_AMBUSH into {path.name}")
                break


# ---------------------------------------------------------------------------
# Mode: state
# Adds EOC_NEMESIS_DEBUG_STATE to master: a 1-min RECURRING that caches the
# hunting Nemesis's state into globals (NPC branch) and displays it (avatar branch).
#
# Global vars written by the NPC branch (refreshed each tick):
#   global_debug_nem_state  : 0=PASSIVE, 1=HUNTING, 2=AMBUSH
#   global_debug_nem_ovr    : NEMESIS_OVR_DIST intensity
#   global_debug_nem_frust  : NEMESIS_FRUSTRATION intensity
#   global_debug_nem_dist   : OMT distance to cached player position
#
# Limitation: with multiple active Nemeses, shows the state of the last NPC
# processed per tick.  Reliable when testing a single Nemesis at a time.
# ---------------------------------------------------------------------------

def _build_state_eoc() -> dict:
    """Build EOC_NEMESIS_DEBUG_STATE — 1-min RECURRING state printer."""

    def _msg(text: str, color: str = "yellow") -> dict:
        return {"u_message": f"<color_{color}>[NEM STATE] {text}</color>", "type": "neutral"}

    def _if_else(cond: dict, then_: list, else_: list) -> dict:
        return {"if": cond, "then": then_, "else": else_}

    _npc_branch: list[dict] = [
        # Stamp current Nemesis state into shared debug globals.
        _if_else(
            {"u_has_effect": "NEMESIS_HUNTING"},
            [{"math": ["global_debug_nem_state = 1"]}],
            [_if_else(
                {"u_has_effect": "NEMESIS_AMBUSH"},
                [{"math": ["global_debug_nem_state = 2"]}],
                [{"math": ["global_debug_nem_state = 0"]}],
            )],
        ),
        {"math": ["global_debug_nem_ovr   = u_effect_intensity('NEMESIS_OVR_DIST')"]},
        {"math": ["global_debug_nem_frust = u_effect_intensity('NEMESIS_FRUSTRATION')"]},
        {"math": [
            "global_debug_nem_dist = "
            "abs(u_val('pos_x') / 24 - global_player_omt_x) + "
            "abs(u_val('pos_y') / 24 - global_player_omt_y)"
        ]},
    ]

    _avatar_branch: list[dict] = [
        # State line: HUNTING / AMBUSH / PASSIVE
        _if_else(
            {"math": ["global_debug_nem_state == 1"]},
            [_msg("state=HUNTING", "yellow")],
            [_if_else(
                {"math": ["global_debug_nem_state == 2"]},
                [_msg("state=AMBUSH", "dark_gray")],
                [_msg("state=PASSIVE", "white")],
            )],
        ),
        # OMT distance bands
        _if_else(
            {"math": ["global_debug_nem_dist <= 2"]},
            [_msg("dist=VERY_CLOSE (<=2 OMT)", "red")],
            [_if_else(
                {"math": ["global_debug_nem_dist <= 8"]},
                [_msg("dist=CLOSE (3-8 OMT)", "yellow")],
                [_if_else(
                    {"math": ["global_debug_nem_dist <= 20"]},
                    [_msg("dist=FAR (9-20 OMT)", "white")],
                    [_msg("dist=DISTANT (>20 OMT)", "dark_gray")],
                )],
            )],
        ),
        # OVR_DIST bands
        _if_else(
            {"math": ["global_debug_nem_ovr >= 20"]},
            [_msg("ovr_dist=HIGH (>=20)", "white")],
            [_if_else(
                {"math": ["global_debug_nem_ovr >= 10"]},
                [_msg("ovr_dist=MED (10-19)", "yellow")],
                [_if_else(
                    {"math": ["global_debug_nem_ovr >= 1"]},
                    [_msg("ovr_dist=LOW (1-9)", "red")],
                    [_msg("ovr_dist=NONE (0)", "dark_gray")],
                )],
            )],
        ),
        # Frustration bands
        _if_else(
            {"math": ["global_debug_nem_frust == 0"]},
            [_msg("frust=ZERO", "white")],
            [_if_else(
                {"math": ["global_debug_nem_frust <= 5"]},
                [_msg("frust=LOW (1-5)", "white")],
                [_if_else(
                    {"math": ["global_debug_nem_frust <= 15"]},
                    [_msg("frust=MED (6-15)", "yellow")],
                    [_msg("frust=HIGH (>15 — nearing give-up)", "red")],
                )],
            )],
        ),
    ]

    return {
        "type":         "effect_on_condition",
        "id":           "EOC_NEMESIS_DEBUG_STATE",
        "eoc_type":     "RECURRING",
        "global":       True,
        "run_for_npcs": True,
        "recurrence":   ["1 minutes", "1 minutes"],
        "condition":    {"u_has_trait": "NEMESIS_MARK"},
        "effect":       _npc_branch,
        "false_effect": [{"if": "u_is_avatar", "then": _avatar_branch}],
    }


def _apply_state(master_data: list) -> None:
    """Append EOC_NEMESIS_DEBUG_STATE to the master EOC array."""
    ids = {obj.get("id") for obj in master_data if isinstance(obj, dict)}
    if "EOC_NEMESIS_DEBUG_STATE" not in ids:
        master_data.append(_build_state_eoc())
    print("  [state] added EOC_NEMESIS_DEBUG_STATE to master")


# ---------------------------------------------------------------------------
# Mode: dread
# Adds EOC_NEMESIS_DEBUG_DREAD to master: a 1-min RECURRING that queues all
# three dread message tiers every minute while any Nemesis is hunting.
# The hunt announcer drains them at its own 1-min cadence.
# ---------------------------------------------------------------------------

def _build_dread_eoc() -> dict:
    """Build EOC_NEMESIS_DEBUG_DREAD — forces all three dread message tiers every minute."""
    return {
        "type":         "effect_on_condition",
        "id":           "EOC_NEMESIS_DEBUG_DREAD",
        "eoc_type":     "RECURRING",
        "global":       True,
        "run_for_npcs": True,
        "recurrence":   ["1 minutes", "1 minutes"],
        # Condition runs in NPC context: true only for NPCs with NEMESIS_HUNTING.
        "condition":    {"u_has_effect": "NEMESIS_HUNTING"},
        "effect": [
            # Queue all three dread tiers.  The hunt announcer drains one per minute
            # per tier, so each dread level fires independently every tick.
            {"math": ["global_nemtrack_dread_high_pending = global_nemtrack_dread_high_pending + 1"]},
            {"math": ["global_nemtrack_dread_mid_pending  = global_nemtrack_dread_mid_pending  + 1"]},
            {"math": ["global_nemtrack_dread_low_pending  = global_nemtrack_dread_low_pending  + 1"]},
        ],
    }


def _apply_dread(master_data: list) -> None:
    """Append EOC_NEMESIS_DEBUG_DREAD to the master EOC array."""
    ids = {obj.get("id") for obj in master_data if isinstance(obj, dict)}
    if "EOC_NEMESIS_DEBUG_DREAD" not in ids:
        master_data.append(_build_dread_eoc())
    print("  [dread] added EOC_NEMESIS_DEBUG_DREAD to master")


# ---------------------------------------------------------------------------
# Mode: death-echo
# Injects a coordinate-free EOC_NEMESIS_DEATH_ECHO_* into each seed file.
# The echo fires on any OMT crossing after the Nemesis is armed (no X/Y/Z check),
# making it easy to verify the message text without traveling to the death OMT.
# Files that already have a death_echo EOC (real player Nemeses) are not modified.
# ---------------------------------------------------------------------------

def _build_seed_echo_eoc(safe_name: str) -> dict:
    """Build a coordinate-free death echo EOC for testing purposes."""
    spawned_var   = f"global_nemesis_spawned_{safe_name}"
    echo_seen_var = f"global_nemesis_echo_seen_{safe_name}"
    return {
        "type":           "effect_on_condition",
        "id":             f"EOC_NEMESIS_DEATH_ECHO_{safe_name}",
        "eoc_type":       "EVENT",
        "required_event": "avatar_enters_omt",
        "condition": {
            "and": [
                # Fires once after the Nemesis is armed, on any OMT crossing.
                # No coordinate check — the real echo restricts to the death OMT.
                {"math": [f"{spawned_var} >= 1"]},
                {"math": [f"{echo_seen_var} == 0"]},
            ]
        },
        "effect": [
            {
                "u_message": (
                    f"<color_dark_gray>[NEM DEBUG] Death echo fired for {safe_name}. "
                    f"(No coordinate check in debug mode.)</color>"
                ),
                "type": "neutral",
            },
            {"math": [f"{echo_seen_var} = 1"]},
        ],
    }


def _apply_death_echo(seed_paths: list[Path], seed_data: dict[Path, list]) -> None:
    """Inject coordinate-free death echo EOCs into seed files that lack one."""
    for path in seed_paths:
        data = seed_data[path]
        # Determine safe_name from the spawn-check EOC id.
        safe_name: str | None = None
        has_echo = False
        for obj in data:
            if not isinstance(obj, dict):
                continue
            eid = obj.get("id", "")
            if eid.startswith("EOC_NEMESIS_SPAWN_CHECK_"):
                safe_name = eid.replace("EOC_NEMESIS_SPAWN_CHECK_", "")
            if eid.startswith("EOC_NEMESIS_DEATH_ECHO_"):
                has_echo = True

        if safe_name is None:
            print(f"  [death-echo] skipping {path.name}: no spawn-check EOC found")
            continue
        if has_echo:
            print(f"  [death-echo] skipping {path.name}: already has death echo EOC")
            continue

        data.append(_build_seed_echo_eoc(safe_name))
        print(f"  [death-echo] injected coordinate-free echo into {path.name}")


# ---------------------------------------------------------------------------
# apply / restore entry points
# ---------------------------------------------------------------------------

_ALL_MODES = frozenset({"spawn", "hunt", "ambush", "state", "dread", "death-echo"})


def apply(modes: set[str], nemesis_filter: str | None) -> None:
    """Apply the selected debug modes.

    Loads each target file once, applies all mode patches in sequence,
    then writes the modified JSON.  .bak backups are created on first apply.
    """
    if not modes:
        modes = {"spawn"}

    seed_paths = _target_seed_files(nemesis_filter)
    master_data = _load(MASTER)

    # Load all seed files up front.
    seed_data: dict[Path, list] = {p: _load(p) for p in seed_paths}

    print(f"Applying modes: {sorted(modes)}")
    if nemesis_filter:
        print(f"  (filtered to: *{nemesis_filter}*)")

    _backup(MASTER)
    for p in seed_paths:
        _backup(p)

    if "spawn"      in modes: _apply_spawn(master_data, seed_paths, seed_data)
    if "state"      in modes: _apply_state(master_data)
    if "dread"      in modes: _apply_dread(master_data)
    if "hunt"       in modes: _apply_hunt(seed_paths, seed_data)
    if "ambush"     in modes: _apply_ambush(seed_paths, seed_data)
    if "death-echo" in modes: _apply_death_echo(seed_paths, seed_data)

    _save(MASTER, master_data)
    for p in seed_paths:
        _save(p, seed_data[p])

    print(f"Done. {len(seed_paths)} seed file(s) + master patched.")
    print("Start a new game — one Nemesis will spawn immediately on game load.")
    print("Each subsequent OMT crossing spawns the next one.")


def restore(verify: bool) -> None:
    """Restore all patched files from their .json.bak backups.

    When verify=True, re-reads each restored file and checks for leftover
    [NEMESIS DEBUG] strings — a sign that restore failed or was partial.
    """
    print("Restoring originals from .bak files...")
    all_targets = [MASTER] + sorted(GEN_DIR.glob("nemesis_*.json"))
    restored = 0
    for path in all_targets:
        if _restore_from_bak(path):
            print(f"  restored {path.name}")
            restored += 1

    print(f"Done. {restored} file(s) restored.")

    if verify:
        print("Verifying restored files...")
        failures: list[str] = []
        checked = 0
        for path in all_targets:
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            if "[NEMESIS DEBUG]" in text:
                failures.append(path.name)
            checked += 1
        if failures:
            print(f"VERIFY FAILED — {len(failures)} file(s) still contain debug strings:")
            for f in failures:
                print(f"  {f}")
            sys.exit(1)
        print(f"  OK: {checked} file(s) checked — no debug strings found.")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nemesis_debug_patch.py",
        description="Apply or restore debug patches to the NEMESIS mod.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    apply_p = sub.add_parser("apply", help="Apply one or more debug modes.")
    apply_p.add_argument(
        "--mode",
        dest="modes",
        action="append",
        choices=sorted(_ALL_MODES),
        metavar="MODE",
        help=(
            "Mode to apply.  Can be specified multiple times.  "
            "Choices: " + ", ".join(sorted(_ALL_MODES)) + ".  "
            "Default: spawn."
        ),
    )
    apply_p.add_argument(
        "--nemesis",
        dest="nemesis",
        metavar="SAFE_NAME",
        default=None,
        help="Restrict seed-file patches to files whose name contains SAFE_NAME.",
    )

    restore_p = sub.add_parser("restore", help="Restore files from .bak backups.")
    restore_p.add_argument(
        "--verify",
        action="store_true",
        help="After restore, check that no [NEMESIS DEBUG] strings remain.",
    )

    return parser


if __name__ == "__main__":
    parser  = _build_parser()
    args    = parser.parse_args()

    if args.command == "apply":
        modes = set(args.modes) if args.modes else {"spawn"}
        apply(modes, args.nemesis)
    elif args.command == "restore":
        restore(args.verify)
