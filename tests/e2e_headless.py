"""End-to-end self test: runs the engine against a private headless Firefox.

    python3 tests/e2e_headless.py

It never touches your normal Firefox: it starts a separate headless instance
with a throw-away profile, serves two tiny test pages (one embedded as a
cross-site iframe, plus a canvas "game"), and checks picking, recording,
background replay, image checks and game mode.
"""

from __future__ import annotations

import http.server
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tab_autoclicker import firefox  # noqa: E402
from tab_autoclicker.engine import Engine, PlayOptions, Step  # noqa: E402
import base64  # noqa: E402

TOP = """<!doctype html><title>Top page</title>
<body style="margin:0;height:3000px">
<button id="b1" style="position:absolute;left:20px;top:20px;width:150px;height:60px">Top button</button>
<button id="b2" style="position:absolute;left:20px;top:1500px;width:150px;height:60px">Far button</button>
<iframe id="f" src="http://localhost:{p2}/frame.html"
  style="position:absolute;left:200px;top:100px;width:400px;height:300px;border:5px solid red;padding:7px"></iframe>
<script>
window.hits = []; window.dbl = 0;
for (const b of document.querySelectorAll('button'))
  b.addEventListener('click', e => hits.push([b.id, e.isTrusted, e.button]));
addEventListener('dblclick', () => dbl++);
</script>"""
FRAME = """<!doctype html><title>Frame</title><body style="margin:0">
<button id="fb" style="position:absolute;left:30px;top:40px;width:120px;height:50px">Frame button</button>
<script>window.hits = [];
document.getElementById('fb').addEventListener('click', e => hits.push(['fb', e.isTrusted, e.button]));</script>"""

