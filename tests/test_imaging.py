"""Matcher checks with made-up images (no browser needed): python3 tests/test_imaging.py"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tab_autoclicker import imaging  # noqa: E402


def button(w=60, h=24, bg=(164, 209, 48), text=(255, 255, 255), edge=(25, 25, 25)):
    """A flat button with a white bar ("text") outlined in dark."""
    px = []
    for y in range(h):
        for x in range(w):
            if 8 <= y < 16 and 10 <= x < 50:
                px.append(text if 9 <= y < 15 and 11 <= x < 49 else edge)
            else:
                px.append(bg)
    return imaging.Image(w, h, bytes(v for p in px for v in p))


def recolour(img, f):
    return imaging.Image(img.width, img.height, bytes(
        max(0, min(255, int(round(v)))) for i in range(0, len(img.rgb), 3) for v in f(*img.rgb[i:i + 3])))


def main() -> int:
    tpl = button()
    lighter = recolour(tpl, lambda r, g, b: (r * .6 + 102, g * .6 + 102, b * .6 + 102))  # 40% towards white
    darker = recolour(tpl, lambda r, g, b: (r * .7, g * .7, b * .7))
    red = button(bg=(209, 60, 48))
    grey = button(bg=(177, 177, 177))
    cases = [("same image", tpl, True, True), ("hovered (lighter)", lighter, False, True),
             ("faded (darker)", darker, False, True), ("a red button", red, False, False),
             ("a grey button", grey, False, False)]
    failed = 0
    for fast in (True, False):
        np = imaging._np
        if not fast:
            imaging._np = None
        for name, img, plain_ok, tone_ok in cases:
            for tone, want in ((False, plain_ok), (True, tone_ok)):
                score = imaging.find(tpl, img, 0, 0, 2, 0.8, tone)[0]
                ok = (score >= 0.8) == want
                failed += not ok
                print(f"  {'PASS' if ok else 'FAIL'}  {'numpy' if fast else 'plain'} {name}, "
                      f"{'lighter/darker allowed' if tone else 'exact colours'}: {score * 100:.0f}%")
        imaging._np = np
    print("ALL CHECKS PASSED" if not failed else f"{failed} CHECK(S) FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
