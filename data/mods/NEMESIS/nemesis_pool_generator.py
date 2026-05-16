#!/usr/bin/env python3
"""nemesis_pool_generator.py — Maintenance tool that auto-builds nemesis_pools.py.

PURPOSE
-------
The NEMESIS mod needs curated lists of CDDA items grouped by narrative role
(what a Tier-1 civilian might wear vs. what a Tier-4 apex survivor carries).
Maintaining these lists by hand is impractical: CDDA has 10 000+ items and
gains new ones with every update.  This script automates the classification by
reading raw CDDA JSON directly from data/json/items/, applying heuristic rules
per category, and writing a new nemesis_pools.py.

It is a *maintenance* tool, not part of the normal gameplay pipeline.  Run it
manually whenever CDDA is updated, review the diff, extend the blocklist as
needed, and commit the result.

WORKFLOW
--------
1. Run:  python3 nemesis_pool_generator.py
2. Review changes:  git diff nemesis_pools.py
3. If bad items appeared, add their IDs to nemesis_pools_blocklist.txt and
   re-run until the pool looks clean.
4. Regenerate seeds and master:
       python3 nemesis_seeder.py
       python3 nemesis_launcher.py

CDDA ITEM FORMAT
----------------
CDDA uses two formats that both need to be handled:

  Old format:  "type": "ARMOR"   (or "GUN", "TOOL", "COMESTIBLE", etc.)
  New format:  "type": "ITEM", "subtypes": ["ARMOR", ...]

Many items inherit fields from a parent via "copy-from": "<parent_id>".
Inheritance can be several levels deep.  The _resolve() function walks the
copy-from chain to find any field, which is essential for correct
classification — e.g. a specific pistol variant inherits skill: "pistol"
from its base item and would be missed without inheritance resolution.

AUTO-MANAGED CATEGORIES  (fully replaced each run)
---------------------------------------------------
  WORN_CIVILIAN    Cotton/wool/denim civilian clothing
  WORN_OUTDOOR     Leather/canvas/neoprene outdoor gear
  WORN_MILITARY    Nylon/kevlar/ceramic tactical clothing
  WORN_HAZMAT      Chemical/gas-proof suits

  CARRY_FOOD       Non-perishable, non-alien FOOD comestibles
  CARRY_MEDICAL_BASIC  Over-the-counter medicine (aspirin, bandages)
  CARRY_MEDICAL_ADV    Strong/injectable drugs (morphine, adrenaline)
  CARRY_TOOLS      Items with useful crafting qualities (CUT, HAMMER, etc.)

  WEAPON_PISTOL    GUN items with skill "pistol"
  WEAPON_SHOTGUN   GUN items with skill "shotgun"
  WEAPON_SMG       GUN items with skill "smg"
  WEAPON_RIFLE     GUN items with skill "rifle"

  WEAPON_AMMO      Dict: gun_id -> (ammo_item_id, [(mag_id, capacity), ...])
                   Sorted ascending by capacity to support tier scaling.

MANUALLY MANAGED CATEGORIES  (hardcoded in _MANUAL — edit this file)
---------------------------------------------------------------------
  WORN_POLICE      Police uniforms, riot gear, tactical holsters
  WORN_SURVIVOR    Crafted survivor armor (lsurvivor, hsurvivor, etc.)

  CARRY_CIVILIAN   Everyday items too heterogeneous for heuristic detection
  CARRY_MILITARY   MREs, military equipment, explosives, military medicine
  CARRY_SCIENCE    Lab instruments, detection gear, research consumables

  WEAPON_MELEE_LIGHT   One-handed melee weapons
  WEAPON_MELEE_HEAVY   Two-handed / high-damage melee
  WEAPON_MELEE_MARTIAL Martial arts weapons (katana, zweihander)
  WEAPON_SPECIAL       Exotic ranged weapons (coilgun, plasma pistol)

  _MANUAL_WEAPON_AMMO  Ammo entries for WEAPON_SPECIAL guns (same format as
                       auto WEAPON_AMMO entries).
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

MOD_DIR    = Path(__file__).parent
ITEMS_DIR  = MOD_DIR.parent.parent / "json" / "items"   # data/mods/NEMESIS -> data/mods -> data -> data/json/items
POOLS_FILE = MOD_DIR / "nemesis_pools.py"
BLOCKLIST  = MOD_DIR / "nemesis_pools_blocklist.txt"


# ---------------------------------------------------------------------------
# Manually maintained categories
#
# These categories are too heterogeneous, too small, or too faction-specific
# for reliable heuristic classification.  Edit the tuples below whenever the
# game gains relevant new items.  The generator writes them verbatim into
# nemesis_pools.py without modification.
# ---------------------------------------------------------------------------

_MANUAL: dict[str, tuple[str, ...]] = {
    "WORN_POLICE": (
        # Uniform
        "leather_police_jacket", "officer_uniform", "police_breeches",
        "motor_police_boots", "police_belt", "jacket_leather",
        # Tactical / protective
        "chestrig", "pants_tactical", "boots_combat",
        "ballistic_vest_light", "ballistic_vest_heavy",
        "holster", "gloves_tactical", "gloves_leather",
        # Riot gear
        "helmet_riot", "helmet_riot_raised", "mask_rioter",
        "armor_riot", "armor_riot_arm", "armor_riot_leg", "armor_riot_torso",
    ),
    "WORN_SURVIVOR": (
        # Light survivor crafted
        "lsurvivor_suit", "lsurvivor_armor", "survivor_jumpsuit",
        "survivor_vest", "boots_survivor",
        # Heavy survivor crafted
        "survivor_suit", "hsurvivor_suit", "hsurvivor_jumpsuit",
        # Accessories
        "mask_survivor", "survivor_goggles",
        "gloves_survivor", "gloves_hsurvivor",
        # Packs / rigs
        "survivor_pack", "survivor_rig", "survivor_runner_pack",
        "helmet_eod",
    ),
    "CARRY_CIVILIAN": (
        # Fire / light
        "lighter", "matches", "flashlight",
        # Tools
        "pockknife", "multitool", "screwdriver", "wrench",
        "pliers", "hacksaw", "crowbar", "duct_tape",
        # Food / drink
        "granola", "2lcanteen", "can_beans", "can_tomato", "can_corn",
        "can_peach", "can_tuna", "can_sardine", "energy_drink",
        "water", "water_clean", "thermos", "bottle_plastic",
        # Medicine / first aid
        "adhesive_bandages", "aspirin",
        # Misc
        "rope_30", "bandana", "toilet_paper", "soap", "sunglasses",
        "pen", "bleach",
    ),
    "CARRY_MILITARY": (
        # MRE variety
        "mre_beef", "mre_chicken", "mre_beefstew",
        "mre_chilibeans", "mre_chickenpesto", "mre_cheesepizza",
        # Equipment
        "military_nvg", "geiger_off", "binoculars",
        "radio", "radio_car", "signal_flare",
        # Explosives
        "grenade", "grenade_emp", "grenade_inc", "flashbang", "c4",
        # Medicine
        "adrenaline_injector", "morphine", "iodine_crystal",
    ),
    "CARRY_SCIENCE": (
        # Detection / measurement
        "geiger_off", "thermometer", "binoculars",
        # Electronics / comms
        "radio", "multitool",
        # Lab consumables
        "syringe", "petri_dish", "iodine_crystal",
        # Lab equipment
        "chemistry_set", "microscope",
        # Security / restraints
        "e_handcuffs",
    ),
    "WEAPON_MELEE_LIGHT": (
        "knife_large", "pipe", "machete", "crowbar",
        "knife_combat", "kukri",
    ),
    "WEAPON_MELEE_HEAVY": (
        "bat", "golf_club", "fire_ax", "hatchet", "rolling_pin", "baton",
        "pike", "sword_wood",
    ),
    "WEAPON_MELEE_MARTIAL": (
        "katana", "nodachi", "zweihander",
    ),
    "WEAPON_SPECIAL": (
        "exodii_plasma_projectile", "coilgun",
    ),
}

# Ammo entries for manually managed weapons in WEAPON_SPECIAL — merged into auto
# WEAPON_AMMO each run.
#
# Format matches the auto-built entries:
#   gun_id -> (ammo_item_id, [(mag_id_or_None, capacity), ...])
#
# The mag_options list must be sorted ascending by capacity, just as the generator
# sorts auto-discovered magazines.  For exotic weapons that have only a single
# magazine (or an internal feed), the list contains exactly one element.  The
# seeder uses list position to implement tier scaling: T1 picks index 0 (smallest),
# T4 picks the last entry (largest).  A single-entry list gives the same magazine
# to all tiers and differentiates only via the tier load-factor.
_MANUAL_WEAPON_AMMO: dict[str, tuple[str, list[tuple[str | None, int]]]] = {
    # Exodii plasma pistol: caotel_plasma_ring is the only magazine (12-cell capacity).
    "exodii_plasma_projectile": ("caotel_cell", [("caotel_plasma_ring", 12)]),
    # Coilgun: internal nail magazine — treated as a detachable mag-well for spawning.
    "coilgun":                  ("nail",        [("nailmag",            50)]),
}


# ---------------------------------------------------------------------------
# Classification constants
# ---------------------------------------------------------------------------

# Body-part IDs that indicate non-human anatomy.  CDDA ARMOR items declare
# which body parts they cover in the "armor" -> "covers" array.  If any cover
# in this set appears, the item is wearable only by mutants or creatures, not
# by a human Nemesis character.
_BAD_COVERS = frozenset({
    "wing", "tail", "tentacle", "paw", "hoof", "beak", "fin",
    "antenna", "stinger", "forelimb",
})

# Standard human body-part IDs.  An ARMOR item must cover at least one of
# these to be included in any WORN category — items that only cover exotic
# parts (e.g. a tail sheath that covers only "tail") are silently skipped.
_STD_COVERS = frozenset({
    "head", "torso", "arm_l", "arm_r", "leg_l", "leg_r",
    "foot_l", "foot_r", "hand_l", "hand_r", "mouth", "eyes",
})

# ID substrings that identify items belonging to non-human factions or
# that are generated by the game engine rather than hand-placed in the world.
# Checked via substring match (any(frag in item_id ...)) so "mutant_" matches
# "mutant_cracklins", "mutant_brain", etc.  Applied to ARMOR, GUN, and FOOD
# classifiers alike.
_BAD_ID_FRAGMENTS = frozenset({
    "zombie_",    # zombie body parts / faction gear
    "mutant_",    # mutant organs / mutation-specific items
    "nether_",    # Nether Realm creatures and their drops
    "fungal_",    # Mycus / fungal faction
    "blob_",      # blob faction
    "triffid_",   # Triffid faction
    "mon_",       # monster-generated pseudo-items
    "pseudo_",    # abstract pseudo-items not meant for inventory
})

# Additional ID substrings rejected specifically for ARMOR items.
# Complements _BAD_ID_FRAGMENTS with armor-domain issues: historical/fantasy
# armors that are thematically wrong for a modern-setting Nemesis, faction
# gear from non-human factions, and broken/non-functional item states.
_BAD_ARMOR_ID_FRAGMENTS = frozenset({
    # Historical / fantasy armors — wrong era for a post-apocalyptic survivor
    "aketon_", "armguard_", "brigandine", "hauberk", "gambeson",
    "roman_", "lamellar_", "scale_arm", "ringmail", "chainmail",
    "fantasy_", "tribal_",
    # Zombie-tagged variants (some armors have _zed / _zombie suffixed copies)
    "_zed", "_zombie",
    # Faction gear not worn by human survivors
    "robofac_",   # robot faction
    "xedra_",     # Xedra alien faction
    # Broken / non-functional item states
    "broken_",
})

# Ammo types that disqualify a gun from the nemesis pools — either primitive,
# modular/non-functional, or too novelty/non-combat for a nemesis character.
_EXCLUDED_AMMO_TYPES = frozenset({
    # Black powder / primitive
    "flintlock", "percussion_cap", "black_powder",
    "bp_38", "bp_9mm", "bp_45", "bp_44", "bp_762",
    "44paper",        # Civil War paper cartridge (.44)
    "36paper",        # Civil War paper cartridge (.36 — colt_navy)
    "blunderbuss",    # historical musket
    # Modular platform (no inherent ammo — requires weapon module)
    "NULL",
    # Novelty / non-combat / unsuitable
    "BB",             # BB gun
    "airgun_pellet",  # air rifle
    "paintball",      # paintball gun
    "fishspear",      # spear gun
    "bolt_ballista",  # siege ballista
    "nl_chem_thrower_ammo",  # chem thrower
    "signal_flare",   # flare gun — not a combat weapon for nemesis
})

# Bad ID fragments for GUN items (alien/faction/bionic/mutation weapons)
_BAD_GUN_ID_FRAGMENTS = frozenset({
    "yrax_",      # Xedra alien weapons
    "xedra_",     # Xedra faction items
    "triffid_",   # plant faction
    "mi_go_",     # Mi-Go faction
    "nether_",    # Nether faction
    "blob_",      # blob faction
    "robofac_",   # robot faction
    "pamd",       # Exodii faction weapons (pamd68, pamd71z, etc.)
    "bio_",       # bionic weapons — implemented as GUN but not holdable items
    "mut_",       # mutation body-part weapons (mut_quills, mut_longpull, etc.)
})

# Material sets used to classify ARMOR items into WORN subcategories.
# CDDA stores materials as a list of strings (or dicts with a "type" key in
# newer JSON).  _norm_materials() normalises both formats to a flat list so
# these sets can be checked with a simple intersection.
#
# The classification priority in _classify_armor is:
#   HAZMAT > MILITARY > OUTDOOR > CIVILIAN
# An item made of kevlar is military even if it also has cotton lining.
_MAT_CIVILIAN = frozenset({
    # Common everyday textiles — the materials of pre-apocalypse clothing
    "cotton", "wool", "denim", "lycra", "polyester", "rayon",
    "silk", "linen", "felt", "velvet",
})
_MAT_OUTDOOR = frozenset({
    # Durable but non-tactical materials — hikers, hunters, motorcycle gear
    "leather", "canvas", "fur", "faux_fur", "neoprene", "vinyl",
})
_MAT_MILITARY = frozenset({
    # Ballistic and heat-resistant materials — body armour, tactical vests,
    # military uniforms (nomex), and EOD suits (thermo_resin, ceramic plates)
    "nylon", "kevlar", "kevlar_layered", "kevlar_rigid", "nomex",
    "thermo_resin", "ceramic",
})
_MAT_HAZMAT = frozenset({
    # Gas/chemical-proof materials — hazmat suits, rubber aprons.
    # Also checked via CHEMICAL_PROTECTION / GAS_PROOF flags, which are more
    # reliable than material alone for full-body coverage detection.
    "rubber", "plastic",
})

# Regex that matches ID substrings identifying military-grade ARMOR items
# regardless of their declared material.  This is needed because some tactical
# items use civilian materials (e.g. a cotton undershirt worn beneath armour)
# but are still inherently military.  The regex supplements _MAT_MILITARY.
_MILITARY_ID_RE = re.compile(
    r"(_army|_tactical|_combat|tacvest|molle_|eod_plate|eod_light|helmet_army|helmet_eod|"
    r"rigid_kevlar|jacket_eod|goggles_nv|plate_carrier)",
    re.I,
)

# CDDA quality IDs whose presence on a TOOL item makes it useful enough for a
# Nemesis to carry.  Quality IDs are declared in the "qualities" array of the
# item JSON.  A tool is included in CARRY_TOOLS if its quality set intersects
# this frozenset.  Purely-electric or single-purpose tools (e.g. a power drill
# with no manual mode) are not excluded here — they are rare enough in the
# wild that false positives are acceptable.
_TOOL_QUALITIES = frozenset({
    "HAMMER", "CUT", "PRY", "DIG", "SCREW", "LOCKPICK",
    "WRENCH", "SAW_WOOD", "DRILL", "PLIERS", "PUNCH", "FILE",
})

# CDDA item flags that disqualify a FOOD/MED item from any CARRY pool.
# These are checked via _resolve() so inherited flags are respected.
_BAD_FOOD_FLAGS = frozenset({
    "CANNIBALISM",          # human or demihuman flesh
    "HIDDEN_HALLU",         # has a hidden hallucinogenic effect
    "FUNGAL_VECTOR",        # spreads Mycus infection on consumption
    "PARASITE",             # contains parasites
    "INEDIBLE",             # cannot actually be eaten
    "TRADER_AVOID",         # traders refuse to stock — body parts, alien matter, rotten food
    "NUTRIENT_OVERRIDE",    # synthetic nutrition block used on unusual/abstract items
})

# Effect IDs in use_action effects array that indicate advanced/strong drugs
_ADV_MED_EFFECT_IDS = frozenset({
    "pkill3", "pkill2",   # strong painkillers (morphine/oxycodone tier)
    "stimulant",          # amphetamines, meth
    "adrenaline",         # adrenaline rush
    "speed",              # speed/amphetamine
    "diazepam",           # benzodiazepine
    "sleep",              # sedatives
    "cocaine",            # cocaine effect
})

# Item flags that directly indicate advanced/injectable drugs
_ADV_MED_FLAGS = frozenset({
    "NO_INGEST",                  # injectable — requires syringe
    "IRREPLACEABLE_CONSUMABLE",   # rare/military-grade
})

# Food-specific bad ID fragments (supplement to the global _BAD_ID_FRAGMENTS).
# These catch categories of items that pass all flag checks but are semantically
# wrong for a Nemesis inventory.  The global set already covers zombie_, mutant_,
# nether_, fungal_, triffid_, blob_.  This set adds:
#   human_/demihuman_ — organ meat from human/demihuman bodies.  These items do
#     not always carry the CANNIBALISM flag in all variants (cooked, rendered fat,
#     tallow, etc.), so flag-based filtering alone is insufficient.
#   jabberwock_ — monster-specific body parts (jabberwock heart).
#   golem_ — flesh golem parts (flesh_golem_heart).
_BAD_FOOD_ID_FRAGMENTS = frozenset({
    "human_",
    "demihuman_",
    "jabberwock_",
    "golem_",
})

# Maps CDDA ammo_type string -> preferred spawnable item ID.
# Derived from data/json/items/ammo/: the first real (non-abstract, non-reloaded)
# item that carries ammo_type=<key> is used as the canonical common round.
_AMMO_TYPE_PREFERRED: dict[str, str] = {
    # Pistol calibers
    "9mm":          "9mm",
    "45":           "45_acp",
    "40":           "40fmj",
    "38":           "38_fmj",
    "357mag":       "357mag_fmj",
    "357sig":       "357sig_fmj",
    "44":           "44fmj",
    "10mm":         "10mm_fmj",
    "50ae":         "50ae_fmj",
    "380":          "380_FMJ",
    "32":           "32_acp",
    "38super":      "38super_fmj",
    "762x25":       "762_25",
    "9x18":         "9x18mm",
    "45colt":       "45colt",
    "454":          "454_Casull",
    "460sw":        "460sw",
    "500":          "500_Magnum",
    # SMG / PDW
    "57":           "57mm",
    "46x30":        "46x30",
    "8x40mm":       "8mm_caseless",
    # Shotgun
    "shot":         "shot_00",
    "410shot":      "410shot_slug",
    # Rifle
    "223":          "223",
    "308":          "762_51",       # ammo_type "308" → item "762_51" (7.62×51 NATO)
    "762":          "762_m87",      # 7.62×39mm AK
    "762R":         "762_54R",      # 7.62×54R
    "545x39":       "545",
    "300blk":       "300blk",
    "338lapua":     "338lapua_hpbt",
    "50":           "50bmg",        # ammo_type "50" → item "50bmg"
    "22":           "22_lr",
    "3006":         "3006",
    "303":          "303_fmjbt",
    "300":          "300_winmag",
    "3030":         "3030",
    "30carbine":    "30carbine",
    "450":          "450_ftx",
    "4570":         "4570_sp",
    "450bushmaster":"450bushmaster",
    # Crossbow
    "bolt":         "bolt_steel",
    # Big game rifle
    "270win":       "270win_jsp",
    "458wm":        "458wm",
    "12mm":         "12mm",
    # Misc
    "nail":         "nail",
}


# ---------------------------------------------------------------------------
# Index builder
# ---------------------------------------------------------------------------

def _build_index() -> dict[str, dict]:
    """Scan all JSON files under ITEMS_DIR and build a flat id -> item dict.

    Both real items (with "id") and abstract base items (with "abstract") are
    indexed.  Abstracts are never spawned in-game but serve as copy-from
    parents that carry shared field values (e.g. a pistol_base abstract that
    defines skill: "pistol" for every pistol in the game).

    Files that fail to parse (malformed JSON, permission errors) are silently
    skipped so a partial CDDA checkout does not crash the generator.
    """
    index: dict[str, dict] = {}
    for path in ITEMS_DIR.rglob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, list):
            continue
        for item in data:
            if not isinstance(item, dict):
                continue
            key = item.get("id") or item.get("abstract")
            if key:
                index[key] = item
    return index


def _resolve(item_id: str, field: str, index: dict, seen: frozenset = frozenset()) -> object:
    """Return the value of field for item_id, walking the copy-from chain.

    CDDA items may omit fields that are defined on a parent item referenced by
    "copy-from": "<parent_id>".  This function walks the chain recursively
    until it finds the field or runs out of parents.

    The seen set guards against circular copy-from references (which should not
    exist in valid CDDA data, but would otherwise cause infinite recursion).

    Returns None if the field is not found anywhere in the inheritance chain.
    """
    if item_id in seen:
        return None
    item = index.get(item_id, {})
    val = item.get(field)
    if val is not None:
        return val
    parent = item.get("copy-from")
    if parent:
        return _resolve(parent, field, index, seen | {item_id})
    return None


def _subtypes(item: dict) -> list[str]:
    """Return the list of functional type strings for an item dict.

    Old CDDA format: "type": "ARMOR" -> returns ["ARMOR"].
    New CDDA format: "type": "ITEM", "subtypes": ["ARMOR", "TOOL"] -> returns
    ["ARMOR", "TOOL"].  The new format allows items to belong to multiple
    functional categories simultaneously.
    """
    t = item.get("type", "")
    if t == "ITEM":
        return item.get("subtypes", [])
    return [t] if t else []


def _norm_materials(raw: object) -> list[str]:
    """Normalise the "material" field into a flat list of material ID strings.

    CDDA uses two formats for the material field:
      Old: "material": "cotton"                    (single string)
      Old: "material": ["cotton", "leather"]       (list of strings)
      New: "material": [{"type": "cotton", "portion": 1}, ...]  (list of dicts)

    Returns a flat list of material type strings regardless of input format.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    out = []
    for m in raw:
        if isinstance(m, str):
            out.append(m)
        elif isinstance(m, dict):
            t = m.get("type")
            if t:
                out.append(t)
    return out


