#!/usr/bin/env python3
"""nemesis_seeder.py — Cold-start world population for the NEMESIS mod.

PURPOSE
-------
The NEMESIS system is normally driven by player death: when a character dies,
nemesis_builder.py generates a hostile NPC based on that character's save file.
However, a new world starts with no dead characters, so there are no Nemeses
until someone dies.  This script solves the "empty world" problem by generating
a full roster of synthetic Nemesis characters without needing any player input.

Each Nemesis is assigned to one of four tiers:
  T1 — Civilian     (Days 1-13):   weak, everyday gear, melee weapons
  T2 — Responder    (Days 14-59):  police/military gear, handguns, shotguns
  T3 — Elite        (Days 60-99):  tactical gear, rifles, bionics, mutations
  T4 — Apex         (Days 100+):   survivor armor, exotic weapons, full loadout

TIER SCALING
------------
Two axes of scaling distinguish the tiers:

1. Gear quality — worn categories, carry categories, and weapon categories are
   weighted per tier (_TierDef.worn_categories etc.).  T1 civilians draw from
   WORN_CIVILIAN; T4 Apex draw from WORN_SURVIVOR.

2. Weapon magazine and load — WEAPON_AMMO stores all compatible magazine sizes
   per gun sorted ascending.  The seeder selects by tier index (T1 gets the
   smallest, T4 gets the largest), then fills it according to _TIER_LOAD_FACTOR
   (T1: 25-50% full, T4: always 100% full).  See _pick_weapon_ammo().

OUTPUT
------
Each Nemesis produces one JSON file:
  nemesis_seed_<first>_<last>_<index>.json

These files are placed in the same directory as this script (or --output DIR).
After generation, run nemesis_launcher.py to rebuild nemesis_master.json, which
is what the CDDA game engine actually loads.

PUBLIC API
----------
  generate_random_nemesis(tier: int) -> NemesisCharData
  seed_nemeses(counts: tuple[int, int, int, int], out_dir: str) -> None

CLI
---
  python3 nemesis_seeder.py [--counts N1 N2 N3 N4] [--output DIR] [--seed SEED]
  --counts N1 N2 N3 N4  Nemeses per tier T1-T4 (default: 15 10 10 5, total 40)
  --seed SEED           Fix the random seed for reproducible output
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from dataclasses import dataclass
from typing import Any

from nemesis_builder import (
    CDDA_BASE_STAT,
    FALLBACK_CARRY,
    FALLBACK_WEAPON,
    FALLBACK_WORN,
    NemesisCharData,
    DeathCause,
    _DEFAULT_SEASON_DAYS,
    _SKIP_BIONICS,
    _SKIP_SKILLS,
    build_faction,
    build_item_group,
    build_memento_item,
    build_npc_class,
    build_npc_template,
    build_narrative_description,
    make_safe_id,
)
from nemesis_eoc import build_per_char_eocs
from nemesis_pools import WORN_CATEGORIES, CARRY_CATEGORIES, WEAPON_CATEGORIES, WEAPON_AMMO

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# Item classification — count_by_charges items must never carry a damage field.
#
# C++ reality (itype.h): count_by_charges() items have damage_max() == 0, so
# damage_level(d) returns 5 (= destroyed / not spawned) for any nonzero d.
# ---------------------------------------------------------------------------

_COUNT_BY_CHARGES_TYPES: frozenset[str] = frozenset({
    # Ammo
    "ammo_9mm_fmj", "ammo_9mm", "ammo_45_acp", "ammo_308", "ammo_223",
    "ammo_762_51", "ammo_762_39", "ammo_12ga", "ammo_00_shot",
    "ammo_plasma", "ammo_laser", "ammo_fusion",
    # Food & drink
    "can_beans", "water", "granola", "mre_beef", "mre_chicken",
    "energy_drink", "flask_hip",
    # Medicine & consumables
    "oxycodone", "aspirin", "morphine", "adrenaline_injector", "iodine_crystal",
    "bandages", "tourniquet_upper", "royal_jelly",
    # Misc consumables
    "matches", "battery",
})


# ---------------------------------------------------------------------------
# Synthetic item helper
# ---------------------------------------------------------------------------

def _item(
    typeid: str,
    damage: int = 0,
    charges: int = 0,
    filthy: bool = False,
    ammo_item: str | None = None,
) -> dict[str, Any]:
    """Build a save-style item dict for passing to build_item_group().

    damage is a CDDA damage LEVEL 0-4 (not the raw per-mille value used
    internally by the engine).  Level 0 = pristine, level 4 = about to break.

    charges, if > 0, sets the item's charge count (ammo rounds, uses remaining,
    etc.).  For count_by_charges items this is required to spawn them at all.

    ammo_item, if set, is forwarded as "ammo-item" in the item_group entry,
    which causes CDDA to pre-load that ammo type into the weapon at spawn time.
    """
    assert 0 <= damage <= 4, f"damage level must be 0-4, got {damage!r} for {typeid!r}"
    d: dict[str, Any] = {"typeid": typeid, "damage": damage}
    if charges > 0:
        d["charges"] = charges
    if filthy:
        d["filthy"] = True
    if ammo_item:
        d["ammo_item"] = ammo_item
    return d


# ---------------------------------------------------------------------------
# Item-damage simulation functions
# ---------------------------------------------------------------------------

def simulate_worn_item(typeid: str, damage_lo: int, damage_hi: int) -> int:
    """Return a random damage level (0-4) for a worn equipment item.

    count_by_charges items (ammo, food, medicine, etc.) always return 0 because
    in CDDA's C++ itype.h, damage_max() == 0 for those items.  Any nonzero
    damage level would cause damage_level() to return 5, which means the item
    is destroyed and never spawned — items in _COUNT_BY_CHARGES_TYPES must
    always be spawned at damage level 0.
    """
    assert 0 <= damage_lo <= damage_hi <= 4, (
        f"damage band out of [0, 4]: got [{damage_lo}, {damage_hi}]"
    )
    if typeid in _COUNT_BY_CHARGES_TYPES:
        return 0
    level = random.randint(damage_lo, damage_hi)
    assert 0 <= level <= 4
    return level


def simulate_weapon(typeid: str, damage_lo: int, damage_hi: int) -> int:
    """Return a random damage level (0-4) for a weapon item.

    Weapons are not count_by_charges items, so any damage level 0-4 is valid.
    The count_by_charges guard is kept defensively — a future refactor might
    pass a consumable item ID here, and silent spawn failure would be hard to
    diagnose without it.
    """
    assert 0 <= damage_lo <= damage_hi <= 4, (
        f"damage band out of [0, 4]: got [{damage_lo}, {damage_hi}]"
    )
    if typeid in _COUNT_BY_CHARGES_TYPES:
        return 0
    level = random.randint(damage_lo, damage_hi)
    assert 0 <= level <= 4
    return level


def simulate_wounds(severity_lo: float, severity_hi: float) -> dict[str, float]:
    """Return randomised body-part HP ratios in [0.0, 1.0] per body part.

    0.0 = fully dead limb (C++ hp_cur == 0, i.e. broken), 1.0 = uninjured.
    Minimum 0.05 so we never spawn with a clinically broken limb — that would
    stun the Nemesis immediately.
    """
    assert 0.0 <= severity_lo <= severity_hi <= 1.0, (
        f"severity band out of [0.0, 1.0]: got [{severity_lo}, {severity_hi}]"
    )
    base = random.uniform(severity_lo, severity_hi)
    ratios: dict[str, float] = {
        bp: round(random.uniform(max(0.05, base - 0.25), min(1.0, base + 0.15)), 2)
        for bp in _BP_IDS
    }
    hit_limb = random.choice(["arm_l", "arm_r", "leg_l", "leg_r"])
    ratios[hit_limb] = round(max(0.05, ratios[hit_limb] - random.uniform(0.20, 0.45)), 2)
    return ratios


# ---------------------------------------------------------------------------
# Body-part IDs
# ---------------------------------------------------------------------------

_BP_IDS: tuple[str, ...] = ("head", "torso", "arm_l", "arm_r", "leg_l", "leg_r")


# ---------------------------------------------------------------------------
# Last words per death cause
# ---------------------------------------------------------------------------

_LAST_WORDS: dict[str, tuple[str, ...]] = {
    "is_fire":          ("It burns... help me!", "Can't escape the fire!", "The smoke..."),
    "is_lava":          ("The cave was so warm.", "I miscalculated the tunnel.", "The heat... so much."),
    "is_infection":     ("Something's wrong with me...", "Don't let them get you.", "It's spreading."),
    "is_starvation":    ("I was so close to finding food.", "My kingdom for a meal.", "So hungry..."),
    "is_thirst":        ("Water. Just water.", "Can't think straight.", "Please, anything to drink."),
    "is_blood_loss":    ("Press here — stop the bleeding...", "Too much blood.", "I can't feel my legs."),
    "is_drowning":      ("Can't... surface...", "The current's too strong.", "Shouldn't have crossed."),
    "is_explosion":     ("I thought I had more time.", "The fuse was too short!", "MOVE!"),
    "is_headshot":      ("Didn't even hear the shot.", "Watch your six.", "One second faster..."),
    "is_nuclear":       ("The dosimeter... I ignored it.", "It was worth the risk.", "The glow... beautiful."),
    "is_mycus":         ("The berries — I was so hungry.", "It's not unpleasant.", "We are one now."),
    "is_triffid":       ("The grove — don't go near it.", "Their roots are everywhere.", "Run from the grove!"),
    "is_teleglow":      ("The portal... I had to know.", "Something followed me through.", "The stars are wrong here."),
    "is_dimension":     ("Resonance cascade — off by one.", "The coordinates were wrong.", "Not this dimension."),
    "is_exhaustion":    ("Just five minutes...", "I can't keep my eyes open.", "Almost... safe..."),
    "is_freezing":      ("Should have kept moving.", "Hypothermia... gradual.", "So tired. And warm."),
    "is_suicide":       ("Better than them having me.", "On my own terms.", "Forgive me."),
    "is_poison":        ("Shouldn't have touched it bare-handed.", "The water — it was the water.", "Everything is numb."),
    "is_trap":          ("I KNEW something was off.", "Who puts a bear trap there?", "Should have checked the floor."),
    "is_darkwyrm":      ("I saw it in the dark. It looked back.", "Don't wake them.", "The wyrm knows your name."),
    "is_amigara":       ("The holes... I have to go in.", "I can't stop walking toward it.", "It fits. It fits perfectly."),
    "is_artifact":      ("Beautiful and terrible.", "I only touched it once.", "The humming won't stop."),
    "is_overdose":      ("One more hit. Just one more.", "I thought I built up tolerance.", "Flying... finally flying."),
    "is_asthma":        ("Forgot my inhaler. Of all the days.", "Can't... breathe...", "Just a little further."),
    "is_electric":      ("Exposed wire. Classic.", "Should have worn gloves.", "The power was supposed to be off."),
    "is_acid":          ("It burns. It burns so much.", "The floor — it was the floor.", "I can't see. Everything is burning."),
    "is_sewage":        ("It wasn't labeled.", "It tasted off immediately.", "Don't drink from the pipes."),
    "is_parasite":      ("Something's moving inside me.", "The pain comes in waves.", "Get it out. GET IT OUT."),
    "is_dermatik":      ("It injected something in me.", "The itching won't stop.", "Too late to cut it out."),
    "is_teleport_wall": ("The portal malfunctioned.", "I materialized in the wall.", "Wrong destination."),
    "is_mutagen":       ("The labels said different things.", "Beautiful. Horrible. Beautiful.", "I don't know what I am."),
    "is_autodoc":       ("The CBM installation... gone wrong.", "At least I tried.", "The sedative isn't enough."),
    "is_subspace":      ("They came through when I opened it.", "The dimensional boundary collapsed.", "Beautiful... but wrong."),
    "generic": (
        "I didn't think it would end like this.",
        "Tell my family I tried.",
        "Keep moving. Don't stop.",
        "The world ended. And then so did I.",
        "Stay safe out there.",
        "I should have listened.",
        "It wasn't supposed to go this way.",
    ),
}


def _pick_last_words(dc: DeathCause) -> str:
    """Return a random last-words string matching the Nemesis's cause of death."""
    for attr, pool in _LAST_WORDS.items():
        if attr == "generic":
            continue
        if getattr(dc, attr, False):
            return random.choice(pool)
    return random.choice(_LAST_WORDS["generic"])


