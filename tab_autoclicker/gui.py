"""Tkinter GUI for Firefox Tab Auto Clicker."""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Callable, Optional

from . import __version__, firefox
from .engine import CLICK_ACTIONS, PRESET_ACTIONS, Engine, EngineError, PlayOptions, Step, TabInfo, safe_url
from .gui_check import CheckDialog
from .gui_bot import BotPanel
from .gui_flow import FlowWindow

APP_NAME = "Bit Heroes Bot"
CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "tab-autoclicker")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
PRESETS_DIR = os.path.join(CONFIG_DIR, "presets")

COLORS = {"off": "#9a9a9a", "on": "#1f9d55", "busy": "#e08a00", "rec": "#d62839"}
GAME_URL = "https://www.kongregate.com/games/Juppiomenz/bit-heroes"


def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, SETTINGS_FILE)
    except OSError:
        pass


def fmt_ms(ms: float) -> str:
    return f"{ms:.0f} ms" if ms < 1000 else f"{ms / 1000:.2f} s".replace(".00 s", " s")


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.engine = Engine(lambda kind, payload: self.q.put((kind, payload)))
        self.steps: list[Step] = []
        self.settings = load_settings()
        self.binary = firefox.find_firefox()
        self.profiles = firefox.list_profiles(self.binary)
        self.connected = False
        self.mode = "idle"
        self.target: Optional[TabInfo] = None
        self._append_first: Optional[bool] = None  # recording session: first step appended?
        self._waiting_quit = False  # the "please quit Firefox" dialog is open
        self._busy_calls = 0
        self.status_note = ""
        self._last_progress: Optional[tuple] = None
        self.game_url = GAME_URL
        self._preset_name = ""                  # the preset shown in the step list ("" = none)
        self._preset_sig: Optional[str] = None  # what that preset holds, to spot changes
        self._insert_at: Optional[int] = None       # recording inserts here (None = at the end)
        self._pending_preset: Optional[str] = None  # a check asked to go to this preset
        self._jump_id: Optional[str] = None         # scheduled start of that preset
        self.flow: Optional[FlowWindow] = None      # the flow chart window, while open
        self.bot: Optional[BotPanel] = None         # the Bit Heroes bot tab
        self.win_size: Optional[tuple[int, int]] = None  # saved page size (per preset, in play settings)
        self._last_dir = self.settings.get("last_dir") or os.path.expanduser("~")

        root.title(APP_NAME)
        root.minsize(900, 700)
        self._style()
        self._build()
        self._load_ui_settings()
        self._update_states()
        for var in (self.repeat_var, self.forever_var, self.pause_var):  # shown on the flow chart's repeat arrow
            var.trace_add("write", lambda *a: self.flow and self.flow.refresh())
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<Escape>", lambda e: self.stop_everything())
        root.bind("<F8>", lambda e: self.toggle_record())
        root.bind("<F9>", lambda e: self.toggle_play())
        signal.signal(signal.SIGTERM, lambda *a: root.after(0, self.on_close))
        signal.signal(signal.SIGINT, lambda *a: root.after(0, self.on_close))
        root.after(50, self._poll)
        root.after(300, self._auto_connect)

    # ------------------------------------------------------------ appearance

    def _style(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        base = tkfont.nametofont("TkDefaultFont")
        base.configure(size=10)
        self.font_bold = base.copy()
        self.font_bold.configure(weight="bold")
        self.font_small = base.copy()
        self.font_small.configure(size=9)
        self.font_title = base.copy()
        self.font_title.configure(size=11, weight="bold")
        bg = style.lookup("TFrame", "background") or "#dcdad5"
        self.root.configure(background=bg)
        style.configure("Section.TLabelframe.Label", font=self.font_title)
        style.configure("Section.TLabelframe", padding=(10, 6, 10, 8))
        style.configure("Hint.TLabel", foreground="#5f6368", font=self.font_small)
        style.configure("Target.TLabel", font=self.font_bold)
        style.configure("Accent.TButton", font=self.font_bold, foreground="#ffffff", background="#1a5fb4")
        style.map("Accent.TButton", background=[("disabled", "#a9b8cc"), ("active", "#1c71d8")],
                  foreground=[("disabled", "#eef1f5")])
        style.configure("Rec.TButton", font=self.font_bold, foreground="#ffffff", background="#c01c28")
        style.map("Rec.TButton", background=[("disabled", "#d9a8ac"), ("active", "#e01b24")],
                  foreground=[("disabled", "#f7eced")])
        style.configure("Treeview", rowheight=22)
        style.configure("Treeview.Heading", font=self.font_bold)

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        # Firefox connection ---------------------------------------------------
        conn = ttk.LabelFrame(outer, text="Firefox", style="Section.TLabelframe")
        conn.grid(row=0, column=0, sticky="ew")
        conn.columnconfigure(1, weight=1)
        top = ttk.Frame(conn)
        top.grid(row=0, column=0, columnspan=3, sticky="ew")
        top.columnconfigure(1, weight=1)
        self.dot = tk.Canvas(top, width=14, height=14, highlightthickness=0, background=self.root["background"])
        self.dot_id = self.dot.create_oval(2, 2, 12, 12, fill=COLORS["off"], outline="")
        self.dot.grid(row=0, column=0, padx=(0, 6))
        self.status_var = tk.StringVar(value="Not connected")
        ttk.Label(top, textvariable=self.status_var, font=self.font_bold).grid(row=0, column=1, sticky="w")
        ttk.Label(top, text="Port").grid(row=0, column=2, padx=(8, 4))
        self.port_var = tk.StringVar(value="9222")
        self.port_entry = ttk.Spinbox(top, from_=1024, to=65535, width=7, textvariable=self.port_var)
        self.port_entry.grid(row=0, column=3)
        self.connect_btn = ttk.Button(top, text="Connect", command=self.toggle_connect, width=11)
        self.connect_btn.grid(row=0, column=4, padx=(8, 0))

        ttk.Label(conn, text="Profile").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.profile_var = tk.StringVar()
        self.profile_box = ttk.Combobox(conn, textvariable=self.profile_var, state="readonly",
                                        values=[p.label for p in self.profiles])
        self.profile_box.grid(row=1, column=1, sticky="ew", padx=(6, 6), pady=(8, 0))
        self.launch_btn = ttk.Button(conn, text="Launch Firefox", command=self.launch_firefox,
                                     style="Accent.TButton")
        self.launch_btn.grid(row=1, column=2, sticky="e", pady=(8, 0))
        opts = ttk.Frame(conn)
        opts.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.keep_prefs_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Keep my normal Firefox settings", variable=self.keep_prefs_var).pack(side="left")
        self.reopen_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Reopen my tabs", variable=self.reopen_var).pack(side="left", padx=(14, 0))
        self.hide_sidebar_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Hide Firefox's sidebar", variable=self.hide_sidebar_var).pack(
            side="left", padx=(14, 0))
        ttk.Label(conn, style="Hint.TLabel", wraplength=720, justify="left",
                  text="Choose the Firefox profile you play Bit Heroes with. Firefox has to run with remote "
                       "control switched on: if it is already open without it, you are asked to quit it, and "
                       "it then restarts with your tabs.").grid(row=3, column=0, columnspan=3, sticky="w",
                                                                pady=(4, 0))

        # The game -----------------------------------------------------------------
        tgt = ttk.LabelFrame(outer, text="Game", style="Section.TLabelframe")
        tgt.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        tgt.columnconfigure(4, weight=1)
        self.game_btn = ttk.Button(tgt, text="Open game window\u2026", command=self.open_game,
                                   style="Accent.TButton")
        self.game_btn.grid(row=0, column=0, sticky="w")
        ttk.Label(tgt, text="or, if it is already open:", style="Hint.TLabel").grid(row=0, column=1, padx=(12, 6))
        self.pick_btn = ttk.Button(tgt, text="\u25CE  Pick its tab by clicking", command=self.toggle_pick)
        self.pick_btn.grid(row=0, column=2, sticky="w")
        self.list_btn = ttk.Button(tgt, text="Choose from list \u25BE", command=self.choose_from_list)
        self.list_btn.grid(row=0, column=3, padx=(6, 0))
        self.show_btn = ttk.Button(tgt, text="Show tab", command=self.show_tab)
        self.show_btn.grid(row=0, column=5, sticky="e")
        self.target_title = tk.StringVar(value="No game tab yet")
        self.target_url = tk.StringVar(value="Press \u201cOpen game window\u2026\u201d, or pick the tab the game runs in.")
        ttk.Label(tgt, textvariable=self.target_title, style="Target.TLabel").grid(
            row=1, column=0, columnspan=6, sticky="w", pady=(8, 0))
        ttk.Label(tgt, textvariable=self.target_url, style="Hint.TLabel").grid(
            row=2, column=0, columnspan=6, sticky="w")

        # Tabs: the bot, and the general click recorder ------------------------------
        self.tabs = ttk.Notebook(outer)
        self.tabs.grid(row=2, column=0, sticky="nsew", pady=(10, 0))
        self.bot = BotPanel(self.tabs, self)
        self.tabs.add(self.bot, text="  Bit Heroes bot  ")
        recorder = ttk.Frame(self.tabs, padding=(8, 8))
        recorder.columnconfigure(0, weight=1)
        recorder.rowconfigure(1, weight=1)
        self.tabs.add(recorder, text="  Click recorder  ")
        size = ttk.Frame(recorder)
        size.grid(row=0, column=0, sticky="w")
        ttk.Label(size, text="Page size").pack(side="left")
        self.size_now_var = tk.StringVar(value="–")
        ttk.Label(size, textvariable=self.size_now_var, font=self.font_bold).pack(side="left", padx=(6, 0))
        self.size_save_btn = ttk.Button(size, text="Save this size", command=self.save_window_size)
        self.size_save_btn.pack(side="left", padx=(10, 0))
        self.size_saved_var = tk.StringVar(value="saved: none")
        ttk.Label(size, textvariable=self.size_saved_var, style="Hint.TLabel").pack(side="left", padx=(10, 0))
        self.size_resize_btn = ttk.Button(size, text="Resize to it", command=self.resize_window)
        self.size_resize_btn.pack(side="left", padx=(6, 0))
        self.resize_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(size, text="before playing", variable=self.resize_var).pack(side="left", padx=(8, 0))

        # Recording (Click recorder tab) -----------------------------------------
        rec = ttk.LabelFrame(recorder, text="Clicks", style="Section.TLabelframe")
        rec.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        rec.columnconfigure(0, weight=1)
        rec.rowconfigure(2, weight=1)
        pre = ttk.Frame(rec)
        pre.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        ttk.Label(pre, text="Preset").pack(side="left")
        self.preset_var = tk.StringVar()
        self.preset_box = ttk.Combobox(pre, textvariable=self.preset_var, state="readonly", width=26)
        self.preset_box.pack(side="left", padx=(6, 0))
        self.preset_box.bind("<<ComboboxSelected>>", lambda e: self.choose_preset(self.preset_var.get()))
        self.preset_save_btn = ttk.Button(pre, text="Save preset…", command=self.save_preset)
        self.preset_save_btn.pack(side="left", padx=(6, 0))
        self.preset_del_btn = ttk.Button(pre, text="Delete preset", command=self.delete_preset)
        self.preset_del_btn.pack(side="left", padx=(4, 0))
        self.preset_note = tk.StringVar(value="")
        ttk.Label(pre, textvariable=self.preset_note, style="Hint.TLabel").pack(side="left", padx=(10, 0))
        ttk.Button(pre, text="Flow chart…", command=self.open_flow).pack(side="right")
        bar = ttk.Frame(rec)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.rec_btn = ttk.Button(bar, text="●  Record  (F8)", command=self.toggle_record,
                                  style="Rec.TButton", width=16)
        self.rec_btn.pack(side="left")
        self.check_btn = ttk.Button(bar, text="＋ Image check…", command=self.add_check)
        self.check_btn.pack(side="left", padx=(6, 0))
        self.edit_btn = ttk.Button(bar, text="Edit…", command=self.edit_selected)
        self.edit_btn.pack(side="left", padx=(10, 0))
        self.del_btn = ttk.Button(bar, text="Delete", command=self.delete_selected)
        self.del_btn.pack(side="left", padx=(4, 0))
        self.clear_btn = ttk.Button(bar, text="Clear", command=self.clear_steps)
        self.clear_btn.pack(side="left", padx=(4, 0))
        self.save_btn = ttk.Button(bar, text="Save…", command=self.save_steps)
        self.save_btn.pack(side="right")
        self.open_btn = ttk.Button(bar, text="Open…", command=self.open_steps)
        self.open_btn.pack(side="right", padx=(0, 4))
        cols = ("n", "wait", "action", "pos", "element")
        self.tree = ttk.Treeview(rec, columns=cols, show="headings", height=7, selectmode="extended")
        for col, title, width, anchor, stretch in (
                ("n", "#", 40, "e", False), ("wait", "Wait before", 95, "e", False),
                ("action", "Action", 110, "w", False), ("pos", "Position", 110, "w", False),
                ("element", "Details", 220, "w", True)):
            self.tree.heading(col, text=title, anchor=anchor)
            self.tree.column(col, width=width, anchor=anchor, stretch=stretch, minwidth=36)
        self.tree.grid(row=2, column=0, sticky="nsew", pady=(6, 0))
        sb = ttk.Scrollbar(rec, orient="vertical", command=self.tree.yview)
        sb.grid(row=2, column=1, sticky="ns", pady=(6, 0))
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<Double-1>", lambda e: self.edit_selected())
        self.tree.bind("<Delete>", lambda e: self.delete_selected())
        self.tree.bind("<Button-3>", self._row_menu)
        self.tree.bind("<Alt-Up>", lambda e: (self.move_selected(-1), "break")[1])
        self.tree.bind("<Alt-Down>", lambda e: (self.move_selected(1), "break")[1])
        self.tree.bind("<<TreeviewSelect>>", lambda e: (self._update_states(), self.flow and self.flow.sync_selection()))
        self.tree.tag_configure("check", background="#e8f0fb")
        self.tree.tag_configure("branch", background="#f1ebfa", foreground="#5b3f8c")
        self.tree.tag_configure("playing", background="#fff1c2")
        ttk.Label(rec, style="Hint.TLabel", text="Clicks are recorded only inside the target tab. Image checks "
                  "wait for something to show up before going on. Double-click a row to edit it, "
                  "right-click for more.").grid(
            row=3, column=0, sticky="w", pady=(4, 0))

        # Playback (Click recorder tab) ------------------------------------------
        play = ttk.LabelFrame(recorder, text="Play", style="Section.TLabelframe")
        play.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        play.columnconfigure(0, weight=1)
        num = dict(width=7)
        pad = dict(pady=(6, 0))

        row = ttk.Frame(play)
        row.grid(row=0, column=0, sticky="w")
        ttk.Label(row, text="Repeat").pack(side="left")
        self.repeat_var = tk.StringVar(value="1")
        self.repeat_spin = ttk.Spinbox(row, from_=1, to=1_000_000, textvariable=self.repeat_var, **num)
        self.repeat_spin.pack(side="left", padx=4)
        ttk.Label(row, text="times").pack(side="left")
        self.forever_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="Forever (until Stop)", variable=self.forever_var,
                        command=self._update_states).pack(side="left", padx=(16, 0))
        ttk.Label(row, text="sleep between rounds").pack(side="left", padx=(20, 4))
        self.pause_var = tk.StringVar(value="1")  # seconds (stored as ms)
        self.pause_spin = ttk.Spinbox(row, from_=0, to=86_400, increment=0.5, textvariable=self.pause_var, **num)
        self.pause_spin.pack(side="left")
        ttk.Label(row, text="s").pack(side="left", padx=(4, 0))

        timing = ttk.Frame(play)
        timing.grid(row=1, column=0, sticky="w", **pad)
        self.timing_var = tk.StringVar(value="recorded")
        ttk.Radiobutton(timing, text="Recorded timing", variable=self.timing_var, value="recorded",
                        command=self._update_states).grid(row=0, column=0, sticky="w")
        ttk.Label(timing, text="speed \u00d7").grid(row=0, column=1, sticky="e", padx=(12, 4))
        self.speed_var = tk.StringVar(value="1.0")
        self.speed_spin = ttk.Spinbox(timing, from_=0.1, to=20, increment=0.25, textvariable=self.speed_var, **num)
        self.speed_spin.grid(row=0, column=2, sticky="w")
        ttk.Radiobutton(timing, text="Fixed interval", variable=self.timing_var, value="fixed",
                        command=self._update_states).grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(timing, text="every").grid(row=1, column=1, sticky="e", padx=(12, 4), pady=(4, 0))
        self.interval_var = tk.StringVar(value="1000")
        self.interval_spin = ttk.Spinbox(timing, from_=10, to=86_400_000, increment=100,
                                         textvariable=self.interval_var, **num)
        self.interval_spin.grid(row=1, column=2, sticky="w", pady=(4, 0))
        ttk.Label(timing, text="ms").grid(row=1, column=3, sticky="w", padx=(4, 0), pady=(4, 0))

        row = ttk.Frame(play)
        row.grid(row=2, column=0, sticky="w", **pad)
        ttk.Label(row, text="Random extra wait up to").pack(side="left")
        self.jitter_var = tk.StringVar(value="0")
        ttk.Spinbox(row, from_=0, to=600_000, increment=50, textvariable=self.jitter_var, **num).pack(
            side="left", padx=4)
        ttk.Label(row, text="ms before each click").pack(side="left")

        row = ttk.Frame(play)
        row.grid(row=3, column=0, sticky="w", **pad)
        ttk.Label(row, text="Aim at").pack(side="left")
        self.aim_var = tk.StringVar(value="position")
        ttk.Radiobutton(row, text="recorded position", variable=self.aim_var, value="position").pack(
            side="left", padx=(8, 0))
        ttk.Radiobutton(row, text="recorded element (follows it if the page layout changes)",
                        variable=self.aim_var, value="element").pack(side="left", padx=(10, 0))

        row = ttk.Frame(play)
        row.grid(row=4, column=0, sticky="w", pady=(4, 0))
        self.scroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="Restore page scroll position", variable=self.scroll_var).pack(side="left")
        self.markers_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="Show click markers in the page", variable=self.markers_var).pack(
            side="left", padx=(14, 0))

        row = ttk.Frame(play)
        row.grid(row=5, column=0, sticky="w", pady=(4, 0))
        self.game_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="Game mode", variable=self.game_var).pack(side="left")
        ttk.Label(row, style="Hint.TLabel", text="for browser games such as Bit Heroes: keeps the game taking "
                  "clicks while its window is covered or not focused").pack(side="left", padx=(6, 0))

        pb = ttk.Frame(play)
        pb.grid(row=6, column=0, sticky="ew", pady=(10, 0))
        self.play_btn = ttk.Button(pb, text="\u25B6  Play  (F9)", command=self.toggle_play,
                                   style="Accent.TButton", width=16)
        self.play_btn.pack(side="left")
        self.progress_var = tk.StringVar(value="")
        ttk.Label(pb, textvariable=self.progress_var).pack(side="left", padx=(12, 0))
        ttk.Label(pb, text="Esc stops everything", style="Hint.TLabel").pack(side="right")

        # Log -----------------------------------------------------------------------
        logf = ttk.Frame(outer)
        logf.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        logf.columnconfigure(0, weight=1)
        self.log_text = tk.Text(logf, height=5, wrap="word", font=self.font_small, relief="flat",
                                background="#f6f5f4", foreground="#333333", padx=6, pady=4,
                                highlightthickness=1, highlightbackground="#c0bdb8")
        self.log_text.grid(row=0, column=0, sticky="ew")
        lsb = ttk.Scrollbar(logf, orient="vertical", command=self.log_text.yview)
        lsb.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=lsb.set, state="disabled")

    # ---------------------------------------------------------------- settings

    def _load_ui_settings(self) -> None:
        s = self.settings
        self.port_var.set(str(s.get("port", 9222)))
        wanted = s.get("profile_path")
        index = next((i for i, p in enumerate(self.profiles) if p.path == wanted), 0)
        if self.profiles:
            self.profile_box.current(index)
        self.keep_prefs_var.set(bool(s.get("keep_prefs", True)))
        self.reopen_var.set(bool(s.get("reopen_tabs", True)))
        self.hide_sidebar_var.set(bool(s.get("hide_sidebar", True)))
        self._apply_play_settings(s.get("play", {}))
        self.game_url = s.get("game_url") or GAME_URL
        if s.get("geometry"):
            try:
                self.root.geometry(s["geometry"])
            except tk.TclError:
                pass
        for d in s.get("last_steps", []):
            try:
                self.steps.append(Step.from_dict(d))
            except (TypeError, ValueError):
                pass
        self._refresh_presets()
        name = s.get("preset") or ""
        data = self._read_preset(name) if name else None
        if data is not None:
            self.preset_var.set(name)
            self._preset_name = name
            self._preset_sig = self._signature(data.get("steps", []), data.get("play", {}))
        self._refresh_tree()

    def _save_ui_settings(self) -> None:
        prof = self._profile()
        self.settings.update({
            "port": self._int(self.port_var, 9222),
            "profile_path": prof.path if prof else None,
            "keep_prefs": self.keep_prefs_var.get(),
            "reopen_tabs": self.reopen_var.get(),
            "hide_sidebar": self.hide_sidebar_var.get(),
            "geometry": self.root.geometry(),
            "last_dir": self._last_dir,
            "last_steps": [s.to_dict() for s in self.steps],
            "play": self._play_settings(),
            "preset": self._preset_name,
            "game_url": self.game_url,
        })
        save_settings(self.settings)

    def _play_settings(self) -> dict:
        return {
            "repeat": self._int(self.repeat_var, 1), "forever": self.forever_var.get(),
            "timing": self.timing_var.get(), "speed": self._float(self.speed_var, 1.0),
            "pause": self._pause_ms(), "interval": self._int(self.interval_var, 1000),
            "jitter": self._int(self.jitter_var, 0), "aim": self.aim_var.get(),
            "restore_scroll": self.scroll_var.get(), "markers": self.markers_var.get(),
            "game_mode": self.game_var.get(),
            "window_size": list(self.win_size) if self.win_size else None,
            "resize": self.resize_var.get(),
        }

    def _apply_play_settings(self, p: dict) -> None:
        self.repeat_var.set(str(p.get("repeat", 1)))
        self.forever_var.set(bool(p.get("forever", False)))
        self.timing_var.set(p.get("timing", "recorded"))
        self.speed_var.set(str(p.get("speed", 1.0)))
        self.pause_var.set(f"{max(0, float(p.get('pause', 1000) or 0)) / 1000:g}")
        self.interval_var.set(str(p.get("interval", 1000)))
        self.jitter_var.set(str(p.get("jitter", 0)))
        self.aim_var.set(p.get("aim", "position"))
        self.scroll_var.set(bool(p.get("restore_scroll", True)))
        self.markers_var.set(bool(p.get("markers", True)))
        self.game_var.set(bool(p.get("game_mode", False)))
        ws = p.get("window_size")
        self.win_size = (int(ws[0]), int(ws[1])) if isinstance(ws, (list, tuple)) and len(ws) == 2 else None
        self.resize_var.set(bool(p.get("resize", False)) and self.win_size is not None)
        self.size_saved_var.set(f"saved: {self.win_size[0]} × {self.win_size[1]}" if self.win_size else "saved: none")

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _int(var: tk.StringVar, default: int, lo: int = 0) -> int:
        try:
            return max(lo, int(float(var.get())))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _float(var: tk.StringVar, default: float) -> float:
        try:
            v = float(var.get())
            return v if v > 0 else default
        except (TypeError, ValueError):
            return default

    def _pause_ms(self) -> int:
        """“Sleep between rounds” is shown in seconds but kept in milliseconds."""
        try:
            return max(0, int(round(float(self.pause_var.get()) * 1000)))
        except (TypeError, ValueError):
            return 1000

    def _profile(self) -> Optional[firefox.Profile]:
        i = self.profile_box.current()
        return self.profiles[i] if 0 <= i < len(self.profiles) else None

    def _port(self) -> int:
        return self._int(self.port_var, 9222, lo=1)

    def log(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{stamp}  {text}\n")
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 500:
            self.log_text.delete("1.0", f"{lines - 500}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def run_bg(self, fn: Callable[[], Any], on_ok: Optional[Callable[[Any], None]] = None,
               on_err: Optional[Callable[[Exception], None]] = None) -> None:
        """Run ``fn`` in a worker thread; callbacks run on the Tk thread."""
        self._busy_calls += 1
        self._update_states()

        def work():
            try:
                result = fn()
            except Exception as exc:  # noqa: BLE001
                self.q.put(("_done", (on_err or self._show_error, exc)))
            else:
                self.q.put(("_done", (on_ok, result)))
        threading.Thread(target=work, daemon=True).start()

    def _center(self, win: tk.Toplevel) -> None:
        """Place a dialog over the main window (not wherever the WM likes)."""
        win.update_idletasks()
        rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
        rw, rh = self.root.winfo_width(), self.root.winfo_height()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        win.geometry(f"+{rx + max(0, (rw - w) // 2)}+{ry + max(0, (rh - h) // 3)}")

    def ask_choice(self, title: str, message: str, choices: list[tuple[str, str]]) -> Optional[str]:
        """Modal dialog with one button per (key, label); returns the key or None."""
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.transient(self.root)
        dlg.resizable(False, False)
        frame = ttk.Frame(dlg, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=message, wraplength=400, justify="left").pack(anchor="w")
        bar = ttk.Frame(frame)
        bar.pack(anchor="e", pady=(14, 0))
        result: dict = {"v": None}
        buttons = []
        for i, (key, label) in enumerate(choices):
            def choose(k=key):
                result["v"] = k
                dlg.destroy()
            b = ttk.Button(bar, text=label, command=choose, style="Accent.TButton" if i == 0 else "TButton")
            b.pack(side="left", padx=(0 if i == 0 else 6, 0))
            buttons.append(b)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.bind("<Return>", lambda e: buttons[0].invoke())
        self._center(dlg)
        try:
            dlg.wait_visibility()
            dlg.grab_set()
        except tk.TclError:
            pass
        buttons[0].focus_set()
        self.root.wait_window(dlg)
        return result["v"]

    def _connect_failed(self, exc: Exception, quiet: bool = False) -> None:
        """Explain a failed connection; offer to restart a stuck Firefox."""
        if isinstance(exc, EngineError) and exc.code == "session_busy":
            port = self._port()
            prof = firefox.profile_serving_port(self.profiles, port)
            self.log(str(exc).splitlines()[0])
            if prof is None:
                self._show_error(exc)
                return
            answer = self.ask_choice(
                "Firefox is busy",
                "Firefox is still held by an earlier remote-control session (for example from an "
                "Auto Clicker that crashed). Firefox only allows one, and it is released only when "
                "Firefox restarts.\n\nRestart Firefox now? Your tabs are reopened.",
                [("restart", "Restart Firefox"), ("cancel", "Not now")])
            if answer == "restart":
                self._wait_for_firefox_exit(prof, port, reason="Firefox has to restart to clear the old session.")
            return
        if quiet:
            self.log(str(exc).splitlines()[0])
        else:
            self._show_error(exc)

    def _show_error(self, exc: Exception) -> None:
        msg = str(exc) if isinstance(exc, EngineError) else f"{type(exc).__name__}: {exc}"
        self.log(msg.splitlines()[0])
        messagebox.showerror(APP_NAME, msg, parent=self.root)

    # ------------------------------------------------------------ event pump

    def _poll(self) -> None:
        progress = None
        try:
            for _ in range(500):
                kind, payload = self.q.get_nowait()
                if kind == "progress":
                    progress = payload  # only the latest matters
                else:
                    self._handle(kind, payload)
        except queue.Empty:
            pass
        if progress:
            self._show_progress(progress)
        self.root.after(50, self._poll)

    def _handle(self, kind: str, payload: Any) -> None:
        if kind == "_done":
            callback, value = payload
            self._busy_calls = max(0, self._busy_calls - 1)
            if callback:
                callback(value)
        elif kind == "log":
            self.log(payload)
        elif kind == "connected":
            self.connected = True
            self.status_var.set(f"Connected to {payload}")
            self.log(f"Connected to {payload} on port {self._port()}.")
        elif kind == "disconnected":
            self.connected = False
            self.mode = "idle"
            self.status_var.set("Not connected")
            self.log(payload)
            self._set_target(None)
        elif kind == "mode":
            if self.mode == "playing" and payload != "playing" and self.progress_var.get():
                self.progress_var.set("Last run: " + self.progress_var.get())
            self.mode = payload
            if payload == "recording":
                self._append_first = bool(self.steps)
            else:
                self._insert_at = None
            if payload != "playing":
                for item in self.tree.tag_has("playing"):
                    self._untag_playing(item)
                if self.flow:
                    self.flow.set_current(None)
            if payload == "idle" and self._pending_preset is not None:
                name, self._pending_preset = self._pending_preset, None
                self._jump_id = self.root.after(300, lambda: self._jump_to_preset(name))
        elif kind == "goto_preset":
            self._pending_preset = payload
        elif kind == "target":
            self._set_target(payload)
            if payload is not None:
                self._update_size_now()
            else:
                self.size_now_var.set("–")
        elif kind == "step":
            self._add_step(payload)
        elif kind == "bot":
            if self.bot:
                self.bot.show(payload)
        elif kind == "status":
            self.status_note = payload
            if self.mode == "playing" and self._last_progress:
                self._show_progress(self._last_progress)
            elif payload:
                self.progress_var.set(payload)
        self._update_states()

    def _set_target(self, tab: Optional[TabInfo]) -> None:
        self.target = tab
        if tab is None:
            self.target_title.set("No game tab yet")
            self.target_url.set("Press “Open game window…”, or pick the tab the game runs in.")
        else:
            self.target_title.set(tab.title or "(untitled page)")
            self.target_url.set(tab.short_url)

    def _show_progress(self, p: tuple) -> None:
        self._last_progress = p
        loop, total, i, n, clicks = p
        rep = f"repeat {loop}" + (f" of {total}" if total else " (forever)")
        text = f"{rep}  ·  step {i} of {n}  ·  {clicks} click{'s' if clicks != 1 else ''}"
        if self.mode == "playing":
            self.progress_var.set(text + (f"  ·  {self.status_note}" if self.status_note else ""))
        else:
            self.progress_var.set("Last run: " + text)
        items = self.tree.get_children()
        for item in self.tree.tag_has("playing"):
            self._untag_playing(item)
        if self.mode == "playing" and 0 < i <= len(items):  # a late update must not re-mark a row
            self.tree.item(items[i - 1], tags=("playing",))
            self.tree.see(items[i - 1])
            if self.flow:
                self.flow.set_current(i - 1)

    def _untag_playing(self, item: str) -> None:
        self.tree.item(item, tags=self._row_tags(self.tree.index(item)))

    def _update_states(self) -> None:
        if self.bot:
            self.bot.update_state()
        idle = self.mode == "idle"
        busy = self._busy_calls > 0
        on = self.connected

        def state(widget, enabled):
            widget.configure(state="normal" if enabled else "disabled")
        color = COLORS["off"] if not on else COLORS["rec"] if self.mode == "recording" else \
            COLORS["busy"] if self.mode in ("playing", "picking") else COLORS["on"]
        self.dot.itemconfigure(self.dot_id, fill=color)
        self.connect_btn.configure(text="Disconnect" if on else "Connect")
        state(self.connect_btn, not busy or on)
        state(self.launch_btn, not on and not busy and not self._waiting_quit and self.binary is not None)
        self.profile_box.configure(state="disabled" if on else "readonly")
        state(self.port_entry, not on)
        self.pick_btn.configure(text="✖  Cancel picking" if self.mode == "picking"
                                else "\u25CE  Pick its tab by clicking")
        state(self.pick_btn, on and (idle or self.mode == "picking"))
        state(self.list_btn, on and idle)
        state(self.show_btn, on and self.target is not None)
        state(self.game_btn, on and idle)
        state(self.size_save_btn, on and self.target is not None)
        state(self.size_resize_btn, on and idle and self.target is not None and self.win_size is not None)
        recording = self.mode == "recording"
        self.rec_btn.configure(text="■  Stop recording" if recording else "●  Record  (F8)")
        state(self.rec_btn, on and self.target is not None and (idle or recording))
        state(self.check_btn, on and self.target is not None and (idle or recording))
        selected = bool(self.tree.selection())
        for w in (self.edit_btn, self.del_btn):
            state(w, idle and selected)
        state(self.clear_btn, idle and bool(self.steps))
        state(self.save_btn, bool(self.steps))
        state(self.open_btn, idle)
        self.preset_box.configure(state="readonly" if idle else "disabled")
        state(self.preset_save_btn, idle and bool(self.steps))
        state(self.preset_del_btn, idle and bool(self._preset_name))
        if idle:
            self.preset_note.set("unsaved changes" if self._preset_changed() else "")
        playing = self.mode == "playing"
        self.play_btn.configure(text="■  Stop  (F9)" if playing else "▶  Play  (F9)")
        state(self.play_btn, playing or (on and idle and self.target is not None and bool(self.steps)))
        state(self.repeat_spin, not self.forever_var.get())
        fixed = self.timing_var.get() == "fixed"
        state(self.interval_spin, fixed)
        state(self.speed_spin, not fixed)

    # -------------------------------------------------------------- connection

    def _auto_connect(self) -> None:
        port = self._port()
        if firefox.port_open(port):
            self.run_bg(lambda: self.engine.connect(port), on_err=lambda e: self._connect_failed(e, quiet=True))
        else:
            self.log(f"Firefox is not running with remote control on port {port}. "
                     "Click “Launch Firefox” to start it.")

    def toggle_connect(self) -> None:
        if self.connected:
            self.run_bg(self.engine.disconnect)
        else:
            port = self._port()
            self.run_bg(lambda: self.engine.connect(port), on_err=self._connect_failed)

    def launch_firefox(self) -> None:
        prof = self._profile()
        port = self._port()
        if not self.binary:
            messagebox.showerror(APP_NAME, "Firefox was not found on this computer.", parent=self.root)
            return
        if prof is None:
            return
        if firefox.port_open(port):
            self.log(f"Something is already listening on port {port}; connecting to it.")
            self.run_bg(lambda: self.engine.connect(port), on_err=self._connect_failed)
            return
        pid = firefox.profile_owner_pid(prof.path)
        if pid is None:
            self._start_firefox(prof, port)
            return
        self._wait_for_firefox_exit(prof, port)

    def _wait_for_firefox_exit(self, prof: firefox.Profile, port: int, reason: str = "") -> None:
        dlg = tk.Toplevel(self.root)
        dlg.title("Restart Firefox")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        frame = ttk.Frame(dlg, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Please quit Firefox", font=self.font_title).pack(anchor="w")
        ttk.Label(frame, wraplength=400, justify="left", text=(
            (reason or "Firefox is running with this profile, but without remote control.") + "\n\n"
            "Quit Firefox completely: menu ☰ → Quit, or press Ctrl+Q in a Firefox window. "
            "As soon as it has closed, Auto Clicker starts it again with remote control"
            + (" and reopens your tabs." if self.reopen_var.get() else "."))).pack(anchor="w", pady=(8, 12))
        bar = ttk.Progressbar(frame, mode="indeterminate", length=400)
        bar.pack(fill="x")
        bar.start(12)
        cancelled = {"v": False}
        self._waiting_quit = True
        self._update_states()

        def cancel():
            cancelled["v"] = True
            self._waiting_quit = False
            dlg.destroy()
            self._update_states()
            self.log("Launch cancelled.")
        ttk.Button(frame, text="Cancel", command=cancel).pack(anchor="e", pady=(12, 0))
        dlg.protocol("WM_DELETE_WINDOW", cancel)
        self._center(dlg)
        self.log("Waiting for Firefox to quit…")

        def check():
            if cancelled["v"]:
                return
            if firefox.profile_owner_pid(prof.path) is None:
                self._waiting_quit = False
                dlg.destroy()
                # Give Firefox a moment to finish writing its session files.
                self.root.after(1200, lambda: self._start_firefox(prof, port))
            else:
                self.root.after(500, check)
        self.root.after(500, check)

    def _start_firefox(self, prof: firefox.Profile, port: int) -> None:
        if firefox.profile_owner_pid(prof.path) is not None:
            self.log("Firefox is still running with this profile.")
            return
        try:
            if self.keep_prefs_var.get():
                if firefox.set_user_pref(prof.path, "remote.prefs.recommended", False):
                    self.log("Set remote.prefs.recommended = false in the profile's user.js, so remote "
                             "control does not switch off Safe Browsing, password saving and similar settings.")
            if firefox.hide_sidebar(prof.path, self.hide_sidebar_var.get()):
                self.log("Firefox's sidebar (the tool icons on the left) is now hidden; the sidebar button "
                         "in the toolbar still opens its tools." if self.hide_sidebar_var.get() else
                         "Firefox's sidebar is no longer hidden by Auto Clicker (change it in Firefox: "
                         "right-click the sidebar button → Customize sidebar).")
            if self.reopen_var.get() and firefox.request_session_restore(prof.path):
                self.log("Firefox will reopen the tabs from your last session.")
            proc = firefox.launch(self.binary, prof.path, port)
        except OSError as exc:
            self._show_error(EngineError(f"Could not start Firefox: {exc}"))
            return
        self.log(f"Starting Firefox ({prof.label}) with remote control on port {port}…")

        def wait_and_connect():
            if not firefox.wait_for_port(port, 60, proc):
                raise EngineError(
                    f"Firefox started, but remote control did not come up on port {port}.\n\n"
                    "If another Firefox window opened instead, Firefox was probably still running. "
                    "Quit it completely and try again.")
            time.sleep(0.5)
            return self.engine.connect(port)
        self.run_bg(wait_and_connect, on_err=self._connect_failed)

    # --------------------------------------------------------------------- tabs

    def toggle_pick(self) -> None:
        if self.mode == "picking":
            self.run_bg(self.engine.cancel_pick)
        else:
            self.run_bg(self.engine.start_pick)

    def choose_from_list(self) -> None:
        def show(tabs: list[TabInfo]) -> None:
            menu = tk.Menu(self.root, tearoff=False)
            for tab in tabs:
                label = tab.label if len(tab.label) < 90 else tab.label[:88] + "…"
                menu.add_command(label=label, command=lambda t=tab: self.run_bg(
                    lambda: self.engine.set_target(t.context),
                    on_ok=lambda info: self.log(f"Target tab: {info.label}")))
            if not tabs:
                menu.add_command(label="(no tabs)", state="disabled")
            x = self.list_btn.winfo_rootx()
            y = self.list_btn.winfo_rooty() + self.list_btn.winfo_height()
            menu.tk_popup(x, y)
        self.run_bg(self.engine.list_tabs, on_ok=show)

    def _update_size_now(self) -> None:
        def show(info: dict) -> None:
            self.size_now_var.set("{} × {}".format(*info["page"]))
        self.run_bg(self.engine.window_size, on_ok=show, on_err=lambda e: self.size_now_var.set("?"))

    def save_window_size(self) -> None:
        """Remember the target tab's current page size (the area clicks and checks use)."""
        def saved(info: dict) -> None:
            vw, vh = info["page"]
            w, h = info["window"]
            self.win_size = (vw, vh)
            self.size_now_var.set(f"{vw} × {vh}")
            self.size_saved_var.set(f"saved: {vw} × {vh}")
            self.resize_var.set(True)
            self.log(f"Saved the page size {vw} × {vh} (the window is {w} × {h}). "
                     "Playback resizes the window to it first; untick “before playing” to stop that.")
            self._update_states()
        self.run_bg(self.engine.window_size, on_ok=saved)

    def resize_window(self) -> None:
        if not self.win_size:
            return
        w, h = self.win_size

        def done(size) -> None:
            self.size_now_var.set(f"{size[0]} × {size[1]}")
            self.log(f"Resized the window: the page is now {size[0]} × {size[1]}.")
        self.run_bg(lambda: self.engine.resize_page(w, h), on_ok=done,
                    on_err=lambda e: (self._show_error(e), self._update_size_now()))

    def open_bot(self) -> None:
        self.tabs.select(0)

    def _bot_tab_shown(self) -> bool:
        return self.tabs.index(self.tabs.select()) == 0

    @staticmethod
    def settings_path() -> str:
        return SETTINGS_FILE

    def open_flow(self) -> None:
        if self.flow:
            self.flow.lift()
        else:
            self.flow = FlowWindow(self)

    def show_tab(self) -> None:
        self.run_bg(self.engine.show_target)

    def open_game(self) -> None:
        url = simpledialog.askstring(
            "Open game in new window",
            "Web address of the game page:\n\n"
            "It opens in a window of its own, so the game keeps running while you use\n"
            "other tabs. Other windows may cover it, but do not minimize it.",
            initialvalue=self.game_url, parent=self.root)
        if not url or not url.strip():
            return
        url = url.strip()
        if "://" not in url:
            url = "https://" + url
        # Direct game links can carry a login token; do not keep those in the settings file.
        if "token" not in url.lower():
            self.game_url = url

        def opened(tab: TabInfo) -> None:
            self.game_var.set(True)
            self.log("Opened the game in a new window and made it the target tab. Game mode is on.")
        self.run_bg(lambda: self.engine.open_in_window(url), on_ok=opened)

    # ------------------------------------------------------------------- steps

    def toggle_record(self, after: Optional[int] = None) -> None:
        """Record clicks: at the end, or inserted after step ``after`` (0-based)."""
        if self.mode == "recording":
            self.run_bg(self.engine.stop_recording)
            return
        if not (self.connected and self.mode == "idle" and self.target):
            return
        self._insert_at = None
        if after is not None:
            self._insert_at = after + 1
        elif self.steps:
            sel = self._selected_indexes()
            choices = [("append", "Add at the end"), ("new", "Start over"), ("cancel", "Cancel")]
            text = f"You already have {len(self.steps)} step(s). Add the new clicks at the end, or start over?"
            if sel and sel[-1] < len(self.steps) - 1:
                choices.insert(0, ("insert", f"Insert after #{sel[-1] + 1}"))
                text = (f"You already have {len(self.steps)} step(s). Insert the new clicks after the selected "
                        f"step #{sel[-1] + 1}, add them at the end, or start over?")
            answer = self.ask_choice("Record", text, choices)
            if answer in (None, "cancel"):
                return
            if answer == "new":
                self.steps.clear()
                self._refresh_tree()
            elif answer == "insert":
                self._insert_at = sel[-1] + 1
        if self._insert_at is not None:
            self.log(f"New clicks will be inserted after step #{self._insert_at}.")
        self.run_bg(self.engine.start_recording)

    def _add_step(self, step: Step) -> None:
        if self._append_first:
            step.delay_ms = max(step.delay_ms, 1000)  # a gap after the previous recording
            self._append_first = False
        if self._insert_at is not None and self._insert_at < len(self.steps):
            at = self._insert_at
            self._insert_at += 1
            self.steps.insert(at, step)
            self._refresh_tree()  # the rows after it are renumbered
            item = self.tree.get_children()[at]
            self.tree.selection_set([item])
            self.tree.see(item)
        else:
            self.steps.append(step)
            self._insert_row(len(self.steps) - 1, step)
            if self.flow:
                self.flow.refresh()
            items = self.tree.get_children()
            if items:
                self.tree.see(items[-1])
        self._update_states()

    def _row(self, i: int, s: Step) -> tuple:
        if s.is_check:
            box = s.box or [s.x, s.y, 0, 0]
            appear = s.wait_for != "disappear"
            limit = f"up to {fmt_ms(s.timeout_ms)}" if s.timeout_ms > 0 else "look once"
            found = s.describe(s.on_found, self.steps)
            if s.click_x is not None and s.on_found in CLICK_ACTIONS:
                found = found.replace("click it", "click the chosen point", 1)
            if s.click_delay_ms and s.on_found in CLICK_ACTIONS:
                found = found.replace("click", f"wait {fmt_ms(s.click_delay_ms)}, click", 1)
            if s.after_ms and s.on_found != "stop":
                found += f" (wait {fmt_ms(s.after_ms)} after)"
            details = (f"“{s.name}” · {limit} · {'found' if appear else 'gone'} → {found}"
                       f" · else → {s.describe(s.on_missing, self.steps)}")
            action = ("↪ " if s.only_on_jump else "") + s.action
            return (i + 1, fmt_ms(s.delay_ms), action, f"{box[0]:.0f}, {box[1]:.0f}  {box[2]:.0f}×{box[3]:.0f}",
                    details)
        action = ("↪ " if s.only_on_jump else "") + s.action + (f" ({fmt_ms(s.hold_ms)})" if s.hold_ms >= 700 else "")
        pos = f"{s.x:.0f}, {s.y:.0f}" + ("  frame" if s.in_frame else "")
        details = s.label or ""
        if s.only_on_jump:
            details = "only when a check jumps here" + (f" · {details}" if details else "")
        return (i + 1, fmt_ms(s.delay_ms), action, pos, details)

    def _row_tags(self, i: int) -> tuple:
        if i >= len(self.steps):
            return ()
        s = self.steps[i]
        return ("branch",) if s.only_on_jump else ("check",) if s.is_check else ()

    def _insert_row(self, i: int, s: Step) -> None:
        self.tree.insert("", "end", values=self._row(i, s), tags=self._row_tags(i))

    def _refresh_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for i, s in enumerate(self.steps):
            self._insert_row(i, s)
        if self.flow:
            self.flow.refresh()
        self._update_states()

    def _selected_indexes(self) -> list[int]:
        items = self.tree.get_children()
        return sorted(items.index(it) for it in self.tree.selection())

    def edit_selected(self) -> None:
        if self.mode != "idle":
            return
        idx = self._selected_indexes()
        if not idx:
            return
        first = self.steps[idx[0]]
        if len(idx) == 1 and first.is_check:
            dlg = CheckDialog(self, first)
            step = dlg.show()
            if step is not None:
                self.steps[idx[0]] = step
                self._refresh_tree()
                self.tree.selection_set([self.tree.get_children()[idx[0]]])
            return
        label = f"step #{idx[0] + 1}" if len(idx) == 1 else f"the {len(idx)} selected steps"
        value = simpledialog.askinteger(APP_NAME, f"Wait before {label} (milliseconds):",
                                        initialvalue=first.delay_ms, minvalue=0, maxvalue=86_400_000,
                                        parent=self.root)
        if value is None:
            return
        for i in idx:
            self.steps[i].delay_ms = value
        self._refresh_tree()
        items = self.tree.get_children()
        self.tree.selection_set([items[i] for i in idx])

    def add_check(self, after: Optional[int] = None) -> None:
        """New image check: after step ``after`` (0-based), after the selected row, or
        at the end (while recording: where the recorded clicks go)."""
        if not (self.connected and self.target and self.mode in ("idle", "recording")):
            return
        recording = self.mode == "recording"
        n = sum(1 for s in self.steps if s.is_check) + 1
        dlg = CheckDialog(self, default_name=f"image {n}")
        step = dlg.show()
        if recording:
            self.engine.recording_paused()  # the next click's wait starts now
        if step is None:
            return
        if recording:
            at = self._insert_at if self._insert_at is not None else len(self.steps)
            if self._insert_at is not None:
                self._insert_at += 1
        elif after is not None:
            at = after + 1
        else:
            idx = self._selected_indexes()
            at = idx[-1] + 1 if idx else len(self.steps)
        at = min(at, len(self.steps))
        self.steps.insert(at, step)
        self._refresh_tree()
        item = self.tree.get_children()[at]
        self.tree.selection_set([item])
        self.tree.see(item)
        self.log(f"Added image check #{at + 1} “{step.name}”.")

    SIMPLE_ACTIONS = ("click", "continue", "skip", "restart", "stop")

    def check_defaults(self) -> dict:
        """Settings a new image check starts with: those of the last new check.
        The first time: look once, click it if found, otherwise go on."""
        d = self.settings.get("check_defaults") or {}
        found, missing = d.get("on_found", "click"), d.get("on_missing", "continue")
        return {"wait_for": d.get("wait_for", "appear") if d.get("wait_for") in ("appear", "disappear") else "appear",
                "timeout_ms": max(0, int(d.get("timeout_ms", 0) or 0)),
                "threshold": int(d.get("threshold") or Step.__dataclass_fields__["threshold"].default),
                "on_found": found if found in self.SIMPLE_ACTIONS else "click",
                "on_missing": missing if missing in self.SIMPLE_ACTIONS and missing != "click" else "continue",
                "click_delay_ms": max(0, int(d.get("click_delay_ms", 0) or 0)),
                "after_ms": max(0, int(d.get("after_ms", 0) or 0)),
                "retry_clicks": max(0, int(d.get("retry_clicks", 0) or 0)),
                "retry_ms": max(100, int(d.get("retry_ms", 1000) or 1000)),
                "tone": bool(d.get("tone", False))}

    def remember_check_defaults(self, st: Step) -> None:
        # Jumps to steps or presets are specific to one check, so they are not carried over.
        self.settings["check_defaults"] = {
            "wait_for": st.wait_for, "timeout_ms": st.timeout_ms, "threshold": st.threshold,
            "on_found": st.on_found if st.on_found in self.SIMPLE_ACTIONS else "click",
            "on_missing": st.on_missing if st.on_missing in self.SIMPLE_ACTIONS else "continue",
            "click_delay_ms": st.click_delay_ms, "after_ms": st.after_ms,
            "retry_clicks": st.retry_clicks, "retry_ms": st.retry_ms, "tone": st.tone}

    def _row_menu(self, event) -> None:
        item = self.tree.identify_row(event.y)
        if not item or self.mode != "idle":
            return
        if item not in self.tree.selection():
            self.tree.selection_set([item])
        self._step_menu(self._selected_indexes(), event.x_root, event.y_root)

    def _step_menu(self, idx: list[int], x_root: int, y_root: int) -> None:
        """Right-click menu for steps (used by the table and the flow chart)."""
        chosen = [self.steps[i] for i in idx]
        only = tk.BooleanVar(value=all(st.only_on_jump for st in chosen))

        def toggle() -> None:
            for st in chosen:
                st.only_on_jump = only.get()
            self._refresh_tree()
            self.tree.selection_set([self.tree.get_children()[i] for i in idx])
        ready = self.connected and self.target is not None
        last = idx[-1]
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label="Edit…", command=self.edit_selected)
        menu.add_checkbutton(label="Run only when a check jumps here", variable=only, command=toggle)
        menu.add_separator()
        menu.add_command(label="Add image check after this…", state="normal" if ready else "disabled",
                         command=lambda: self.add_check(after=last))
        menu.add_command(label="Record clicks after this…", state="normal" if ready else "disabled",
                         command=lambda: self.toggle_record(after=last))
        menu.add_separator()
        menu.add_command(label="Move up  (Alt+↑)", state="normal" if idx[0] > 0 else "disabled",
                         command=lambda: self.move_selected(-1))
        menu.add_command(label="Move down  (Alt+↓)", state="normal" if last < len(self.steps) - 1 else "disabled",
                         command=lambda: self.move_selected(1))
        menu.add_separator()
        menu.add_command(label="Delete", command=self.delete_selected)
        menu.tk_popup(x_root, y_root)

    def move_selected(self, delta: int) -> None:
        """Move the selected steps one place up (-1) or down (+1)."""
        if self.mode != "idle":
            return
        idx = self._selected_indexes()
        if not idx or (delta < 0 and idx[0] == 0) or (delta > 0 and idx[-1] == len(self.steps) - 1):
            return
        chosen = set(idx)
        for i in (idx if delta < 0 else reversed(idx)):
            j = i + delta
            self.steps[i], self.steps[j] = self.steps[j], self.steps[i]
        self._refresh_tree()
        items = self.tree.get_children()
        self.tree.selection_set([items[i + delta] for i in sorted(chosen)])
        self.tree.see(items[idx[0] + delta])

    def delete_selected(self) -> None:
        if self.mode != "idle":
            return
        idx = self._selected_indexes()
        for i in reversed(idx):
            del self.steps[i]
        self._refresh_tree()

    def clear_steps(self) -> None:
        if self.steps and messagebox.askyesno(APP_NAME, f"Remove all {len(self.steps)} steps?",
                                              parent=self.root):
            self.steps.clear()
            self._refresh_tree()

    def save_steps(self) -> None:
        path = filedialog.asksaveasfilename(parent=self.root, title="Save clicks", defaultextension=".json",
                                            initialdir=self._last_dir, initialfile="clicks.json",
                                            filetypes=[("Auto Clicker recording", "*.json"), ("All files", "*")])
        if not path:
            return
        self._last_dir = os.path.dirname(path)
        data = self._recording_data()
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
        except OSError as exc:
            self._show_error(EngineError(f"Could not save: {exc}"))
            return
        self.log(f"Saved {len(self.steps)} clicks to {path}")

    def open_steps(self) -> None:
        path = filedialog.askopenfilename(parent=self.root, title="Open clicks", initialdir=self._last_dir,
                                          filetypes=[("Auto Clicker recording", "*.json"), ("All files", "*")])
        if not path:
            return
        self._last_dir = os.path.dirname(path)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            steps = [Step.from_dict(d) for d in (data["steps"] if isinstance(data, dict) else data)]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self._show_error(EngineError(f"This file is not an Auto Clicker recording: {exc}"))
            return
        self.steps = steps
        if isinstance(data, dict) and isinstance(data.get("play"), dict):
            self._apply_play_settings(data["play"])
        self._preset_name, self._preset_sig = "", None
        self.preset_var.set("")
        self._refresh_tree()
        url = data.get("url") if isinstance(data, dict) else ""
        self.log(f"Opened {len(steps)} clicks from {os.path.basename(path)}" + (f" (recorded on {url})" if url else ""))

    def _recording_data(self, name: str = "") -> dict:
        data = {"app": "tab-autoclicker", "version": 2,
                "url": safe_url(self.target.url if self.target else (self.steps[0].url if self.steps else "")),
                "steps": [s.to_dict() for s in self.steps], "play": self._play_settings()}
        if name:
            data["name"] = name
        return data

    # ----------------------------------------------------------------- presets
    # A preset is a recording file (steps + play settings) kept in PRESETS_DIR,
    # so one can be kept per game mode and switched with one click.

    @staticmethod
    def _preset_path(name: str) -> str:
        safe = re.sub(r"[^\w\- ]+", "_", name).strip() or "preset"
        return os.path.join(PRESETS_DIR, safe + ".json")

    @staticmethod
    def _signature(steps: list, play: dict) -> str:
        # Step ids are internal (older presets have none yet), so they are no "change".
        steps = [{k: v for k, v in st.items() if k != "uid"} for st in steps]
        return json.dumps({"steps": steps, "play": play}, sort_keys=True)

    def _preset_changed(self) -> bool:
        if not self._preset_name or self._preset_sig is None:
            return False
        return self._signature([s.to_dict() for s in self.steps], self._play_settings()) != self._preset_sig

    def _read_preset(self, name: str) -> Optional[dict]:
        try:
            with open(self._preset_path(name), encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) and isinstance(data.get("steps"), list) else None
        except (OSError, ValueError):
            return None

    def _preset_names(self) -> list[str]:
        names = []
        try:
            files = sorted(f for f in os.listdir(PRESETS_DIR) if f.endswith(".json"))
        except OSError:
            files = []
        for f in files:
            try:
                with open(os.path.join(PRESETS_DIR, f), encoding="utf-8") as fh:
                    names.append(str(json.load(fh).get("name") or f[:-5]))
            except (OSError, ValueError, AttributeError):
                continue
        return sorted(names, key=str.lower)

    def _refresh_presets(self) -> None:
        names = self._preset_names()
        self.preset_box.configure(values=names or ["(no presets yet)"])
        if self._preset_name not in names:
            self._preset_name, self._preset_sig = "", None
        self.preset_var.set(self._preset_name)

    def choose_preset(self, name: str) -> None:
        if name == "(no presets yet)" or (name == self._preset_name and not self._preset_changed()):
            self.preset_var.set(self._preset_name)
            return
        data = self._read_preset(name)
        if data is None:
            self._show_error(EngineError(f"Could not read the preset “{name}”."))
            self._refresh_presets()
            return
        if name == self._preset_name:  # the same preset again: go back to what is saved
            answer = self.ask_choice("Reload preset", f"Throw away the unsaved changes and reload “{name}” "
                                     "as it is saved?", [("reload", "Reload it"), ("cancel", "Cancel")])
            if answer == "reload":
                self._load_preset(name, data)
            else:
                self.preset_var.set(self._preset_name)
            return
        if self.steps and (self._preset_changed() or not self._preset_name):
            what = (f"the changes to “{self._preset_name}”" if self._preset_name
                    else f"the current {len(self.steps)} step(s) as a preset")
            answer = self.ask_choice("Switch preset", f"Load the preset “{name}”? You have not saved {what}.",
                                     [("switch", "Load it"), ("cancel", "Cancel")])
            if answer != "switch":
                self.preset_var.set(self._preset_name)
                return
        self._load_preset(name, data)

    def _load_preset(self, name: str, data: dict) -> bool:
        try:
            steps = [Step.from_dict(d) for d in data["steps"]]
        except (TypeError, ValueError) as exc:
            self._show_error(EngineError(f"The preset “{name}” is damaged: {exc}"))
            self.preset_var.set(self._preset_name)
            return False
        self.steps = steps
        if isinstance(data.get("play"), dict):
            self._apply_play_settings(data["play"])
        self._preset_name = name
        self.preset_var.set(name)
        self._preset_sig = self._signature([s.to_dict() for s in self.steps], self._play_settings())
        self._refresh_tree()
        self.log(f"Loaded preset “{name}” ({len(steps)} step(s)).")
        return True

    def _jump_to_preset(self, requested: str) -> None:
        """An image check ended playback to continue with another preset."""
        self._jump_id = None
        names = self._preset_names()
        if requested:
            name = requested
        elif names:  # the next one after the current preset, wrapping around
            i = names.index(self._preset_name) if self._preset_name in names else -1
            name = names[(i + 1) % len(names)]
        else:
            name = ""
        data = self._read_preset(name) if name else None
        if data is None:
            self.log("Cannot go to " + (f"the preset “{name}”" if name else "the next preset")
                     + ": it does not exist. Playback stopped.")
            return
        if self.mode != "idle" or not self._load_preset(name, data):
            return
        self.toggle_play(chained=True)

    def save_preset(self) -> None:
        if not self.steps:
            return
        name = simpledialog.askstring(
            "Save preset", "Name of this preset (for example “Dungeon” or “PvP”).\n"
            "It keeps the clicks, image checks and Play settings.",
            initialvalue=self._preset_name, parent=self.root)
        name = (name or "").strip()
        if not name:
            return
        path = self._preset_path(name)
        if name != self._preset_name and os.path.exists(path):
            if not messagebox.askyesno(APP_NAME, f"A preset called “{name}” already exists. Replace it?",
                                       parent=self.root):
                return
        data = self._recording_data(name)
        try:
            os.makedirs(PRESETS_DIR, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, path)
        except OSError as exc:
            self._show_error(EngineError(f"Could not save the preset: {exc}"))
            return
        self._preset_name = name
        self._preset_sig = self._signature(data["steps"], data["play"])
        self._refresh_presets()
        self._update_states()
        self.log(f"Saved preset “{name}” ({len(self.steps)} step(s)).")

    def delete_preset(self) -> None:
        name = self._preset_name
        if not name or not messagebox.askyesno(
                APP_NAME, f"Delete the preset “{name}”? The steps shown now stay until you change them.",
                parent=self.root):
            return
        try:
            os.remove(self._preset_path(name))
        except OSError as exc:
            self._show_error(EngineError(f"Could not delete the preset: {exc}"))
            return
        self._preset_name, self._preset_sig = "", None
        self._refresh_presets()
        self._update_states()
        self.log(f"Deleted preset “{name}”.")

    # --------------------------------------------------------------- playback

    def _play_options(self) -> PlayOptions:
        return PlayOptions(
            repeat=0 if self.forever_var.get() else max(1, self._int(self.repeat_var, 1)),
            timing=self.timing_var.get(),
            speed=self._float(self.speed_var, 1.0),
            interval_ms=max(10, self._int(self.interval_var, 1000)),
            jitter_ms=self._int(self.jitter_var, 0),
            repeat_pause_ms=self._pause_ms(),
            aim=self.aim_var.get(),
            restore_scroll=self.scroll_var.get(),
            markers=self.markers_var.get(),
            game_mode=self.game_var.get(),
            window_size=self.win_size if self.resize_var.get() and self.win_size else None,
        )

    def toggle_play(self, chained: bool = False) -> None:
        if self.mode == "playing":
            self.engine.stop_play()
            return
        if self._cancel_jump():
            return
        if not chained and self._bot_tab_shown():   # F9 on the bot tab: start the bot
            self.bot.toggle()
            return
        if not (self.connected and self.mode == "idle" and self.target and self.steps):
            return
        jumps = any(s.is_check and (s.on_found in PRESET_ACTIONS or s.on_missing in PRESET_ACTIONS)
                    for s in self.steps)
        if jumps and not chained and (self._preset_changed() or not self._preset_name):
            what = (f"your changes to “{self._preset_name}”" if self._preset_name
                    else "these steps (they are not saved as a preset)")
            answer = self.ask_choice(
                "Play", f"An image check here switches to another preset, which replaces the steps on "
                        f"screen. Save {what} first?",
                [("save", "Save preset…"), ("play", "Play anyway"), ("cancel", "Cancel")])
            if answer == "save":
                self.save_preset()
                if self._preset_changed() or not self._preset_name:
                    return  # saving was cancelled
            elif answer != "play":
                return
        opts = self._play_options()
        self.progress_var.set("starting…")
        self.status_note = ""
        self._last_progress = None
        self.log(f"Playing {len(self.steps)} step(s) "
                 + ("forever" if opts.repeat == 0 else f"{opts.repeat} time(s)")
                 + (f", every {opts.interval_ms} ms" if opts.timing == "fixed" else
                    f" at {opts.speed:g}× recorded speed") + ".")
        self.run_bg(lambda: self.engine.play(self.steps, opts))

    def stop_everything(self) -> None:
        if self.mode != "idle":
            self.run_bg(self.engine.stop_all)
        else:
            self._cancel_jump()

    def _cancel_jump(self) -> bool:
        """Stop a preset switch that is about to start (Esc / Stop right after it)."""
        if self._jump_id is None:
            return False
        self.root.after_cancel(self._jump_id)
        self._jump_id = None
        self.log("Switching to the next preset was cancelled.")
        return True

    # ------------------------------------------------------------------ closing

    def on_close(self) -> None:
        if self.bot:
            self.bot.close()
        self._save_ui_settings()
        try:
            self.engine.disconnect()  # ends the session so Firefox accepts a new one
        except Exception:  # noqa: BLE001
            pass
        self.root.destroy()


def main() -> None:
    root = tk.Tk(className="tab-autoclicker")
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
