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

MODES = {"quest": "Quest", "raid": "Raid", "pvp": "PvP", "gauntlet": "Gauntlet / Trials", "wb": "World Boss",
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
DIALOG_NEXT = (774, 325)      # the yellow arrow of a character's dialog
# Raid and World Boss windows show one raid / boss at a time: ◀ ▶ step through
# them, and a row of dots (grey, the current one green) says which one it is.
CAROUSEL_PREV, CAROUSEL_NEXT = (168, 312), (686, 312)
DOTS_Y, DOTS_X = 463, (200, 660)
DOT_GREY, DOT_GREEN = (0x93, 0x93, 0x93), (0x00, 0x93, 0x00)
# The Quest zone map: ◀ ▶ step through the zones (no wrapping; an arrow that
# cannot go further is greyed out). The zone is told by its title (zone_NN refs).
ZONE_PREV, ZONE_NEXT = (65, 290), (790, 290)
ZONE_ARROW_PX = {ZONE_PREV: (62, 290), ZONE_NEXT: (792, 290)}
ARROW_ON = (238, 234, 114)    # a usable arrow; a greyed-out one is about (134, 133, 76)
ZONES = 20                    # zone_01 (Bit Valley) … zone_20 (Big Claw)
ESC_WAIT_S = 5.0             # after Esc in a battle: how long the quit popup may take to appear
# World Boss lobby: the team rows (1 = the summoner) are 56 px apart. A row
# that exists has a light frame on its left; an empty one shows a blue Invite button.
WB_ROW_Y = (197, 253, 309, 365, 421)
WB_FRAME_X, WB_FRAME_DY, WB_FRAME_RGB = 116, -12, (0xd1, 0xd9, 0xe0)
WB_INVITE = ((372, (0x12, 0x67, 0xd3)), (404, (0xff, 0xff, 0xff)), (444, (0x12, 0x67, 0xd3)))
OUT_OF = {"not_enough_energy": "quest", "not_enough_tickets": "pvp", "not_enough_shards": "raid",
          "not_enough_xeals": "wb", "not_enough_tokens": "gauntlet", "not_enough_badges": "invasion"}

# Screens, most specific first: (name, reference images that must all match;
# a name starting with "!" must NOT match).
SCREENS: list[tuple[str, tuple[str, ...]]] = [
    ("disconnected", ("disconnected", "reconnect")),
    ("still_there", ("still_there_text", "still_there_yes")),
    # A character talking (e.g. the first meeting with a boss; a new browser
    # shows these again): a text box with a yellow arrow to go on. The
    # speaker's picture is on the left or on the right, over the box's ends,
    # so the box is known by the middle of its bottom edge.
    ("dialog", ("dialog_arrow", "dialog_bottom")),
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
    # A drop-down list (Gauntlet / Trials difficulty, Invasion wave), over its window.
    ("pick_list", ("list_title", "list_up")),
    ("team", ("team_title", "accept")),
    ("select_opponent", ("select_opponent",)),
    ("pvp_window", ("pvp_arena",)),
    ("raid_difficulty", ("raid_normal", "raid_heroic")),
    ("raid_window", ("raid_title", "raid_summon")),
    ("gauntlet_window", ("gauntlet",)),
    # Trials (every other week instead of Gauntlet): the same window with
    # another title. Known by its Difficulty field (Invasion has a Wave field).
    ("trials_window", ("difficulty_label", "!gauntlet")),
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
    "trials_window": ("gauntlet", (584, 292), (688, 123)),
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
    wb_players: int = 1            # World Boss: wait in the lobby until this many players are in the team
    battle_speed: int = 1          # game speed, everywhere (1 = normal); saved under its first name
    raid_number: int = 0           # which raid (1 = the first); 0 = the one the game shows
    wb_boss: int = 0               # which World Boss (1 = the first); 0 = the one the game shows
    quest_zone: int = 0            # which Quest zone (1 = Bit Valley); 0 = the one the game shows
    gauntlet_difficulty: int = 0   # Gauntlet difficulty (the number in its window); 0 = as set in the game
    trials_difficulty: int = 0     # Trials difficulty; 0 = as set in the game
    invasion_wave: int = 0         # Invasion wave; 0 = as set in the game

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
        s.wb_players = min(5, max(1, int(d.get("wb_players", 1) or 1)))
        s.battle_speed = min(10, max(1, int(d.get("battle_speed", 1) or 1)))
        s.raid_number = min(30, max(0, int(d.get("raid_number", 0) or 0)))
        s.wb_boss = min(30, max(0, int(d.get("wb_boss", 0) or 0)))
        s.quest_zone = min(ZONES, max(0, int(d.get("quest_zone", 0) or 0)))
        for key in ("gauntlet_difficulty", "trials_difficulty", "invasion_wave"):
            setattr(s, key, min(999999, max(0, int(d.get(key, 0) or 0))))
        return s

    def to_dict(self) -> dict:
        return {"order": self.order, "enabled": self.enabled, "raid_difficulty": self.raid_difficulty,
                "pvp_opponent": self.pvp_opponent, "quest_point": list(self.quest_point),
                "rest_min": self.rest_min, "invasion_s": self.invasion_s,
                "wb_players": self.wb_players, "battle_speed": self.battle_speed,
                "raid_number": self.raid_number, "wb_boss": self.wb_boss, "quest_zone": self.quest_zone,
                "gauntlet_difficulty": self.gauntlet_difficulty, "trials_difficulty": self.trials_difficulty,
                "invasion_wave": self.invasion_wave}

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


def _near(img: imaging.Image, x: int, y: int, rgb: tuple, tol: int = 40) -> bool:
    i = (y * img.width + x) * 3
    return all(abs(img.rgb[i + k] - rgb[k]) <= tol for k in range(3))


def wb_team(img: imaging.Image) -> tuple:
    """World Boss lobby: (players in the team, team size) from the rows shown."""
    rows = invites = 0
    for y in WB_ROW_Y:
        if not _near(img, WB_FRAME_X, y + WB_FRAME_DY, WB_FRAME_RGB):
            break
        rows += 1
        invites += all(_near(img, x, y, rgb) for x, rgb in WB_INVITE)
    return rows - invites, rows


def carousel(img: imaging.Image) -> Optional[tuple]:
    """Raid / World Boss window: (number of the one shown, how many there are), or None."""
    dots, current, x = 0, 0, DOTS_X[0]
    while x < DOTS_X[1]:
        green = _near(img, x, DOTS_Y, DOT_GREEN, 30)
        if green or _near(img, x, DOTS_Y, DOT_GREY, 12):
            dots += 1
            if green:
                current = dots
            x += 12            # past this dot (dots are 26 px apart, about 8 px wide)
        else:
            x += 1
    return (current, dots) if current else None


def zone(img: imaging.Image) -> tuple:
    """Quest zone map: (zone number or None if unknown, can go back, can go on)."""
    best, number = 0.0, None
    for n in range(1, ZONES + 1):
        score = matches(img, f"zone_{n:02d}")
        if score > best:
            best, number = score, n
    can = {p: _near(img, *ZONE_ARROW_PX[p], ARROW_ON) for p in (ZONE_PREV, ZONE_NEXT)}
    return (number if best >= 0.95 else None), can[ZONE_PREV], can[ZONE_NEXT]


# ----------------------------------------------------------- reading numbers
# The game's pixel font, read glyph by glyph: light text on dark boxes. Each
# glyph is cut out between empty columns and compared with the templates in
# glyphs.json (made from screenshots with known text: tests/make_glyphs.py).
INK = 170                     # brightest channel at least this: text
BIG_TEXT = 14                 # line height of the numbers (the small "TIER" lines are 12)
_GLYPHS: Optional[list] = None


def _glyph_templates() -> list:
    global _GLYPHS
    if _GLYPHS is None:
        path = DIR / "glyphs.json"
        _GLYPHS = json.loads(path.read_text()) if path.exists() else []
    return _GLYPHS


def _is_ink(img: imaging.Image, x: int, y: int) -> bool:
    i = (y * img.width + x) * 3
    return max(img.rgb[i], img.rgb[i + 1], img.rgb[i + 2]) >= INK


def glyphs(img: imaging.Image, box: tuple) -> list:
    """The big-font text lines in ``box`` (x0, y0, x1, y1): for each line, its
    glyphs as bitmaps ("#." rows joined by "/")."""
    x0, y0, x1, y1 = box
    ink_rows = [y for y in range(y0, y1) if any(_is_ink(img, x, y) for x in range(x0, x1))]
    lines, start = [], None
    for y in range(y0, y1 + 1):
        if y in ink_rows and start is None:
            start = y
        elif y not in ink_rows and start is not None:
            lines.append((start, y))
            start = None
    out = []
    for ly0, ly1 in lines:
        if ly1 - ly0 < BIG_TEXT:
            continue
        cols = [x for x in range(x0, x1) if any(_is_ink(img, x, y) for y in range(ly0, ly1))]
        line, seg = [], []
        for x in cols + [None]:
            if seg and (x is None or x != seg[-1] + 1):
                ys = [y for y in range(ly0, ly1) if any(_is_ink(img, c, y) for c in seg)]
                line.append("/".join("".join("#" if _is_ink(img, c, y) else "." for c in seg)
                                     for y in range(ys[0], ys[-1] + 1)) + f"@{ys[0] - ly0}")
                seg = []
            if x is not None:
                seg.append(x)
        out.append(line)
    return out


def _grid(bitmap: str, gw: int = 6, gh: int = 9) -> list:
    rows = bitmap.split("@")[0].split("/")
    h, w = len(rows), len(rows[0])
    return [rows[min(h - 1, (r * h) // gh)][min(w - 1, (c * w) // gw)] == "#" for r in range(gh) for c in range(gw)]


def read_glyph(bitmap: str) -> str:
    """The character a glyph bitmap shows ("?" if none is close)."""
    best, char = 1e9, "?"
    rows = bitmap.split("@")[0].split("/")
    size = (len(rows[0]), len(rows), int(bitmap.split("@")[1]))
    for t in _glyph_templates():
        if t["bits"] == bitmap:
            return t["c"]
        trows = t["bits"].split("@")[0].split("/")
        tsize = (len(trows[0]), len(trows), int(t["bits"].split("@")[1]))
        d = sum(a != b for a, b in zip(_grid(bitmap), _grid(t["bits"])))
        d += 3 * sum(abs(a - b) for a, b in zip(size, tsize))
        if d < best:
            best, char = d, t["c"]
    return char if best <= 12 else "?"


def read_numbers(img: imaging.Image, box: tuple) -> Optional[tuple]:
    """The numbers on the first big-font line in ``box``: "13,495 - 20,003"
    gives (13495, 20003), "58" gives (58,). None if unreadable."""
    lines = glyphs(img, box)
    if not lines:
        return None
    text = "".join(read_glyph(g) for g in lines[0])
    if "?" in text:
        return None
    parts = [p.replace(",", "") for p in text.split("-")]
    if not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


# Drop-down lists (Gauntlet / Trials difficulty, Invasion wave): five rows,
# highest first, a scroll bar with ▲ ▼ and a handle that can be dragged.
LIST_X = (285, 545)
LIST_ROW_TOP, LIST_ROW_PITCH, LIST_ROW_H = 137, 63.25, 54
LIST_ROWS = 5
LIST_CLOSE = (598, 95)
SCROLL_X, SCROLL_TRACK = 573, (149, 447)     # the handle moves between these
FIELD_BOX = (510, 402, 625, 428)             # the chosen value in the window, inside its frame
FIELD_OPEN = (651, 413)                      # that ▼


def list_row_center(k: int) -> tuple:
    return 415, int(LIST_ROW_TOP + LIST_ROW_PITCH * k + LIST_ROW_H / 2)


def list_rows(img: imaging.Image) -> list:
    """The numbers in each row of an open drop-down list (None: unreadable)."""
    out = []
    for k in range(LIST_ROWS):
        top = int(LIST_ROW_TOP + LIST_ROW_PITCH * k)
        out.append(read_numbers(img, (LIST_X[0], top + 3, LIST_X[1], top + LIST_ROW_H - 3)))
    return out


def scroll_handle(img: imaging.Image) -> Optional[tuple]:
    """(top, length) of the list's scroll bar handle: it has light edges (the track has none)."""
    def light(y):
        i = (y * img.width + SCROLL_X) * 3
        return min(img.rgb[i:i + 3]) >= 0xb8
    ys = [y for y in range(*SCROLL_TRACK) if light(y)]
    if not ys:
        return None
    return ys[0], ys[-1] - ys[0] + 1


def look_closer(img: imaging.Image, screen: Optional[str]) -> dict:
    """What the bot needs to know beyond the screen's name (see Brain.decide)."""
    if screen == "wb_lobby":
        return {"team": wb_team(img)}
    if screen in ("raid_window", "wb_carousel"):
        return {"dots": carousel(img)}
    if screen == "quest_map":
        return {"zone": zone(img)}
    if screen in PICK_FIELD:
        value = read_numbers(img, FIELD_BOX)
        return {"field": value[0] if value and len(value) == 1 else None}
    if screen == "pick_list":
        return {"rows": list_rows(img), "handle": scroll_handle(img)}
    return {}


# Windows with a drop-down to set before Play: screen -> (what, setting).
PICK_FIELD = {"gauntlet_window": ("Gauntlet difficulty", "gauntlet_difficulty"),
              "trials_window": ("Trials difficulty", "trials_difficulty"),
              "invasion_window": ("Invasion wave", "invasion_wave")}
# Once one of the game's lists has been scrolled (with ▼, by dragging the rows
# or the scroll bar), a click on a row often picks another row or none at
# all. So the bot only picks from the rows shown when a list opens (its top).


@dataclass
class Decision:
    what: str                          # for the log: what the bot does
    point: Optional[tuple] = None      # where to click; None = just wait
    settle: float = 1.5                # seconds to wait after the click before looking again
    key: Optional[str] = None          # a key to press instead of clicking (WebDriver key value)
    stays: bool = False                # the click changes the picture, not the screen (◀ ▶):
                                       # just wait ``settle`` before looking again


ESCAPE = ""


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
        # Leaving an Invasion battle: Esc (keeps autopilot on) if the game takes
        # it, else autopilot off + the battle's X. None = not tried yet.
        self.esc_works: Optional[bool] = None
        self.esc_at: Optional[float] = None        # when Esc was pressed in this battle
        self.esc_tries = 0
        self.leave_by: Optional[str] = None        # how this battle is being left: "esc" or "x"
        self.notes: set = set()                    # notes already given (see _warned)
        self.picking: Optional[dict] = None        # a drop-down being set: key, what, want, range, steps
        self.picked: set = set()                   # settings already chosen in the game this run
        self.chosen: dict = {}                     # setting -> the value the bot clicked

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

    def decide(self, screen: Optional[str], now: float, team: Optional[tuple] = None,
               dots: Optional[tuple] = None, zone: Optional[tuple] = None, field: Optional[int] = None,
               rows: Optional[list] = None, handle: Optional[tuple] = None) -> Decision:
        """What to do on ``screen``. From look_closer: ``team`` is (players,
        size) in the World Boss lobby, ``dots`` (shown, count) in the Raid and
        World Boss windows, ``zone`` (number, can go back, can go on) on the
        zone map, ``field`` the difficulty / wave set in the Gauntlet, Trials
        and Invasion windows, ``rows`` and ``handle`` an open drop-down list."""
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
        if screen == "dialog":
            return Decision("dialog → next", DIALOG_NEXT, settle=1.0)
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
            if self.leave_by == "esc":
                note = "" if self.esc_works else " (Esc works: autopilot stays on)"
                self.esc_works = True
                return Decision(f"quit the battle → Yes{note}", YES)
            return Decision("leave → Yes", YES)
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
        self.esc_at, self.esc_tries, self.leave_by = None, 0, None
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
        if screen == "pick_list":
            return self._pick(rows, handle)
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
            if screen == "wb_lobby" and owner == mode and self.s.wb_players > 1 and team:
                players, size = team
                need = min(self.s.wb_players, size)
                if players < need:
                    return Decision(f"World Boss lobby: {players} of {need} players; waiting for the rest")
                return Decision(f"World Boss lobby: {players} of {size} players → Start", go)
            if screen in PICK_FIELD and owner == mode:
                what, key = PICK_FIELD[screen]
                want = getattr(self.s, key)
                chosen = self.chosen.get(key)
                if chosen is not None and field is not None and field != chosen \
                        and not self._warned(f"check {key}"):
                    return Decision(f"{what}: the game set {field:,}, not the {chosen:,} clicked; playing it")
                if want and key not in self.picked:
                    if field is None:
                        if not self._warned(f"read {key}"):
                            return Decision(f"could not read the {what}; playing it as set in the game")
                    elif abs(field - want) >= (1 if key == "invasion_wave" else 10):
                        self.picking = {"key": key, "what": what, "want": want}
                        return Decision(f"{what} is {field:,} → choose {want:,}", FIELD_OPEN)
            want = {"raid_window": self.s.raid_number, "wb_carousel": self.s.wb_boss}.get(screen, 0)
            if owner == mode and want and dots:
                shown, count = dots
                kind = "raid" if screen == "raid_window" else "World Boss"
                if want > count:
                    if not self._warned(f"{kind} {want}"):
                        plural = "raids" if kind == "raid" else "World Bosses"
                        return Decision(f"there are only {count} {plural}, not {want}; playing the one shown")
                elif shown != want:
                    step = CAROUSEL_NEXT if shown < want else CAROUSEL_PREV
                    return Decision(f"{kind} {shown} of {count} → {'▶' if shown < want else '◀'} (to {kind} {want})",
                                    step, settle=1.2, stays=True)
            if owner == mode:
                return Decision(f"{screen.replace('_', ' ')} → go", go)
            return Decision(f"{screen.replace('_', ' ')} → close", close)
        if screen == "quest_map":
            if mode == "quest":
                want = self.s.quest_zone
                if want and zone:
                    number, back, on = zone
                    if number is None:        # a zone the bot does not know: head for the known ones
                        if back or on:
                            return Decision(f"unknown zone → {'◀' if back else '▶'} (to zone {want})",
                                            ZONE_PREV if back else ZONE_NEXT, settle=1.5, stays=True)
                    elif number < want and on:
                        return Decision(f"zone {number} → ▶ (to zone {want})", ZONE_NEXT, settle=1.5, stays=True)
                    elif number > want and back:
                        return Decision(f"zone {number} → ◀ (to zone {want})", ZONE_PREV, settle=1.5, stays=True)
                    elif number != want and not self._warned(f"zone {want}"):
                        return Decision(f"zone {want} is not open yet; playing zone {number}")
                return Decision("zone map → dungeon", tuple(self.s.quest_point))
            return Decision("zone map → close", (772, 75))
        if screen == "main":
            if mode is None:
                return Decision("")
            return Decision(f"town → {MODES[mode]}", ICON[mode])
        return Decision("")

    def _battle(self, screen: str, mode: Optional[str], now: float) -> Decision:
        """In a battle (or a dungeon). Invasion runs on and on: fight long
        enough for the loot, then leave and confirm. Leaving: Esc, which keeps
        autopilot on. If the game ignores Esc, switch autopilot off (the
        battle's X only works then) and press the X. Anywhere else autopilot stays on."""
        if mode == "invasion" and self.entered == "invasion":   # only a battle the bot started
            if self.fight_since is None:
                self.fight_since = now
            # invasion_s is game time; the game runs at the bot's speed.
            done = (now - self.fight_since) * max(1, self.s.battle_speed) >= self.s.invasion_s
            if screen == "autopilot_on":
                if not done:
                    return Decision("")
                if self.esc_works is not False and self.leave_by != "x":
                    waited = self.esc_at is not None and now - self.esc_at >= ESC_WAIT_S
                    if self.esc_at is None or (waited and self.esc_tries < 2):
                        self.esc_at, self.leave_by = now, "esc"
                        self.esc_tries += 1
                        again = " again" if self.esc_tries > 1 else ""
                        return Decision(f"Invasion: fought {self.s.invasion_s} s → Esc{again} (quit the battle)",
                                        key=ESCAPE, settle=ESC_WAIT_S)
                    if not waited:
                        return Decision("")
                    if self.esc_works is None:
                        self.esc_works = False
                    self.leave_by = "x"
                    return Decision("Invasion: Esc did not open the quit popup → autopilot off, then X",
                                    AUTO_BUTTON)
                self.leave_by = "x"
                return Decision(f"Invasion: fought {self.s.invasion_s} s → autopilot off", AUTO_BUTTON)
            if done:
                self.leave_by = "x"
                return Decision("Invasion: leave the battle", BATTLE_X)
        if screen == "autopilot_off":
            return Decision("autopilot is off → on", AUTO_BUTTON)
        return Decision("")

    def _pick(self, rows: Optional[list], handle: Optional[tuple]) -> Decision:
        """In an open drop-down list: click the wanted difficulty / wave if it
        is among the rows shown when the list opens (see the note above
        PICK_FIELD). Gauntlet and Trials first list ranges ("12,488 -
        13,494"); a range opens the list of its values (10 apart, highest first)."""
        p = self.picking
        if not p:
            return Decision("drop-down list → close", LIST_CLOSE)
        if not rows or any(r is None for r in rows):
            return self._give_up(p, "could not read the list")
        want, what = p["want"], p["what"]
        at_bottom = handle is None or handle[0] + handle[1] >= SCROLL_TRACK[1] - 3
        far = (f"{want:,} is too far down the game's list (the bot only clicks the rows shown when "
               "it opens); set it once in the game, which keeps it")

        if all(len(r) == 2 for r in rows):             # ranges
            hit = next((k for k, (a, b) in enumerate(rows) if a <= want <= b), None)
            if hit is None and want > rows[0][1]:
                hit = 0                                # above the hardest: the hardest range
            if hit is None and at_bottom:
                hit = LIST_ROWS - 1                    # below the easiest (a short list)
            if hit is None:
                return self._give_up(p, far)
            a, b = rows[hit]
            return Decision(f"{what}: {want:,} → the range {a:,} – {b:,}", list_row_center(hit),
                            settle=1.2, stays=True)

        values = [r[0] for r in rows]
        if want > values[0]:
            k, note = 0, " (the highest there is)"
        elif want >= values[-1] - (values[-2] - values[-1]) // 2:
            k, note = min(range(LIST_ROWS), key=lambda i: abs(values[i] - want)), ""
        elif at_bottom:
            k, note = LIST_ROWS - 1, " (the lowest there is)"
        else:
            return self._give_up(p, far)
        self.picked.add(p["key"])
        self.picking = None
        self.chosen[p["key"]] = values[k]
        return Decision(f"{what} → {values[k]:,}{note}", list_row_center(k))

    def _give_up(self, p: dict, why: str) -> Decision:
        self.picked.add(p["key"])
        self.picking = None
        return Decision(f"{p['what']}: {why} → close the list, play as set", LIST_CLOSE)

    def _warned(self, what: str) -> bool:
        """True if the user was already told about ``what`` (each note once)."""
        if what in self.notes:
            return True
        self.notes.add(what)
        return False

    def _count(self, mode: Optional[str]) -> str:
        return f"{MODES[mode]} #{self.runs[mode]}" if mode else ""
