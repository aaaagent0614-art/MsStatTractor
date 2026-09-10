"""Crop-box definitions for the stat panel and its fields.

These fixed pixel boxes are a LAST-RESORT fallback only: auto mode normally
locates the panel by OCR detection every pass and persists the detected
fractional positions (see settings.auto_stat_frac), so it self-corrects under
resolution/DPI/magnifier changes. FIELD_BOXES below are used only before the
first successful detection (or if the user never runs auto mode). Manual mode
doesn't use these at all -- it OCRs the user-marked rectangles directly.

2026-09-10 game patch ("改版"): the bottom HUD stopped scaling with the client
size. Measured on native 1366x768 and native 1920x1080 captures, every element
is the SAME pixel size (the HP bar blob is 128x16 in both, LV->HP is 453px in
both) and the whole bottom status strip keeps a fixed ~1334px width that is
horizontally CENTRED. The old linear scale_box() therefore misplaces every
field on a client wider than the 1366 reference.

Consequence: nothing position-based is trustworthy across resolutions any more
-- OCR detection (find_stat_fields) is the real fix, and the derived-geometry
helpers at the bottom of this module let the quickbar follow the detected stat
panel instead of a fixed fraction.
"""
from __future__ import annotations

from dataclasses import dataclass

REFERENCE_CLIENT_SIZE = (1366, 768)  # 2026-09-10 patch: native reference client

# Whole stat strip, in absolute pixels at REFERENCE_CLIENT_SIZE (post-patch).
# Grabbed once per tick with a single mss.grab() call; FIELD_BOXES below are
# sliced out of it in-memory (no extra screen captures).
STAT_PANEL_BOX = (0, 712, 940, 768)  # (left, top, right, bottom)

# Per-field boxes, same reference frame, taken from the tight detection boxes on
# the patched 1366x768 capture (measured 2026-09-10; the 1920x1080 capture
# agrees on every edge). They are deliberately NOT padded: recognition-only OCR
# on a slightly-too-generous crop drops digits and separators, turning into
# wrong values rather than a loud failure ('LV. 47' -> 'LV. @7' at pad 2,
# 'LV. a7' at pad 6). See ocr.read_field for the upscaling half of this.
FIELD_BOXES = {
    "LV": (10, 734, 80, 758),
    "HP": (463, 728, 537, 742),
    "MP": (630, 725, 721, 744),
    "EXP": (807, 729, 917, 742),
}

# The quickbar (8 slots, two rows of four) sits above the right end of the
# status strip -- NOT at a fixed fraction of the frame any more (the strip is
# centred, so the quickbar's fractional position changes with the client
# width). Prefer the derived position (quick_bar_box_from_stat) built from the
# detected LV/EXP boxes; this fraction is only the last-resort fallback for a
# native 1366x768 client.
QUICK_BAR_FRAC = (0.887, 0.802, 0.997, 0.906)  # (left, top, right, bottom) as fractions

# --- Post-patch derived geometry -------------------------------------------
#
# Native-pixel offsets, measured off the patched 1366x768 and 1920x1080
# captures, which agree on all of these (that agreement is the evidence for
# the fixed-pixel layout).
#
# STAT_SPAN_PX: horizontal distance from the LV text's left edge to the EXP
#   text's right edge (1920 capture: 288 -> 1194 = 906; 1366 capture: 9 -> 918
#   = 909). Ratio of it to the reference gives the frame's UI scale, which is
#   what makes this work under Magpie magnification too.
# QUICK_BAR_FROM_STAT: quickbar bounds relative to the status strip's left edge
#   in x and to the frame's BOTTOM edge in y (negative = upward). Sized to match
#   how the game draws the key label row plus the slot content, with a little
#   slack -- measured across all three patched samples, a box this size reads
#   the potion counts on every one of them, while a tighter box (which is what
#   the geometry "should" be) lost the counts on the small 1366 client because
#   the digits are only ~11px tall there.
STAT_SPAN_PX = 907.0
QUICK_BAR_FROM_STAT = (1200.0, 1351.0, -152.0, -72.0)  # (left, right, top, bottom)


@dataclass(frozen=True)
class Box:
    left: int
    top: int
    right: int
    bottom: int

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.right, self.bottom)


def scale_box(box: tuple[int, int, int, int], client_size: tuple[int, int]) -> Box:
    """Scale a box defined at REFERENCE_CLIENT_SIZE to an arbitrary client size.

    Naive linear scaling. Correct when the frame is the reference client
    magnified as a whole (Magpie-style screen magnifiers) -- wrong on a native
    wider client, where the patched UI keeps its pixel size and re-centres
    instead. Detection supersedes both; prefer quick_bar_box_from_stat() /
    the auto-located stat boxes wherever a detection result exists.
    """
    ref_w, ref_h = REFERENCE_CLIENT_SIZE
    cw, ch = client_size
    sx, sy = cw / ref_w, ch / ref_h
    l, t, r, b = box
    return Box(round(l * sx), round(t * sy), round(r * sx), round(b * sy))


def quick_bar_box_from_stat(
    stat_left: float, exp_right: float, frame_size: tuple[int, int],
    bottom_y: float | None = None, clamp: bool = True,
) -> tuple[int, int, int, int] | None:
    """Quickbar bounds derived from the detected status strip, in frame pixels.

    `stat_left` is the LV text's left edge, `exp_right` the EXP text's right
    edge (both in frame pixels, from the same detection pass). Their distance
    measures the frame's UI scale, so the quickbar lands correctly on a native
    client AND under a screen magnifier -- the two cases a fixed fraction
    cannot tell apart (see the module docstring).

    `bottom_y` overrides what counts as the frame's bottom edge. Pass the
    marked stat region's bottom screen edge in manual mode, where the frame is
    the marked strip rather than the whole client; the derived quickbar then
    sits ABOVE the strip, so `clamp` must be False there (clamping would fold
    it back onto the strip).

    Returns None when the span is unusable (nothing detected yet, or the two
    boxes are not actually the same strip).
    """
    w, h = frame_size
    span = float(exp_right) - float(stat_left)
    if span <= 1.0:
        return None
    s = span / STAT_SPAN_PX
    off_l, off_r, off_t, off_b = QUICK_BAR_FROM_STAT
    left = int(round(stat_left + off_l * s))
    right = int(round(stat_left + off_r * s))
    base = float(h if bottom_y is None else bottom_y)
    top = int(round(base + off_t * s))
    bottom = int(round(base + off_b * s))
    if top > bottom:
        top, bottom = bottom, top
    if clamp:
        left, top = max(0, left), max(0, top)
        right, bottom = min(w, right), min(h, bottom)
    if right - left < 20 or bottom - top < 10:
        return None
    return (left, top, right, bottom)