# ---------------------------------------------------------------------------
# Cause -> faction_mon mapping (mirrors nemesis_builder.generate_npc_data)
# ---------------------------------------------------------------------------

def _faction_mon(dc: DeathCause) -> str:
    """Return the monster faction ID that best matches the Nemesis's death cause.

    The faction determines which in-world faction the Nemesis is allied with,
    influencing which monsters ignore or attack them in the field.  A character
    who died from Mycus infection becomes an ally of the fungal faction, etc.
    """
    if dc.is_mycus:    return "fungus"
    if dc.is_triffid:  return "triffid"
    if dc.is_dermatik: return "dermatik"
    if dc.is_parasite: return "spider"
    if dc.is_teleglow: return "nether_player_hate"
    return "zombie"


# ---------------------------------------------------------------------------
# Tier definitions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _TierDef:
    label:          str
    first_names:    tuple[str, ...]
    cause_attrs:    tuple[str, ...]
    # Weighted category maps: category_name -> relative weight (need not sum to 1).
    # Keys must exist in WORN_CATEGORIES / CARRY_CATEGORIES / WEAPON_CATEGORIES.
    worn_categories:   dict[str, float]
    carry_categories:  dict[str, float]
    weapon_categories: dict[str, float]
    # skill_ranges must not contain ids in builder._SKIP_SKILLS
    skill_ranges:   dict[str, tuple[int, int]]
    # bionic_pool must not contain ids in builder._SKIP_BIONICS
    bionic_pool:    tuple[str, ...]
    max_bionics:    int
    mutation_pool:  tuple[str, ...]
    max_mutations:  int
    style_pool:     tuple[str, ...]
    style_prob:     float
    stat_lo:        int
    stat_hi:        int
    delay_lo:       int
    delay_hi:       int
    severity_lo:    float
    severity_hi:    float
    damage_lo:      int                         # CDDA damage level 0-4
    damage_hi:      int
    worn_count:     tuple[int, int]
    carry_count:    tuple[int, int]
    # (oter_id, is_outside) — real CDDA IDs; substring-matched by builder's
    # categorize_death_location.  Used for Tier pool (70%) and combined (30%).
    oter_pool:      tuple[tuple[str, bool], ...]

    def __post_init__(self) -> None:
        assert 0 <= self.damage_lo <= self.damage_hi <= 4, (
            f"{self.label}: damage levels must be in [0, 4], "
            f"got [{self.damage_lo}, {self.damage_hi}]"
        )
        assert 0.0 <= self.severity_lo <= self.severity_hi <= 1.0, (
            f"{self.label}: severity must be in [0.0, 1.0]"
        )
        assert self.oter_pool,          f"{self.label}: oter_pool must not be empty"
        assert self.cause_attrs,        f"{self.label}: cause_attrs must not be empty"
        assert self.worn_categories,    f"{self.label}: worn_categories must not be empty"
        assert self.carry_categories,   f"{self.label}: carry_categories must not be empty"
        assert self.weapon_categories,  f"{self.label}: weapon_categories must not be empty"
        bad_skills  = frozenset(self.skill_ranges) & _SKIP_SKILLS
        bad_bionics = frozenset(self.bionic_pool)  & _SKIP_BIONICS
        assert not bad_skills,  f"{self.label}: skill_ranges contains skipped skills:  {bad_skills}"
        assert not bad_bionics, f"{self.label}: bionic_pool contains skipped bionics: {bad_bionics}"
        bad_worn    = frozenset(self.worn_categories)   - frozenset(WORN_CATEGORIES)
        bad_carry   = frozenset(self.carry_categories)  - frozenset(CARRY_CATEGORIES)
        bad_weapon  = frozenset(self.weapon_categories) - frozenset(WEAPON_CATEGORIES)
        assert not bad_worn,   f"{self.label}: unknown worn categories:   {bad_worn}"
        assert not bad_carry,  f"{self.label}: unknown carry categories:  {bad_carry}"
        assert not bad_weapon, f"{self.label}: unknown weapon categories: {bad_weapon}"


