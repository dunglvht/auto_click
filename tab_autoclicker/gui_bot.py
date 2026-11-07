"""The “Bit Heroes bot” window: choose modes and settings, start and stop."""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

from .bitheroes import MODES, RAID_DIFFICULTY, BotSettings

if TYPE_CHECKING:  # pragma: no cover
    from .gui import App

INTRO = ("Plays the ticked modes in this order, using the game's own autopilot for the fighting. "
         "When a mode runs out (energy, shards, xeals, tokens or tickets) it answers “No” to the "
         "offer to buy more and moves on. When every mode is out, it rests and starts again. "
         "It never spends gems.")


class BotWindow(tk.Toplevel):
    def __init__(self, app: "App"):
        super().__init__(app.root)
        self.app = app
        self.title("Bit Heroes bot")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.s = BotSettings.from_dict(app.settings.get("bot") or {})
        pad = ttk.Frame(self, padding=14)
        pad.pack(fill="both", expand=True)
        ttk.Label(pad, text=INTRO, wraplength=440, justify="left").pack(anchor="w")

        box = ttk.LabelFrame(pad, text="Modes", padding=8)
        box.pack(fill="x", pady=(10, 0))
        self.mode_list = ttk.Frame(box)
        self.mode_list.pack(fill="x")
        self.enabled = {m: tk.BooleanVar(value=m in self.s.enabled) for m in MODES}
        self.runs_vars = {m: tk.StringVar(value="") for m in MODES}
        self._draw_modes()

        opt = ttk.LabelFrame(pad, text="Settings", padding=8)
        opt.pack(fill="x", pady=(10, 0))
        ttk.Label(opt, text="Raid difficulty").grid(row=0, column=0, sticky="w")
        self.raid_var = tk.StringVar(value=self.s.raid_difficulty.title())
        ttk.Combobox(opt, textvariable=self.raid_var, state="readonly", width=9,
                     values=[d.title() for d in RAID_DIFFICULTY]).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(opt, text="PvP opponent").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.pvp_var = tk.StringVar(value=str(self.s.pvp_opponent))
        ttk.Spinbox(opt, from_=1, to=4, width=4, textvariable=self.pvp_var).grid(
            row=1, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(opt, text="row in the list (1 = the top, usually the weakest)", style="Hint.TLabel").grid(
            row=1, column=2, sticky="w", pady=(6, 0))
        ttk.Label(opt, text="Quest dungeon at").grid(row=2, column=0, sticky="w", pady=(6, 0))
        qf = ttk.Frame(opt)
        qf.grid(row=2, column=1, columnspan=2, sticky="w", padx=6, pady=(6, 0))
        self.qx_var = tk.StringVar(value=str(self.s.quest_point[0]))
        self.qy_var = tk.StringVar(value=str(self.s.quest_point[1]))
        ttk.Entry(qf, textvariable=self.qx_var, width=5).pack(side="left")
        ttk.Label(qf, text=",").pack(side="left")
        ttk.Entry(qf, textvariable=self.qy_var, width=5).pack(side="left")
        ttk.Label(qf, text="  on the zone map (430, 300 = the middle dungeon)", style="Hint.TLabel").pack(side="left")
        ttk.Label(opt, text="Invasion: fight").grid(row=4, column=0, sticky="w", pady=(6, 0))
        self.inv_var = tk.StringVar(value=str(self.s.invasion_s))
        ttk.Spinbox(opt, from_=5, to=600, width=4, textvariable=self.inv_var).grid(
            row=4, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(opt, text="seconds (enough for the loot), then leave the battle", style="Hint.TLabel").grid(
            row=4, column=2, sticky="w", pady=(6, 0))
        ttk.Label(opt, text="When all are out, rest").grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.rest_var = tk.StringVar(value=str(self.s.rest_min))
        ttk.Spinbox(opt, from_=1, to=600, width=4, textvariable=self.rest_var).grid(
            row=3, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(opt, text="minutes, then start again", style="Hint.TLabel").grid(
            row=3, column=2, sticky="w", pady=(6, 0))
        ttk.Label(opt, text="World Boss: start with").grid(row=5, column=0, sticky="w", pady=(6, 0))
        self.wb_var = tk.StringVar(value=str(self.s.wb_players))
        ttk.Spinbox(opt, from_=1, to=5, width=4, textvariable=self.wb_var).grid(
            row=5, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(opt, text="players (waits in the lobby until they have joined; 1 = alone)",
                  style="Hint.TLabel").grid(row=5, column=2, sticky="w", pady=(6, 0))
        ttk.Label(opt, text="Game speed").grid(row=6, column=0, sticky="w", pady=(6, 0))
        self.speed_var = tk.StringVar(value=str(self.s.battle_speed))
        ttk.Spinbox(opt, from_=1, to=10, width=4, textvariable=self.speed_var).grid(
            row=6, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(opt, text="× (1 = normal; switch off other speed-hack add-ons)", style="Hint.TLabel").grid(
            row=6, column=2, sticky="w", pady=(6, 0))
        self.pick_vars = {}
        for row, (key, label, top, hint) in enumerate((
                ("raid_number", "Raid", 30, "1 = the first raid in the Raid window"),
                ("wb_boss", "World Boss", 30, "1 = the first boss when summoning"),
                ("quest_zone", "Quest zone", 20, "1 = Bit Valley … 20 = Big Claw"),
                ("gauntlet_difficulty", "Gauntlet difficulty", 999999, "near the top of its list, see README"),
                ("trials_difficulty", "Trials difficulty", 999999, "the same, for the weeks with Trials"),
                ("invasion_wave", "Invasion wave", 999, "one of the 5 highest waves")),
                start=7):
            ttk.Label(opt, text=label).grid(row=row, column=0, sticky="w", pady=(6, 0))
            var = self.pick_vars[key] = tk.StringVar(value=str(getattr(self.s, key)))
            ttk.Spinbox(opt, from_=0, to=top, width=7 if top > 999 else 4, textvariable=var).grid(
                row=row, column=1, sticky="w", padx=6, pady=(6, 0))
            ttk.Label(opt, text=hint + "; 0 = as shown", style="Hint.TLabel").grid(
                row=row, column=2, sticky="w", pady=(6, 0))

        bar = ttk.Frame(pad)
        bar.pack(fill="x", pady=(12, 0))
        self.start_btn = ttk.Button(bar, text="▶  Start bot", style="Accent.TButton", command=self.toggle)
        self.start_btn.pack(side="left")
        self.status_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.status_var, wraplength=330).pack(side="left", padx=(10, 0))
        ttk.Label(pad, text="The game page is set to 854 × 545 (the size the bot knows). "
                            "Stop it any time with Stop or F9 in the main window.",
                  style="Hint.TLabel", wraplength=440, justify="left").pack(anchor="w", pady=(10, 0))
        self.update_state()

    # ------------------------------------------------------------------ modes

    def _draw_modes(self) -> None:
        for w in self.mode_list.winfo_children():
            w.destroy()
        for i, m in enumerate(self.s.order):
            ttk.Label(self.mode_list, text=f"{i + 1}.").grid(row=i, column=0, sticky="w")
            ttk.Checkbutton(self.mode_list, text=MODES[m], variable=self.enabled[m], width=17).grid(
                row=i, column=1, sticky="w")
            ttk.Button(self.mode_list, text="▲", width=2, command=lambda i=i: self._move(i, -1)).grid(
                row=i, column=2, padx=(4, 0))
            ttk.Button(self.mode_list, text="▼", width=2, command=lambda i=i: self._move(i, 1)).grid(
                row=i, column=3, padx=(2, 0))
            ttk.Label(self.mode_list, textvariable=self.runs_vars[m], style="Hint.TLabel").grid(
                row=i, column=4, sticky="w", padx=(10, 0))

    def _move(self, i: int, delta: int) -> None:
        j = i + delta
        if 0 <= j < len(self.s.order):
            self.s.order[i], self.s.order[j] = self.s.order[j], self.s.order[i]
            self._draw_modes()

    # ---------------------------------------------------------------- actions

    def _read(self) -> BotSettings:
        s = self.s
        s.enabled = [m for m in s.order if self.enabled[m].get()]
        s.raid_difficulty = self.raid_var.get().lower()
        try:
            s.pvp_opponent = min(4, max(1, int(self.pvp_var.get())))
            s.quest_point = (int(self.qx_var.get()), int(self.qy_var.get()))
            s.rest_min = max(1, int(self.rest_var.get()))
            s.invasion_s = max(5, int(self.inv_var.get()))
            s.wb_players = min(5, max(1, int(self.wb_var.get())))
            s.battle_speed = min(10, max(1, int(self.speed_var.get())))
            limits = {"quest_zone": 20, "raid_number": 30, "wb_boss": 30, "invasion_wave": 999}
            for key, var in self.pick_vars.items():
                setattr(s, key, max(0, min(limits.get(key, 999999), int(var.get().replace(",", "")))))
        except ValueError:
            pass
        self.app.settings["bot"] = s.to_dict()
        return s

    def toggle(self) -> None:
        if self.app.mode == "playing":
            self.app.engine.stop_play()
            return
        settings = self._read()
        shots = os.path.join(os.path.dirname(self.app.settings_path()), "bot-screens")
        self.app.run_bg(lambda: self.app.engine.start_bot(settings, shots))

    def update_state(self) -> None:
        playing = self.app.mode == "playing"
        ready = self.app.connected and self.app.target is not None
        self.start_btn.configure(text="■  Stop bot" if playing else "▶  Start bot",
                                 state="normal" if playing or (ready and self.app.mode == "idle") else "disabled")
        if not playing and not self.status_var.get():
            self.status_var.set("" if ready else "Connect and choose the game tab first.")

    def show(self, info: dict) -> None:
        self.status_var.set(info.get("status", ""))
        for m, n in (info.get("runs") or {}).items():
            if m in self.runs_vars:
                self.runs_vars[m].set(f"{n} run{'s' if n != 1 else ''}" if n else "")

    def close(self) -> None:
        self._read()
        self.app.bot_win = None
        self.destroy()
