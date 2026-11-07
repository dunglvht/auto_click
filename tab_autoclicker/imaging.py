"""Tiny image helpers for image checks: PNG decoding and template matching.

Standard library only (numpy is used when installed, it is just faster).
Images are plain RGB byte strings, row by row.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

try:  # optional speed-ups
    import numpy as _np
except ImportError:  # pragma: no cover - depends on the machine
    _np = None
try:
    from PIL import Image as _PIL
except ImportError:  # pragma: no cover - depends on the machine
    _PIL = None

PIXEL_TOLERANCE = 48  # a pixel "matches" if no colour channel differs by more
# "Allow lighter or darker": the image may differ from the template by one
# contrast change (gain) and brightness shift, the same for all colour channels,
# like a hover highlight or a fade. Colours still count: a red or grey button
# does not match a green one.
TONE_GAIN = (0.6, 1.6)
TONE_SHIFT = 90
TONE_SPOTS = 3  # brightness is fitted only at the spots with the best plain match


@dataclass
class Image:
    width: int
    height: int
    rgb: bytes  # width * height * 3 bytes

    def crop(self, x: int, y: int, w: int, h: int) -> "Image":
        x, y = max(0, x), max(0, y)
        w, h = max(0, min(w, self.width - x)), max(0, min(h, self.height - y))
        rows = [self.rgb[((y + r) * self.width + x) * 3:((y + r) * self.width + x + w) * 3] for r in range(h)]
        return Image(w, h, b"".join(rows))


def decode_png(data: bytes) -> Image:
    """Decode an 8-bit, non-interlaced PNG (what Firefox and Tk write)."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG image")
    if _PIL is not None:  # much faster for whole-page screenshots
        import io
        im = _PIL.open(io.BytesIO(data)).convert("RGB")
        return Image(im.width, im.height, im.tobytes())
    pos, idat, palette = 8, [], b""
    width = height = depth = ctype = interlace = None
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", chunk)
        elif kind == b"PLTE":
            palette = chunk
        elif kind == b"IDAT":
            idat.append(chunk)
        elif kind == b"IEND":
            break
    if width is None or depth != 8 or interlace:
        raise ValueError("unsupported PNG (only 8-bit, non-interlaced)")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
    if channels is None:
        raise ValueError(f"unsupported PNG colour type {ctype}")
    raw = zlib.decompress(b"".join(idat))
    stride = width * channels
    out = bytearray(stride * height)
    prev = bytearray(stride)
    src = 0
    for row in range(height):
        ftype = raw[src]
        line = bytearray(raw[src + 1:src + 1 + stride])
        src += 1 + stride
        if ftype == 1:      # Sub
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ftype == 2:    # Up
            line = bytearray((a + b) & 0xFF for a, b in zip(line, prev))
        elif ftype == 3:    # Average
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:    # Paeth
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else b if pb <= pc else c
                line[i] = (line[i] + pred) & 0xFF
        elif ftype != 0:
            raise ValueError(f"bad PNG filter {ftype}")
        out[row * stride:(row + 1) * stride] = line
        prev = line
    if ctype == 2:
        rgb = bytes(out)
    elif ctype == 6:
        rgb = bytes(b for i in range(0, len(out), 4) for b in out[i:i + 3])
    elif ctype == 0:
        rgb = bytes(v for g in out for v in (g, g, g))
    elif ctype == 4:
        rgb = bytes(v for i in range(0, len(out), 2) for v in (out[i],) * 3)
    else:  # palette
        rgb = b"".join(palette[i * 3:i * 3 + 3] for i in out)
    return Image(width, height, rgb)


def _score_at(tpl: Image, img: Image, ox: int, oy: int, need: float) -> float:
    """Share of template pixels matching ``img`` at offset (ox, oy); gives up
    early (returning 0) once the score can no longer reach ``need``."""
    total = tpl.width * tpl.height
    allowed = int(total * (1 - need))
    bad = 0
    tw, iw, t, im = tpl.width, img.width, tpl.rgb, img.rgb
    tol = PIXEL_TOLERANCE
    for r in range(tpl.height):
        ti = r * tw * 3
        ii = ((oy + r) * iw + ox) * 3
        for k in range(0, tw * 3, 3):
            if (abs(t[ti + k] - im[ii + k]) > tol or abs(t[ti + k + 1] - im[ii + k + 1]) > tol
                    or abs(t[ti + k + 2] - im[ii + k + 2]) > tol):
                bad += 1
                if bad > allowed:
                    return 0.0
    return 1.0 - bad / total