# ---------------------------------------------------------------------------
# T1 — Civilians (Day 1–13)
# ---------------------------------------------------------------------------

_TIER_CIVILIAN = _TierDef(
    label       = "Civilian",
    first_names = (
        "Alice", "Bob", "Carol", "Dave", "Eve", "Frank", "Grace", "Hank",
        "Irene", "Jack", "Karen", "Liam", "Mia", "Noah", "Olivia", "Pete",
        "Quinn", "Rosa", "Sam", "Tara", "Uma", "Vince", "Wendy", "Xander",
    ),
    cause_attrs = (
        "is_infection", "is_starvation", "is_thirst", "is_blood_loss",
        "is_exhaustion", "is_headshot", "is_drowning", "is_poison", "is_acid",
    ),
    worn_categories   = {"CIVILIAN": 0.7, "OUTDOOR": 0.3},
    carry_categories  = {"CIVILIAN": 0.5, "TOOLS": 0.3, "MEDICAL_BASIC": 0.2},
    weapon_categories = {"MELEE_HEAVY": 0.6, "MELEE_LIGHT": 0.4},
    skill_ranges = {
        "gun":      (0, 2),
        "melee":    (1, 4),
        "dodge":    (1, 3),
        "survival": (1, 3),
        "bashing":  (1, 3),
    },
    bionic_pool    = (),
    max_bionics    = 0,
    mutation_pool  = (),
    max_mutations  = 0,
    style_pool     = (),
    style_prob     = 0.0,
    stat_lo        = 7,
    stat_hi        = 11,
    delay_lo       = 1,
    delay_hi       = 13,
    severity_lo    = 0.10,
    severity_hi    = 0.50,
    damage_lo      = 2,
    damage_hi      = 4,
    worn_count     = (3, 6),
    carry_count    = (2, 6),
    oter_pool      = (
        ("house_01",  False),
        ("house_02",  False),
        ("house_03",  False),
        ("cabin",     False),
        ("cabin_1",   False),
        ("park",      True),
        ("field",     True),
    ),
)

# ---------------------------------------------------------------------------
# T2 — Responders (Day 14–59)
# ---------------------------------------------------------------------------

_TIER_RESPONDER = _TierDef(
    label       = "Responder",
    first_names = (
        "Aaron", "Brett", "Cole", "Dean", "Eric", "Finn", "Grant", "Hunter",
        "Ivan", "Jason", "Kent", "Lance", "Marco", "Neil", "Owen", "Perry",
        "Rex", "Scott", "Tyler", "Wade", "Zach",
    ),
    cause_attrs = (
        "is_fire", "is_explosion", "is_headshot", "is_blood_loss",
        "is_infection", "is_trap", "is_electric", "is_asthma", "is_acid",
    ),
    worn_categories   = {"POLICE": 0.5, "MILITARY": 0.3, "OUTDOOR": 0.2},
    carry_categories  = {"CIVILIAN": 0.2, "MEDICAL_BASIC": 0.3, "MILITARY": 0.3, "FOOD": 0.2},
    weapon_categories = {"PISTOL": 0.4, "SHOTGUN": 0.25, "SMG": 0.25, "MELEE_HEAVY": 0.1},
    skill_ranges = {
        "gun":      (3, 5),
        "pistol":   (2, 4),
        "shotgun":  (2, 4),
        "melee":    (3, 5),
        "dodge":    (3, 5),
        "survival": (2, 4),
        "bashing":  (2, 4),
        "stabbing": (2, 4),
    },
    bionic_pool  = (
        "bio_armor_head", "bio_armor_torso", "bio_night_vision",
        "bio_painkiller",
    ),
    max_bionics   = 1,
    mutation_pool = (),
    max_mutations = 0,
    style_pool    = ("style_judo", "style_karate", "style_capoeira"),
    style_prob    = 0.25,
    stat_lo       = 9,
    stat_hi       = 14,
    delay_lo      = 14,
    delay_hi      = 59,
    severity_lo   = 0.35,
    severity_hi   = 0.75,
    damage_lo     = 1,
    damage_hi     = 2,
    worn_count    = (4, 7),
    carry_count   = (3, 7),
    oter_pool     = (
        ("hospital_1",      False),
        ("hospital_2",      False),
        ("police_1",        False),
        ("police_2",        False),
        ("megastore_0_0_0", False),
        ("megastore_0_1_0", False),
        ("dollarstore",     False),
        ("garage_gas_1",    True),
        ("bank",            False),
    ),
)

# ---------------------------------------------------------------------------
# T3 — Elites (Day 60–99)
# ---------------------------------------------------------------------------

