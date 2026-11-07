"""Bit Heroes bot: recognise the game's screens and press the right buttons.

The game's own autopilot does the exploring and fighting; the bot only
handles the menus between runs. Every tick it takes one screenshot of the
game, finds which known screen is showing (by small reference images cut
from the game at a page size of 854 × 545), and decides what to click.
Modes are played in the user's order; when one runs out ("Not enough
tickets…"), the bot answers No and moves on to the next. When every mode
is out, it rests and starts again.

This module only decides; ``Engine.start_bot`` takes the screenshots and clicks.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import imaging

PAGE_SIZE = (854, 545)        # the page size the reference images were taken at
DIR = Path(__file__).with_name("bitheroes")
MARGIN = 5                    # how far (px) a reference image may have moved
NEED = 0.85                   # share of pixels that must match
FROZEN_S = 90                 # an unknown screen that does not change for this long: the game hung
LOAD_S = 120                  # after reloading, give the game this long to load before judging it again
# Own thresholds. Stricter for look-alikes: the "Not enough X" words differ in a
# few letters, and the green Rerun and Regroup buttons sit in the same place
# (they match each other 87 %). Looser for the town's icon bar, whose background
# shifts when the town view scrolls (77 %); a window on top dims it to about 28 %.
NEED_FOR = {"sidebar": 0.6, "not_enough_energy": 0.97, "not_enough_tickets": 0.97, "not_enough_shards": 0.97,
          "not_enough_xeals": 0.97, "not_enough_tokens": 0.97, "not_enough_badges": 0.97, "rerun": 0.95, "regroup_left": 0.95}

MODES = {"quest": "Quest", "raid": "Raid", "pvp": "PvP", "gauntlet": "Gauntlet", "wb": "World Boss",
         "invasion": "Invasion"}
DEFAULT_ORDER = ["raid", "wb", "invasion", "gauntlet", "pvp", "quest"]
RAID_DIFFICULTY = {"normal": 224, "hard": 426, "heroic": 628}   # x of the button (y 244)

# Where things are on the page (CSS px at 854 × 545).
ICON = {"quest": (44, 45), "pvp": (44, 120), "wb": (44, 195), "raid": (44, 272), "gauntlet": (807, 345),
        "invasion": (807, 270)}
AUTO_BUTTON = (835, 270)      # autopilot on/off, in battles
PARK = (427, 2)               # where the mouse rests between clicks: no button there
LOOK_S = 0.25                 # how often the bot looks while menus change
RECLICK_S = 4.0               # no change this long after a click: it was missed, click again
BATTLE_X = (816, 33)          # leave the battle (only works with autopilot off)
YES, NO = (355, 375), (498, 375)
TEAM_CLOSE = (749, 81)        # the X of the team window
OUT_OF = {"not_enough_energy": "quest", "not_enough_tickets": "pvp", "not_enough_shards": "raid",
          "not_enough_xeals": "wb", "not_enough_tokens": "gauntlet", "not_enough_badges": "invasion"}

# Screens, most specific first: (name, reference images that must all match;
# a name starting with "!" must NOT match).
SCREENS: list[tuple[str, tuple[str, ...]]] = [
    ("disconnected", ("disconnected", "reconnect")),
    ("still_there", ("still_there_text", "still_there_yes")),
    *[(name, (name,)) for name in OUT_OF],
    ("team_not_full", ("team_not_full",)),
    ("quit_battle", ("quit_battle",)),
    ("reward_items", ("items_title", "items_x")),
    ("season_rewards", ("season_rewards",)),
    ("cleared", ("cleared", "rerun")),
    ("wb_result", ("regroup",)),
    ("wb_defeat", ("regroup_left", "town_right")),
    ("victory_continue", ("continue",)),
    ("result_middle", ("town_middle",)),
    ("result_right", ("town_right", "!rerun")),   # not "Cleared" while its banner slides in
    ("team", ("team_title", "accept")),
    ("select_opponent", ("select_opponent",)),
    ("pvp_window", ("pvp_arena",)),
    ("raid_difficulty", ("raid_normal", "raid_heroic")),
    ("raid_window", ("raid_title", "raid_summon")),
    ("gauntlet_window", ("gauntlet",)),
    ("wb_lobby", ("wb_start", "wb_private")),
    ("wb_summon", ("wb_summon_title", "wb_tier_label")),
    ("wb_carousel", ("wb_title_carousel", "wb_summon_big")),
    ("wb_list", ("wb_title_list", "wb_filter")),
    ("invasion_window", ("invasion",)),
    ("dungeon_window", ("dungeon",)),
    ("quest_map", ("zones",)),
    # Autopilot off: its arrows icon turns pink (between floors the button may be red,
    # but the icon stays white). In a battle the X lights up; in a dungeon, the door.
    ("autopilot_off", ("auto_off_icon", "battle_x")),
    ("dungeon_autopilot_off", ("auto_off_icon", "dungeon_door")),
    ("autopilot_on", ("auto_on",)),      # exploring or fighting: nothing to do (except in Invasion)
    ("main", ("sidebar",)),
]
# Known screens on which the game is busy by itself: a frozen picture here is a hung game.
WAITING = {"autopilot_on"}
# Windows that belong to one mode: (mode, where its "go" button is, where its close X is).
WINDOWS = {
    "pvp_window": ("pvp", (584, 287), (688, 120)),
    "raid_window": ("raid", (575, 392), (677, 120)),
    "gauntlet_window": ("gauntlet", (584, 292), (688, 123)),
    "invasion_window": ("invasion", (584, 292), (688, 123)),
    "wb_list": ("wb", (534, 482), (710, 110)),
    "wb_carousel": ("wb", (575, 393), (680, 123)),
    "wb_summon": ("wb", (510, 420), (585, 140)),
    "wb_lobby": ("wb", (405, 491), (772, 82)),
    "dungeon_window": ("quest", (510, 470), (627, 125)),
}
# Buttons pulse and light up under the mouse, so they may be lighter or
# darker than their reference image. Everything else must match its colours
# (also: open windows dim the town, which must not pass for the main screen).
BUTTONS = {"reconnect", "still_there_yes", "rerun", "regroup_left", "auto_off_icon", "town_right", "town_middle", "regroup", "continue", "accept",
           "raid_summon", "raid_normal", "raid_heroic", "wb_filter", "wb_summon_big", "wb_start", "zones"}


@dataclass
class BotSettings:
    order: list = field(default_factory=lambda: list(DEFAULT_ORDER))   # mode keys, in order
    enabled: list = field(default_factory=lambda: list(DEFAULT_ORDER))
    raid_difficulty: str = "heroic"
    pvp_opponent: int = 1          # row in the opponent list; 1 = the weakest
    quest_point: tuple = (430, 300)  # the dungeon to click on the zone map
    rest_min: int = 30             # when every mode is out: wait this long, then start again
    invasion_s: int = 30           # Invasion: fight this long (enough for the loot), then leave the battle

    @classmethod
    def from_dict(cls, d: dict) -> "BotSettings":
        s = cls()
        order = [m for m in d.get("order", []) if m in MODES]
        s.order = order + [m for m in DEFAULT_ORDER if m not in order]
        s.enabled = [m for m in d.get("enabled", s.enabled) if m in MODES]
        if d.get("raid_difficulty") in RAID_DIFFICULTY:
            s.raid_difficulty = d["raid_difficulty"]
        s.pvp_opponent = min(4, max(1, int(d.get("pvp_opponent", 1) or 1)))
        qp = d.get("quest_point")
        if isinstance(qp, (list, tuple)) and len(qp) == 2:
            s.quest_point = (int(qp[0]), int(qp[1]))
        s.rest_min = max(1, int(d.get("rest_min", 30) or 30))
        s.invasion_s = max(5, int(d.get("invasion_s", 30) or 30))
        return s

    def to_dict(self) -> dict:
        return {"order": self.order, "enabled": self.enabled, "raid_difficulty": self.raid_difficulty,
                "pvp_opponent": self.pvp_opponent, "quest_point": list(self.quest_point),
                "rest_min": self.rest_min, "invasion_s": self.invasion_s}

    @property
    def modes(self) -> list:
        return [m for m in self.order if m in self.enabled]


_REFS: Optional[dict] = None


def references() -> dict:
    """name -> (Image, (x, y, w, h)) for every reference image."""
    global _REFS
    if _REFS is None:
        boxes = json.loads((DIR / "boxes.json").read_text())
        _REFS = {name: (imaging.decode_png((DIR / f"{name}.png").read_bytes()), tuple(box))
                 for name, box in boxes.items()}
    return _REFS


def matches(img: imaging.Image, name: str) -> float:
    """How well the screenshot shows reference ``name`` at its place (0..1)."""
    tpl, (x, y, w, h) = references()[name]
    x0, y0 = max(0, x - MARGIN), max(0, y - MARGIN)
    area = img.crop(x0, y0, w + (x - x0) + MARGIN, h + (y - y0) + MARGIN)
    return imaging.find(tpl, area, x - x0, y - y0, MARGIN, NEED, name in BUTTONS)[0]


def _shows(img: imaging.Image, ref: str) -> bool:
    if ref.startswith("!"):
        return not _shows(img, ref[1:])
    return matches(img, ref) >= NEED_FOR.get(ref, NEED)


def identify(img: imaging.Image) -> Optional[str]:
    for screen, refs in SCREENS:
        if all(_shows(img, r) for r in refs):
            return screen
    return None


@dataclass
class Decision:
    what: str                          # for the log: what the bot does
    point: Optional[tuple] = None      # where to click; None = just wait
    settle: float = 1.5                # seconds to wait after the click before looking again


class Brain:
    """The bot's state and decisions (no browser access, so it can be tested)."""

    def __init__(self, settings: BotSettings, now: float = 0.0):
        self.s = settings
        self.modes = settings.modes
        self.current = 0                       # index into self.modes
        self.out: set = set()                  # modes that ran out in this cycle
        self.rest_until = 0.0
        self.runs = {m: 0 for m in MODES}
        self.prev_screen: Optional[str] = None
        self.screen_since = now
        self.close_team = False   # a "Not enough" popup came from a team screen: leave it, don't accept again
        self.fight_since: Optional[float] = None   # Invasion: when the current battle started
        self.entered: Optional[str] = None         # the mode whose team the bot last accepted
        self.last_known = now

    @property
    def mode(self) -> Optional[str]:
        if self.rest_until or not self.modes:
            return None
        return self.modes[self.current]

    def status(self, now: float) -> str:
        if self.rest_until:
            return "resting until " + time.strftime("%H:%M", time.localtime(time.time() + self.rest_until - now))
        return f"playing {MODES[self.mode]}" if self.mode else "nothing to play"

    def _next_mode(self, now: float) -> str:
        for k in range(1, len(self.modes) + 1):
            j = (self.current + k) % len(self.modes)
            if self.modes[j] not in self.out:
                self.current = j
                return f"next: {MODES[self.modes[j]]}"
        self.rest_until = now + self.s.rest_min * 60
        return f"every mode is out; resting {self.s.rest_min} min"

    def decide(self, screen: Optional[str], now: float) -> Decision:
        fresh = screen != self.prev_screen  # count each result screen once
        if fresh:
            self.prev_screen, self.screen_since = screen, now
        if screen is not None:
            self.last_known = now
        if self.rest_until and now >= self.rest_until:
            self.rest_until, self.out, self.current = 0.0, set(), 0
        mode = self.mode
        if screen is None:
            return Decision("")
        if screen == "disconnected":
            return Decision("disconnected → Reconnect", (427, 377), settle=20)
        if screen == "still_there":
            return Decision("“Are you still there?” → Yes", (427, 377))
        if screen in OUT_OF:
            ran_out = OUT_OF[screen]
            self.out.add(ran_out)
            self.close_team = True
            note = ""
            if ran_out == mode:
                note = "; " + self._next_mode(now)
            return Decision(f"out of {screen.split('_')[-1]} → No{note}", NO)
        if screen not in ("team", "team_not_full"):
            self.close_team = False
        if screen == "team_not_full":
            self.entered = mode
            return Decision("team not full → Yes", YES)
        if screen == "quit_battle":
            return Decision("leave the lobby → Yes", YES)
        if screen == "reward_items":
            return Decision("rewards → close", (614, 155))
        if screen == "season_rewards":
            return Decision("daily reward → Claim", (427, 478))
        if screen == "cleared":
            if mode in ("quest", "raid"):
                self.runs[mode] += fresh
                return Decision(f"cleared ({self._count(mode)}) → Rerun", (365, 498), settle=4)
            return Decision("cleared → Town", (524, 498), settle=4)
        if screen == "wb_result":
            self.runs["wb"] += fresh
            if mode == "wb":
                return Decision(f"fight over ({self._count('wb')}) → Regroup", (444, 498), settle=3)
            return Decision("fight over → Town", (522, 498), settle=4)
        if screen == "wb_defeat":
            self.runs["wb"] += fresh
            if mode == "wb":
                return Decision(f"lost ({self._count('wb')}) → Regroup", (365, 498), settle=3)
            return Decision("lost → Town", (524, 498), settle=4)
        if screen == "victory_continue":
            # Autopilot continues by itself; only help if it does not.
            if now - self.screen_since > 10:
                return Decision("victory → Continue", (500, 498))
            return Decision("")
        if screen == "dungeon_autopilot_off":
            # Leaving an Invasion battle switches autopilot off for every mode.
            return Decision("autopilot is off in the dungeon → on", AUTO_BUTTON)
        if screen in ("autopilot_on", "autopilot_off"):
            return self._battle(screen, mode, now)
        self.fight_since = None
        self.entered = None
        if screen in ("result_middle", "result_right"):
            if mode in ("pvp", "gauntlet", "invasion"):
                self.runs[mode] += fresh
            x = 443 if screen == "result_middle" else 522
            return Decision(f"fight over{f' ({self._count(mode)})' if mode in ('pvp', 'gauntlet', 'invasion') else ''} → Town",
                            (x, 498), settle=4)
        if screen == "team":
            if self.close_team:
                return Decision("team of a mode that ran out → close", TEAM_CLOSE)
            self.entered = mode
            return Decision("team → Accept", (550, 487))
        if screen == "select_opponent":
            n = self.s.pvp_opponent
            return Decision(f"opponent #{n} → Fight", (638, 214 + 57 * (n - 1)))
        if screen == "raid_difficulty":
            if mode == "raid":
                return Decision(f"raid → {self.s.raid_difficulty.title()}",
                                (RAID_DIFFICULTY[self.s.raid_difficulty], 244))
            return Decision("raid difficulty → close", (748, 112))
        if screen in WINDOWS:
            owner, go, close = WINDOWS[screen]
            if owner == mode:
                return Decision(f"{screen.replace('_', ' ')} → go", go)
            return Decision(f"{screen.replace('_', ' ')} → close", close)
        if screen == "quest_map":
            if mode == "quest":
                return Decision("zone map → dungeon", tuple(self.s.quest_point))
            return Decision("zone map → close", (772, 75))
        if screen == "main":
            if mode is None:
                return Decision("")
            return Decision(f"town → {MODES[mode]}", ICON[mode])
        return Decision("")

    def _battle(self, screen: str, mode: Optional[str], now: float) -> Decision:
        """In a battle (or a dungeon). Invasion runs on and on: fight long
        enough for the loot, then switch autopilot off (the battle's X only
        works then), leave, and confirm. Anywhere else autopilot stays on."""
        if mode == "invasion" and self.entered == "invasion":   # only a battle the bot started
            if self.fight_since is None:
                self.fight_since = now
            done = now - self.fight_since >= self.s.invasion_s
            if screen == "autopilot_on":
                return Decision(f"Invasion: fought {self.s.invasion_s} s → autopilot off", AUTO_BUTTON) if done \
                    else Decision("")
            if done:
                return Decision("Invasion: leave the battle", BATTLE_X)
        if screen == "autopilot_off":
            return Decision("autopilot is off → on", AUTO_BUTTON)
        return Decision("")

    def _count(self, mode: Optional[str]) -> str:
        return f"{MODES[mode]} #{self.runs[mode]}" if mode else ""