# ---------------------------------------------------------------------------
# Pocket-data helpers (for WEAPON_AMMO auto-build)
# ---------------------------------------------------------------------------

def _get_pockets(item_id: str, index: dict) -> list[dict]:
    pd = _resolve(item_id, "pocket_data", index)
    if pd is None:
        return []
    return [p for p in pd if isinstance(p, dict)]


def _get_mag_capacity(mag_id: str, ammo_types: list[str], index: dict) -> int:
    """Return the capacity of mag_id for the first matching ammo type."""
    for pocket in _get_pockets(mag_id, index):
        if pocket.get("pocket_type") == "MAGAZINE":
            ar = pocket.get("ammo_restriction", {})
            for at in ammo_types:
                if at in ar:
                    return ar[at]
            if ar:
                return max(ar.values())
    return 0


def _build_weapon_ammo_entry(
    gun_id: str,
    index: dict,
) -> tuple[str, list[tuple[str | None, int]]] | None:
    """Return (ammo_item_id, mag_options) for gun_id, or None if no ammo found.

    mag_options is a list of (mag_id_or_None, capacity) tuples sorted ascending
    by capacity.  Multiple entries occur when a gun accepts several different
    magazines of varying sizes (e.g. a standard 15-round pistol mag plus a
    33-round extended mag).  The seeder uses this list to implement tier scaling:
    Tier 1 picks index 0 (smallest), Tier 4 picks the last entry (largest).
    Guns with internal or fixed magazines always have exactly one entry with
    mag_id=None.

    Lookup strategy (most specific to least):
    1. Detachable mag: MAGAZINE_WELL pocket -> item_restriction lists mag IDs.
       All listed mags are resolved for capacity and deduplicated by capacity so
       that two 15-round variants collapse to a single entry.
    2. Internal mag: MAGAZINE pocket directly on the gun item.
    3. Legacy fallback: clip_size field (old JSON format, no pocket_data).
    """
    raw_ammo = _resolve(gun_id, "ammo", index)
    if not raw_ammo:
        return None
    ammo_types = [raw_ammo] if isinstance(raw_ammo, str) else list(raw_ammo)
    ammo_types = [a for a in ammo_types if a not in _EXCLUDED_AMMO_TYPES]
    if not ammo_types:
        return None

    # Resolve preferred spawnable ammo item ID from the ammo_type string.
    # CDDA ammo_type strings are abstract identifiers ("308", "9mm") that do not
    # directly name a spawnable item.  _AMMO_TYPE_PREFERRED maps each to the
    # canonical common round (e.g. "308" -> "762_51" = 7.62x51 NATO brass).
    ammo_item_id: str | None = None
    ammo_type_key: str | None = None
    for at in ammo_types:
        if at in _AMMO_TYPE_PREFERRED:
            ammo_item_id = _AMMO_TYPE_PREFERRED[at]
            ammo_type_key = at
            break
    if ammo_item_id is None:
        ammo_type_key = ammo_types[0]
        ammo_item_id = ammo_type_key

    pockets = _get_pockets(gun_id, index)

    # --- Path 1: detachable magazine (MAGAZINE_WELL pocket) --------------------
    # Collect all magazine IDs from all MAGAZINE_WELL pockets (a gun can have
    # multiple mag-wells, though this is rare).  Resolve each mag's capacity for
    # the gun's ammo type, deduplicate by capacity (keep the first mag seen for
    # each capacity), then sort ascending so the resulting list goes from smallest
    # to largest magazine.
    mag_ids: list[str] = []
    for pocket in pockets:
        if pocket.get("pocket_type") == "MAGAZINE_WELL":
            mag_ids.extend(pocket.get("item_restriction", []))
    if mag_ids:
        cap_to_mag: dict[int, str] = {}
        for mid in mag_ids:
            cap = _get_mag_capacity(mid, ammo_types, index)
            if cap > 0 and cap not in cap_to_mag:
                cap_to_mag[cap] = mid
        if cap_to_mag:
            # Sort by capacity ascending: index 0 = smallest, index -1 = largest
            mag_options = [(mid, cap) for cap, mid in sorted(cap_to_mag.items())]
            return (ammo_item_id, mag_options)
        # Mags exist but none returned a capacity — spawn with capacity 1 so the
        # gun at least appears in the pool rather than being silently dropped.
        return (ammo_item_id, [(mag_ids[0], 1)])

    # --- Path 2: internal magazine (MAGAZINE pocket on the gun itself) ---------
    # Revolvers, tube-fed shotguns, and most bolt-action rifles fall here.
    # Internal mag guns have no alternative sizes, so mag_options has one entry.
    for pocket in pockets:
        if pocket.get("pocket_type") == "MAGAZINE":
            ar = pocket.get("ammo_restriction", {})
            cap = 0
            if ammo_type_key and ammo_type_key in ar:
                cap = ar[ammo_type_key]
            elif ar:
                cap = max(ar.values())
            if cap > 0:
                return (ammo_item_id, [(None, cap)])

    # --- Path 3: legacy clip_size field (pre-pocket-data JSON format) ----------
    clip = _resolve(gun_id, "clip_size", index)
    if clip and isinstance(clip, (int, float)) and clip > 0:
        return (ammo_item_id, [(None, int(clip))])

    return None