def _tone_map(xs, ys) -> tuple[float, float]:
    """Fit y ≈ gain * x + offset (all channels together), within the limits."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    vx = sum((v - mx) ** 2 for v in xs) / n
    gain = sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / n / vx if vx > 1 else 1.0
    gain = min(max(gain, TONE_GAIN[0]), TONE_GAIN[1])
    shift = min(max(my - mx, -TONE_SHIFT), TONE_SHIFT)
    return gain, mx + shift - gain * mx


def _tone_score_at(tpl: Image, img: Image, ox: int, oy: int) -> float:
    """Like ``_score_at``, after fitting a brightness/contrast change."""
    tw, iw = tpl.width, img.width
    t = tpl.rgb
    m = b"".join(img.rgb[((oy + r) * iw + ox) * 3:((oy + r) * iw + ox + tw) * 3] for r in range(tpl.height))
    n, tol, keep, best = len(t) // 3, PIXEL_TOLERANCE, None, 0.0
    for _ in range(2):  # fit, then fit again on the pixels that matched (ignores covered parts)
        idx = [3 * i + c for i in (range(n) if keep is None else keep) for c in range(3)]
        gain, off = _tone_map([t[j] for j in idx], [m[j] for j in idx])
        ok = [i for i in range(n) if all(abs(gain * t[3 * i + c] + off - m[3 * i + c]) <= tol for c in range(3))]
        best = max(best, len(ok) / n)
        if len(ok) < n * 0.3:
            break
        keep = ok
    return best


def _tone_score_np(t, w) -> float:
    """numpy version of ``_tone_score_at`` (t, w: float arrays of shape (n, 3))."""
    keep, best = None, 0.0
    for _ in range(2):
        x, y = (t, w) if keep is None else (t[keep], w[keep])
        mx, my = float(x.mean()), float(y.mean())
        vx = float(((x - mx) ** 2).mean())
        gain = float(((x - mx) * (y - my)).mean()) / vx if vx > 1 else 1.0
        gain = min(max(gain, TONE_GAIN[0]), TONE_GAIN[1])
        shift = min(max(my - mx, -TONE_SHIFT), TONE_SHIFT)
        ok = (_np.abs(t * gain + (mx + shift - gain * mx) - w) <= PIXEL_TOLERANCE).all(1)
        best = max(best, float(ok.mean()))
        if ok.mean() < 0.3:
            break
        keep = ok
    return best


def find(tpl: Image, img: Image, ex: int, ey: int, radius: int, need: float = 0.0,
         tone: bool = False) -> tuple[float, int, int]:
    """Best match of ``tpl`` in ``img`` near the expected offset (ex, ey).

    Returns (score 0..1, x, y). The expected spot is tried first, so the
    usual case (the image is exactly where it was) costs one comparison.
    With ``tone``, the image may also be lighter or darker than the template.
    """
    if tpl.width == 0 or tpl.height == 0:
        return 0.0, ex, ey
    spots = [(dx, dy) for dy in range(-radius, radius + 1) for dx in range(-radius, radius + 1)]
    spots.sort(key=lambda d: d[0] * d[0] + d[1] * d[1])
    valid = [(ex + dx, ey + dy) for dx, dy in spots
             if 0 <= ex + dx and 0 <= ey + dy
             and ex + dx + tpl.width <= img.width and ey + dy + tpl.height <= img.height]
    if not valid:
        return 0.0, ex, ey
    if _np is not None:
        t = _np.frombuffer(tpl.rgb, _np.uint8).reshape(tpl.height, tpl.width, 3).astype(_np.int16)
        a = _np.frombuffer(img.rgb, _np.uint8).reshape(img.height, img.width, 3).astype(_np.int16)
        th, tw = tpl.height, tpl.width
        if len(valid) > 9 and th * tw > 1500:
            # Big image, many spots: rank the spots on every 3rd pixel first.
            ts = t[::3, ::3]
            rough = sorted(((float((_np.abs(a[y:y + th:3, x:x + tw:3] - ts).max(axis=2) <= PIXEL_TOLERANCE).mean()),
                             x, y) for x, y in valid), reverse=True)
            valid = [(x, y) for _, x, y in rough[:5]]
        plain = []
        for x, y in valid:
            score = float((_np.abs(a[y:y + th, x:x + tw] - t).max(axis=2) <= PIXEL_TOLERANCE).mean())
            plain.append((score, x, y))
            if score >= 0.999:
                return plain[-1]
        best = max(plain)
        if tone and best[0] < 0.999:
            # Fitting the brightness is slow, so only try the spots that already match best.
            tf = t.reshape(-1, 3).astype(_np.float32)
            for _, x, y in sorted(plain, reverse=True)[:TONE_SPOTS]:
                win = a[y:y + tpl.height, x:x + tpl.width].reshape(-1, 3).astype(_np.float32)
                score = _tone_score_np(tf, win)
                if score > best[0]:
                    best = (score, x, y)
        return best
    best = (0.0, ex, ey)
    for x, y in valid:
        # Spots that cannot beat ``need`` (or the best so far) are abandoned early.
        score = _score_at(tpl, img, x, y, max(need, best[0]))
        if tone and score < max(need, best[0], 1e-9):
            score = max(score, _tone_score_at(tpl, img, x, y))
        if score > best[0]:
            best = (score, x, y)
            if need and score >= need:
                return best
    if best[0] == 0.0:  # nothing came close: report the real score of the nearest spot
        x, y = valid[0]
        best = (_score_at(tpl, img, x, y, 0.0), x, y)
        if tone:
            best = (max(best[0], _tone_score_at(tpl, img, x, y)), x, y)
    elif tone and best[0] < need:  # report at least the plain score of that spot
        best = (max(best[0], _score_at(tpl, img, best[1], best[2], 0.0)), best[1], best[2])
    return best
