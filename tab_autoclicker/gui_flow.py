"""Flow chart of the steps: see, and change, how playback moves between them.

The chart is always drawn from ``app.steps`` (the same list as the table), so
both views stay in sync. ``build_graph`` has no Tk code and is easy to test.
"""

from __future__ import annotations

import math
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk
from typing import Optional

from .engine import CLICK_ACTIONS, PRESET_ACTIONS, STEP_ACTIONS, Step

MAIN_W, SIDE_W, PRESET_W = 270, 250, 200
LANE_GAP, COL_GAP, ROW_GAP = 14, 22, 30
LEFT = 44                        # room for the "repeat" arrow on the left
FOUND_C, ELSE_C, SEQ_C, BACK_C = "#2e9d4f", "#d9730d", "#5f6368", "#7a5cb0"


@dataclass
class Node:
    key: str                     # "start", "end", "stop", "s<index>", "p:<preset>" ("p:" = next preset)
    kind: str                    # start / end / stop / step / preset
    title: str
    sub: str = ""
    index: Optional[int] = None  # step index for kind == "step"
    col: int = 0                 # 0 = normal order, 1 = "only when jumped to" (+ Stop), 2 = presets
    row: int = 0
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0


@dataclass
class Edge:
    src: str
    dst: str
    label: str = ""
    port: str = "out"            # out / found / else
    color: str = SEQ_C
    dashed: bool = False
    both: bool = False           # a check whose found and not-found outcomes go the same way


def is_step(key: str) -> bool:
    """Node keys of steps are "s<index>" (not to be confused with "start"/"stop")."""
    return key[:1] == "s" and key[1:].isdigit()


def _main_from(steps: list[Step], j: int) -> str:
    while j < len(steps) and steps[j].only_on_jump:
        j += 1
    return f"s{j}" if j < len(steps) else "end"


def after(steps: list[Step], i: int) -> str:
    """Where playback goes after step i when nothing jumps: a step, "end", or
    "back" (the end of an "only when jumped to" block returns to its caller)."""
    if steps[i].only_on_jump:
        return f"s{i + 1}" if i + 1 < len(steps) and steps[i + 1].only_on_jump else "back"
    return _main_from(steps, i + 1)


def _block_end(steps: list[Step], i: int) -> int:
    while i + 1 < len(steps) and steps[i + 1].only_on_jump:
        i += 1
    return i


def action_target(steps: list[Step], i: int, action: str) -> str:
    st = steps[i]
    if action in ("continue", "click"):
        return after(steps, i)
    if action == "skip":
        nxt = after(steps, i)
        return after(steps, int(nxt[1:])) if is_step(nxt) else nxt
    if action in STEP_ACTIONS:
        t = next((n for n, s in enumerate(steps) if s.uid == st.goto_step), None)
        return f"s{t}" if t is not None else "stop"
    if action == "restart":
        return "end"
    if action in PRESET_ACTIONS:
        return "p:" + st.goto_preset
    return "stop"


def _outcome_label(st: Step, found: bool, action: str) -> str:
    appear = st.wait_for != "disappear"
    text = ("found" if appear else "gone") if found else ("not found" if appear else "still there")
    if action in CLICK_ACTIONS and found and appear:
        text += " · click" + (f" after {st.click_delay_ms / 1000:g} s" if st.click_delay_ms else "")
    if found and st.after_ms and action != "stop":
        text += f" · then wait {st.after_ms / 1000:g} s"
    if action == "skip":
        text += " · skip"
    if action == "restart":
        text += " · next repeat"
    return text