def _build_weapon_ammo_map(
    auto: dict[str, list[str]],
    index: dict,
) -> dict[str, tuple[str, list[tuple[str | None, int]]]]:
    """Build the complete WEAPON_AMMO dict for all guns that will appear in pools.

    Covers all four auto-classified gun categories (PISTOL, SHOTGUN, SMG, RIFLE)
    plus the manually defined WEAPON_SPECIAL entries from _MANUAL_WEAPON_AMMO.
    The manual entries always win (dict.update overwrites auto-derived entries if
    there happens to be a collision, which in practice only occurs if a special
    weapon was also picked up by the auto classifier — a scenario that indicates
    the classifier filter needs tightening).

    Returns a dict mapping gun_id -> (ammo_item_id, mag_options) where mag_options
    is sorted ascending by capacity as produced by _build_weapon_ammo_entry.
    """
    ammo_map: dict[str, tuple[str, list[tuple[str | None, int]]]] = {}
    for cat in ("WEAPON_PISTOL", "WEAPON_SHOTGUN", "WEAPON_SMG", "WEAPON_RIFLE"):
        for gun_id in auto.get(cat, []):
            entry = _build_weapon_ammo_entry(gun_id, index)
            if entry:
                ammo_map[gun_id] = entry
    ammo_map.update(_MANUAL_WEAPON_AMMO)
    return ammo_map