# A canvas "game" that behaves like a Unity WebGL one: it draws every frame,
# ignores the mouse after its window loses focus, and its READY button comes
# back after a random delay. A red STOP sign appears once the score reaches 5.
GAME = """<!doctype html><title>Canvas game</title><body style="margin:0;background:#123">
<canvas id="c" width="800" height="600" style="display:block"></canvas>
<script>
window.score = 0; window.bad = 0; window.ignored = 0; window.focused = true; window.ready = true;
window.order = '';
addEventListener('blur', () => { focused = false; });
addEventListener('focus', () => { focused = true; });
const c = document.getElementById('c'), g = c.getContext('2d');
const inBox = (e, x, y, w, h) => e.offsetX >= x && e.offsetX < x + w && e.offsetY >= y && e.offsetY < y + h;
c.addEventListener('mousedown', (e) => {
  if (!focused) { ignored++; return; }
  if (ready && inBox(e, 100, 100, 160, 60)) {
    score++; order += 'R'; ready = false; setTimeout(() => { ready = true; }, 300 + Math.random() * 600);
  } else if (inBox(e, 100, 400, 200, 60)) { bad++; order += 'B'; }
});
(function frame() {
  g.fillStyle = '#123'; g.fillRect(0, 0, 800, 600);
  if (ready) { g.fillStyle = '#2a2'; g.fillRect(100, 100, 160, 60); g.fillStyle = '#fff';
               g.font = 'bold 28px sans-serif'; g.fillText('READY', 132, 142); }
  if (score >= 5) { g.fillStyle = '#d22'; g.fillRect(500, 100, 120, 60); }
  g.fillStyle = '#35a'; g.fillRect(100, 400, 200, 60);
  requestAnimationFrame(frame);
})();
</script>"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(pages: dict) -> int:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = pages.get(self.path.split("?")[0])
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write((body or "not found").encode())

        def log_message(self, *args):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


class Checker:
    def __init__(self):
        self.failed = 0

    def check(self, cond, what):
        print(("  PASS  " if cond else "  FAIL  ") + what)
        if not cond:
            self.failed += 1


def main() -> int:
    binary = firefox.find_firefox()
    if not binary:
        print("Firefox not found")
        return 2
    if firefox.is_snap_firefox(binary):
        profile = os.path.join(os.path.expanduser("~"), "snap", "firefox", "common", "tab-autoclicker-selftest")
        shutil.rmtree(profile, ignore_errors=True)
        os.makedirs(profile)
    else:
        profile = tempfile.mkdtemp(prefix="tab-autoclicker-selftest-")
    firefox.set_user_pref(profile, "remote.prefs.recommended", False)
    p2 = serve({"/frame.html": FRAME})
    p1 = serve({"/top.html": TOP.format(p2=p2), "/game.html": GAME})
    port = free_port()
    proc = subprocess.Popen([binary, "--headless", "--no-remote", "--profile", profile,
                             "--remote-debugging-port", str(port), *firefox.windows_args(), "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    c = Checker()
    events: queue.Queue = queue.Queue()
    eng = Engine(lambda kind, payload: events.put((kind, payload)))
    steps: list[Step] = []
    try:
        assert firefox.wait_for_port(port, 60), "headless Firefox did not start"
        print(f"Connected to {eng.connect(port)}")
        cl = eng.client

        def js(ctx, expr):
            r = cl.send("script.evaluate", {"expression": expr, "target": {"context": ctx}, "awaitPromise": False})
            return cl.script_result(r)

        def user_click(ctx, x, y, button=0, hold=40, move=True):
            acts = ([{"type": "pointerMove", "x": x, "y": y, "duration": 0}] if move else []) + [
                {"type": "pointerDown", "button": button}, {"type": "pause", "duration": hold},
                {"type": "pointerUp", "button": button}]
            cl.send("input.performActions", {"context": ctx, "actions": [
                {"type": "pointer", "id": "user", "parameters": {"pointerType": "mouse"}, "actions": acts}]})

        def wait_for(pred, timeout=10.0):
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                if pred():
                    return True
                time.sleep(0.05)
            return False

        def drain():
            while True:
                try:
                    kind, payload = events.get_nowait()
                except queue.Empty:
                    return
                if kind == "step":
                    steps.append(payload)
                elif kind == "log":
                    print("        log:", payload)

        url = f"http://127.0.0.1:{p1}/top.html"
        a = cl.send("browsingContext.create", {"type": "tab", "background": True})["context"]
        cl.send("browsingContext.navigate", {"context": a, "url": url, "wait": "complete"})
        b = cl.send("browsingContext.create", {"type": "tab", "background": True})["context"]
        cl.send("browsingContext.navigate", {"context": b, "url": url, "wait": "complete"})
        time.sleep(0.5)
        frame = cl.send("browsingContext.getTree", {"root": a})["contexts"][0]["children"][0]["context"]

        print("Pick a tab by clicking in it")
        cl.send("browsingContext.activate", {"context": a})
        eng.start_pick()
        user_click(a, 60, 50)
        c.check(wait_for(lambda: eng.target is not None and eng.mode == "idle"), "a click in the tab selects it")
        c.check(eng.target and eng.target.context == a, "the clicked tab became the target")
        c.check(eng.target and eng.target.title == "Top page", "target title is read")
        time.sleep(1.5)
        c.check(js(a, "hits.length") == 0, "the picking click did not press the page's button")
        c.check(js(b, "document.querySelector('tab-autoclicker-ui') === null"), "pick banner removed from other tabs")

        print("Record clicks (top page, cross-site iframe, scrolled page, double click)")
        eng.start_recording()
        user_click(a, 60, 50)
        time.sleep(0.15)
        user_click(frame, 60, 60)                  # = (272, 172) in the top page
        time.sleep(0.15)
        js(a, "scrollTo(0, 1400)")
        user_click(a, 50, 130)                     # b2 while scrolled
        js(a, "scrollTo(0, 0)")
        time.sleep(0.15)
        user_click(a, 60, 50)
        time.sleep(0.1)
        user_click(a, 60, 50, move=False)
        time.sleep(0.5)
        eng.stop_recording()
        drain()
        c.check(len(steps) == 5, f"5 steps recorded (got {len(steps)})")
        if len(steps) == 5:
            s0, s1, s2 = steps[0], steps[1], steps[2]
            c.check((s0.x, s0.y, s0.button) == (60, 50, 0) and s0.selector == "#b1", "top click position + selector")
            c.check(s1.in_frame and (s1.x, s1.y) == (272, 172), f"iframe click in page coordinates ({s1.x}, {s1.y})")
            c.check(s2.scroll_y == 1400 and s2.selector == "#b2", "scroll position recorded")
            c.check(100 <= steps[4].delay_ms < 600, f"delays recorded ({[s.delay_ms for s in steps]})")
            c.check('"Top button"' in s0.label, f"label: {s0.label}")
        c.check(js(a, "document.querySelector('tab-autoclicker-ui') === null"), "REC badge removed after stop")
        c.check(js(a, "hits.length") == 5 - 1, "recorded clicks worked normally on the page")

        print("Replay while the target tab is in the background")
        js(a, "hits = []; dbl = 0"); js(frame, "hits = []")
        cl.send("browsingContext.activate", {"context": b})
        c.check(js(a, "document.visibilityState") == "hidden", "target tab is hidden before playback")
        eng.play(steps, PlayOptions(repeat=2, repeat_pause_ms=300))
        c.check(wait_for(lambda: eng.mode == "idle", 20), "playback finished")
        drain()
        top_hits = js(a, "JSON.stringify(hits)")
        frame_hits = js(frame, "JSON.stringify(hits)")
        c.check(top_hits.count('"b1",true') == 6 and top_hits.count('"b2",true') == 2, f"top page hits {top_hits}")
        c.check(frame_hits.count('"fb",true') == 2, f"iframe hits {frame_hits}")
        c.check(js(a, "dbl") >= 2, "recorded double click replays as a double click")
        c.check(js(a, "document.visibilityState") == "hidden", "target tab stayed in the background")
        c.check(js(a, "Math.round(scrollY)") == 0, "scroll restored for the last click")

        print("Aim at elements that moved")
        js(a, "hits = []; { const e = document.getElementById('b1'); e.style.left = '320px'; e.style.top = '520px'; }")
        eng.play(steps[:1], PlayOptions(repeat=1, aim="element"))
        wait_for(lambda: eng.mode == "idle", 10)
        c.check(js(a, "JSON.stringify(hits)").count("b1") == 1, "element aiming follows the moved button")
        js(a, "hits = []")
        eng.play(steps[:1], PlayOptions(repeat=1, aim="position"))
        wait_for(lambda: eng.mode == "idle", 10)
        c.check(js(a, "hits.length") == 0, "position aiming clicks the recorded spot")

        print("Fixed interval, forever, stop")
        js(a, "hits = []; { const e = document.getElementById('b1'); e.style.left = '20px'; e.style.top = '20px'; }")
        eng.play(steps[:1], PlayOptions(repeat=0, timing="fixed", interval_ms=100, repeat_pause_ms=0, markers=False))
        time.sleep(1.05)
        eng.stop_play()
        wait_for(lambda: eng.mode == "idle", 5)
        drain()
        n = js(a, "hits.length")
        c.check(8 <= n <= 12, f"~10 clicks in 1 s at 100 ms interval (got {n})")

        js(a, "hits = []; window.times = []; document.getElementById('b1').addEventListener('click', "
              "() => times.push(performance.now()))")
        eng.play([steps[0], steps[0]], PlayOptions(repeat=3, timing="fixed", interval_ms=100, repeat_pause_ms=700,
                                                   markers=False))
        wait_for(lambda: eng.mode == "idle", 10)
        drain()
        t = js(a, "JSON.stringify(times.map(Math.round))")
        gaps = [b - a_ for a_, b in zip(*(lambda v: (v, v[1:]))(__import__("json").loads(t)))]
        c.check(len(gaps) == 5 and all(80 <= gaps[k] <= 250 for k in (0, 2, 4)) and all(650 <= gaps[k] <= 950 for k in (1, 3)),
                f"fixed interval sleeps between rounds (gaps {gaps} ms)")

        print("Image checks on a canvas game (game mode)")
        g = cl.send("browsingContext.create", {"type": "tab", "background": True})["context"]
        cl.send("browsingContext.navigate", {"context": g, "url": f"http://127.0.0.1:{p1}/game.html",
                                              "wait": "complete"})
        cl.send("browsingContext.activate", {"context": g})
        eng.set_target(g)
        time.sleep(0.3)

        def template(box):
            return base64.b64encode(eng._capture(cl, g, box)).decode()
        ready_img = template((100, 100, 160, 60))
        js(g, "score = 5")
        time.sleep(0.2)
        stop_img = template((500, 100, 120, 60))
        js(g, "score = 0")
        time.sleep(0.2)

        def check(name, box, image, **kw):
            return Step(delay_ms=0, x=box[0] + box[2] / 2, y=box[1] + box[3] / 2, kind="check", box=list(box),
                        image=image, name=name, **kw)
        ready = check("READY", (100, 100, 160, 60), ready_img, timeout_ms=3000, on_found="click", on_missing="stop")
        stop_sign = check("STOP", (500, 100, 120, 60), stop_img, timeout_ms=0, on_found="stop",
                          on_missing="continue")
        c.check(eng.test_check(ready)[0] and not eng.test_check(stop_sign)[0], "test_check sees READY, not STOP")
        point = eng.test_check(ready)[2]
        c.check(point is not None and abs(point[0] - 180) <= 1 and abs(point[1] - 130) <= 1,
                f"a found image is clicked at its centre (180, 130); got {point}")
        gone = Step.from_dict({**ready.to_dict(), "wait_for": "disappear"})
        js(g, "ready = false")
        time.sleep(0.2)
        c.check(eng.test_check(gone)[0], "a “disappears” check passes once the image is gone")
        js(g, "ready = true")
        time.sleep(0.2)
        c.check(Step.from_dict(ready.to_dict()).template().width == 160, "check steps survive save/load")

        def play_until_idle(steps_, opts, timeout=30):
            eng.play(steps_, opts)
            ok = wait_for(lambda: eng.mode == "idle", timeout)
            logs = []
            while True:
                try:
                    kind, payload = events.get_nowait()
                except queue.Empty:
                    break
                if kind == "log":
                    logs.append(payload)
            return ok, logs

        # While playing, the window "loses focus" twice; game mode keeps the game listening.
        blur_twice = threading.Timer(0.4, lambda: (js(g, "dispatchEvent(new FocusEvent('blur'))"),
                                                   time.sleep(1.0), js(g, "dispatchEvent(new FocusEvent('blur'))")))
        blur_twice.start()
        ok, logs = play_until_idle([ready, stop_sign], PlayOptions(repeat=0, game_mode=True, markers=True))
        blur_twice.join()
        c.check(ok and js(g, "score") == 5, f"waited for READY and clicked it 5 times (score {js(g, 'score')})")
        c.check(any("stopped by check #2 (STOP)" in m for m in logs), "the STOP check ended playback")
        c.check(sum("found (100% match) → clicked it at 180, 130" in m for m in logs) == 5,
                "each found check logs where it clicked")
        c.check(js(g, "ignored") == 0 and js(g, "focused"), "game mode kept the game focused (no clicks ignored)")
        c.check(js(g, "bad") == 0, "no stray clicks")
        c.check(js(g, "document.querySelector('tab-autoclicker-ui') === null"), "markers were not left behind")

        js(g, "score = 0; dispatchEvent(new FocusEvent('blur'))")
        ok, logs = play_until_idle([ready], PlayOptions(repeat=1, game_mode=False), 10)
        c.check(js(g, "ignored") >= 1 and js(g, "score") == 0,
                "without game mode an unfocused game ignores the click (control test)")
        js(g, "dispatchEvent(new FocusEvent('focus')); score = 0; ignored = 0")

        never = check("never", (500, 100, 120, 60), stop_img, timeout_ms=0)
        bad_click = Step(delay_ms=0, x=150, y=420)
        ok, logs = play_until_idle([Step.from_dict({**never.to_dict(), "on_missing": "skip"}), bad_click,
                                    Step(delay_ms=0, x=150, y=120)], PlayOptions(repeat=2, game_mode=True))
        c.check(js(g, "bad") == 0 and js(g, "score") >= 1, "“skip the next step” skipped the click after the check")
        t0 = time.monotonic()
        ok, logs = play_until_idle([Step.from_dict({**never.to_dict(), "on_missing": "restart"}), bad_click],
                                   PlayOptions(repeat=3, game_mode=True, repeat_pause_ms=0))
        c.check(js(g, "bad") == 0 and any("in 3 repeats" in m for m in logs),
                "“start the next repeat” goes back to step 1")
        c.check(time.monotonic() - t0 >= 0.5, "a round of only instant checks does not spin")

        js(g, "score = 0")
        goto = Step.from_dict({**ready.to_dict(), "on_found": "click_preset", "goto_preset": "Raid"})
        ok, logs = play_until_idle([goto, bad_click], PlayOptions(repeat=0, game_mode=True))
        c.check(ok and js(g, "score") == 1 and js(g, "bad") == 0, "“click it, then go to preset” clicks once and ends")
        c.check(any("clicked it at 180, 130, then go to preset “Raid”" in m for m in logs),
                "the log says which preset comes next")
        drain()
        jumps = []
        eng.notify = lambda kind, payload: jumps.append(payload) if kind == "goto_preset" else events.put((kind, payload))
        play_until_idle([Step.from_dict({**never.to_dict(), "on_missing": "preset"})], PlayOptions(repeat=1))
        eng.notify = lambda kind, payload: events.put((kind, payload))
        c.check(jumps == [""], f"“go to preset” (next in the list) is reported to the window ({jumps})")

        js(g, "score = 0; bad = 0")
        wait_for(lambda: js(g, "ready"), 3)
        aimed = Step.from_dict({**ready.to_dict(), "click_x": 50, "click_y": 320})  # = (150, 420), outside the box
        where = eng.test_check(aimed)[2]
        c.check(where == (150.0, 420.0), f"a chosen click point is reported by Test now ({where})")
        ok, logs = play_until_idle([aimed], PlayOptions(repeat=1, game_mode=True))
        c.check(js(g, "bad") == 1 and js(g, "score") == 0 and any("clicked it at 150, 420" in m for m in logs),
                "“click it” clicks the chosen point instead of the middle")
        c.check(Step.from_dict(aimed.to_dict()).click_x == 50, "the click point survives save/load")

        print("Jumps to steps")
        from tab_autoclicker.engine import EngineError  # noqa: E402

        def fresh():
            wait_for(lambda: js(g, "ready"), 3)
            js(g, "score = 0; bad = 0; order = ''")
        press_ready = Step(delay_ms=0, x=180, y=130)
        branch_bad = Step(delay_ms=0, x=150, y=420, only_on_jump=True)
        fresh()
        jump = Step.from_dict({**never.to_dict(), "on_missing": "step", "goto_step": branch_bad.uid})
        ok, logs = play_until_idle([jump, press_ready, branch_bad], PlayOptions(repeat=1))
        c.check(js(g, "order") == "BR", f"a jump runs the “only when jumped to” step, then comes back ({js(g, 'order')})")
        fresh()
        ok, logs = play_until_idle([press_ready, branch_bad], PlayOptions(repeat=2, repeat_pause_ms=1000))
        c.check(js(g, "order") == "RR", f"normal order skips “only when jumped to” steps ({js(g, 'order')})")
        fresh()
        plain_bad = Step(delay_ms=0, x=150, y=420)
        goto = Step.from_dict({**never.to_dict(), "on_missing": "step", "goto_step": press_ready.uid})
        ok, logs = play_until_idle([goto, plain_bad, press_ready], PlayOptions(repeat=1))
        c.check(js(g, "order") == "R", f"a jump to a normal step carries on from there ({js(g, 'order')})")
        fresh()
        click_jump = Step.from_dict({**ready.to_dict(), "on_found": "click_step", "goto_step": branch_bad.uid})
        ok, logs = play_until_idle([click_jump, branch_bad], PlayOptions(repeat=1))
        c.check(js(g, "order") == "RB" and any("clicked it at 180, 130, then go to step #2" in m for m in logs),
                f"“click it, then go to step” clicks, then runs that step ({js(g, 'order')})")
        fresh()
        b2 = Step(delay_ms=0, x=150, y=420, only_on_jump=True)
        b1 = Step.from_dict({**never.to_dict(), "on_missing": "step", "goto_step": b2.uid, "only_on_jump": True})
        enter = Step.from_dict({**ready.to_dict(), "on_found": "click_step", "goto_step": b1.uid})
        ok, logs = play_until_idle([enter, b1, b2], PlayOptions(repeat=1))
        c.check(js(g, "order") == "RB", f"a chain of jumps between marked steps runs each once ({js(g, 'order')})")
        gone_target = Step.from_dict({**never.to_dict(), "on_missing": "step", "goto_step": "nope"})
        ok, logs = play_until_idle([gone_target, press_ready], PlayOptions(repeat=1))
        c.check(any("goes to a step that was deleted" in m for m in logs), "a jump to a deleted step stops safely")
        try:
            eng.play([branch_bad], PlayOptions())
            c.check(False, "refuses to play when every step waits for a jump")
        except EngineError:
            c.check(True, "refuses to play when every step waits for a jump")
        loop = Step.from_dict({**never.to_dict(), "on_missing": "step"})
        loop.goto_step = loop.uid
        t0 = time.monotonic()
        eng.play([loop], PlayOptions(repeat=1))
        time.sleep(1.0)
        eng.stop_play()
        wait_for(lambda: eng.mode == "idle", 5)
        c.check(time.monotonic() - t0 < 3, "a check that jumps to itself does not hang, and Stop works")
        drain()

        print("Click delay and Test and click")
        fresh()
        js(g, "window.readyAt = 0; window.clickAt = 0;"
              "c.addEventListener('mousedown', () => { clickAt = performance.now(); }, true);")
        delayed = Step.from_dict({**ready.to_dict(), "click_delay_ms": 700})
        t0 = time.monotonic()
        ok, logs = play_until_idle([delayed], PlayOptions(repeat=1, game_mode=True))
        took = time.monotonic() - t0
        c.check(js(g, "score") == 1 and took >= 0.7 and any("clicked it at 180, 130 after 0.7 s" in m for m in logs),
                f"playback waits the click delay before clicking ({took:.2f} s)")
        fresh()
        t0 = time.monotonic()
        passed, score, point, clicked = eng.test_and_click(Step.from_dict({**ready.to_dict(), "click_delay_ms": 400}), True)
        took = time.monotonic() - t0
        c.check(passed and clicked and point == (180.0, 130.0) and took >= 0.4 and wait_for(lambda: js(g, "score") == 1, 2),
                f"Test and click finds it, waits, and clicks once ({took:.2f} s)")
        fresh()
        res = eng.test_and_click(Step.from_dict({**ready.to_dict(), "on_found": "continue"}))
        time.sleep(0.3)
        c.check(res[0] and not res[3] and js(g, "score") == 0, "Test and click does not click when “when found” is “go on”")

        fresh()
        js(g, "window.stamps = []; c.addEventListener('mousedown', (e) => stamps.push([e.offsetY < 300 ? 'R' : 'B', "
              "performance.now()]), true)")
        waiting = Step.from_dict({**ready.to_dict(), "after_ms": 800})
        missing_wait = Step.from_dict({**never.to_dict(), "after_ms": 800, "on_missing": "continue"})
        ok, logs = play_until_idle([waiting, Step(delay_ms=0, x=150, y=420), missing_wait,
                                    Step(delay_ms=0, x=150, y=420)], PlayOptions(repeat=1, timing="fixed", interval_ms=50))
        st = js(g, "JSON.stringify(stamps)")
        import json as _json
        st = _json.loads(st)
        gap_ok = len(st) == 3 and 750 <= st[1][1] - st[0][1] <= 1300 and st[2][1] - st[1][1] < 400
        c.check(gap_ok and any("(after waiting 0.8 s)" in m for m in logs),
                f"“wait after” waits only after a successful check ({[(k, round(t - st[0][1])) for k, t in st]})")

        print("Click again when the image is still there")
        fresh()
        js(g, "window.swallow = 2; c.addEventListener('mousedown', (e) => {"
              " if (swallow > 0) { swallow--; e.stopImmediatePropagation(); } }, true)")
        retrying = Step.from_dict({**ready.to_dict(), "retry_clicks": 3, "retry_ms": 120})
        ok, logs = play_until_idle([retrying], PlayOptions(repeat=1, game_mode=True))
        c.check(js(g, "score") == 1 and any("clicking again (3 of 4)" in m for m in logs)
                and any("gone after 3 clicks" in m for m in logs),
                f"missed clicks are repeated until the image goes away ({js(g, 'score')})")
        fresh()
        # The first click makes the game think it went to the background.
        js(g, "ignored = 0; c.addEventListener('mousedown', () => { focused = false; }, {capture: true, once: true})")
        ok, logs = play_until_idle([retrying], PlayOptions(repeat=1, game_mode=True))
        c.check(js(g, "score") == 1 and js(g, "ignored") == 1,
                f"a retry tells the game again that it has focus ({js(g, 'score')}, ignored {js(g, 'ignored')})")
        fresh()
        # A game whose input got stuck: it ignores clicks until it loses focus once.
        js(g, "window.stuck = true; addEventListener('blur', () => { stuck = false; });"
              " c.addEventListener('mousedown', (e) => { if (stuck) e.stopImmediatePropagation(); }, true)")
        ok, logs = play_until_idle([retrying], PlayOptions(repeat=1, game_mode=True))
        c.check(js(g, "score") == 1 and any("resetting the input" in m and "focus losses hidden" in m for m in logs),
                f"a retry resets a game whose input got stuck, like Stop and Play ({js(g, 'score')})")
        fresh()
        js(g, "window.swallow = 99")
        once = Step.from_dict({**ready.to_dict(), "retry_clicks": 1, "retry_ms": 120})
        ok, logs = play_until_idle([once], PlayOptions(repeat=1, game_mode=True))
        c.check(js(g, "score") == 0 and any("still there after 2 clicks; going on anyway" in m for m in logs),
                "gives up after the set number of extra clicks")
        js(g, "swallow = 0")

        print("Game mode pauses while the game tab is hidden")
        js(g, "score = 0")
        cl.send("browsingContext.activate", {"context": a})
        eng.play([Step(delay_ms=100, x=150, y=120)], PlayOptions(repeat=1, game_mode=True))
        time.sleep(1.0)
        c.check(eng.mode == "playing" and js(g, "score") == 0, "no clicks while the game tab is hidden")
        cl.send("browsingContext.activate", {"context": g})
        c.check(wait_for(lambda: eng.mode == "idle", 5) and js(g, "score") == 1, "playback resumes once visible")
        drain()

        print("Window size")
        eng.set_target(g)
        before = eng.window_size()
        c.check(before["page"][0] > 0 and before["window"][0] >= before["page"][0], f"reads the sizes ({before})")
        got = eng.resize_page(700, 480)
        c.check(got == (700, 480) and js(g, "innerWidth") == 700 and js(g, "innerHeight") == 480,
                f"resizes the window so the page is exactly 700 × 480 ({got})")
        ok, logs = play_until_idle([Step(delay_ms=0, x=5, y=5)], PlayOptions(repeat=1, window_size=(640, 400)))
        c.check(js(g, "innerWidth") == 640 and js(g, "innerHeight") == 400 and any("Resized the window" in m for m in logs),
                "“resize before playing” sets the saved page size first")
        eng.resize_page(*before["page"])

        print("Save/load and reconnect")
        c.check([Step.from_dict(s.to_dict()) for s in steps] == steps, "steps survive a save/load round trip")
        eng.disconnect()
        eng2 = Engine(lambda *a: None)
        try:
            eng2.connect(port)
            c.check(True, "a new session can be opened after disconnect (session ended cleanly)")
        except Exception as exc:  # noqa: BLE001
            c.check(False, f"reconnect failed: {exc}")
        finally:
            eng2.disconnect()
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
