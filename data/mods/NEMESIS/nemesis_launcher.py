#!/usr/bin/env python3
"""nemesis_launcher.py -- Lightweight CLI entry point for the NEMESIS mod pipeline.

This script orchestrates the two-step process that turns a dead player character
into an active Nemesis NPC in the game world.

STEP 1 — Per-character file generation (only when a save file is given):
  a. nemesis_builder.generate_npc_data()  -- parse the .sav and .log files, extract
     all character data, and build the NPC-side JSON objects (faction, item groups,
     npc_class, npc_template).
  b. nemesis_eoc.build_per_char_eocs()    -- build the five per-character EOC objects
     (spawn_check, spawn_success, death_notify, wounds EOC, effects EOC).
  c. Write the combined list to nemesis_<safe_name>.json in the mod directory.

STEP 2 — Master recompilation (always runs, with or without a save file):
  nemesis_eoc.compile_master()  -- scan all nemesis_*.json files in the mod
  directory, extract routing metadata from each, group Nemeses by anniversary day,
  build hub EOCs + the master routing tree + the full hunt system, and write the
  result to nemesis_master.json.

ARCHITECTURE NOTE
-----------------
nemesis_master.json contains O(1) EVENT listeners regardless of how many Nemesis
files exist.  Two EVENTs fire on every OMT crossing (MASTER gated, DEATH_RELAY
ungated), plus four attack-flag EVENTs, two acoustic EVENTs, one revenge EVENT,
and two RECURRING EOCs (HUNT_ANNOUNCER, TRACKER).  The hub routing tree fans out
only to the Nemeses whose anniversary is currently active.
"""
#
# Usage (after a death):
#   python3 nemesis_launcher.py <path/to/character.sav> [--delay-days N] [--output PATH]
#
# Recompile master without a new death:
#   python3 nemesis_launcher.py
#
# Workflow:
#   1. If a save file is provided:
#      a. nemesis_builder.generate_npc_data()  — parse save, build NPC JSON objects
#      b. nemesis_eoc.build_per_char_eocs()    — build per-character EOC JSON objects
#      c. Write combined list to nemesis_<safe_name>.json
#   2. nemesis_eoc.compile_master()            — scan all nemesis_*.json, write nemesis_master.json
#
# Architecture (nemesis_master.json):
#   O(1) EVENT evaluations per OMT entry regardless of nemesis count.
#   Two EVENT listeners (MASTER = gated spawn routing; DEATH_RELAY = ungated death messages),
#   plus four attack-flag EVENTs, two acoustic EVENTs, one revenge EVENT, one swarm EVENT,
#   and two RECURRING EOCs (HUNT_ANNOUNCER, TRACKER).

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import nemesis_builder
import nemesis_eoc

SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
GENERATED_DIR = os.path.join(SCRIPT_DIR, "generated")
MASTER_OUT    = os.path.join(SCRIPT_DIR, "nemesis_master.json")


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------

def _positive_int(value: str) -> int:
    """argparse type converter that accepts only integers >= 1.

    Used for --delay-days so the CLI prevents zero or negative delays before the
    pipeline starts.  Raises argparse.ArgumentTypeError on invalid input.
    """
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' is not an integer")
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be ≥ 1, got {n}")
    return n


# ---------------------------------------------------------------------------
# Summary output
# ---------------------------------------------------------------------------