def build_graph(steps: list[Step], repeat_label: str, presets: list[str]) -> tuple[dict, list]:
    """Nodes (by key) and edges of the flow chart."""
    nodes: dict[str, Node] = {"start": Node("start", "start", "▶  Start", row=0)}
    for i, st in enumerate(steps):
        if st.is_check:
            limit = f"up to {st.timeout_ms / 1000:g} s" if st.timeout_ms > 0 else "look once"
            title = f"#{i + 1}  {st.action}"
            sub = f"“{st.name}” · {limit}"
        else:
            title = f"#{i + 1}  {st.action}"
            sub = f"{st.x:.0f}, {st.y:.0f}" + (f" · wait {st.delay_ms / 1000:g} s" if st.delay_ms else "")
        nodes[f"s{i}"] = Node(f"s{i}", "step", title, sub, index=i, col=1 if st.only_on_jump else 0, row=i + 1)
    end_row = len(steps) + 1
    nodes["end"] = Node("end", "end", "End of round" if repeat_label else "■  Done", repeat_label, row=end_row)
    nodes["stop"] = Node("stop", "stop", "■  Stop playback", col=1, row=end_row)
    names = list(presets)
    used = {"p:" + s.goto_preset for s in steps if s.is_check and
            (s.on_found in PRESET_ACTIONS or s.on_missing in PRESET_ACTIONS)}
    keys = ["p:"] + ["p:" + n for n in names]
    keys += sorted(k for k in used if k not in keys)  # presets that were deleted since
    for r, key in enumerate(keys):
        name = key[2:]
        nodes[key] = Node(key, "preset", f"▶  Preset “{name}”" if name else "▶  Next preset",
                          "" if not name or name in names else "(deleted)", col=2, row=r + 1)

    edges: list[Edge] = []
    edges.append(Edge("start", _main_from(steps, 0)))
    callers: dict[int, list[int]] = {}   # block end -> checks that jump into that block
    for i, st in enumerate(steps):
        if not st.is_check:
            edges.append(Edge(f"s{i}", after(steps, i)))
            continue
        for found in (True, False):
            action = st.on_found if found else st.on_missing
            dst = action_target(steps, i, action)
            if action in STEP_ACTIONS and is_step(dst) and steps[int(dst[1:])].only_on_jump:
                callers.setdefault(_block_end(steps, int(dst[1:])), []).append(i)
            label = _outcome_label(st, found, action)
            if action in STEP_ACTIONS and dst == "stop":
                label += " · deleted step!"
            edges.append(Edge(f"s{i}", dst, label, "found" if found else "else", FOUND_C if found else ELSE_C))
        a, b = edges[-2], edges[-1]
        if a.dst == b.dst:  # both outcomes go the same way: one arrow, e.g. "click if found"
            (fa, *pa), (fb, *pb) = a.label.split(" · "), b.label.split(" · ")
            only_a, only_b = [p for p in pa if p not in pb], [p for p in pb if p not in pa]
            label = " · ".join([p for p in pa if p in pb]
                               + ([", ".join(only_a) + f" if {fa}"] if only_a else [])
                               + ([", ".join(only_b) + f" if {fb}"] if only_b else []))
            edges[-2:] = [Edge(a.src, a.dst, label, "out", both=True)]

    def resolve_back(block_end: int, seen: frozenset) -> set[str]:
        """Where the branch ending at ``block_end`` goes back to. Jumps from inside a
        branch stay in it, so they go back wherever their own branch goes back."""
        out: set[str] = set()
        for c in callers.get(block_end, []):
            if steps[c].only_on_jump:
                own = _block_end(steps, c)
                if own != block_end and own not in seen:
                    out |= resolve_back(own, seen | {own})
            else:
                out.add(after(steps, c))
        return out

    final: list[Edge] = []
    for e in edges:
        if e.dst != "back":
            final.append(e)
            continue
        i = int(e.src[1:])
        for dst in sorted(resolve_back(_block_end(steps, i), frozenset())):
            final.append(Edge(e.src, dst, ("back · " + e.label) if e.label else "back", e.port,
                              e.color if e.port != "out" else BACK_C, dashed=True, both=e.both))
    if repeat_label:
        final.append(Edge("end", "start", repeat_label, color=SEQ_C))
    return nodes, final


