"""Turn per-field OCR text (from regions.py's FIELD_BOXES + ocr.py's
read_field()) into structured HP/MP/EXP/LV values.

Each field is OCR'd from its own tightly-cropped, isolated box now (see the
2026-08-17 recognition-only rework in ocr.py/capture.py) -- there's exactly
one string per field, always. Earlier versions of this module had to handle
RapidOCR sometimes merging a label+value into one detected box and sometimes
splitting them into two ('HP' + '[506/824]'), with a position-based
nearest-neighbor fallback for the split case; that's gone now, because
per-field cropping means there's no "other box" to merge or split against --
regex against the one string is always enough.

EXP is shown by the game as `cur[percentage%]` together, e.g. `162950[38.05%]`.
The '.' and/or closing ']' are the most OCR-fragile part of the string (observed
dropped in testing, e.g. '4980%' instead of '49.80%') -- normalized by treating
a bare >=3-digit run before '%' as implying 2 decimal places, since that's this
game's percentage precision.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_PAIR_RE = {
    "HP": re.compile(r"HP\D{0,3}(\d+)\D+(\d+)", re.IGNORECASE),
    "MP": re.compile(r"MP\D{0,3}(\d+)\D+(\d+)", re.IGNORECASE),
}
# The game renders EXP as `cur[pct%]`, so the opening bracket is structure,
# not decoration: a read without one is broken and must not yield a number.
# Measured over 12,384 live reads, requiring it costs 0.4% -- and those are
# garbage like 'EXP101332182' (booked +101,322,049 of phantom gain before this)
# and 'EXP357041183.37%]', where the missing bracket merged 357041 and 183 into
# one number. OCR reads '[' as '(' or '{' often enough to accept those too.
_EXP_CUR_RE = re.compile(r"EXP\D{0,3}(\d+)\s*[\[({]", re.IGNORECASE)
# Percentage is 0-99.99. Separator between the two digit groups is normally
# '.', but OCR sometimes drops it entirely (bare 3-4 digit run) or -- seen
# with recognition-only OCR on this tiny font -- reads it as a space, a colon,
# or inserts a stray space around the dot ('63 14%', '75:11%', '53 .73%'). The
# separator class is `+` (one-or-more) precisely for that last form, where the
# dot survives but a space lands in front of it; a bare 3-4 digit run (no
# separator at all) stays its own alternative so a dot-free read still parses.
# A bare 1-2 digit run is deliberately NOT matched, ambiguous with stray
# adjacent OCR noise.
_EXP_PCT_RE = re.compile(r"(\d{1,2}[\s.:]+\d{1,2}|\d{3,4})\s*%")
_LV_RE = re.compile(r"LV\.?\D{0,3}(\d+)", re.IGNORECASE)
# How far to the right of a bare 'LV.' detection label to widen the field
# box so the per-tick recognition read covers the level digits (see the
# label-only fallback in find_stat_fields, 2026-09-06).
_LV_LABEL_EXTEND_PX = 110

# Detection-text patterns for locating the stat panel fields in a full-frame
# detection pass (see find_stat_fields) -- the same regexes the per-field
# recognition path uses, applied to the boxes detection finds.
_STAT_FIELD_PATTERNS: dict[str, re.Pattern] = {
    "LV": _LV_RE,
    "HP": _PAIR_RE["HP"],
    "MP": _PAIR_RE["MP"],
    "EXP": _EXP_CUR_RE,
}

# Meso counter (inventory open) renders as digits with comma separators,
# e.g. "1,234,567". OCR drops or mangles commas regularly, so accept any
# digit/comma run and strip the commas -- the digits alone are the value.
_MESO_RE = re.compile(r"[\d,]+")
# Anything beyond a quadrillion is OCR garbage, not a real counter (the
# classic client caps mesos far lower; even a raised-cap server never
# approaches this). Guards against a stray digit run from some other
# text on screen.
_MESO_MAX = 10**15
# A detection box counts as the meso counter only if its text is PURE
# digits/commas (the counter renders as '154,821' with no label in the
# same box). Item stack counts qualify too -- they're just smaller (see
# find_meso_from_boxes' scoring).
_MESO_PURE_DIGIT_RE = re.compile(r"^[\d,]+$")
# The stat panel strip (LV/HP/MP/EXP) sits at the bottom of the client --
# its LV value is also pure digits and must never be picked as meso.
_STAT_STRIP_MARGIN = 50
# The strip also sits in the bottom band of the frame (post-patch: LV's top
# edge is at 0.956 of the height on the 1366 client, 0.962 on a 2560 frame).
# Detection is only allowed to match fields inside this band, which is what
# keeps a MONSTER'S name tag out: it renders 'Lv.48 大幽靈' -- a literal
# _LV_RE match -- but sits up in the play field (measured 0.69 of the height
# on the 2026-09-11 capture). Only applied when the caller passes the frame
# size.
_STAT_BAND_FRAC = 0.88


@dataclass
class StatSnapshot:
    level: int | None
    hp_cur: int | None
    hp_max: int | None
    mp_cur: int | None
    mp_max: int | None
    exp_cur: int | None
    exp_pct: float | None


def _normalize_pct(raw: str) -> float:
    # Recognition-only OCR on the tiny EXP field font reads the decimal point
    # as a space ('63 14%') or colon ('75:11%'), or inserts a stray space around
    # it ('53 .73%'). When any separator survives, collapse the whole separator
    # run to a single dot. A bare digit run (dot dropped entirely, e.g. '5373'
    # for '53.73') has no separator, so fall back to treating the last two
    # digits as the fraction -- this game's percentage is always 2dp.
    if re.search(r"[\s:.]", raw):
        return float(re.sub(r"[\s:.]+", ".", raw).strip("."))
    if len(raw) > 2:
        return float(f"{raw[:-2]}.{raw[-2:]}")
    return float(raw)


def _find_pair(label: str, text: str) -> tuple[int | None, int | None]:
    m = _PAIR_RE[label].search(text)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def _find_exp(text: str) -> tuple[int | None, float | None]:
    m = _EXP_CUR_RE.search(text)
    if not m:
        return None, None
    cur = int(m.group(1))
    pm = _EXP_PCT_RE.search(text[m.end():])
    pct = _normalize_pct(pm.group(1)) if pm else None
    return cur, pct


def _find_level(text: str) -> int | None:
    m = _LV_RE.search(text)
    return int(m.group(1)) if m else None


# --- EXP percentage sanity (2026-09-11) ------------------------------------
# The tiny EXP font misreads a leading '6' in the FIRST decimal as '8', and
# only there: over 3248 live reads on 2026-09-11, 182 (5.6%) came back exactly
# +0.19/+0.20 high with the second decimal untouched (81.61 -> '81.80', 82.66
# -> '82.86'), confined to the X.6Y bands. exp_cur was correct on every one of
# those ticks, so exp_cur is the value to trust: exp_cur / pct measures that
# level's EXP requirement T, a fixed constant per level. Because the error is
# one-way (the read is always TOO HIGH, hence the implied T is always TOO
# LOW), a running MAXIMUM over the level estimates T safely -- bad reads can
# only pull it down, never up. A read is rewritten only when it disagrees with
# exp_cur/T by the bug's own signature, so a mis-seeded T can never corrupt a
# good read: it just switches the correction off until a clean one arrives.
_EXP_PCT_BUG_LO, _EXP_PCT_BUG_HI = 0.15, 0.25  # the '6'->'8' signature (+0.20)
_EXP_T_MAX_DRIFT = 1.02  # ignore an implied T this far above the running max


class ExpTotalTracker:
    """Per-level EXP-requirement estimate, used to sanity-check exp_pct.

    Lives in the engine, not the HUD: it is a stateful derivation over
    StatSnapshots and it mutates the snapshot it is handed. One instance per
    overlay; a level change invalidates the estimate.
    """

    def __init__(self) -> None:
        self.total: float | None = None
        self.level: int | None = None

    def sanitize(self, snap: StatSnapshot) -> None:
        """Rewrite `snap.exp_pct` in place when it shows the known misread."""
        pct = snap.exp_pct
        if snap.exp_cur is None or pct is None or not (0.0 < pct < 100.0):
            return
        if snap.level != self.level:
            self.level = snap.level
            self.total = None  # new level -> new EXP requirement
        t_obs = snap.exp_cur / (pct / 100.0)
        if self.total is None or t_obs > self.total:
            # Learn upward only; ignore an implied T far above the max (a pct
            # read with a dropped digit, not a level change -- level changes
            # are handled by the reset above).
            if self.total is None or t_obs <= self.total * _EXP_T_MAX_DRIFT:
                self.total = t_obs
            return
        expected = snap.exp_cur / self.total * 100.0
        if _EXP_PCT_BUG_LO <= pct - expected <= _EXP_PCT_BUG_HI:
            snap.exp_pct = round(expected, 2)


def parse_fields(field_text: dict[str, str]) -> StatSnapshot:
    """field_text: {'LV': ..., 'HP': ..., 'MP': ..., 'EXP': ...} -- the raw
    recognized text for each of regions.py's FIELD_BOXES."""
    hp_cur, hp_max = _find_pair("HP", field_text.get("HP", ""))
    mp_cur, mp_max = _find_pair("MP", field_text.get("MP", ""))
    exp_cur, exp_pct = _find_exp(field_text.get("EXP", ""))
    level = _find_level(field_text.get("LV", ""))
    return StatSnapshot(
        level=level,
        hp_cur=hp_cur, hp_max=hp_max,
        mp_cur=mp_cur, mp_max=mp_max,
        exp_cur=exp_cur, exp_pct=exp_pct,
    )


