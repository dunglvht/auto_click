"""Pick / record / replay logic on top of WebDriver BiDi. No GUI code here.

All positions are CSS pixels relative to the top-level page viewport of the
target tab. Clicks are synthesized *inside* Firefox (``input.performActions``),
so the real mouse pointer is never moved and the tab may stay in the
background.
"""

from __future__ import annotations

import base64
import random
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, fields, replace
from typing import Any, Callable, Optional

from . import agent_js as js
from . import bitheroes, imaging
from .bidi import BiDiClient, BiDiError, Channel, ConnectionLost, ScriptError, WindowRef, from_remote

SANDBOX = "tab-autoclicker"
PICK_CHANNEL = "tab-autoclicker-pick"
REC_CHANNEL = "tab-autoclicker-record"
POINTER_ID = "tab-autoclicker-mouse"
BUTTON_NAMES = {0: "Left", 1: "Middle", 2: "Right", 3: "Back", 4: "Forward"}

# Image checks: what to do when the check succeeds / fails.
CHECK_ACTIONS = {
    "continue": "go on",
    "click": "click it, then go on",
    "click_preset": "click it, then go to preset",
    "click_step": "click it, then go to step",
    "skip": "skip the next step",
    "restart": "start the next repeat",
    "preset": "go to preset",
    "step": "go to step",
    "stop": "stop playback",
}
PRESET_ACTIONS = ("click_preset", "preset")  # end this playback and play another preset
STEP_ACTIONS = ("click_step", "step")      # jump to another step of this recording
CLICK_ACTIONS = ("click", "click_preset", "click_step")
MATCH_MARGIN = 4        # CSS px around a check region searched for the image
CHECK_POLL_S = 0.25     # how often a waiting check looks again
GAME_MIN_HOLD_MS = 50   # games read the mouse once per frame; shorter presses can be missed


def safe_url(url: str) -> str:
    """Drop a query string that carries a login token (direct game links do),
    so it does not end up in saved recordings."""
    base, sep, query = (url or "").partition("?")
    return base if sep and "token" in query.lower() else url or ""


def step_label(steps: list, uid: str) -> str:
    """“#5” for the step with this uid, as the user sees it in the list."""
    index = next((i for i, s in enumerate(steps) if s.uid == uid), None)
    return f"#{index + 1}" if index is not None else "(a deleted step)"


