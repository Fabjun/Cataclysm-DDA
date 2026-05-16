#!/usr/bin/env python3
"""nemesis_eoc.py -- Phase 3-5: per-character EOC builders, master routing, and overmap hunt.

PURPOSE
-------
This module produces all CDDA Effect-On-Condition (EOC) JSON for the NEMESIS system.
It operates in three layers that are assembled into two output artifacts:

LAYER 1 — Per-character EOCs (one set per Nemesis, written to nemesis_<name>.json):
  - EOC_NEMESIS_SPAWN_CHECK_<name>   : ACTIVATION, routed by day hubs.  Gates spawn
    behind: spawn_lock, spawned state, delay elapsed, location match, anniversary.
  - EOC_NEMESIS_SPAWN_SUCCESS_<name> : ACTIVATION, fires on successful spawn.  Sets
    spawned=2, stamps cooldown, shows perception-gated message, applies wounds + effects.
  - EOC_NEMESIS_DEATH_NOTIFY_<name>  : ACTIVATION, delivers the kill message to the
    player.  Triggered by the death-relay on OMT entry after the NPC dies.
  - EOC_NEMESIS_WOUNDS_<name>        : ACTIVATION, applies body-part HP ratios to the
    spawned NPC via math expressions.
  - EOC_NEMESIS_EFFECTS_<name>       : ACTIVATION, applies spawn-time status effects
    and u_learn_martial_art calls to the spawned NPC.
  - EOC_NEMESIS_DEATH_ECHO_<name>    : EVENT (avatar_enters_omt), fires exactly once
    when the player visits the OMT where the Nemesis's original character died.  Bakes
    a cause-specific atmospheric message at generation time.  Omitted for seed Nemeses
    (no real save-file position).
  - EOC_NEMESIS_HUNT_REDIRECT_<name> : EVENT (avatar_enters_omt), fires while Nemesis
    is active (spawned == 2) and not dead.  Freshens global player position vars and
    calls u_run_npc_eocs to redirect the Nemesis's overmap goal to the player's
    current position.  Works on unloaded NPCs — CDDA finds them via the overmap buffer.
  - EOC_NEMESIS_REDIRECT_NPC_<name>  : ACTIVATION, runs in NPC context via
    u_run_npc_eocs.  Condition: NPC has NEMESIS_HUNTING.  Sets u_set_goal to the
    player's current OMT position so the NPC physically walks toward the player even
    while unloaded.  Mirrors the _hot trail goal-update in EOC_NEMESIS_TRACKER.

LAYER 2 — Hub routing (one EOC per (death_day, days_per_year) pair, in master):
  - EOC_HUB_<Y>Y_<D>D  : ACTIVATION, groups all Nemeses that share the same calendar
    anniversary.  Each hub runs its member spawn-check EOCs via run_eocs.

LAYER 3 — Global system EOCs (always present, written to nemesis_master.json):
  - EOC_NEMESIS_MASTER         : EVENT (avatar_enters_omt), 1-in-_SPAWN_ONE_IN_N gate
    + _SPAWN_COOLDOWN_MINUTES cooldown.  Routes to day hubs whose anniversary matches.
  - EOC_NEMESIS_DEATH_RELAY    : EVENT (avatar_enters_omt), unconditional.  Relays
    pending kill messages regardless of the spawn gate.
  - EOC_NEMESIS_FLAG_*x4       : EVENT, stamp global_player_just_attacked=1.
  - EOC_NEMESIS_REVENGE_TRIGGER: EVENT (character_takes_damage), arms NEMESIS_HUNTING.
  - EOC_NEMESIS_HUNT_ANNOUNCER : RECURRING (1 min), drains pending message counters.
  - EOC_NEMESIS_ACOUSTIC_*x2   : EVENT, raise global_nemesis_noise_level on loud actions.
  - EOC_NEMESIS_TRACKER        : RECURRING (60 min), overmap hunt core loop.

PRIMARY EXPORTS
---------------
build_per_char_eocs(char_data) -- build 7 or 8 per-character EOC dicts (8 when death_omt is set).
compile_master(mod_dir, out_path) -- scan all nemesis_*.json, write nemesis_master.json.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from nemesis_builder import (
    NemesisCharData,
    DeathCause,
    _sanitize_markup,       # used by build_spawn_success_eoc, build_death_notify_eoc
    _DEFAULT_SEASON_DAYS,   # used as default for days_per_year in build_spawn_check_eoc
    _death_narrative,       # used by build_death_notify_eoc
)


# ---------------------------------------------------------------------------
# Phase 3: routing constants
# ---------------------------------------------------------------------------

_DEFAULT_DAYS_PER_YEAR: int = _DEFAULT_SEASON_DAYS * 4  # single source of truth

_DAY_GATE_RE = re.compile(
    r"time\('now'\)\s*%\s*time\('(\d+)\s*days'\)\s*>=\s*time\('(\d+)\s*days'\)"
)


# ---------------------------------------------------------------------------
# Phase 5: overmap hunt constants
# ---------------------------------------------------------------------------

_TRACKER_INTERVAL_MIN: int = 60   # minutes between tracker ticks
_TRACKER_INTERVAL_MAX: int = 60

_OVR_DIST_START:  int = 30   # initial overmap search radius (OMT units)
_OVR_DIST_MIN:    int = 2    # floor — Nemesis is very close to player
_OVR_DIST_SHRINK: int = 3    # radius shrinks by this per hot-trail tick

_FRUSTRATION_INCR: int = 1   # cold-trail ticks per interval
_FRUSTRATION_DECR: int = 2   # recovery per hot-trail tick (faster than accumulation)

_NOISE_WEIGHT:  int = 2      # score contribution per noise unit
_VISION_WEIGHT: int = 10     # score bonus per OMT of visual proximity
_HOT_THRESHOLD: int = 15     # minimum score to enter pursuit mode

_ACOUSTIC_NOISE_SMASH:  int = 30  # terrain destruction noise floor
_ACOUSTIC_NOISE_RANGED: int = 20  # ranged attack noise floor
_ACOUSTIC_NOISE_DECAY:  int = 5   # decay per tracker tick

_PROX_WARNING_DIST: int = 8   # OMT radius for cold-trail proximity warning (legacy fallback)

_AMBUSH_OVR_DIST:   int = 5   # OVR_DIST intensity set on give-up; also the proximity radius
                               # at which a waiting Nemesis re-arms the hunt

# Dread system — stat-based proximity warning on cold trail.
# dread_range = max(_DREAD_RANGE_MIN, (NPC_INT + NPC_PER) / _DREAD_RANGE_DIVISOR)
# This scales with the Nemesis's actual stats so stronger characters feel more threatening
# at longer range.  Tier labels are never used here — the formula works for both offline
# seeded Nemeses and real Mode 2 player characters.
_DREAD_RANGE_MIN:      int = 3   # minimum dread detection radius regardless of NPC stats
_DREAD_RANGE_DIVISOR:  int = 4   # divisor applied to (INT+PER) to get dread range in OMTs

# Player-side dread score thresholds — higher score = more sensitive to approaching Nemesis.
# score = player_PER + survival_skill * 2 + morale_bonus (2 if morale > 0, else 0)
_DREAD_SCORE_HIGH: int = 15  # "hairs stand on end"
_DREAD_SCORE_MID:  int = 12  # "something is wrong"
_DREAD_SCORE_LOW:  int = 9   # "vaguely unsettled"

# Two-phase cold trail — disciplined tracker vs. animal drift.
# Phase 1 (FRUSTRATION < (INT+PER) * _TRACKER_MEMORY_FRACTION): wander around last-known
#   player position.  The Nemesis is methodically searching the area where it lost you.
# Phase 2 (FRUSTRATION >= threshold): wander from its own current position.  The Nemesis
#   has lost patience and roams erratically — easier to shake but unpredictable.
# The threshold is intentionally halfway to give-up so each phase lasts the same duration.
# Weak Nemeses (low INT+PER) enter Phase 2 after just a few hours; strong ones stay
# disciplined for most of their total hunt window before drifting.
_TRACKER_MEMORY_FRACTION: float = 0.25   # fraction of (INT+PER) at which phase 1 -> phase 2

# Minimum minutes that must pass between spawns (applied via time_since on global_nemesis_last_spawn).
# 0 = no cooldown; every eligible OMT crossing can trigger a spawn (test phase setting).
# Set to 60 for production to prevent multiple spawns per in-game hour.
_SPAWN_COOLDOWN_MINUTES: int = 0

# 1-in-N chance that an eligible OMT crossing will attempt a Nemesis spawn check.
# 1 = every crossing attempts (100% — test phase, pair with _SPAWN_COOLDOWN_MINUTES = 0).
# 100 = production: roughly 1% chance per crossing.  Combined with the 60-minute cooldown
# this gives unpredictable, infrequent spawns — the player can't predict the exact moment.
# Without this gate, a Nemesis spawns on the FIRST crossing after the cooldown expires,
# making the cadence regular and easy to anticipate.
_SPAWN_ONE_IN_N: int = 1

# NPC-defended locations where the Nemesis abandons pursuit on a cold trail.
# (omt_id, om_radius): radius 0 = must be on the tile; >0 = within N OMTs of any tile of that type.
_SAFE_ZONE_OMTS: tuple[tuple[str, int], ...] = (
    ("shelter",        0),   # evac shelter — NPC-guarded single OMT
    ("evac_center_13", 2),   # refugee center — 3×3 OMT block, radius 2 covers all tiles
    ("outpost",        0),   # Old Guard military outpost
    ("ranch_camp_1",  12),   # Free Traders ranch — 9×9 OMTs; radius 12 covers full complex
)



# ---------------------------------------------------------------------------
# Phase 3: metadata model (read from per-character JSON files during scan)
# ---------------------------------------------------------------------------

@dataclass
class NemesisMeta:
    """Routing metadata extracted from a per-character nemesis JSON file during scanning.

    compile_master() scans every nemesis_*.json file and extracts one NemesisMeta per
    file via _extract_meta().  The collected metadata is then used to build the hub
    EOCs (grouped by anniversary day) and the master routing tree.

    safe_name      : ID fragment extracted from the spawn-check EOC ID.
    spawn_check_id : Full EOC ID for routing (e.g., "EOC_NEMESIS_SPAWN_CHECK_alice_smith_0042").
    death_notify_id: Full EOC ID for the death-relay to call, or None if not found.
    death_day      : Day-of-year from the anniversary gate expression, or None for seeds.
    days_per_year  : Total days in a CDDA year for this character's world, or None.
    is_outside     : True = spawns outdoors, False = spawns indoors, None = no location gate.
    """
    safe_name:       str
    spawn_check_id:  str
    death_notify_id: str | None
    death_day:       int | None
    days_per_year:   int | None
    is_outside:      bool | None


# ---------------------------------------------------------------------------
# Phase 3: file-scanning helpers
# ---------------------------------------------------------------------------

def _find_eoc(data: list[dict[str, Any]], fragment: str) -> dict[str, Any] | None:
    """Return the first effect_on_condition dict whose "id" contains fragment, or None."""
    return next(
        (o for o in data
         if o.get("type") == "effect_on_condition" and fragment in o.get("id", "")),
        None,
    )


def _extract_meta(
    filename: str, data: list[dict[str, Any]]
) -> NemesisMeta | None:
    """Extract routing metadata from a per-character nemesis JSON file.

    Returns None if the file doesn't contain a recognisable spawn-check EOC.
    """
    spawn_check = _find_eoc(data, "EOC_NEMESIS_SPAWN_CHECK_")
    if spawn_check is None:
        return None

    spawn_check_id = spawn_check.get("id", "")
    prefix = "EOC_NEMESIS_SPAWN_CHECK_"
    if not spawn_check_id.startswith(prefix):
        return None
    safe_name = spawn_check_id[len(prefix):]

    death_notify    = _find_eoc(data, "EOC_NEMESIS_DEATH_NOTIFY_")
    death_notify_id = death_notify.get("id") if death_notify else None

    death_day:    int | None  = None
    days_per_year: int | None = None
    is_outside:   bool | None = None

    cond     = spawn_check.get("condition") or {}
    and_list: list[Any] = cond.get("and", []) if isinstance(cond, dict) else []

    for item in and_list:
        if not isinstance(item, dict):
            continue
        for expr in item.get("math") or []:
            m = _DAY_GATE_RE.search(str(expr))
            if m:
                days_per_year = int(m.group(1))
                death_day     = int(m.group(2))
        if item == "u_is_outside":
            is_outside = True
        elif item == {"not": "u_is_outside"}:
            is_outside = False

    if (death_day is None or days_per_year is None) and not filename.startswith("nemesis_seed_"):
        print(
            f"NEM: warning: {filename}: could not parse anniversary gate from spawn_check "
            f"conditions — Nemesis will not be routed to a day hub.",
            file=sys.stderr,
        )

    return NemesisMeta(
        safe_name       = safe_name,
        spawn_check_id  = spawn_check_id,
        death_notify_id = death_notify_id,
        death_day       = death_day,
        days_per_year   = days_per_year,
        is_outside      = is_outside,
    )


def scan_nemesis_files(mod_dir: str) -> list[NemesisMeta]:
    """Scan mod_dir for nemesis_*.json files and return their routing metadata.

    Skips nemesis_master.json and files without a recognisable spawn-check EOC.
    """
    results: list[NemesisMeta] = []
    try:
        filenames = sorted(os.listdir(mod_dir))
    except OSError as exc:
        print(f"NEM: warning: cannot list {mod_dir}: {exc}", file=sys.stderr)
        return results

    for fname in filenames:
        if not fname.startswith("nemesis_") or not fname.endswith(".json"):
            continue
        if fname == "nemesis_master.json":
            continue
        path = os.path.join(mod_dir, fname)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"NEM: warning: skipping {fname}: {exc}", file=sys.stderr)
            continue
        if not isinstance(data, list):
            continue
        meta = _extract_meta(fname, data)
        if meta is not None:
            results.append(meta)

    return results


# ---------------------------------------------------------------------------
# Phase 3: hub and master EOC builders
# ---------------------------------------------------------------------------

@dataclass
class HubKey:
    """Key identifying one day-hub EOC in the master routing tree.

    A hub groups all Nemeses that share the same (death_day, days_per_year)
    anniversary.  The master EOC dispatches to each hub only when the current
    in-game date modulo days_per_year >= death_day, so multiple Nemeses with the
    same anniversary pay one date-comparison instead of N comparisons.
    """
    death_day:     int
    days_per_year: int
    hub_id:        str


def _hub_effect(group: list[NemesisMeta]) -> list[dict[str, Any]]:
    """Build the effect list for one hub EOC: run each member's spawn-check EOC."""
    return [{"run_eocs": [meta.spawn_check_id]} for meta in group]


