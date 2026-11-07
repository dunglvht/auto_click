"""The Bit Heroes bot panel (the main window's first tab): modes, their
options, start and stop. Plus the dungeon picker, a zone map to click on."""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING, Optional

from .bitheroes import (COST_MAX, DUNGEON_DIFFICULTY, MAP_BOX, MODES, RAID_DIFFICULTY, ZONE_NAMES, ZONES,
                        BotSettings, DIR)

if TYPE_CHECKING:  # pragma: no cover
    from .gui import App

INTRO = ("Tick the modes to play and put them in order. The bot plays the first until it runs out "
         "(energy, shards, xeals, tokens, tickets, badges), answers “No” to buying more, and goes on "
         "to the next. When all are out, it rests and starts again. It never spends gems. "
         "The game's autopilot does the fighting.")
HINT = ("0 = whatever the game has set. Costs are per run (1–5). Raid / Boss #: 1 = the first in the "
        "summon window. Difficulty and wave: the bot can pick the 5 highest waves and, for "
        "Gauntlet / Trials, the top of the list (see README).")

# The options of each mode, shown on its row: (setting, label, kind, details).
SPIN, COMBO, ZONE, DUNGEON = "spin", "combo", "zone", "dungeon"
LEVELS = [d.title() for d in RAID_DIFFICULTY]
MODE_OPTIONS = {
    "raid": [("raid_number", "Raid #", SPIN, (0, 30, 3)), ("raid_difficulty", "Difficulty", COMBO, LEVELS)],
    "wb": [("wb_boss", "Boss #", SPIN, (0, 30, 3)), ("wb_players", "Players", SPIN, (1, 5, 3))],
    "invasion": [("invasion_wave", "Wave", SPIN, (0, 999, 4)), ("invasion_cost", "Badges", SPIN, (0, COST_MAX, 3)),
                 ("invasion_s", "Fight s", SPIN, (5, 600, 4))],
    "gauntlet": [("gauntlet_difficulty", "Gauntlet", SPIN, (0, 999999, 7)),
                 ("trials_difficulty", "Trials", SPIN, (0, 999999, 7)),
                 ("gauntlet_cost", "Tokens", SPIN, (0, COST_MAX, 3))],
    "pvp": [("pvp_opponent", "Opponent", SPIN, (1, 4, 3)), ("pvp_cost", "Tickets", SPIN, (0, COST_MAX, 3))],
    "quest": [("quest_zone", "Zone", ZONE, None), ("quest_points", "Dungeon", DUNGEON, None),
              ("quest_difficulty", "Difficulty", COMBO, [d.title() for d in DUNGEON_DIFFICULTY])],
}
ZONE_CHOICES = ["as shown"] + [f"{n}  {name}" for n, name in enumerate(ZONE_NAMES, 1)]


def zone_label(n: int) -> str:
    return ZONE_CHOICES[n] if 0 <= n <= ZONES else ZONE_CHOICES[0]


def zone_number(label: str) -> int:
    head = label.split()[0] if label else ""
    return int(head) if head.isdigit() else 0