# ---------------------------------------------------------------------------
# Classifiers
# ---------------------------------------------------------------------------

def _classify_armor(item_id: str, index: dict) -> str | None:
    """Classify an ARMOR item into a WORN subcategory, or return None to skip.

    Classification pipeline:
    1. ID fragment filters — reject faction/monster/historical gear by substring.
    2. Active-state filter — items ending in _on are the "powered on" twin of an
       _off item (e.g. thermal_outfit_on / thermal_outfit_off).  Spawning the _on
       variant directly causes CDDA to activate it immediately, which is wrong.
       We skip _on items; the _off counterpart will be picked up instead.
    3. Body-part coverage filter — the item must cover at least one standard human
       body part and must not cover any non-human anatomy.
    4. Medical device filter — splints and tourniquets are wearable items in CDDA
       but belong in CARRY_MEDICAL from a loot narrative perspective.
    5. Material / flag classification — determines the WORN subcategory:
         HAZMAT   > MILITARY > OUTDOOR > CIVILIAN  (priority order)
       An item with kevlar lining AND cotton outer is classified as MILITARY.
    """
    if any(frag in item_id for frag in _BAD_ID_FRAGMENTS):
        return None
    if any(frag in item_id for frag in _BAD_ARMOR_ID_FRAGMENTS):
        return None
    # Active-state items (e.g. thermal_outfit_on) — spawn the _off version instead.
    # Items without an _off counterpart are exotic enough to skip.
    if item_id.endswith("_on"):
        return None

    armor_segs = _resolve(item_id, "armor", index) or []
    if not armor_segs:
        return None

    all_covers: set[str] = set()
    for seg in armor_segs:
        if isinstance(seg, dict):
            all_covers.update(seg.get("covers", []))

    if all_covers & _BAD_COVERS:
        return None
    if not (all_covers & _STD_COVERS):
        return None

    flags = set(_resolve(item_id, "flags", index) or [])
    mats  = set(_norm_materials(_resolve(item_id, "material", index)))

    # Splints and tourniquets are ARMOR items in CDDA but are medical devices,
    # not clothing.  They belong in CARRY_MEDICAL pools, not WORN pools.
    if flags & frozenset({"TOURNIQUET", "SPLINT"}):
        return None

    if "CHEMICAL_PROTECTION" in flags or "GAS_PROOF" in flags:
        return "WORN_HAZMAT"
    # rubber/plastic covering most of the body → hazmat
    if mats & _MAT_HAZMAT:
        full_body = {"torso", "arm_l", "arm_r", "leg_l", "leg_r"}
        if len(all_covers & full_body) >= 3:
            return "WORN_HAZMAT"

    if mats & _MAT_MILITARY or _MILITARY_ID_RE.search(item_id):
        return "WORN_MILITARY"

    if mats & _MAT_OUTDOOR:
        return "WORN_OUTDOOR"

    if mats & _MAT_CIVILIAN:
        return "WORN_CIVILIAN"

    return None


