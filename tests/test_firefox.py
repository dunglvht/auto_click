"""Profile settings the app writes (no browser needed): python3 tests/test_firefox.py"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tab_autoclicker import firefox  # noqa: E402

failed = 0


def check(ok: bool, what: str) -> None:
    global failed
    failed += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  {what}")


def user_js(d: str) -> str:
    with open(os.path.join(d, "user.js"), encoding="utf-8") as fh:
        return fh.read()


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "user.js"), "w", encoding="utf-8") as fh:
            fh.write('user_pref("my.own.pref", 1);\n')
        check(firefox.hide_sidebar(d, True) and '"sidebar.visibility", "hide-launcher"' in user_js(d),
              "hides the sidebar launcher (horizontal tabs: hide-launcher)")
        check(not firefox.hide_sidebar(d, True), "a second time changes nothing")
        check(firefox.hide_sidebar(d, False) and "sidebar.visibility" not in user_js(d)
              and "my.own.pref" in user_js(d), "unticked: removes only the app's line, the user's lines stay")
        with open(os.path.join(d, "user.js"), "a", encoding="utf-8") as fh:
            fh.write('user_pref("sidebar.visibility", "always-show");\n')
        check(not firefox.hide_sidebar(d, False) and '"always-show"' in user_js(d),
              "a sidebar setting the user wrote is never removed")
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "prefs.js"), "w", encoding="utf-8") as fh:
            fh.write('user_pref("sidebar.verticalTabs", true);\n')
        firefox.hide_sidebar(d, True)
        check('"sidebar.visibility", "hide-sidebar"' in user_js(d), "with vertical tabs: hide-sidebar")
    print("ALL CHECKS PASSED" if not failed else f"{failed} CHECK(S) FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