class EngineError(Exception):
    """A user-facing error (message is shown as-is)."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


@dataclass
class Step:
    delay_ms: int                 # wait before this press (press-to-press)
    x: float                      # press position (top-level viewport, CSS px)
    y: float
    button: int = 0
    hold_ms: int = 80             # press duration
    up_x: Optional[float] = None  # release position (differs for drags)
    up_y: Optional[float] = None
    scroll_x: float = 0.0         # page scroll when the click was recorded
    scroll_y: float = 0.0
    selector: Optional[str] = None  # CSS selector of the clicked element (top document only)
    fx: float = 0.5               # relative position inside that element
    fy: float = 0.5
    label: str = ""
    in_frame: bool = False
    url: str = ""
    vw: int = 0                   # viewport size when recorded
    vh: int = 0
    # --- image checks (kind == "check"); x, y is then the centre of ``box``
    kind: str = "click"           # "click" or "check"
    box: Optional[list] = None    # [x, y, w, h] region in CSS px
    image: str = ""               # base64 PNG of the region, in device pixels
    dpr: float = 1.0              # devicePixelRatio when the image was taken
    name: str = ""
    wait_for: str = "appear"      # "appear" or "disappear"
    timeout_ms: int = 0           # 0 = look once
    threshold: int = 80           # % of pixels that must match
    tone: bool = False            # also match when lighter/darker (hover highlight, fading)
    on_found: str = "click"       # a CHECK_ACTIONS key
    on_missing: str = "stop"
    goto_preset: str = ""         # for the "go to preset" actions; "" = the next preset in the list
    click_x: Optional[float] = None  # where "click it" clicks, from the box's top-left (CSS px);
    click_y: Optional[float] = None  # None = the middle of the image. May lie outside the box.
    goto_step: str = ""           # for the "go to step" actions: the target step's uid
    click_delay_ms: int = 0       # "click it": wait this long after the image is found, then click
    after_ms: int = 0             # wait this long after a successful check (after its click); not when it fails
    retry_clicks: int = 0         # "click it": if the image is still there after the click, click again up to this often
    retry_ms: int = 1000          # ... looking again this long after each click
    # --- any step
    only_on_jump: bool = False    # skipped in normal order; runs only when a check jumps to it
    uid: str = field(default_factory=lambda: uuid.uuid4().hex[:10])  # stable id for jumps

    def __post_init__(self):
        if self.up_x is None:
            self.up_x = self.x
        if self.up_y is None:
            self.up_y = self.y

    @property
    def is_check(self) -> bool:
        return self.kind == "check"

    @property
    def is_drag(self) -> bool:
        return not self.is_check and (abs(self.up_x - self.x) > 4 or abs(self.up_y - self.y) > 4)

    @property
    def action(self) -> str:
        if self.is_check:
            verb = "Wait for" if self.timeout_ms > 0 else "Check"
            return f"{verb} image" + (" gone" if self.wait_for == "disappear" else "")
        name = BUTTON_NAMES.get(self.button, f"Button {self.button}")
        return f"{name} drag" if self.is_drag else f"{name} click"

    @property
    def preset_label(self) -> str:
        return f"“{self.goto_preset}”" if self.goto_preset else "(the next one in the list)"

    def describe(self, action: str, steps: Optional[list] = None) -> str:
        """Human-readable action, including the preset or step it goes to."""
        text = CHECK_ACTIONS.get(action, action)
        if action in PRESET_ACTIONS:
            return f"{text} {self.preset_label}"
        if action in STEP_ACTIONS and steps is not None:
            return f"{text} {step_label(steps, self.goto_step)}"
        return text

    @property
    def title(self) -> str:
        return self.name or ("image" if self.is_check else self.label)

    def template(self) -> imaging.Image:
        cached = getattr(self, "_tpl", None)
        if cached is None or cached[0] != self.image:
            cached = (self.image, imaging.decode_png(base64.b64decode(self.image)))
            object.__setattr__(self, "_tpl", cached)
        return cached[1]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["url"] = safe_url(self.url)  # also for recordings made by older versions
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Step":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class PlayOptions:
    repeat: int = 1               # 0 = forever
    timing: str = "recorded"      # "recorded" or "fixed"
    speed: float = 1.0            # recorded timing is divided by this
    interval_ms: int = 1000       # press-to-press interval in "fixed" mode
    jitter_ms: int = 0            # random extra delay 0..jitter before each click
    repeat_pause_ms: int = 1000   # sleep between rounds (both timings)
    aim: str = "position"         # "position" or "element"
    restore_scroll: bool = True
    markers: bool = True
    game_mode: bool = False       # keep a game working when its window is not focused
    window_size: Optional[tuple] = None  # (w, h) page size to resize the window to before playing


@dataclass
class TabInfo:
    context: str
    title: str = ""
    url: str = ""

    @property
    def short_url(self) -> str:
        """The address without a long query string (game links can hold login tokens)."""
        base, sep, query = self.url.partition("?")
        return base + ("?…" if sep and (len(query) > 40 or "token" in query.lower()) else sep + query)

    @property
    def label(self) -> str:
        title = self.title or "(untitled)"
        return f"{title}  —  {self.short_url}" if self.url else title


class Engine:
    def __init__(self, notify: Callable[[str, Any], None]):
        """``notify(kind, payload)`` is called from worker threads with:
        connected(str) / disconnected(str) / mode(str) / target(TabInfo|None) /
        step(Step) / progress(tuple) / status(str) / goto_preset(str) / log(str)"""
        self.notify = notify
        self.client: Optional[BiDiClient] = None
        self.browser = ""
        self._lock = threading.RLock()
        self.parents: dict[str, Optional[str]] = {}
        self.urls: dict[str, str] = {}
        self.target: Optional[TabInfo] = None
        self.mode = "idle"            # idle | picking | recording | playing
        self._pick_script: Optional[str] = None
        self._rec_script: Optional[str] = None
        self._last_press_t: Optional[float] = None
        self._stop = threading.Event()
        self._play_thread: Optional[threading.Thread] = None
        self._last_ptr: Optional[tuple] = None
        self._dpr = 1.0

    # ------------------------------------------------------------------ utils

    def log(self, text: str) -> None:
        self.notify("log", text)

    def _set_mode(self, mode: str) -> None:
        with self._lock:
            self.mode = mode
        self.notify("mode", mode)

    def _require(self, *, target: bool = False, idle: bool = True) -> BiDiClient:
        client = self.client
        if client is None or not client.connected:
            raise EngineError("Not connected to Firefox.")
        if target and self.target is None:
            raise EngineError("Choose a target tab first.")
        if idle and self.mode != "idle":
            raise EngineError(f"Busy ({self.mode}). Stop that first.")
        return client

    @property
    def connected(self) -> bool:
        return self.client is not None and self.client.connected

    # ------------------------------------------------------------- connection

    def connect(self, port: int) -> str:
        if self.connected:
            return self.browser
        client = BiDiClient(on_event=self._on_event, on_close=self._on_close)
        try:
            caps = client.connect(port)
        except ConnectionRefusedError:
            raise EngineError(
                f"Firefox is not listening for remote control on port {port}.\n\n"
                "Start it with “Launch Firefox” (or run: firefox "
                f"--remote-debugging-port {port})") from None
        except BiDiError as exc:
            client.close()
            if "Maximum number of active sessions" in exc.message:
                raise EngineError(
                    "Firefox already has another remote-control session (for example "
                    "from an Auto Clicker that crashed, or a tool such as Selenium).\n\n"
                    "Firefox only allows one at a time. Restart Firefox to clear it.",
                    code="session_busy") from None
            raise EngineError(f"Firefox refused the connection: {exc}") from None
        except Exception as exc:  # noqa: BLE001
            client.close()
            raise EngineError(f"Could not connect to Firefox on port {port}: {exc}") from None
        self.client = client
        self.browser = f"{caps.get('browserName', 'firefox').title()} {caps.get('browserVersion', '')}".strip()
        client.send("session.subscribe", {"events": [
            "script.message", "browsingContext.contextCreated",
            "browsingContext.contextDestroyed", "browsingContext.load"]})
        self._refresh_tree()
        self.notify("connected", self.browser)
        return self.browser

    def disconnect(self) -> None:
        client = self.client
        if client is None:
            return
        self.stop_all()
        if self._play_thread:
            self._play_thread.join(3)
        self.client = None
        client.close()
        self._reset_state()
        self.notify("disconnected", "Disconnected.")

    def _reset_state(self) -> None:
        with self._lock:
            self.mode = "idle"
            self.parents, self.urls = {}, {}
            self._pick_script = self._rec_script = None
        self._stop.set()

    def _on_close(self, reason: str) -> None:
        self.client = None
        self._reset_state()
        self.notify("mode", "idle")
        self.notify("disconnected", "Lost the connection to Firefox (was it closed?).")

    # ----------------------------------------------------------- context tree

    def _refresh_tree(self) -> list:
        tree = self.client.send("browsingContext.getTree", {})["contexts"]
        parents: dict[str, Optional[str]] = {}
        urls: dict[str, str] = {}

        def walk(nodes, parent):
            for node in nodes or []:
                parents[node["context"]] = parent
                urls[node["context"]] = node.get("url", "")
                walk(node.get("children"), node["context"])
        walk(tree, None)
        with self._lock:
            self.parents, self.urls = parents, urls
        return tree

    def _contexts_under(self, top: Optional[str] = None) -> list[str]:
        """All known contexts (optionally only those inside tab ``top``)."""
        with self._lock:
            ids = list(self.parents)
        return [c for c in ids if top is None or self.top_of(c) == top]

    def top_of(self, ctx: Optional[str]) -> Optional[str]:
        for attempt in range(2):
            with self._lock:
                parents = self.parents
            cur, hops = ctx, 0
            while cur in parents and parents[cur] is not None and hops < 64:
                cur, hops = parents[cur], hops + 1
            if cur in parents:
                return cur
            if attempt == 0 and self.client:
                try:
                    self._refresh_tree()
                except Exception:  # noqa: BLE001
                    return None
        return None

    def _on_event(self, method: str, params: dict) -> None:
        if method == "browsingContext.contextCreated":
            with self._lock:
                self.parents[params["context"]] = params.get("parent")
                self.urls[params["context"]] = params.get("url", "")
        elif method == "browsingContext.contextDestroyed":
            self._forget_context(params)
        elif method == "browsingContext.load":
            ctx = params.get("context")
            with self._lock:
                if ctx in self.parents:
                    self.urls[ctx] = params.get("url", "")
            if self.target and ctx == self.target.context:
                threading.Thread(target=self._refresh_target_info, daemon=True).start()
        elif method == "script.message":
            channel = params.get("channel")
            source = (params.get("source") or {}).get("context")
            data = from_remote(params.get("data")) or {}
            if channel == PICK_CHANNEL:
                self._on_pick(source)
            elif channel == REC_CHANNEL and isinstance(data, dict):
                self._on_recorded_click(source, data)

    def _forget_context(self, info: dict) -> None:
        def ids(node):
            yield node["context"]
            for child in node.get("children") or []:
                yield from ids(child)
        gone = set(ids(info))
        with self._lock:
            # Also drop descendants we learned about via contextCreated.
            changed = True
            while changed:
                changed = False
                for c, p in list(self.parents.items()):
                    if p in gone and c not in gone:
                        gone.add(c)
                        changed = True
            for c in gone:
                self.parents.pop(c, None)
                self.urls.pop(c, None)
        if self.target and self.target.context in gone:
            self.log("The target tab was closed.")
            if self.mode == "recording":
                self._rec_script = None
                self._set_mode("idle")
            self._stop.set()
            self.target = None
            self.notify("target", None)

    # ------------------------------------------------------------------- tabs

    def list_tabs(self) -> list[TabInfo]:
        client = self._require(idle=False)
        tops = [n["context"] for n in self._refresh_tree()]
        results = client.send_many(
            [("script.callFunction", client.call_params(c, js.TITLE, sandbox=SANDBOX)) for c in tops], timeout=4)
        tabs = []
        for ctx, res in zip(tops, results):
            title, url = "", self.urls.get(ctx, "")
            if isinstance(res, dict):
                try:
                    title, url = client.script_result(res)
                except Exception:  # noqa: BLE001
                    pass
            tabs.append(TabInfo(ctx, title, url))
        return tabs

    def set_target(self, ctx: str) -> TabInfo:
        self._require(idle=True)
        top = self.top_of(ctx)
        if top is None:
            raise EngineError("That tab no longer exists.")
        tab = TabInfo(top, "", self.urls.get(top, ""))
        self._fetch_title(tab)
        self.target = tab
        self._last_ptr = None
        self.notify("target", tab)
        return tab

    def _fetch_title(self, tab: TabInfo) -> None:
        client = self.client
        if client is None:
            return
        try:
            tab.title, tab.url = client.call(tab.context, js.TITLE, sandbox=SANDBOX, timeout=4)
        except Exception:  # noqa: BLE001
            tab.url = self.urls.get(tab.context, tab.url)

    def _refresh_target_info(self) -> None:
        tab = self.target
        if tab is None:
            return
        self._fetch_title(tab)
        if self.target is tab:
            self.notify("target", tab)

    def show_target(self) -> None:
        client = self._require(target=True, idle=False)
        client.send("browsingContext.activate", {"context": self.target.context})

    # ------------------------------------------------------------ window size

    def window_size(self, client: Optional[BiDiClient] = None) -> dict:
        """Sizes of the target tab: page (the area the clicks use) and whole window."""
        client = client or self._require(target=True, idle=False)
        tab = self.target.context
        info = client.send("browsingContext.getTree", {"root": tab, "maxDepth": 0})["contexts"][0]
        wid = info.get("clientWindow")
        win = next((w for w in client.send("browser.getClientWindows", {})["clientWindows"]
                    if w.get("clientWindow") == wid), None)
        _, _, vw, vh = client.call(tab, js.SCROLL_POS, sandbox=SANDBOX, timeout=5)
        if win is None:
            raise EngineError("Could not find the Firefox window of the target tab.")
        return {"page": (int(vw), int(vh)), "window": (int(win["width"]), int(win["height"])),
                "state": win.get("state", "normal"), "id": wid}

    def resize_page(self, width: int, height: int, client: Optional[BiDiClient] = None) -> tuple[int, int]:
        """Resize the target's window so its page area is ``width`` × ``height`` CSS px
        (toolbars and borders are measured and added). Returns the page size reached."""
        client = client or self._require(target=True, idle=False)
        for _ in range(4):
            info = self.window_size(client)
            vw, vh = info["page"]
            if abs(vw - width) <= 1 and abs(vh - height) <= 1:
                return vw, vh
            w, h = info["window"]
            try:
                client.send("browser.setClientWindowState", {
                    "clientWindow": info["id"], "state": "normal",
                    "width": max(200, w + width - vw), "height": max(150, h + height - vh)}, timeout=10)
            except BiDiError as exc:
                raise EngineError(f"Firefox could not resize the window: {exc.message}") from None
            time.sleep(0.4)  # let the page lay out at the new size
        vw, vh = self.window_size(client)["page"]
        if abs(vw - width) > 1 or abs(vh - height) > 1:
            raise EngineError(f"The window only got to a page size of {vw} × {vh} (wanted {width} × {height}). "
                              "The screen may be too small, or the window manager refused.")
        return vw, vh

    def open_in_window(self, url: str) -> TabInfo:
        """Open ``url`` in a new Firefox window and make it the target. A game
        in its own window keeps running while you use other tabs."""
        client = self._require()
        ctx = client.send("browsingContext.create", {"type": "window"})["context"]
        with self._lock:
            self.parents.setdefault(ctx, None)
        client.send("browsingContext.navigate", {"context": ctx, "url": url, "wait": "none"}, timeout=30)
        return self.set_target(ctx)

    # ------------------------------------------------------------------- pick

    def start_pick(self) -> None:
        client = self._require()
        self._set_mode("picking")
        try:
            self._pick_script = client.send("script.addPreloadScript", {
                "functionDeclaration": js.PICK, "sandbox": SANDBOX,
                "arguments": [{"type": "channel", "value": {"channel": PICK_CHANNEL, "ownership": "none"}}],
            })["script"]
            self._refresh_tree()
            client.send_many([("script.callFunction",
                               client.call_params(c, js.PICK, Channel(PICK_CHANNEL), sandbox=SANDBOX))
                              for c in self._contexts_under()], timeout=5)
        except Exception:
            self._end_pick()
            raise
        self.log("Pick mode: click anywhere inside the Firefox tab you want to automate.")

    def cancel_pick(self) -> None:
        if self.mode == "picking":
            self._end_pick()
            self.log("Tab picking cancelled.")

    def _end_pick(self) -> None:
        client, script = self.client, self._pick_script
        self._pick_script = None
        if client and client.connected:
            if script:
                try:
                    client.send("script.removePreloadScript", {"script": script}, timeout=5)
                except Exception:  # noqa: BLE001
                    pass
            client.send_many([("script.callFunction", client.call_params(c, js.CANCEL_PICK, sandbox=SANDBOX))
                              for c in self._contexts_under()], timeout=3)
        self._set_mode("idle")

    def _on_pick(self, source: Optional[str]) -> None:
        if self.mode != "picking" or not source:
            return
        top = self.top_of(source)
        self._end_pick()
        if top is None:
            self.log("Could not identify that tab; please try again.")
            return
        tab = self.set_target(top)
        self.log(f"Target tab: {tab.label}")

    # ----------------------------------------------------------------- record

    def start_recording(self) -> None:
        client = self._require(target=True)
        tab = self.target.context
        self._set_mode("recording")
        self._last_press_t = None
        try:
            self._rec_script = client.send("script.addPreloadScript", {
                "functionDeclaration": js.RECORD, "sandbox": SANDBOX, "contexts": [tab],
                "arguments": [{"type": "channel", "value": {"channel": REC_CHANNEL, "ownership": "none"}}],
            })["script"]
            self._refresh_tree()
            client.send_many([("script.callFunction",
                               client.call_params(c, js.RECORD, Channel(REC_CHANNEL), sandbox=SANDBOX))
                              for c in self._contexts_under(tab)], timeout=5)
        except Exception:
            self.stop_recording()
            raise
        self.log("Recording: click in the target tab. Press Stop when you are done.")

    def stop_recording(self) -> None:
        if self.mode != "recording":
            return
        client, script = self.client, self._rec_script
        self._rec_script = None
        self._set_mode("idle")
        if client and client.connected:
            if script:
                try:
                    client.send("script.removePreloadScript", {"script": script}, timeout=5)
                except Exception:  # noqa: BLE001
                    pass
            if self.target:
                client.send_many([("script.callFunction", client.call_params(c, js.STOP_RECORD, sandbox=SANDBOX))
                                  for c in self._contexts_under(self.target.context)], timeout=3)
        self.log("Recording stopped.")

    def _frame_offset(self, ctx: str) -> Optional[tuple[float, float]]:
        """Offset of a (nested) frame's viewport inside the top-level viewport."""
        ox = oy = 0.0
        cur = ctx
        for _ in range(16):
            with self._lock:
                parent = self.parents.get(cur)
            if parent is None:
                return ox, oy
            entries = self.client.call(parent, js.FRAME_OFFSETS, sandbox=SANDBOX, timeout=5) or []
            for win, fx, fy in entries:
                if isinstance(win, WindowRef) and win.context == cur:
                    ox, oy = ox + fx, oy + fy
                    break
            else:
                return None
            cur = parent
        return None

    def _on_recorded_click(self, source: Optional[str], d: dict) -> None:
        tab = self.target
        if self.mode != "recording" or tab is None or not source or self.top_of(source) != tab.context:
            return
        try:
            x, y = float(d["x"]), float(d["y"])
            ux, uy = float(d.get("ux", x)), float(d.get("uy", y))
            sx, sy = float(d.get("sx") or 0), float(d.get("sy") or 0)
            vw, vh = int(d.get("vw") or 0), int(d.get("vh") or 0)
            in_frame = source != tab.context
            selector = d.get("selector") if not in_frame else None
            if in_frame:
                off = self._frame_offset(source)
                if off is None:
                    self.log("Warning: could not locate the frame of the last click; "
                             "its position may be off.")
                    off = (0.0, 0.0)
                x, y, ux, uy = x + off[0], y + off[1], ux + off[0], uy + off[1]
                try:
                    sx, sy, vw, vh = self.client.call(tab.context, js.SCROLL_POS, sandbox=SANDBOX, timeout=5)
                except Exception:  # noqa: BLE001
                    sx = sy = 0.0
            t = float(d.get("t") or time.time() * 1000)
            delay = 0 if self._last_press_t is None else max(0, int(round(t - self._last_press_t)))
            self._last_press_t = t
            step = Step(delay_ms=delay, x=round(x, 1), y=round(y, 1), button=int(d.get("button") or 0),
                        hold_ms=int(round(float(d.get("hold") or 0))), up_x=round(ux, 1), up_y=round(uy, 1),
                        scroll_x=float(sx), scroll_y=float(sy), selector=selector,
                        fx=float(d.get("fx", 0.5)), fy=float(d.get("fy", 0.5)),
                        label=str(d.get("label") or ""), in_frame=in_frame,
                        url=safe_url(str(d.get("href") or "")), vw=int(vw), vh=int(vh))
        except (KeyError, TypeError, ValueError) as exc:
            self.log(f"Ignored a malformed click report: {exc}")
            return
        except (BiDiError, ScriptError, ConnectionLost, TimeoutError) as exc:
            self.log(f"Could not record a click: {exc}")
            return
        self.notify("step", step)

    # ----------------------------------------------------------- screenshots

    def _capture(self, client: BiDiClient, tab: str, box: Optional[tuple] = None) -> bytes:
        """PNG of the tab's visible page (or of ``box`` = x, y, w, h in CSS px),
        without Auto Clicker's own badges and markers."""
        try:
            info = client.call(tab, js.OVERLAYS, False, sandbox=SANDBOX, timeout=5)
            self._dpr = float(info[0]) or 1.0
        except (BiDiError, ScriptError, TimeoutError):
            info = None
        params: dict = {"context": tab, "origin": "viewport"}
        if box:
            params["clip"] = {"type": "box", "x": box[0], "y": box[1], "width": box[2], "height": box[3]}
        try:
            data = client.send("browsingContext.captureScreenshot", params, timeout=20)["data"]
        finally:
            if info is not None:
                client.send_nowait("script.callFunction", client.call_params(tab, js.OVERLAYS, True, sandbox=SANDBOX))
        return base64.b64decode(data)

    def screenshot(self) -> dict:
        """The target tab as the user sees it, for choosing an image-check region."""
        client = self._require(target=True, idle=False)
        tab = self.target.context
        png = self._capture(client, tab)
        sx, sy, vw, vh = client.call(tab, js.SCROLL_POS, sandbox=SANDBOX, timeout=5)
        return {"png": png, "dpr": self._dpr, "vw": vw, "vh": vh, "sx": sx, "sy": sy}

    def _match(self, client: BiDiClient, tab: str, step: Step, need: float = 0.0) -> tuple[float, float, float]:
        """Look for the check's image around its region: (score %, click x, y).
        The click point follows the image if it is found slightly moved."""
        tpl = step.template()
        x, y, w, h = step.box
        m = MATCH_MARGIN
        x0, y0 = max(0.0, x - m), max(0.0, y - m)
        img = imaging.decode_png(self._capture(client, tab, (x0, y0, w + (x - x0) + m, h + (y - y0) + m)))
        dpr = self._dpr
        score, bx, by = imaging.find(tpl, img, int(round((x - x0) * dpr)), int(round((y - y0) * dpr)),
                                     int(round(m * dpr)) + 1, need / 100.0, step.tone)
        left, top = x0 + bx / dpr, y0 + by / dpr  # where the image's top-left was found
        if step.click_x is not None and step.click_y is not None:
            return score * 100.0, left + step.click_x, top + step.click_y
        return score * 100.0, left + tpl.width / 2 / dpr, top + tpl.height / 2 / dpr

    def test_check(self, step: Step) -> tuple[bool, float, Optional[tuple[float, float]]]:
        """Run an image check once against the page as it is now:
        (passes, score %, where playback would click or None). The click
        spot is marked in the page with a blue ring; nothing is clicked."""
        client = self._require(target=True, idle=False)
        tab = self.target.context
        score, cx, cy = self._match(client, tab, step)
        present = score >= step.threshold
        point = (cx, cy) if present else None
        if point:
            client.send_nowait("script.callFunction", client.call_params(
                tab, js.MARKER, cx, cy, "rgba(30,110,230,.95)", 2500, sandbox=SANDBOX))
        return (present if step.wait_for == "appear" else not present), score, point

    def test_and_click(self, step: Step, game_mode: bool = False) -> tuple[bool, float, Optional[tuple], bool]:
        """Like ``test_check``, but when the image is found and the check's “when
        found” action clicks, also wait its click delay and click, as playback
        would. Jumps are not followed. Returns (passes, score %, point, clicked)."""
        client = self._require(target=True)  # not while playing or recording
        tab = self.target.context
        passed, score, point = self.test_check(step)
        if not (passed and point and step.wait_for != "disappear" and step.on_found in CLICK_ACTIONS):
            return passed, score, point, False
        if step.click_delay_ms > 0:
            time.sleep(min(step.click_delay_ms, 60_000) / 1000.0)
        script = self._keep_focus(client, tab, True) if game_mode else None
        self._last_ptr = None  # always move to the spot first
        try:
            click = Step(delay_ms=0, x=point[0], y=point[1], hold_ms=80)
            clicked = self._replay_step(client, tab, click, 80, PlayOptions(restore_scroll=False, markers=True), 0)
        finally:
            if game_mode:
                try:
                    self._keep_focus(client, tab, False, script)
                except Exception:  # noqa: BLE001
                    pass
        return passed, score, point, clicked

    def recording_paused(self) -> None:
        """While recording, the user spent time in Auto Clicker (e.g. adding an
        image check): measure the next click's wait from now."""
        if self.mode == "recording":
            self._last_press_t = time.time() * 1000

    # --------------------------------------------------------------- playback

    def play(self, steps: list[Step], opts: PlayOptions) -> None:
        client = self._require(target=True)
        if not steps:
            raise EngineError("Nothing to play: record some clicks first.")
        if all(s.only_on_jump for s in steps):
            raise EngineError("Every step is set to run only when a check jumps to it, so there is "
                              "nothing to start with. Untick “only when jumped to” on the first step.")
        self._stop.clear()
        self._set_mode("playing")
        self._play_thread = threading.Thread(target=self._play_worker,
                                             args=(client, self.target.context, list(steps), opts),
                                             name="playback", daemon=True)
        self._play_thread.start()

    def stop_play(self) -> None:
        self._stop.set()

    def stop_all(self) -> None:
        if self.mode == "playing":
            self.stop_play()
        elif self.mode == "recording":
            self.stop_recording()
        elif self.mode == "picking":
            self.cancel_pick()

    def _keep_focus(self, client: BiDiClient, tab: str, on: bool, script: Optional[str] = None) -> Optional[str]:
        """Game mode on/off in the tab and all its frames (also after reloads)."""
        if script:
            try:
                client.send("script.removePreloadScript", {"script": script}, timeout=5)
            except Exception:  # noqa: BLE001
                pass
            script = None
        if on:
            script = client.send("script.addPreloadScript", {
                "functionDeclaration": "() => (" + js.KEEP_FOCUS + ")(true)",
                "sandbox": SANDBOX, "contexts": [tab]})["script"]
        client.send_many([("script.callFunction", client.call_params(c, js.KEEP_FOCUS, on, sandbox=SANDBOX))
                          for c in self._contexts_under(tab)], timeout=5)
        return script

    def _refocus(self, client: BiDiClient, tab: str, force: bool = False) -> None:
        """Game mode: tell the game again that it has focus (games ignore the
        mouse while they think they are in the background)."""
        try:
            client.send_many([("script.callFunction", client.call_params(c, js.KEEP_FOCUS, True, force, sandbox=SANDBOX))
                              for c in self._contexts_under(tab)], timeout=5)
        except Exception:  # noqa: BLE001
            pass

    def _reset_input(self, client: BiDiClient, tab: str, game_mode: bool) -> str:
        """Before clicking again: do what stopping and starting playback does.
        Firefox forgets its mouse state, and in game mode the game sees itself
        lose focus and get it back. Returns a note for the log."""
        self._last_ptr = None
        try:
            client.send("input.releaseActions", {"context": tab}, timeout=5)
        except Exception:  # noqa: BLE001
            pass
        if not game_mode:
            return ""
        contexts = self._contexts_under(tab)
        try:
            client.send_many([("script.callFunction", client.call_params(c, js.FOCUS_CYCLE, "blur", sandbox=SANDBOX))
                              for c in contexts], timeout=5)
            time.sleep(0.3)  # let the game run a few frames "in the background"
            results = client.send_many([("script.callFunction", client.call_params(
                c, js.FOCUS_CYCLE, "focus", sandbox=SANDBOX)) for c in contexts], timeout=5)
            time.sleep(0.3)
        except Exception:  # noqa: BLE001
            return ""
        notes = []
        for res in results:
            try:
                info = client.script_result(res) if isinstance(res, dict) else None
            except ScriptError:
                info = None
            if isinstance(info, dict) and info.get("game"):
                notes.append(f"game page {'has' if info.get('focus') else 'does not have'} focus, "
                             f"{info.get('visible')}, {info.get('blocked', 0)} focus losses hidden from it")
        return "; ".join(notes)

    def _wait_visible(self, client: BiDiClient, tab: str) -> Optional[bool]:
        """Game mode: a hidden tab does not run its game, so wait until it is
        shown. Returns None if stopped, else whether it had to wait."""
        try:
            if client.call(tab, js.VISIBILITY, sandbox=SANDBOX, timeout=5) != "hidden":
                return False
        except (BiDiError, ScriptError, TimeoutError):
            return False
        self.log("Paused: the game tab is hidden (another tab is in front of it, or its window is "
                 "minimized or completely covered). Games stop running then, so playback waits until "
                 "the tab is visible again.")
        self.notify("status", "paused: game tab is hidden")
        while not self._stop.wait(0.5):
            try:
                if client.call(tab, js.VISIBILITY, sandbox=SANDBOX, timeout=5) != "hidden":
                    self.log("The game tab is visible again; playback continues.")
                    self.notify("status", "")
                    self._refocus(client, tab)  # it may have missed getting focus back
                    return True
            except (BiDiError, ScriptError, TimeoutError):
                return True
        return None

    def _play_worker(self, client: BiDiClient, tab: str, steps: list[Step], opts: PlayOptions) -> None:
        clicks = 0
        loop = 0
        reason = "finished"
        self._last_ptr = None
        speed = max(0.05, float(opts.speed or 1.0))
        focus_script = None
        try:
            if opts.window_size:
                try:
                    before = self.window_size(client)["page"]
                    w, h = opts.window_size
                    if before != (w, h):
                        self.resize_page(w, h, client)
                        self.log(f"Resized the window: the page went from {before[0]} × {before[1]} to {w} × {h}.")
                except (EngineError, BiDiError, ScriptError, TimeoutError) as exc:
                    self.log(f"Could not resize the window: {exc}")
            try:
                _, _, vw, vh = client.call(tab, js.SCROLL_POS, sandbox=SANDBOX, timeout=5)
                rec = next(((s.vw, s.vh) for s in steps if s.vw and s.vh), None)
                # Page layouts mostly follow the width; a height change (e.g. the
                # bookmarks bar appearing) rarely moves anything, so ignore it.
                if rec and abs(rec[0] - vw) > 2:
                    self.log(f"Note: the page is {int(vw)} px wide but was {rec[0]} px when recorded, "
                             "so positions may be off. Resize the window or aim at elements.")
                dpr = client.call(tab, js.OVERLAYS, True, sandbox=SANDBOX, timeout=5)[0]
                self._dpr = float(dpr) or 1.0
                if any(s.is_check and abs(s.dpr - self._dpr) > 0.01 for s in steps):
                    self.log("Note: the page zoom (or screen scaling) changed since the image checks were "
                             "made, so they will probably not match. Reset the zoom with Ctrl+0.")
            except Exception:  # noqa: BLE001
                pass
            if opts.game_mode:
                focus_script = self._keep_focus(client, tab, True)
            prev_press = time.monotonic()
            prev_end = prev_press
            first = True
            index = {st.uid: n for n, st in enumerate(steps)}
            while opts.repeat == 0 or loop < opts.repeat:
                loop += 1
                round_start, round_clicks = time.monotonic(), clicks
                round_first = True
                # Jumps: "only when jumped to" steps run as a side branch. When the
                # branch (that step and the marked steps right after it) is done,
                # playback returns to the step after the check that entered it.
                # Jumps from one marked step to another stay in the branch.
                returns: list[tuple[int, bool]] = []
                branch = False
                last_jump = (0.0, -1)  # (time, clicks) of the last jump, against spinning

                def advance(j: int) -> int:
                    nonlocal branch
                    while True:
                        if branch:
                            if j < len(steps) and steps[j].only_on_jump:
                                return j
                            branch = False
                            if returns:
                                j, branch = returns.pop()
                                continue
                        while j < len(steps) and steps[j].only_on_jump:
                            j += 1
                        return j

                i = advance(0)
                while i < len(steps):
                    step = steps[i]
                    number = i + 1
                    if opts.timing == "fixed":
                        # Clicks are one interval apart; image checks run straight away.
                        due = prev_press + (0.0 if first or step.is_check else opts.interval_ms / 1000.0)
                        if round_first and not first:  # plus the sleep between rounds
                            due = max(due, prev_end + opts.repeat_pause_ms / 1000.0)
                    else:
                        gap = step.delay_ms / 1000.0 / speed
                        if round_first:
                            due = prev_end + gap + (0.0 if first else opts.repeat_pause_ms / 1000.0)
                        else:
                            due = prev_press + gap
                    if opts.jitter_ms > 0 and not step.is_check:
                        due += random.uniform(0, opts.jitter_ms) / 1000.0
                    wait = due - time.monotonic()
                    sleeping = round_first and not first and wait >= 2
                    if sleeping:
                        until = time.strftime("%H:%M:%S", time.localtime(time.time() + wait))
                        self.notify("status", f"sleeping until {until} before round {loop}")
                    if wait > 0 and self._stop.wait(wait):
                        reason = "stopped"
                        return
                    if sleeping:
                        self.notify("status", "")
                    if self._stop.is_set():
                        reason = "stopped"
                        return
                    if opts.game_mode and self._wait_visible(client, tab) is None:
                        reason = "stopped"
                        return
                    first = round_first = False
                    if step.is_check:
                        outcome = self._run_check(client, tab, step, opts, number)
                        if outcome is None:
                            reason = "stopped"
                            return
                        passed, point, score = outcome
                        action = step.on_found if passed else step.on_missing
                        prev_press = time.monotonic()  # later waits count from here
                        if passed:
                            what = "found" if step.wait_for != "disappear" else "gone"
                            if action in CLICK_ACTIONS and point is not None:
                                done = f"clicked it at {point[0]:.0f}, {point[1]:.0f}"
                                if step.click_delay_ms > 0:
                                    done += f" after {step.click_delay_ms / 1000:g} s"
                                if action != "click":
                                    done += ", then " + step.describe(action, steps).split(", then ", 1)[1]
                            else:
                                done = step.describe(action, steps)
                            if step.after_ms > 0 and action != "stop":
                                done += f" (after waiting {step.after_ms / 1000:g} s)"
                            self.log(f"Check #{number} ({step.title}): {what} ({score:.0f}% match) → {done}.")
                        if action in CLICK_ACTIONS and point is not None:
                            if step.click_delay_ms > 0 and self._stop.wait(step.click_delay_ms / 1000.0):
                                reason = "stopped"
                                return
                            click = Step(delay_ms=0, x=point[0], y=point[1], hold_ms=80)
                            if self._replay_step(client, tab, click, self._hold(click, opts, speed),
                                                 replace(opts, aim="position", restore_scroll=False), number):
                                clicks += 1
                            if step.retry_clicks > 0 and step.wait_for != "disappear":
                                got = self._retry_click(client, tab, step, opts, speed, number)
                                if got is None:
                                    reason = "stopped"
                                    return
                                clicks += got
                        if passed and step.after_ms > 0 and action != "stop":
                            # "Wait after": only after a success; a failed check goes on at once.
                            if self._stop.wait(step.after_ms / 1000.0):
                                reason = "stopped"
                                return
                            prev_press = time.monotonic()
                        prev_end = time.monotonic()
                        self.notify("progress", (loop, opts.repeat, number, len(steps), clicks))
                        if action == "stop":
                            reason = f"stopped by check #{number} ({step.title})"
                            return
                        if action in PRESET_ACTIONS:
                            reason = f"ended by check #{number} ({step.title}) to go to another preset"
                            self.notify("goto_preset", step.goto_preset)
                            return
                        if action == "restart":
                            break
                        if action in STEP_ACTIONS:
                            target = index.get(step.goto_step)
                            if target is None:
                                reason = f"stopped: check #{number} goes to a step that was deleted"
                                return
                            # Jumping around with no click in between must not spin.
                            if (clicks == last_jump[1] and time.monotonic() - last_jump[0] < 0.2
                                    and self._stop.wait(0.2)):
                                reason = "stopped"
                                return
                            last_jump = (time.monotonic(), clicks)
                            if steps[target].only_on_jump:
                                if not branch:  # entering a branch: remember where to come back to
                                    returns.append((i + 1, False))
                                branch = True
                            else:  # a plain "go to": carry on from there
                                returns.clear()
                                branch = False
                            i = target
                            continue
                        i = advance(i + 1)
                        if action == "skip":
                            i = advance(i + 1)
                        continue
                    prev_press = time.monotonic()
                    if self._replay_step(client, tab, step, self._hold(step, opts, speed), opts, number):
                        clicks += 1
                    prev_end = time.monotonic()
                    self.notify("progress", (loop, opts.repeat, number, len(steps), clicks))
                    i = advance(i + 1)
                # A round of only instant checks (no clicks) must not spin as fast as it can.
                if clicks == round_clicks and time.monotonic() - round_start < 0.2 and self._stop.wait(0.2):
                    reason = "stopped"
                    return
        except ConnectionLost:
            reason = "connection lost"
        except Exception as exc:  # noqa: BLE001
            reason = f"error: {exc}"
        finally:
            if client and client.connected:
                try:
                    self._last_ptr = None  # releasing also forgets where Firefox's pointer was
                    client.send("input.releaseActions", {"context": tab}, timeout=5)
                    if opts.game_mode:
                        self._keep_focus(client, tab, False, focus_script)
                except Exception:  # noqa: BLE001
                    pass
            self.notify("status", "")
            self._set_mode("idle")
            self.log(f"Playback {reason}: {clicks} click{'s' if clicks != 1 else ''} "
                     f"in {loop} repeat{'s' if loop != 1 else ''}.")

    # ---------------------------------------------------------- Bit Heroes bot

    def start_bot(self, settings: "bitheroes.BotSettings", shots_dir: Optional[str] = None) -> None:
        """Play Bit Heroes in the target tab until stopped (see bitheroes.py).
        Screens the bot does not know, for a while, are saved to ``shots_dir``."""
        client = self._require(target=True)
        if not settings.modes:
            raise EngineError("Tick at least one mode for the bot to play.")
        self._stop.clear()
        self._set_mode("playing")
        self._play_thread = threading.Thread(target=self._bot_worker,
                                             args=(client, self.target.context, settings, shots_dir),
                                             name="bot", daemon=True)
        self._play_thread.start()

    def _bot_worker(self, client: BiDiClient, tab: str, settings: "bitheroes.BotSettings",
                    shots_dir: Optional[str]) -> None:
        brain = bitheroes.Brain(settings, time.monotonic())
        reason = "stopped"
        focus_script = None
        speed_script = None
        speed_now: Optional[int] = None   # the speed set in the game; None = not known (e.g. after a reload)
        last_wait = ""                    # the last "waiting…" message logged
        last_click: Optional[tuple] = None
        tries = 0
        saved_unknown = False
        frame, frame_since = None, time.monotonic()   # to notice a frozen game
        last_reload = -1e9
        try:
            want = bitheroes.PAGE_SIZE
            size = self.window_size(client)["page"]
            if size != want:
                try:
                    size = self.resize_page(*want, client=client)
                    self.log(f"Bot: resized the window so the game page is {want[0]} × {want[1]}.")
                except (EngineError, BiDiError, ScriptError, TimeoutError) as exc:
                    self.log(f"Bot: could not resize the window: {exc}")
            if size != want:
                reason = (f"stopped: the game page must be {want[0]} × {want[1]} (it is {size[0]} × {size[1]}). "
                          "Un-maximize the Firefox window and try again")
                return
            self._dpr = float(client.call(tab, js.OVERLAYS, True, sandbox=SANDBOX, timeout=5)[0]) or 1.0
            if abs(self._dpr - 1.0) > 0.01:
                reason = "stopped: set the page zoom (and screen scaling) to 100% (Ctrl+0)"
                return
            focus_script = self._keep_focus(client, tab, True)
            if settings.battle_speed > 1:
                speed_script = self._speed_hook(client, tab)
                self.log(f"Bot: the game runs at {settings.battle_speed}× speed. "
                         "If it does not speed up, reload the game once (F5) while the bot runs. "
                         "Switch off any other speed-hack add-on.")
            self.log("Bot started: " + " → ".join(bitheroes.MODES[m] for m in settings.modes) + ".")
            pending = None      # a look already taken (while waiting for a click to take effect)
            last_seen = None    # the screen of the previous look: act only on a screen seen twice
            while not self._stop.is_set():
                if self._wait_visible(client, tab) is None:
                    break
                now = time.monotonic()
                if pending:
                    img, screen, sample = pending
                    pending = None
                else:
                    try:
                        img, screen, sample = self._bot_look(client, tab)
                    except (BiDiError, ScriptError, TimeoutError, ValueError) as exc:
                        # A hung page may not even give a screenshot: that counts as not moving.
                        self.log(f"Bot: could not take a screenshot ({exc}).")
                        img, screen, sample = None, None, frame
                if sample != frame:
                    frame, frame_since = sample, now
                if (screen is None or screen in bitheroes.WAITING) and now - frame_since > bitheroes.FROZEN_S:
                    # Battles and loading screens always move; a still picture means the game hung.
                    if now - last_reload < 5 * 60:
                        self.log(f"Bot: the game froze again (not moving for {bitheroes.FROZEN_S} s).")
                        reloaded = False
                    else:
                        self.log(f"Bot: the game has not moved for {bitheroes.FROZEN_S} s (frozen); reloading it.")
                        reloaded = self._reload_game(client, tab)
                    if not reloaded:
                        # A hung page does not even reload: start afresh in a new window.
                        self.log("Bot: closing the frozen game and opening it again in a new window.")
                        tab = self._reopen_game(client, tab)
                        focus_script = self._keep_focus(client, tab, True, focus_script)
                        if speed_script:
                            speed_script = self._speed_hook(client, tab, speed_script)
                    speed_now = None
                    last_reload = time.monotonic()
                    brain.last_known = frame_since = time.monotonic() + bitheroes.LOAD_S  # time to load
                    continue
                if img is None:
                    self._stop.wait(3)
                    continue
                if screen != last_seen:
                    # Just appeared: look once more before acting, so a window that is
                    # still sliding or fading in is not clicked too early.
                    last_seen = screen
                    if screen is not None:
                        self._stop.wait(bitheroes.LOOK_S)
                        continue
                if speed_script and speed_now is None:
                    # At the start and after a reload (a fresh page starts at 1×);
                    # a failure is logged once, not on every look.
                    self._set_speed(client, tab, settings.battle_speed)
                    speed_now = settings.battle_speed
                d = brain.decide(screen, now, **bitheroes.look_closer(img, screen))
                self.notify("bot", {"status": brain.status(now), "screen": screen, "runs": dict(brain.runs)})
                self.notify("status", "bot: " + brain.status(now) + (f" · {screen.replace('_', ' ')}" if screen else ""))
                if screen is not None and screen not in bitheroes.WAITING:
                    saved_unknown = False
                elif now - brain.last_known > 180 and not saved_unknown and shots_dir:
                    saved_unknown = True
                    self._save_unknown(img, shots_dir)
                if screen is None and now - brain.last_known > 15 * 60:
                    self.log("Bot: no known screen for 15 minutes; reloading the game.")
                    self._reload_game(client, tab)
                    speed_now = None
                    brain.last_known = frame_since = time.monotonic() + bitheroes.LOAD_S
                    continue
                if d.key:
                    last_click, tries, last_wait = None, 0, ""
                    self.log(f"Bot: {d.what}.")
                    self._press_key(client, tab, d.key)
                    pending = self._await_change(client, tab, screen, d.settle)
                    continue
                if d.point is None:
                    last_click, tries = None, 0
                    if d.what and d.what != last_wait:
                        self.log(f"Bot: {d.what}.")
                    last_wait = d.what
                    self._stop.wait(1.0 if brain.mode else 5.0)
                    continue
                last_wait = ""
                key = (screen, d.point, d.what if d.stays else "")   # ◀ ▶: progress shows in the text
                tries = tries + 1 if key == last_click else 0
                last_click = key
                if tries == 0:
                    self.log(f"Bot: {d.what}.")
                elif tries >= 6:
                    self.log(f"Bot: still on “{screen.replace('_', ' ')}” after {tries} clicks; waiting a minute.")
                    tries = 0
                    if self._stop.wait(60):
                        break
                    continue
                elif tries >= 2:
                    note = self._reset_input(client, tab, True)
                    self.log(f"Bot: the click did not register; resetting the input and clicking again"
                             + (f" [{note}]" if note else "") + ".")
                click = Step(delay_ms=0, x=d.point[0], y=d.point[1], hold_ms=80)
                self._replay_step(client, tab, click, 80, PlayOptions(restore_scroll=False), 0)
                self._park_pointer(client, tab)
                if d.stays:
                    self._stop.wait(d.settle)
                    continue
                pending = self._await_change(client, tab, screen, d.settle)
        except ConnectionLost:
            reason = "connection lost"
        except Exception as exc:  # noqa: BLE001
            reason = f"error: {exc}"
        finally:
            if client and client.connected:
                try:
                    self._last_ptr = None
                    client.send("input.releaseActions", {"context": tab}, timeout=5)
                    if focus_script is not None:
                        self._keep_focus(client, tab, False, focus_script)
                except Exception:  # noqa: BLE001
                    pass
                if speed_script is not None:
                    self._speed_hook(client, tab, speed_script, on=False)
                    self._set_speed(client, tab, 1)
            self.notify("status", "")
            self.notify("bot", {"status": "stopped", "screen": None, "runs": dict(brain.runs)})
            self._set_mode("idle")
            done = ", ".join(f"{bitheroes.MODES[m]} {n}" for m, n in brain.runs.items() if n)
            self.log(f"Bot {reason}." + (f" Runs: {done}." if done else ""))

    def _await_change(self, client: BiDiClient, tab: str, screen: Optional[str], settle: float) -> Optional[tuple]:
        """After a click or key: look again quickly until the screen changes
        (it worked) and return that look. Some windows load data from the
        server first, so only after RECLICK_S without any change is the click
        taken as missed (and repeated)."""
        until = time.monotonic() + max(settle, bitheroes.RECLICK_S)
        while not self._stop.wait(bitheroes.LOOK_S) and time.monotonic() < until:
            try:
                look = self._bot_look(client, tab)
            except (BiDiError, ScriptError, TimeoutError, ValueError):
                return None
            if look[1] != screen:
                return look
        return None

    def _press_key(self, client: BiDiClient, tab: str, key: str) -> None:
        """Press and release a key in the game. Keys sent to the tab do not
        reach a cross-site frame, so they go to the game's frame itself: the
        innermost frame that has focus, else the one with a canvas."""
        target = tab
        contexts = self._contexts_under(tab)
        results = client.send_many([("script.callFunction", client.call_params(c, js.KEY_TARGET, sandbox=SANDBOX))
                                    for c in contexts], timeout=5)
        found = {}
        for c, r in zip(contexts, results):
            try:
                found[c] = client.script_result(r) if isinstance(r, dict) else None
            except ScriptError:
                found[c] = None
        for kind in ("focus", "canvas"):
            hit = [c for c in contexts if found.get(c) == kind]
            if hit:
                target = hit[-1]
                break
        try:
            client.send("input.performActions", {"context": target, "actions": [{
                "type": "key", "id": "tab-autoclicker-keys",
                "actions": [{"type": "keyDown", "value": key}, {"type": "pause", "duration": 80},
                            {"type": "keyUp", "value": key}]}]}, timeout=10)
        except (BiDiError, TimeoutError) as exc:
            self.log(f"Bot: could not press the key ({exc}).")

    def _speed_hook(self, client: BiDiClient, tab: str, script: Optional[str] = None,
                    on: bool = True) -> Optional[str]:
        """Bot: install the speed control (agent_js.SPEED) in the game's tab,
        also for pages loaded later (reloads). Returns the preload script."""
        if script:
            try:
                client.send("script.removePreloadScript", {"script": script}, timeout=5)
            except Exception:  # noqa: BLE001
                pass
        if not on:
            return None
        try:
            return client.send("script.addPreloadScript", {
                "functionDeclaration": "() => { (" + js.SPEED + ")(0); }", "contexts": [tab]})["script"]
        except (BiDiError, TimeoutError) as exc:
            self.log(f"Bot: could not set up the game speed ({exc}).")
            return None

    def _set_speed(self, client: BiDiClient, tab: str, factor: int) -> bool:
        """Set the game's speed in the tab and all its frames (in the page's own realm)."""
        results = client.send_many([("script.callFunction", client.call_params(c, js.SPEED, factor, sandbox=None))
                                    for c in self._contexts_under(tab)], timeout=5)
        ok = any(isinstance(r, dict) and r.get("type") == "success" for r in results)
        if not ok:
            self.log(f"Bot: could not set the game speed to {factor}×.")
        return ok

    def _bot_look(self, client: BiDiClient, tab: str) -> tuple:
        """One screenshot of the game: (image, screen name or None, a fingerprint)."""
        img = imaging.decode_png(self._capture(client, tab))
        return img, bitheroes.identify(img), hash(img.rgb[::97])

    def _park_pointer(self, client: BiDiClient, tab: str) -> None:
        """Move the mouse off the button just clicked: buttons light up while
        hovered, which would change how they look in the next screenshot.
        Not at once: the game reads the mouse once per frame, and a release
        it sees away from the button does not count as a click."""
        self._stop.wait(0.3)
        x, y = bitheroes.PARK
        try:
            client.send("input.performActions", {"context": tab, "actions": [{
                "type": "pointer", "id": POINTER_ID, "parameters": {"pointerType": "mouse"},
                "actions": [{"type": "pointerMove", "x": x, "y": y, "duration": 0}]}]}, timeout=10)
            self._last_ptr = (tab, x, y)
        except (BiDiError, TimeoutError):
            self._last_ptr = None

    def _reload_game(self, client: BiDiClient, tab: str) -> bool:
        """Reload the game's tab. A page whose script hangs does not reload
        (Firefox never answers), so this gives up after 10 s and returns False."""
        try:
            client.send("browsingContext.reload", {"context": tab}, timeout=10)
        except (BiDiError, TimeoutError):
            self.log("Bot: the frozen page does not reload.")
            return False
        self._stop.wait(20)
        return True

    def _reopen_game(self, client: BiDiClient, tab: str) -> str:
        """Close the hung ``tab`` (which ends its stuck page process), open the
        game's address in a new window of the bot's size, and make it the
        target. Closing comes first: a new tab of the same site would be put
        in the same stuck process. Returns the new tab."""
        url = self.urls.get(tab) or (self.target.url if self.target else "")
        try:
            client.send("browsingContext.close", {"context": tab, "promptUnload": False}, timeout=30)
            self._stop.wait(2)
            new = client.send("browsingContext.create", {"type": "window"}, timeout=30)["context"]
            with self._lock:
                self.parents.setdefault(new, None)
            client.send("browsingContext.navigate", {"context": new, "url": url, "wait": "none"}, timeout=30)
        except (BiDiError, TimeoutError) as exc:
            self.log(f"Bot: could not reopen the game ({exc}).")
            return tab
        tab_info = TabInfo(new, "", url)
        self.target = tab_info
        self._last_ptr = None
        self.notify("target", tab_info)
        try:
            self.resize_page(*bitheroes.PAGE_SIZE, client=client)
        except (EngineError, BiDiError, ScriptError, TimeoutError) as exc:
            self.log(f"Bot: could not resize the new window: {exc}")
        self._stop.wait(20)
        return new

    def _save_unknown(self, img: imaging.Image, shots_dir: str) -> None:
        """Keep a screenshot of a screen the bot does not know (to teach it later)."""
        try:
            import os
            from PIL import Image as PILImage
            os.makedirs(shots_dir, exist_ok=True)
            path = os.path.join(shots_dir, time.strftime("unknown-%Y%m%d-%H%M%S.png"))
            PILImage.frombytes("RGB", (img.width, img.height), img.rgb).save(path)
            self.log(f"Bot: this screen has not been recognised for 3 minutes; saved it as {path}.")
        except Exception:  # noqa: BLE001 - only a diagnostic
            pass

    def _retry_click(self, client: BiDiClient, tab: str, step: Step, opts: PlayOptions,
                     speed: float, number: int) -> Optional[int]:
        """After a check's click: while the image is still there, click it again
        (up to ``step.retry_clicks`` times). Returns the extra clicks, or None if stopped."""
        extra = 0
        for attempt in range(1, step.retry_clicks + 2):
            if self._stop.wait(max(100, step.retry_ms) / 1000.0):
                return None
            if opts.game_mode and self._wait_visible(client, tab) is None:
                return None
            try:
                score, cx, cy = self._match(client, tab, step, step.threshold)
            except (BiDiError, ScriptError, TimeoutError, ValueError):
                return extra
            if score < step.threshold:
                if attempt > 1:
                    self.log(f"Check #{number} ({step.title}): the image is gone after {attempt} clicks.")
                return extra
            if attempt > step.retry_clicks:
                self.log(f"Check #{number} ({step.title}): still there after {attempt} clicks; going on anyway.")
                return extra
            note = self._reset_input(client, tab, opts.game_mode)
            self.log(f"Check #{number} ({step.title}): still there after the click ({score:.0f}% match) "
                     f"→ resetting the input and clicking again ({attempt + 1} of {step.retry_clicks + 1})"
                     + (f" [{note}]" if note else "") + ".")
            click = Step(delay_ms=0, x=cx, y=cy, hold_ms=80)
            if self._replay_step(client, tab, click, self._hold(click, opts, speed),
                                 replace(opts, aim="position", restore_scroll=False), number):
                extra += 1
        return extra

    @staticmethod
    def _hold(step: Step, opts: PlayOptions, speed: float) -> int:
        if opts.timing == "fixed":
            hold = min(step.hold_ms, opts.interval_ms * 0.5)
        else:
            hold = step.hold_ms / speed
        if opts.game_mode:
            hold = max(hold, GAME_MIN_HOLD_MS)
        return int(max(0, min(hold, 30000)))

    def _run_check(self, client: BiDiClient, tab: str, step: Step, opts: PlayOptions,
                   number: int) -> Optional[tuple[bool, Optional[tuple[float, float]], float]]:
        """Look for the image until it is found or the timeout runs out.
        Returns (passed, click point, score %) or None if playback was stopped."""
        if not step.box or not step.image:
            self.log(f"Check #{number} has no image; treated as not found.")
            return False, None, 0.0
        deadline = time.monotonic() + max(0, step.timeout_ms) / 1000.0
        want = step.wait_for != "disappear"
        waiting = False
        score, point = 0.0, None
        if opts.restore_scroll:
            try:
                if client.call(tab, js.RESTORE_SCROLL, step.scroll_x, step.scroll_y, sandbox=SANDBOX, timeout=5):
                    time.sleep(0.05)
            except (BiDiError, ScriptError, TimeoutError):
                pass
        while True:
            try:
                score, cx, cy = self._match(client, tab, step, step.threshold if want else 0)
                point = (cx, cy)
            except (BiDiError, ScriptError, TimeoutError, ValueError) as exc:
                self.log(f"Check #{number}: could not take a screenshot ({exc}).")
                score, point = 0.0, None
            present = score >= step.threshold
            if present == want:
                if waiting:
                    self.notify("status", "")
                return True, (point if present else None), score
            if time.monotonic() >= deadline:
                if waiting:
                    self.notify("status", "")
                what = "not found" if want else "still there"
                self.log(f"Check #{number} ({step.title}): {what} "
                         f"(best match {score:.0f}%, needs {step.threshold}%) → {step.describe(step.on_missing)}.")
                return False, None, score
            if not waiting and step.timeout_ms > 0:
                waiting = True
                self.notify("status", f"check #{number}: waiting for “{step.title}”"
                            + (" to go away" if not want else ""))
            if self._stop.wait(CHECK_POLL_S):
                return None
            if opts.game_mode:
                paused_at = time.monotonic()
                waited = self._wait_visible(client, tab)
                if waited is None:
                    return None
                if waited:  # time spent paused does not count against the timeout
                    deadline += time.monotonic() - paused_at

    def _replay_step(self, client: BiDiClient, tab: str, step: Step, hold: int,
                     opts: PlayOptions, number: int) -> bool:
        x, y = step.x, step.y
        dx, dy = step.up_x - step.x, step.up_y - step.y
        aimed = False
        try:
            if opts.aim == "element" and step.selector and not step.in_frame:
                pt = client.call(tab, js.ELEMENT_POINT, step.selector, step.fx, step.fy,
                                 sandbox=SANDBOX, timeout=5)
                if pt:
                    x, y = pt
                    aimed = True
                else:
                    self.log(f"Click #{number}: element not found, using the recorded position.")
            if not aimed and opts.restore_scroll:
                if client.call(tab, js.RESTORE_SCROLL, step.scroll_x, step.scroll_y, sandbox=SANDBOX, timeout=5):
                    time.sleep(0.05)
            ctx, lx, ly = self._descend(client, tab, x, y)
            self._dispatch_click(client, ctx, lx, ly, lx + dx, ly + dy, step.button, hold, step.is_drag)
        except (BiDiError, ScriptError, TimeoutError) as exc:
            msg = str(exc)
            if "move target out of bounds" in msg:
                msg = "position is outside the visible page area"
            self.log(f"Click #{number} skipped: {msg}")
            return False
        if opts.markers:
            client.send_nowait("script.callFunction", client.call_params(
                tab, js.MARKER, x, y, "rgba(230,40,60,.95)", sandbox=SANDBOX))
        return True

    def _descend(self, client: BiDiClient, ctx: str, x: float, y: float) -> tuple[str, float, float]:
        """Find the innermost frame at (x, y); return it and the local position."""
        for _ in range(10):
            try:
                hit = client.call(ctx, js.FRAME_AT_POINT, x, y, sandbox=SANDBOX, timeout=5)
            except (BiDiError, ScriptError):
                break
            if not hit or not isinstance(hit[0], WindowRef):
                break
            ctx, x, y = hit[0].context, hit[1], hit[2]
        return ctx, x, y

    def _dispatch_click(self, client: BiDiClient, ctx: str, x: float, y: float, ux: float, uy: float,
                        button: int, hold: int, drag: bool) -> None:
        x, y, ux, uy = int(round(x)), int(round(y)), int(round(ux)), int(round(uy))
        actions = []
        # Re-sending a move to the same spot would reset Firefox's click counter
        # and break recorded double clicks, so only move when needed.
        if self._last_ptr != (ctx, x, y):
            actions.append({"type": "pointerMove", "x": x, "y": y, "duration": 0})
        actions.append({"type": "pointerDown", "button": button})
        if drag:
            actions.append({"type": "pointerMove", "x": ux, "y": uy, "duration": max(hold, 60)})
        elif hold > 0:
            actions.append({"type": "pause", "duration": hold})
        actions.append({"type": "pointerUp", "button": button})
        self._last_ptr = None  # unknown if the command fails half-way
        client.send("input.performActions", {"context": ctx, "actions": [{
            "type": "pointer", "id": POINTER_ID, "parameters": {"pointerType": "mouse"},
            "actions": actions}]}, timeout=30 + hold / 1000.0)
        self._last_ptr = (ctx, ux, uy) if drag else (ctx, x, y)