def parse_meso(text: str | None) -> int | None:
    """Extract the meso counter value from OCR text.

    Accepts '1,234,567', '1234567', and the mangled in-between forms OCR
    actually produces ('1,234567', 'l234,567' won't match the digit run and
    yields None). Returns None for empty/unrecognized input and for
    implausibly large values (OCR garbage from unrelated text).
    """
    m = _MESO_RE.search(text or "")
    if not m:
        return None
    digits = m.group(0).replace(",", "")
    if not digits:
        return None
    value = int(digits)
    if value > _MESO_MAX:
        return None
    return value


def find_meso_candidate(
    boxes: list[tuple[int, int, int, int, str]], frame_size: tuple[int, int]
) -> tuple[int, int, int, int, int] | None:
    """Pick the meso counter out of full-frame detection boxes and return
    (x, y, w, h, value) -- the box so callers can cache the position for
    cheap recognition-only re-reads, plus the parsed value.

    `boxes` are (x, y, w, h, text) from ocr.detect_text(). The meso counter
    is the largest pure-digit text blob on screen that is NOT in the bottom
    stat-panel strip (the LV value there is also pure digits). Item stack
    counts are pure digits too but smaller; when digit counts tie, the lower
    blob wins -- the counter sits at the bottom edge of the inventory
    window, below the item grid. None when nothing qualifies (inventory
    closed)."""
    fw, fh = frame_size
    candidates: list[tuple[int, int, str, int, int, int, int]] = []
    for x, y, w, h, text in boxes:
        stripped = text.strip()
        if not _MESO_PURE_DIGIT_RE.match(stripped):
            continue
        if y + h > fh - _STAT_STRIP_MARGIN:
            continue  # stat-panel strip: LV/HP/MP/EXP live here
        digits = stripped.replace(",", "")
        if not digits:
            continue
        if int(digits) > _MESO_MAX:
            continue
        candidates.append((len(digits), y, stripped, x, y, w, h))
    if not candidates:
        return None
    # Most digits wins; ties break toward the bottom of the screen.
    candidates.sort(key=lambda c: (c[0], c[1]))
    _, _, text, x, y, w, h = candidates[-1]
    value = parse_meso(text)
    if value is None:
        return None
    return x, y, w, h, value