_TIER_ELITE = _TierDef(
    label       = "Elite",
    first_names = (
        "Bravo", "Cipher", "Delta", "Echo", "Foxtrot", "Ghost", "Havoc",
        "Indigo", "Jackdaw", "Kilo", "Lynch", "Maverick", "Nexus",
        "Omega", "Phantom", "Rook", "Specter", "Tango", "Umber", "Viper",
    ),
    cause_attrs = (
        "is_nuclear", "is_mycus", "is_triffid", "is_teleglow",
        "is_dimension", "is_explosion", "is_autodoc", "is_mutagen",
    ),
    worn_categories   = {"MILITARY": 0.6, "HAZMAT": 0.2, "POLICE": 0.2},
    carry_categories  = {"MILITARY": 0.5, "MEDICAL_ADV": 0.3, "TOOLS": 0.2},
    weapon_categories = {"RIFLE": 0.4, "SMG": 0.3, "PISTOL": 0.2, "MELEE_MARTIAL": 0.1},
    skill_ranges = {
        "gun":      (6, 10),
        "rifle":    (5, 10),
        "pistol":   (4,  8),
        "smg":      (3,  7),
        "melee":    (5,  9),
        "dodge":    (6, 10),
        "survival": (5,  9),
        "bashing":  (4,  8),
        "stabbing": (4,  8),
        "archery":  (3,  6),
        "throw":(3,  6),
    },
    bionic_pool  = (
        "bio_armor_head", "bio_armor_torso", "bio_armor_legs",
        "bio_night_vision", "bio_painkiller", "bio_adrenaline",
        "bio_speed", "bio_hydraulics", "bio_targeting",
    ),
    max_bionics   = 4,
    mutation_pool = (
        "HYPEROPIC", "MYOPIC", "TOUGH", "FLEET", "QUICK",
    ),
    max_mutations = 2,
    style_pool    = (
        "style_krav_maga", "style_taekwondo", "style_tiger",
        "style_crane", "style_brawling",
    ),
    style_prob    = 0.65,
    stat_lo       = 12,
    stat_hi       = 18,
    delay_lo      = 60,
    delay_hi      = 99,
    severity_lo   = 0.60,
    severity_hi   = 0.95,
    damage_lo     = 0,
    damage_hi     = 1,
    worn_count    = (5, 9),
    carry_count   = (4, 8),
    oter_pool     = (
        ("lab_1x1_RES_8_bedrooms_maintenance", False),
        ("lab_1x1_RES_8_commons_MAINT",        False),
        ("mine_finale",                         False),
        ("mil_base_1a",                         True),
        ("mil_base_1b",                         True),
        ("bunker",                              False),
        ("bunker_basement_1",                   False),
        ("mil_surplus",                         False),
    ),
)

# ---------------------------------------------------------------------------
# T4 — Apex Survivors (Day 100–365)
# ---------------------------------------------------------------------------

_TIER_APEX = _TierDef(
    label       = "Apex",
    first_names = (
        "Animus", "Basilisk", "Calamity", "Dominion", "Eternity", "Famine",
        "Grimoire", "Harbinger", "Icarus", "Juggernaut", "Kronos", "Leviathan",
        "Mortis", "Nihil", "Omen", "Plague", "Quasar", "Ragnarok",
        "Singularity", "Thanatos",
    ),
    cause_attrs = (
        "is_darkwyrm", "is_amigara", "is_artifact", "is_subspace",
        "is_dimension", "is_teleglow", "is_nuclear", "is_overdose",
        "is_teleport_wall",
    ),
    worn_categories   = {"SURVIVOR": 0.5, "MILITARY": 0.3, "HAZMAT": 0.2},
    carry_categories  = {"MILITARY": 0.4, "MEDICAL_ADV": 0.4, "SCIENCE": 0.2},
    weapon_categories = {"SPECIAL": 0.3, "RIFLE": 0.3, "MELEE_MARTIAL": 0.2, "PISTOL": 0.2},
    skill_ranges = {
        # electronics, mechanics, firstaid excluded — they are in _SKIP_SKILLS
        "gun":      (8, 10),
        "rifle":    (8, 10),
        "pistol":   (6, 10),
        "smg":      (6,  9),
        "melee":    (8, 10),
        "dodge":    (8, 10),
        "survival": (8, 10),
        "bashing":  (7, 10),
        "stabbing": (7, 10),
        "archery":  (5,  9),
        "throw":(5,  9),
    },
    bionic_pool  = (
        # bio_metabolics excluded — it is in _SKIP_BIONICS
        "bio_armor_head", "bio_armor_torso", "bio_armor_arms", "bio_armor_legs",
        "bio_night_vision", "bio_painkiller", "bio_adrenaline",
        "bio_speed", "bio_hydraulics", "bio_targeting",
        "bio_emp", "bio_shockwave",
    ),
    max_bionics   = 8,
    mutation_pool = (
        "TOUGH", "FLEET", "QUICK", "PAINRESIST",
        "INFRARED", "FASTHEALER2", "HOOVES",
    ),
    max_mutations = 4,
    style_pool    = (
        "style_krav_maga", "style_taekwondo", "style_tiger",
        "style_crane", "style_brawling", "style_ninjutsu",
        "style_capoeira", "style_muay_thai",
    ),
    style_prob    = 0.90,
    stat_lo       = 15,
    stat_hi       = 20,
    delay_lo      = 100,
    delay_hi      = 365,
    severity_lo   = 0.70,
    severity_hi   = 0.99,
    damage_lo     = 0,
    damage_hi     = 1,
    worn_count    = (5, 10),
    carry_count   = (5,  9),
    oter_pool     = (
        ("labyrinth_string_dimension_tunnel_portal", True),
        ("mine_amigara_finale_central",              False),
        ("temple_stairs",                            False),
        ("temple_finale",                            False),
        ("mine_finale",                              False),
        ("mine_spiral_finale_n",                     False),
        ("lab_1x1_RES_8_bedrooms_security",          False),
        ("lab_1x1_RES_8_commons_SEC",                False),
    ),
)


_TIERS: tuple[_TierDef, ...] = (
    _TIER_CIVILIAN, _TIER_RESPONDER, _TIER_ELITE, _TIER_APEX,
)

# Integer tier map: 1-indexed to match user-facing language ("Tier 1", "Tier 4").
_TIER_MAP: dict[int, _TierDef] = {i + 1: t for i, t in enumerate(_TIERS)}


# ---------------------------------------------------------------------------
# Weapon ammo tier scaling
# ---------------------------------------------------------------------------

# Fraction of magazine capacity that is loaded into the weapon at spawn time,
# expressed as a (min, max) range per tier.  The seeder samples uniformly from
# this range and multiplies by mag_capacity, then clamps to [1, mag_capacity].
#
# Design rationale:
#   T1 Civilians found their gun early in the cataclysm and spent most of their
#   ammo surviving before they died.  Their magazines are nearly empty.
#   T2 Responders were better supplied but still expendable round-by-round.
#   T3 Elites topped off before every engagement as doctrine demands.
#   T4 Apex survivors never let their weapon go below full — preparation is life.
_TIER_LOAD_FACTOR: dict[int, tuple[float, float]] = {
    1: (0.25, 0.50),   # Civilian: quarter to half magazine — scarce ammo
    2: (0.50, 1.00),   # Responder: half to full — disciplined but finite supply
    3: (0.75, 1.00),   # Elite: mostly to fully loaded — well-supplied operator
    4: (1.00, 1.00),   # Apex: always full — perfect preparation, no exceptions
}


def _pick_weapon_ammo(
    ammo_entry: tuple[str, list[tuple[str | None, int]]],
    tier_num: int,
) -> tuple[str, str | None, int, int]:
    """Select ammo, magazine, and loaded-round count based on Nemesis tier.

    Higher-tier Nemeses carry larger magazines and carry them fuller.  The two
    axes of scaling are:

    1. Magazine size — WEAPON_AMMO stores all distinct magazine sizes for each
       gun, sorted ascending by capacity (as produced by nemesis_pool_generator).
       The tier maps to a list index by:
           idx = min(tier_num - 1, len(mag_options) - 1)
       So T1 picks index 0 (smallest magazine), T4 picks the last entry (largest).
       When a gun has only one magazine size, all tiers receive the same magazine
       and differentiation comes entirely from the load factor.

    2. Load factor — _TIER_LOAD_FACTOR defines a (min_frac, max_frac) range for
       each tier.  The loaded round count is sampled uniformly:
           rounds = max(1, round(uniform(lo, hi) * capacity))
       This ensures T1 weapons are partially depleted while T4 weapons are topped
       off, reinforcing the narrative that higher-tier Nemeses were better
       prepared when they died.

    Returns (ammo_id, mag_id_or_None, loaded_rounds, mag_capacity).
    loaded_rounds is always in [1, mag_capacity].
    mag_capacity is the full capacity of the selected magazine (used by the
    caller to fill the spare magazine carried in the inventory).
    """
    ammo_id, mag_options = ammo_entry
    idx = min(tier_num - 1, len(mag_options) - 1)
    mag_id, mag_capacity = mag_options[idx]
    lo, hi = _TIER_LOAD_FACTOR[tier_num]
    loaded_rounds = max(1, round(random.uniform(lo, hi) * mag_capacity))
    return ammo_id, mag_id, loaded_rounds, mag_capacity