def _print_char_summary(
    char_data: nemesis_builder.NemesisCharData,
    mod_json: list[dict[str, Any]],
    out_path: str,
) -> None:
    """Print a human-readable summary of the generated Nemesis to stdout.

    Reads back the generated JSON from mod_json to report item counts per group
    and the full trait list.  Also decodes all active DeathCause flags into a
    comma-separated list of cause labels so the operator can verify that death
    detection worked correctly.  Warnings are printed to stderr for any JSON object
    that cannot be located by type+id in the generated list.
    """
    def _get(type_: str, id_: str) -> dict[str, Any]:
        obj = next(
            (o for o in mod_json if o.get("type") == type_ and o.get("id") == id_),
            None,
        )
        if obj is None:
            print(f"  Warning: {type_} '{id_}' not found in generated JSON", file=sys.stderr)
            return {}
        return obj

    safe  = char_data.safe_name
    cls   = _get("npc_class",  char_data.class_id)
    worn  = _get("item_group", char_data.worn_gid)
    carry = _get("item_group", char_data.carry_gid)
    weap  = _get("item_group", char_data.weapon_gid)

    death_cause = char_data.death_cause  # guaranteed non-None (DeathCause)
    cause_flags: list[str] = []
    flag_attrs: list[tuple[str, str]] = [
        ("is_fire",          "fire"),
        ("is_infection",     "infection"),
        ("is_starvation",    "starvation"),
        ("is_thirst",        "thirst"),
        ("is_suicide",       "suicide"),
        ("is_drowning",      "drowning"),
        ("is_teleport_wall", "teleport_wall"),
        ("is_blood_loss",    "blood_loss"),
        ("is_sewage",        "sewage"),
        ("is_headshot",      "headshot(proxy)"),
        ("is_dimension",     "dimension"),
        ("is_nuclear",       "nuclear"),
        ("is_lava",          "lava"),
        ("is_mycus",         "mycus"),
        ("is_trap",          "trap"),
        ("is_mutagen",       "mutagen"),
        ("is_freezing",      "freezing"),
        ("is_autodoc",       "autodoc"),
        ("is_dermatik",      "dermatik"),
        ("is_amigara",       "amigara"),
        ("is_teleglow",      "teleglow"),
        ("is_explosion",     "explosion"),
        ("is_darkwyrm",      "darkwyrm"),
        ("is_triffid",       "triffid"),
        ("is_exhaustion",    "exhaustion"),
        ("is_subspace",      "subspace"),
        ("is_artifact",      "artifact"),
        ("is_electric",      "electric"),
        ("is_asthma",        "asthma"),
        ("is_overdose",      "overdose"),
        ("is_poison",        "poison"),
        ("is_parasite",      "parasite"),
    ]
    for attr, label in flag_attrs:
        if getattr(death_cause, attr, False):
            cause_flags.append(label)
    if death_cause.parasite_effect:
        cause_flags.append(f"parasite:{death_cause.parasite_effect}")

    traits = cls.get("traits", [])
    print(f"\nNEM: wrote {out_path}")
    print(f"  Character   : {char_data.char_name!r}")
    print(f"  Safe ID     : {safe}")
    print(f"  Template    : {char_data.tmpl_id}")
    print(f"  Class       : {char_data.class_id}")
    print(f"  Delay       : {char_data.delay_days} day(s)")
    print(f"  Death causes: {', '.join(cause_flags) if cause_flags else '(generic)'}")
    print(f"  Worn items  : {len(worn.get('entries', []))}")
    print(f"  Carry items : {len(carry.get('entries', []))}")
    print(f"  Weapon items: {len(weap.get('entries', []))}")
    print(f"  Traits      : {len(traits)}")
    if traits:
        print(f"    {[t[0] for t in traits]}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point for nemesis_launcher.py.

    Parses arguments and runs the two-step pipeline: optional per-character file
    generation followed by mandatory master recompilation.  Exits with code 1 on
    any error (file not found, JSON parse failure, unexpected exception).

    Arguments
    ---------
    save_file   (positional, optional) : Path to the dead character's .sav file.
                Omit to recompile nemesis_master.json without generating a new file.
    --output    : Override the output path for the per-character JSON file.
                Default: <mod_dir>/nemesis_<safe_name>.json
    --delay-days: Override the computed spawn delay (in-game days, minimum 1).
    """
    parser = argparse.ArgumentParser(
        description=(
            "NEM_P5: Generate a per-character Nemesis NPC JSON from a CDDA save\n"
            "and recompile nemesis_master.json.\n"
            "Omit <save_file> to recompile master only."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "save_file",
        nargs="?",
        default=None,
        help="Path to the dead character's .sav file.",
    )
    parser.add_argument(
        "--output",
        default=None,
        metavar="PATH",
        help="Override destination path (default: <mod_dir>/nemesis_<safe_name>.json).",
    )
    parser.add_argument(
        "--delay-days",
        type=_positive_int,
        default=None,
        metavar="N",
        help="Override the computed spawn delay (in-game days, minimum 1).",
    )
    args = parser.parse_args()

    # --- Step 1: generate per-character file (only when save is given) ---
    if args.save_file is not None:
        if not os.path.isfile(args.save_file):
            print(f"NEM error: save file not found: {args.save_file}", file=sys.stderr)
            sys.exit(1)

        try:
            npc_json, char_data = nemesis_builder.generate_npc_data(
                args.save_file,
                delay_days_override=args.delay_days,
            )
            eoc_json = nemesis_eoc.build_per_char_eocs(char_data)
        except json.JSONDecodeError as exc:
            print(f"NEM error: could not parse save file: {exc}", file=sys.stderr)
            sys.exit(1)
        except Exception as exc:  # noqa: BLE001
            print(f"NEM error: {exc}", file=sys.stderr)
            sys.exit(1)

        combined: list[dict[str, Any]] = npc_json + eoc_json

        out_path = args.output or os.path.join(
            GENERATED_DIR, f"nemesis_{char_data.safe_name}.json"
        )
        out_dir = os.path.dirname(out_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(combined, fh, indent=2, ensure_ascii=False)
            fh.write("\n")

        _print_char_summary(char_data, combined, out_path)

    # --- Step 2: always recompile master ---
    try:
        nemesis_eoc.compile_master(GENERATED_DIR, MASTER_OUT)
    except Exception as exc:  # noqa: BLE001
        print(f"NEM error: master compilation failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