def find_meso_from_boxes(
    boxes: list[tuple[int, int, int, int, str]], frame_size: tuple[int, int]
) -> int | None:
    """Value-only convenience over find_meso_candidate (see its docstring)."""
    found = find_meso_candidate(boxes, frame_size)
    return found[4] if found is not None else None


def find_meso_in_region(
    boxes: list[tuple[int, int, int, int, str]],
) -> tuple[int, int, int, int, int] | None:
    """Pick the meso counter out of detection boxes inside a user-marked meso
    region (see capture.ManualScreenCapture). Returns (x, y, w, h, value).

    Unlike find_meso_candidate this has no bottom-strip filter or "ties break
    toward the bottom" tie-break: the region is already scoped to the counter
    by the user, so the rule is simply the pure-digit blob with the most
    digits (item stack counts are smaller). None when nothing qualifies.
    """
    best: tuple[int, int, int, int, int, int] | None = None  # (ndigits, x, y, w, h, value)
    for x, y, w, h, text in boxes:
        stripped = text.strip()
        if not _MESO_PURE_DIGIT_RE.match(stripped):
            continue
        digits = stripped.replace(",", "")
        if not digits:
            continue
        value = int(digits)
        if value > _MESO_MAX:
            continue
        if best is None or len(digits) > best[0]:
            best = (len(digits), int(x), int(y), int(w), int(h), value)
    if best is None:
        return None
    return best[1], best[2], best[3], best[4], best[5]