class BotPanel(ttk.Frame):
    def __init__(self, parent, app: "App"):
        super().__init__(parent, padding=(10, 8))
        self.app = app
        self.s = BotSettings.from_dict(app.settings.get("bot") or {})
        self.picker: Optional[DungeonPicker] = None
        ttk.Label(self, text=INTRO, wraplength=760, justify="left").pack(anchor="w")

        self.table = ttk.Frame(self)
        self.table.pack(fill="x", pady=(10, 0))
        self.enabled = {m: tk.BooleanVar(value=m in self.s.enabled) for m in MODES}
        self.runs_vars = {m: tk.StringVar(value="") for m in MODES}
        self.vars: dict[str, tk.StringVar] = {}
        for opts in MODE_OPTIONS.values():
            for key, _label, kind, details in opts:
                if kind == SPIN:
                    self.vars[key] = tk.StringVar(value=str(getattr(self.s, key)))
                elif kind == COMBO:
                    self.vars[key] = tk.StringVar(value=getattr(self.s, key).title())
                elif kind == ZONE:
                    self.vars[key] = tk.StringVar(value=zone_label(self.s.quest_zone))
                    self.vars[key].trace_add("write", lambda *a: self._show_dungeon())
        self.dungeon_var = tk.StringVar()
        self._draw_rows()

        general = ttk.Frame(self)
        general.pack(fill="x", pady=(10, 0))
        ttk.Label(general, text="When all are out, rest").pack(side="left")
        self.rest_var = tk.StringVar(value=str(self.s.rest_min))
        ttk.Spinbox(general, from_=1, to=600, width=4, textvariable=self.rest_var).pack(side="left", padx=4)
        ttk.Label(general, text="min").pack(side="left")
        ttk.Label(general, text="Game speed").pack(side="left", padx=(24, 4))
        self.speed_var = tk.StringVar(value=str(self.s.battle_speed))
        ttk.Spinbox(general, from_=1, to=10, width=3, textvariable=self.speed_var).pack(side="left")
        ttk.Label(general, text="× (1 = normal; switch off other speed-hack add-ons)",
                  style="Hint.TLabel").pack(side="left", padx=(4, 0))

        ttk.Label(self, text=HINT, style="Hint.TLabel", wraplength=760, justify="left").pack(
            anchor="w", pady=(8, 0))
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(10, 0))
        self.start_btn = ttk.Button(bar, text="▶  Start bot  (F9)", style="Accent.TButton", width=18,
                                    command=self.toggle)
        self.start_btn.pack(side="left")
        self.status_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.status_var, wraplength=560, font=app.font_bold).pack(
            side="left", padx=(12, 0))
        self.update_state()

    # ------------------------------------------------------------------ table

    def _draw_rows(self) -> None:
        for w in self.table.winfo_children():
            w.destroy()
        for c, title in ((1, "Mode"), (4, "Options")):
            ttk.Label(self.table, text=title, font=self.app.font_bold).grid(row=0, column=c, sticky="w",
                                                                            columnspan=3, pady=(0, 4))
        for i, m in enumerate(self.s.order, start=1):
            pad = dict(pady=3)
            ttk.Label(self.table, text=f"{i}.").grid(row=i, column=0, sticky="w", **pad)
            ttk.Checkbutton(self.table, text=MODES[m], variable=self.enabled[m], width=17).grid(
                row=i, column=1, sticky="w", **pad)
            ttk.Button(self.table, text="▲", width=2, command=lambda i=i: self._move(i - 1, -1)).grid(
                row=i, column=2, padx=(2, 0), **pad)
            ttk.Button(self.table, text="▼", width=2, command=lambda i=i: self._move(i - 1, 1)).grid(
                row=i, column=3, padx=(2, 12), **pad)
            for k, opt in enumerate(MODE_OPTIONS.get(m, [])):
                self._option(i, 4 + k, *opt)
            ttk.Label(self.table, textvariable=self.runs_vars[m], style="Hint.TLabel").grid(
                row=i, column=7, sticky="w", padx=(8, 0), **pad)

    def _option(self, row: int, col: int, key: str, label: str, kind: str, details) -> None:
        cell = ttk.Frame(self.table)
        cell.grid(row=row, column=col, sticky="w", padx=(0, 14), pady=3)
        ttk.Label(cell, text=label).pack(side="left", padx=(0, 4))
        if kind == SPIN:
            lo, hi, width = details
            ttk.Spinbox(cell, from_=lo, to=hi, width=width, textvariable=self.vars[key]).pack(side="left")
        elif kind == COMBO:
            ttk.Combobox(cell, textvariable=self.vars[key], values=details, state="readonly",
                         width=8).pack(side="left")
        elif kind == ZONE:
            ttk.Combobox(cell, textvariable=self.vars[key], values=ZONE_CHOICES, state="readonly",
                         width=19).pack(side="left")
        elif kind == DUNGEON:
            ttk.Button(cell, text="Pick on map…", command=self.pick_dungeon).pack(side="left")
            ttk.Label(cell, textvariable=self.dungeon_var, style="Hint.TLabel").pack(side="left", padx=(6, 0))
            self._show_dungeon()

    def _move(self, i: int, delta: int) -> None:
        j = i + delta
        if 0 <= j < len(self.s.order):
            self.s.order[i], self.s.order[j] = self.s.order[j], self.s.order[i]
            self._draw_rows()

    def _show_dungeon(self) -> None:
        z = zone_number(self.vars["quest_zone"].get())
        if z == 0:
            picked = len(self.s.quest_points)
            self.dungeon_var.set(f"picked in {picked} zone{'s' if picked != 1 else ''}" if picked else "none picked")
        elif str(z) in self.s.quest_points:
            self.dungeon_var.set("picked")
        else:
            self.dungeon_var.set("not picked")

    def pick_dungeon(self) -> None:
        if self.picker:
            self.picker.lift()
            return
        z = zone_number(self.vars["quest_zone"].get()) or 12
        self.picker = DungeonPicker(self, z)

    # ---------------------------------------------------------------- actions

    def _read(self) -> BotSettings:
        s = self.s
        s.enabled = [m for m in s.order if self.enabled[m].get()]
        for opts in MODE_OPTIONS.values():
            for key, _label, kind, details in opts:
                try:
                    if kind == SPIN:
                        lo, hi, _w = details
                        setattr(s, key, min(hi, max(lo, int(self.vars[key].get().replace(",", "")))))
                    elif kind == COMBO:
                        setattr(s, key, self.vars[key].get().lower())
                    elif kind == ZONE:
                        s.quest_zone = zone_number(self.vars[key].get())
                except ValueError:
                    pass
        try:
            s.rest_min = max(1, int(self.rest_var.get()))
            s.battle_speed = min(10, max(1, int(self.speed_var.get())))
        except ValueError:
            pass
        self.app.settings["bot"] = s.to_dict()
        return s

    def save(self) -> None:
        self._read()

    def toggle(self) -> None:
        if self.app.mode == "playing":
            self.app.engine.stop_play()
            return
        if not (self.app.connected and self.app.target is not None and self.app.mode == "idle"):
            return
        settings = self._read()
        shots = os.path.join(os.path.dirname(self.app.settings_path()), "bot-screens")
        self.app.run_bg(lambda: self.app.engine.start_bot(settings, shots))

    def update_state(self) -> None:
        playing = self.app.mode == "playing"
        ready = self.app.connected and self.app.target is not None
        self.start_btn.configure(text="■  Stop bot  (F9)" if playing else "▶  Start bot  (F9)",
                                 state="normal" if playing or (ready and self.app.mode == "idle") else "disabled")
        if not playing:
            if not self.app.connected:
                self.status_var.set("Start Firefox first (Launch Firefox above).")
            elif self.app.target is None:
                self.status_var.set("Open the game (Open game window above), or pick its tab.")
            elif self.status_var.get().startswith(("Start Firefox", "Open the game")):
                self.status_var.set("")

    def show(self, info: dict) -> None:
        self.status_var.set(info.get("status", ""))
        for m, n in (info.get("runs") or {}).items():
            if m in self.runs_vars:
                self.runs_vars[m].set(f"{n} run{'s' if n != 1 else ''}" if n else "")

    def close(self) -> None:
        self._read()
        if self.picker:
            self.picker.destroy()