def build_hub_eocs(
    nemeses: list[NemesisMeta],
) -> tuple[list[dict[str, Any]], list[HubKey]]:
    """Group nemeses by (death_day, days_per_year) and build one hub EOC per group.

    Nemeses without death_day/days_per_year fall into the day-0 default bucket.
    """
    groups: dict[tuple[int, int], list[NemesisMeta]] = defaultdict(list)
    for meta in nemeses:
        key = (
            meta.death_day     if meta.death_day     is not None else 0,
            meta.days_per_year if meta.days_per_year is not None else _DEFAULT_DAYS_PER_YEAR,
        )
        groups[key].append(meta)

    hub_eocs: list[dict[str, Any]] = []
    hub_keys: list[HubKey]          = []

    for (death_day, days_per_year), group in sorted(groups.items()):
        hub_id = f"EOC_HUB_{days_per_year}Y_{death_day:04d}D"
        hub_eocs.append({
            "type":     "effect_on_condition",
            "id":       hub_id,
            "eoc_type": "ACTIVATION",
            "effect":   _hub_effect(group),
        })
        hub_keys.append(HubKey(
            death_day     = death_day,
            days_per_year = days_per_year,
            hub_id        = hub_id,
        ))

    return hub_eocs, hub_keys


def build_master_eoc(keys: list[HubKey]) -> dict[str, Any]:
    """Build EOC_NEMESIS_MASTER — sole avatar_enters_omt listener for spawn routing.

    Two tuning constants control spawn frequency:
      _SPAWN_ONE_IN_N         — 1-in-N random gate per OMT crossing (1 = always, 100 = 1%).
      _SPAWN_COOLDOWN_MINUTES — minimum minutes between any two spawns (0 = no cooldown).

    Test phase: both set to their minimum (1 and 0) so spawns fire immediately and
    on every crossing, making the pipeline easy to verify without waiting in-game days.
    Production: _SPAWN_ONE_IN_N = 100 and _SPAWN_COOLDOWN_MINUTES = 60 give infrequent,
    unpredictable spawns that the player cannot time or anticipate.
    """
    hub_routing: list[dict[str, Any]] = [
        {
            "if": {"math": [
                f"time('now') % time('{key.days_per_year} days') "
                f">= time('{key.death_day} days')"
            ]},
            "then": {"run_eocs": [key.hub_id]},
        }
        for key in sorted(keys, key=lambda k: (k.days_per_year, k.death_day))
    ]
    if _SPAWN_COOLDOWN_MINUTES > 0:
        condition: dict[str, Any] = {
            "and": [
                {"one_in_chance": _SPAWN_ONE_IN_N},
                {"math": [f"time_since(global_nemesis_last_spawn) >= time('{_SPAWN_COOLDOWN_MINUTES} minutes')"]},
            ]
        }
    else:
        condition = {"one_in_chance": _SPAWN_ONE_IN_N}
    return {
        "type":           "effect_on_condition",
        "id":             "EOC_NEMESIS_MASTER",
        "eoc_type":       "EVENT",
        "required_event": "avatar_enters_omt",
        "condition":      condition,
        "effect": [{"math": ["global_nemesis_spawn_lock = 0"]}] + hub_routing,
    }