# ---- coin-icon verification (2026-09-06, method A) -----------------------
# find_meso_candidate trusts "the largest pure-digit blob on screen", which a
# stray LARGER number elsewhere (chat, floating damage, another window) can
# steal. Method A (user's suggestion, 2026-09-06): a candidate is only the
# meso counter when the coin icon sits immediately to its LEFT -- the classic
# inventory's meso row renders  [coin icon] [1,371,339] 楓幣. The coin is
# found by colour (golden yellow), not template matching, so it survives
# Magpie upscaling (colour doesn't change with scale). Candidates are tried
# in the old order (most digits, then lower on screen) and the first one with
# enough gold pixels to its left wins.
#
# Tuning measured on samples/maple_story_ui_20260906_1841x1035.png: the real
# meso box has ~157 gold pixels in the lookbehind window; a floating damage
# number ("1048") has 0. Threshold 60 keeps a comfortable margin on both
# sides (a 1.348x downscale to the native 1366x768 still leaves ~87).
_GOLD_LEFT_LOOKBACK = 90  # minimum lookback, in reference-1366 pixels
# ...but the icon's distance from the digits scales with the UI (see the
# 2026-09-11 note below), so the window is at least this many ROW HEIGHTS.
_GOLD_LEFT_LOOKBACK_ROWS = 6.0
_GOLD_LEFT_VPAD = 12      # vertical padding above/below the box
_GOLD_MIN_PX = 60         # gold pixels required in that window

# ---- second-row (楓葉點數) verification (2026-09-11) ----------------------
# The coin check alone still admitted floating damage numbers: any yellow
# thing in the game world within 90px to their left cleared the threshold.
# Measured after the coin check shipped -- samples/maple_story_ui_patched_
# 2560x1440.png (inventory CLOSED) yields value=180, and the live log booked
# value=8 and value=625 the same way. So the coin icon is necessary but not
# sufficient.
#
# The inventory's currency block always renders TWO stacked rows:
#     [coin icon] 1,371,339  楓幣
#     [leaf icon] 0          楓葉點數
# Measured on samples/maple_story_ui_20260906_1841x1035.png: the 楓葉點數
# box sits 26px below the meso box, i.e. 1.30 row-heights, with 19px of
# horizontal overlap; the leaf icon's own area holds 0 coin-gold pixels, so
# it is a genuinely different widget. Requiring that second row is exactly
# "the inventory must be open" -- which is the only time the counter exists,
# and the documented contract of find_meso_candidate_verified (it already
# returns None when the inventory is closed).
_MESO_ROW_DY = (0.6, 2.2)  # second currency row: this many row-heights below


def _count_gold_left_of(frame_rgb, x: int, y: int, w: int, h: int) -> int:
    """Number of coin-gold pixels in the window immediately left of a box.
    `frame_rgb` is a PIL Image or HxWx3 numpy RGB array of the full frame."""
    import numpy as np
    from PIL import Image

    if frame_rgb is None:
        return 0
    hsv = np.asarray(Image.fromarray(np.asarray(frame_rgb)).convert("HSV"))
    H, S, V = hsv[..., 0].astype(int), hsv[..., 1].astype(int), hsv[..., 2].astype(int)
    gold = (H >= 22) & (H <= 48) & (S >= 110) & (V >= 150)
    # The coin icon sits a fixed FRACTION OF THE ROW left of the digits, so the
    # lookbehind window has to grow with the UI scale -- the row height is the
    # scale proxy that needs no extra plumbing. Measured 2026-09-11 on Alex's
    # 2559x1439 (1.87x) frame: the icon ends 92px left of the digits, just past
    # the old fixed 90px window, so the REAL meso row scored 38 gold pixels and
    # was rejected -- the counter "could not be read at max resolution" while
    # 1366/1920 stayed inside the window. 6 row-heights = 90px at the reference
    # row height (~15px) and 174px at 1.87x, which covers every measured frame.
    lookback = max(_GOLD_LEFT_LOOKBACK, int(round(_GOLD_LEFT_LOOKBACK_ROWS * h)))
    x0, x1 = max(0, x - 4 - lookback), max(0, x - 4)
    y0, y1 = max(0, y - _GOLD_LEFT_VPAD), min(gold.shape[0], y + h + _GOLD_LEFT_VPAD)
    if x1 <= x0 or y1 <= y0:
        return 0
    return int(gold[y0:y1, x0:x1].sum())


