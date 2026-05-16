#!/usr/bin/env python3
"""nemesis_builder.py -- Phase 1-3: save-file parsing, data extraction, and NPC JSON building.

PURPOSE
-------
This module is the core data layer of the NEMESIS system.  It handles three related
concerns that form an ordered pipeline:

1. READING a CDDA .sav file and its companion .log memorial log, then extracting all
   character data that is relevant for the Nemesis NPC: base stats, skills, bionics,
   mutations, worn equipment, carried inventory, weapon, body-part HP ratios, the
   overmap death location, and the cause of death.

2. CLASSIFYING the cause of death via detect_death_cause(), which queries both the
   player data dict (physical state at the moment of death: effects, HP, flags) and
   the memorial log (event substrings).  The result is a DeathCause dataclass whose
   boolean flags drive downstream trait assignment, spawn effects, and faction
   selection for the Nemesis NPC.

3. BUILDING CDDA JSON objects that define the Nemesis NPC: one faction dict, three
   item_group dicts (worn / carry / weapon), one npc_class (with stats, skills,
   bionics, mutations, and death-cause traits), and one npc_template (with inline
   NPC_DEATH EOCs for the memento item and per-cause monster spawning).

ARCHITECTURE
------------
This module does NOT produce EOC JSON.  All per-character spawn routing, wound
application, hunt-system integration, and death-message logic lives in
nemesis_eoc.py.  The split keeps each module independently testable: builder tests
can validate JSON structure without a full EOC tree, and EOC tests can mock
NemesisCharData without ever touching a save file.

PRIMARY EXPORTS
---------------
generate_npc_data(save_path, delay_days_override)
    Parse a .sav file and return (npc_json, char_data):
        npc_json  -- list of CDDA JSON dicts ready to serialize
        char_data -- NemesisCharData for nemesis_eoc.build_per_char_eocs()

NemesisCharData  -- dataclass shared with nemesis_eoc.py
DeathCause       -- dataclass carrying all death-cause flags and derived traits
"""

from __future__ import annotations

import json
import math
import os
import random
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

_PROFESSIONS_JSON: str = os.path.normpath(
    os.path.join(SCRIPT_DIR, "..", "..", "json", "professions.json")
)


# ---------------------------------------------------------------------------
# Numeric constants
# ---------------------------------------------------------------------------

CDDA_BASE_STAT = 8
TURNS_PER_DAY: int = 14_400
_DEFAULT_SEASON_DAYS: int = 91  # exported: nemesis_eoc.py uses _DEFAULT_SEASON_DAYS * 4


# ---------------------------------------------------------------------------
# NPC / item group constants
# ---------------------------------------------------------------------------

# NEMESIS_MARK is a zero-effect marker mutation required by the EOC routing system
# (EOC_NEMESIS_REVENGE_TRIGGER queries it to identify all Nemesis NPCs without
# needing per-character listeners).  All other generic "zombie boss" traits removed:
# the Nemesis is an exact mortal copy, modified only by cause of death.
NEMESIS_THEMATIC_TRAITS: list[str] = ["NEMESIS_MARK"]

# Fallback entries used when an extracted item list is empty (e.g., the dead
# character was naked or unarmed).  Choosing recognisable but mundane items avoids
# Nemeses that spawn literally empty-handed, which CDDA handles poorly.
FALLBACK_WORN:   list[str] = ["army_pants", "boots", "tshirt"]
FALLBACK_CARRY:  list[str] = ["scrap", "plastic_chunk"]
FALLBACK_WEAPON: list[str] = ["pipe"]

# Non-combat skills that are irrelevant to a Nemesis NPC's fighting capability.
# Crafting, driving, and social skills are either unused by NPCs in combat or give
# an unfair advantage that breaks game balance (e.g., a Nemesis with computer:10
# can hack doors trivially).  Combat-adjacent skills like melee, firearms, dodge,
# archery are NOT in this set and are transferred to the NPC.
_SKIP_SKILLS: frozenset[str] = frozenset({
    "barter", "speech", "driving", "cooking", "computer",
    "electronics", "fabrication", "mechanics", "tailoring",
    "chemistry", "firstaid",
})

# Power-source and metabolic CBMs that are either non-functional on NPCs or give
# disproportionate survivability.  Bio_solar and bio_metabolics in particular could
# make the Nemesis regenerate power indefinitely, which is not intended.
_SKIP_BIONICS: frozenset[str] = frozenset({
    "bio_power_storage", "bio_power_storage_mkII", "bio_power_storage_basic",
    "bio_reactor", "bio_furnace", "bio_cable", "bio_ups",
    "bio_metabolics", "bio_solar",
})

# Order of body parts in legacy .sav files (pre-"body" dict format).  Older saves
# stored HP as flat hp_cur / hp_max arrays in this fixed order instead of the
# modern named-key body dict.  Used by extract_body_part_hp_ratios() for backward
# compatibility with saves created before the CDDA body-part rework.
_LEGACY_BP_ORDER: list[str] = ["head", "torso", "arm_l", "arm_r", "leg_l", "leg_r"]


# ---------------------------------------------------------------------------
# Death-cause constants
# ---------------------------------------------------------------------------

_FIRE_EFFECT_IDS: frozenset[str] = frozenset({"onfire", "heatstroke", "blisters"})

# Traits applied per cause of death.  No generic undead traits here — only
# direct physical/biological consequences of the specific cause.
_FIRE_DEATH_TRAITS:       list[str] = ["DEFORMED", "UGLY", "FACIAL_HAIR_NONE", "M_SKIN2"]
_LAVA_DEATH_TRAITS:       list[str] = ["DEFORMED", "UGLY", "FACIAL_HAIR_NONE", "M_SKIN2"]
_INFECTION_DEATH_TRAITS:  list[str] = ["SAPIOVORE", "CANNIBAL"]
_HUNGER_DEATH_TRAITS:     list[str] = ["HUNGER3", "LIGHTWEIGHT"]
_SUICIDE_DEATH_TRAITS:    list[str] = ["QUIETMOVES"]
# Drowning: bloated corpse, no aquatic adaptations (skills["swimming"] set to 10 instead).
_DROWNING_DEATH_TRAITS:   list[str] = ["FAT"]
_TELEPORT_DEATH_TRAITS:   list[str] = ["CHAOTIC_BAD", "TERRIFYING"]
_BLOOD_LOSS_DEATH_TRAITS: list[str] = ["CANNIBAL"]
# Sewage: only vomitous remains; trail and slime omitted.
_SEWAGE_DEATH_TRAITS:     list[str] = ["VOMITOUS"]
_HEADSHOT_DEATH_TRAITS:   list[str] = ["GLASSJAW"]
_DIMENSION_DEATH_TRAITS:  list[str] = ["DEBUG_PHASE_MOVEMENT"]
_NUCLEAR_DEATH_TRAITS:    list[str] = ["CHAOTIC"]
_MUTAGEN_DEATH_TRAITS:    list[str] = ["CHAOTIC", "ANIMALDISCORD", "ANIMALEMPATH"]
_FREEZING_DEATH_TRAITS:   list[str] = ["RABBIT_FEET"]
_AUTODOC_DEATH_TRAITS:    list[str] = ["CENOBITE"]
_AMIGARA_DEATH_TRAITS:    list[str] = ["TERRIFYING", "DOWN"]
_TELEGLOW_DEATH_TRAITS:   list[str] = ["HALLUCINATION"]
_EXPLOSION_DEATH_TRAITS:  list[str] = ["DEAF", "FACIAL_HAIR_NONE"]

# All facial hair trait IDs except FACIAL_HAIR_NONE.  One is chosen at random for
# every Nemesis that has no death-cause reason to lack facial hair (i.e., not fire,
# lava, or explosion).  Without an explicit FACIAL_HAIR_* trait, CDDA NPCs render
# with no visible facial hair by default, making every Nemesis look identical.
_FACIAL_HAIR_VARIANTS: tuple[str, ...] = (
    "FACIAL_HAIR_GOATEE",     "FACIAL_HAIR_CIRCLE",     "FACIAL_HAIR_ROYALE",
    "FACIAL_HAIR_ANCHOR",     "FACIAL_HAIR_SHORTBOXED",  "FACIAL_HAIR_CHEVRON",
    "FACIAL_HAIR_3DAYSTUBBLE","FACIAL_HAIR_HORSESHOE",  "FACIAL_HAIR_MUSTACHE",
    "FACIAL_HAIR_MUTTONCHOPS","FACIAL_HAIR_GUNSLINGER",  "FACIAL_HAIR_CHIN_STRIP",
    "FACIAL_HAIR_CHIN_CURTAIN","FACIAL_HAIR_CHIN_STRAP", "FACIAL_HAIR_BEARD",
    "FACIAL_HAIR_HANDLEBAR",  "FACIAL_HAIR_NECKBEARD",  "FACIAL_HAIR_PENCIL",
    "FACIAL_HAIR_SHENANDOAH", "FACIAL_HAIR_SIDEBURNS",  "FACIAL_HAIR_SOUL_PATCH",
    "FACIAL_HAIR_TOOTHBRUSH", "FACIAL_HAIR_VANDYKE",    "FACIAL_HAIR_WALRUS",
    "FACIAL_HAIR_ZAPPA",      "FACIAL_HAIR_BEARD_LONG",  "FACIAL_HAIR_BEARD_VERY_LONG",
)
_DARKWYRM_DEATH_TRAITS:   list[str] = ["NIGHTVISION"]
# Exhaustion: LIGHTSTEP removed; only the sleep-deprivation consequences remain.
_EXHAUSTION_DEATH_TRAITS: list[str] = ["SEESLEEP", "INSOMNIA"]
_ARTIFACT_DEATH_TRAITS:   list[str] = ["HALLUCINATION"]
_ELECTRIC_DEATH_TRAITS:   list[str] = ["JITTERY"]
_POISON_DEATH_TRAITS:     list[str] = ["TOLERANCE", "NAUSEA", "ACIDBLOOD"]
# Parasites (covers both is_parasite and is_dermatik): bloated and slowed.
# Insect-form mutations (INSECT_ARMS, COMPOUND_EYES, CHITIN2) removed.
_PARASITE_DEATH_TRAITS:   list[str] = ["FAT", "PONDEROUS1"]
# Acid: flesh partially dissolved, blood turned corrosive.
_ACID_DEATH_TRAITS:       list[str] = ["DEFORMED", "UGLY", "ACIDBLOOD"]
# Asthma: the NPC carries the same condition that killed them.
_ASTHMA_DEATH_TRAITS:     list[str] = ["ASTHMA"]
# Overdose: the addiction that caused the overdose persists into undeath.
_OVERDOSE_DEATH_TRAITS:   list[str] = ["ADDICTIVE"]
# Trap: the carelessness that got them killed is baked in.
_TRAP_DEATH_TRAITS:       list[str] = ["CLUMSY"]