def _classify_gun(item_id: str, skill_map: dict[str, str], index: dict) -> str | None:
    """Classify a GUN item into a WEAPON subcategory, or return None to skip.

    Classification is primarily skill-based: the "skill" field (resolved through
    the copy-from chain by classify_all's pre-pass) determines whether a gun is
    a pistol, shotgun, SMG, or rifle.

    Additional exclusion filters:
    - ID fragments from _BAD_GUN_ID_FRAGMENTS: alien/bionic/mutation/faction guns
      that are implemented as GUN items in CDDA but cannot be picked up or held
      by a human character (bio_laser_gun, mut_quills, etc.)
    - USE_UPS flag: guns powered by a UPS (universal power supply) have no
      conventional ammo and cannot be meaningfully pre-loaded for a Nemesis.
    - _EXCLUDED_AMMO_TYPES: black-powder firearms, BB guns, paintball guns,
      flare guns, modular platform receivers (ammo type "NULL"), etc.
    """
    if any(frag in item_id for frag in _BAD_GUN_ID_FRAGMENTS):
        return None
    # UPS-powered guns have no conventional ammo and cannot be pre-loaded.
    flags = set(_resolve(item_id, "flags", index) or [])
    if "USE_UPS" in flags:
        return None
    skill = skill_map.get(item_id)
    cat = {
        "pistol":  "WEAPON_PISTOL",
        "shotgun": "WEAPON_SHOTGUN",
        "smg":     "WEAPON_SMG",
        "rifle":   "WEAPON_RIFLE",
    }.get(skill)
    if cat is None:
        return None
    # Reject black-powder / primitive / non-combat ammo types.
    ammo = _resolve(item_id, "ammo", index) or []
    if isinstance(ammo, str):
        ammo = [ammo]
    if set(ammo) & _EXCLUDED_AMMO_TYPES:
        return None
    return cat


