"""The Bit Heroes bot gets a hung game going again: python3 tests/e2e_bot_freeze.py

Starts its own headless Firefox (never your normal one) with a canvas page
that animates for a few seconds and then hangs its main thread for good,
like the game does. The bot must notice and reload it; when it hangs again
right away, the bot must open it in a fresh tab and close the hung one.
"""

from __future__ import annotations

import os
import queue
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

HANG = """<!doctype html><title>Hanging game</title><body style="margin:0;background:#123">
<canvas id="c" width="854" height="545"></canvas>
<script>
const g = document.getElementById('c').getContext('2d');
let n = 0;
(function frame() {
  g.fillStyle = `hsl(${(n++ * 7) % 360},60%,40%)`; g.fillRect(0, 0, 854, 545);
  requestAnimationFrame(frame);
})();
setTimeout(() => { for (;;) {} }, 3000);   // hang for good, like the frozen game
</script>"""


def main() -> int:
    binary = firefox.find_firefox()
    if firefox.is_snap_firefox(binary):
        profile = os.path.join(os.path.expanduser("~"), "snap", "firefox", "common", "tab-autoclicker-bottest")
        shutil.rmtree(profile, ignore_errors=True)
        os.makedirs(profile)
    else:
        profile = tempfile.mkdtemp(prefix="tab-autoclicker-bottest-")
    firefox.set_user_pref(profile, "remote.prefs.recommended", False)
    firefox.set_user_pref(profile, "dom.max_script_run_time", 0)   # no "page is slowing down" prompt
    loads = {"n": 0}

    class Counting(dict):
        """Counts the loads; only the first load hangs (a fresh start cures it)."""
        def get(self, key, default=None):
            if key == "/hang.html":
                loads["n"] += 1
                if loads["n"] > 1:
                    return HANG.replace("for (;;) {}", "")
            return super().get(key, default)
    p = serve(Counting({"/hang.html": HANG}))
    port = free_port()
    proc = subprocess.Popen([binary, "--headless", "--no-remote", "--profile", profile,
                             "--remote-debugging-port", str(port), *firefox.windows_args(), "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    c = Checker()
    logs: queue.Queue = queue.Queue()
    eng = Engine(lambda kind, payload: logs.put(payload) if kind == "log" else None)
    seen: list[str] = []

    def wait_log(text: str, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                line = logs.get(timeout=0.5)
            except queue.Empty:
                continue
            seen.append(line)
            if text in line:
                return True
        return False
    try:
        assert firefox.wait_for_port(port, 60), "headless Firefox did not start"
        eng.connect(port)
        first = eng.open_in_window(f"http://localhost:{p}/hang.html").context
        time.sleep(1)
        bitheroes.FROZEN_S, bitheroes.LOAD_S = 4, 6
        eng.start_bot(bitheroes.BotSettings(order=["pvp"], enabled=["pvp"]))
        c.check(wait_log("Bot started", 30), "the bot starts (page resized to 854 × 545)")
        c.check(wait_log("reloading it", 60), "notices the hung game and tries to reload it")
        c.check(wait_log("opening it again in a new window", 60), "the hung page does not reload: reopens it in a new window")
        time.sleep(25)
        tabs = [t.context for t in eng.list_tabs()]
        c.check(first not in tabs and eng.target and eng.target.context in tabs,
                f"the hung tab is closed and the new one is the target ({len(tabs)} tab(s))")
        c.check(eng.window_size()["page"] == bitheroes.PAGE_SIZE, "the new window has the bot's page size")
        c.check(loads["n"] >= 2, f"the game page was loaded again ({loads['n']} loads)")
        eng.stop_play()
        eng._play_thread.join(30)
        c.check(eng.mode == "idle", "Stop works")
    finally:
        try:
            eng.disconnect()
        finally:
            firefox.stop(proc, 10)
            shutil.rmtree(profile, ignore_errors=True)
    for line in seen:
        print("        log:", line)
    print("ALL CHECKS PASSED" if not c.failed else f"{c.failed} CHECK(S) FAILED")
    return 1 if c.failed else 0


if __name__ == "__main__":
    sys.exit(main())