class FlowWindow:
    """A non-modal window next to the main one."""

    def __init__(self, app):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.geometry("900x720")
        self.win.minsize(520, 360)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        bar = ttk.Frame(self.win, padding=(10, 8))
        bar.pack(fill="x")
        ttk.Label(bar, wraplength=860, justify="left", style="Hint.TLabel", text=(
            "Click a box to select its row, double-click to edit it, right-click for more. "
            "Drag the green ● (found) or orange ● (not found) handle of an image check onto a box to change "
            "where it goes. Dashed arrows go back after an “only when jumped to” step.")).pack(anchor="w")
        frame = ttk.Frame(self.win)
        frame.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(frame, background="#fbfbfa", highlightthickness=0)
        ys = ttk.Scrollbar(frame, orient="vertical", command=self.canvas.yview)
        xs = ttk.Scrollbar(frame, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.status = tk.StringVar(value="")
        ttk.Label(self.win, textvariable=self.status, padding=(10, 4)).pack(fill="x")
        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        self.canvas.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-2, "units"))
        self.canvas.bind("<Button-5>", lambda e: self.canvas.yview_scroll(2, "units"))
        self.canvas.tag_bind("node", "<Button-1>", self._click)
        self.canvas.tag_bind("node", "<Double-1>", self._double)
        self.canvas.tag_bind("node", "<Button-3>", self._menu)
        self.canvas.tag_bind("port", "<ButtonPress-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []
        self.thumbs: dict[str, tk.PhotoImage] = {}
        self.current: Optional[int] = None
        self.drag: Optional[tuple[int, str]] = None
        self._pending = False
        self.refresh()

    # ------------------------------------------------------------- lifecycle

    def close(self) -> None:
        self.app.flow = None
        self.win.destroy()

    def lift(self) -> None:
        self.win.deiconify()
        self.win.lift()

    def refresh(self) -> None:
        """Redraw soon (several changes in a row cause one redraw)."""
        if not self._pending:
            self._pending = True
            self.win.after_idle(self._redraw)

    def set_current(self, index: Optional[int]) -> None:
        if index == self.current:
            return
        self.current = index
        self._paint_states()

    def sync_selection(self) -> None:
        self._paint_states()

    # ---------------------------------------------------------------- layout

    def _repeat_label(self) -> str:
        app = self.app
        sleep = app._pause_ms()
        sleep = f" · sleep {sleep / 1000:g} s" if sleep else ""
        if app.forever_var.get():
            return "repeat forever" + sleep
        n = app._int(app.repeat_var, 1)
        return f"repeat ×{n}{sleep}" if n > 1 else ""

    def _redraw(self) -> None:
        self._pending = False
        app = self.app
        name = app._preset_name
        self.win.title("Flow chart — " + (f"preset “{name}”" if name else "unsaved steps"))
        self.nodes, self.edges = build_graph(app.steps, self._repeat_label(), app._preset_names())
        c = self.canvas
        c.delete("all")
        self._layout()
        self.port_pos: dict[tuple[str, str], tuple[float, float]] = {}
        for e in self.edges:
            self._draw_edge(e)
        for n in self.nodes.values():
            self._draw_node(n)
        self._draw_ports()
        self._paint_states()
        box = c.bbox("all") or (0, 0, 100, 100)
        c.configure(scrollregion=(0, 0, box[2] + 30, box[3] + 30))

    def _layout(self) -> None:
        heights: dict[int, float] = {}
        for n in self.nodes.values():
            if n.kind == "step":
                n.h = 62 if self.app.steps[n.index].is_check else 46
            elif n.kind == "preset":
                n.h = 32
            else:
                n.h = 34
            if n.col < 2:
                heights[n.row] = max(heights.get(n.row, 0), n.h)
        y, row_y = 20.0, {}
        for r in sorted(heights):
            row_y[r] = y + heights[r] / 2
            y += heights[r] + ROW_GAP
        lanes = sum(1 for e in self.edges if not self._straight(e) and e.src != "end")
        x_side = LEFT + MAIN_W + COL_GAP + lanes * LANE_GAP + COL_GAP
        x_preset = x_side + SIDE_W + 70
        for n in self.nodes.values():
            if n.col == 2:
                n.x, n.w, n.y = x_preset, PRESET_W, 20 + (n.row - 1) * (n.h + 12) + n.h / 2
            else:
                n.x, n.w = (LEFT, MAIN_W) if n.col == 0 else (x_side, SIDE_W)
                n.y = row_y[n.row]
        self.lane_x0 = LEFT + MAIN_W + COL_GAP
        # Short arrows get the lanes nearest the boxes, so fewer lines cross.
        routed = [e for e in self.edges if not self._straight(e) and e.src != "end"]
        routed.sort(key=lambda e: abs(self.nodes[e.dst].y - self.nodes[e.src].y) if e.dst in self.nodes else 0)
        self.lane_of = {id(e): k for k, e in enumerate(routed)}

    def _straight(self, e: Edge) -> bool:
        """Neighbours in one column (nothing of that column in between): a plain line down."""
        a, b = self.nodes.get(e.src), self.nodes.get(e.dst)
        if not a or not b or a.col != b.col or b.row <= a.row or a.col == 2:
            return False
        return not any(n.col == a.col and a.row < n.row < b.row for n in self.nodes.values())

    # --------------------------------------------------------------- drawing

    def _port_y(self, n: Node, port: str) -> float:
        return n.y + {"out": 0, "found": -n.h * 0.22, "else": n.h * 0.22}[port]

    def _draw_edge(self, e: Edge) -> None:
        c = self.canvas
        a, b = self.nodes.get(e.src), self.nodes.get(e.dst)
        if not a or not b:
            return
        dash = (5, 4) if e.dashed else None
        width = 2 if e.port != "out" or e.dashed else 1.6
        if e.src == "end":  # the repeat arrow, on the left
            x = LEFT - 24
            pts = [a.x, a.y, x, a.y, x, b.y, b.x, b.y]
            c.create_line(*pts, arrow="last", fill=e.color, width=1.6, dash=dash, tags="edge")
            c.create_text(x - 4, (a.y + b.y) / 2, text=e.label, angle=90, fill=e.color,
                          font=("TkDefaultFont", 8), tags="edge")
            return
        if self._straight(e):
            x = a.x + a.w / 2 + (-40 if e.port == "else" else 40 if e.port == "found" else 0)
            c.create_line(x, a.y + a.h / 2, x, b.y - b.h / 2, arrow="last", fill=e.color, width=width,
                          dash=dash, tags="edge")
            self._remember_ports(e, x, a.y + a.h / 2, horizontal=True)
            if e.label:
                self._label(x + (16 if e.both else 6), a.y + a.h / 2 + 3, e.label, e.color, "nw")
            return
        lane = self.lane_x0 + self.lane_of.get(id(e), 0) * LANE_GAP
        y0, y1 = self._port_y(a, e.port), b.y
        x0 = a.x + a.w if lane > a.x else a.x
        x1 = b.x if b.x > lane else b.x + b.w
        c.create_line(x0, y0, lane, y0, lane, y1, x1, y1, arrow="last", fill=e.color, width=width, dash=dash,
                      tags="edge")
        self._remember_ports(e, x0, y0, horizontal=False)
        if e.label:
            right = lane > a.x
            self._label(x0 + (4 if right else -4), y0 - 2, e.label, e.color, "sw" if right else "se")

    def _remember_ports(self, e: Edge, x: float, y: float, horizontal: bool) -> None:
        """The drag handles sit where their arrow starts."""
        n = self.nodes.get(e.src)
        if n is None or n.kind != "step" or not self.app.steps[n.index].is_check:
            return
        if e.both:  # one arrow for both outcomes: two handles side by side
            dx, dy = (8, 0) if horizontal else (0, 8)
            self.port_pos.setdefault((e.src, "found"), (x + dx, y - dy))
            self.port_pos.setdefault((e.src, "else"), (x - dx, y + dy))
        elif e.port in ("found", "else"):
            self.port_pos.setdefault((e.src, e.port), (x, y))

    def _draw_ports(self) -> None:
        c = self.canvas
        for n in self.nodes.values():
            if n.kind != "step" or not self.app.steps[n.index].is_check:
                continue
            for port, color in (("found", FOUND_C), ("else", ELSE_C)):
                px, py = self.port_pos.get((n.key, port), (n.x + n.w, self._port_y(n, port)))
                c.create_oval(px - 6, py - 6, px + 6, py + 6, fill=color, outline="#ffffff", width=2,
                              tags=("port", f"port:{n.index}:{port}"))

    def _label(self, x: float, y: float, text: str, color: str, anchor: str) -> None:
        t = self.canvas.create_text(x, y, text=text, fill=color, anchor=anchor, font=("TkDefaultFont", 8),
                                    tags="edge")
        bx = self.canvas.bbox(t)
        r = self.canvas.create_rectangle(bx[0] - 2, bx[1], bx[2] + 2, bx[3], fill="#fbfbfa", outline="",
                                         tags="edge")
        self.canvas.tag_lower(r, t)

    def _rounded(self, x0, y0, x1, y1, r, **kw) -> int:
        pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
               x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
        return self.canvas.create_polygon(pts, smooth=True, **kw)

    def _draw_node(self, n: Node) -> None:
        c = self.canvas
        tags = ("node", f"n:{n.key}")
        x0, y0, x1, y1 = n.x, n.y - n.h / 2, n.x + n.w, n.y + n.h / 2
        st = self.app.steps[n.index] if n.kind == "step" else None
        if n.kind == "step":
            fill, outline = ("#f1ebfa", "#7a5cb0") if st.only_on_jump else \
                ("#e8f0fb", "#4a6fa5") if st.is_check else ("#ffffff", "#8a94a6")
            self._rounded(x0, y0, x1, y1, 10, fill=fill, outline=outline, width=1.5, tags=tags + ("body",))
        elif n.kind == "preset":
            self._rounded(x0, y0, x1, y1, 14, fill="#e6f4ea", outline="#2e9d4f", width=1.5, tags=tags + ("body",))
        elif n.kind == "stop":
            self._rounded(x0, y0, x1, y1, 16, fill="#fdecea", outline="#c5221f", width=1.5, tags=tags + ("body",))
        else:
            self._rounded(x0, y0, x1, y1, 16, fill="#eceff1", outline="#8a94a6", width=1.5, tags=tags + ("body",))
        tx = x0 + 12
        if st is not None and st.is_check and st.image:
            img = self._thumb(st)
            if img is not None:
                c.create_image(x0 + 8, n.y, image=img, anchor="w", tags=tags)
                tx = x0 + 16 + img.width()
        if st is not None and st.only_on_jump:
            c.create_text(x1 - 8, y0 + 4, text="↪ only when jumped to", anchor="ne", fill="#7a5cb0",
                          font=("TkDefaultFont", 7), tags=tags)
        if n.sub:
            c.create_text(tx, n.y - 8, text=n.title, anchor="w", font=("TkDefaultFont", 9, "bold"),
                          fill="#202124", tags=tags, width=x1 - tx - 8)
            c.create_text(tx, n.y + 10, text=n.sub, anchor="w", font=("TkDefaultFont", 8), fill="#5f6368",
                          tags=tags, width=x1 - tx - 8)
        else:
            c.create_text(tx if n.kind == "step" else (x0 + x1) / 2, n.y, text=n.title,
                          anchor="w" if n.kind == "step" else "center", font=("TkDefaultFont", 9, "bold"),
                          fill="#202124", tags=tags)

    def _thumb(self, st: Step) -> Optional[tk.PhotoImage]:
        img = self.thumbs.get(st.image)
        if img is None:
            try:
                img = tk.PhotoImage(master=self.win, data=st.image)
            except tk.TclError:
                return None
            k = math.ceil(max(img.width() / 44, img.height() / 44, 1))
            if k > 1:
                img = img.subsample(k)
            self.thumbs[st.image] = img
        return img

    def _paint_states(self) -> None:
        """Selected rows get a thick outline, the running step a yellow fill."""
        c = self.canvas
        try:
            selected = set(self.app._selected_indexes())
        except tk.TclError:
            selected = set()
        for n in self.nodes.values():
            if n.kind != "step":
                continue
            body = [i for i in c.find_withtag(f"n:{n.key}") if "body" in c.gettags(i)]
            if not body:
                continue
            st = self.app.steps[n.index] if n.index < len(self.app.steps) else None
            if st is None:
                continue
            fill = "#fff1c2" if n.index == self.current else \
                "#f1ebfa" if st.only_on_jump else "#e8f0fb" if st.is_check else "#ffffff"
            c.itemconfigure(body[0], fill=fill, width=3 if n.index in selected else 1.5,
                            outline="#1a5fb4" if n.index in selected else
                            ("#7a5cb0" if st.only_on_jump else "#4a6fa5" if st.is_check else "#8a94a6"))
        if self.current is not None:
            n = self.nodes.get(f"s{self.current}")
            if n:
                self._see(n)

    def _see(self, n: Node) -> None:
        c = self.canvas
        region = c.cget("scrollregion").split()
        if len(region) != 4:
            return
        total = float(region[3]) or 1
        top, bottom = c.canvasy(0), c.canvasy(c.winfo_height())
        if n.y - n.h < top or n.y + n.h > bottom:
            c.yview_moveto(max(0.0, (n.y - c.winfo_height() / 2) / total))

    # ----------------------------------------------------------- interaction

    def _node_at_current(self) -> Optional[Node]:
        for t in self.canvas.gettags("current"):
            if t.startswith("n:"):
                return self.nodes.get(t[2:])
        return None

    def _click(self, event) -> None:
        n = self._node_at_current()
        if n and n.kind == "step":
            items = self.app.tree.get_children()
            if n.index < len(items):
                self.app.tree.selection_set([items[n.index]])
                self.app.tree.see(items[n.index])

    def _double(self, event) -> None:
        n = self._node_at_current()
        if n and n.kind == "step" and self.app.mode == "idle":
            self._click(event)
            self.app.edit_selected()
        elif n and n.kind == "preset" and n.key != "p:":
            self.app.choose_preset(n.key[2:])

    def _menu(self, event) -> None:
        n = self._node_at_current()
        if n is None or self.app.mode != "idle":
            return
        if n.kind == "step":
            self._click(event)
            self.app._step_menu([n.index], event.x_root, event.y_root)
        elif n.kind == "preset" and n.key != "p:" and not n.sub:
            menu = tk.Menu(self.win, tearoff=False)
            menu.add_command(label=f"Load preset “{n.key[2:]}”", command=lambda: self.app.choose_preset(n.key[2:]))
            menu.tk_popup(event.x_root, event.y_root)

    def _drag_start(self, event) -> None:
        if self.app.mode != "idle":
            self.status.set("Stop playback or recording before changing the flow.")
            return
        for t in self.canvas.gettags("current"):
            if t.startswith("port:"):
                _, i, port = t.split(":")
                self.drag = (int(i), port)
                x0, y0, x1, y1 = self.canvas.coords("current")
                self._drag_from = ((x0 + x1) / 2, (y0 + y1) / 2)
                self.status.set("Drop it on the box it should go to.")
                return

    def _drag_move(self, event) -> None:
        if not self.drag:
            return
        c = self.canvas
        c.delete("dragline")
        x, y = c.canvasx(event.x), c.canvasy(event.y)
        color = FOUND_C if self.drag[1] == "found" else ELSE_C
        c.create_line(*self._drag_from, x, y, arrow="last", fill=color, width=2, dash=(3, 3), tags="dragline")

    def _drag_end(self, event) -> None:
        if not self.drag:
            return
        i, port = self.drag
        self.drag = None
        c = self.canvas
        c.delete("dragline")
        x, y = c.canvasx(event.x), c.canvasy(event.y)
        target = None
        for item in reversed(c.find_overlapping(x - 1, y - 1, x + 1, y + 1)):
            for t in c.gettags(item):
                if t.startswith("n:"):
                    target = self.nodes.get(t[2:])
                    break
            if target:
                break
        self.status.set("")
        if target is None:
            return
        self.connect(i, port, target)

    def connect(self, i: int, port: str, target: Node) -> None:
        """Point outcome ``port`` of check ``i`` at ``target``. Whether the target runs
        only when jumped to is that step's own setting (right-click it)."""
        steps = self.app.steps
        st = steps[i]
        attr = "on_found" if port == "found" else "on_missing"
        clicky = port == "found" and getattr(st, attr) in CLICK_ACTIONS and st.wait_for != "disappear"

        def apply(action: str, **extra) -> None:
            setattr(st, attr, action)
            for k, v in extra.items():
                setattr(st, k, v)
            self.app._refresh_tree()
            what = "found" if port == "found" else "not found"
            self.app.log(f"Check #{i + 1} ({st.title}): when {what} → {st.describe(action, steps)}.")

        if target.kind == "end":
            apply("restart")
        elif target.kind == "stop":
            apply("stop")
        elif target.kind == "preset":
            apply("click_preset" if clicky else "preset", goto_preset=target.key[2:])
        elif target.kind in ("start", "step"):
            first = _main_from(steps, 0)
            j = target.index if target.kind == "step" else (int(first[1:]) if first != "end" else None)
            if j is None:
                return
            tgt = steps[j]
            if f"s{j}" == after(steps, i):  # simply the next step
                apply("click" if clicky else "continue")
            else:
                apply("click_step" if clicky else "step", goto_step=tgt.uid)
