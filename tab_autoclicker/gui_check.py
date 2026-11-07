"""The "Image check" dialog: choose a region of the target tab and what to do."""

from __future__ import annotations

import base64
import math
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable, Optional

from .engine import CHECK_ACTIONS, CLICK_ACTIONS, PRESET_ACTIONS, STEP_ACTIONS, Engine, Step, safe_url

FOUND_KEYS = ["click", "click_step", "click_preset", "continue", "skip", "step", "restart", "preset", "stop"]
MISSING_KEYS = ["stop", "continue", "skip", "step", "restart", "preset"]
NEXT_PRESET = "the next preset in the list"
MAX_VIEW = (900, 520)  # largest screenshot size shown in the dialog


class CheckDialog:
    """Modal dialog; ``result`` is the finished Step, or None if cancelled."""

    def __init__(self, app, step: Optional[Step] = None, default_name: str = "image"):
        self.app = app
        self.engine: Engine = app.engine
        self.root: tk.Tk = app.root
        self.orig = step
        self.result: Optional[Step] = None
        self.shot: Optional[dict] = None       # last screenshot (engine.screenshot())
        self.photo: Optional[tk.PhotoImage] = None
        self.view: Optional[tk.PhotoImage] = None
        self.factor = 1                        # screenshot px per canvas px
        self.sel: Optional[tuple[int, int, int, int]] = None  # selection in screenshot px
        self.drag_from: Optional[tuple[int, int]] = None
        self.preview: Optional[tk.PhotoImage] = None
        self.template_png: Optional[bytes] = base64.b64decode(step.image) if step and step.image else None
        self.template_changed = False          # a new region was chosen in this dialog
        self.click_css: Optional[tuple[float, float]] = None  # chosen click point, page CSS px
        if step and step.box and step.click_x is not None and step.click_y is not None:
            self.click_css = (step.box[0] + step.click_x, step.box[1] + step.click_y)

        dlg = self.dlg = tk.Toplevel(self.root)
        dlg.title("Image check")
        dlg.transient(self.root)
        dlg.protocol("WM_DELETE_WINDOW", self.cancel)
        dlg.bind("<Escape>", lambda e: self.cancel())
        outer = ttk.Frame(dlg, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)

        ttk.Label(outer, wraplength=860, justify="left", text=(
            "Drag a box around something that shows the game is ready for the next click, for example "
            "a button. Keep it small, and leave out numbers or animations that change. "
            "Right-click to choose where to click, if not in the middle of the box.")).grid(
            row=0, column=0, columnspan=2, sticky="w")
        self.canvas = tk.Canvas(outer, width=640, height=360, background="#202124", highlightthickness=1,
                                highlightbackground="#888", cursor="crosshair")
        self.canvas.grid(row=1, column=0, columnspan=2, sticky="nw", pady=(8, 0))
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<ButtonPress-3>", self._set_point)
        self.canvas_msg = self.canvas.create_text(320, 180, fill="#dddddd", text="Taking a screenshot…")

        form = ttk.Frame(outer)
        form.grid(row=2, column=0, sticky="nw", pady=(10, 0))
        form.columnconfigure(3, weight=1)  # wide rows spanning columns 1-3 must not push the unit labels
        side = ttk.Frame(outer)
        side.grid(row=2, column=1, sticky="ne", pady=(10, 0), padx=(16, 0))

        # A new check starts like the last new one (most checks share their settings).
        s = step or Step(delay_ms=0, x=0, y=0, kind="check", name=default_name, **app.check_defaults())
        r = 0
        ttk.Label(form, text="Name").grid(row=r, column=0, sticky="w")
        self.name_var = tk.StringVar(value=s.name or default_name)
        ttk.Entry(form, textvariable=self.name_var, width=28).grid(row=r, column=1, columnspan=3, sticky="w", padx=6)
        r += 1
        ttk.Label(form, text="Wait until it").grid(row=r, column=0, sticky="w", pady=(6, 0))
        self.wait_for_var = tk.StringVar(value=s.wait_for)
        wf = ttk.Frame(form)
        wf.grid(row=r, column=1, columnspan=3, sticky="w", padx=6, pady=(6, 0))
        ttk.Radiobutton(wf, text="appears", value="appear", variable=self.wait_for_var,
                        command=self._sync).pack(side="left")
        ttk.Radiobutton(wf, text="disappears", value="disappear", variable=self.wait_for_var,
                        command=self._sync).pack(side="left", padx=(10, 0))
        r += 1
        ttk.Label(form, text="Wait at most").grid(row=r, column=0, sticky="w", pady=(6, 0))
        self.timeout_var = tk.StringVar(value=f"{s.timeout_ms / 1000:g}")
        ttk.Spinbox(form, from_=0, to=86_400, increment=1, width=7, textvariable=self.timeout_var).grid(
            row=r, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(form, text="seconds  (0 = just look once)").grid(row=r, column=2, columnspan=2, sticky="w",
                                                                  pady=(6, 0))
        r += 1
        self.found_label = ttk.Label(form, text="When found")
        self.found_label.grid(row=r, column=0, sticky="w", pady=(6, 0))
        self.found_box = ttk.Combobox(form, state="readonly", width=24)
        self.found_box.grid(row=r, column=1, columnspan=3, sticky="w", padx=6, pady=(6, 0))
        r += 1
        self.missing_label = ttk.Label(form, text="If not found")
        self.missing_label.grid(row=r, column=0, sticky="w", pady=(6, 0))
        self.missing_box = ttk.Combobox(form, state="readonly", width=24,
                                        values=[CHECK_ACTIONS[k] for k in MISSING_KEYS])
        self.missing_box.grid(row=r, column=1, columnspan=3, sticky="w", padx=6, pady=(6, 0))
        self.missing_box.current(MISSING_KEYS.index(s.on_missing) if s.on_missing in MISSING_KEYS else 0)
        r += 1
        ttk.Label(form, text="Click at").grid(row=r, column=0, sticky="w", pady=(6, 0))
        self.click_mode = tk.StringVar(value="point" if self.click_css else "middle")
        cf = ttk.Frame(form)
        cf.grid(row=r, column=1, columnspan=3, sticky="w", padx=6, pady=(6, 0))
        ttk.Radiobutton(cf, text="middle of the image", value="middle", variable=self.click_mode,
                        command=self._click_mode_changed).pack(side="left")
        ttk.Radiobutton(cf, text="point I choose (right-click the screenshot)", value="point",
                        variable=self.click_mode, command=self._click_mode_changed).pack(side="left", padx=(10, 0))
        r += 1
        ttk.Label(form, text="Click delay").grid(row=r, column=0, sticky="w", pady=(6, 0))
        self.click_delay_var = tk.StringVar(value=str(s.click_delay_ms))
        ttk.Spinbox(form, from_=0, to=60_000, increment=100, width=7, textvariable=self.click_delay_var).grid(
            row=r, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(form, text="ms  (wait after finding the image, then click)").grid(
            row=r, column=2, columnspan=2, sticky="w", pady=(6, 0))
        r += 1
        ttk.Label(form, text="Wait after").grid(row=r, column=0, sticky="w", pady=(6, 0))
        self.after_var = tk.StringVar(value=str(s.after_ms))
        ttk.Spinbox(form, from_=0, to=3_600_000, increment=100, width=7, textvariable=self.after_var).grid(
            row=r, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(form, text="ms  (only after success, after the click; none if not found)").grid(
            row=r, column=2, columnspan=2, sticky="w", pady=(6, 0))
        r += 1
        ttk.Label(form, text="Click again").grid(row=r, column=0, sticky="w", pady=(6, 0))
        self.retry_var = tk.StringVar(value=str(s.retry_clicks))
        ttk.Spinbox(form, from_=0, to=20, increment=1, width=7, textvariable=self.retry_var).grid(
            row=r, column=1, sticky="w", padx=6, pady=(6, 0))
        rf = ttk.Frame(form)
        rf.grid(row=r, column=2, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(rf, text="times at most if the image is still there").pack(side="left")
        self.retry_ms_var = tk.StringVar(value=str(s.retry_ms))
        ttk.Spinbox(rf, from_=100, to=60_000, increment=100, width=6, textvariable=self.retry_ms_var).pack(
            side="left", padx=4)
        ttk.Label(rf, text="ms after the click").pack(side="left")
        r += 1
        ttk.Label(form, text="Go to step").grid(row=r, column=0, sticky="w", pady=(6, 0))
        own = step.uid if step else None
        self.step_uids = [st.uid for st in app.steps if st.uid != own]
        labels = [f"#{n + 1}  {st.action} · {st.title or f'{st.x:.0f}, {st.y:.0f}'}"
                  for n, st in enumerate(app.steps) if st.uid != own]
        self.step_box = ttk.Combobox(form, state="readonly", width=40, values=labels)
        self.step_box.grid(row=r, column=1, columnspan=3, sticky="w", padx=6, pady=(6, 0))
        if s.goto_step in self.step_uids:
            self.step_box.current(self.step_uids.index(s.goto_step))
        self.step_box.bind("<<ComboboxSelected>>", lambda e: self._sync_preset())
        r += 1
        ttk.Label(form, text="Go to preset").grid(row=r, column=0, sticky="w", pady=(6, 0))
        names = self.app._preset_names()
        if s.goto_preset and s.goto_preset not in names:
            names.append(s.goto_preset)  # deleted since; keep showing what the check says
        self.preset_box = ttk.Combobox(form, state="readonly", width=24, values=[NEXT_PRESET] + names)
        self.preset_box.grid(row=r, column=1, columnspan=3, sticky="w", padx=6, pady=(6, 0))
        self.preset_box.set(s.goto_preset or NEXT_PRESET)
        self.found_box.bind("<<ComboboxSelected>>", lambda e: self._sync_preset())
        self.missing_box.bind("<<ComboboxSelected>>", lambda e: self._sync_preset())
        r += 1
        ttk.Label(form, text="Match needed").grid(row=r, column=0, sticky="w", pady=(6, 0))
        self.threshold_var = tk.StringVar(value=str(s.threshold))
        ttk.Spinbox(form, from_=50, to=100, increment=1, width=7, textvariable=self.threshold_var).grid(
            row=r, column=1, sticky="w", padx=6, pady=(6, 0))
        mf = ttk.Frame(form)
        mf.grid(row=r, column=2, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(mf, text="% of the pixels").pack(side="left")
        self.tone_var = tk.BooleanVar(value=bool(s.tone))
        ttk.Checkbutton(mf, variable=self.tone_var,
                        text="also when lighter or darker (hover, fading)").pack(side="left", padx=(12, 0))
        r += 1
        ttk.Label(form, text="Wait before").grid(row=r, column=0, sticky="w", pady=(6, 0))
        self.delay_var = tk.StringVar(value=str(s.delay_ms))
        ttk.Spinbox(form, from_=0, to=86_400_000, increment=100, width=7, textvariable=self.delay_var).grid(
            row=r, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(form, text="ms  (after the previous step)").grid(row=r, column=2, columnspan=2, sticky="w",
                                                                   pady=(6, 0))
        r += 1
        self.own_only_var = tk.BooleanVar(value=bool(s.only_on_jump))
        ttk.Checkbutton(form, variable=self.own_only_var, text=(
            "Run this check only when another check jumps to it (normal playback skips it)")).grid(
            row=r, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self._found_key = s.on_found
        self._sync()

        ttk.Label(side, text="Selected image").pack(anchor="w")
        self.preview_lbl = tk.Label(side, background="#202124", width=22, height=7, text="(none yet)",
                                    foreground="#bbbbbb")
        self.preview_lbl.pack(anchor="w", pady=(4, 0))
        self.size_var = tk.StringVar(value="")
        ttk.Label(side, textvariable=self.size_var, style="Hint.TLabel").pack(anchor="w")
        self.test_var = tk.StringVar(value="")
        ttk.Label(side, textvariable=self.test_var, wraplength=220, justify="left").pack(anchor="w", pady=(6, 0))

        bar = ttk.Frame(outer)
        bar.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        self.retake_btn = ttk.Button(bar, text="Retake screenshot", command=self.take_screenshot)
        self.retake_btn.pack(side="left")
        self.test_btn = ttk.Button(bar, text="Test now", command=self.test_now)
        self.test_btn.pack(side="left", padx=(6, 0))
        self.test_click_btn = ttk.Button(bar, text="Test and click", command=self.test_and_click)
        self.test_click_btn.pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Cancel", command=self.cancel).pack(side="right")
        ttk.Button(bar, text="OK", style="Accent.TButton", command=self.ok).pack(side="right", padx=(0, 6))

        if self.template_png:
            self._show_preview_png(self.template_png)
        self.app._center(dlg)
        try:
            dlg.wait_visibility()
            dlg.grab_set()
        except tk.TclError:
            pass
        if self.engine.connected and self.engine.target:
            self.take_screenshot()
        else:
            self.canvas.itemconfigure(self.canvas_msg, text="Connect and choose a target tab to take a screenshot.")
            self.retake_btn.configure(state="disabled")
            self.test_btn.configure(state="disabled")
            self.test_click_btn.configure(state="disabled")

    # ---------------------------------------------------------------- helpers

    def _sync(self) -> None:
        appear = self.wait_for_var.get() != "disappear"
        keys = FOUND_KEYS if appear else [k for k in FOUND_KEYS if k not in CLICK_ACTIONS]
        current = self._current_found()
        self.found_box.configure(values=[CHECK_ACTIONS[k] for k in keys])
        self.found_box.current(keys.index(current) if current in keys else 0)
        self.found_label.configure(text="When found" if appear else "When gone")
        self.missing_label.configure(text="If not found" if appear else "If still there")
        if hasattr(self, "preset_box") and hasattr(self, "step_box"):
            self._sync_preset()

    def _sync_preset(self) -> None:
        """Enable the preset / step choices only when an action needs them."""
        actions = (self._current_found(), self._missing_key())
        wanted = any(a in PRESET_ACTIONS for a in actions)
        self.preset_box.configure(state="readonly" if wanted else "disabled")
        wanted = any(a in STEP_ACTIONS for a in actions)
        self.step_box.configure(state="readonly" if wanted else "disabled")

    def _target_step(self) -> Optional[Step]:
        i = self.step_box.current()
        if i < 0:
            return None
        uid = self.step_uids[i]
        return next((st for st in self.app.steps if st.uid == uid), None)

    def _current_found(self) -> str:
        label = self.found_box.get()
        for k, v in CHECK_ACTIONS.items():
            if v == label:
                return k
        return self._found_key

    def _missing_key(self) -> str:
        label = self.missing_box.get()
        return next((k for k, v in CHECK_ACTIONS.items() if v == label), "stop")

    def _run(self, fn: Callable, on_ok: Callable, what: str) -> None:
        def failed(exc: Exception) -> None:
            if self.dlg.winfo_exists():
                self.test_var.set(f"Could not {what}: {exc}")
        self.app.run_bg(fn, on_ok=lambda v: self.dlg.winfo_exists() and on_ok(v), on_err=failed)

    # ------------------------------------------------------------- screenshot

    def take_screenshot(self) -> None:
        self.canvas.itemconfigure(self.canvas_msg, text="Taking a screenshot…")
        self._run(self.engine.screenshot, self._show_screenshot, "take a screenshot")

    def _show_screenshot(self, shot: dict) -> None:
        self.shot = shot
        self.photo = tk.PhotoImage(master=self.dlg, data=base64.b64encode(shot["png"]))
        w, h = self.photo.width(), self.photo.height()
        self.factor = max(1, math.ceil(max(w / MAX_VIEW[0], h / MAX_VIEW[1])))
        self.view = self.photo.subsample(self.factor) if self.factor > 1 else self.photo
        self.canvas.delete("all")
        self.canvas.configure(width=self.view.width(), height=self.view.height())
        self.canvas.create_image(0, 0, image=self.view, anchor="nw")
        self.sel = None
        if self.orig and self.orig.box and not self.template_changed:
            dpr = shot["dpr"]
            x, y, bw, bh = self.orig.box
            self.sel = (round(x * dpr), round(y * dpr), round((x + bw) * dpr), round((y + bh) * dpr))
            self._draw_sel()
        self._draw_point()
        self.app._center(self.dlg)

    def _set_point(self, event) -> None:
        if self.photo is None or not self.shot:
            return
        x, y = self._canvas_to_img(event)
        dpr = self.shot["dpr"]
        self.click_css = (x / dpr, y / dpr)
        self.click_mode.set("point")
        self._draw_point()

    def _click_mode_changed(self) -> None:
        if self.click_mode.get() == "point" and self.click_css is None:
            self.test_var.set("Right-click the screenshot where playback should click.")
        self._draw_point()

    def _draw_point(self) -> None:
        """A crosshair where playback clicks (only when a point was chosen)."""
        self.canvas.delete("pt")
        if self.click_mode.get() != "point" or not self.click_css or not self.shot:
            return
        k = self.shot["dpr"] / self.factor
        x, y = self.click_css[0] * k, self.click_css[1] * k
        for color, w in (("#ffffff", 4), ("#1e6ee6", 2)):
            self.canvas.create_oval(x - 8, y - 8, x + 8, y + 8, outline=color, width=w, tags="pt")
            self.canvas.create_line(x - 13, y, x + 13, y, fill=color, width=w, tags="pt")
            self.canvas.create_line(x, y - 13, x, y + 13, fill=color, width=w, tags="pt")
        self.test_var.set(f"Playback clicks at {self.click_css[0]:.0f}, {self.click_css[1]:.0f} "
                          "(it moves along if the image is found a little off).")

    def _canvas_to_img(self, event) -> tuple[int, int]:
        w, h = self.photo.width(), self.photo.height()
        return (min(max(0, int(self.canvas.canvasx(event.x) * self.factor)), w),
                min(max(0, int(self.canvas.canvasy(event.y) * self.factor)), h))

    def _press(self, event) -> None:
        if self.photo is None:
            return
        self.drag_from = self._canvas_to_img(event)
        self.sel = None
        self._draw_sel()

    def _motion(self, event) -> None:
        if self.drag_from is None:
            return
        x0, y0 = self.drag_from
        x1, y1 = self._canvas_to_img(event)
        self.sel = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        self._draw_sel()

    def _release(self, event) -> None:
        if self.drag_from is None:
            return
        self._motion(event)
        self.drag_from = None
        if self.sel and (self.sel[2] - self.sel[0] < 6 or self.sel[3] - self.sel[1] < 6):
            self.sel = None
            self._draw_sel()
            self.test_var.set("That box is too small. Drag a bigger box.")
            return
        if self.sel:
            self.template_changed = True
            self.template_png = self._crop_png(self.sel)
            self._show_preview_png(self.template_png)
            self.test_var.set("")

    def _draw_sel(self) -> None:
        self.canvas.delete("sel")
        if not self.sel:
            return
        f = self.factor
        x0, y0, x1, y1 = (v / f for v in self.sel)
        self.canvas.create_rectangle(x0, y0, x1, y1, outline="#ff3b30", width=2, tags="sel")
        self.canvas.create_rectangle(x0 - 1, y0 - 1, x1 + 1, y1 + 1, outline="#ffffff", width=1, tags="sel")
        dpr = self.shot["dpr"] if self.shot else 1
        self.size_var.set(f"{(self.sel[2] - self.sel[0]) / dpr:.0f} × {(self.sel[3] - self.sel[1]) / dpr:.0f} px "
                          f"at {self.sel[0] / dpr:.0f}, {self.sel[1] / dpr:.0f}")

    def _crop_png(self, sel: tuple[int, int, int, int]) -> bytes:
        crop = tk.PhotoImage(master=self.dlg)
        crop.tk.call(crop, "copy", self.photo, "-from", *sel)
        data = crop.tk.call(crop, "data", "-format", "png")
        return data if isinstance(data, bytes) else base64.b64decode(data)

    def _show_preview_png(self, png: bytes) -> None:
        img = tk.PhotoImage(master=self.dlg, data=base64.b64encode(png))
        w, h = img.width(), img.height()
        if w > 200 or h > 140:
            k = math.ceil(max(w / 200, h / 140))
            img = img.subsample(k)
        else:
            k = max(1, min(200 // max(1, w), 140 // max(1, h), 6))
            if k > 1:
                img = img.zoom(k)
        self.preview = img
        self.preview_lbl.configure(image=img, width=0, height=0, text="")

    # ------------------------------------------------------------------ result

    def _build_step(self) -> Optional[Step]:
        if not self.template_png:
            messagebox.showinfo("Image check", "Drag a box around part of the screenshot first.", parent=self.dlg)
            return None
        try:
            timeout = max(0.0, float(self.timeout_var.get()))
            threshold = min(100, max(50, int(float(self.threshold_var.get()))))
            delay = max(0, int(float(self.delay_var.get())))
            click_delay = min(60_000, max(0, int(float(self.click_delay_var.get()))))
            after = min(3_600_000, max(0, int(float(self.after_var.get()))))
            retry = min(20, max(0, int(float(self.retry_var.get()))))
            retry_ms = min(60_000, max(100, int(float(self.retry_ms_var.get()))))
        except ValueError:
            messagebox.showinfo("Image check", "Please enter numbers for the times and the match.", parent=self.dlg)
            return None
        if self.click_mode.get() == "point" and self.click_css is None:
            messagebox.showinfo("Image check", "Right-click the screenshot where playback should click, "
                                "or choose “middle of the image”.", parent=self.dlg)
            return None
        jumps = self._current_found() in STEP_ACTIONS or self._missing_key() in STEP_ACTIONS
        target = self._target_step()
        if jumps and target is None:
            messagebox.showinfo("Image check", "Choose the step to go to.", parent=self.dlg)
            return None
        base = self.orig or Step(delay_ms=0, x=0, y=0, kind="check")
        values = dict(click_delay_ms=click_delay, after_ms=after, retry_clicks=retry, retry_ms=retry_ms, tone=bool(self.tone_var.get()), goto_step=target.uid if jumps and target else "", only_on_jump=bool(self.own_only_var.get()),
                      delay_ms=delay, name=self.name_var.get().strip() or "image",
                      wait_for=self.wait_for_var.get(), timeout_ms=int(round(timeout * 1000)),
                      threshold=threshold, on_found=self._current_found(), on_missing=self._missing_key(),
                      goto_preset="" if self.preset_box.get() == NEXT_PRESET else self.preset_box.get())
        if self.template_changed and self.shot and self.sel:
            dpr = self.shot["dpr"]
            x0, y0, x1, y1 = self.sel
            box = [x0 / dpr, y0 / dpr, (x1 - x0) / dpr, (y1 - y0) / dpr]
            values.update(box=box, image=base64.b64encode(self.template_png).decode(), dpr=dpr,
                          x=round(box[0] + box[2] / 2, 1), y=round(box[1] + box[3] / 2, 1),
                          scroll_x=float(self.shot["sx"]), scroll_y=float(self.shot["sy"]),
                          vw=int(self.shot["vw"]), vh=int(self.shot["vh"]),
                          url=safe_url(self.engine.target.url) if self.engine.target else base.url)
        box = values.get("box") or base.box
        if self.click_mode.get() == "point" and box:
            values.update(click_x=round(self.click_css[0] - box[0], 1), click_y=round(self.click_css[1] - box[1], 1))
        else:
            values.update(click_x=None, click_y=None)
        data = base.to_dict()
        data.update(values, kind="check")
        return Step.from_dict(data)

    def test_now(self) -> None:
        step = self._build_step()
        if step is None:
            return
        self.test_var.set("Testing…")

        def show(res) -> None:
            passed, score, point = res
            appear = step.wait_for != "disappear"
            seen = score >= step.threshold
            text = (("✔ " if passed else "✘ ") + ("Found" if seen else "Not found")
                    + f" on the page now ({score:.0f}% match, needs {step.threshold}%)."
                    + ("" if appear else ("  It is still there." if seen else "  It is gone.")))
            if point and appear and step.on_found in CLICK_ACTIONS:
                text += (f"\n\nTest now does not click. During playback it would click at "
                         f"{point[0]:.0f}, {point[1]:.0f} (marked in the game with a blue ring).")
            self.test_var.set(text)
        self._run(lambda: self.engine.test_check(step), show, "test")

    def test_and_click(self) -> None:
        """Test now, and if found, wait the click delay and click (like playback)."""
        step = self._build_step()
        if step is None:
            return
        if self.app.mode != "idle":
            self.test_var.set("Stop recording or playback first: a test click would be recorded or get in the way.")
            return
        clicks = step.on_found in CLICK_ACTIONS and step.wait_for != "disappear"
        wait = f" and wait {step.click_delay_ms / 1000:g} s" if clicks and step.click_delay_ms else ""
        self.test_var.set(f"Looking for the image{wait}…")
        self.test_click_btn.configure(state="disabled")

        def show(res) -> None:
            self.test_click_btn.configure(state="normal")
            passed, score, point, clicked = res
            seen = score >= step.threshold
            text = ("✔ Found" if seen else "✘ Not found") + f" ({score:.0f}% match, needs {step.threshold}%)."
            if clicked:
                text += (f"\n\nClicked at {point[0]:.0f}, {point[1]:.0f}"
                         + (f" after waiting {step.click_delay_ms / 1000:g} s" if step.click_delay_ms else "") + ".")
            elif seen and not clicks:
                text += f"\n\nNot clicked: “When found” is “{CHECK_ACTIONS[step.on_found]}”."
            elif seen:
                text += "\n\nThe click did not go through (see the log)."
            self.test_var.set(text)

        def failed(exc: Exception) -> None:
            if self.dlg.winfo_exists():
                self.test_click_btn.configure(state="normal")
                self.test_var.set(f"Could not test: {exc}")
        game = bool(self.app.game_var.get())
        self.app.run_bg(lambda: self.engine.test_and_click(step, game), on_ok=lambda v: self.dlg.winfo_exists() and show(v),
                        on_err=failed)

    def ok(self) -> None:
        step = self._build_step()
        if step is None:
            return
        self.result = step
        if self.orig is None:
            self.app.remember_check_defaults(step)
        self.dlg.destroy()

    def cancel(self) -> None:
        self.result = None
        self.dlg.destroy()

    def show(self) -> Optional[Step]:
        self.root.wait_window(self.dlg)
        return self.result