# Combined pool for the 30% cross-tier location rule.
# Flat concatenation: tiers with larger oter_pools have proportionally more
# representation, which reflects genuine geographic variety.
_ALL_OTER_POOL: tuple[tuple[str, bool], ...] = tuple(
    entry for tier in _TIERS for entry in tier.oter_pool
)


# ---------------------------------------------------------------------------
# Location picker — 70 % from tier's own pool, 30 % from all tiers combined
# ---------------------------------------------------------------------------

def _pick_location(tier: _TierDef) -> tuple[dict[str, Any] | None, str | None]:
    """Return (location_cond, oter_id).

    70 % of the time uses the Nemesis's own tier pool (contextual death —
    a Civilian dies near a house).  30 % uses the combined pool of all tiers,
    producing the "unexpected death in a strange place" narrative.
    """
    pool = tier.oter_pool if random.random() < 0.70 else _ALL_OTER_POOL
    oter_id, is_outside = random.choice(pool)
    return ("u_is_outside" if is_outside else {"not": "u_is_outside"}), oter_id


# ---------------------------------------------------------------------------
# Per-nemesis data assembly helpers
# ---------------------------------------------------------------------------

_LAST_NAME_POOL: tuple[str, ...] = (
    "Smith", "Jones", "Brown", "Taylor", "Williams", "Davis", "Miller",
    "Wilson", "Moore", "Anderson", "Jackson", "Martin", "Lee", "Harris",
    "Thompson", "Garcia", "Martinez", "Robinson", "Clark", "Lewis",
)


def _make_death_cause(tier: _TierDef) -> DeathCause:
    """Pick a random cause of death from the tier's cause_attrs pool."""
    return DeathCause(**{random.choice(tier.cause_attrs): True})


def _pick_stats(tier: _TierDef) -> tuple[int, int, int, int]:
    """Return (str_max, dex_max, int_max, per_max) sampled from the tier's stat range."""
    lo, hi = tier.stat_lo, tier.stat_hi
    return (
        random.randint(lo, hi),  # str_max
        random.randint(lo, hi),  # dex_max
        random.randint(lo, hi),  # int_max
        random.randint(lo, hi),  # per_max
    )


def _pick_skills(tier: _TierDef) -> dict[str, int]:
    """Return a dict of skill_id -> level sampled from the tier's skill_ranges.

    Skills in _SKIP_SKILLS (electronics, mechanics, firstaid) are excluded
    because nemesis_builder handles them separately.  _TierDef.__post_init__
    already asserts that skill_ranges contains no skipped skills; the filter
    here is a defensive second check.
    """
    # Respect _SKIP_SKILLS at the seeder level; _TierDef.__post_init__ also
    # enforces this, so this filter is purely defensive.
    skills: dict[str, int] = {}
    for skill_id, (lo, hi) in tier.skill_ranges.items():
        if skill_id in _SKIP_SKILLS:
            continue
        level = random.randint(lo, hi)
        if level > 0:
            skills[skill_id] = level
    return skills


def _pick_bionics(tier: _TierDef) -> list[str]:
    """Return a random sample of bionic IDs from the tier's bionic_pool.

    The sample size is drawn uniformly from [0, max_bionics], so some Nemeses
    of the same tier will have fewer bionics than the tier maximum.  This
    produces natural variety within a tier rather than every Elite or Apex
    Nemesis having the exact same number of bionics.
    """
    if not tier.bionic_pool or tier.max_bionics == 0:
        return []
    # Respect _SKIP_BIONICS — __post_init__ guards, but filter defensively.
    eligible = [b for b in tier.bionic_pool if b not in _SKIP_BIONICS]
    count = random.randint(0, tier.max_bionics)
    if count == 0 or not eligible:
        return []
    return random.sample(eligible, k=min(count, len(eligible)))


def _pick_mutations(tier: _TierDef) -> list[str]:
    """Return a random sample of mutation IDs from the tier's mutation_pool."""
    if not tier.mutation_pool or tier.max_mutations == 0:
        return []
    count = random.randint(0, tier.max_mutations)
    if count == 0:
        return []
    return random.sample(list(tier.mutation_pool), k=min(count, len(tier.mutation_pool)))


def _pick_styles(tier: _TierDef) -> list[str]:
    """Return a list of zero or one martial arts style IDs for this Nemesis.

    style_prob is the probability that a Nemesis of this tier has any martial
    arts style at all (T1 Civilians: 0%, T4 Apex: 90%).  If the roll passes,
    one style is chosen at random from the tier's style_pool.
    """
    if not tier.style_pool or random.random() >= tier.style_prob:
        return []
    return [random.choice(tier.style_pool)]



def _pick_from_categories(
    categories: dict[str, tuple[str, ...]],
    cat_weights: dict[str, float],
    count: int,
    bonus_items: tuple[str, ...] | list[str] = (),
) -> list[str]:
    """Weighted sampling without replacement from item categories.

    Each item's base weight = (category_weight / total_weight) / category_size.
    Bonus items receive an additive +0.15 weight boost on top of their base weight
    (or +0.15 if they appear in no category), making death-cause / location
    thematic items noticeably more likely without guaranteeing them.
    """
    total_w = sum(cat_weights.values())
    item_weights: dict[str, float] = {}
    for cat_name, cat_w in cat_weights.items():
        items = categories.get(cat_name, ())
        if not items:
            continue
        per_item = (cat_w / total_w) / len(items)
        for item in items:
            item_weights[item] = item_weights.get(item, 0.0) + per_item

    for item in bonus_items:
        item_weights[item] = item_weights.get(item, 0.0) + 0.15

    if not item_weights:
        return []

    pool_items  = list(item_weights.keys())
    pool_wts    = [item_weights[i] for i in pool_items]
    selected: list[str] = []
    for _ in range(min(count, len(pool_items))):
        idx = random.choices(range(len(pool_items)), weights=pool_wts)[0]
        selected.append(pool_items[idx])
        pool_items.pop(idx)
        pool_wts.pop(idx)
    return selected


def _pick_weapon_typeid(weapon_cats: dict[str, float]) -> str:
    """Pick one weapon type ID from weighted weapon categories.

    Works identically to _pick_from_categories but returns a single item ID
    rather than a list, and operates on WEAPON_CATEGORIES instead of
    WORN/CARRY pools.  Falls back to "bat" if all categories are empty.
    """
    total_w = sum(weapon_cats.values())
    item_weights: dict[str, float] = {}
    for cat_name, cat_w in weapon_cats.items():
        weapons = WEAPON_CATEGORIES.get(cat_name, ())
        if not weapons:
            continue
        per_item = (cat_w / total_w) / len(weapons)
        for wp in weapons:
            item_weights[wp] = item_weights.get(wp, 0.0) + per_item
    if not item_weights:
        return "bat"
    pool_items = list(item_weights.keys())
    pool_wts   = [item_weights[i] for i in pool_items]
    return random.choices(pool_items, weights=pool_wts)[0]


