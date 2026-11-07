"""End-to-end check of the bot's game speed and key presses in a private headless Firefox.

    python3 tests/e2e_speed.py

Like the bot, it uses a game page in its own window with the game in a
cross-site frame. Checks that the speed control makes the frame's clocks,
timers and animation frames run faster, never jumps back in time, is set up
again by a reload, and that an Esc key press reaches the game's frame.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from e2e_headless import Checker, free_port, serve  # noqa: E402
from tab_autoclicker import bitheroes, firefox  # noqa: E402
from tab_autoclicker.engine import Engine  # noqa: E402

TOP = """<!doctype html><title>Game page</title><body style="margin:0">
<iframe src="http://localhost:{p2}/game.html" style="border:0;width:800px;height:500px"></iframe>"""
GAME = """<!doctype html><title>Game</title><body style="margin:0">
<canvas width="800" height="500" style="background:#246"></canvas>
<script>
  var keys = [], frames = [];
  addEventListener('keydown', (e) => keys.push(e.key));
  (function loop(t) { frames.push(t); if (frames.length > 120) frames.shift(); requestAnimationFrame(loop); })(0);
</script>"""


def main() -> int:
    binary = firefox.find_firefox()
    if not binary:
        print("Firefox not found")
        return 2
    if firefox.is_snap_firefox(binary):
        profile = os.path.join(os.path.expanduser("~"), "snap", "firefox", "common", "tab-autoclicker-speedtest")
        shutil.rmtree(profile, ignore_errors=True)
        os.makedirs(profile)
    else:
        profile = tempfile.mkdtemp(prefix="tab-autoclicker-speedtest-")
    firefox.set_user_pref(profile, "remote.prefs.recommended", False)
    p2 = serve({"/game.html": GAME})
    p1 = serve({"/top.html": TOP.format(p2=p2)})
    port = free_port()
    proc = subprocess.Popen([binary, "--headless", "--no-remote", "--profile", profile,
                             "--remote-debugging-port", str(port), *firefox.windows_args(), "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    c = Checker()
    eng = Engine(lambda kind, payload: None)
    try:
        assert firefox.wait_for_port(port, 60), "headless Firefox did not start"
        print(f"Connected to {eng.connect(port)}")
        cl = eng.client
        tab = eng.open_in_window(f"http://127.0.0.1:{p1}/top.html").context
        time.sleep(2)

        def frame() -> str:
            return cl.send("browsingContext.getTree", {"root": tab})["contexts"][0]["children"][0]["context"]

        def js(expr):
            r = cl.send("script.evaluate", {"expression": expr, "target": {"context": frame()}, "awaitPromise": True})
            return cl.script_result(r)

        def rate(seconds=1.0):
            """How fast the frame's clocks run, compared with real time."""
            p0, d0, t0 = js("performance.now()"), js("Date.now()"), time.monotonic()
            time.sleep(seconds)
            p1_, d1, t1 = js("performance.now()"), js("Date.now()"), time.monotonic()
            real = (t1 - t0) * 1000
            return (p1_ - p0) / real, (d1 - d0) / real

        print("Speed set in a running page")
        c.check(eng._set_speed(cl, tab, 4), "the speed is set in the tab and its frames")
        perf, date = rate()
        c.check(3.2 < perf < 4.8 and 3.2 < date < 4.8, f"clocks run 4× ({perf:.2f}×, Date {date:.2f}×)")
        fired = js("new Promise((ok) => { const t = performance.now(); "
                   "setTimeout(() => ok(performance.now() - t), 2000); })")
        c.check(1800 < fired < 2600, f"a 2 s timer is 2 s of game time ({fired:.0f} ms)")
        span = js("frames[frames.length - 1] - frames[frames.length - 61]")
        c.check(span > 2500, f"animation frames see game time: 60 frames = {span:.0f} ms (1× would be ~1000)")
        before = js("performance.now()")
        c.check(eng._set_speed(cl, tab, 1), "back to 1×")
        after = js("performance.now()")
        perf, _ = rate()
        c.check(after >= before and 0.8 < perf < 1.2, f"no jump back in time; clocks run 1× ({perf:.2f}×)")

        print("Speed after a reload")
        script = eng._speed_hook(cl, tab)
        c.check(script is not None, "the speed control is kept for later page loads")
        cl.send("browsingContext.reload", {"context": tab, "wait": "complete"})
        time.sleep(2)
        c.check(js("Object.getOwnPropertyNames(globalThis).includes('__tacSpeed')") is True,
                "set up again before the reloaded game starts")
        c.check(eng._set_speed(cl, tab, 3), "speed 3× after the reload")
        perf, _ = rate()
        c.check(2.4 < perf < 3.6, f"clocks run 3× ({perf:.2f}×)")
        eng._speed_hook(cl, tab, script, on=False)
        eng._set_speed(cl, tab, 1)

        print("Esc reaches the game")
        # (Keys only reach a focused frame; the bot's clicks give the game focus.)
        cl.send("input.performActions", {"context": tab, "actions": [{
            "type": "pointer", "id": "m", "parameters": {"pointerType": "mouse"}, "actions": [
                {"type": "pointerMove", "x": 400, "y": 250}, {"type": "pointerDown", "button": 0},
                {"type": "pause", "duration": 50}, {"type": "pointerUp", "button": 0}]}]})
        time.sleep(0.3)
        eng._press_key(cl, tab, bitheroes.ESCAPE)
        time.sleep(0.3)
        keys = js("keys")
        c.check(keys == ["Escape"], f"the game's frame got Esc after a click in the game ({keys})")
    finally:
        try:
            eng.disconnect()
        except Exception:  # noqa: BLE001
            pass
        firefox.stop(proc)
        shutil.rmtree(profile, ignore_errors=True)
    print("\nALL CHECKS PASSED" if not c.failed else f"\n{c.failed} CHECK(S) FAILED")
    return 1 if c.failed else 0


if __name__ == "__main__":
    sys.exit(main())