def build_death_relay_eoc(death_notify_ids: list[str]) -> dict[str, Any]:
    """Build EOC_NEMESIS_DEATH_RELAY — fires unconditionally on OMT entry.

    Relays kill messages independently of the 1-in-100 spawn gate.
    """
    return {
        "type":           "effect_on_condition",
        "id":             "EOC_NEMESIS_DEATH_RELAY",
        "eoc_type":       "EVENT",
        "required_event": "avatar_enters_omt",
        "effect":         [{"run_eocs": [eid]} for eid in death_notify_ids],
    }


# ---------------------------------------------------------------------------
# Phase 4: attack-flag and revenge EVENT EOC builders
# ---------------------------------------------------------------------------

def build_player_attack_flag_eocs() -> list[dict[str, Any]]:
    """Build four EVENT EOCs that stamp global_player_just_attacked = 1.

    Covers melee/ranged attacks against characters and monsters.
    The flag is read by EOC_NEMESIS_REVENGE_TRIGGER and cleared every minute
    by EOC_NEMESIS_HUNT_ANNOUNCER.
    """
    _set_flag: list[dict[str, Any]] = [{"math": ["global_player_just_attacked = 1"]}]
    return [
        {
            "type": "effect_on_condition",
            "id": "EOC_NEMESIS_FLAG_MELEE_CHAR",
            "eoc_type": "EVENT",
            "required_event": "character_melee_attacks_character",
            "condition": "u_is_avatar",
            "effect": _set_flag,
        },
        {
            "type": "effect_on_condition",
            "id": "EOC_NEMESIS_FLAG_MELEE_MON",
            "eoc_type": "EVENT",
            "required_event": "character_melee_attacks_monster",
            "condition": "u_is_avatar",
            "effect": _set_flag,
        },
        {
            "type": "effect_on_condition",
            "id": "EOC_NEMESIS_FLAG_RANGED_CHAR",
            "eoc_type": "EVENT",
            "required_event": "character_ranged_attacks_character",
            "condition": "u_is_avatar",
            "effect": _set_flag,
        },
        {
            "type": "effect_on_condition",
            "id": "EOC_NEMESIS_FLAG_RANGED_MON",
            "eoc_type": "EVENT",
            "required_event": "character_ranged_attacks_monster",
            "condition": "u_is_avatar",
            "effect": _set_flag,
        },
    ]


def build_revenge_trigger() -> dict[str, Any]:
    """Build EOC_NEMESIS_REVENGE_TRIGGER — arms a Nemesis into HUNTING mode on hit.

    Fires on character_takes_damage.  Condition: the hit character is a Nemesis
    (has NEMESIS_MARK), is not already hunting, and the player attacked recently.
    Sets NEMESIS_HUNTING + initial OVR_DIST on the NPC and queues a hunt announcement.
    """
    _arm_hunting: list[dict[str, Any]] = [
        {"u_add_effect": "NEMESIS_HUNTING",     "duration": "PERMANENT"},
        {"u_add_effect": "NEMESIS_OVR_DIST",    "duration": "PERMANENT",
         "intensity": _OVR_DIST_START},
        {"u_lose_effect": "NEMESIS_FRUSTRATION"},
        {
            "u_location_variable": {"u_val": "nemesis_last_tracked_pos"},
            "min_radius": 0,
            "max_radius": 0,
        },
        {"math": ["global_player_just_attacked = 0"]},
        {"math": ["global_nemesis_hunt_pending = global_nemesis_hunt_pending + 1"]},
        {"math": ["global_nemesis_battlecry_pending = global_nemesis_battlecry_pending + 1"]},
    ]
    return {
        "type":           "effect_on_condition",
        "id":             "EOC_NEMESIS_REVENGE_TRIGGER",
        "eoc_type":       "EVENT",
        "required_event": "character_takes_damage",
        "condition": {
            "and": [
                {"u_has_trait": "NEMESIS_MARK"},
                {"not": {"u_has_effect": "NEMESIS_HUNTING"}},
                {"math": ["global_player_just_attacked == 1"]},
            ]
        },
        "effect": _arm_hunting,
    }


# ---------------------------------------------------------------------------
# Phase 5: hunt announcer, swarm, acoustic, and tracker RECURRING/EVENT builders
# ---------------------------------------------------------------------------

def build_hunt_announcer() -> dict[str, Any]:
    """Build EOC_NEMESIS_HUNT_ANNOUNCER — RECURRING (1 min) message dispatcher.

    Drains nine pending-message counters per tick:
      global_nemesis_hunt_pending           -> red       "ominous presence"    (new hunt armed)
      global_nemesis_battlecry_pending      -> red       "battle cry"          (hunt armed, voice)
      global_nemtrack_msg_pending           -> yellow    "closing in"          (hot trail)
      global_nemtrack_ambush_pending        -> dark_gray "pursuit fades"       (give-up)
      global_nemtrack_resume_pending        -> yellow    "pursuit returns"     (ambush re-trigger)
      global_nemtrack_phase_shift_pending   -> dark_gray "lost the thread"     (phase 1 -> 2)
      global_nemtrack_dread_high_pending    -> red       "hairs on neck"       (dread tier 3)
      global_nemtrack_dread_mid_pending     -> yellow    "something is wrong"  (dread tier 2)
      global_nemtrack_dread_low_pending     -> dark_gray "vaguely unsettled"   (dread tier 1)
      global_nemtrack_prox_pending          -> dark_gray "presence lingers"    (legacy fallback)
    Also resets global_player_just_attacked to clear the ~1-minute attack window.
    """
    return {
        "type":           "effect_on_condition",
        "id":             "EOC_NEMESIS_HUNT_ANNOUNCER",
        "eoc_type":       "RECURRING",
        "global":         True,
        "recurrence":     ["1 minutes", "1 minutes"],
        "effect": [
            {"math": ["global_player_just_attacked = 0"]},
            {
                "if":   {"math": ["global_nemesis_hunt_pending >= 1"]},
                "then": [
                    {
                        "u_message": (
                            "<color_red>You feel an ominous presence — "
                            "something from your past is hunting you.</color>"
                        ),
                        "type": "bad",
                    },
                    {"math": [
                        "global_nemesis_hunt_pending = global_nemesis_hunt_pending - 1"
                    ]},
                ],
            },
            {
                "if":   {"math": ["global_nemesis_battlecry_pending >= 1"]},
                "then": [
                    {
                        "u_message": (
                            "<color_red>A voice cuts through the air: "
                            "\"You will pay for what you've done!\"</color>"
                        ),
                        "type": "bad",
                    },
                    {"math": [
                        "global_nemesis_battlecry_pending = global_nemesis_battlecry_pending - 1"
                    ]},
                ],
            },
            {
                "if":   {"math": ["global_nemtrack_msg_pending >= 1"]},
                "then": [
                    {
                        "u_message": (
                            "<color_yellow>A chill runs down your spine — "
                            "something is closing in on your position.</color>"
                        ),
                        "type": "bad",
                    },
                    {"math": [
                        "global_nemtrack_msg_pending = global_nemtrack_msg_pending - 1"
                    ]},
                ],
            },
            {
                "if":   {"math": ["global_nemtrack_ambush_pending >= 1"]},
                "then": [
                    {
                        "u_message": (
                            "<color_dark_gray>The feeling of pursuit fades... "
                            "it may be lying in wait.</color>"
                        ),
                        "type": "neutral",
                    },
                    {"math": [
                        "global_nemtrack_ambush_pending = global_nemtrack_ambush_pending - 1"
                    ]},
                ],
            },
            {
                "if":   {"math": ["global_nemtrack_resume_pending >= 1"]},
                "then": [
                    {
                        "u_message": (
                            "<color_yellow>The feeling of pursuit returns — "
                            "something has found your trail again.</color>"
                        ),
                        "type": "bad",
                    },
                    {"math": [
                        "global_nemtrack_resume_pending = global_nemtrack_resume_pending - 1"
                    ]},
                ],
            },
            {
                "if":   {"math": ["global_nemtrack_prox_pending >= 1"]},
                "then": [
                    {
                        "u_message": (
                            "<color_dark_gray>A presence lingers just beyond your sight — "
                            "something is out there, closer than you'd like.</color>"
                        ),
                        "type": "neutral",
                    },
                    {"math": [
                        "global_nemtrack_prox_pending = global_nemtrack_prox_pending - 1"
                    ]},
                ],
            },
            {
                "if":   {"math": ["global_nemtrack_phase_shift_pending >= 1"]},
                "then": [
                    {
                        "u_message": (
                            "<color_dark_gray>The methodical feeling fades — "
                            "whatever is out there has lost the thread, "
                            "but it hasn't stopped.</color>"
                        ),
                        "type": "neutral",
                    },
                    {"math": [
                        "global_nemtrack_phase_shift_pending = "
                        "global_nemtrack_phase_shift_pending - 1"
                    ]},
                ],
            },
            # Dread system — three tiers of proximity warning scaled to player PER + survival
            # + morale.  Only one fires per tick per Nemesis; they are mutually exclusive at
            # the source (only the highest matching tier queues a message each tick).
            {
                "if":   {"math": ["global_nemtrack_dread_high_pending >= 1"]},
                "then": [
                    {
                        "u_message": (
                            "<color_red>The hairs on the back of your neck stand on end. "
                            "It's close.</color>"
                        ),
                        "type": "bad",
                    },
                    {"math": [
                        "global_nemtrack_dread_high_pending = "
                        "global_nemtrack_dread_high_pending - 1"
                    ]},
                ],
            },
            {
                "if":   {"math": ["global_nemtrack_dread_mid_pending >= 1"]},
                "then": [
                    {
                        "u_message": (
                            "<color_yellow>Something is wrong. "
                            "Your instincts scream that you are not alone.</color>"
                        ),
                        "type": "bad",
                    },
                    {"math": [
                        "global_nemtrack_dread_mid_pending = "
                        "global_nemtrack_dread_mid_pending - 1"
                    ]},
                ],
            },
            {
                "if":   {"math": ["global_nemtrack_dread_low_pending >= 1"]},
                "then": [
                    {
                        "u_message": (
                            "<color_dark_gray>You feel vaguely unsettled, "
                            "as if a memory is pulling at you.</color>"
                        ),
                        "type": "neutral",
                    },
                    {"math": [
                        "global_nemtrack_dread_low_pending = "
                        "global_nemtrack_dread_low_pending - 1"
                    ]},
                ],
            },
        ],
    }