class DungeonPicker(tk.Toplevel):
    """A zone's map: click the dungeon the bot should play in that zone."""

    def __init__(self, panel: BotPanel, zone: int):
        super().__init__(panel)
        self.panel = panel
        self.title("Quest dungeon")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.close)
        pad = ttk.Frame(self, padding=12)
        pad.pack(fill="both", expand=True)
        top = ttk.Frame(pad)
        top.pack(fill="x")
        ttk.Label(top, text="Zone").pack(side="left")
        self.zone_var = tk.StringVar(value=zone_label(zone))
        box = ttk.Combobox(top, textvariable=self.zone_var, values=ZONE_CHOICES[1:], state="readonly", width=26)
        box.pack(side="left", padx=6)
        box.bind("<<ComboboxSelected>>", lambda e: self._load())
        ttk.Button(top, text="Clear this zone", command=self._clear).pack(side="left", padx=(12, 0))
        ttk.Button(top, text="Done", style="Accent.TButton", command=self.close).pack(side="right")
        ttk.Label(pad, style="Hint.TLabel", wraplength=660, justify="left", text=(
            "Click the dungeon the bot should play in this zone. Dungeons with three stars (D1–D3) "
            "ask for a difficulty: the bot takes the one set under Quest. The red one with a single "
            "star is the zone's boss (D4). Each zone keeps its own pick.")).pack(anchor="w", pady=(8, 6))
        w, h = MAP_BOX[2] - MAP_BOX[0], MAP_BOX[3] - MAP_BOX[1]
        self.canvas = tk.Canvas(pad, width=w, height=h, highlightthickness=0, cursor="crosshair")
        self.canvas.pack()
        self.canvas.bind("<Button-1>", self._click)
        self.note_var = tk.StringVar()
        ttk.Label(pad, textvariable=self.note_var).pack(anchor="w", pady=(6, 0))
        self.photo: Optional[tk.PhotoImage] = None
        self._load()
        self.transient(panel.winfo_toplevel())

    def _zone(self) -> int:
        return zone_number(self.zone_var.get())

    def _load(self) -> None:
        z = self._zone()
        self.canvas.delete("all")
        path = DIR / "maps" / f"zone_{z:02d}.png"
        try:
            self.photo = tk.PhotoImage(file=str(path))
            self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
        except tk.TclError:
            self.canvas.create_text(10, 10, anchor="nw", text=f"No map for zone {z}.")
        self._mark()

    def _mark(self) -> None:
        self.canvas.delete("mark")
        z = self._zone()
        p = self.panel.s.quest_points.get(str(z))
        if p:
            x, y = p[0] - MAP_BOX[0], p[1] - MAP_BOX[1]
            self.canvas.create_oval(x - 14, y - 14, x + 14, y + 14, outline="#e01b24", width=4, tags="mark")
            self.canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill="#e01b24", outline="", tags="mark")
            self.note_var.set(f"Zone {z} ({ZONE_NAMES[z - 1]}): the bot clicks {p[0]}, {p[1]}.")
        else:
            self.note_var.set(f"Zone {z} ({ZONE_NAMES[z - 1]}): nothing picked yet; the bot would click "
                              f"{self.panel.s.quest_point[0]}, {self.panel.s.quest_point[1]}.")

    def _click(self, event) -> None:
        z = self._zone()
        self.panel.s.quest_points[str(z)] = (event.x + MAP_BOX[0], event.y + MAP_BOX[1])
        self._mark()
        self.panel._show_dungeon()

    def _clear(self) -> None:
        self.panel.s.quest_points.pop(str(self._zone()), None)
        self._mark()
        self.panel._show_dungeon()

    def close(self) -> None:
        self.panel.picker = None
        self.panel._show_dungeon()
        self.panel.save()
        self.destroy()