def _has_currency_row_below(
    boxes: list[tuple[int, int, int, int, str]], x: int, y: int, w: int, h: int
) -> bool:
    """True when another currency row sits directly under this candidate box.

    The inventory's 楓幣 row always has the 楓葉點數 row beneath it (geometry
    in the module note above _MESO_ROW_DY); a damage number out in the game
    world never does. Deliberately "some box, any text" rather than "a
    pure-digit box": most players' 楓葉點數 counter renders as a bare '0', and
    detection then merges it with its own label ('0楓葉點數'), so demanding a
    clean digit box would reject the real row.
    """
    lo, hi = _MESO_ROW_DY[0] * h, _MESO_ROW_DY[1] * h
    pad = max(10, w // 2)  # the row below may be indented / start at its icon
    for bx, by, bw, bh, _text in boxes:
        dy = by - y
        if not (lo <= dy <= hi):
            continue
        # Both currency rows are rendered in the SAME font, so their boxes are
        # the same height. That ratio is what separates the real pair from an
        # unrelated text box that merely happens to sit just below a floating
        # damage number (measured: the 2560 patched sample pairs a 70px-tall
        # '180' with a 30px game-world label 88px under it -- rejected here).
        if not (0.6 * h <= bh <= 1.6 * h):
            continue
        if min(x + w + pad, bx + bw) - max(x - pad, bx) > 0:
            return True
    return False


def find_meso_candidate_verified(
    boxes: list[tuple[int, int, int, int, str]],
    frame_rgb,
    frame_size: tuple[int, int],
) -> tuple[int, int, int, int, int] | None:
    """find_meso_candidate plus the coin-icon and second-row checks.

    Same candidate filtering/order; the first candidate that has the coin icon
    to its left AND the 楓葉點數 row directly under it wins (module notes
    above). None when nothing qualifies (inventory closed) or no candidate
    passes both checks. Manual mode keeps using find_meso_in_region -- the user
    already scoped that region to the counter.
    """
    fw, fh = frame_size
    candidates: list[tuple[int, int, str, int, int, int, int]] = []
    for x, y, w, h, text in boxes:
        stripped = text.strip()
        if not _MESO_PURE_DIGIT_RE.match(stripped):
            continue
        if y + h > fh - _STAT_STRIP_MARGIN:
            continue  # stat-panel strip: LV/HP/MP/EXP live here
        digits = stripped.replace(",", "")
        if not digits:
            continue
        if int(digits) > _MESO_MAX:
            continue
        candidates.append((len(digits), y, stripped, x, y, w, h))
    candidates.sort(key=lambda c: (c[0], c[1]))
    for _, _, text, x, y, w, h in candidates:
        if _count_gold_left_of(frame_rgb, x, y, w, h) < _GOLD_MIN_PX:
            continue
        if not _has_currency_row_below(boxes, x, y, w, h):
            continue
        value = parse_meso(text)
        if value is None:
            continue
        if value == 0:
            # A coin-backed zero is order-of-magnitude wrong for any
            # player with actual meso (the classic counter renders the
            # real balance, not '0', except when genuinely broke). An OCR
            # glitch flashing 0 would wipe the HUD/confirm-dialog balance
            # and seed a bogus session baseline (reported 2026-09-07), so
            # skip the reading and keep the previous value.
            continue
        return x, y, w, h, value
    return None


def find_stat_fields(
    boxes: list[tuple[int, int, int, int, str]],
    frame_size: tuple[int, int] | None = None,
) -> dict[str, tuple[int, int, int, int]]:
    """Locate the stat panel fields in a full-frame detection pass.

    Matches each detection box's text against the known panel patterns
    ('LV. 32', 'HP[602/602]', 'MP[...]', 'EXP...[%]') and returns the box
    (x, y, w, h) per field found. Works on magnified frames too -- the text
    is bigger but the patterns are unchanged, which is what makes the HUD
    survive screen magnifiers (Megapipe). Empty dict when the panel is not
    visible (covered / not rendered / OCR failed).

    The LOWEST matching box wins, not the first one detection happens to
    list, and (when `frame_size` is given) only boxes inside the bottom
    _STAT_BAND_FRAC of the frame are eligible at all. A monster's name tag
    renders 'Lv.48 大幽靈', which _LV_RE matches exactly, and it sits
    mid-screen -- taking the first match made a monster tag the LV field
    whenever a locate pass ran while one was visible, and the derived
    quickbar geometry then measured its UI scale from that bogus span and
    landed in the status strip (2026-09-11: quickbar slots came back {} /
    {7: 1} on the 2560 frame, while the same frame with the real LV anchor
    reads {8: 535}).
    """
    if frame_size is None:
        return _find_stat_fields_in(boxes)
    band = _STAT_BAND_FRAC * frame_size[1]
    found = _find_stat_fields_in([b for b in boxes if b[1] >= band])
    if found:
        return found
    # Nothing matched inside the strip band at all -- e.g. a manually marked
    # region much taller than the strip, where the fields are nowhere near its
    # bottom 12%. Report the old, unfiltered result rather than nothing.
    return _find_stat_fields_in(boxes)


def _find_stat_fields_in(
    boxes: list[tuple[int, int, int, int, str]],
) -> dict[str, tuple[int, int, int, int]]:
    found: dict[str, tuple[int, int, int, int]] = {}
    digit_boxes: list[tuple[int, int, int, int, str]] = []
    for x, y, w, h, text in boxes:
        stripped = text.strip()
        matched = False
        for name, pat in _STAT_FIELD_PATTERNS.items():
            if not pat.search(stripped):
                continue
            if name not in found or y > found[name][1]:
                found[name] = (int(x), int(y), int(w), int(h))
            matched = True
            break
        if not matched and re.fullmatch(r"[\d,]+", stripped):
            digit_boxes.append((int(x), int(y), int(w), int(h), stripped))

    # LV split fallback: detection often splits 'LV. 32' into an 'LV.' box
    # and a separate '32' box (seen on the 1280x868 client frame). Neither
    # matches _LV_RE alone; merge the 'LV.' label with the digit box
    # immediately to its right (vertically overlapping) into one box. Same
    # lowest-wins rule as above.
    if "LV" not in found:
        best: tuple[int, int, int, int] | None = None
        for x, y, w, h, text in boxes:
            stripped = text.strip()
            if not re.match(r"^LV\.?$", stripped, re.IGNORECASE):
                continue
            for dx, dy, dw, dh, _dtext in digit_boxes:
                if dx < x + w - 4 or dx > x + w + 80:
                    continue
                if dy >= y + h or dy + dh <= y:
                    continue
                x0, y0 = min(x, dx), min(y, dy)
                x1, y1 = max(x + w, dx + dw), max(y + h, dy + dh)
                if best is None or y0 > best[1]:
                    best = (x0, y0, x1 - x0, y1 - y0)
                break
        if best is not None:
            found["LV"] = best
    # LV label-only fallback (2026-09-06): on the native 1366x768 client the
    # digit is sometimes NOT detected at all -- the frame yields a bare 'LV.'
    # label box (seen on samples/maple_story_ui_20260906_c.png, and it made
    # the LV field vanish from every tick for minutes in the log while EXP
    # kept reading). No digit box exists to merge, so widen the label box to
    # the right to cover where the digits sit; the per-tick recognition OCR
    # then reads the whole 'LV. 44' and parses normally. 110px covers the
    # digits at 1366-2045px client widths without reaching the HP field.
    if "LV" not in found:
        best = None
        for x, y, w, h, text in boxes:
            stripped = text.strip()
            if not re.match(r"^LV\.?$", stripped, re.IGNORECASE):
                continue
            if best is None or y > best[1]:
                best = (int(x), int(y), int(w) + _LV_LABEL_EXTEND_PX, int(h))
        if best is not None:
            found["LV"] = best
    return found