def build_acoustic_trigger_smash() -> dict[str, Any]:
    """Build EOC_NEMESIS_ACOUSTIC_SMASH — raises noise floor on terrain destruction."""
    return {
        "type":           "effect_on_condition",
        "id":             "EOC_NEMESIS_ACOUSTIC_SMASH",
        "eoc_type":       "EVENT",
        "required_event": "character_smashes_tile",
        "condition":      "u_is_avatar",
        "effect": [
            {"math": [
                f"global_nemesis_noise_level = "
                f"max(global_nemesis_noise_level, {_ACOUSTIC_NOISE_SMASH})"
            ]},
        ],
    }


def build_acoustic_trigger_ranged() -> dict[str, Any]:
    """Build EOC_NEMESIS_ACOUSTIC_RANGED — raises noise floor on ranged attacks."""
    return {
        "type":           "effect_on_condition",
        "id":             "EOC_NEMESIS_ACOUSTIC_RANGED",
        "eoc_type":       "EVENT",
        "required_event": "character_ranged_attacks_monster",
        "condition":      "u_is_avatar",
        "effect": [
            {"math": [
                f"global_nemesis_noise_level = "
                f"max(global_nemesis_noise_level, {_ACOUSTIC_NOISE_RANGED})"
            ]},
        ],
    }