def _get_cause_bonus(dc: DeathCause) -> tuple[list[str], list[str]]:
    """Return thematic item hints based on how the original player character died.

    The two lists returned are (bonus_worn, bonus_carry).  They are passed into
    _pick_from_categories as the `bonus_items` argument, which gives each listed
    item an additive +0.15 weight boost on top of its base category weight.  The
    effect is probabilistic: thematic items become noticeably more likely without
    being guaranteed, so not every Nemesis ends up carrying identical gear.

    The death-cause flags are set in _make_death_cause from the save's cause of
    death string.  Only the first matching branch fires, so the ordering here
    matters — more specific / memorable causes (drowning, nuclear) come before
    generic ones (fire, infection).

    Items listed here must exist in WORN_CATEGORIES or CARRY_CATEGORIES (or both).
    Items not found in any category still receive the weight boost and can be
    selected if they appear in the pool at all; unknown IDs are silently ignored
    by _pick_from_categories.
    """
    if dc.is_drowning:   return ["wetsuit"],             ["2lcanteen", "water_clean"]
    if dc.is_nuclear:    return ["hazmat_suit"],          ["geiger_off", "iodine_crystal"]
    if dc.is_freezing:   return ["mittens", "scarf", "hat_knit"], ["thermos"]
    if dc.is_fire:       return [],                       ["matches", "lighter"]
    if dc.is_infection:  return [],                       ["antibiotics", "tourniquet_upper"]
    if dc.is_poison:     return [],                       ["royal_jelly", "antibiotics"]
    if dc.is_explosion:  return ["helmet_army"],          []
    if dc.is_headshot:   return ["helmet_riot"],          []
    if dc.is_blood_loss: return [],                       ["tourniquet_upper", "adhesive_bandages"]
    return [], []


def _get_location_bonus(oter_id: str | None) -> tuple[list[str], list[str]]:
    """Return thematic item hints based on the overmap tile where the player died.

    Works identically to _get_cause_bonus — the two lists are (bonus_worn,
    bonus_carry), and they are passed into _pick_from_categories as the
    `bonus_items` argument for a +0.15 additive weight boost per item.

    oter_id is the CDDA overmap terrain ID (e.g., "house_north", "mil_base_3"),
    or None if the save did not record a death location.  The function matches
    by substring so partial keywords like "hospital" catch all hospital variants
    ("hospital_1", "hospital_roof", etc.) without listing each explicitly.

    Location bonuses stack with cause bonuses: both lists are concatenated before
    being passed to _pick_from_categories, so a player who drowned in a hospital
    can end up with both a wetsuit (cause bonus) and morphine (location bonus) in
    their Nemesis inventory.
    """
    if not oter_id:
        return [], []
    oid = oter_id.lower()
    if any(kw in oid for kw in ("house", "cabin")):
        return [], ["can_beans", "matches", "pockknife", "bottle_plastic"]
    if "hospital" in oid:
        return [], ["morphine", "antibiotics", "syringe", "adrenaline_injector"]
    if any(kw in oid for kw in ("mil_base", "bunker")):
        return ["pants_army", "kevlar"], ["mre_beef", "adrenaline_injector"]
    if "lab" in oid:
        return ["hazmat_suit"], ["iodine_crystal", "geiger_off", "syringe"]
    if "police" in oid:
        return ["chestrig", "holster"], ["adhesive_bandages", "tourniquet_upper"]
    if any(kw in oid for kw in ("megastore", "dollarstore")):
        return [], ["energy_drink", "granola", "can_beans", "bottle_plastic"]
    if any(kw in oid for kw in ("mine", "amigara", "temple")):
        return [], ["rope_30", "pickaxe", "flashlight"]
    return [], []


# ---------------------------------------------------------------------------
# Internal bundle — bridges generate and build stages
#
# NemesisCharData carries everything nemesis_eoc needs.  But build_npc_class
# also needs bonus stats, bionics, mutations, skills, and job_description —
# none of which are stored in NemesisCharData.  _NemesisBundle holds both so
# seed_nemeses can build the JSON without re-deriving them.
# ---------------------------------------------------------------------------

@dataclass
class _NemesisBundle:
    """Intermediate data container bridging the generate and build stages.

    NemesisCharData (from nemesis_eoc.py) captures everything needed to write
    the EOC JSON: NPC IDs, item groups, wound ratios, delay, death cause, etc.
    But _build_npc_json also needs per-character stats, bionics, mutations,
    skills, and the narrative job_description — none of which live in
    NemesisCharData.  _NemesisBundle groups both so a single call to
    _generate_bundle produces all data, and a single call to _build_npc_json
    plus build_per_char_eocs consumes it without any re-derivation.

    bonus_str / _dex / _int / _per are CDDA stat deltas above CDDA_BASE_STAT
    (8 by default).  A value of 4 means the character has effective stat 12.
    """
    char_data:       NemesisCharData
    bonus_str:       int
    bonus_dex:       int
    bonus_int:       int
    bonus_per:       int
    bionics:         list[str]
    mutations:       list[str]
    skills:          dict[str, int]
    job_description: str