def _classify_food(item_id: str, item: dict, index: dict) -> str | None:
    # --- ID-based filters (fast path, before any JSON resolution) --------------

    # Shared bad fragments: zombie_, mutant_, nether_, fungal_, triffid_, blob_,
    # mon_, pseudo_.  These catch most faction/monster-origin items.
    if any(frag in item_id for frag in _BAD_ID_FRAGMENTS):
        return None

    # Food-specific bad fragments: human_, demihuman_, jabberwock_, golem_.
    if any(frag in item_id for frag in _BAD_FOOD_ID_FRAGMENTS):
        return None

    # Active crafting processes — items whose ID ends in _active are mid-state
    # intermediates (sourdough_young_uncovered_active, curing_roe_active, etc.).
    # They cannot be consumed as-is and should never spawn in an inventory.
    if item_id.endswith("_active"):
        return None

    # Wild creature eggs — all named egg_<animal> items are butchering byproducts
    # (egg_spider_wolf, egg_butterfly, egg_salmon, etc.).  A Nemesis would not
    # carry 130 varieties of raw animal eggs.
    # Exception: egg_salad is a prepared dish and does NOT represent a raw egg.
    if item_id.startswith("egg_") and item_id != "egg_salad":
        return None

    # --- Flag-based filters ----------------------------------------------------

    ct = _resolve(item_id, "comestible_type", index)
    if ct not in ("FOOD", "MED"):
        return None

    flags = set(_resolve(item_id, "flags", index) or [])
    if flags & _BAD_FOOD_FLAGS:
        return None

    if ct == "FOOD":
        return "CARRY_FOOD"

    # MED items: split into CARRY_MEDICAL_BASIC (aspirin, bandages) vs.
    # CARRY_MEDICAL_ADV (morphine, adrenaline, antibiotics).
    #
    # Two heuristics are used in combination because CDDA does not have a
    # single "drug strength" field:
    #
    # 1. Flag check — _ADV_MED_FLAGS contains flags that directly signal a
    #    strong drug: NO_INGEST (requires a syringe = injectable), and
    #    IRREPLACEABLE_CONSUMABLE (rare/military-grade medicine).
    #
    # 2. use_action effect scan — many drugs declare their effect via a
    #    use_action block containing an effects list.  We serialize the block
    #    to JSON and substring-search for known strong effect IDs
    #    (_ADV_MED_EFFECT_IDS).  This is intentionally loose (string search
    #    rather than proper parsing) but is fast and sufficient for the
    #    classification granularity we need.
    if flags & _ADV_MED_FLAGS:
        return "CARRY_MEDICAL_ADV"

    ua = _resolve(item_id, "use_action", index)
    if ua is not None:
        ua_str = json.dumps(ua).lower()
        for effect_id in _ADV_MED_EFFECT_IDS:
            if f'"{effect_id}"' in ua_str:
                return "CARRY_MEDICAL_ADV"

    return "CARRY_MEDICAL_BASIC"


def _classify_tool(item_id: str, index: dict) -> str | None:
    """Classify a TOOL item into CARRY_TOOLS if it has a useful crafting quality.

    CDDA tools declare their capabilities as quality entries in the "qualities"
    array.  Quality entries can appear as bare strings, [id, level] lists, or
    {"id": ..., "level": ...} dicts depending on JSON version.  We normalise
    all three and check against _TOOL_QUALITIES.

    Tools without any relevant quality (e.g. a music player or a toy) are
    returned as None and silently skipped.
    """
    qualities = _resolve(item_id, "qualities", index) or []
    names: set[str] = set()
    for q in qualities:
        if isinstance(q, (list, tuple)):
            names.add(q[0])
        elif isinstance(q, dict):
            qid = q.get("id")
            if qid:
                names.add(qid)
        elif isinstance(q, str):
            names.add(q)
    if names & _TOOL_QUALITIES:
        return "CARRY_TOOLS"
    return None


# ---------------------------------------------------------------------------
# Main classification
# ---------------------------------------------------------------------------

