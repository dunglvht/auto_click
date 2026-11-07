"""Make tab_autoclicker/bitheroes/glyphs.json from screenshots with known numbers.

    python3 tests/make_glyphs.py

Every glyph of the labelled texts below becomes a template for reading the
game's numbers (bitheroes.read_numbers). Run it again after adding samples.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tab_autoclicker import bitheroes as bh, imaging  # noqa: E402

DATA = os.path.join(os.path.dirname(__file__), "data")
LISTS = {   # screenshot: the numbers in its five list rows
    "wave_list.png": ["58", "57", "56", "55", "54"],
    "wave_list_scrolled.png": ["56", "55", "54", "53", "52"],
    "gauntlet_ranges.png": ["13,495-20,003", "12,488-13,494", "11,735-12,487", "9,755-11,734", "9,465-9,754"],
    "gauntlet_values.png": ["12,487", "12,477", "12,467", "12,457", "12,447"],
    "gauntlet_values_top.png": ["13,494", "13,484", "13,474", "13,464", "13,454"],
    "gauntlet_values_dragged.png": ["13,054", "13,044", "13,034", "13,024", "13,014"],
}
FIELDS = {"gauntlet_window.png": "14,578", "invasion_window.png": "58"}


def main() -> int:
    samples = []
    for name, texts in LISTS.items():
        img = imaging.decode_png(open(os.path.join(DATA, name), "rb").read())
        for k, text in enumerate(texts):
            top = int(bh.LIST_ROW_TOP + bh.LIST_ROW_PITCH * k)
            samples.append((name, text, bh.glyphs(img, (bh.LIST_X[0], top + 3, bh.LIST_X[1], top + bh.LIST_ROW_H - 3))))
    for name, text in FIELDS.items():
        img = imaging.decode_png(open(os.path.join(DATA, name), "rb").read())
        samples.append((name, text, bh.glyphs(img, bh.FIELD_BOX)))
    templates = {}
    for name, text, lines in samples:
        if not lines or len(lines[0]) != len(text):
            print(f"{name}: expected {len(text)} glyphs for {text!r}, found {len(lines[0]) if lines else 0}")
            return 1
        for char, bits in zip(text, lines[0]):
            templates.setdefault(bits, char)
            if templates[bits] != char:
                print(f"{name}: one glyph is both {templates[bits]!r} and {char!r}")
                return 1
    out = [{"c": c, "bits": b} for b, c in sorted(templates.items(), key=lambda i: i[1])]
    path = os.path.join(os.path.dirname(bh.__file__), "bitheroes", "glyphs.json")
    with open(path, "w", newline="\n") as fh:
        fh.write(json.dumps(out, indent=1) + "\n")
    print(f"{len(out)} templates for {''.join(sorted(set(templates.values())))} in {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