def build_tracker_eoc() -> dict[str, Any]:
    """Build EOC_NEMESIS_TRACKER — the overmap hunt core loop (RECURRING, 60 min).

    Runs for all characters (run_for_npcs: true).  Two roles per tick:

    Avatar (condition FALSE — no NEMESIS_MARK):
      Stamps player OMT position into global vars; decays acoustic noise by
      _ACOUSTIC_NOISE_DECAY per tick.  Also drains the global_nemesis_close flag
      set by the Nemesis branch to queue proximity warning messages.

    Nemesis NPC (condition TRUE — has NEMESIS_MARK):
      The NPC can be in one of three states; all three are checked each tick.

      ACTIVE HUNT (has NEMESIS_HUNTING):
        Compute score = vision contribution + noise contribution.
        HOT trail (close or score >= _HOT_THRESHOLD):
          Update last-known position, call u_set_goal, shrink OVR_DIST, reduce
          frustration, queue "closing in" message.
        COLD trail:
          Wander within OVR_DIST of last-known position, increase frustration.
          On give-up (frustration * 2 >= INT + PER): drop HUNTING, add AMBUSH,
          set OVR_DIST to _AMBUSH_OVR_DIST, queue "ambush" message.
        Safe-zone override: if player is in a safe zone on a cold trail, give up
          immediately regardless of frustration level.

      AMBUSH (has NEMESIS_AMBUSH):
        Reduced-range proximity check only.  If the player enters _AMBUSH_OVR_DIST
        OMTs of the Nemesis, re-arm the hunt: remove AMBUSH, add HUNTING, reset
        OVR_DIST to _OVR_DIST_START, clear frustration, queue "pursuit returns" msg.
        The hit-based re-trigger (EOC_NEMESIS_REVENGE_TRIGGER) also remains active
        while in ambush — being hit re-arms the hunt regardless of distance.

      PASSIVE (neither HUNTING nor AMBUSH):
        No action.  The Nemesis wanders as a normal NPC until hit by the player.
    """
    _safe_zone_cond: dict[str, Any] = {
        "or": [
            {"u_near_om_location": omt_id, "range": radius}
            for omt_id, radius in _SAFE_ZONE_OMTS
        ]
    }

    _hot: list[dict[str, Any]] = [
        {
            "location_variable_adjust": {"global_val": "global_nemesis_player_ms_pos"},
            "output_var": {"u_val": "nemesis_last_tracked_pos"},
        },
        {
            "u_set_goal": {
                "om_terrain": "field",
                "var": {"u_val": "nemesis_last_tracked_pos"},
            }
        },
        {
            "u_add_effect": "NEMESIS_OVR_DIST",
            "duration":     "PERMANENT",
            # Shrink the search radius by _OVR_DIST_SHRINK each hot-trail tick until the
            # floor _OVR_DIST_MIN is reached.  The outer max is the only guard needed;
            # the previous inner max(current, _OVR_DIST_START) was incorrect — it floored
            # the pre-subtraction value at 30, making OVR_DIST permanently stuck at 27
            # after the first hot tick instead of continuing to shrink toward 2.
            "intensity": {"math": [
                f"max({_OVR_DIST_MIN}, "
                f"u_effect_intensity('NEMESIS_OVR_DIST') - {_OVR_DIST_SHRINK})"
            ]},
        },
        {
            "u_add_effect": "NEMESIS_FRUSTRATION",
            "duration":     "PERMANENT",
            # Recover frustration by _FRUSTRATION_DECR per hot tick.  Floor is 0, not 1:
            # a floor of 1 would prevent full recovery — a Nemesis alternating hot/cold
            # ticks would accumulate frustration monotonically (each cold tick adds 1, each
            # hot tick clamps at 1 rather than reaching 0) and give up far too quickly.
            "intensity": {"math": [
                f"max(0, u_effect_intensity('NEMESIS_FRUSTRATION') - {_FRUSTRATION_DECR})"
            ]},
        },
        {"math": ["global_nemtrack_msg_pending = global_nemtrack_msg_pending + 1"]},
    ]

    # Helper: OVR_DIST clamped to floor, used in both wander phases.
    _ovr_dist_expr = (
        f"max({_OVR_DIST_MIN}, u_effect_intensity('NEMESIS_OVR_DIST'))"
    )

    # Phase 1 — disciplined tracker: wander within OVR_DIST of the last known player
    # position.  The Nemesis is methodically combing the area where it last saw you.
    _wander_phase1: list[dict[str, Any]] = [
        {
            "location_variable_adjust": {"u_val": "nemesis_last_tracked_pos"},
            "output_var":   {"u_val": "nemesis_wander_target"},
            "overmap_tile": True,
            "x_adjust": {"math": [f"rng(-({_ovr_dist_expr}), {_ovr_dist_expr})"]},
            "y_adjust": {"math": [f"rng(-({_ovr_dist_expr}), {_ovr_dist_expr})"]},
        },
        {
            "u_set_goal": {"om_terrain": "field", "var": {"u_val": "nemesis_wander_target"}},
        },
    ]

    # Phase 2 — animal drift: wander within OVR_DIST of the NPC's own current position.
    # The Nemesis has lost patience with the last-known position and now roams erratically
    # from wherever it happens to be — unpredictable but easier to outlast.
    _wander_phase2: list[dict[str, Any]] = [
        # Stamp own current position into a scratch variable before adjusting it.
        {
            "u_location_variable": {"u_val": "nemesis_self_pos"},
            "min_radius": 0,
            "max_radius": 0,
        },
        {
            "location_variable_adjust": {"u_val": "nemesis_self_pos"},
            "output_var":   {"u_val": "nemesis_wander_target"},
            "overmap_tile": True,
            "x_adjust": {"math": [f"rng(-({_ovr_dist_expr}), {_ovr_dist_expr})"]},
            "y_adjust": {"math": [f"rng(-({_ovr_dist_expr}), {_ovr_dist_expr})"]},
        },
        {
            "u_set_goal": {"om_terrain": "field", "var": {"u_val": "nemesis_wander_target"}},
        },
    ]

    # Phase transition message — queued exactly once when FRUSTRATION first crosses the
    # memory threshold, signalling to the player that the Nemesis has lost discipline.
    _phase_shift_msg: list[dict[str, Any]] = [
        {"math": [
            "global_nemtrack_phase_shift_pending = global_nemtrack_phase_shift_pending + 1"
        ]},
    ]

    # memory_threshold = (INT + PER) * _TRACKER_MEMORY_FRACTION
    # Represented as integer math: FRUSTRATION * (1 / fraction) >= INT + PER
    # With fraction = 0.25: FRUSTRATION * 4 >= INT + PER.
    _memory_divisor: int = round(1 / _TRACKER_MEMORY_FRACTION)  # 4 with default 0.25

    _wander: list[dict[str, Any]] = [
        # Increment frustration first so the phase check below uses the updated value.
        {
            "u_add_effect": "NEMESIS_FRUSTRATION",
            "duration":     "PERMANENT",
            "intensity": {"math": [
                f"max(u_effect_intensity('NEMESIS_FRUSTRATION'), 0) + {_FRUSTRATION_INCR}"
            ]},
        },
        # Select wander phase based on frustration relative to NPC stats.
        # Phase 1 (disciplined tracker): FRUSTRATION * divisor < INT + PER
        # Phase 2 (animal drift):        FRUSTRATION * divisor >= INT + PER
        {
            "if": {"math": [
                f"u_effect_intensity('NEMESIS_FRUSTRATION') * {_memory_divisor} < "
                f"u_val('intelligence') + u_val('perception')"
            ]},
            "then": _wander_phase1,
            "else": [
                # Queue the phase-shift message only on the exact tick of transition —
                # when FRUSTRATION * divisor just reached INT + PER (i.e. equals it within
                # one increment).  This avoids spamming the message on subsequent ticks.
                {
                    "if": {"math": [
                        f"u_effect_intensity('NEMESIS_FRUSTRATION') * {_memory_divisor} < "
                        f"u_val('intelligence') + u_val('perception') + {_FRUSTRATION_INCR}"
                    ]},
                    "then": _phase_shift_msg,
                },
                *_wander_phase2,
            ],
        },
        # Dread check — if the player is within this Nemesis's stat-based dread range,
        # signal the avatar branch to attempt a dread message on its next tick.
        # dread_range = max(_DREAD_RANGE_MIN, (INT+PER) / _DREAD_RANGE_DIVISOR).
        # The computed range is stored globally so the avatar branch can read it.
        {
            "math": [
                f"global_nemesis_dread_range = "
                f"max({_DREAD_RANGE_MIN}, "
                f"(u_val('intelligence') + u_val('perception')) / {_DREAD_RANGE_DIVISOR})"
            ]
        },
        {
            "if": {"math": ["global_nemtrack_omt_dist <= global_nemesis_dread_range"]},
            "then": [{"math": ["global_nemesis_close = 1"]}],
        },
    ]

    _give_up: list[dict[str, Any]] = [
        # Drop active hunt, enter ambush state with a reduced OVR_DIST radius.
        # Frustration is zeroed here so the ambush state carries no stale counter.
        # Both re-trigger paths also clear it, but explicit reset here makes the
        # invariant "ambush state = zero frustration" hold unconditionally.
        {"u_lose_effect": "NEMESIS_HUNTING"},
        {"u_lose_effect": "NEMESIS_FRUSTRATION"},
        {"u_add_effect": "NEMESIS_AMBUSH",   "duration": "PERMANENT", "intensity": 1},
        {"u_add_effect": "NEMESIS_OVR_DIST", "duration": "PERMANENT",
         "intensity": _AMBUSH_OVR_DIST},
        {"math": ["global_nemtrack_ambush_pending = global_nemtrack_ambush_pending + 1"]},
    ]

    # Ambush re-arm — fires when the player enters _AMBUSH_OVR_DIST of a waiting Nemesis.
    # Mirrors the EOC_NEMESIS_REVENGE_TRIGGER logic: set last-known-pos to player pos,
    # restore full OVR_DIST, clear frustration, then add HUNTING.
    _ambush_rearm: list[dict[str, Any]] = [
        {"u_lose_effect": "NEMESIS_AMBUSH"},
        {
            "location_variable_adjust": {"global_val": "global_nemesis_player_ms_pos"},
            "output_var": {"u_val": "nemesis_last_tracked_pos"},
        },
        {"u_add_effect": "NEMESIS_HUNTING",   "duration": "PERMANENT"},
        {"u_add_effect": "NEMESIS_OVR_DIST",  "duration": "PERMANENT",
         "intensity": _OVR_DIST_START},
        {"u_lose_effect": "NEMESIS_FRUSTRATION"},
        {"math": ["global_nemtrack_resume_pending = global_nemtrack_resume_pending + 1"]},
    ]

    return {
        "type":           "effect_on_condition",
        "id":             "EOC_NEMESIS_TRACKER",
        "eoc_type":       "RECURRING",
        "global":         True,
        "run_for_npcs":   True,
        "recurrence":     [f"{_TRACKER_INTERVAL_MIN} minutes", f"{_TRACKER_INTERVAL_MAX} minutes"],
        "condition": {"u_has_trait": "NEMESIS_MARK"},
        "effect": [
            # Ambush mode — runs when the Nemesis gave up the active hunt.
            # Computes distance to the player each tick; if within _AMBUSH_OVR_DIST OMTs,
            # the proximity re-trigger fires and the full hunt resumes.
            {
                "if": {"u_has_effect": "NEMESIS_AMBUSH"},
                "then": [
                    {"math": [
                        "global_nemtrack_omt_dist = "
                        "abs(u_val('pos_x') / 24 - global_player_omt_x) + "
                        "abs(u_val('pos_y') / 24 - global_player_omt_y)"
                    ]},
                    {
                        "if": {"math": [
                            f"global_nemtrack_omt_dist <= {_AMBUSH_OVR_DIST}"
                        ]},
                        "then": _ambush_rearm,
                    },
                ],
            },
            # Active hunt loop
            {
                "if": {"u_has_effect": "NEMESIS_HUNTING"},
                "then": [
                    {"math": [
                        "global_nemtrack_omt_dist = "
                        "abs(u_val('pos_x') / 24 - global_player_omt_x) + "
                        "abs(u_val('pos_y') / 24 - global_player_omt_y)"
                    ]},
                    {"math": [
                        f"global_nemtrack_score = "
                        f"max(0, u_val('perception') - global_nemtrack_omt_dist * 3) "
                        f"* {_VISION_WEIGHT}"
                    ]},
                    {
                        "if": {
                            "and": [
                                {"math": ["global_nemesis_noise_level > 0"]},
                                {"not": {"u_has_flag": "DEAF"}},
                            ]
                        },
                        "then": [{"math": [
                            f"global_nemtrack_score = global_nemtrack_score + "
                            f"global_nemesis_noise_level * {_NOISE_WEIGHT}"
                        ]}],
                    },
                    {
                        "if": {
                            "or": [
                                {
                                    "and": [
                                        {"math": [
                                            f"global_nemtrack_omt_dist <= {_OVR_DIST_MIN}"
                                        ]},
                                        {"math": ["global_nemtrack_score > 0"]},
                                    ]
                                },
                                {"math": [
                                    f"global_nemtrack_score >= {_HOT_THRESHOLD}"
                                ]},
                            ]
                        },
                        "then": _hot,
                        "else": [
                            {
                                "if": {"math": ["global_player_in_safe_zone == 1"]},
                                "then": _give_up,
                                "else": [
                                    {
                                        "if": {"math": [
                                            "u_effect_intensity('NEMESIS_FRUSTRATION') * 2 < "
                                            "u_val('intelligence') + u_val('perception')"
                                        ]},
                                        "then": _wander,
                                        "else": _give_up,
                                    }
                                ],
                            }
                        ],
                    },
                ],
            },
        ],
        "false_effect": [
            {
                "if": "u_is_avatar",
                "then": [
                    {
                        "u_location_variable": {
                            "global_val": "global_nemesis_player_ms_pos"
                        },
                        "min_radius": 0,
                        "max_radius": 0,
                    },
                    {"math": ["global_player_omt_x = u_val('pos_x') / 24"]},
                    {"math": ["global_player_omt_y = u_val('pos_y') / 24"]},
                    {
                        "if":   {"math": ["global_nemesis_noise_level > 0"]},
                        "then": [{"math": [
                            f"global_nemesis_noise_level = "
                            f"max(0, global_nemesis_noise_level - {_ACOUSTIC_NOISE_DECAY})"
                        ]}],
                    },
                    {
                        "if":   _safe_zone_cond,
                        "then": [{"math": ["global_player_in_safe_zone = 1"]}],
                        "else": [{"math": ["global_player_in_safe_zone = 0"]}],
                    },
                    {
                        # Dread message dispatch — fires once per tracker tick when a
                        # hunting Nemesis is within its stat-based dread range.
                        # Dread score = player PER + survival * 2 + morale bonus.
                        # Higher score = more sensitive player = message fires at longer range.
                        # Three message tiers give graduated feedback without spoiling exact
                        # Nemesis position.
                        "if": {"math": ["global_nemesis_close == 1"]},
                        "then": [
                            {"math": ["global_nemesis_close = 0"]},
                            {"math": [
                                "global_nemesis_dread_score = "
                                "u_val('perception') + u_skill('survival') * 2"
                            ]},
                            {
                                "if": {"math": ["u_val('morale') > 0"]},
                                "then": [{"math": [
                                    "global_nemesis_dread_score = "
                                    "global_nemesis_dread_score + 2"
                                ]}],
                            },
                            {
                                "if": {"math": [
                                    f"global_nemesis_dread_score >= {_DREAD_SCORE_HIGH}"
                                ]},
                                "then": [{"math": [
                                    "global_nemtrack_dread_high_pending = "
                                    "global_nemtrack_dread_high_pending + 1"
                                ]}],
                                "else": [
                                    {
                                        "if": {"math": [
                                            f"global_nemesis_dread_score >= {_DREAD_SCORE_MID}"
                                        ]},
                                        "then": [{"math": [
                                            "global_nemtrack_dread_mid_pending = "
                                            "global_nemtrack_dread_mid_pending + 1"
                                        ]}],
                                        "else": [
                                            {
                                                "if": {"math": [
                                                    f"global_nemesis_dread_score >= {_DREAD_SCORE_LOW}"
                                                ]},
                                                "then": [{"math": [
                                                    "global_nemtrack_dread_low_pending = "
                                                    "global_nemtrack_dread_low_pending + 1"
                                                ]}],
                                            }
                                        ],
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ],
    }


# ---------------------------------------------------------------------------
# Phase 3 (per-character EOC builders)
# ---------------------------------------------------------------------------

def build_spawn_check_eoc(
    template_id: str,
    unique_id: str,
    safe_name: str,
    delay_days: int,
    location_cond: dict[str, Any] | None = None,
    death_day: int | None = None,
    days_per_year: int = _DEFAULT_SEASON_DAYS * 4,
) -> dict[str, Any]:
    """Build EOC_NEMESIS_SPAWN_CHECK_<safe_name> — ACTIVATION, routed by day hubs.

    spawn lifecycle (global_nemesis_spawned_{safe}):
      0 = unarmed; false_effect records char_start on first routing call
      1 = armed; all gates must pass to spawn
      2 = spawned or dead; permanently skipped
    """
    spawned_var    = f"global_nemesis_spawned_{safe_name}"
    char_start_var = f"global_nemesis_char_start_{safe_name}"

    conditions: list[Any] = [
        {"math": ["global_nemesis_spawn_lock == 0"]},
        {"math": [f"{spawned_var} == 1"]},
        {"math": [f"time_since({char_start_var}) >= time('{delay_days} days')"]},
    ]
    if location_cond is not None:
        conditions.append(location_cond)
    if death_day is not None:
        conditions.append({"math": [
            f"time('now') % time('{days_per_year} days') >= time('{death_day} days')"
        ]})

    return {
        "type":     "effect_on_condition",
        "id":       f"EOC_NEMESIS_SPAWN_CHECK_{safe_name}",
        "eoc_type": "ACTIVATION",
        "condition": {"and": conditions},
        "effect": [
            {
                "u_spawn_npc": template_id,
                "unique_id":   unique_id,
                "real_count":  1,
                "min_radius":  40,
                "max_radius":  55,
                "true_eocs":   [f"EOC_NEMESIS_SPAWN_SUCCESS_{safe_name}"],
            }
        ],
        "false_effect": [
            {
                "if": {"math": [f"{spawned_var} == 0"]},
                "then": [
                    {"math": [f"{spawned_var} = 1"]},
                    {"math": [f"{char_start_var} = time('now')"]},
                ],
            }
        ],
    }


def build_wound_eoc(
    safe_name: str,
    bp_ratios: dict[str, float],
) -> dict[str, Any]:
    """Build EOC_NEMESIS_WOUNDS_<safe_name> — applies death wounds to the spawned NPC.

    Called via u_run_npc_eocs from EOC_NEMESIS_SPAWN_SUCCESS (NPC is 'u' there).
    Body parts at ratio >= 1.0 are skipped as a no-op.
    """
    effects: list[dict[str, Any]] = []
    for bp_id, ratio in sorted(bp_ratios.items()):
        if ratio >= 1.0:
            continue
        ratio_str = f"{ratio:.6g}"
        effects.append({
            "math": [f"u_hp('{bp_id}') = u_hp_max('{bp_id}') * {ratio_str}"]
        })
    return {
        "type":     "effect_on_condition",
        "id":       f"EOC_NEMESIS_WOUNDS_{safe_name}",
        "eoc_type": "ACTIVATION",
        "effect":   effects,
    }


def build_effects_eoc(
    safe_name: str,
    spawn_effects: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build EOC_NEMESIS_EFFECTS_<safe_name> — applies spawn-time u_add_effect entries.

    An empty effect list is valid; no conditional call needed at the spawn-success site.
    """
    return {
        "type":     "effect_on_condition",
        "id":       f"EOC_NEMESIS_EFFECTS_{safe_name}",
        "eoc_type": "ACTIVATION",
        "effect":   list(spawn_effects),
    }


def build_spawn_success_eoc(
    safe_name: str,
    char_name: str,
    unique_id: str,
    wound_eoc_id: str,
    effects_eoc_id: str,
    per_threshold: int = 10,
) -> dict[str, Any]:
    """Build EOC_NEMESIS_SPAWN_SUCCESS_<safe_name> — post-spawn finalization.

    Sets spawned_{safe} = 2, stamps the cross-Nemesis cooldown, shows a
    perception-gated warning, and applies wounds + spawn effects to the NPC.
    """
    spawned_var  = f"global_nemesis_spawned_{safe_name}"
    safe_display = _sanitize_markup(char_name)

    effect: list[dict[str, Any]] = [
        {"math": [f"{spawned_var} = 2"]},
        {"math": ["global_nemesis_last_spawn = time('now')"]},
        {"math": ["global_nemesis_spawn_lock = 1"]},
        {
            "if": {"math": [f"u_val('perception') >= {per_threshold}"]},
            "then": {
                "u_message": (
                    f"<color_red>A chill runs down your spine... "
                    f"{safe_display} has returned from the grave.</color>"
                ),
                "type": "bad",
            },
        },
        {"u_run_npc_eocs": [wound_eoc_id],   "unique_ids": [unique_id]},
        {"u_run_npc_eocs": [effects_eoc_id], "unique_ids": [unique_id]},
    ]
    return {
        "type":     "effect_on_condition",
        "id":       f"EOC_NEMESIS_SPAWN_SUCCESS_{safe_name}",
        "eoc_type": "ACTIVATION",
        "effect":   effect,
    }


def build_death_notify_eoc(
    safe_name: str,
    char_name: str,
    death_cause: DeathCause | None = None,
) -> dict[str, Any]:
    """Build EOC_NEMESIS_DEATH_NOTIFY_<safe_name> — relays the kill message to the player.

    u_message is a no-op inside the NPC's own death_eoc (u-actor is the NPC there).
    Called by EOC_NEMESIS_DEATH_RELAY on every OMT entry until just_died is cleared.
    """
    just_died_var = f"global_nemesis_just_died_{safe_name}"
    return {
        "type":     "effect_on_condition",
        "id":       f"EOC_NEMESIS_DEATH_NOTIFY_{safe_name}",
        "eoc_type": "ACTIVATION",
        "condition": {"math": [f"{just_died_var} == 1"]},
        "effect": [
            {
                "u_message": f"<color_green>{_death_narrative(_sanitize_markup(char_name), death_cause)}</color>",
                "type": "good",
            },
            {
                "u_message": "A weight lifts from your shoulders. It's over.",
                "type": "good",
            },
            {
                # Morale bonus for defeating a Nemesis — meaningful relief, not permanent power.
                # decay_start at 3 h, expires at 6 h; morale_nemesis_justice is defined in nemesis_hunting.json.
                "u_add_morale": "morale_nemesis_justice",
                "bonus": 20,
                "max_bonus": 20,
                "duration": "6 hours",
                "decay_start": "3 hours",
            },
            {"math": [f"{just_died_var} = 0"]},
        ],
    }


def _death_echo_message(char_name: str, death_cause: DeathCause | None) -> str:
    """Return the atmospheric message shown once when the player visits the Nemesis's death OMT.

    Messages are cause-specific and baked at EOC-generation time so no runtime string
    handling is required.  Each message grounds the player in the specific circumstances
    of that character's death — an echo of what happened rather than a description of the
    Nemesis itself.

    The char_name is assumed to already be sanitized (no CDDA markup).
    """
    n  = _sanitize_markup(char_name)
    dc = death_cause
    prefix = f"You stand where {n} died."

    if dc is None:
        return f"{prefix} The ground feels wrong underfoot. Something ended here."
    if dc.is_drowning:
        return f"{prefix} There's a smell of stagnant water. The air is cold and still."
    if dc.is_lava:
        return (
            f"{prefix} The rock here is different — fused, blackened. "
            f"The heat that ended {n} left its mark."
        )
    if dc.is_fire:
        return f"{prefix} The ground is scorched. You can still smell smoke, faintly."
    if dc.is_freezing:
        return f"{prefix} An unnatural chill settles over you. It was cold here when {n} fell."
    if dc.is_blood_loss:
        return f"{prefix} Rust-colored patches stain the ground. {n} bled out here."
    if dc.is_infection:
        return f"{prefix} Something smells wrong. Sweetly, sickly wrong."
    if dc.is_explosion:
        return f"{prefix} Scorch marks radiate outward from a central point. {n} didn't see it coming."
    if dc.is_acid:
        return (
            f"{prefix} The surface here is eaten away, pitted and wrong. "
            f"Whatever got {n} was thorough."
        )
    if dc.is_electric:
        return f"{prefix} Your skin prickles. The air tastes of ozone."
    if dc.is_poison:
        return f"{prefix} The vegetation nearby looks wrong. Something toxic was here."
    if dc.is_sewage:
        return f"{prefix} The smell hits you first. You try not to think about {n}'s last moments."
    if dc.is_overdose:
        return f"{prefix} An empty stillness. Whatever {n} was chasing, they found it here."
    if dc.is_starvation:
        return f"{prefix} There's nothing here. No food, no comfort. Just absence."
    if dc.is_thirst:
        return f"{prefix} Dry. Everything is dry. Even the air feels parched."
    if dc.is_exhaustion:
        return f"{prefix} You feel suddenly, inexplicably tired. Like the place remembers."
    if dc.is_headshot:
        return f"{prefix} Whatever ended {n} here was quick. The spot gives nothing away."
    if dc.is_dermatik or dc.is_parasite:
        return f"{prefix} You feel an instinctive, irrational urge to move. To not stand still."
    if dc.is_mycus:
        return f"{prefix} Fungal tendrils reach through the cracks in the ground nearby."
    if dc.is_triffid:
        return f"{prefix} The vegetation here grows wrong — dense, deliberate, like it was fed."
    if dc.is_darkwyrm or dc.is_dimension or dc.is_subspace or dc.is_teleglow:
        return f"{prefix} Reality feels thin here. Like the place remembers being somewhere else."
    if dc.is_amigara:
        return f"{prefix} There's a crack in the earth nearby. You don't want to look at it for long."
    if dc.is_nuclear:
        return f"{prefix} Your dosimeter twitches. This ground will be wrong for a long time."
    if dc.is_trap:
        return f"{prefix} You step carefully. The ground underfoot feels deliberate."
    if dc.is_autodoc:
        return f"{prefix} A faint medicinal smell. Antiseptic and copper. A procedure gone wrong."
    if dc.is_artifact:
        return f"{prefix} Your eyes slide off a shape in the corner of your vision. Something was here."
    if dc.is_mutagen:
        return f"{prefix} Something in the air makes your fingers tingle. Don't breathe deep."
    if dc.is_teleport_wall:
        return f"{prefix} The wall here has an odd texture. Like it remembers being disturbed."
    if dc.is_asthma:
        return f"{prefix} You catch yourself breathing carefully without meaning to."
    if dc.is_suicide:
        return f"{prefix} The silence here is absolute. Whatever happened, it was a choice."

    return f"{prefix} The ground feels wrong underfoot. Something ended here."


def build_death_echo_eoc(
    safe_name: str,
    char_name: str,
    death_omt: tuple[int, int, int],
    death_cause: DeathCause | None = None,
) -> dict[str, Any]:
    """Build EOC_NEMESIS_DEATH_ECHO_<safe_name> — fires once when the player visits the OMT
    where the Nemesis's original character died.

    Event type: avatar_enters_omt.  Condition chain (short-circuits in order):
      1. global_nemesis_spawned_{safe} >= 1  — Nemesis is armed; skips unarmed Nemeses
         efficiently before any coordinate math.
      2. global_nemesis_echo_seen_{safe} == 0  — fires exactly once per run.
      3. u_val('pos_x') / 24 == DEATH_X  — player at death OMT X.
      4. u_val('pos_y') / 24 == DEATH_Y  — player at death OMT Y.
      5. u_val('pos_z') == DEATH_Z       — player at death Z level.

    The atmospheric message is baked at generation time from char_name and death_cause
    (see _death_echo_message).  Seed Nemeses (death_omt is None) never generate this EOC.
    """
    spawned_var   = f"global_nemesis_spawned_{safe_name}"
    echo_seen_var = f"global_nemesis_echo_seen_{safe_name}"
    death_x, death_y, death_z = death_omt
    msg = _death_echo_message(char_name, death_cause)

    return {
        "type":           "effect_on_condition",
        "id":             f"EOC_NEMESIS_DEATH_ECHO_{safe_name}",
        "eoc_type":       "EVENT",
        "required_event": "avatar_enters_omt",
        "condition": {
            "and": [
                {"math": [f"{spawned_var} >= 1"]},
                {"math": [f"{echo_seen_var} == 0"]},
                {"math": [f"u_val('pos_x') / 24 == {death_x}"]},
                {"math": [f"u_val('pos_y') / 24 == {death_y}"]},
                {"math": [f"u_val('pos_z') == {death_z}"]},
            ]
        },
        "effect": [
            {"u_message": msg, "type": "neutral"},
            {"math": [f"{echo_seen_var} = 1"]},
        ],
    }


# ---------------------------------------------------------------------------
# Per-character overmap redirect (Option B tracking fix)
# ---------------------------------------------------------------------------

def build_hunt_redirect_eoc(
    safe_name: str,
    unique_id:  str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the two per-Nemesis EOCs that maintain overmap goal tracking for unloaded NPCs.

    BACKGROUND
    ----------
    RECURRING EOCs with run_for_npcs: true only process NPCs that are currently loaded
    into the active map (~2-3 OMTs from the player).  When the player moves further away,
    the Nemesis NPC is unloaded and the tracker stops firing for it.  The NPC physically
    moves toward its last-set goal on the overmap (CDDA simulates this), but the goal is
    never updated, so the NPC walks toward wherever the player WAS — not where they are now.

    This pair of EOCs fixes the problem by redirecting the NPC's goal on every OMT
    crossing using an EVENT EOC (avatar_enters_omt, always runs in avatar context) that
    calls u_run_npc_eocs (which can target unloaded NPCs via the overmap buffer).

    HOW IT WORKS
    ------------
    1. EOC_NEMESIS_HUNT_REDIRECT_<safe_name>: avatar_enters_omt EVENT.
       - Condition: global_nemesis_spawned_<safe_name> == 2 (alive and active).
         State 3 means dead (set by the inline NPC_DEATH EOC); state < 2 means not
         yet spawned.  Both are filtered here to avoid u_run_npc_eocs on non-existent
         or dead NPCs.  Dead NPCs remain in CDDA's unique_npcs registry (the registry
         is only cleaned by unique_npc_despawn, not by NPC death), but are removed from
         the overmap buffer.  Calling u_run_npc_eocs on a dead NPC would cause CDDA to
         emit an internal debugmsg on every OMT crossing.
       - Effect: freshens global_nemesis_player_ms_pos and global_player_omt_x/y with
         the player's current position, then calls u_run_npc_eocs to run the NPC-context
         redirect EOC on this specific Nemesis.

    2. EOC_NEMESIS_REDIRECT_NPC_<safe_name>: ACTIVATION, runs in NPC context.
       - Condition: u_has_effect("NEMESIS_HUNTING").  Passive and AMBUSH Nemeses are
         not redirected — only active hunters.
       - Effect: copies global_nemesis_player_ms_pos into nemesis_last_tracked_pos and
         calls u_set_goal.  u_set_goal directly sets guy->goal and recomputes the A*
         overmap path (verified in src/npctalk.cpp f_npc_goal) even for unloaded NPCs,
         because u_run_npc_eocs retrieves the NPC from the overmap buffer and runs the
         EOC on its raw object.  The NPC then physically walks toward the player's
         current position on the overmap, closing the distance until it loads into the
         active map and the RECURRING tracker takes over.
    """
    spawned_var = f"global_nemesis_spawned_{safe_name}"

    # Avatar-context EVENT EOC: freshen player position then run NPC redirect.
    redirect_avatar: dict[str, Any] = {
        "type":           "effect_on_condition",
        "id":             f"EOC_NEMESIS_HUNT_REDIRECT_{safe_name}",
        "eoc_type":       "EVENT",
        "required_event": "avatar_enters_omt",
        "condition": {"math": [f"{spawned_var} == 2"]},
        "effect": [
            # Freshen player position globals with the reading at this exact OMT crossing.
            # The RECURRING tracker also stamps these every 60 min, but they can be up to
            # 60 min stale on a given OMT crossing — freshening here guarantees the NPC
            # always receives the player's actual current position, not a cached one.
            {
                "u_location_variable": {"global_val": "global_nemesis_player_ms_pos"},
                "min_radius": 0,
                "max_radius": 0,
            },
            {"math": ["global_player_omt_x = u_val('pos_x') / 24"]},
            {"math": ["global_player_omt_y = u_val('pos_y') / 24"]},
            # Run the goal-redirect from the NPC's own context.  u_run_npc_eocs without
            # "local: true" searches the overmap buffer by unique_id, which finds unloaded
            # NPCs (verified in src/overmapbuffer.cpp find_npc_by_unique_id).
            {
                "u_run_npc_eocs": [f"EOC_NEMESIS_REDIRECT_NPC_{safe_name}"],
                "unique_ids":     [unique_id],
            },
        ],
    }

    # NPC-context ACTIVATION EOC: update goal to player's current position.
    # This mirrors the _hot trail goal-update in build_tracker_eoc() exactly.
    redirect_npc: dict[str, Any] = {
        "type":     "effect_on_condition",
        "id":       f"EOC_NEMESIS_REDIRECT_NPC_{safe_name}",
        "eoc_type": "ACTIVATION",
        "condition": {"u_has_effect": "NEMESIS_HUNTING"},
        "effect": [
            # Copy global player position into this NPC's last-tracked position variable.
            {
                "location_variable_adjust": {"global_val": "global_nemesis_player_ms_pos"},
                "output_var": {"u_val": "nemesis_last_tracked_pos"},
            },
            # Set overmap goal.  For unloaded NPCs this directly updates guy->goal and
            # recomputes the A* path so the NPC walks toward the player on the overmap.
            {
                "u_set_goal": {
                    "om_terrain": "field",
                    "var": {"u_val": "nemesis_last_tracked_pos"},
                }
            },
        ],
    }

    return redirect_avatar, redirect_npc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_per_char_eocs(char_data: NemesisCharData) -> list[dict[str, Any]]:
    """Build all per-character EOC JSON objects for one Nemesis.

    Returns 7 EOC dicts normally, 8 when char_data.death_omt is set:
      [spawn_check, spawn_success, death_notify, wound_eoc, effects_eoc,
       hunt_redirect_avatar, redirect_npc,
       (death_echo_eoc if death_omt is not None)]

    The hunt_redirect pair implements Option B overmap tracking: it fires on every
    OMT crossing and calls u_run_npc_eocs to redirect the Nemesis's overmap goal to
    the player's current position, even when the NPC is unloaded.  See
    build_hunt_redirect_eoc() for full design notes.

    These are concatenated with the npc_json from nemesis_builder.generate_npc_data()
    and written to nemesis_<safe_name>.json by the launcher.
    """
    wound_eoc   = build_wound_eoc(char_data.safe_name, char_data.bp_ratios)
    spawn_effects = char_data.death_cause.spawn_effects + [
        {"u_learn_martial_art": style_id}
        for style_id in char_data.martial_arts
    ]
    effects_eoc = build_effects_eoc(char_data.safe_name, spawn_effects)
    redirect_avatar, redirect_npc = build_hunt_redirect_eoc(
        char_data.safe_name,
        char_data.unique_id,
    )

    eocs: list[dict[str, Any]] = [
        build_spawn_check_eoc(
            char_data.tmpl_id,
            char_data.unique_id,
            char_data.safe_name,
            char_data.delay_days,
            location_cond = char_data.location_cond,
            death_day     = char_data.death_day,
            days_per_year = char_data.days_per_year,
        ),
        build_spawn_success_eoc(
            char_data.safe_name,
            char_data.char_name,
            char_data.unique_id,
            wound_eoc_id   = wound_eoc["id"],
            effects_eoc_id = effects_eoc["id"],
        ),
        build_death_notify_eoc(char_data.safe_name, char_data.char_name, char_data.death_cause),
        wound_eoc,
        effects_eoc,
        redirect_avatar,
        redirect_npc,
    ]

    if char_data.death_omt is not None:
        eocs.append(build_death_echo_eoc(
            char_data.safe_name,
            char_data.char_name,
            char_data.death_omt,
            char_data.death_cause,
        ))

    return eocs


def compile_master(mod_dir: str, master_out_path: str) -> None:
    """Scan all nemesis_*.json files in mod_dir and write the master routing JSON.

    Produces nemesis_master.json containing:
      - EOC_NEMESIS_MASTER        (EVENT, 1-in-100 + 1-hour cooldown, routes to day hubs)
      - EOC_NEMESIS_DEATH_RELAY   (EVENT, unconditional, relays death messages)
      - EOC_NEMESIS_FLAG_*×4      (EVENT, attack-flag setters)
      - EOC_NEMESIS_REVENGE_TRIGGER (EVENT, arms NEMESIS_HUNTING on hit)
      - EOC_NEMESIS_HUNT_ANNOUNCER  (RECURRING, delivers queued messages to player)
      - EOC_NEMESIS_ACOUSTIC_*×2    (EVENT, noise stamps)
      - EOC_NEMESIS_TRACKER         (RECURRING, overmap hunt core loop)
      - EOC_HUB_*                   (ACTIVATION, per-day routing hubs)
    """
    nemeses = scan_nemesis_files(mod_dir)

    if not nemeses:
        print(
            "NEM: No nemesis_<name>.json files found — writing empty routing tree.",
            file=sys.stderr,
        )

    hub_eocs, hub_keys = build_hub_eocs(nemeses) if nemeses else ([], [])

    death_notify_ids: list[str] = [
        n.death_notify_id for n in nemeses if n.death_notify_id is not None
    ]

    master           = build_master_eoc(hub_keys)
    relay            = build_death_relay_eoc(death_notify_ids)
    attack_flag_eocs = build_player_attack_flag_eocs()
    revenge_trigger  = build_revenge_trigger()
    hunt_announcer   = build_hunt_announcer()
    acoustic_smash   = build_acoustic_trigger_smash()
    acoustic_ranged  = build_acoustic_trigger_ranged()
    tracker          = build_tracker_eoc()

    output: list[dict[str, Any]] = (
        [master, relay]
        + attack_flag_eocs
        + [
            revenge_trigger, hunt_announcer,
            acoustic_smash, acoustic_ranged,
            tracker,
        ]
        + hub_eocs
    )

    with open(master_out_path, "w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    year_lengths = {k.days_per_year for k in hub_keys}
    print(f"NEM: nemesis_master.json written → {master_out_path}")
    print(f"  Nemeses         : {len(nemeses)}")
    print(f"  Hubs generated  : {len(hub_eocs)}")
    print(f"  Death notifiers : {len(death_notify_ids)}")
    print(f"  Year length(s)  : {sorted(year_lengths) if year_lengths else 'n/a'}")
    print(f"  EVENT listeners : 9  (MASTER + DEATH_RELAY + FLAG×4 + REVENGE + ACOUSTIC×2)")
    print(f"  RECURRING       : 2  (HUNT_ANNOUNCER + TRACKER [NEM_P5 overmap hunt])")
    if hub_keys:
        print("  Hub routing:")
        for key in sorted(hub_keys, key=lambda k: (k.days_per_year, k.death_day)):
            label = f"day {key.death_day}/{key.days_per_year}"
            print(f"    {key.hub_id:35s} → {label}")