def classify_all(index: dict, blocklist: frozenset[str]) -> dict[str, list[str]]:
    """Classify all real items in the index into NEMESIS pool categories.

    Two-phase approach:

    Phase 1 — skill map pre-pass: GUN items are classified by their "skill"
    field (pistol, shotgun, smg, rifle), but that field is often inherited
    through copy-from chains.  Resolving it per-item inside the main loop
    would be correct but redundant.  Instead, we resolve it once for every
    GUN item and store the results in skill_map so _classify_gun can look up
    the result in O(1).

    Phase 2 — main dispatch: iterate all real items (those with an "id" field,
    not abstract parents), skip blocklisted IDs, determine functional subtypes,
    and dispatch to the appropriate classifier.  Items not matching any
    classified subtype (BOOK, BIONIC_ITEM, etc.) are silently skipped.

    Returns a dict mapping category name -> sorted list of item IDs.
    Each category list is a deterministic snapshot; sorted() ensures that
    nemesis_pools.py diffs are readable and do not fluctuate between runs.
    """
    categories: dict[str, list[str]] = defaultdict(list)

    # Pre-pass: resolve skill for all GUN items (needed by _classify_gun).
    skill_map: dict[str, str] = {}
    for k, v in index.items():
        if "id" not in v or "GUN" not in _subtypes(v):
            continue
        skill = _resolve(k, "skill", index)
        if skill:
            skill_map[k] = skill

    for item_id, item in index.items():
        if "id" not in item:
            continue
        if item_id in blocklist:
            continue

        subs = _subtypes(item)
        cat: str | None = None

        if "ARMOR" in subs:
            cat = _classify_armor(item_id, index)
        elif "GUN" in subs:
            cat = _classify_gun(item_id, skill_map, index)
        elif "COMESTIBLE" in subs:
            cat = _classify_food(item_id, item, index)
        elif "TOOL" in subs:
            cat = _classify_tool(item_id, index)

        if cat:
            categories[cat].append(item_id)

    return {cat: sorted(items) for cat, items in categories.items()}


# ---------------------------------------------------------------------------
# Blocklist reader
# ---------------------------------------------------------------------------

def _load_blocklist() -> frozenset[str]:
    """Read nemesis_pools_blocklist.txt and return the set of excluded item IDs.

    File format: one item ID per line.  Anything after a '#' character on a
    line is treated as a comment and ignored.  Blank lines are skipped.

    The blocklist is the recommended escape hatch when a heuristic classifier
    incorrectly includes an item: add the ID to the file, re-run the generator,
    and commit both the blocklist change and the regenerated pools file.
    """
    if not BLOCKLIST.exists():
        return frozenset()
    lines = BLOCKLIST.read_text(encoding="utf-8").splitlines()
    ids: set[str] = set()
    for line in lines:
        line = line.split("#")[0].strip()
        if line:
            ids.add(line)
    return frozenset(ids)


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

# Number of item IDs written per line inside tuple literals in nemesis_pools.py.
# Four per line keeps the file readable and produces manageable git diffs.
_ITEMS_PER_LINE = 4


def _fmt_tuple(name: str, items: tuple[str, ...] | list[str]) -> str:
    """Render a pool tuple as a Python variable assignment literal.

    Output format (four items per line, all quoted):
        NAME: tuple[str, ...] = (
            "item_a", "item_b", "item_c", "item_d",
            "item_e",
        )
    """
    if not items:
        return f"{name}: tuple[str, ...] = ()\n"
    lines = [f"{name}: tuple[str, ...] = (\n"]
    chunk = list(items)
    for i in range(0, len(chunk), _ITEMS_PER_LINE):
        batch = chunk[i : i + _ITEMS_PER_LINE]
        lines.append("    " + ", ".join(f'"{x}"' for x in batch) + ",\n")
    lines.append(")\n")
    return "".join(lines)


def _fmt_ammo_dict(
    name: str,
    ammo_map: dict[str, tuple[str, list[tuple[str | None, int]]]],
) -> str:
    """Render WEAPON_AMMO as a Python dict literal.

    Each entry is formatted as:
        "gun_id": ("ammo_item_id", [("mag_id", cap), ("bigger_mag", cap2)]),

    For internal-mag guns the list contains exactly one element with mag_id=None:
        "mossberg_500": ("shot_00", [(None, 6)]),

    For detachable-mag guns all distinct capacity variants appear in the list,
    sorted ascending, so the seeder can select by tier index.
    """
    if not ammo_map:
        return f"{name}: dict[str, tuple[str, list[tuple[str | None, int]]]] = {{}}\n"
    lines = [f"{name}: dict[str, tuple[str, list[tuple[str | None, int]]]] = {{\n"]
    for gun_id in sorted(ammo_map):
        ammo_id, mag_options = ammo_map[gun_id]
        opts_str = ", ".join(
            f'("{mid}", {cap})' if mid is not None else f"(None, {cap})"
            for mid, cap in mag_options
        )
        lines.append(f'    "{gun_id}": ("{ammo_id}", [{opts_str}]),\n')
    lines.append("}\n")
    return "".join(lines)


