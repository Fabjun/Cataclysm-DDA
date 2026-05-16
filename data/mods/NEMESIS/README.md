# NEMESIS — The Persistent Nemesis System for Cataclysm: DDA

> *You thought death was the end. It wasn't.*

Every character you lose becomes something else — a hunter. The Nemesis System reads your dead character's save file and turns them into a hostile NPC that carries their gear, wields their skills, and bears the mark of how they died. It will find you. Eventually.

---

## Table of Contents

- [What is the Nemesis System?](#what-is-the-nemesis-system)
- [Requirements](#requirements)
- [Installation](#installation)
- [How to Use It](#how-to-use-it)
  - [Starting a New Game](#scenario-a-starting-a-new-game)
  - [After Your Character Dies](#scenario-b-after-your-character-dies)
- [What to Expect In-Game](#what-to-expect-in-game)
  - [When a Nemesis spawns](#when-a-nemesis-spawns)
  - [The hunt](#the-hunt)
  - [When you kill the Nemesis](#when-you-kill-the-nemesis)
  - [The death echo](#the-death-echo)
- [The Four Nemesis Tiers](#the-four-nemesis-tiers)
- [Tuning the Mod](#tuning-the-mod)
  - [Population Size and Tier Composition](#population-size-and-tier-composition)
  - [Spawn Delay Override](#spawn-delay-override)
  - [Spawn Frequency](#spawn-frequency)
  - [Hunt Behavior](#hunt-behavior)
- [Cause of Death Effects](#cause-of-death-effects)
- [Known Issues and Future Plans](#known-issues-and-future-plans)
- [Debug Mode](#debug-mode)
- [For Developers](#for-developers)
  - [File Overview](#file-overview)
  - [Pipeline Architecture](#pipeline-architecture)
  - [Spawn System](#spawn-system--technical)
  - [Hunt System](#hunt-system--technical)
  - [Updating Item Pools](#updating-item-pools-after-a-cdda-update)

---

## What is the Nemesis System?

When your character dies in Cataclysm: DDA, the game saves everything about them — their stats, their skills, what they were wearing, what killed them. This mod reads that file and uses it to create a new NPC: your Nemesis.

The Nemesis is not a generic hostile survivor. It is a copy of *you*. It wears what you were wearing at the time of death, carries what you had in your pockets, and wields your weapon — loaded to the degree your tier of experience suggests. Its body bears the wounds of your final moments. And if you died in a fire, it looks the part. If you drowned, it is bloated and pale. If a fungal infection took you, it carries the hunger of the mycus.

The Nemesis does not appear immediately. Weaker characters return in a matter of days. A powerful, well-equipped character — someone who should have known better — takes months to claw their way back. This gives you time. Not much. But time.

If you survive long enough and kill your Nemesis, it drops a small token: a memento. Pick it up and read what it says. It tells you who that person was, how they died, and why they came back. A small, grim piece of closure — until your next death starts the cycle again.

---

## Requirements

- **Cataclysm: Dark Days Ahead** — the base game (`dda` mod dependency)
- **Python 3.8 or newer** — used to run the generation scripts
  - No third-party packages required; only the Python standard library
  - Check your version: `python3 --version`
- The NEMESIS mod files placed in your CDDA mods directory

---

## Installation

### Step 1 — Place the mod files

Copy the entire `NEMESIS` folder into your CDDA mods directory:

```
<cdda_install>/data/mods/NEMESIS/
```

The folder should contain files like `modinfo.json`, `nemesis_launcher.py`, and so on.

### Step 2 — Enable the mod in CDDA

When creating a **new world** in Cataclysm, open the world options and navigate to the **Mods** tab. Find "Nemesis System" in the list and add it to your active mods. The mod must be enabled at world creation — it cannot be added to an existing world afterward.

### Step 3 — Verify Python

Open a terminal (Command Prompt on Windows, Terminal on macOS/Linux) and run:

```bash
python3 --version
```

You should see `Python 3.8.x` or higher. If the command is not found, download Python from [python.org](https://www.python.org/downloads/) and install it.

### Where are my save files?

The generation scripts need the path to your character's `.sav` file. Here is where CDDA saves your games:

| Platform | Default save location |
|----------|-----------------------|
| **Linux** | `~/.local/share/cataclysm-dda/save/<world_name>/<character>.sav` |
| **macOS** | `~/Library/Application Support/Cataclysm-DDA/save/<world_name>/<character>.sav` |
| **Windows** | `%APPDATA%\Cataclysm-DDA\save\<world_name>\<character>.sav` |

> **Tip:** On portable builds (the game folder downloaded without an installer), saves are stored next to the game executable instead of in the system user directory. Check your CDDA install's `Options > Directories` menu to see the exact path on your system.

---

## How to Use It

Open a terminal and navigate to the mod directory:

```bash
cd path/to/your/cdda/data/mods/NEMESIS
```

There are two situations you will encounter.

---

### Scenario A: Starting a New Game

*You have just created a world and have not died yet — but you want Nemeses to already exist in the world from the start.*

The **seeder** generates a set of synthetic Nemeses without needing a real save file. These are fictional characters drawn from the same pools of names, equipment, and stats that a real Nemesis would use.

**Run the seeder:**
```bash
python3 nemesis_seeder.py
```

This creates 40 Nemeses by default (15 Civilians, 10 Responders, 10 Elites, 5 Apex) and writes one JSON file per character into the mod directory.

**Then compile the master file:**
```bash
python3 nemesis_launcher.py
```

This assembles all the individual character files into a single routing file (`nemesis_master.json`) that the game reads. Always run this step after the seeder.

Start your new world. The Nemeses are out there, waiting.

---

### Scenario B: After Your Character Dies

*Your character has died and you want them to come back as a Nemesis.*

Find your dead character's `.sav` file (see the [save locations table](#where-are-my-save-files) above), then run:

```bash
python3 nemesis_launcher.py /path/to/your/character.sav
```

**Example on Linux/macOS:**
```bash
python3 nemesis_launcher.py "~/.local/share/cataclysm-dda/save/MyWorld/Alice.sav"
```

**Example on Windows:**
```cmd
python3 nemesis_launcher.py "%APPDATA%\Cataclysm-DDA\save\MyWorld\Alice.sav"
```

The launcher will:
1. Read the save file and extract your character's data
2. Generate a character JSON file (`nemesis_alice.json` or similar)
3. Automatically recompile `nemesis_master.json`
4. Print a summary of what it found: traits, items, death causes, delay

Load your save game. Your character is coming back — on their own schedule.

> **You can combine both.** Run the seeder once when you start a new world for the initial population, then run the launcher each time a character dies to add that character to the roster. The scripts do not conflict.

---

## What to Expect In-Game

### When a Nemesis spawns

You will cross an overmap tile boundary and — if the Nemesis is ready — a red message appears:

> *You feel an ominous presence — something from your past is hunting you.*

Shortly after, the NPC spawns somewhere between 40 and 55 overmap tiles away. It is dressed in gear from the dead character's wardrobe, worn and dirty. Its weapon is loaded proportionally to its tier. Its body carries the wounds from its final fight.

The spawn is not guaranteed on every tile crossing — there is a 1-in-100 chance per crossing, with a minimum one-hour cooldown between spawns. This prevents the Nemesis from appearing the moment you leave camp.

### The hunt

The Nemesis is initially passive — it wanders and follows its own agenda. But if you engage it in combat, something changes.

Once you land a hit, the Nemesis enters **hunting mode**. From that point on, it actively tracks you using three inputs:

- **Memory** — it remembers the last place you were seen
- **Noise** — gunshots and smashing terrain make you easier to find
- **Sight** — a perceptive Nemesis can spot you at range

Every hour, the Nemesis re-evaluates. If it is close enough or your noise level is high, it moves toward your last known position. If it loses the trail, it wanders in an expanding search pattern. If it accumulates too many cold ticks — the exact number scales with its Intelligence and Perception — it gives up the active hunt and goes quiet. But it is still out there.

**Smarter, stronger Nemeses persist longer.** An Apex will keep hunting for months of cold trail. A Civilian gives up after a few missed contacts.

**Safe zones exist.** Evac shelters, the Refugee Center, and certain NPC-guarded settlements cause the Nemesis to break off its cold-trail pursuit. It will not follow you inside permanently.

When you are close to a hunting Nemesis and it has lost the trail:
> *A presence lingers just beyond your sight — something is out there, closer than you'd like.*

### When you kill the Nemesis

The NPC dies. It drops a **memento** — a small, lightweight leather token. Pick it up and read its description. It carries the character's name, an atmospheric line reflecting how the original player died, and a closing sentence.

Depending on the cause of death, killing the Nemesis may also trigger a final event:
- A Nemesis that died to **dermatik parasites** releases larvae on death
- A **mycus-infected** Nemesis releases spore clouds
- A **parasite-riddled** Nemesis releases the creatures that were living inside it

After death the Nemesis is gone permanently. It will not respawn.

### The death echo

If the Nemesis was generated from a real dead character (not a seeded placeholder), the system records where that character died. If you visit that exact overmap tile, an atmospheric message fires — once, then never again:

> *You stand where [name] died. The ground feels wrong underfoot. Something ended here.*

The message is cause-specific. A character who drowned leaves a cold, still smell of stagnant water. A fire death leaves scorched earth. A character who collapsed from exhaustion leaves the air feeling inexplicably tired. The message reflects the cause of the original death as detected from the save file and memorial log.

Seeded placeholder Nemeses have no death location — only characters built from real save files trigger this.

---

## The Four Nemesis Tiers

The tier is determined by how powerful the dead character was — or, for pre-generated seed Nemeses, is assigned randomly. Higher tiers have better stats, better gear, and take longer to arrive.

| Tier | Name | Spawn delay | Stats (per attribute) | Magazine load |
|------|------|-------------|----------------------|---------------|
| **T1** | Civilian | 1–13 days | 7–11 | 25–50% full |
| **T2** | Responder | 14–59 days | 9–14 | 50–100% |
| **T3** | Elite | 60–99 days | 12–18 | 75–100% |
| **T4** | Apex | 100–365 days | 15–20 | Always 100% |

**What each tier represents:**

**Civilian** — Someone who barely survived long enough to die. Poorly equipped, minimally skilled, carrying civilian clothes and maybe a handgun with a half-empty magazine. They come back quickly because they never posed much of a threat to begin with. Don't underestimate them entirely — even a weak Nemesis can catch you off guard.

**Responder** — A first responder, soldier, or early-game survivor who had learned the basics. Police gear or military surplus, pistols and shotguns, a bionic or two. They arrive within the first two months. By the time they show up, you had better have a plan.

**Elite** — A hardened character who had spent real time in the apocalypse. Military equipment, rifles, multiple bionics, possibly mutations. They take two to three months to appear and they are persistent hunters. A high-Perception Elite is difficult to shake once it has your scent.

**Apex** — The last stage of human survival pushed past its limit. Survivor gear, exotic weapons, up to eight bionics, multiple mutations, almost certainly trained in martial arts. They carry full magazines and have nearly no physical damage. These are the characters who *should* have lived. They take the longest to return — but when they do, there is nothing casual about the encounter.

The weapon magazine load also scales with tier: a Civilian carries a partially-loaded firearm (they probably died before they could reload), while an Apex is always combat-ready at full capacity.

---

## Tuning the Mod

There are three things you can tune: how many Nemeses exist, how fast they can appear, and how stubbornly they hunt once active. Not all of these require editing code — the most important settings are exposed as command-line options.

---

### Population Size and Tier Composition

The seeder's `--counts` flag controls how many Nemeses are pre-generated, and in what ratio per tier.

**Recommended (default) — 800 total:**
```bash
python3 nemesis_seeder.py --counts 300 200 200 100
```

The four numbers are T1 T2 T3 T4 in order (Civilian, Responder, Elite, Apex). The recommended split front-loads weaker Nemeses so early players encounter mostly manageable threats, with Apex entries appearing rarely but consistently.

**Adjusting difficulty by composition:**

| Want fewer of... | Example command |
|------------------|-----------------|
| Apex only (easiest) | `python3 nemesis_seeder.py --counts 300 200 200 50` |
| All high tiers (hardest) | `python3 nemesis_seeder.py --counts 50 100 200 150` |
| Balanced equal distribution | `python3 nemesis_seeder.py --counts 200 200 200 200` |
| Minimal population (quick test) | `python3 nemesis_seeder.py --counts 5 3 2 1` |

> **Rule:** Every tier must have at least 1 entry. You cannot set a count to 0.

**Reproducing the same population** (useful for sharing a specific game configuration):
```bash
python3 nemesis_seeder.py --counts 300 200 200 100 --seed 42
```

After any seeder run, always recompile the master:
```bash
python3 nemesis_launcher.py
```

---

### Spawn Delay Override

If you want a specific character to arrive sooner or later than the system calculates, pass `--delay-days` to the launcher:

```bash
python3 nemesis_launcher.py /path/to/character.sav --delay-days 7
```

This overrides the automatic tier-based delay. Useful for testing a specific Nemesis without waiting, or for artificially extending a grace period after a particularly dangerous death.

---

### Spawn Frequency

The spawn system checks for an eligible Nemesis every time you cross an overmap tile boundary. Two constants in `nemesis_eoc.py` control how often this actually produces a spawn:

```python
_SPAWN_ONE_IN_N: int = 1           # 1 = always attempt (test phase)
                                   # set to 100 for production (1% chance per crossing)

_SPAWN_COOLDOWN_MINUTES: int = 0   # 0 = no cooldown (test phase)
                                   # set to 60 for production (one spawn per hour max)
```

Both constants work together. In production (`_SPAWN_ONE_IN_N = 100`, `_SPAWN_COOLDOWN_MINUTES = 60`): each OMT crossing has a 1% chance of attempting a spawn, and the cooldown prevents another attempt for 60 minutes after a successful spawn. This gives infrequent, unpredictable appearances — the player cannot time or anticipate them.

In test phase (both at minimum): every crossing attempts a spawn immediately, making the pipeline easy to verify without waiting in-game days.

After changing either constant, recompile:

```bash
python3 nemesis_launcher.py
```

---

### Hunt Behavior

The constants that control how the active hunt works are all gathered at the top of `nemesis_eoc.py`, clearly labelled. Edit any of them and recompile to apply the change.

| Constant | Default | What it controls |
|----------|---------|-----------------|
| `_TRACKER_INTERVAL_MIN / MAX` | `60` | Minutes between hunt ticks (lower = more reactive but more CPU load) |
| `_HOT_THRESHOLD` | `15` | Score required to consider the trail "hot" — lower means easier to detect the player |
| `_OVR_DIST_START` | `30` | Search radius (in overmap tiles) when hunting begins |
| `_OVR_DIST_MIN` | `2` | Minimum search radius — Nemesis stays this close once it has you cornered |
| `_OVR_DIST_SHRINK` | `3` | How fast the search radius narrows per hot-trail tick |
| `_FRUSTRATION_INCR` | `1` | Cold-trail ticks added per missed interval |
| `_FRUSTRATION_DECR` | `2` | Ticks recovered per hot-trail tick (higher = more forgiving if you briefly evade) |

> **Persistence reminder:** the give-up threshold is `frustration × 2 >= Intelligence + Perception`. Apex Nemeses have INT/PER up to 20, so with default constants they can tolerate up to 20 cold ticks before giving up — about 20 in-game hours.

After any edit to `nemesis_eoc.py`, recompile:
```bash
python3 nemesis_launcher.py
```

---

## Cause of Death Effects

The mod detects over 30 different causes of death from the character's save file and memorial log. Each cause gives the Nemesis unique traits, permanent effects, or item carry bonuses that reflect how the original character died. Detection is done from two independent sources: the character's physical state at the moment of death (active effects, radiation level, body temperature, body-part HP) and entries in the memorial log.

### Environmental

| Cause | Detection | Traits & effects |
|-------|-----------|-----------------|
| **Fire** | Effects: `onfire`, `heatstroke`, `blisters` | DEFORMED, UGLY, charred skin (M_SKIN2), no facial hair |
| **Lava** | Log: "lava" (near death) | Same as fire — same physical damage, same appearance |
| **Freezing** | Body temp < 3200 | RABBIT_FEET (cold-adapted nervous system) |
| **Drowning** | Save flag: `underwater` | FAT (bloated), Swimming skill 10 |
| **Acid** | Effect: `corroding` | DEFORMED, UGLY, ACIDBLOOD — flesh partially dissolved |
| **Nuclear / radiation** | Radiation ≥ 100; Log: "mininuke" (keyword) | CHAOTIC, permanently glowing; may carry iodine tablets |
| **Electric conduit** | Log: "high-energy conduit" | JITTERY, permanently zapped |
| **Explosion** | Log: "exploded" or "explosion" (keyword match) | DEAF, no facial hair — eardrums and face took the blast |
| **Trap** (beartrap, landmine, pit, etc.) | Log: trap-specific strings (near death) | CLUMSY, spawns with active beartrap and bleed effects |

### Combat & Medical

| Cause | Detection | Traits & effects |
|-------|-----------|-----------------|
| **Headshot** | Head HP = 0 at death | GLASSJAW — the fatal weakness persists |
| **Blood loss** | Log: "Bled to death.", "hypovolemic shock", "loss of red blood cells" | CANNIBAL — insatiable hunger for flesh |
| **Infection** | Log: "Succumbed to the infection." | SAPIOVORE, CANNIBAL — the infection changed their appetites |
| **Poison / venom** | Effects: `badpoison`, `paralyzepoison`, venom (spider/snake/insect), `foodpoison` | TOLERANCE, NAUSEA, ACIDBLOOD |
| **Overdose** | Log: "Died of datura overdose.", "Died of a healing stimulant overdose.", "Died of adrenaline overdose.", "Died of an alcohol overdose.", "Died of a drug overdose." | ADDICTIVE, permanently stimulant- and adrenaline-affected |
| **Asthma attack** | Log: "Succumbed to an asthma attack." | ASTHMA, permanently winded |
| **Autodoc failure** | Log: "Failed install of bionic" / "Failed to remove bionic" | CENOBITE — pain and surgery left their mark |

### Starvation & Exhaustion

| Cause | Detection | Traits & effects |
|-------|-----------|-----------------|
| **Starvation** | Log: "Died of starvation." | HUNGER3, LIGHTWEIGHT — permanently ravenous |
| **Thirst** | Log: "Died of thirst." | HUNGER3, LIGHTWEIGHT |
| **Exhaustion** (collapsed from sleep deprivation) | Log: "Succumbed to lack of sleep." | SEESLEEP, INSOMNIA — can never truly rest again |

### Dimensional & Supernatural

| Cause | Detection | Traits & effects |
|-------|-----------|-----------------|
| **Mycus** infection | Log: "marloss" / "Marloss" (near death) | 1–3 random fungal mutations (THRESH_MYCUS, M_BLOOM, M_FERTILE, M_PROVENANCE); releases spore clouds on death |
| **Triffid** grove | Log: "Destroyed a triffid grove." | 1–3 random plant mutations (CHLOROMORPH, THORNS, ROOTS3) |
| **Dermatik** infestation | Log: "Injected with dermatik eggs." / "Dermatik eggs hatched." | FAT, PONDEROUS1; releases dermatik larvae on death |
| **Parasite** (brainworms / bloodworms / blood spiders) | Effects: `brainworms`, `bloodworms`, `blood_spiders` | FAT, PONDEROUS1 + type-specific bonus; releases the matching creature on death |
| **Teleglow** / dimensional instability | Log: "Spontaneous teleport." | HALLUCINATION — reality is not what it seems |
| **Dimension travel** / resonance cascade | Log: "Traveled from" / "resonance cascade" | Can phase through obstacles |
| **Dark wyrm** | Log: "Awoke a group of dark wyrms!" / "Drew the attention of more dark wyrms!" | NIGHTVISION — touched by deep dark |
| **Subspace specimen** release | Log: "Released subspace specimens." / "Terminated subspace specimens." | 1–3 random traits (M_PROVENANCE, CHAOTIC_BAD, TERRIFYING) |
| **Amigara horror** | Log: "Angered a group of amigara horrors!" | TERRIFYING, DOWN — wrong posture, wrong presence |
| **Artifact** | Log: "Opened a strange temple." / "Activated the " (prefix match) | HALLUCINATION |
| **Teleported into a wall** | Log: "Teleported into a" | CHAOTIC_BAD, TERRIFYING, plus one random limb missing |

### Other

| Cause | Detection | Traits & effects |
|-------|-----------|-----------------|
| **Suicide** | Log: "committed suicide." | QUIETMOVES — still and deliberate |
| **Sewage ingestion** | Log: "Ate a sewage sample." | VOMITOUS |
| **Mutagen** | Log: "mutagen." (near death) | CHAOTIC, ANIMALDISCORD, ANIMALEMPATH |

### Undetectable causes

Some deaths leave no recoverable signature in the save file or memorial log. These include: direct combat deaths (killed by zombie or animal with no distinguishing effects), vehicle collision (non-explosion), fall damage, and lightning strike. Nemeses from these deaths appear with no cause-specific traits — they are generic, and no less dangerous for it.

---

---

## Debug Mode

The debug patch is a development tool built into the mod. It modifies the generated JSON files to make testing fast — stripping delays, removing location requirements, and adding in-game feedback messages so you can verify the spawn and hunt pipeline without waiting in-game days.

**Apply the patch:**
```bash
python3 nemesis_debug_patch.py apply
```

This does three things:
1. Reduces every Nemesis spawn delay to 0 days (immediate eligibility)
2. Removes the indoor/outdoor location requirement from all spawn checks
3. Adds `[NEMESIS DEBUG]` messages in-game: yellow when the master EOC fires, green/red per individual spawn check (pass/fail)

Backups of every modified file are saved as `.json.bak` before changes are made.

**Restore the originals:**
```bash
python3 nemesis_debug_patch.py restore
```

Reads the `.bak` files and restores all originals. Always restore before sharing the mod or switching to production settings.

**Verify the restore was clean:**
```bash
python3 nemesis_debug_patch.py restore --verify
```

After restoring, checks every restored `.json` file for leftover `[NEMESIS DEBUG]` strings and prints a pass/fail report. Use this after any partial-apply failure or when in doubt.

> **Never ship the mod with the debug patch applied.** The `.bak` files are not part of the mod — they are local working files only.

### Expanding the Debug Mode

The debug patch is a living tool. New `--mode` flags will be added alongside each new feature to make that feature testable in isolation. The flags are stackable — you can combine several in one apply command:

```bash
python3 nemesis_debug_patch.py apply --mode hunt --mode state
```

| Flag | What it enables | Tests |
|------|----------------|-------|
| *(default)* | Strips spawn gates, adds spawn debug messages | Spawn pipeline |
| `--mode hunt` | Injects NEMESIS_HUNTING on all Nemeses immediately — no hit required | Hunt system, dread messages |
| `--mode ambush` | Injects NEMESIS_AMBUSH + 5-OMT radius — skips the hunting phase | Ambush mode, proximity re-trigger |
| `--mode state` | Adds a RECURRING EOC printing each Nemesis's state every minute | State machine correctness |
| `--mode dread` | Lowers dread detection threshold to 1 OMT — guarantees messages fire | Dread system messages |
| `--mode death-echo` | Sets all Nemesis death OMT positions to the player's current location | Death echo feature |
| `--nemesis <name>` | Applies the patch only to the named Nemesis | Isolated single-character testing |

Each flag is only available once the feature it tests has been implemented.

---

## For Developers

The rest of this document covers the technical internals of the mod. If you are a player who just wants to use it, you are done — everything above is all you need.

---

### File Overview

| File | Purpose | Edit manually? |
|------|---------|----------------|
| `modinfo.json` | Mod metadata (name, authors, dependencies) | Yes |
| `nemesis_hunting.json` | `NEMESIS_MARK` mutation + hunt effect types | Yes |
| `nemesis_items.json` | Base memento item template (fallback) | Yes |
| `nemesis_master.json` | Master routing EOC tree | **No** — auto-generated |
| `nemesis_seed_*.json` | Pre-generated Nemesis characters | **No** — auto-generated |
| `nemesis_builder.py` | Save-file parsing + NPC JSON building | Yes |
| `nemesis_eoc.py` | All EOC builders + master compilation | Yes |
| `nemesis_launcher.py` | CLI entry point (death workflow) | Yes |
| `nemesis_seeder.py` | Synthetic cold-start seed generation | Yes |
| `nemesis_pools.py` | Item category pools used by the seeder | Partially — auto-managed categories are overwritten by the pool generator |
| `nemesis_pool_generator.py` | Maintenance tool — rebuilds pool categories from CDDA item JSON | Yes |
| `nemesis_pools_blocklist.txt` | Items excluded from all auto-generated categories | Yes |
| `nemesis_debug_patch.py` | Debug tool — strips spawn gates, adds in-game debug messages | Yes |

**Do not edit `nemesis_master.json` or any `nemesis_seed_*.json` file by hand.** They are overwritten on every launcher/seeder run.

---

### Pipeline Architecture

```
Player death (.sav file)
        │
        ▼
nemesis_launcher.py           ← CLI entry point
        │
        ├─► nemesis_builder.py
        │     ├─ load_save_file()        read .sav (strips # comment lines)
        │     ├─ _load_memorial_log()    read companion .log file
        │     ├─ extract_*()             stats, gear, skills, bionics, mutations,
        │     │                          martial arts, spells, body-part HP ratios,
        │     │                          death location, death day
        │     ├─ detect_death_cause()    34 classifiers from player state + log
        │     └─ build_*()              faction, item groups, npc_class, npc_template
        │
        ├─► nemesis_eoc.py
        │     └─ build_per_char_eocs()   5–6 EOC dicts per Nemesis:
        │                                  spawn_check, spawn_success, death_notify,
        │                                  wounds EOC, effects EOC
        │                                  [+ death_echo EOC when death OMT is known]
        │
        ├─► writes nemesis_<name>.json   NPC JSON + EOC JSON, one file per character
        │
        └─► nemesis_eoc.compile_master()
              ├─ scan all nemesis_*.json
              ├─ extract routing metadata (death_day, days_per_year, location)
              ├─ build_hub_eocs()        group by anniversary; one hub per unique day
              ├─ build_master_eoc()      single EVENT listener routes to day hubs
              ├─ build hunt system EOCs  TRACKER, ANNOUNCER, ACOUSTIC, REVENGE, FLAGS
              └─ writes nemesis_master.json
```

For cold-start seeding, `nemesis_seeder.py` replaces the "player death" step with synthetic character generation using `nemesis_pools.py` item categories, then calls the same EOC pipeline.

---

### Spawn System — Technical

Each Nemesis has a single `EOC_NEMESIS_SPAWN_CHECK_<safe_name>` ACTIVATION EOC. It is never called directly; it is routed through day hubs in `nemesis_master.json`.

**Global spawn variable** — `global_nemesis_spawned_<safe_name>`:
- `0` — unarmed; the first time the EOC's false_effect fires, it arms the Nemesis and records `global_nemesis_char_start_<safe_name> = time('now')`
- `1` — armed and waiting; all gates are evaluated each time the hub routes to this check
- `2` — spent; the Nemesis has spawned (or died); this check is permanently skipped

**Gate conditions (all must pass to spawn):**
1. `global_nemesis_spawn_lock == 0` — cross-Nemesis cooldown (set to 1 for 1 hour after any spawn)
2. `spawned_var == 1` — must be armed
3. `time_since(char_start_var) >= time('<delay_days> days')` — delay elapsed
4. Location condition — `u_is_outside` or `{"not": "u_is_outside"}` (from death location)
5. Anniversary gate (live Nemesis only) — `time('now') % time('<N> days') >= time('<death_day> days')`

**Spawn radius:** 40–55 OMTs from the player. Wounds and effects are applied via `u_run_npc_eocs` immediately after spawn.

**Day hubs** group all Nemeses that share the same `(death_day, days_per_year)` pair. The master EOC evaluates one `if/then` condition per hub instead of per character, keeping EOC evaluation O(unique anniversaries) rather than O(total Nemesis count).

---

### Hunt System — Technical

The hunt system is driven by four effect types defined in `nemesis_hunting.json` and the tracker EOC built by `nemesis_eoc.py`. Understanding the state machine is essential for debugging hunt behavior.

#### State Machine

```
PASSIVE (spawned, no hunt effects)
    → player hits Nemesis → ADD NEMESIS_HUNTING, SET OVR_DIST=30

ACTIVE HUNT (has NEMESIS_HUNTING)
    Tracker fires every 60 min — see score formula below.

    Hot trail (score >= 15 OR within 2 OMTs with score > 0):
        update last-known position, u_set_goal toward player,
        shrink OVR_DIST by 3 (floor 2), reduce FRUSTRATION by 2

    Cold trail — two phases driven by FRUSTRATION and NPC stats:

        Phase 1 — Disciplined Tracker (FRUSTRATION < (INT+PER)/4)
            Wander within OVR_DIST of last-known player position.
            The Nemesis searches the area where you disappeared.

        Phase 2 — Animal Drift (FRUSTRATION >= (INT+PER)/4)
            Wander within OVR_DIST of the NPC's own current position.
            The Nemesis moves erratically, no longer anchored to memory.

    Give-up (FRUSTRATION * 2 >= INT + PER):
        REMOVE NEMESIS_HUNTING, ADD NEMESIS_AMBUSH, SET OVR_DIST=5

AMBUSH (has NEMESIS_AMBUSH, reduced 5-OMT detection radius)
    → player enters 5-OMT radius → re-arm hunt (REMOVE AMBUSH, ADD HUNTING,
                                                  RESET OVR_DIST=30, RESET FRUSTRATION)
    → player hits Nemesis       → re-arm hunt (always available via REVENGE_TRIGGER)
```

**Why two cold-trail phases?** Weak Nemeses (low INT+PER) lose discipline after only a few hours and begin roaming erratically — they are easier to shake because their search becomes unpredictable. Strong Nemeses maintain methodical searching for much longer before their patience breaks. The threshold is `FRUSTRATION >= (INT+PER)/4`, which is exactly halfway to give-up, and scales naturally with the same stats that drive every other hunt decision.

| Typical INT+PER | Phase 1 (tracker) | Phase 2 (animal) | Give-up after |
|----------------|------------------|-----------------|---------------|
| 14–16 (weak)   | ~4 h             | ~4 h            | ~8 h          |
| 20–24          | ~5–6 h           | ~5–6 h          | ~11 h         |
| 26–30          | ~7 h             | ~7 h            | ~14 h         |
| 32–40 (strong) | ~8–9 h           | ~8–9 h          | ~17–18 h      |

#### Effect Types

**NEMESIS_MARK** — mutation. Applied to all Nemesis NPCs. Zero cost, not purifiable, invisible to players. Selector for `EOC_NEMESIS_REVENGE_TRIGGER` and `EOC_NEMESIS_TRACKER`.

**NEMESIS_HUNTING** — effect type. Permanent duration. Presence = active pursuit. Tracker runs full hunt logic while this is active.

**NEMESIS_OVR_DIST** — effect type. Intensity = current overmap search radius (OMT units). Set to 30 on arm, decremented by 3 per hot tick, floored at 2. Set to 5 on give-up (ambush radius). Reset to 30 on ambush re-trigger.

**NEMESIS_FRUSTRATION** — effect type. Intensity = cold-trail tick counter. +1 per cold tick, -2 per hot tick. Give-up: `intensity * 2 >= INT + PER`. Reset to 0 on ambush re-trigger.

**NEMESIS_AMBUSH** — effect type. Permanent, intensity 1. Marks post-give-up ambush state. Present between give-up and proximity re-trigger.

#### Score Formula

```
score = max(0, NPC_perception - omt_dist * 3) * 10    ← vision contribution
      + noise_level * 2                                ← acoustic (if not DEAF)
```

Hot threshold: `score >= 15`. Noise sources: ranged attack (20), terrain destruction (30). Noise decays by 5 per tracker tick.

**Safe zones** (cold-trail give-up locations): `shelter`, `evac_center_13` (r=2), `outpost`, `ranch_camp_1` (r=12).

---

### Updating Item Pools After a CDDA Update

When CDDA adds new items, the seeder's item categories in `nemesis_pools.py` can go stale. The pool generator rebuilds the auto-managed categories by scanning CDDA's item JSON directly:

```bash
python3 nemesis_pool_generator.py
```

Review the changes:
```bash
git diff nemesis_pools.py
```

If new items appear that do not belong (wrong-genre armor, debug items, raw ingredients, etc.), add their IDs to `nemesis_pools_blocklist.txt` one line per item, then re-run the generator:

```bash
# add bad IDs to nemesis_pools_blocklist.txt, then:
python3 nemesis_pool_generator.py
```

Regenerate seeds and recompile master:
```bash
python3 nemesis_seeder.py
python3 nemesis_launcher.py
```

**Manually maintained categories** (never overwritten by the generator):
`WORN_POLICE`, `WORN_SURVIVOR`, `CARRY_CIVILIAN`, `CARRY_MILITARY`, `CARRY_SCIENCE`,
`WEAPON_MELEE_LIGHT`, `WEAPON_MELEE_HEAVY`, `WEAPON_MELEE_MARTIAL`, `WEAPON_SPECIAL`

---

---

## Known Issues and Future Plans

### Planned Features

**Async Multiplayer Mode** — A planned second mode allowing players to upload their dead characters to a shared pool and download other real players' dead characters to populate their worlds. Interface design is in progress; the backend is not yet decided.

**NPC Bounties** — Survivor NPCs at refugee centers could offer supplies in exchange for mementos recovered from Nemesis corpses. Requires understanding CDDA's mission and trader dialogue systems.

**Coordinated Hunts** — When two hunting Nemeses are within range of each other, one flanks while the other pursues directly. Feasibility depends on whether RECURRING EOCs can query nearby NPC effects — not yet confirmed.

---

### Filthy gear — one-time morale inconsistency popup

**What happens:** Nemesis NPCs spawn wearing filthy gear (the `FILTHY` item flag marks worn items as bloody and contaminated, matching the lore of an undead revenant). On the first morale tick after a Nemesis spawns — at most one minute — CDDA fires a debug popup:

```
DEBUG : Morale "Filthy gear" is inconsistent.
```

**Why it happens:** CDDA's morale system works in two layers. Every minute it builds a fresh `test_morale` from the NPC's worn items (which correctly includes the filthy-gear penalty), then compares it against the NPC's *stored* morale via `consistent_with()`. The stored morale is empty on a freshly spawned NPC — the filthy-gear penalty is never injected into it at spawn time. `consistent_with()` detects the mismatch and fires the popup. Immediately after, `sync_permanent()` copies the penalty into the stored morale, and every subsequent tick passes cleanly. The popup fires **exactly once per Nemesis**, then disappears permanently.

**Why it cannot be fixed from the mod:** The sync happens inside `Character::check_and_recover_morale()` in `src/character_morale.cpp`. There is no EOC action or JSON hook that can call `sync_permanent()` on an NPC before the first scheduled morale tick. The `FILTHY` flag itself cannot be replaced with a JSON-only trait that negates the penalty — the `update_squeamish_penalty()` function that computes it contains no trait checks and is not accessible from the mod system.

**The morale penalty has no gameplay effect on the Nemesis.** It does not cause fleeing — NPC flee decisions are driven by attitude and effect flags, not morale level. It does not affect combat capability. The only consequence is the one-time popup.

**Planned fix — two lines in `src/character_morale.cpp`:**

The root cause is that `sync_permanent()` runs *after* `consistent_with()` in the function. Swapping the order eliminates the first-tick gap:

```cpp
// In Character::check_and_recover_morale(), after apply_persistent_morale():

// Current order (causes popup on first tick):
if( !morale->consistent_with( test_morale ) ) {
    ...
    morale->sync_permanent( test_morale );
}

// Fixed order (pre-sync before comparison — no popup):
morale->sync_permanent( test_morale );
if( !morale->consistent_with( test_morale ) ) {
    ...
}
```

This is a base game change, not a mod change. It is safe for all characters — `sync_permanent()` is already called unconditionally today whenever the check fails, so making it unconditional changes only the timing, not the outcome. The `consistent_with()` check remains in place to catch genuine morale corruption for the player character.

---

*NEMESIS System — mod by Du & Ich*