def _generate_bundle(tier_def: _TierDef, index: int, tier_num: int) -> _NemesisBundle:
    """Compute all per-character data for one Nemesis without emitting any JSON.

    This function is the single source of randomness for an entire Nemesis.  It
    calls every sub-picker in sequence and assembles the results into a
    _NemesisBundle that can then be handed to _build_npc_json and
    build_per_char_eocs separately.  Keeping generation and serialization apart
    makes both stages independently testable.

    Parameters
    ----------
    tier_def  : Configuration for this tier (name pools, count ranges, category
                weights, damage ranges, delay range, etc.).  One of the four
                entries in _TIER_MAP.
    index     : A unique integer suffix that prevents name collisions when two
                Nemeses happen to receive the same first + last name combination.
                In seed mode this is a global sequential counter; in random mode
                it is a random number in [0, 9999].
    tier_num  : Numeric tier (1–4).  Used by _pick_weapon_ammo to select the
                appropriate magazine size and load fraction for this tier.

    Pipeline (in order)
    --------------------
    1. Pick first/last name -> derive all CDDA IDs (class, template, item groups, etc.)
    2. Pick stats via _pick_stats
    3. Pick death location and wound ratios
    4. Pick death cause, skills, bionics, mutations, martial-arts style, last words
    5. Resolve thematic worn/carry bonuses from cause and location
    6. Pick worn items and carried items via weighted sampling
    7. Pick weapon type, then magazine + ammo via tier-scaled _pick_weapon_ammo
    8. Build narrative job_description and assemble NemesisCharData + _NemesisBundle
    """
    first     = random.choice(tier_def.first_names)
    last      = random.choice(_LAST_NAME_POOL)
    char_name = f"{first} {last}"
    safe_name = f"{make_safe_id(char_name)}_{index:04d}"

    class_id   = f"NC_NEMESIS_{safe_name}"
    tmpl_id    = f"npc_nemesis_{safe_name}"
    worn_gid   = f"nem_worn_{safe_name}"
    carry_gid  = f"nem_carry_{safe_name}"
    weapon_gid = f"nem_weapon_{safe_name}"
    unique_id  = f"nemesis_{safe_name}"
    faction_id = f"nemesis_faction_{safe_name}"

    str_max, dex_max, int_max, per_max = _pick_stats(tier_def)

    days_per_year = _DEFAULT_SEASON_DAYS * 4
    delay_days    = random.randint(tier_def.delay_lo, tier_def.delay_hi)
    death_day     = None  # seeded nemeses carry no anniversary gate; delay_days handles tier timing

    location_cond, oter_id = _pick_location(tier_def)
    bp_ratios    = simulate_wounds(tier_def.severity_lo, tier_def.severity_hi)
    death_cause  = _make_death_cause(tier_def)
    skills       = _pick_skills(tier_def)
    bionics      = _pick_bionics(tier_def)
    mutations    = _pick_mutations(tier_def)
    martial_arts = _pick_styles(tier_def)
    last_words   = _pick_last_words(death_cause)

    if death_cause.is_drowning:
        skills["swimming"] = 10

    worn_count    = random.randint(*tier_def.worn_count)
    carry_count   = random.randint(*tier_def.carry_count)

    cause_worn_bonus, cause_carry_bonus   = _get_cause_bonus(death_cause)
    loc_worn_bonus,   loc_carry_bonus     = _get_location_bonus(oter_id)
    bonus_worn  = cause_worn_bonus  + loc_worn_bonus
    bonus_carry = cause_carry_bonus + loc_carry_bonus

    worn_typeids  = _pick_from_categories(WORN_CATEGORIES,  tier_def.worn_categories,  worn_count,  bonus_worn)
    carry_typeids = _pick_from_categories(CARRY_CATEGORIES, tier_def.carry_categories, carry_count, bonus_carry)
    worn_items    = [
        _item(tid, damage=simulate_worn_item(tid, tier_def.damage_lo, tier_def.damage_hi), filthy=True)
        for tid in worn_typeids
    ]
    carried_items = [
        _item(tid, damage=simulate_worn_item(tid, tier_def.damage_lo, tier_def.damage_hi))
        for tid in carry_typeids
    ]

    weapon_typeid = _pick_weapon_typeid(tier_def.weapon_categories)
    ammo_entry    = WEAPON_AMMO.get(weapon_typeid)
    if ammo_entry:
        # _pick_weapon_ammo selects the right magazine size for this tier and
        # samples a tier-appropriate load fraction.  Higher tiers get larger
        # magazines and carry them closer to full (see _TIER_LOAD_FACTOR).
        ammo_id, mag_id, loaded_rounds, mag_capacity = _pick_weapon_ammo(
            ammo_entry, tier_num
        )
        weapon_item = _item(
            weapon_typeid,
            damage=simulate_weapon(weapon_typeid, tier_def.damage_lo, tier_def.damage_hi),
            ammo_item=ammo_id,
            charges=loaded_rounds,
        )
        # Extra loose ammo in carry — one full reload of the selected magazine.
        carried_items.append(_item(ammo_id, charges=mag_capacity))
        # Pre-loaded spare magazine in carry (detachable mag weapons only).
        if mag_id:
            carried_items.append(_item(mag_id, ammo_item=ammo_id, charges=mag_capacity))
    else:
        weapon_item = _item(
            weapon_typeid,
            damage=simulate_weapon(weapon_typeid, tier_def.damage_lo, tier_def.damage_hi),
        )

    job_description = build_narrative_description(
        profession_name = tier_def.label.lower(),
        death_message   = None,
        last_words      = last_words,
        death_cause     = death_cause,
    )

    char_data = NemesisCharData(
        safe_name     = safe_name,
        char_name     = char_name,
        tmpl_id       = tmpl_id,
        class_id      = class_id,
        worn_gid      = worn_gid,
        carry_gid     = carry_gid,
        weapon_gid    = weapon_gid,
        unique_id     = unique_id,
        faction_id    = faction_id,
        bp_ratios     = bp_ratios,
        location_cond = location_cond,
        death_day     = death_day,
        days_per_year = days_per_year,
        delay_days    = delay_days,
        death_cause   = death_cause,
        worn_items    = worn_items,
        carried_items = carried_items,
        weapon_item   = weapon_item,
        death_omt     = None,
        death_oter_id = oter_id,
        martial_arts  = martial_arts,
    )

    return _NemesisBundle(
        char_data       = char_data,
        bonus_str       = str_max - CDDA_BASE_STAT,
        bonus_dex       = dex_max - CDDA_BASE_STAT,
        bonus_int       = int_max - CDDA_BASE_STAT,
        bonus_per       = per_max - CDDA_BASE_STAT,
        bionics         = bionics,
        mutations       = mutations,
        skills          = skills,
        job_description = job_description,
    )


# ---------------------------------------------------------------------------
# NPC JSON assembly — separated from data generation for testability
# ---------------------------------------------------------------------------