# Trait pools for randomised sampling (1–3 picked per character at generation time).
_MYCUS_TRAIT_POOL:   list[str] = ["THRESH_MYCUS", "M_BLOOM", "M_FERTILE", "M_PROVENANCE"]
_TRIFFID_TRAIT_POOL: list[str] = ["CHLOROMORPH", "THORNS", "ROOTS3"]
_SUBSPACE_TRAIT_POOL: list[str] = ["M_PROVENANCE", "CHAOTIC_BAD", "TERRIFYING"]

_PARASITE_EFFECT_PRIORITY: tuple[str, ...] = ("brainworms", "bloodworms", "blood_spiders")
_PARASITE_SPAWN_MAP: dict[str, tuple[str, list[int]]] = {
    "bloodworms":    ("mon_worm_larva",          [2, 4]),
    "brainworms":    ("mon_dermatik_larva",       [1, 2]),
    "blood_spiders": ("mon_spider_cellar_small",  [3, 5]),
}

_DERMATIK_SPAWN_MONSTER: str       = "mon_dermatik_larva"
_DERMATIK_SPAWN_COUNT:   list[int] = [3, 5]
_POISON_EFFECT_IDS: frozenset[str] = frozenset({
    # Toxin / chemical poisoning (zombie spitter venom, poison gas, bad mushrooms).
    "badpoison", "paralyzepoison",
    # Biological venom from spiders, snakes, and insects — same consequence as toxin deaths.
    "venom_player1", "venom_player2", "venom_dmg", "venom_weaken",
    "venom_blind", "venom_pain",
    # Food poisoning — same Nemesis consequence as sewage / toxin deaths.
    "foodpoison",
})
_TELEPORT_WALL_LIMBS: list[str] = ["arm_l", "arm_r", "leg_l", "leg_r"]

# All patterns below match substrings of the human-readable "message" (or legacy
# "preformatted") field in the memorial log JSON.  The memorial_logger.cpp source was
# audited to confirm every string — the C++ event-type enum names (e.g., "awakes_dark_wyrms")
# are NEVER written to the log file; only the translated human-readable messages are.
_AUTODOC_LOG_PATTERNS:    list[str] = [
    # event_type::fails_to_install_cbm → "Failed install of bionic: <name>."
    # event_type::fails_to_remove_cbm  → "Failed to remove bionic: <name>."
    "Failed install of bionic",
    "Failed to remove bionic",
]
_DERMATIK_LOG_PATTERNS:   list[str] = [
    # event_type::dermatik_eggs_hatch    → "Dermatik eggs hatched."
    # event_type::dermatik_eggs_injected → "Injected with dermatik eggs."
    "Dermatik eggs hatched.",
    "Injected with dermatik eggs.",
]
_AMIGARA_LOG_PATTERNS:    list[str] = [
    # event_type::angers_amigara_horrors → "Angered a group of amigara horrors!"
    "Angered a group of amigara horrors!",
]
_TELEGLOW_LOG_PATTERNS:   list[str] = [
    # event_type::teleglow_teleports → "Spontaneous teleport."
    "Spontaneous teleport.",
]
_DARKWYRM_LOG_PATTERNS:   list[str] = [
    # event_type::awakes_dark_wyrms → "Awoke a group of dark wyrms!"
    # timed_event dark-wyrm repopulation → "Drew the attention of more dark wyrms!"
    "Awoke a group of dark wyrms!",
    "Drew the attention of more dark wyrms!",
]
_TRIFFID_LOG_PATTERNS:    list[str] = [
    # event_type::destroys_triffid_grove → "Destroyed a triffid grove."
    "Destroyed a triffid grove.",
]
_EXHAUSTION_LOG_PATTERNS: list[str] = [
    # event_type::falls_asleep_from_exhaustion → "Succumbed to lack of sleep."
    "Succumbed to lack of sleep.",
]
_SUBSPACE_LOG_PATTERNS:   list[str] = [
    # event_type::releases_subspace_specimens   → "Released subspace specimens."
    # event_type::terminates_subspace_specimens → "Terminated subspace specimens."
    "Released subspace specimens.",
    "Terminated subspace specimens.",
]
_ARTIFACT_LOG_PATTERNS:   list[str] = [
    # event_type::opens_temple       → "Opened a strange temple."
    # event_type::activates_artifact → "Activated the <artifact name>."
    # The artifact name is unpredictable, so we match only the verb prefix.
    "Opened a strange temple.",
    "Activated the ",
]
# Explosion detection uses word-level search on lowercased text.
# "The fuel tank of the <vehicle> exploded!" → caught by "exploded" in _WORDS.
# "Activated a mininuke." → caught by _detect_nuclear() via "mininuke" substring;
#   not treated as a generic explosion since the blast is nuclear in nature.
_EXPLOSION_LOG_EXACT: tuple[str, ...] = ()   # no remaining exact patterns
_EXPLOSION_LOG_WORDS: tuple[str, ...] = ("explosion", "explodes", "exploded")

_FROSTBITE_TEMP_THRESHOLD: int = 3200

_TRAP_DEATH_PATTERNS: list[str] = [
    "Caught by a beartrap",
    "Stepped on a land mine",
    "Triggered a shotgun trap",
    "Triggered a crossbow trap",
    "Triggered a booby trap",
    "Fell into a spiked pit",
    "Triggered a flood trap",
    "Triggered a life-draining trap",
    "Triggered a shadow trap",
    "Lost to a pool of darkness",
]
_ELECTRIC_DEATH_LOG: str = "Stepped into an exposed high-energy conduit"

_NUCLEAR_SPAWN_EFFECTS: list[dict] = [
    {"u_add_effect": "glowing",     "duration": "PERMANENT"},
]
_TRAP_SPAWN_EFFECTS: list[dict] = [
    {"u_add_effect": "beartrap", "duration": 300},
    {"u_add_effect": "bleed",    "duration": 120},
]
_ELECTRIC_SPAWN_EFFECTS: list[dict] = [
    {"u_add_effect": "zapped",      "duration": "PERMANENT"},
]
_ASTHMA_SPAWN_EFFECTS: list[dict] = [
    {"u_add_effect": "winded",      "duration": "PERMANENT"},
]
_OVERDOSE_SPAWN_EFFECTS: list[dict] = [
    {"u_add_effect": "meth",        "duration": "PERMANENT"},
    {"u_add_effect": "adrenaline",  "duration": "PERMANENT"},
]

_TRAP_WINDOW_TURNS:    int = TURNS_PER_DAY
_MUTAGEN_WINDOW_TURNS: int = TURNS_PER_DAY // 4
_RADIATION_THRESHOLD:  int = 100

_DEATH_MESSAGE_PATTERNS: list[str] = [
    "Succumbed to the infection.",
    "Died of starvation.",
    "Died of thirst.",
    "Died of hypovolemic shock.",
    "Died from loss of red blood cells.",
    "Teleported into a ",
    "was killed.",
    "committed suicide.",
]

# Overmap terrain substrings that indicate an indoor (roofed) death location.
# Used by categorize_death_location() to refine the spawn condition beyond
# the raw levz comparison.
_INDOOR_OTER_SUBSTRINGS: frozenset[str] = frozenset({
    "house", "cabin", "shack", "office", "store", "hospital", "lab",
    "bunker", "shelter", "mall", "church", "school", "mansion", "barn",
    "garage", "factory", "warehouse", "prison", "military", "bank",
    "pharmacy", "library", "museum", "hotel", "megastore", "tower",
    "cleaners", "station", "building", "center", "centre", "restaurant",
    "diner", "gym", "laundromat", "gas_station",
})


# ---------------------------------------------------------------------------
# DeathCause dataclass
# ---------------------------------------------------------------------------