def _write_pools(
    auto: dict[str, list[str]],
    ammo_map: dict[str, tuple[str, list[tuple[str | None, int]]]],
) -> None:
    """Write the complete nemesis_pools.py file from classified item lists.

    The output file contains:
    - One tuple variable per pool category (WORN_CIVILIAN, CARRY_FOOD, etc.).
      Auto-classified categories come from the `auto` dict; manually managed
      categories are taken from the _MANUAL constant in this file.
    - Three index dicts (WORN_CATEGORIES, CARRY_CATEGORIES, WEAPON_CATEGORIES)
      that map short key strings to the tuple variables.  These are what
      nemesis_seeder.py imports and passes to _pick_from_categories().
    - WEAPON_AMMO dict mapping gun_id -> (ammo_item_id, mag_options list).

    The file is written atomically (single write_text call) to avoid leaving a
    partial file on disk if the process is interrupted.
    """
    out: list[str] = []
    out.append('#!/usr/bin/env python3\n')
    out.append('"""Item pool categories for the NEMESIS seeder.\n\n')
    out.append('AUTO-GENERATED — do not edit directly.\n')
    out.append('Run nemesis_pool_generator.py to regenerate.\n')
    out.append('Add unwanted item IDs to nemesis_pools_blocklist.txt to exclude them.\n')
    out.append('Edit nemesis_pool_generator.py to update manually managed categories.\n')
    out.append('"""\n\n')
    out.append('from __future__ import annotations\n\n')

    sections = [
        ("# Worn — auto-classified", [
            "WORN_CIVILIAN", "WORN_OUTDOOR", "WORN_MILITARY", "WORN_HAZMAT",
        ]),
        ("# Worn — manually maintained", [
            "WORN_POLICE", "WORN_SURVIVOR",
        ]),
        ("# Carry — auto-classified", [
            "CARRY_FOOD", "CARRY_MEDICAL_BASIC", "CARRY_MEDICAL_ADV", "CARRY_TOOLS",
        ]),
        ("# Carry — manually maintained", [
            "CARRY_CIVILIAN", "CARRY_MILITARY", "CARRY_SCIENCE",
        ]),
        ("# Weapons — auto-classified (ranged by skill)", [
            "WEAPON_PISTOL", "WEAPON_SHOTGUN", "WEAPON_SMG", "WEAPON_RIFLE",
        ]),
        ("# Weapons — manually maintained", [
            "WEAPON_MELEE_LIGHT", "WEAPON_MELEE_HEAVY", "WEAPON_MELEE_MARTIAL", "WEAPON_SPECIAL",
        ]),
    ]

    for comment, cat_names in sections:
        out.append(f"\n{comment}\n\n")
        for cat in cat_names:
            items = auto.get(cat) or list(_MANUAL.get(cat, ()))
            out.append(_fmt_tuple(cat, items))
            out.append("\n")

    # Index dicts
    out.append("\n# ---------------------------------------------------------------------------\n")
    out.append("# Index dicts — accessed by name in nemesis_seeder.py\n")
    out.append("# ---------------------------------------------------------------------------\n\n")
    out.append("WORN_CATEGORIES: dict[str, tuple[str, ...]] = {\n")
    for k in ["CIVILIAN", "OUTDOOR", "POLICE", "MILITARY", "HAZMAT", "SURVIVOR"]:
        out.append(f'    "{k}": WORN_{k},\n')
    out.append("}\n\n")

    out.append("CARRY_CATEGORIES: dict[str, tuple[str, ...]] = {\n")
    for k in ["CIVILIAN", "TOOLS", "MEDICAL_BASIC", "MEDICAL_ADV", "MILITARY", "FOOD", "SCIENCE"]:
        out.append(f'    "{k}": CARRY_{k},\n')
    out.append("}\n\n")

    out.append("WEAPON_CATEGORIES: dict[str, tuple[str, ...]] = {\n")
    for k in ["MELEE_LIGHT", "MELEE_HEAVY", "MELEE_MARTIAL", "PISTOL", "SHOTGUN", "SMG", "RIFLE", "SPECIAL"]:
        out.append(f'    "{k}": WEAPON_{k},\n')
    out.append("}\n\n")

    out.append("# gun_id -> (ammo_item_id, [(mag_id_or_None, capacity), ...])\n")
    out.append("# mag_options list is sorted ascending by capacity.\n")
    out.append("# The seeder picks by tier: T1 -> index 0 (smallest), T4 -> index -1 (largest).\n")
    out.append("# Auto-classified guns + manual WEAPON_SPECIAL entries.\n")
    out.append("# Guns absent from this dict spawn without preloaded ammo.\n")
    out.append(_fmt_ammo_dict("WEAPON_AMMO", ammo_map))

    POOLS_FILE.write_text("".join(out), encoding="utf-8")


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def _print_stats(
    new: dict[str, list[str]],
    ammo_map: dict[str, tuple[str, list[tuple[str | None, int]]]],
    old_pools_path: Path,
) -> None:
    """Print a before/after diff summary for all auto-managed categories.

    The old nemesis_pools.py is loaded via importlib (before being overwritten)
    so that added/removed item counts can be shown per category.  If the old
    file does not exist or fails to import (e.g. first run, syntax error in
    previous output), the diff columns are simply omitted.

    Also reports WEAPON_AMMO coverage: how many of the auto-classified guns
    have a valid ammo entry, and which ones (if any) are missing one.  Missing
    entries mean those guns will spawn without preloaded ammo in-game.
    """
    old: dict[str, list[str]] = {}
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_old_pools", old_pools_path)
        mod  = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        for cat in new:
            old_val = getattr(mod, cat, ())
            old[cat] = list(old_val)
    except Exception:
        pass  # no old file or import error — skip diff stats

    print("\nPool statistics (auto-managed categories):\n")
    auto_cats = {
        "WORN_CIVILIAN", "WORN_OUTDOOR", "WORN_MILITARY", "WORN_HAZMAT",
        "CARRY_FOOD", "CARRY_MEDICAL_BASIC", "CARRY_MEDICAL_ADV", "CARRY_TOOLS",
        "WEAPON_PISTOL", "WEAPON_SHOTGUN", "WEAPON_SMG", "WEAPON_RIFLE",
    }
    for cat in sorted(auto_cats):
        n_new = set(new.get(cat, []))
        n_old = set(old.get(cat, []))
        added   = n_new - n_old
        removed = n_old - n_new
        print(
            f"  {cat:<22s}: {len(n_new):4d} items"
            + (f"  +{len(added)}" if added else "")
            + (f"  -{len(removed)}" if removed else "")
        )

    # WEAPON_AMMO coverage: every gun in the auto categories should have an ammo
    # entry so it can be pre-loaded at spawn time.  Missing entries are printed
    # as a warning so they can be investigated and fixed.
    gun_cats = {"WEAPON_PISTOL", "WEAPON_SHOTGUN", "WEAPON_SMG", "WEAPON_RIFLE"}
    all_auto_guns = [g for c in gun_cats for g in new.get(c, [])]
    no_entry = [g for g in all_auto_guns if g not in ammo_map]
    manual = len(_MANUAL_WEAPON_AMMO)
    auto_entries = len(ammo_map) - manual
    print(
        f"  {'WEAPON_AMMO':<22s}: {len(ammo_map):4d} entries"
        f"  ({auto_entries} auto / {manual} manual)"
        + (f"  {len(no_entry)} gun(s) without ammo entry: {no_entry}" if no_entry else "")
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the full generation pipeline and write nemesis_pools.py.

    Pipeline:
    1. Load all CDDA item JSON from ITEMS_DIR into a flat index.
    2. Load the blocklist of manually excluded item IDs.
    3. Classify all items into pool categories.
    4. Build the WEAPON_AMMO dict for all classified guns.
    5. Print before/after statistics.
    6. Write the new nemesis_pools.py.
    """
    if not ITEMS_DIR.exists():
        print(
            f"Error: CDDA items directory not found at:\n  {ITEMS_DIR}\n"
            "Run this script from within the Cataclysm-DDA repository.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Loading CDDA items from {ITEMS_DIR} ...")
    index    = _build_index()
    print(f"  {len(index)} items/abstracts indexed.")

    blocklist = _load_blocklist()
    if blocklist:
        print(f"  {len(blocklist)} items in blocklist.")

    print("Classifying ...")
    auto = classify_all(index, blocklist)

    print("Building WEAPON_AMMO map ...")
    ammo_map = _build_weapon_ammo_map(auto, index)

    # Load and print diff stats before overwriting the old file.
    _print_stats(auto, ammo_map, POOLS_FILE)

    _write_pools(auto, ammo_map)
    print(f"\nWrote {POOLS_FILE}")
    print("\nNext steps:")
    print("  git diff data/mods/NEMESIS/nemesis_pools.py   -- review new items")
    print("  Add unwanted IDs to nemesis_pools_blocklist.txt, then re-run.")
    print("  python3 nemesis_seeder.py --counts 5 5 5 5 --output /tmp/nem_test")


if __name__ == "__main__":
    main()