def _build_npc_json(bundle: _NemesisBundle) -> list[dict[str, Any]]:
    """Serialize one Nemesis bundle into CDDA JSON objects.

    Returns a list of six dicts in the following order (same order CDDA expects
    when they are written to a single JSON file):

    1. Faction         -- via build_faction().  The Nemesis belongs to a unique
                          per-character faction so CDDA's faction-hostility system
                          correctly identifies all copies of this Nemesis as the
                          same entity even if multiple instances are alive at once.
    2. Memento item    -- via build_memento_item().  A lore item that the player
                          finds on the Nemesis corpse; it carries the character's
                          name and death circumstances as flavour text.
    3. Worn item group -- via build_item_group() for worn_gid.  CDDA uses this
                          item_group to equip the NPC when it spawns.
    4. Carry item group -- via build_item_group() for carry_gid.  Inventory items
                          (food, tools, spare ammo).
    5. Weapon item group -- via build_item_group() for weapon_gid.  The single
                          primary weapon with pre-loaded ammo.
    6. NPC class       -- via build_npc_class().  Defines stats, skills, bionics,
                          mutations, and which item groups to use.
    7. NPC template    -- via build_npc_template().  Ties the class to a unique
                          NPC instance ID and sets faction membership.

    This function does NOT emit EOC JSON (spawn triggers, delay logic, etc.).
    Those are produced separately by build_per_char_eocs(bundle.char_data) and
    concatenated with this list in seed_nemeses before writing to disk.
    """
    cd = bundle.char_data
    return [
        build_faction(cd.safe_name, mon_faction=_faction_mon(cd.death_cause)),
        build_memento_item(cd.safe_name, cd.char_name, cd.death_cause),
        build_item_group(cd.worn_gid,   cd.worn_items,    FALLBACK_WORN),
        build_item_group(cd.carry_gid,  cd.carried_items, FALLBACK_CARRY),
        build_item_group(cd.weapon_gid, [cd.weapon_item] if cd.weapon_item else [], FALLBACK_WEAPON),
        build_npc_class(
            cd.class_id, cd.char_name,
            cd.worn_gid, cd.carry_gid, cd.weapon_gid,
            bundle.bonus_str, bundle.bonus_dex, bundle.bonus_int, bundle.bonus_per,
            bundle.bionics, [], bundle.mutations, bundle.skills, cd.martial_arts,
            death_cause     = cd.death_cause,
            job_description = bundle.job_description,
        ),
        build_npc_template(cd.tmpl_id, cd.class_id, cd.safe_name, cd.char_name, cd.faction_id,
                           death_cause=cd.death_cause),
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_random_nemesis(tier: int) -> NemesisCharData:
    """Generate a single random Nemesis for the given tier (1–4) and return its data.

    This is the lightweight entry point used when the caller only needs the
    NemesisCharData dataclass — for example, to feed it into build_per_char_eocs()
    when a real player has just died and the launcher creates a live Nemesis on the
    fly (as opposed to the pre-generated seed files used for cold-start worlds).

    NemesisCharData contains: all CDDA IDs (class, template, item groups, faction,
    unique NPC id), the item lists (worn, carried, weapon), wound body-part ratios,
    spawn location condition, delay in days, death cause, and martial-arts style.
    It does NOT include stats, bionics, mutations, or skills — those only appear in
    the full _NemesisBundle / _build_npc_json path.

    To write a complete JSON file with NPC class, template, and EOCs, call
    seed_nemeses() instead.

    Raises KeyError if tier is not in {1, 2, 3, 4}.
    """
    if tier not in _TIER_MAP:
        raise KeyError(f"tier must be 1-4, got {tier!r}")
    tier_def = _TIER_MAP[tier]
    bundle = _generate_bundle(tier_def, index=random.randint(0, 9999), tier_num=tier)
    return bundle.char_data


# TEST PHASE: 40 total (15 T1 / 10 T2 / 10 T3 / 5 T4). Change to (300, 200, 200, 100) for production.
_DEFAULT_COUNTS: tuple[int, int, int, int] = (4, 3, 2, 1)


def seed_nemeses(counts: tuple[int, int, int, int], out_dir: str) -> None:
    """Write a full set of pre-generated Nemesis JSON files to disk.

    This is the primary entry point for the cold-start seed workflow.  CDDA worlds
    that start without any player deaths still need Nemeses to appear after some
    time; seed files are pre-generated synthetic characters that fill this role.
    Each seed file contains a complete, self-contained set of CDDA JSON objects:
    faction, memento item, item groups, NPC class, NPC template, and all EOCs.

    Parameters
    ----------
    counts  : (n_tier1, n_tier2, n_tier3, n_tier4) — how many Nemeses to generate
              for each tier.  Must have exactly 4 values, each >= 1.
    out_dir : Directory to write files into.  Created if it does not exist.
              Any existing nemesis_seed_*.json files in this directory are
              removed first so stale entries do not accumulate across runs.

    Output naming convention
    ------------------------
    nemesis_seed_<safe_name>.json  where safe_name = <first>_<last>_<index:04d>.
    Example: nemesis_seed_alice_smith_0042.json

    After this function returns, run:
        python3 nemesis_launcher.py
    to recompile nemesis_master.json with all seed files included.

    Raises ValueError if any count < 1 or len(counts) != 4.
    Raises RuntimeError if out_dir cannot be created or a file cannot be written.
    """
    if len(counts) != len(_TIERS):
        raise ValueError(
            f"counts must have exactly {len(_TIERS)} values (one per tier), "
            f"got {len(counts)}"
        )
    bad = [(i + 1, n) for i, n in enumerate(counts) if n < 1]
    if bad:
        raise ValueError(
            "all counts must be ≥ 1; bad tiers: "
            + ", ".join(f"T{t}={n}" for t, n in bad)
        )

    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"Cannot create output directory {out_dir!r}: {exc}") from exc

    existing = [
        f for f in os.listdir(out_dir)
        if f.startswith("nemesis_seed_") and f.endswith(".json")
    ]
    if existing:
        print(f"NEM seeder: removing {len(existing)} existing seed file(s)...")
        for fname in existing:
            os.remove(os.path.join(out_dir, fname))

    total = sum(counts)
    width = len(str(total))
    tier_counts: dict[str, int] = {t.label: 0 for t in _TIERS}
    eoc_warnings = 0
    global_idx = 0

    for (tier_int, tier_def), tier_n in zip(_TIER_MAP.items(), counts):
        for _ in range(tier_n):
            bundle   = _generate_bundle(tier_def, global_idx, tier_int)
            cd       = bundle.char_data
            npc_json = _build_npc_json(bundle)

            try:
                eoc_json: list[dict[str, Any]] = build_per_char_eocs(cd)
            except NotImplementedError:
                eoc_json = []
                eoc_warnings += 1

            combined = npc_json + eoc_json
            out_path = os.path.join(out_dir, f"nemesis_seed_{cd.safe_name}.json")

            try:
                with open(out_path, "w", encoding="utf-8") as fh:
                    json.dump(combined, fh, indent=2, ensure_ascii=False)
                    fh.write("\n")
            except OSError as exc:
                raise RuntimeError(f"Failed to write {out_path!r}: {exc}") from exc

            tier_counts[tier_def.label] += 1
            global_idx += 1
            print(
                f"  [{global_idx:>{width}}/{total}]"
                f" T{tier_int} {tier_def.label:10s}"
                f" {cd.char_name!r:25s}"
                f" delay={cd.delay_days:3d}d"
                f" oter={cd.death_oter_id or 'none'!r}"
                f" → {os.path.basename(out_path)}"
            )

    print(f"\nNEM seeder: wrote {global_idx} file(s) to {out_dir!r}")
    for label, n in tier_counts.items():
        print(f"  {label:12s}: {n}")
    if eoc_warnings:
        print(
            f"  Warning: {eoc_warnings} file(s) missing EOC JSON "
            f"(NotImplementedError — check nemesis_eoc.py stubs).",
            file=sys.stderr,
        )
    print("  Run: python3 nemesis_launcher.py   to recompile nemesis_master.json")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _positive_int(value: str) -> int:
    """argparse type converter that accepts only integers >= 1.

    Rejects zero and negative numbers with a user-readable error message.
    Used for the --counts argument so the CLI prevents degenerate inputs
    (0 Nemeses per tier) before seed_nemeses() is ever called.
    """
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' is not an integer")
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be ≥ 1, got {n}")
    return n


def main() -> None:
    """CLI entry point for the Nemesis seeder.

    Parses arguments, validates them, and delegates to seed_nemeses().  The
    caller is responsible for running nemesis_launcher.py afterward to rebuild
    nemesis_master.json.

    Arguments
    ---------
    --counts / -n  N N N N  : Nemeses per tier T1 T2 T3 T4.  Default: 15 10 10 5 (total 40, test phase).
                              Change _DEFAULT_COUNTS to (300, 200, 200, 100) for production.
                              All values must be >= 1.
    --output / -o  DIR      : Destination directory.  Defaults to the directory
                              containing this script (i.e., data/mods/NEMESIS/).
    --seed         INT      : Fixed random seed for reproducible output.  Omit for
                              random generation (default behaviour in production).

    Exit codes
    ----------
    0 : All files written successfully.
    1 : Validation error or filesystem error (message printed to stderr).
    """
    parser = argparse.ArgumentParser(
        description=(
            "NEM seeder: generate synthetic Nemesis JSON files for cold-start worlds.\n"
            "Specify per-tier counts with --counts T1 T2 T3 T4 (default: 300 200 200 100).\n"
            "Run nemesis_launcher.py afterward to rebuild nemesis_master.json."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--counts", "-n",
        type=_positive_int,
        nargs=4,
        default=list(_DEFAULT_COUNTS),
        metavar="N",
        help="Nemeses per tier T1 T2 T3 T4 (default: 300 200 200 100, total: 800).",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        metavar="DIR",
        help="Output directory (default: same directory as this script).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        metavar="SEED",
        help="Random seed for reproducible generation.",
    )
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    out_dir = args.output or os.path.join(SCRIPT_DIR, "generated")
    counts = tuple(args.counts)
    total = sum(counts)

    print(f"NEM seeder: generating T1={counts[0]} T2={counts[1]} T3={counts[2]} T4={counts[3]} = {total} total...")
    try:
        seed_nemeses(counts, out_dir)
    except (ValueError, RuntimeError) as exc:
        print(f"NEM error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