@dataclass
class DeathCause:
    """All death-cause flags for one Nemesis, plus derived traits and spawn effects.

    Every flag is a boolean that detect_death_cause() sets to True when the
    corresponding cause was detected in the .sav or memorial log.  Flags are not
    mutually exclusive: a character may have died of fire (is_fire) while also
    having radiation poisoning (is_nuclear).

    Derived data (read-only properties):
      extra_traits -- death-cause mutations and traits applied to the NPC class.
                      Ordering within the list is intentional: it matches CDDA's
                      trait-priority rules when multiple traits conflict.
      spawn_effects -- u_add_effect entries injected on spawn via the effects EOC.

    Three "pool" fields (mycus_traits_selected, triffid_traits_selected,
    subspace_traits_selected) are populated by __post_init__ from fixed pools using
    a random sample of 1-3 items.  They are marked init=False so they are invisible
    to callers and are never accidentally overwritten after construction.
    """
    is_infection:     bool = False
    is_starvation:    bool = False
    is_thirst:        bool = False
    is_suicide:       bool = False
    is_fire:          bool = False
    is_drowning:      bool = False
    is_teleport_wall: bool = False
    is_blood_loss:    bool = False
    is_sewage:        bool = False
    is_headshot:      bool = False
    is_dimension:     bool = False
    is_nuclear:       bool = False
    is_lava:          bool = False
    is_mycus:         bool = False
    is_trap:          bool = False
    is_mutagen:       bool = False
    is_freezing:      bool = False
    is_autodoc:       bool = False
    is_dermatik:      bool = False
    is_amigara:       bool = False
    is_teleglow:      bool = False
    is_explosion:     bool = False
    is_darkwyrm:      bool = False
    is_triffid:       bool = False
    is_exhaustion:    bool = False
    is_subspace:      bool = False
    is_artifact:      bool = False
    is_electric:      bool = False
    is_asthma:        bool = False
    is_overdose:      bool = False
    is_parasite:      bool = False
    parasite_effect:  str | None = None
    is_poison:        bool = False
    is_acid:          bool = False

    # Precomputed random trait samples — set once by __post_init__, never mutated.
    # init=False keeps them out of the constructor signature and detect_death_cause call.
    mycus_traits_selected:    list[str] = field(default_factory=list, init=False)
    triffid_traits_selected:  list[str] = field(default_factory=list, init=False)
    subspace_traits_selected: list[str] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        """Populate the random trait-sample fields immediately after construction.

        Mycus, Triffid, and Subspace deaths each confer a random subset of
        transformation traits (1-3 from their respective pool).  Sampling happens
        once here and is stored on the instance so every caller of extra_traits
        gets the same stable result for a given DeathCause object.  This prevents
        different parts of the pipeline from seeing different trait sets for the
        same character.
        """
        def _sample(pool: list[str]) -> list[str]:
            if not pool:
                return []
            k = random.randint(1, min(3, len(pool)))
            return random.sample(pool, k=k)

        if self.is_mycus:
            self.mycus_traits_selected = _sample(_MYCUS_TRAIT_POOL)
        if self.is_triffid:
            self.triffid_traits_selected = _sample(_TRIFFID_TRAIT_POOL)
        if self.is_subspace:
            self.subspace_traits_selected = _sample(_SUBSPACE_TRAIT_POOL)

    @property
    def is_hunger_death(self) -> bool:
        """True if the character died from starvation or thirst (both use HUNGER3 traits)."""
        return self.is_starvation or self.is_thirst

    @property
    def extra_traits(self) -> list[str]:
        """Return deduplicated death-cause traits in priority order.

        Mycus, Triffid, and Subspace use their precomputed random samples
        so each generated Nemesis gets a unique subset of transformation traits.
        Dermatik deaths share _PARASITE_DEATH_TRAITS (insect-form mutations removed).
        """
        seen: set[str] = set()
        result: list[str] = []
        for trait in (
            (_FIRE_DEATH_TRAITS         if self.is_fire          else []) +
            (_INFECTION_DEATH_TRAITS    if self.is_infection     else []) +
            (_HUNGER_DEATH_TRAITS       if self.is_hunger_death  else []) +
            (_SUICIDE_DEATH_TRAITS      if self.is_suicide       else []) +
            (_DROWNING_DEATH_TRAITS     if self.is_drowning      else []) +
            (_TELEPORT_DEATH_TRAITS     if self.is_teleport_wall else []) +
            (_BLOOD_LOSS_DEATH_TRAITS   if self.is_blood_loss    else []) +
            (_SEWAGE_DEATH_TRAITS       if self.is_sewage        else []) +
            (_HEADSHOT_DEATH_TRAITS     if self.is_headshot      else []) +
            (_DIMENSION_DEATH_TRAITS    if self.is_dimension     else []) +
            (_NUCLEAR_DEATH_TRAITS      if self.is_nuclear       else []) +
            (_LAVA_DEATH_TRAITS         if self.is_lava          else []) +
            (self.mycus_traits_selected if self.is_mycus         else []) +
            (_MUTAGEN_DEATH_TRAITS      if self.is_mutagen       else []) +
            (_FREEZING_DEATH_TRAITS     if self.is_freezing      else []) +
            (_AUTODOC_DEATH_TRAITS      if self.is_autodoc       else []) +
            (_AMIGARA_DEATH_TRAITS      if self.is_amigara       else []) +
            (_TELEGLOW_DEATH_TRAITS     if self.is_teleglow      else []) +
            (_EXPLOSION_DEATH_TRAITS    if self.is_explosion     else []) +
            (_DARKWYRM_DEATH_TRAITS     if self.is_darkwyrm      else []) +
            (self.triffid_traits_selected if self.is_triffid     else []) +
            (_EXHAUSTION_DEATH_TRAITS   if self.is_exhaustion    else []) +
            (self.subspace_traits_selected if self.is_subspace   else []) +
            (_ARTIFACT_DEATH_TRAITS     if self.is_artifact      else []) +
            (_ELECTRIC_DEATH_TRAITS     if self.is_electric      else []) +
            (_POISON_DEATH_TRAITS       if self.is_poison        else []) +
            (_ACID_DEATH_TRAITS         if self.is_acid          else []) +
            (_ASTHMA_DEATH_TRAITS       if self.is_asthma        else []) +
            (_OVERDOSE_DEATH_TRAITS     if self.is_overdose      else []) +
            (_TRAP_DEATH_TRAITS         if self.is_trap          else []) +
            # Parasite and dermatik share the same trait set (no insect mutations).
            (_PARASITE_DEATH_TRAITS     if (self.is_parasite or self.is_dermatik) else []) +
            (["NAUSEA"]      if self.parasite_effect == "bloodworms" else []) +
            (["INATTENTIVE"] if self.parasite_effect == "brainworms" else [])
        ):
            if trait not in seen:
                seen.add(trait)
                result.append(trait)
        return result

    @property
    def uses_fire_item_damage(self) -> bool:
        """True if item damage should be biased toward fire/burn damage levels.

        Fire and explosion deaths both leave scorched, charred equipment.  The
        seeder uses this flag to skew the damage roll for the Nemesis's items
        toward higher values when building item group entries.
        """
        return self.is_fire or self.is_explosion

    @property
    def spawn_effects(self) -> list[dict[str, Any]]:
        """Return the list of u_add_effect entries to apply to the NPC at spawn time.

        These are injected into EOC_NEMESIS_EFFECTS_<safe_name> via the effects EOC
        and applied to the NPC immediately after it spawns.  The effects use
        "PERMANENT" duration so they persist for the NPC's lifetime.

        Trap deaths use a short non-permanent bleed/beartrap duration to simulate
        lingering injury without permanently crippling the NPC.
        """
        effects: list[dict[str, Any]] = []
        if self.is_nuclear:  effects.extend(_NUCLEAR_SPAWN_EFFECTS)
        if self.is_trap:     effects.extend(_TRAP_SPAWN_EFFECTS)
        if self.is_electric: effects.extend(_ELECTRIC_SPAWN_EFFECTS)
        if self.is_asthma:   effects.extend(_ASTHMA_SPAWN_EFFECTS)
        if self.is_overdose: effects.extend(_OVERDOSE_SPAWN_EFFECTS)
        return effects


# ---------------------------------------------------------------------------
# NemesisCharData — transfer object passed from builder to eoc module
# ---------------------------------------------------------------------------

@dataclass
class NemesisCharData:
    """All per-character data produced by the builder and consumed by the EOC module.

    This dataclass is the interface contract between nemesis_builder.py and
    nemesis_eoc.py.  It carries everything build_per_char_eocs() needs to generate
    the five per-character EOC objects (spawn_check, spawn_success, death_notify,
    wound_eoc, effects_eoc) without calling back into the builder.

    Field notes
    -----------
    safe_name     : URL-safe ID fragment derived from char_name; used as suffix in
                    all CDDA IDs (class, template, item groups, faction, EOC IDs).
    bp_ratios     : dict[bp_id, ratio in [0.0, 1.0]].  Ratios below 1.0 are applied
                    by the wounds EOC via math expressions on u_hp / u_hp_max.
    location_cond : CDDA condition dict (or string) passed directly into the spawn-
                    check EOC's condition list.  "u_is_outside" for outdoor deaths,
                    {"not": "u_is_outside"} for indoor/underground.  None = no gate.
    death_day     : Day-of-year (modulo days_per_year) when the character died.  Used
                    to build the anniversary gate in the spawn-check EOC so the Nemesis
                    becomes eligible to spawn only on or after the anniversary of death.
    delay_days    : Minimum in-game days from world start before the Nemesis spawns.
                    Computed from stat totals + skill weight for live saves; set by
                    tier for seed Nemeses.
    death_oter_id : CDDA overmap terrain ID string at the death location (e.g.,
                    "house_north_01"), or None if the memorial log did not record one.
    martial_arts  : List of martial-arts style IDs from the player's ma_styles array.
                    Applied to the NPC via u_learn_martial_art in the effects EOC.
    """
    safe_name:     str
    char_name:     str
    tmpl_id:       str
    class_id:      str
    worn_gid:      str
    carry_gid:     str
    weapon_gid:    str
    unique_id:     str
    faction_id:    str
    bp_ratios:     dict[str, float]
    location_cond: dict[str, Any] | None
    death_day:     int | None
    days_per_year: int
    delay_days:    int
    death_cause:   DeathCause          # always set; never None
    worn_items:    list[dict[str, Any]]
    carried_items: list[dict[str, Any]]
    weapon_item:   dict[str, Any] | None
    death_omt:     tuple[int, int, int] | None
    death_oter_id: str | None          # overmap terrain type at death location
    martial_arts:  list[str]           # learned style IDs from martial_arts_data


# ---------------------------------------------------------------------------
# Save-file loading
# ---------------------------------------------------------------------------

def load_save_file(path: str) -> dict[str, Any]:
    """Read a CDDA .sav file and return its contents as a parsed dict.

    CDDA save files are JSON with leading `#`-comment lines (version info and
    metadata written by the engine) that must be stripped before parsing.
    The function removes any line whose first character is `#` and then parses
    the remaining content as standard JSON.
    """
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.readlines()
    json_lines = [l for l in lines if not l.startswith("#")]
    return json.loads("".join(json_lines))


# ---------------------------------------------------------------------------
# Field extraction helpers
# ---------------------------------------------------------------------------

def make_safe_id(name: str) -> str:
    """Convert a player name into a CDDA-safe lowercase ID fragment.

    Replaces any run of non-alphanumeric characters with a single underscore,
    lowercases the result, and strips leading/trailing underscores.  If the
    result starts with a digit (which CDDA does not allow as an ID prefix), the
    function prepends "n_".  An empty result falls back to "unknown".

    Example: "Alice O'Brien" -> "alice_o_brien"
    """
    safe = re.sub(r"[^a-zA-Z0-9]+", "_", name).lower().strip("_")
    if not safe:
        return "unknown"
    if safe[0].isdigit():
        safe = "n_" + safe
    return safe


def _sanitize_markup(text: str) -> str:
    """Strip CDDA colour-markup angle brackets from a string.

    CDDA uses <color_red>...</color> tags in item descriptions and NPC dialogue.
    These tags must be removed before embedding text in JSON fields that CDDA does
    not re-parse for markup (e.g., npc_class.name, item description strings that
    appear outside the game's markup render path).  Explicitly imported and used by
    nemesis_eoc.py as well.
    """
    return text.replace("<", "").replace(">", "")


def _iter_pocket_items(root: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield every item dict found inside root's pocket tree (breadth-first)."""
    stack = [root]
    while stack:
        item = stack.pop()
        contents = item.get("contents") or {}
        if not isinstance(contents, dict):
            continue
        for pocket in contents.get("pockets") or []:
            if not isinstance(pocket, dict):
                continue
            for child in pocket.get("items") or []:
                if not isinstance(child, dict):
                    continue
                typeid = child.get("typeid", "")
                if typeid and typeid not in ("null", "none", ""):
                    yield child
                stack.append(child)


def extract_worn(player_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the list of worn-equipment dicts from the player save data.

    In CDDA saves, player["worn"] is a flat list of item dicts.  Each dict has a
    "typeid" field identifying the item type.  Items with typeid "null", "none",
    or an empty string are placeholder entries from the engine and must be filtered
    out; they carry no meaningful data and would produce invalid item_group entries.
    """
    result: list[dict[str, Any]] = []
    for item in player_data.get("worn") or []:
        if not isinstance(item, dict):
            continue
        typeid = item.get("typeid", "")
        if typeid and typeid not in ("null", "none", ""):
            result.append(item)
    return result


def extract_carried(player_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return all carried (non-worn) item dicts from the player save data.

    CDDA's pocket system (modern saves): items carried in backpacks, vests, etc.
    are nested inside the "worn" list as pocket contents rather than in a separate
    "inv" field.  _iter_pocket_items() walks the pocket tree of each worn item
    breadth-first to collect every child item.

    Legacy flat inv structure (pre-pocket saves): carried items live in a separate
    player["inv"]["items"] list of stacks.  Each stack is either a single item dict
    or a list of item dicts.  Both forms are supported for backward compatibility
    with older save files.
    """
    result: list[dict[str, Any]] = []
    for item in player_data.get("worn") or []:
        if isinstance(item, dict):
            result.extend(_iter_pocket_items(item))

    # Legacy flat inv structure (pre-pocket saves).
    inv = player_data.get("inv") or {}
    if isinstance(inv, dict):
        for stack in inv.get("items") or []:
            if isinstance(stack, list):
                for sub in stack:
                    if isinstance(sub, dict):
                        typeid = sub.get("typeid", "")
                        if typeid and typeid not in ("null", "none", ""):
                            result.append(sub)
            elif isinstance(stack, dict):
                typeid = stack.get("typeid", "")
                if typeid and typeid not in ("null", "none", ""):
                    result.append(stack)
    return result


def extract_weapon(player_data: dict[str, Any]) -> dict[str, Any] | None:
    """Return the player's wielded weapon dict, or None if the character was unarmed.

    CDDA stores the weapon in player["weapon"].  An unarmed character has a weapon
    dict with typeid "null" or similar; these must be treated as None so the weapon
    item group falls back to FALLBACK_WEAPON rather than spawning a null item.
    """
    weapon = player_data.get("weapon") or {}
    if not isinstance(weapon, dict):
        return None
    typeid = weapon.get("typeid", "")
    return weapon if (typeid and typeid not in ("null", "none", "")) else None


def extract_bionics(player_data: dict[str, Any]) -> list[str]:
    """Return installed CBM IDs from the player's save data, minus power-source CBMs.

    CDDA stores bionics in player["my_bionics"] as a list of dicts with an "id" key.
    Power-source CBMs (reactors, solar panels, cables) are excluded via _SKIP_BIONICS
    because they either don't function on NPCs or grant disproportionate survivability.
    """
    result: list[str] = []
    for b in player_data.get("my_bionics") or []:
        if not isinstance(b, dict):
            continue
        bid = b.get("id", "")
        if bid and bid not in _SKIP_BIONICS:
            result.append(bid)
    return result


def _xp_to_level(xp: int) -> int:
    """Approximate a CDDA spell level from accumulated XP using a log2 curve.

    CDDA spell XP grows exponentially per level (roughly 75 * 2^level).  Inverting
    this gives level ~= log2(xp/75 + 1).  Clamped to [1, 9] since level 0 spells
    are not meaningful on an NPC and CDDA spell levels cap at 9 in practice.
    """
    if xp <= 0:
        return 1
    approx = int(math.log(max(1, xp / 75) + 1) / math.log(2))
    return max(1, min(approx, 9))


def extract_martial_arts(player_data: dict[str, Any]) -> list[str]:
    """Return the list of martial-arts style IDs the player had learned.

    Stored in player["martial_arts_data"]["ma_styles"] as a list of style ID
    strings.  Applied to the Nemesis NPC at spawn time via u_learn_martial_art in
    the effects EOC, giving the NPC the same fighting styles the player had.
    """
    ma_data = player_data.get("martial_arts_data") or {}
    if not isinstance(ma_data, dict):
        return []
    styles = ma_data.get("ma_styles") or []
    return [s for s in styles if isinstance(s, str) and s]


def extract_spells(player_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a list of {"id": spell_id, "level": N} dicts from the player's spellbook.

    Spell XP is stored in player["magic"]["spellbook"] as a list of dicts with
    "id" and "xp" fields.  XP is converted to a level via _xp_to_level() so the
    Nemesis NPC is built with an explicit level rather than raw XP (CDDA npc_class
    spells use the level format).
    """
    result: list[dict[str, Any]] = []
    magic = player_data.get("magic") or {}
    if not isinstance(magic, dict):
        return result
    for entry in magic.get("spellbook") or []:
        if not isinstance(entry, dict):
            continue
        sid = entry.get("id", "")
        if not sid:
            continue
        level = _xp_to_level(int(entry.get("xp", 0)))
        result.append({"id": sid, "level": level})
    return result


def extract_mutations(player_data: dict[str, Any]) -> list[str]:
    """Return a list of mutation IDs the player had at death.

    CDDA has stored mutations in two formats across versions:
    - Modern: player["mutations"] is a dict keyed by mutation ID (values are mutation
      state dicts).  The IDs are the keys.
    - Legacy: player["mutations"] is a list of strings or dicts with an "id" field.
    Both formats are handled.  Empty or falsy IDs are silently skipped.
    """
    raw = player_data.get("mutations") or {}
    if isinstance(raw, dict):
        return [k for k in raw if k]
    if isinstance(raw, list):
        result: list[str] = []
        for m in raw:
            if not m:
                continue
            mid = m.get("id", "") if isinstance(m, dict) else str(m)
            if mid:
                result.append(mid)
        return result
    return []


def extract_body_part_hp_ratios(player_data: dict[str, Any]) -> dict[str, float]:
    """Return HP ratios in [0.0, 1.0] per body part from the player save data.

    CDDA has used two save formats for body-part HP:
    - Modern (post body-part rework): player["body"] is a dict keyed by body part ID.
      Each value is a dict with "hp_cur" and "hp_max" fields.
    - Legacy: player["hp_cur"] and player["hp_max"] are parallel arrays in the order
      defined by _LEGACY_BP_ORDER (head, torso, arm_l, arm_r, leg_l, leg_r).

    To avoid spawning the Nemesis with clinically broken limbs (which causes severe
    NPC pathfinding issues), the extracted HP is smoothed by blending 80% of the
    death-time HP with 20% of max HP before computing the ratio.  This ensures the
    Nemesis always spawns functional even when the player had a body part at 0 HP.

    Returns a fallback of {bp: 1.0} for all legacy parts if no HP data is found.
    """
    ratios: dict[str, float] = {}

    body = player_data.get("body")
    if isinstance(body, dict):
        for bp_id, bp_data in body.items():
            if not isinstance(bp_data, dict):
                continue
            hp_cur = int(bp_data.get("hp_cur", 0))
            hp_max = int(bp_data.get("hp_max", 0))
            if hp_max <= 0:
                continue
            new_hp = int(hp_cur * 0.8 + hp_max * 0.2)
            ratios[bp_id] = max(0.0, min(1.0, new_hp / hp_max))
    else:
        hp_cur_arr = player_data.get("hp_cur") or []
        hp_max_arr = player_data.get("hp_max") or []
        if isinstance(hp_cur_arr, list) and isinstance(hp_max_arr, list):
            for idx, bp_id in enumerate(_LEGACY_BP_ORDER):
                if idx >= len(hp_cur_arr) or idx >= len(hp_max_arr):
                    break
                hp_cur = int(hp_cur_arr[idx])
                hp_max = int(hp_max_arr[idx])
                if hp_max <= 0:
                    continue
                new_hp = int(hp_cur * 0.8 + hp_max * 0.2)
                ratios[bp_id] = max(0.0, min(1.0, new_hp / hp_max))

    if not ratios:
        return {bp_id: 1.0 for bp_id in _LEGACY_BP_ORDER}
    return ratios


def extract_skills(player_data: dict[str, Any]) -> dict[str, int]:
    """Return a dict of {skill_id: level} for all combat-relevant skills at death.

    CDDA stores skills in player["skills"] as a dict mapping skill IDs to skill
    state objects with a "level" field, or (in very old saves) to plain integers.
    Skills in _SKIP_SKILLS (crafting, driving, social) are excluded.  Skills at
    level 0 are also excluded since they add no value to the NPC's combat profile.
    """
    result: dict[str, int] = {}
    skills_raw = player_data.get("skills") or {}
    if not isinstance(skills_raw, dict):
        return result
    for skill_id, skill_obj in skills_raw.items():
        if skill_id in _SKIP_SKILLS:
            continue
        if isinstance(skill_obj, dict):
            level = int(skill_obj.get("level", 0))
        elif isinstance(skill_obj, (int, float)):
            level = int(skill_obj)
        else:
            continue
        if level > 0:
            result[skill_id] = level
    return result


def _load_profession_raw_names() -> dict[str, Any]:
    """Load the raw profession name data from the CDDA professions.json file.

    Returns a dict mapping profession ID strings to their "name" field, which may
    be a plain string or a {"male": ..., "female": ...} dict depending on how CDDA
    defines the profession.  Returns an empty dict if the file is missing or
    unparseable (graceful fallback: extract_profession() then uses the raw ID).
    """
    if not os.path.isfile(_PROFESSIONS_JSON):
        return {}
    try:
        with open(_PROFESSIONS_JSON, encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}
    result: dict[str, Any] = {}
    for entry in data:
        if not isinstance(entry, dict) or entry.get("type") != "profession":
            continue
        pid = entry.get("id")
        if isinstance(pid, str) and pid and "name" in entry:
            result[pid] = entry["name"]
    return result


def extract_profession(player_data: dict[str, Any]) -> str | None:
    """Return the human-readable profession name for the dead character, or None.

    CDDA saves the profession as an ID string in player["profession"].  The readable
    name is looked up from professions.json, which may store it as a plain string or
    as a gender-keyed dict {"male": ..., "female": ...}.  Gender is determined from
    player["male"] (True = male, default True if absent).

    Falls back to a title-cased version of the ID if the profession is not found in
    professions.json.  Returns None only if the save has no profession field at all.
    """
    prof_id = player_data.get("profession")
    if not isinstance(prof_id, str) or not prof_id:
        return None

    is_male: bool = bool(player_data.get("male", True))
    raw_names = _load_profession_raw_names()
    raw = raw_names.get(prof_id)

    if raw is None:
        return prof_id.replace("_", " ").title()
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        gender_key = "male" if is_male else "female"
        return (
            raw.get(gender_key)
            or raw.get("male")
            or raw.get("female")
            or prof_id.replace("_", " ").title()
        )
    return prof_id.replace("_", " ").title()


def extract_death_message(log: list[dict[str, Any]]) -> str | None:
    """Return the first memorial log entry that matches a known death-message pattern.

    CDDA writes a final cause-of-death sentence to the memorial log when the
    character dies (e.g., "Succumbed to the infection.").  This function scans the
    log for any entry whose "message" or "preformatted" field contains one of the
    strings in _DEATH_MESSAGE_PATTERNS and returns the full trimmed text of the
    first match.  Returns None if no match is found (generic / modded death).
    """
    for pattern in _DEATH_MESSAGE_PATTERNS:
        for entry in log:
            if not isinstance(entry, dict):
                continue
            text = entry.get("message") or entry.get("preformatted") or ""
            if pattern in text:
                return text.strip()
    return None


def extract_last_words(log: list[dict[str, Any]]) -> str | None:
    """Return the player's last words from the memorial log, or None if absent.

    CDDA writes "Last words: <text>" to the memorial log when the player has set a
    last-words message in the character options.  This function returns the text
    portion without the prefix, stripped of whitespace.  Returns None if no
    last-words entry is found or if the entry text is empty after stripping.
    """
    prefix = "Last words: "
    for entry in log:
        if not isinstance(entry, dict):
            continue
        text = entry.get("message") or entry.get("preformatted") or ""
        if text.startswith(prefix):
            words = text[len(prefix):].strip()
            return words if words else None
    return None


def extract_location(data: dict[str, Any]) -> dict[str, Any] | None:
    """Return a simple levz-only spawn location condition (kept for backward compatibility).

    This older helper uses only the levz (surface level z-coordinate) to determine
    whether the character died indoors or outdoors: levz >= 0 maps to "u_is_outside",
    levz < 0 maps to {"not": "u_is_outside"}.

    It is superseded by categorize_death_location(), which additionally checks the
    overmap terrain ID to distinguish indoor-surface from outdoor-surface deaths.
    Kept here because nemesis_launcher.py used it in earlier versions and external
    callers may depend on the stable function signature.
    """
    levz = data.get("levz")
    if levz is None:
        return None
    return "u_is_outside" if int(levz) >= 0 else {"not": "u_is_outside"}


def extract_death_day(data: dict[str, Any], days_per_year: int) -> int | None:
    """Return the day-of-year (0-based) on which the character died, or None.

    CDDA stores the current game time in save["turn"] as an absolute turn count.
    Dividing by TURNS_PER_DAY gives the total day number; taking modulo days_per_year
    gives the day within the current year (0 = New Year's Day, 1 = day 2, etc.).
    This value is used to build the anniversary gate in the spawn-check EOC so the
    Nemesis only becomes eligible to spawn on or after the calendar anniversary of
    the player's death.  Returns None if the "turn" field is absent.
    """
    raw = data.get("turn")
    if raw is None:
        return None
    day_of_year = (int(raw) // TURNS_PER_DAY) % days_per_year
    return day_of_year


def extract_omt_position(data: dict[str, Any]) -> tuple[int, int, int] | None:
    """Return the player's OMT coordinates at death, or None if unavailable.

    Tries player["pos"] (tile coords ÷ 24) first; falls back to root-level
    levx/levy/levz (submap coords ÷ 2).
    """
    player_data = data.get("player")
    if not isinstance(player_data, dict):
        player_data = data

    pos = player_data.get("pos")
    if isinstance(pos, dict):
        x = pos.get("x")
        y = pos.get("y")
        z = pos.get("z", data.get("levz", 0))
        if isinstance(x, int) and isinstance(y, int) and isinstance(z, int):
            return (x // 24, y // 24, z)

    levx = data.get("levx")
    levy = data.get("levy")
    levz = data.get("levz")
    if isinstance(levx, int) and isinstance(levy, int) and isinstance(levz, int):
        return (levx // 2, levy // 2, levz)

    return None


def extract_death_oter_id(log: list[dict[str, Any]]) -> str | None:
    """Return the oter_id from the last memorial log entry that carries one."""
    last_oter: str | None = None
    for entry in log:
        if not isinstance(entry, dict):
            continue
        oter_id = entry.get("oter_id")
        if isinstance(oter_id, str) and oter_id:
            last_oter = oter_id
    return last_oter


def categorize_death_location(
    levz: int | None,
    oter_id: str | None,
) -> dict[str, Any] | None:
    """Derive the EOC spawn-location condition from where the character died.

    Underground (levz < 0): is_outside false.
    Surface indoor (oter_id matches a roofed terrain): is_outside false.
    Surface outdoor (or oter_id absent/unknown): is_outside true.

    The returned dict is passed directly as location_cond into the spawn-check EOC,
    so the Nemesis spawns in terrain that mirrors its death environment.
    """
    if levz is None:
        return None
    if int(levz) < 0:
        return {"not": "u_is_outside"}
    if oter_id is not None:
        lower = oter_id.lower()
        if any(kw in lower for kw in _INDOOR_OTER_SUBSTRINGS):
            return {"not": "u_is_outside"}
    return "u_is_outside"


# ---------------------------------------------------------------------------
# Death-cause detection
# ---------------------------------------------------------------------------

def _load_memorial_log(sav_path: str) -> list[dict[str, Any]]:
    """Load the companion memorial log for a .sav file and return its entries.

    CDDA writes a .log file alongside every .sav file with the same base name.
    The log is a JSON array of event dicts, each having "message" (or "preformatted")
    and "time" fields.  Like the .sav itself, the file may have leading `#` comment
    lines that must be stripped before parsing.  Returns an empty list if the .log
    file does not exist, cannot be read, or contains invalid JSON.
    """
    log_path = os.path.splitext(sav_path)[0] + ".log"
    if not os.path.isfile(log_path):
        return []
    try:
        with open(log_path, encoding="utf-8") as fh:
            lines = [ln for ln in fh if not ln.startswith("#")]
        data = json.loads("".join(lines))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _log_contains(log: list[dict[str, Any]], *substrings: str) -> bool:
    """Return True if any memorial log entry contains any of the given substrings.

    Scans every entry's "message" and "preformatted" fields.  Used for events that
    can be detected regardless of when they occurred (infection, starvation, suicide,
    etc.).  For events that are only meaningful near the time of death, use the
    time-windowed _log_contains_near_death() instead.
    """
    for entry in log:
        if not isinstance(entry, dict):
            continue
        text = entry.get("message") or entry.get("preformatted") or ""
        for sub in substrings:
            if sub in text:
                return True
    return False


def _detect_parasite_effect(player_data: dict[str, Any]) -> str | None:
    """Return the active parasite effect ID if the player had a parasitic infestation.

    Checks player["effects"] for any of the IDs in _PARASITE_EFFECT_PRIORITY
    (brainworms, bloodworms, blood_spiders) in priority order.  Priority matters
    because a character can have multiple parasites simultaneously; we pick the most
    narratively significant one for the Nemesis monster-spawn selection.  Returns
    None if the player had no parasite effects at the time of death.
    """
    effects = player_data.get("effects")
    if not isinstance(effects, dict):
        return None
    for eid in _PARASITE_EFFECT_PRIORITY:
        if eid in effects:
            return eid
    return None


def _detect_effects_intersection(
    player_data: dict[str, Any], effect_ids: frozenset[str]
) -> bool:
    """Return True if any of the given effect IDs are active in the player's effects dict.

    player["effects"] is a dict keyed by effect ID string.  A set intersection
    against the keys is the cheapest way to test membership for multiple IDs at once.
    Returns False if the effects field is missing or not a dict.
    """
    effects = player_data.get("effects")
    if not isinstance(effects, dict):
        return False
    return bool(effect_ids & effects.keys())


def _detect_fire_from_effects(player_data: dict[str, Any]) -> bool:
    """Return True if the player had active fire-related effects (onfire, heatstroke, blisters)."""
    return _detect_effects_intersection(player_data, _FIRE_EFFECT_IDS)


def _detect_drowning(player_data: dict[str, Any]) -> bool:
    """Return True if the player was underwater at the time of death (CDDA sets 'underwater': true)."""
    return bool(player_data.get("underwater", False))


def _detect_headshot_from_hp(player_data: dict[str, Any]) -> bool:
    """Return True if the player's head HP was exactly 0 at death (proxy for headshot).

    CDDA does not explicitly log "headshot" as a death cause, so we infer it from
    head HP being zero in the body-part data.  This is a heuristic: melee overkill
    that happened to hit the head repeatedly can also produce hp_cur == 0 for the
    head.  Both modern body dict and legacy hp_cur array formats are handled.
    """
    body = player_data.get("body")
    if isinstance(body, dict):
        head = body.get("head")
        if isinstance(head, dict):
            return int(head.get("hp_cur", -1)) == 0
    else:
        hp_cur_arr = player_data.get("hp_cur") or []
        if isinstance(hp_cur_arr, list) and len(hp_cur_arr) > 0:
            return int(hp_cur_arr[0]) == 0
    return False


def _log_max_time(log: list[dict[str, Any]]) -> int | None:
    """Return the maximum "time" value across all memorial log entries, or None.

    Used by _log_contains_near_death() to anchor the time window relative to the
    last logged event (which is typically the death event or very close to it).
    """
    max_t: int | None = None
    for entry in log:
        if not isinstance(entry, dict):
            continue
        t = entry.get("time")
        if isinstance(t, int) and (max_t is None or t > max_t):
            max_t = t
    return max_t


def _log_contains_near_death(
    log: list[dict[str, Any]],
    *substrings: str,
    window_turns: int,
) -> bool:
    """Return True if any log entry within the time window contains any of the substrings.

    Some events are only meaningful as a cause of death if they occurred recently
    before the character died (e.g., "lava" or "mutagen." could appear in the log
    from encounters months earlier and should not count as the death cause).
    The window is defined as [max_time - window_turns, max_time], where max_time
    is the timestamp of the most recent log entry (effectively the death moment).
    Entries outside the window are ignored.
    """
    max_t = _log_max_time(log)
    if max_t is None:
        return False
    cutoff = max_t - window_turns
    for entry in log:
        if not isinstance(entry, dict):
            continue
        t = entry.get("time")
        if not isinstance(t, int) or t < cutoff:
            continue
        text = entry.get("message") or entry.get("preformatted") or ""
        for sub in substrings:
            if sub in text:
                return True
    return False


def _detect_nuclear(
    player_data: dict[str, Any],
    log: list[dict[str, Any]],
) -> bool:
    """Return True if the player died from radiation or a mininuke detonation.

    Two signals are checked: (1) player["radiation"] >= _RADIATION_THRESHOLD (100),
    which indicates lethal radiation sickness; (2) the memorial log contains the
    word "mininuke", which covers both self-detonation and being caught in a
    mininuke blast.  Either signal is sufficient.
    """
    rad = player_data.get("radiation", 0)
    if isinstance(rad, (int, float)) and int(rad) >= _RADIATION_THRESHOLD:
        return True
    return _log_contains(log, "mininuke")


def _detect_freezing(player_data: dict[str, Any]) -> bool:
    """Return True if the player had active frostbite or hypothermia at death.

    Checks two signals in the modern body dict format: a positive frostbite_timer
    on any body part, or a temp_cur below _FROSTBITE_TEMP_THRESHOLD (3200) on any
    part.  CDDA uses an internal temperature scale where normal body temperature is
    around 3700; below 3200 is the threshold for severe hypothermia risk.  Returns
    False if the save uses the legacy format (no body dict).
    """
    body = player_data.get("body")
    if not isinstance(body, dict):
        return False
    for bp_data in body.values():
        if not isinstance(bp_data, dict):
            continue
        if int(bp_data.get("frostbite_timer", 0)) > 0:
            return True
        temp_cur = bp_data.get("temp_cur")
        if isinstance(temp_cur, (int, float)) and temp_cur < _FROSTBITE_TEMP_THRESHOLD:
            return True
    return False


def _detect_explosion(log: list[dict[str, Any]]) -> bool:
    """Return True if the memorial log suggests the player died in an explosion.

    Uses a time window (_TRAP_WINDOW_TURNS = 1 day) rather than scanning the entire
    log, because explosion-related words ("explosion", "explodes", "exploded") appear
    in ordinary combat log entries throughout a character's life — a fuel tank
    explosion two months ago must not trigger DEAF traits for a character who later
    died of starvation.  Exact patterns (_EXPLOSION_LOG_EXACT) and keyword patterns
    (_EXPLOSION_LOG_WORDS) are both checked within the window.
    """
    return _log_contains_near_death(
        log,
        *_EXPLOSION_LOG_EXACT,
        *_EXPLOSION_LOG_WORDS,
        window_turns=_TRAP_WINDOW_TURNS,
    )


def detect_death_cause(
    player_data: dict[str, Any],
    log: list[dict[str, Any]],
) -> DeathCause:
    """Analyse the player's save state and memorial log to determine how the character died.

    Returns a fully-populated DeathCause dataclass.  All flags are independent of
    each other — multiple can be True simultaneously (e.g., a character who drowned
    in a nuclear-contaminated area will have both is_drowning and is_nuclear set).

    Detection uses two complementary sources:
    - player_data: physical state at the moment of saving (HP, active effects,
      radiation level, underwater flag, body temperature).
    - log: memorial log entries (event substrings, timestamps).

    Some causes use a time window (lava, mutagen, traps, freezing): if the event
    happened months before death it is not treated as the cause.  Others use
    unconditional log search (infection, starvation, suicide) because those events
    are definitive and don't become false positives over time.

    The constructed DeathCause also triggers __post_init__, which immediately samples
    the random trait pools for mycus / triffid / subspace deaths.
    """
    parasite_effect = _detect_parasite_effect(player_data)
    return DeathCause(
        is_infection     = _log_contains(log, "Succumbed to the infection."),
        is_starvation    = _log_contains(log, "Died of starvation."),
        is_thirst        = _log_contains(log, "Died of thirst."),
        is_suicide       = _log_contains(log, "committed suicide."),
        is_fire          = _detect_fire_from_effects(player_data),
        is_drowning      = _detect_drowning(player_data),
        is_teleport_wall = _log_contains(log, "Teleported into a "),
        is_blood_loss    = _log_contains(
            log,
            # event_type::dies_from_bleeding      → "Bled to death."
            # event_type::dies_from_hypovolemia   → "Died of hypovolemic shock."
            # event_type::dies_from_redcells_loss → "Died from loss of red blood cells."
            "Bled to death.",
            "Died of hypovolemic shock.",
            "Died from loss of red blood cells.",
        ),
        is_sewage    = _log_contains(log, "Ate a sewage sample."),
        is_headshot  = _detect_headshot_from_hp(player_data),
        is_dimension = _log_contains(log, "Traveled from ", "resonance cascade"),
        is_nuclear   = _detect_nuclear(player_data, log),
        is_lava      = _log_contains_near_death(
            log, "lava", window_turns=_TRAP_WINDOW_TURNS
        ),
        is_mycus     = _log_contains_near_death(
            log, "marloss", "Marloss", window_turns=_TRAP_WINDOW_TURNS
        ),
        is_trap      = _log_contains_near_death(
            log, *_TRAP_DEATH_PATTERNS, window_turns=_TRAP_WINDOW_TURNS
        ),
        is_mutagen   = _log_contains_near_death(
            log, "mutagen.", window_turns=_MUTAGEN_WINDOW_TURNS
        ),
        is_freezing  = _detect_freezing(player_data),
        is_autodoc   = _log_contains(log, *_AUTODOC_LOG_PATTERNS),
        is_dermatik  = _log_contains(log, *_DERMATIK_LOG_PATTERNS),
        is_amigara   = _log_contains(log, *_AMIGARA_LOG_PATTERNS),
        is_teleglow  = _log_contains(log, *_TELEGLOW_LOG_PATTERNS),
        is_explosion = _detect_explosion(log),
        is_darkwyrm  = _log_contains(log, *_DARKWYRM_LOG_PATTERNS),
        is_triffid   = _log_contains(log, *_TRIFFID_LOG_PATTERNS),
        is_exhaustion = _log_contains_near_death(
            log, *_EXHAUSTION_LOG_PATTERNS, window_turns=_TRAP_WINDOW_TURNS
        ),
        is_subspace  = _log_contains(log, *_SUBSPACE_LOG_PATTERNS),
        is_artifact  = _log_contains(log, *_ARTIFACT_LOG_PATTERNS),
        is_electric  = _log_contains_near_death(
            log, _ELECTRIC_DEATH_LOG, window_turns=_TRAP_WINDOW_TURNS
        ),
        # event_type::dies_from_asthma_attack → "Succumbed to an asthma attack."
        is_asthma    = _log_contains(log, "Succumbed to an asthma attack."),
        # event_type::dies_from_drug_overdose writes one of five cause-specific messages.
        is_overdose  = _log_contains(
            log,
            "Died of datura overdose.",
            "Died of a healing stimulant overdose.",
            "Died of adrenaline overdose.",
            "Died of an alcohol overdose.",
            "Died of a drug overdose.",
        ),
        is_parasite     = parasite_effect is not None,
        parasite_effect = parasite_effect,
        is_poison    = _detect_effects_intersection(player_data, _POISON_EFFECT_IDS),
        is_acid      = _detect_effects_intersection(player_data, {"corroding"}),
    )


# ---------------------------------------------------------------------------
# JSON object builders (NPC-side only; EOC builders live in nemesis_eoc.py)
# ---------------------------------------------------------------------------

def build_narrative_description(
    profession_name: str | None,
    death_message: str | None,
    last_words: str | None,
    death_cause: "DeathCause | None" = None,
) -> str:
    """Compose the narrative job_description string for the Nemesis NPC class.

    Two-part format:
      Famous last words: "[last_words]" [humorous one-liner about how they died].

    If last_words is absent, the quote slot is filled with "..." to signal silence
    rather than omitting the field entirely — the label is always present.
    If no cause-specific line is available, a plain fallback closes the description.
    Markup angle brackets are stripped from all source strings to avoid corrupting
    the JSON.  profession_name and death_message are retained as fallback context
    but are no longer the primary structure.
    """
    quote = _sanitize_markup(last_words).strip() if last_words else "..."
    cause_line = _job_desc_cause_line(death_cause)
    return f'Famous last words: "{quote}" {cause_line}'


def _item_to_group_entry(item: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a save-file item dict to an item_group entry.

    Preserves typeid, damage level (per-mille to 0-5 scale), and charge count.
    Returns None for null/empty typeids.
    CDDA damage in saves: 0-4000 per-mille (modern) or 0-5 direct levels (legacy).
    """
    typeid = item.get("typeid", "")
    if not typeid or typeid in ("null", "none", ""):
        return None

    entry: dict[str, Any] = {"item": typeid, "prob": 100}

    raw_damage = item.get("damage", 0)
    if isinstance(raw_damage, (int, float)):
        raw = int(raw_damage)
        level = min(4, raw // 1000) if raw > 5 else raw
        if level > 0:
            entry["damage"] = [level, level]

    charges = item.get("charges")
    if isinstance(charges, int) and charges > 0:
        entry["charges"] = [charges, charges]

    if item.get("ammo_item"):
        entry["ammo-item"] = item["ammo_item"]

    if item.get("filthy"):
        entry["custom-flags"] = ["FILTHY"]

    return entry


def build_item_group(
    group_id: str,
    items: list[dict[str, Any]],
    fallback_typeids: list[str],
) -> dict[str, Any]:
    """Build an item_group from complete save-file item dicts (exact-copy approach).

    No artificial damage, FILTHY flags, or flammability filtering applied.
    Falls back to plain fallback_typeids when the extracted list is empty.
    """
    entries = [e for e in (_item_to_group_entry(i) for i in items) if e is not None]
    if not entries:
        entries = [{"item": t, "prob": 100} for t in fallback_typeids]
    return {
        "type": "item_group",
        "id": group_id,
        "subtype": "collection",
        "entries": entries,
    }


def build_faction(safe_name: str, mon_faction: str = "zombie") -> dict[str, Any]:
    """Build a unique per-character CDDA faction dict for the Nemesis.

    Each Nemesis gets its own faction so CDDA's faction-hostility system correctly
    treats all spawned copies of this Nemesis as the same entity.  The faction is
    pre-configured to be hostile to all major NPC-defended settlement factions
    (old_guard, free_merchants, etc.) so Nemeses are hunted by friendly NPCs and
    cannot shelter in settlements.

    mon_faction controls which monster faction the Nemesis aligns with on the
    overmap (affects which monsters will attack the Nemesis vs. ignore it).
    The default "zombie" makes the Nemesis hostile to zombies; mycus/triffid deaths
    use the corresponding faction to align the Nemesis with those factions.
    """
    return {
        "type": "faction",
        "id": f"nemesis_faction_{safe_name}",
        "name": f"Nemesis ({safe_name})",
        "likes_u": -100,
        "respects_u": -100,
        "known_by_u": True,
        "description": "A nemesis returned from death.",
        "size": 1,
        "power": 100,
        "wealth": 0,
        "fac_food_supply": [[0, {"calories": 0, "vitamins": {}}]],
        "mon_faction": mon_faction,
        "relations": {
            # The Nemesis attacks all NPC-defended settlement factions on sight.
            # Their members retaliate, making settlements actively hostile to the Nemesis.
            "old_guard":      {"kill on sight": True},
            "free_merchants": {"kill on sight": True},
            "tacoma_commune": {"kill on sight": True},
            "your_followers": {"kill on sight": True},
            "robofac":        {"kill on sight": True},
            "exodii":         {"kill on sight": True},
        },
    }


def build_npc_class(
    class_id: str,
    char_name: str,
    worn_gid: str,
    carry_gid: str,
    weapon_gid: str,
    bonus_str: int,
    bonus_dex: int,
    bonus_int: int,
    bonus_per: int,
    bionics: list[str],
    spells: list[dict[str, Any]],
    mutations: list[str],
    skills: dict[str, int],
    martial_arts: list[str],
    death_cause: DeathCause | None = None,
    job_description: str = "A tormented soul, driven by vengeance.",
) -> dict[str, Any]:
    """Build the CDDA npc_class JSON object for the Nemesis.

    The npc_class controls the NPC's base stats (as deltas above CDDA_BASE_STAT),
    equipment sources (via worn_override / carry_override / weapon_override pointing
    to item_group IDs), and the full trait / bionic / spell / skill / style lists.

    Trait ordering: mutations first (player's own traits), then NEMESIS_THEMATIC_TRAITS
    (the NEMESIS_MARK technical marker), then death-cause traits (DeathCause.extra_traits).
    dict.fromkeys preserves insertion order while deduplicating so no trait appears
    twice even if the player already had a trait that matches a death-cause trait.

    bonus_str/dex/int/per are stat values above CDDA_BASE_STAT (8).  A value of 4
    means the NPC has an effective stat of 12.  These come from the player's str_max
    etc. fields minus CDDA_BASE_STAT.

    martial_arts is passed through for documentation but is NOT applied here — it is
    applied at spawn time via u_learn_martial_art in the effects EOC.  The npc_class
    itself does not have a martial-arts field.
    """
    extra      = death_cause.extra_traits if death_cause is not None else []

    # Assign a random facial hair style unless one is already present.  Without an
    # explicit FACIAL_HAIR_* trait, CDDA renders NPCs with no facial hair by default —
    # every Nemesis looks identical.  Death causes that naturally destroy facial hair
    # (fire, lava, explosion) already include FACIAL_HAIR_NONE in their extra_traits,
    # so the check below will find it and skip the random assignment.  Real player
    # saves may also carry a FACIAL_HAIR_* trait from character creation, which is
    # preserved as-is via the mutations list.
    combined_for_hair_check = mutations + extra
    if not any(t.startswith("FACIAL_HAIR_") for t in combined_for_hair_check):
        extra = extra + [random.choice(_FACIAL_HAIR_VARIANTS)]

    # mutations first (player's own), then NEMESIS_MARK (technical marker only),
    # then death-cause traits.  dict.fromkeys preserves order and deduplicates.
    all_traits = list(dict.fromkeys(mutations + NEMESIS_THEMATIC_TRAITS + extra))
    trait_entries: list[list[Any]] = [[t, 100] for t in all_traits]

    obj: dict[str, Any] = {
        "type": "npc_class",
        "id": class_id,
        "name": {"str": f"Nemesis ({_sanitize_markup(char_name)})"},
        "job_description": job_description,
        "common": False,
        "bonus_str": bonus_str,
        "bonus_dex": bonus_dex,
        "bonus_int": bonus_int,
        "bonus_per": bonus_per,
        "worn_override": worn_gid,
        "carry_override": carry_gid,
        "weapon_override": weapon_gid,
    }

    if trait_entries:
        obj["traits"] = trait_entries

    if bionics:
        obj["bionics"] = [{"id": [b], "chance": 100} for b in bionics]

    if spells:
        obj["spells"] = spells

    if skills:
        obj["skills"] = [
            {"skill": sid, "level": {"constant": lvl}}
            for sid, lvl in sorted(skills.items())
        ]

    return obj


def _job_desc_cause_line(death_cause: "DeathCause | None") -> str:
    """Return a short, slightly humorous one-liner describing how the Nemesis died.

    Used as the second half of the job_description field shown when the player
    examines the Nemesis NPC.  The tone is dry and matter-of-fact — absurdity
    arises from specificity, not from jokes.  All lines are complete sentences.
    Order of checks mirrors _death_narrative() for maintainability.
    """
    dc = death_cause
    if dc is None:
        return "Cause of death: the apocalypse, probably."
    if dc.is_drowning:
        return "Went for a swim. Didn't come back."
    if dc.is_lava:
        return "Found out lava is hot. First-hand."
    if dc.is_fire:
        return "Got too close to a fire. Literally."
    if dc.is_freezing:
        return "Didn't dress for the weather."
    if dc.is_blood_loss:
        return "Ran out of blood."
    if dc.is_infection:
        return "Ignored a small scratch until it wasn't small anymore."
    if dc.is_overdose:
        return "Took one pill too many."
    if dc.is_sewage:
        return "Drank too much sewer water."
    if dc.is_acid:
        return "Got dissolved. Partially."
    if dc.is_poison:
        return "Got poisoned. Something bit, stung, or oozed."
    if dc.is_electric:
        return "Touched a live wire. Briefly."
    if dc.is_explosion:
        return "Stood too close to a very big boom."
    if dc.is_headshot:
        return "Didn't see it coming."
    if dc.is_dermatik:
        return "Became a wasp nursery."
    if dc.is_parasite:
        return "Something moved in and never moved out."
    if dc.is_mycus:
        return "Got too friendly with the mushrooms."
    if dc.is_triffid:
        return "Got eaten by a plant."
    if dc.is_darkwyrm or dc.is_dimension or dc.is_subspace or dc.is_teleglow:
        return "Stepped through a portal that didn't go anywhere good."
    if dc.is_amigara:
        return "Found a hole that fit them exactly. Walked in. Didn't walk out."
    if dc.is_nuclear:
        return "Stood downwind of something glowing."
    if dc.is_starvation:
        return "Forgot to eat. For too long."
    if dc.is_thirst:
        return "Died of thirst. Somehow, in a world full of puddles."
    if dc.is_exhaustion:
        return "Sat down and didn't get back up."
    if dc.is_suicide:
        return "Made a decision."
    if dc.is_mutagen:
        return "Took one mutation too many."
    if dc.is_teleport_wall:
        return "Teleported into solid rock."
    if dc.is_trap:
        return "The floor was a trap. Literally."
    if dc.is_asthma:
        return "Forgot their inhaler."
    if dc.is_autodoc:
        return "Let a robot perform surgery. Unsupervised."
    if dc.is_artifact:
        return "Played with something they didn't understand."
    return "Cause of death: the apocalypse, probably."


def _death_narrative(char_name: str, death_cause: "DeathCause | None") -> str:
    """Return a plain-text death narrative for the given character and cause.

    Used in two contexts: (1) as the second half of the memento item's description,
    and (2) as the u_message text in EOC_NEMESIS_DEATH_NOTIFY.  No CDDA colour
    markup is included here; callers wrap the text in <color_green>...</color>
    themselves if needed.  Returns the generic "found peace" fallback when no
    specific cause matches or cause is None (unknown / generic death).
    """
    n  = char_name
    dc = death_cause
    if dc is None:
        return f"The tortured soul of {n} has finally found peace."
    if dc.is_drowning:
        return f"{n} slips beneath still waters — as before."
    if dc.is_lava:
        return f"The heat that unmade {n} finishes what it started."
    if dc.is_fire:
        return f"The fire that took {n} claims them once more."
    if dc.is_freezing:
        return f"The cold takes {n} a second time."
    if dc.is_blood_loss:
        return f"{n} bleeds out — as in their final hour."
    if dc.is_infection:
        return f"The infection has run its course. {n} is gone."
    if dc.is_acid:
        return f"The acid that unmade {n} finishes its work."
    if dc.is_poison or dc.is_overdose:
        return f"The toxin that ended {n} has done so again."
    if dc.is_electric:
        return f"The current that took {n} completes its circuit."
    if dc.is_explosion:
        return f"{n} goes out as they came in — with a bang."
    if dc.is_headshot:
        return f"A clean end for {n}. As clean as it gets."
    if dc.is_dermatik:
        return f"The host expires. {n} is gone at last."
    if dc.is_parasite:
        return f"The parasite and host are gone together. {n} rests."
    if dc.is_mycus:
        return f"The fungus reclaims what was {n}."
    if dc.is_triffid:
        return f"Returned to the earth. {n} is at peace."
    if dc.is_darkwyrm or dc.is_dimension or dc.is_subspace or dc.is_teleglow:
        return f"{n} dissolves back into the void from which they came."
    if dc.is_amigara:
        return f"{n} finds their form at last and releases it."
    if dc.is_nuclear:
        return f"The radiation that unmade {n} finishes the work."
    if dc.is_starvation or dc.is_thirst or dc.is_exhaustion:
        return f"{n} is finally allowed to rest."
    if dc.is_suicide:
        return f"{n} finds the peace they once sought."
    if dc.is_mutagen:
        return f"What remains of {n} dissolves at last."
    if dc.is_teleport_wall:
        return f"{n} phases out — permanently this time."
    if dc.is_trap:
        return f"{n} falls once more — this time for good."
    if dc.is_asthma:
        return f"{n} draws their final breath."
    if dc.is_artifact:
        return f"The artifact's work is undone. {n} is free."
    return f"The tortured soul of {n} has finally found peace."


def _memento_description(death_cause: "DeathCause | None") -> str:
    """Return a single atmospheric line hinting at how the Nemesis originally died.

    Written from the player's perspective as they examine the memento item found on
    the Nemesis's corpse.  The line is embedded inside the item's description field
    preceded by a quote marker, followed by the full _death_narrative sentence.
    Returns the generic "tattered remains" fallback for unknown or None causes.
    """
    dc = death_cause
    if dc is None:
        return "The tattered remains hint at a life lived — and lost — before yours began."
    if dc.is_drowning:
        return "Cold to the touch. Heavier than it should be."
    if dc.is_lava:
        return "The heat never fully left it. Careful."
    if dc.is_fire:
        return "The edges are charred. The smell of smoke never quite fades."
    if dc.is_freezing:
        return "It carries a chill that shouldn't be there. The cold never let them go."
    if dc.is_blood_loss:
        return "A dark stain that won't wash out. Something bled here."
    if dc.is_infection:
        return "Slightly warm. Feverish, almost. Whatever took them lingers."
    if dc.is_poison or dc.is_overdose:
        return "A faint bitterness in the air around it. Handle with care."
    if dc.is_acid:
        return "The surface is pitted and eaten away. Something corrosive happened here."
    if dc.is_sewage:
        return "It smells faintly of the pipes. You don't want to know."
    if dc.is_autodoc:
        return "Clean incision marks along the edges. Surgery. Unsupervised."
    if dc.is_electric:
        return "A faint static. The hairs on your arm stand up."
    if dc.is_explosion:
        return "Scorched and warped. It survived something that shouldn't have been survived."
    if dc.is_headshot:
        return "Clean. Precise. Someone made sure of it."
    if dc.is_dermatik:
        return "Something moved inside this, once."
    if dc.is_parasite:
        return "Two lives ended here. One of them wasn't willing."
    if dc.is_mycus:
        return "Faint spores cling to the surface. Don't breathe too deep."
    if dc.is_triffid:
        return "Root-marks, like something tried to grow through it."
    if dc.is_darkwyrm or dc.is_dimension or dc.is_subspace or dc.is_teleglow:
        return "It shouldn't be here. Neither should you."
    if dc.is_amigara:
        return "The shape of it feels wrong. Like it belongs somewhere else entirely."
    if dc.is_nuclear:
        return "The counter clicks softly. Don't hold it long."
    if dc.is_starvation or dc.is_thirst or dc.is_exhaustion:
        return "Worn down to almost nothing. Like them."
    if dc.is_suicide:
        return "Left behind deliberately. A final decision made in full."
    if dc.is_mutagen:
        return "The material has changed. Hard to say what it was before."
    if dc.is_teleport_wall:
        return "Half of it seems to be somewhere else entirely."
    if dc.is_trap:
        return "Bent and sprung. Someone's last surprise."
    if dc.is_asthma:
        return "Lightweight. They were carrying little at the end."
    if dc.is_artifact:
        return "It hums faintly. Whatever they touched, some of it stayed."
    return "The tattered remains hint at a life lived — and lost — before yours began."


def build_memento_item(
    safe_name: str,
    char_name: str,
    death_cause: "DeathCause | None" = None,
) -> dict[str, Any]:
    """Build a per-nemesis memento item with an atmospheric, cause-driven description."""
    memo      = _memento_description(death_cause)
    narrative = _death_narrative(char_name, death_cause)
    return {
        "type":        "ITEM",
        "id":          f"nemesis_memento_{safe_name}",
        "name":        {"str": f"memento — {char_name}"},
        "description": f"\"{memo}\" - {narrative}",
        "weight":      "50 g",
        "volume":      "250 ml",
        "price":       "0 cent",
        "material":    ["leather"],
        "symbol":      "?",
        "color":       "dark_gray",
        "flags":       ["NO_REPAIR"],
    }


def build_npc_template(
    template_id: str,
    class_id: str,
    safe_name: str,
    char_name: str,
    faction_id: str,
    death_cause: DeathCause | None = None,
) -> dict[str, Any]:
    """Build the CDDA npc template dict that instantiates the Nemesis as a unique NPC.

    The npc template wires together: the npc_class (equipment and stats), the unique
    NPC ID (for u_spawn_npc / u_run_npc_eocs targeting), the faction, and a list of
    inline NPC_DEATH EOCs that fire when this NPC is killed.

    Inline death EOCs (applied in order):
    1. Base death EOC (always present): sets global_nemesis_just_died_<safe_name>=1
       (signals the death-notify relay) and spawns the memento item on the corpse.
    2. Dermatik death EOC (if is_dermatik): spawns mon_dermatik_larva x[3,5] nearby.
    3. Parasite death EOC (if is_parasite): spawns the appropriate parasite monster
       from _PARASITE_SPAWN_MAP based on the active parasite effect.
    4. Mycus death EOC (if is_mycus): spawns mon_spore x[2,4] nearby.

    attitude=10 is CDDA's "hostile" stance; mission=0 and chat="TALK_DONE" prevent
    the NPC from initiating dialogue or missions.
    """
    just_died_var = f"global_nemesis_just_died_{safe_name}"
    # global_nemesis_spawned_<safe_name> = 3 permanently marks this Nemesis as dead
    # so EOC_NEMESIS_HUNT_REDIRECT_<safe_name> (which gates on spawned == 2) stops
    # calling u_run_npc_eocs for this NPC.  Without this, CDDA emits a debugmsg on
    # every OMT crossing because unique_npc_exists() returns true for dead Nemeses
    # (the unique_npcs registry is not erased on death, only on unique_npc_despawn)
    # while find_npc_by_unique_id() returns nullptr (NPC removed from overmap buffer).
    spawned_var = f"global_nemesis_spawned_{safe_name}"
    death_eocs: list[dict[str, Any]] = [
        {
            "id": f"EOC_NEMESIS_DEATH_inline_{template_id}",
            "eoc_type": "NPC_DEATH",
            "effect": [
                {"math": [f"{just_died_var} = 1"]},
                {"math": [f"{spawned_var} = 3"]},
                {"u_spawn_item": f"nemesis_memento_{safe_name}", "count": 1},
            ],
        }
    ]
    if death_cause is not None and death_cause.is_dermatik:
        death_eocs.append({
            "id": f"EOC_NEMESIS_DEATH_DERMATIK_{safe_name}",
            "eoc_type": "NPC_DEATH",
            "effect": [
                {
                    "u_spawn_monster": _DERMATIK_SPAWN_MONSTER,
                    "real_count": _DERMATIK_SPAWN_COUNT,
                    "min_radius": 1,
                    "max_radius": 3,
                }
            ],
        })
    if death_cause is not None and death_cause.is_parasite:
        monster_id, count_range = _PARASITE_SPAWN_MAP.get(
            death_cause.parasite_effect or "",
            ("mon_spider_widow_small", [3, 5]),
        )
        death_eocs.append({
            "id": f"EOC_NEMESIS_DEATH_PARASITE_{safe_name}",
            "eoc_type": "NPC_DEATH",
            "effect": [
                {
                    "u_spawn_monster": monster_id,
                    "real_count": count_range,
                    "min_radius": 1,
                    "max_radius": 3,
                }
            ],
        })
    if death_cause is not None and death_cause.is_mycus:
        death_eocs.append({
            "id": f"EOC_NEMESIS_DEATH_MYCUS_{safe_name}",
            "eoc_type": "NPC_DEATH",
            "effect": [
                {
                    "u_spawn_monster": "mon_spore",
                    "real_count": [2, 4],
                    "min_radius": 0,
                    "max_radius": 2,
                }
            ],
        })
    return {
        "type": "npc",
        "id": template_id,
        "name_unique": _sanitize_markup(char_name),
        "class": class_id,
        "attitude": 10,
        "mission": 0,
        "chat": "TALK_DONE",
        "faction": faction_id,
        "death_eocs": death_eocs,
    }


# ---------------------------------------------------------------------------
# Main assembly: the sole public API consumed by nemesis_launcher.py
# ---------------------------------------------------------------------------

def generate_npc_data(
    save_path: str,
    delay_days_override: int | None = None,
) -> tuple[list[dict[str, Any]], NemesisCharData]:
    """Parse a CDDA .sav file and build all NPC-side JSON objects for the Nemesis.

    This is the primary public entry point for the live-Nemesis path (called by
    nemesis_launcher.py when a real player character dies).  It reads the save file,
    loads the companion memorial log, extracts all relevant data, and assembles the
    CDDA JSON objects needed to define the NPC.

    Parameters
    ----------
    save_path           : Absolute or relative path to the dead character's .sav file.
                          The companion .log file is loaded from the same directory
                          with the same base name.
    delay_days_override : If provided, overrides the computed spawn delay.  Useful
                          for testing (pass 1 to spawn immediately) or for manual
                          control of Nemesis difficulty.  If None, the delay is
                          computed as stat_sum + skill_weight (stronger characters
                          take longer to return, giving the player more preparation time).

    Returns
    -------
    npc_json  : List of CDDA JSON dicts: [faction, worn_group, carry_group,
                weapon_group, npc_class, npc_template].  Ready to serialize.
    char_data : NemesisCharData for passing to nemesis_eoc.build_per_char_eocs().
                Contains all IDs, item lists, BP ratios, location condition, delay,
                and the DeathCause object.
    """
    data = load_save_file(save_path)
    # Load memorial log early — needed for oter_id-based location categorization
    # as well as death-cause detection and narrative extraction.
    log  = _load_memorial_log(save_path)

    season_days:   int = int(data.get("initial_season", _DEFAULT_SEASON_DAYS))
    days_per_year: int = season_days * 4

    levz          = data.get("levz")
    oter_id       = extract_death_oter_id(log)
    location_cond = categorize_death_location(levz, oter_id)
    death_day     = extract_death_day(data, days_per_year)

    player_data = data.get("player")
    if not isinstance(player_data, dict):
        player_data = data

    char_name = player_data.get("name", "Unknown")
    str_max   = int(player_data.get("str_max", CDDA_BASE_STAT))
    dex_max   = int(player_data.get("dex_max", CDDA_BASE_STAT))
    int_max   = int(player_data.get("int_max", CDDA_BASE_STAT))
    per_max   = int(player_data.get("per_max", CDDA_BASE_STAT))

    safe       = make_safe_id(char_name)
    class_id   = f"NC_NEMESIS_{safe}"
    tmpl_id    = f"npc_nemesis_{safe}"
    worn_gid   = f"nem_worn_{safe}"
    carry_gid  = f"nem_carry_{safe}"
    weapon_gid = f"nem_weapon_{safe}"
    unique_id  = f"nemesis_{safe}"
    faction_id = f"nemesis_faction_{safe}"

    worn_items    = extract_worn(player_data)
    carried_items = extract_carried(player_data)
    weapon_item   = extract_weapon(player_data)
    bionics       = extract_bionics(player_data)
    spells        = extract_spells(player_data)
    martial_arts  = extract_martial_arts(player_data)
    mutations     = extract_mutations(player_data)
    skills        = extract_skills(player_data)
    bp_ratios     = extract_body_part_hp_ratios(player_data)

    death_cause = detect_death_cause(player_data, log)

    if death_cause.is_teleport_wall:
        lost_limb = random.choice(_TELEPORT_WALL_LIMBS)
        bp_ratios[lost_limb] = 0.0

    # Drowning: bloated corpse that was a strong swimmer.
    # No aquatic mutation traits; the skill reflects the character's affinity with water.
    if death_cause.is_drowning:
        skills["swimming"] = 10

    profession_name = extract_profession(player_data)
    death_message   = extract_death_message(log)
    last_words      = extract_last_words(log)
    job_description = build_narrative_description(profession_name, death_message, last_words, death_cause)

    weapon_items = [weapon_item] if weapon_item is not None else []

    bonus_str = str_max - CDDA_BASE_STAT
    bonus_dex = dex_max - CDDA_BASE_STAT
    bonus_int = int_max - CDDA_BASE_STAT
    bonus_per = per_max - CDDA_BASE_STAT

    skill_weight = sum(skills.values()) // 4
    delay_days: int = max(1, str_max + dex_max + int_max + per_max + skill_weight)
    if delay_days_override is not None:
        delay_days = delay_days_override

    if death_cause.is_mycus:
        faction_mon = "fungus"
    elif death_cause.is_triffid:
        faction_mon = "triffid"
    elif death_cause.is_dermatik:
        faction_mon = "dermatik"
    elif death_cause.is_parasite:
        faction_mon = "spider"
    elif death_cause.is_teleglow:
        faction_mon = "nether_player_hate"
    else:
        faction_mon = "zombie"

    npc_json: list[dict[str, Any]] = [
        build_faction(safe, mon_faction=faction_mon),
        build_item_group(worn_gid,   worn_items,   FALLBACK_WORN),
        build_item_group(carry_gid,  carried_items, FALLBACK_CARRY),
        build_item_group(weapon_gid, weapon_items,  FALLBACK_WEAPON),
        build_npc_class(
            class_id, char_name,
            worn_gid, carry_gid, weapon_gid,
            bonus_str, bonus_dex, bonus_int, bonus_per,
            bionics, spells, mutations, skills, martial_arts,
            death_cause=death_cause,
            job_description=job_description,
        ),
        build_npc_template(tmpl_id, class_id, safe, char_name, faction_id,
                           death_cause=death_cause),
    ]

    char_data = NemesisCharData(
        safe_name     = safe,
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
        death_omt     = extract_omt_position(data),
        death_oter_id = oter_id,
        martial_arts  = martial_arts,
    )

    return npc_json, char_data
