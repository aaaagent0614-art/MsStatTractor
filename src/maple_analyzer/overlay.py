"""Always-on-top HUD: capture -> OCR -> parse -> session -> redraw, on a timer.

Per-tick timing (2026-08-17 rework, measured against the live game): the
original whole-panel detection+recognition OCR pass was ~600-680ms/tick --
detection (finding text regions) was the entire cost, not recognition. Since
regions.py's FIELD_BOXES already pins down exactly where each field's text
is, detection was pure waste; switched to four small recognition-only calls
(no detection stage) on individually pre-cropped fields, ~15ms each, ~60ms
total. Capture itself is ~3.5ms. `_tick()` now also computes its own elapsed
work time and schedules the next call at `TARGET_MS - elapsed`, floored at
0, instead of the old fixed post-delay (which added TARGET_MS on top of
whatever the work took, so it could never reach the target rate no matter
how fast OCR got) -- this is what actually makes the real cycle approach
TARGET_MS rather than merely bound the *added* delay to it.

UI (2026-08-17 rework, restyled same day per an HTML design preview the user
approved): CustomTkinter, three tabs (Live/History/Settings). Status/session
timer live in their own strip at the top of Live (a pill + a chip, not just
another text row); stats and session info sit in aligned grids with tabular
numerals; History renders each session as a card, not scrollback text. See
~/.claude/notes/maplestory-analyzer/final-spec-2026-08-17.md Section 3 for
the full spec. This module still only calls Session's public methods and
reads StatSnapshot/SessionSummary fields -- the capture/OCR/parser engine
(capture.py/ocr.py/parser.py/regions.py) is untouched by this rework, per
the hard UI/engine separation rule in that same doc.

Settings + i18n (2026-08-17, later same day): all UI-layer settings live in
one `Settings` struct (settings.py) instead of scattered instance attributes,
so a future persistence layer can load/save it wholesale. All user-facing
strings route through `self._t(key)` into i18n.py's translation table (English
+ Traditional Chinese, zh default) instead of literals inline here -- static
widgets built once register themselves in `self._i18n_labels` so a language
switch can walk the list and reconfigure every one of them, tabs get renamed
via CTkTabview.rename(), and History cards (built dynamically per session) are
simply torn down and rebuilt from `self._session_history` on switch.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog
from typing import Protocol

import customtkinter as ctk
from PIL import Image

from . import __version__
from .i18n import Lang, t
from .ocr import StatPanelOcr
from .parser import StatSnapshot, find_meso_candidate_verified, find_meso_in_region, find_stat_fields, parse_fields, parse_meso
from .rate import Session, SessionSummary
from .regions import QUICK_BAR_FRAC
from .region_selector import RegionSelector
from .settings import Settings, app_data_dir, load_settings, save_settings


def _open_log_file():
    """Return a writable stream for the windowed (console=False) build's debug
    logging: a real file next to the exe instead of devnull, so the per-tick
    OCR readout can be inspected to diagnose bad recognition."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = os.getcwd()
    path = os.path.join(base, "MsStatTractor.log")
    try:
        return open(path, "a", encoding="utf-8", errors="replace")
    except OSError:
        return open(os.devnull, "w", encoding="utf-8", errors="replace")


def _history_path() -> str:
    """Where the session history persists (next to the exe when frozen, else
    the cwd)."""
    return str(app_data_dir() / "MsStatTractor.history.json")

# The console's codepage (e.g. cp950 Traditional Chinese) can't represent
# every character OCR might misread out of the game's UI -- printing one
# used to raise UnicodeEncodeError and silently kill the tick loop (see
# _tick's try/except below for the other half of this fix). errors="replace"
# swaps unencodable characters for '?' instead of crashing.
if sys.stdout is None:
    # PyInstaller's windowed build (console=False) has no stdout/stderr at
    # all (both are None), which crashes not just .reconfigure() below but
    # every bare print() elsewhere in this module (tick-error/debug
    # logging) the moment they run. Swap in a no-op sink so those stay
    # harmless instead of taking down the app.
    #
    # encoding/errors are NOT optional here: open() defaults to the locale
    # codepage with errors='strict', i.e. cp950 on this zh-TW machine. The
    # PP-OCR recognition dictionary is largely *Simplified* Chinese, so a
    # garbage read (game window obscured, floating damage numbers over the
    # panel) routinely produces characters Big5/cp950 cannot encode -- and
    # printing one raised UnicodeEncodeError straight through the sink,
    # killing the tick loop. Same errors="replace" the console path below
    # has always had; the windowed build was the only place missing it.
    sys.stdout = sys.stderr = _open_log_file()
else:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if sys.stderr is not None:
        # Tk writes uncaught-callback tracebacks here, and a traceback can
        # carry the same unencodable OCR text in its repr.
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

if sys.platform == "win32":
    # Without declaring DPI awareness, Windows scales the whole rendered
    # window as a bitmap after the fact -- Tk still thinks the window is
    # e.g. 420 logical px, but the OS-scaled result doesn't match, and
    # widget content ends up clipped past the visible window edge (observed
    # live: value labels cut off mid-digit). Declaring per-monitor-v2
    # awareness lets Windows and Tk agree on actual pixel dimensions instead.
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        pass

# --- Scroll-ghost mitigation (2026-09-03, v1.9.0) ---
# Windows Tk redraws canvas-embedded widgets asynchronously after a scroll.
# With the library's default wheel step and fast wheel spin, several steps
# coalesce before a redraw lands, so the user sees old text ghosting over
# the new position while scrolling (CustomTkinter issue #1510; upstream
# says unsolvable). Flushing an idle redraw inside every wheel step keeps
# each painted frame complete: fast scrolling reads as stepwise but clean
# instead of smeared. CTkScrollableFrame.__init__ binds the *current* class
# method per instance, so this must be patched at module level before
# OverlayApp builds any scrollable frame. Dragging the scrollbar thumb
# still drives canvas.yview directly and is not covered here.
_ORIG_SCROLL_WHEEL = ctk.CTkScrollableFrame._mouse_wheel_all


def _scroll_wheel_redraw_synced(self, event):
    _ORIG_SCROLL_WHEEL(self, event)
    try:
        # Flush the embedded widgets' pending redraw before the next wheel
        # step moves the canvas again.
        self._parent_canvas.update_idletasks()
    except Exception:
        pass


ctk.CTkScrollableFrame._mouse_wheel_all = _scroll_wheel_redraw_synced

# Tick cadence: 2Hz running (500ms), restored 2026-09-02 after the 1Hz
# experiment (v1.8.4) made OCR reads visibly worse on the user's machine --
# values misread and the higher latency made the HUD look frozen. The CPU
# cost (Task Manager ~20-30%) is accepted in exchange for correct readings.
TARGET_MS = 500  # target full tick cycle -- 2Hz
# Stopped/idle tick cadence. While stopped nothing is being recorded -- the
# HUD only keeps the live OCR readouts fresh -- so a 1Hz cycle is plenty and
# halves the idle CPU/GPU load (see _do_tick's return).
TARGET_MS_IDLE = 1000
# Background locator cadence. Each pass runs full-frame *detection* OCR
# (~600ms+) in a daemon thread to re-find the stat panel fields and the
# meso counter, so the tick thread only ever does cheap recognition reads
# on cached boxes. Every 10 ticks = ~5s at 2Hz. Also what makes the HUD
# survive screen magnifiers (Megapipe): the detected positions track the
# rescaled layout every pass.
LOCATE_INTERVAL_TICKS = 10
# How often (in ticks) manual mode re-reads the meso counter via cheap
# recognition-only OCR on the marked meso region (~15ms) -- no detection, so
# no CPU spike like the locator's periodic detection pass used to cause.
# 10 ticks @ 2Hz = ~5s.
MESO_SCAN_INTERVAL_TICKS = 10
# Same cadence for the quick-slot potion counter: recognition-only OCR on
# the marked slot, throttled so a session reads ~every 5s (10 ticks @ 2Hz).
QUICK_SLOT_SCAN_INTERVAL_TICKS = 10

# The quickbar is 8 slots laid out as two rows of four (2026-09-03), each
# keyed by a keyboard key. Slot 1-4 are the top row (left→right), 5-8 the
# bottom row. Names are shown as-is -- they're the game's own key labels.
QUICK_SLOT_NAMES = ["Shift", "Ins", "Home", "PgUp", "Ctrl", "Del", "End", "PgDn"]
QUICK_SLOT_COUNT = 8

# OCR readings of the tiny key labels above each quickbar slot are fuzzy
# ("Shit" for Shift, "Pup" for PgUp, "Dell" for Del...). _match_quick_key()
# normalises them back to QUICK_SLOT_NAMES so the key labels can anchor the
# real slot geometry (see _quickbar_slots_from_boxes) instead of blindly
# dividing the whole crop into 8 equal cells -- the crop includes blank
# margins, so equal division landed counts in the WRONG slot (measured on
# samples/maple_story_ui_20260906_a/b.png: 181/1056 were read fine but
# assigned one slot left of the real one, 2026-09-06).
_QUICK_KEY_TEXT = {
    "shift": "Shift", "shit": "Shift", "shif": "Shift", "shft": "Shift", "sft": "Shift",
    "shirt": "Shift", "sh1ft": "Shift",
    "ins": "Ins", "lns": "Ins", "ims": "Ins", "insl": "Ins",
    "home": "Home", "hom": "Home", "hm": "Home", "hme": "Home", "hone": "Home",
    "pgup": "PgUp", "pgu": "PgUp", "pup": "PgUp", "pg": "PgUp",
    "ctrl": "Ctrl", "ctl": "Ctrl", "ctr": "Ctrl", "ctri": "Ctrl", "crl": "Ctrl",
    "del": "Del", "dell": "Del", "dei": "Del", "de1": "Del", "de": "Del", "dei": "Del",
    "end": "End", "endd": "End", "en": "End", "erd": "End",
    "pgdn": "PgDn", "pgd": "PgDn", "pdn": "PgDn", "pgn": "PgDn",
}
_QUICK_TOP_KEYS = QUICK_SLOT_NAMES[:4]  # top row order: Shift, Ins, Home, PgUp


def _match_quick_key(text: str) -> str | None:
    """Normalise an OCR'd quickbar key label to its standard name, or None."""
    t = text.strip().lower().replace(" ", "")
    if t in _QUICK_KEY_TEXT:
        return _QUICK_KEY_TEXT[t]
    if len(t) >= 4:
        for variant, name in _QUICK_KEY_TEXT.items():
            if t.startswith(variant) or variant.startswith(t):
                return name
    return None


_COUNT_TRAILING = ".,:;'`\"|"


def _quick_count_text(text: str) -> str | None:
    """Return the clean digit run when `text` is a quickbar count OCR blob.

    Slot counts are short pure-digit strings ('320') but the slot border
    can glue one junk char onto the last digit ('156.' measured on
    2026-09-07 real capture), which the strict _pure_digits filter used to
    drop -- so HP counts sitting low in their slot were never read.
    Accepts digits with optional thousands commas plus at most ONE trailing
    separator char. Rejects anything with letters, '%', or long text (key
    labels, bottom-toolbar readouts like 'S54%' must not become counts).
    """
    t = text.strip()
    if not t or len(t) > 12:
        return None
    if t[-1] in _COUNT_TRAILING:
        t = t[:-1]
    if not t or not all(ch in "0123456789," for ch in t):
        return None
    return t


def _quickbar_slots_from_boxes(
    boxes: list[tuple[int, int, int, int, str]], w: int, h: int,
) -> dict[int, int]:
    """Assign pure-digit OCR boxes to quickbar slots (1-8) using the key
    labels above each slot as geometry anchors.

    The quickbar row renders each slot's key label on top (Shift/Ins/...),
    so a detected key label tells us where that slot actually starts --
    dividing the crop into equal eighths is wrong because the crop contains
    blank margins and slot borders (measured 2026-09-06: equal division put
    slot-4/8 potion counts one slot left). Falls back to equal division when
    no key label is readable (tiny native-res text).
    """
    keys: list[tuple[str, int, int, int, int]] = []
    digs: list[tuple[str, int, int, int, int]] = []
    for x, y, bw, bh, text in boxes:
        k = _match_quick_key(text)
        if k:
            keys.append((k, x, y, bw, bh))
        elif _quick_count_text(text) is not None:
            digs.append((text, x, y, bw, bh))

    # Row geometry: key labels of the top row sit at the same y; the bottom
    # row's labels (and the slot content) sit one slot-height below.
    keys.sort(key=lambda kb: (kb[2], kb[1]))
    result: dict[int, int] = {}

    if not keys:
        # Fallback: equal division over the crop (original behaviour).
        for text, x, y, bw, bh in digs:
            val = parse_meso(text)
            if val is None:
                continue
            cx, cy = x + bw / 2.0, y + bh / 2.0
            col = int(cx / (w / 4.0))
            row = int(cy / (h / 2.0))
            if 0 <= col < 4 and 0 <= row < 2:
                result[row * 4 + col + 1] = val
        return result

    # Top-row key labels anchor the four column centres. The labels' NAMES
    # fix their logical column (Shift=0..PgUp=3), so the slot width comes
    # from the OUTERMOST readable labels divided by their column distance --
    # averaging neighbouring gaps breaks when a middle label (e.g. Home)
    # isn't read and leaves one oversized gap (measured 2026-09-06).
    top_y = keys[0][2]
    top_keys = [kb for kb in keys if kb[2] <= top_y + 12]
    top_keys.sort(key=lambda kb: kb[1])
    by_col: dict[int, float] = {}
    for k, x, _y, bw, _bh in top_keys:
        if k not in _QUICK_TOP_KEYS:
            continue
        col = _QUICK_TOP_KEYS.index(k)
        cx = x + bw / 2.0
        if col not in by_col:
            by_col[col] = cx
    if not by_col:
        return result
    cols_sorted = sorted(by_col)
    c0, cN = cols_sorted[0], cols_sorted[-1]
    x0c, xNc = by_col[c0], by_col[cN]
    if cN > c0:
        cw = (xNc - x0c) / (cN - c0)
    else:
        cw = max(10.0, w / 8.0)
    # Every column centre follows the same spacing, even the ones whose
    # label wasn't read.
    filled: dict[int, float] = {c: x0c + (c - c0) * cw for c in range(4)}
    # Bottom row sits one slot-height below the top labels. Height is the
    # slot width when no bottom label was read (slots are square-ish).
    bot_labels = [kb for kb in keys if kb[2] > top_y + 12]
    if bot_labels:
        bot_top = min(kb[2] for kb in bot_labels)
        row_h = max(20.0, bot_top - top_y)
    else:
        row_h = max(20.0, cw)

    for text, x, y, bw, bh in digs:
        cx, cy = x + bw / 2.0, y + bh / 2.0
        col = None
        for c in range(4):
            x0 = filled[c] - cw / 2.0
            x1 = filled[c] + cw / 2.0
            if x0 - 2 <= cx <= x1 + 2:
                col = c
                break
        if col is None:
            continue
        if cy < top_y + row_h * 0.9:
            row = 0
        elif cy < top_y + row_h * 2.0:
            row = 1
        else:
            continue  # below the quickbar (bottom toolbar counts etc.)
        slot = row * 4 + col + 1
        val = parse_meso(text)
        if val is not None:
            result[slot] = val
    return result

# Number of history sessions at which the History tab starts nudging the user
# to clean up old records (see _update_history_summary).
_HISTORY_CLEANUP_THRESHOLD = 50

# Live tab's Pause/Resume/Start + Restart button row -- see _apply_run_state.
BUTTON_HEIGHT = 28
STOPPED_BUTTON_WIDTH = 96  # Start alone, centered -- smaller than the two-button width

# Color tokens, matching the approved HTML design preview.
BG = "#0d1117"
SURFACE = "#161b22"
SURFACE_2 = "#1c2230"
INK = "#e6edf3"
INK_DIM = "#8b96a5"
INK_FAINT = "#5b6577"
ACCENT = "#5eead4"
ACCENT_INK = "#06322c"
HP_COLOR = "#ff6b6b"
MP_COLOR = "#5b9dff"
EXP_COLOR = "#ffc247"
OK_COLOR = "#3ddc84"
TRACK_BG = "#12291f"

# Chrome text (tabs, headers, buttons, switches, kv labels -- anything that
# can carry translated content) picks its font family from the active
# language via OverlayApp._font(); Segoe UI has no real Traditional Chinese
# glyphs of its own (falls back to a system CJK font Windows picks for you,
# inconsistent with the rest of the UI), so zh uses Microsoft JhengHei
# (Windows' standard Traditional Chinese UI font) instead.
_FONT_FAMILY: dict[Lang, str] = {"en": "Segoe UI", "zh": "Microsoft JhengHei"}

# Fixed English-only chrome that never carries translated text (the game's
# own on-screen abbreviations LV/HP/MP/EXP, and the +/- scale stepper) stays
# on a plain Segoe UI tuple -- no language switching needed for pure ASCII.
_FONT_LABEL = ("Segoe UI", 10, "bold")
_FONT_UI_BOLD = ("Segoe UI", 13, "bold")

# Pure-numeric value labels (HP/MP/EXP readouts, session EXP diffs, history
# card numbers) stay on Consolas regardless of language -- they never render
# CJK text, and Consolas' monospacing is what keeps tabular digits aligned.
_FONT_MONO = ("Consolas", 12)
_FONT_MONO_SM = ("Consolas", 10)
_FONT_MONO_BOLD = ("Consolas", 12, "bold")


class PanelSource(Protocol):
    def grab_fields(self) -> dict:
        ...

    def grab_full(self) -> Image.Image:
        ...


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def _fmt_loss(loss: int) -> str:
    return f"-{loss}" if loss > 0 else "0"


@dataclasses.dataclass
class _Baseline:
    """The pre-start snapshot the player confirmed (or hand-edited) in the
    start dialog: everything the new session's numbers are measured against.
    EXP mirrors the latest OCR snapshot; meso + quick-slot counts were
    read while the inventory was open (they can't be re-read after Start)."""
    exp_cur: int | None = None
    exp_pct: float | None = None
    level: int | None = None
    meso: int | None = None
    hp_potion: int | None = None
    mp_potion: int | None = None


def _history_net_total(s: SessionSummary) -> int | None:
    """History card's meso headline: drop meso + equipment sale proceeds -
    potion spend (2026-09-02). None when the session never read any meso
    endpoint (no inventory openings) and recorded no sale either."""
    meso_g = s.meso_gained
    sale_m = s.sale_meso or 0
    cost = s.potion_cost or 0
    if meso_g is not None:
        return meso_g + sale_m - cost
    if sale_m:
        return sale_m - cost
    return None


def _fmt_summary(s: SessionSummary, index: int) -> str:
    # Console/debug log only, not shown in the UI -- deliberately left in
    # plain English regardless of self._settings.language.
    diff = s.exp_diff
    diff_s = f"+{diff:,}" if diff is not None else "?"
    pct_diff = s.exp_pct_diff
    pct_s = f" (+{pct_diff:.2f}%)" if pct_diff is not None else ""
    start_s = f"{s.start_exp:,}" if s.start_exp is not None else "?"
    end_s = f"{s.end_exp:,}" if s.end_exp is not None else "?"
    dur_min = s.duration_s / 60
    if s.interval_minutes is not None and abs(dur_min - s.interval_minutes) > 0.05:
        dur_s = f"{dur_min:.1f}m of {s.interval_minutes:.0f}m, restarted early"
    else:
        dur_s = f"{dur_min:.1f}m"
    meso_s = f"{s.meso_gained:+,}" if s.meso_gained is not None else "?"
    if s.sale_meso:
        meso_s += f" (sale +{s.sale_meso:,})"
    return (
        f"#{index} ({dur_s}): "
        f"EXP {start_s} -> {end_s} ({diff_s}{pct_s})  "
        f"Meso {meso_s}"
    )


def _version_is_newer(a: str, b: str) -> bool:
    """Compare two version strings numerically: True when a is newer than b.

    Only the leading 'x.y.z' numeric triple counts; any suffix ('-beta',
    '-rc1', …) is ignored -- a pre-release tag of the SAME version is never
    \"newer\" than the release itself, and suffix strings would otherwise
    corrupt the parse (int('0-beta') raises, and the old lexicographic
    fallback then wrongly ranked '1.8.0-beta' above '1.8.0')."""
    def _triple(v: str) -> tuple[int, int, int] | None:
        m = re.match(r"(\d+)\.(\d+)\.(\d+)", v.strip())
        return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None

    pa, pb = _triple(a), _triple(b)
    if pa is None or pb is None:
        return False  # unparseable -- never claim an update we can't stand behind
    return pa > pb


class OverlayApp:
    def __init__(self, source: PanelSource):
        self._source = source
        self._ocr = StatPanelOcr()
        self._session = Session(require_calibration=False)  # HP/MP retired (2026-09-02): no max calibration needed; EXP self-corrects
        self._session_history: list[SessionSummary] = self._load_history()
        self._settings = load_settings()

        self._last: StatSnapshot = StatSnapshot(None, None, None, None, None, None, None)
        # Newest-first: History cards are inserted at index 0 rather than
        # appended, so this tracks the card widgets in display order (index 0
        # = topmost/newest) to pack each new one with before=.
        self._history_cards: list[ctk.CTkFrame] = []
        # Static widgets whose text is a plain translated string (no
        # per-tick data baked in) register themselves here as they're built,
        # so _apply_language() can walk this list and reconfigure every one
        # instead of _build_*_tab needing to be re-run from scratch.
        self._i18n_labels: list[tuple[ctk.CTkBaseClass, str, int, bool]] = []
        # Guards _do_tick's finalize-on-timeout check against the rename
        # dialog's nested event loop -- see _do_tick and _on_rename_clicked.
        self._modal_open = False
        # "running" / "paused" / "stopped" -- see _on_pause_button_clicked and
        # _finalize_and_maybe_stop. "stopped" reached via the timer is
        # implemented by pausing the already-running Session (its clock
        # freezes and record() no-ops, exactly what "stopped" needs) rather
        # than adding a third Session state; starting "stopped" here needs no
        # such call since nothing has fed this fresh Session a tick yet --
        # _do_tick simply doesn't call session.record() until Start is
        # clicked, so it can't begin calibrating or accumulating unasked.
        #
        # Starts stopped rather than tracking immediately on launch -- opening
        # the app (or the .exe) shouldn't silently start a session before the
        # user has actually arrived at the game and decided to track.
        self._run_state = "stopped"
        # Deferred finalize for equipment-sale revenue. A session that stops
        # is NOT committed to History immediately -- it stays "pending" so the
        # user can sell their accumulated drops and record the proceeds
        # against this session (see _on_record_sale_clicked). _sale_recorded
        # flips True once the user records a sale; the pending session is
        # committed when the next one starts (or on app close).
        self._session_pending = False
        self._sale_recorded = False
        # Set once the sale is recorded for the current session and cleared on
        # the next Start -- drives the 賣裝收益/淨收益 rows (only shown after
        # the sale is recorded, since net income is wrong before it, 2026-09-03).
        self._sale_done = False
        # Last capture failure message, so _do_tick can log state changes
        # instead of repeating the same line every 2s retry.
        self._last_capture_error: str | None = None
        self._last_client_size: tuple[int, int] | None = None
        # Background locator state (see _try_locate / _apply_locate). A
        # daemon thread periodically runs full-frame detection to find the
        # stat panel fields AND the meso counter, caching their positions
        # so the tick thread only does cheap recognition reads. This is what
        # keeps the HUD correct under screen magnifiers (Megapipe): the
        # detected positions track the magnified layout every pass.
        self._locate_ticks = 0
        self._locate_thread: threading.Thread | None = None
        self._locate_ocr: StatPanelOcr | None = None
        # Detected stat-field boxes as fractions of the client frame:
        # {'LV': (fx, fy, fw, fh), ...}. None until the locator has found
        # the panel (tick falls back to regions.FIELD_BOXES meanwhile).
        self._stat_boxes: dict[str, tuple[float, float, float, float]] | None = None
        # Detected meso counter box (fractions), refreshed every locate pass
        # so a dragged inventory window or a zoom change self-corrects.
        self._meso_box: tuple[float, float, float, float] | None = None
        # Manual-region capture source (see settings.manual_*): built lazily
        # when the user marks regions and toggles manual mode on, and used by
        # _active_source() in place of the auto GameWindowCapture.
        self._manual_source = None
        self._manual_calibrated = False
        self._meso_scan_ticks = 0
        # Update-check state (see _check_for_updates): the latest release tag
        # when a newer version is available, else None.
        self._update_available: str | None = None
        self._update_hint_label = None
        # Startup screen size, for detecting resolution changes that would
        # invalidate the manual region coordinates (see _check_screen_change).
        self._screen_size: tuple[int, int] | None = self._query_screen_size()
        self._screen_warned = False
        # Timed status-pill override after a manual detection pass (see
        # _run_manual_detection): the result text is shown until this timestamp.
        self._detect_result_until = 0.0
        self._detect_result_text = ""
        self._detect_result_ok = False
        # Compact 2x2 gameplay overlay (see _ensure_compact_win) -- shown while
        # a session is running so the full window can stay minimized.
        # Baseline of the CURRENT session (confirmed in the pre-start dialog,
        # 2026-09-02): the compact window shows 初始 = this, 變化 = Session.
        self._baseline: _Baseline = _Baseline()
        self._compact_win = None
        self._compact_labels: dict[str, ctk.CTkLabel] = {}
        # 初始 (start) value labels -- grey small line above the coloured
        # delta (2026-09-02: each cell shows 初始 + 變化).
        self._compact_initial: dict[str, ctk.CTkLabel] = {}
        self._compact_eta = None  # set inside _ensure_compact_win's add_cell
        self._compact_meso_sub = None  # meso cell's third line (net income)
        # Manual stat overrides (2026-08-27): values the player typed over an
        # OCR misread on the Dashboard (see _on_stat_edit). Folded into the
        # merged snapshot every tick (see _apply_manual_overrides) until
        # cleared by another edit or a 辨識 pass re-reads the game.
        self._manual_overrides: dict[str, int] = {}
        # Last meso counter value seen (any state). Lets the Dashboard's
        # 起始楓幣 row show the freshly-detected value right after a 辨識 pass,
        # before a session has started (see _render). Updated by the locator
        # and the manual meso scan regardless of run_state.
        self._last_meso: int | None = None
        # Same idea for the quick-slot potion counts (2026-09-03): the 辨識
        # pass reads them so the potion rows show a value before Start.
        self._last_hp_slot_count: int | None = None
        self._last_mp_slot_count: int | None = None

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        # 120% content via widget scaling ONLY. set_window_scaling is
        # deliberately NOT used: it rescales Tk's coordinate system, and during
        # a window drag the widgets repaint against the wrong geometry, which is
        # the "文字重疊" ghosting reported 2026-08-25. Widget scaling merely
        # multiplies font/padding sizes, which is safe. The window is sized for
        # the 120% widgets (400x520 * 1.2) directly, not via a scaling factor.
        self._settings.scale_pct = 120
        ctk.set_widget_scaling(1.2)

        self.root = ctk.CTk()
        self.root.title("MsStatTractor")
        self.root.attributes("-topmost", self._settings.topmost)
        self.root.configure(fg_color=BG)
        # Window size (2026-09-02): widened from 320 to 400. The 320 width was
        # set (2026-08-28) when the compare feature was moved off the Dashboard
        # and the whole UI was squeezed to 2/3 of the old 480; but at 120%
        # widget scaling the Settings tab's hint text then wrapped after ~12
        # CJK chars per line, reading as clipped ("UI 不夠寬", reported
        # 2026-09-02). Live tab is scrollable, so a taller fixed window is no
        # longer needed; the width now gives text room to breathe.
        self.root.geometry("400x660+40+40")
        # Fixed, non-resizable window.
        self.root.resizable(False, False)
        # Commit any pending session + persist the compact window position on
        # close, so a stopped-but-uncommitted session isn't silently lost.
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # segmented_button_font is deliberately set to the same chrome font
        # as the rest of the UI: without it, CTkTabview falls back to
        # customtkinter's default font (Roboto, which isn't installed on
        # Windows and renders the tab labels in a mismatched fallback).
        self._tabview = ctk.CTkTabview(
            self.root, fg_color=BG, segmented_button_fg_color=SURFACE,
            segmented_button_font=self._font(13, bold=True),
        )
        self._tabview.pack(fill="both", expand=True, padx=8, pady=8)
        # CTkTabview's tab name doubles as its segmented-button label and its
        # internal dict key -- there's no separate "id" to address a tab by,
        # so the translated string itself is the key. rename() (used by
        # _apply_language) swaps the key/label together and keeps the
        # frame/selection intact; this dict just tracks the current name per
        # logical tab so rename() always has both the old and new string.
        self._tab_names = {
            "live": t("tab_live", self._settings.language),
            "history": t("tab_history", self._settings.language),
            "compare": t("tab_compare", self._settings.language),
            "settings": t("tab_settings", self._settings.language),
        }
        for name in self._tab_names.values():
            self._tabview.add(name)

        self._build_live_tab(self._tabview.tab(self._tab_names["live"]))
        self._build_history_tab(self._tabview.tab(self._tab_names["history"]))
        self._rebuild_history_cards()
        self._build_compare_tab(self._tabview.tab(self._tab_names["compare"]))
        self._build_settings_tab(self._tabview.tab(self._tab_names["settings"]))
        self._tabview.set(self._tab_names["live"])  # CTkTabview defaults to the last-added tab otherwise
        self._apply_visibility()
        self._apply_detect_button_visibility()
        self._apply_run_state()

        # Rebuild the manual-region capture source from the persisted settings.
        # Without this, a relaunch with use_manual + a marked region saved
        # starts with _manual_source=None, so _active_source() falls back to the
        # auto GameWindowCapture -- which can't read the game under a screen
        # magnifier, leaving the session stuck "calibrating" forever (the
        # reported "按開始後一直顯示偵測中"). The first tick then runs the
        # one-shot manual detection inline (see _try_locate).
        self._rebuild_manual_source()
        self._update_history_summary()

        # Background GitHub update check -- non-blocking, shows a hint on the
        # dashboard when a newer release exists.
        self._check_for_updates()

        self._tick()

    # ---- i18n ------------------------------------------------------------

    def _t(self, key: str, **kwargs: object) -> str:
        return t(key, self._settings.language, **kwargs)

    def _localize_error(self, message: str) -> str:
        """Translate the known capture.py RuntimeError messages (game
        minimized / not found) shown via _set_status_error --
        these are routine, expected states, not exceptional ones, so they
        deserve a real translation rather than leaking capture.py's raw
        English text into a zh-language UI. Anything unrecognized (a real
        bug, not a known game-window state) passes through unchanged."""
        if message == "game window is minimized":
            return self._t("status_error_minimized")
        if message.startswith("No window found with title containing"):
            return self._t("status_error_not_found")
        return message

    def _font(self, size: int, bold: bool = False) -> tuple:
        """Chrome-text font at the given size, in the active language's font
        family (see _FONT_FAMILY). Use for any widget that renders translated
        text; pure-numeric value labels should use the module-level
        _FONT_MONO* constants instead (see their docstring)."""
        family = _FONT_FAMILY[self._settings.language]
        return (family, size, "bold") if bold else (family, size)

    def _scale_header_text(self) -> str:
        return self._t("settings_window_scale") + f" — {self._settings.scale_pct}%"

    def _interval_header_text(self) -> str:
        return self._t("settings_session_interval") + f" — {self._settings.window_min} {self._t('unit_min')}"

    def _i18n(self, widget: ctk.CTkBaseClass, key: str, size: int, bold: bool = True) -> ctk.CTkBaseClass:
        """Set a widget's text + font from a translation key and register it
        for re-translation on language switch. Use for any widget whose text
        is *only* the translated string (no per-tick value baked in) --
        widgets that mix in live data (timer, status pill, kv values) instead
        call self._t(...)/self._font(...) directly wherever they're
        re-rendered every tick."""
        widget.configure(text=self._t(key), font=self._font(size, bold))
        self._i18n_labels.append((widget, key, size, bold))
        return widget

    def _apply_language(self, lang: Lang) -> None:
        if lang == self._settings.language:
            return
        self._settings.language = lang
        self._persist_settings()

        for logical, key in (
            ("live", "tab_live"), ("history", "tab_history"),
            ("compare", "tab_compare"), ("settings", "tab_settings"),
        ):
            old_name = self._tab_names[logical]
            new_name = self._t(key)
            if new_name != old_name:
                self._tabview.rename(old_name, new_name)
                self._tab_names[logical] = new_name

        # Tab labels are translated text, so they follow the language font
        # like every other chrome label (see the construction-site comment
        # on _tabview for why this matters).
        self._tabview.configure(segmented_button_font=self._font(13, bold=True))

        for widget, key, size, bold in self._i18n_labels:
            widget.configure(text=self._t(key), font=self._font(size, bold))

        self._status_pill.configure(font=self._font(9, bold=True))
        self._timer_label.configure(font=self._font(10, bold=True))
        self._scale_header_label.configure(text=self._scale_header_text(), font=self._font(11, bold=True))
        self._interval_header_label.configure(text=self._interval_header_text(), font=self._font(11, bold=True))
        # _pause_button's text depends on _run_state, not just language, so it
        # isn't in _i18n_labels -- _apply_run_state() re-derives it from
        # scratch, which also happens to pick up the new language/font.
        self._apply_run_state()

        # History cards mix translated chrome (SESSION #N, HP/MP LOSS) with
        # per-session data and aren't worth tracking widget-by-widget --
        # tearing down and rebuilding from the data we already keep is
        # simpler and this only happens on an explicit language switch, and
        # _append_history_card already picks up the new language/font.
        self._rebuild_history_cards()
        self._refresh_manual_status()
        # Compare tab's dropdown labels carry translated chrome (紀錄 #n) and
        # the placeholder -- rebuild them so they match the new language.
        self._refresh_compare_tab()
        # Quick-slot pickers' 關閉 label is translated too.
        if hasattr(self, "_quick_menu_hp_quick_slot_index"):
            values = [self._t("settings_quick_slot_off")] + list(QUICK_SLOT_NAMES)
            for attr in ("hp_quick_slot_index", "mp_quick_slot_index"):
                menu = getattr(self, "_quick_menu_" + attr)
                menu.configure(values=values)
                idx = getattr(self._settings, attr)
                menu.set(self._t("settings_quick_slot_off") if not idx else QUICK_SLOT_NAMES[idx - 1])
            self._refresh_quick_bar_status()

        self._render(self._last)  # refreshes status pill / timer text immediately

    # ---- tab construction ------------------------------------------------

    def _build_live_tab(self, parent) -> None:
        # Scrollable so the window can stay compact at any scale while every
        # block stays reachable -- a scrollbar appears on the right when the
        # content is taller than the tab (per user request 2026-08-24: at
        # 120% scale the Start button used to sit below the fold with no
        # way to reach it).
        scroll = ctk.CTkScrollableFrame(parent, fg_color=BG, label_text="")
        scroll.pack(fill="both", expand=True)
        parent = scroll
        parent.grid_columnconfigure(0, weight=1)

        # Status + session timer share one row, both shrunk down (smaller
        # font/padding than the rest of the chrome) so a longer localized
        # status string (the capture-error states from _localize_error run
        # much longer than "Tracking"/"追蹤中") still leaves room for the
        # timer instead of pushing the Restart button out of the window.
        # wraplength caps the status pill's own width so it wraps to a
        # second line rather than growing sideways into the timer's column.
        strip = ctk.CTkFrame(parent, fg_color="transparent")
        strip.grid(row=0, column=0, sticky="ew", padx=2, pady=(2, 3))
        strip.grid_columnconfigure(0, weight=1)
        strip.grid_columnconfigure(1, weight=0)
        strip.grid_columnconfigure(2, weight=0)

        self._status_pill = ctk.CTkLabel(
            strip, text=self._t("status_tracking"), corner_radius=999, fg_color=TRACK_BG,
            text_color=OK_COLOR, font=self._font(9, bold=True), padx=8, pady=2,
            anchor="w", justify="left", wraplength=110,
        )
        self._status_pill.grid(row=0, column=0, sticky="w")

        # Mixes translated chrome ("left"/"剩餘") with the countdown digits,
        # so it needs the language-aware font (self._font), not the fixed
        # digits-only _FONT_MONO_BOLD -- unlike the pure-numeric value labels.
        self._timer_label = ctk.CTkLabel(
            strip, text="--:--", corner_radius=999, fg_color=SURFACE_2,
            text_color=INK, font=self._font(10, bold=True), padx=8, pady=2,
        )
        self._timer_label.grid(row=0, column=1, sticky="e")

        # "辨識" button: re-runs the manual-mode detection of the marked stat
        # region on demand (see _run_manual_detection). Only meaningful -- and
        # therefore only shown -- when manual mode is on.
        self._detect_button = ctk.CTkButton(
            strip, text="", command=self._on_detect_clicked,
            fg_color=SURFACE_2, hover_color=TRACK_BG, text_color=INK,
            corner_radius=999, height=22, width=48,
        )
        self._i18n(self._detect_button, "detect_button", size=10, bold=True)
        self._detect_button.grid(row=0, column=2, sticky="e", padx=(6, 0))

        # Stat grid: label | mini bar | tabular value, aligned via one grid
        # rather than independently left-justified label:value text. Labels
        # (LV/HP/MP/EXP) are the game's own on-screen abbreviations -- see
        # i18n.py's docstring for why these are not translated.
        stats = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=12)
        stats.grid(row=1, column=0, sticky="ew", padx=2, pady=(0, 3))
        stats.grid_columnconfigure(0, weight=0)
        stats.grid_columnconfigure(1, weight=1)
        stats.grid_columnconfigure(2, weight=0)

        self._stat_rows: dict[str, tuple] = {}
        self._value_labels: dict[str, ctk.CTkLabel] = {}
        self._bars: dict[str, ctk.CTkProgressBar] = {}

        def add_stat_row(row: int, key: str, label_text: str, color: str, with_bar: bool) -> None:
            lbl = ctk.CTkLabel(stats, text=label_text, font=_FONT_LABEL, text_color=color, anchor="w")
            lbl.grid(row=row, column=0, sticky="w", padx=(12, 6), pady=0)
            value = ctk.CTkLabel(stats, text="--", font=_FONT_MONO, text_color=INK, anchor="e")
            value.grid(row=row, column=2, sticky="e", padx=(6, 12), pady=0)
            # Editable on click (2026-08-27): the player can correct an OCR
            # misread by hand -- see _on_stat_edit.
            value.bind("<Button-1>", lambda _e, k=key: self._on_stat_edit(k))
            value.configure(cursor="hand2")
            bar = None
            if with_bar:
                bar = ctk.CTkProgressBar(stats, height=5, progress_color=color, fg_color=SURFACE_2)
                bar.set(0)
                bar.grid(row=row, column=1, sticky="ew", padx=6, pady=0)
                self._bars[key] = bar
            self._stat_rows[key] = (lbl, bar, value)
            self._value_labels[key] = value

        # HP/MP rows removed (2026-09-02 user request): potion counts replaced
        # them, so the panel shows only LV and EXP. HP/MP are no longer OCR'd
        # on the tick either (see _do_tick), saving ~half the recognition cost.
        add_stat_row(0, "level", "LV", EXP_COLOR, with_bar=False)
        add_stat_row(1, "exp", "EXP", EXP_COLOR, with_bar=True)

        # Session info: label | tabular value, same alignment discipline.
        # Split into two cards with clear spacing between blocks (per user
        # request 2026-08-24): EXP block (start/diff/ETA/projection) and a
        # losses+meso block (HP/MP loss, start/current meso).
        self._kv_rows: dict[str, tuple] = {}

        def add_kv_row(card, row: int, key: str, i18n_key: str) -> None:
            lbl = ctk.CTkLabel(card, text_color=INK_DIM, anchor="w")
            self._i18n(lbl, i18n_key, size=11, bold=False)
            lbl.grid(row=row, column=0, sticky="w", padx=(12, 6), pady=0)
            value = ctk.CTkLabel(card, text="--", font=_FONT_MONO, text_color=INK, anchor="e")
            value.grid(row=row, column=1, sticky="e", padx=(6, 12), pady=0)
            self._kv_rows[key] = (lbl, value)
            self._value_labels[key] = value

        exp_card = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=12)
        exp_card.grid(row=2, column=0, sticky="ew", padx=2, pady=(0, 6))
        exp_card.grid_columnconfigure(0, weight=1)
        exp_card.grid_columnconfigure(1, weight=0)
        add_kv_row(exp_card, 0, "startexp", "kv_start_exp")
        add_kv_row(exp_card, 1, "expdiff", "kv_exp_diff")
        add_kv_row(exp_card, 2, "eta", "kv_eta")
        add_kv_row(exp_card, 3, "projexp", "kv_proj_exp")
        # Map name (editable): click the value to type it by hand. Uses a
        # regular (non-mono) font because map names are CJK text.
        map_lbl = ctk.CTkLabel(exp_card, text_color=INK_DIM, anchor="w")
        self._i18n(map_lbl, "kv_map", size=11, bold=False)
        map_lbl.grid(row=4, column=0, sticky="w", padx=(12, 6), pady=0)
        self._map_value_label = ctk.CTkLabel(
            exp_card, text="--", font=self._font(12), text_color=INK, anchor="e", cursor="hand2",
        )
        self._map_value_label.grid(row=4, column=1, sticky="e", padx=(6, 12), pady=0)
        self._map_value_label.bind("<Button-1>", lambda _e: self._on_map_edit())

        # Potion card (third block): HP/MP potion slot (which quickbar key) +
        # the potion count read from each slot. Always visible with all four
        # rows (2026-09-02): positions default to '--' and are picked right
        # here on the dashboard (click the value) or on the Settings tab;
        # counts default to '--' and fill in after 辨識.
        potion_card = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=12)
        potion_card.grid(row=3, column=0, sticky="ew", padx=2, pady=(0, 6))
        potion_card.grid_columnconfigure(0, weight=1)
        potion_card.grid_columnconfigure(1, weight=0)
        self._potion_card = potion_card
        add_kv_row(potion_card, 0, "hpslot", "kv_hp_potion_slot")
        add_kv_row(potion_card, 1, "mpslot", "kv_mp_potion_slot")
        add_kv_row(potion_card, 2, "hpcount", "kv_hp_potion_count")
        add_kv_row(potion_card, 3, "mpcount", "kv_mp_potion_count")
        # Click behaviours (2026-09-02): position values open a quickbar-key
        # picker; counts can be hand-corrected for an OCR misread.
        for attr, key in (("hp_quick_slot_index", "hpslot"),
                          ("mp_quick_slot_index", "mpslot")):
            val = self._kv_rows[key][1]
            val.bind("<Button-1>", lambda e, a=attr: self._on_dashboard_slot_pick(e, a))
            val.configure(cursor="hand2")
        for key in ("hpcount", "mpcount"):
            val = self._kv_rows[key][1]
            val.bind("<Button-1>", lambda _e, k=key: self._on_dashboard_count_edit(k))
            val.configure(cursor="hand2")

        # Meso card (fourth block, 2026-09-03): start/current meso, potion
        # cost, and (only after 記錄賣裝) sale revenue + net income.
        meso_card = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=12)
        meso_card.grid(row=4, column=0, sticky="ew", padx=2, pady=(0, 6))
        meso_card.grid_columnconfigure(0, weight=1)
        meso_card.grid_columnconfigure(1, weight=0)
        add_kv_row(meso_card, 0, "mesostart", "kv_meso_start")
        add_kv_row(meso_card, 1, "mesocurrent", "kv_meso_current")
        add_kv_row(meso_card, 2, "potioncost", "kv_potion_cost")
        add_kv_row(meso_card, 3, "mesosale", "kv_meso_sale")
        add_kv_row(meso_card, 4, "netmeso", "kv_net_meso")
        # Faint hint under the meso rows: the counter only exists while the
        # inventory is open.
        self._meso_hint_label = ctk.CTkLabel(
            meso_card, text="", anchor="w", justify="left",
            text_color=INK_FAINT, font=self._font(9, bold=False),
        )
        self._i18n(self._meso_hint_label, "meso_hint", size=9, bold=False)
        self._meso_hint_label.grid(row=5, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 4))

        # 記錄賣裝 button + hint: shown only while a session is stopped-but-
        # pending (see _apply_run_state). Records the meso from selling the
        # session's equipment drops against this session.
        self._record_sale_button = ctk.CTkButton(
            meso_card, command=self._on_record_sale_clicked,
            fg_color=ACCENT, text_color=ACCENT_INK, hover_color="#7ff2e0",
            corner_radius=9, height=28,
        )
        self._i18n(self._record_sale_button, "record_sale_button", size=12, bold=True)
        self._record_sale_button.grid(row=6, column=0, columnspan=2, sticky="ew", padx=12, pady=(4, 0))
        self._record_sale_button.grid_remove()

        self._record_sale_hint = ctk.CTkLabel(
            meso_card, text="", anchor="w", justify="left",
            text_color=INK_FAINT, font=self._font(9, bold=False),
        )
        self._i18n(self._record_sale_hint, "record_sale_hint", size=9, bold=False)
        self._record_sale_hint.grid(row=7, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 8))
        self._record_sale_hint.grid_remove()

        # Three buttons share one row: the left cycles Pause/Resume/Start
        # (see _on_pause_button_clicked), the middle is Stop -- only shown
        # while paused, and ends the session WITHOUT starting a new one (the
        # one thing the right button, unconditional Restart, can't do), the
        # right is Restart. Stop and Restart are hidden while stopped.
        button_row = ctk.CTkFrame(parent, fg_color="transparent")
        button_row.grid(row=5, column=0, sticky="ew", padx=2, pady=(0, 2))
        button_row.grid_columnconfigure(0, weight=1)
        button_row.grid_columnconfigure(1, weight=1)
        button_row.grid_columnconfigure(2, weight=1)

        self._pause_button = ctk.CTkButton(
            button_row, command=self._on_pause_button_clicked,
            fg_color=SURFACE_2, hover_color=TRACK_BG, text_color=INK,
            corner_radius=9, height=BUTTON_HEIGHT,
        )
        self._pause_button.grid(row=0, column=0, sticky="ew", padx=(0, 3))

        self._stop_button = ctk.CTkButton(
            button_row, command=self._on_stop_clicked,
            fg_color=SURFACE_2, hover_color=TRACK_BG, text_color=HP_COLOR,
            corner_radius=9, height=BUTTON_HEIGHT,
        )
        self._i18n(self._stop_button, "stop_button", size=12, bold=True)
        self._stop_button.grid(row=0, column=1, sticky="ew", padx=(0, 3))

        self._restart_button = ctk.CTkButton(
            button_row, command=self._on_restart_clicked,
            fg_color=ACCENT, text_color=ACCENT_INK, hover_color="#7ff2e0",
            corner_radius=9, height=BUTTON_HEIGHT,
        )
        self._i18n(self._restart_button, "restart_button", size=13, bold=True)
        self._restart_button.grid(row=0, column=2, sticky="ew", padx=(3, 0))

        # Update-available hint (hidden until _check_for_updates finds a newer
        # release) -- sits under the button row, out of the way.
        self._update_hint_label = ctk.CTkLabel(
            parent, text="", corner_radius=8, fg_color=SURFACE_2,
            text_color=EXP_COLOR, font=self._font(10, bold=True),
            # wraplength is widget-scaled (x1.2) before reaching Tk, so the
            # effective wrap point is 240*1.2 = 288px -- just inside the
            # ~292px card content width at the 400px window. See the comment
            # on _wraplength users for the scaling trap.
            anchor="w", justify="left", wraplength=240,
        )
        self._update_hint_label.grid(row=6, column=0, sticky="ew", padx=2, pady=(4, 2))
        self._update_hint_label.grid_remove()

        # Compatibility-capture warning (shown when WGC isn't doing the work --
        # see _render). Sits under the update hint, out of the way.
        self._compat_hint_label = ctk.CTkLabel(
            parent, text="", corner_radius=8, fg_color=SURFACE_2,
            text_color=EXP_COLOR, font=self._font(9, bold=True),
            anchor="w", justify="left", wraplength=240,
        )
        self._compat_hint_label.grid(row=7, column=0, sticky="ew", padx=2, pady=(0, 2))
        self._compat_hint_label.grid_remove()

    def _build_history_tab(self, parent) -> None:
        # Summary strip: total sessions / today's EXP / current-map avg rate
        # (see _update_history_summary), plus a cleanup nudge when large.
        self._history_summary_label = ctk.CTkLabel(
            parent, text="", anchor="w", justify="left",
            text_color=INK_DIM, font=self._font(10, bold=False),
        )
        self._history_summary_label.pack(fill="x", padx=12, pady=(4, 0))

        self._clear_history_button = ctk.CTkButton(
            parent, command=self._on_clear_history_clicked,
            fg_color=SURFACE_2, hover_color=TRACK_BG, text_color=HP_COLOR,
            corner_radius=9, height=28,
        )
        self._i18n(self._clear_history_button, "history_clear_button", size=11, bold=True)
        self._clear_history_button.pack(fill="x", padx=2, pady=(2, 4))

        self._history_frame = ctk.CTkScrollableFrame(parent, fg_color=BG, label_text="")
        self._history_frame.pack(fill="both", expand=True, padx=2, pady=(0, 2))
        self._history_empty_label = ctk.CTkLabel(
            self._history_frame, text_color=INK_FAINT,
        )
        self._i18n(self._history_empty_label, "history_empty", size=13, bold=False)
        self._history_empty_label.pack(pady=24)

    def _build_compare_tab(self, parent) -> None:
        """Compare tab (2026-08-28): pick two History records via dropdowns
        and diff their per-minute metrics side by side. The old baseline
        comparison on the Dashboard (and the History-card 比較 button) was
        removed in favour of this dedicated tab."""
        # Hint shown while there are no sessions to compare.
        self._compare_empty_label = ctk.CTkLabel(
            parent, text="", anchor="w", justify="left",
            text_color=INK_FAINT, font=self._font(11, bold=False),
        )
        self._i18n(self._compare_empty_label, "compare_no_sessions", size=11, bold=False)
        self._compare_empty_label.pack(fill="x", padx=12, pady=(12, 0))

        # Two pickers stacked vertically (per user request 2026-08-28: side
        # by side didn't fit the 320px-wide window) -- each row is a label
        # plus its dropdown.
        for key, attr in (("compare_pick_a", "_compare_menu_a"), ("compare_pick_b", "_compare_menu_b")):
            row = ctk.CTkFrame(parent, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=(0, 6))
            ctk.CTkLabel(row, text=self._t(key), font=self._font(11, bold=True),
                         text_color=INK_DIM, anchor="w").pack(side="left", padx=(0, 8))
            menu = ctk.CTkOptionMenu(
                row, values=[self._t("compare_placeholder")],
                command=lambda _v, a=attr: self._on_compare_select(),
                fg_color=SURFACE_2, button_color=SURFACE_2, button_hover_color=TRACK_BG,
                text_color=INK, font=self._font(10, bold=False),
                dropdown_fg_color=SURFACE_2, dropdown_hover_color=TRACK_BG,
                dropdown_text_color=INK, dropdown_font=self._font(10, bold=False),
            )
            menu.set(self._t("compare_placeholder"))
            menu.pack(side="left", fill="x", expand=True)
            setattr(self, attr, menu)
        self._compare_sel_a: SessionSummary | None = None
        self._compare_sel_b: SessionSummary | None = None

        # Result table: 項目 | 紀錄 A | 紀錄 B | A 比 B
        self._compare_table = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=12)
        self._compare_table.pack(fill="x", padx=12, pady=(0, 6))
        for c in range(4):
            self._compare_table.grid_columnconfigure(c, weight=1 if c == 0 else 0)
        self._compare_table.grid_columnconfigure(1, weight=1)
        self._compare_table.grid_columnconfigure(2, weight=1)
        self._compare_table.grid_columnconfigure(3, weight=1)

        self._compare_rows: dict[str, tuple] = {}

        def add_row(row: int, key: str, i18n_key: str, monospace: bool = True) -> None:
            lbl = ctk.CTkLabel(self._compare_table, text=self._t(i18n_key), font=self._font(10, bold=True),
                               text_color=INK_DIM, anchor="w")
            lbl.grid(row=row, column=0, sticky="w", padx=(12, 6), pady=2)
            a = ctk.CTkLabel(self._compare_table, text="—", font=_FONT_MONO_SM if monospace else self._font(10),
                             text_color=INK, anchor="e")
            a.grid(row=row, column=1, sticky="e", padx=(6, 6), pady=2)
            b = ctk.CTkLabel(self._compare_table, text="—", font=_FONT_MONO_SM if monospace else self._font(10),
                             text_color=INK, anchor="e")
            b.grid(row=row, column=2, sticky="e", padx=(6, 6), pady=2)
            d = ctk.CTkLabel(self._compare_table, text="—", font=_FONT_MONO_SM,
                             text_color=INK_DIM, anchor="e")
            d.grid(row=row, column=3, sticky="e", padx=(6, 12), pady=2)
            self._compare_rows[key] = (a, b, d)

        add_row(0, "duration", "compare_duration", monospace=False)
        add_row(1, "map", "compare_map", monospace=False)
        add_row(2, "exp", "compare_exp_total")
        # HP/MP loss/min rows removed (2026-09-02) -- HP/MP aren't tracked.
        add_row(3, "expmin", "compare_exp_per_min")
        add_row(4, "mesomin", "compare_meso_per_min")
        add_row(5, "wild", "compare_wild_meso")
        add_row(6, "equip", "compare_equip_meso")

        self._refresh_compare_tab()

    def _compare_label_for(self, summary: "SessionSummary") -> str:
        """Dropdown label for one History record: name (or 紀錄 #n) plus its
        start time so two records are distinguishable at a glance."""
        for i, s in enumerate(self._session_history, start=1):
            if s is summary:
                name = s.name or self._t("history_session", n=i)
                break
        else:
            name = self._t("history_session", n=0)
        ts = time.strftime("%m-%d %H:%M", time.localtime(summary.start_time))
        return f"{name}（{ts}）"

    def _summary_from_label(self, label: str) -> SessionSummary | None:
        for s in self._session_history:
            if self._compare_label_for(s) == label:
                return s
        return None

    def _refresh_compare_tab(self) -> None:
        """Re-sync the Compare tab's dropdowns with the current session
        history (called after any record is added/removed/cleared)."""
        if not hasattr(self, "_compare_menu_a"):
            return
        values = [self._compare_label_for(s) for s in self._session_history]
        placeholder = self._t("compare_placeholder")
        if not values:
            self._compare_empty_label.pack(fill="x", padx=12, pady=(12, 0))
            self._compare_menu_a.configure(values=[placeholder])
            self._compare_menu_b.configure(values=[placeholder])
            self._compare_menu_a.set(placeholder)
            self._compare_menu_b.set(placeholder)
            self._compare_sel_a = None
            self._compare_sel_b = None
            self._on_compare_select()
            return
        self._compare_empty_label.pack_forget()
        # Keep the previous selection if its record still exists.
        def keep(old: SessionSummary | None, menu) -> SessionSummary | None:
            if old is not None and old in self._session_history:
                menu.configure(values=values)
                menu.set(self._compare_label_for(old))
                return old
            menu.configure(values=values)
            menu.set(placeholder)
            return None
        self._compare_sel_a = keep(self._compare_sel_a, self._compare_menu_a)
        self._compare_sel_b = keep(self._compare_sel_b, self._compare_menu_b)
        self._on_compare_select()

    def _on_compare_select(self) -> None:
        """Dropdown changed: resolve both selections and render the diff
        table (or clear it when either side is unset)."""
        if not hasattr(self, "_compare_rows"):
            return
        a_label = self._compare_menu_a.get()
        b_label = self._compare_menu_b.get()
        placeholder = self._t("compare_placeholder")
        a = None if a_label == placeholder else self._summary_from_label(a_label)
        b = None if b_label == placeholder else self._summary_from_label(b_label)
        self._compare_sel_a = a
        self._compare_sel_b = b

        def set_row(key: str, a_val: str, b_val: str, diff: str | None = None,
                    color: str = INK_DIM) -> None:
            a_lbl, b_lbl, d_lbl = self._compare_rows[key]
            a_lbl.configure(text=a_val, text_color=INK)
            b_lbl.configure(text=b_val, text_color=INK)
            d_lbl.configure(text=diff if diff is not None else "—", text_color=color)

        for _a, _b, _d in self._compare_rows.values():
            _a.configure(text="—", text_color=INK_DIM)
            _b.configure(text="—", text_color=INK_DIM)
            _d.configure(text="—", text_color=INK_DIM)
        if a is None or b is None:
            return

        unit = self._t("unit_min_short")

        def per_min(value: int | None, dur: float) -> float | None:
            if value is None or dur <= 0:
                return None
            return value / dur * 60

        def fmt_num(v) -> str:
            return f"{v:,.0f}" if v is not None else "—"

        def fmt_pct(a_v, b_v, invert: bool = False) -> tuple[str | None, str]:
            """Signed % of A vs B. invert=True means lower is better (loss
            metrics): the sign flips so positive always = A is better."""
            if a_v is None or b_v is None or b_v == 0:
                return None, INK_DIM
            raw = (b_v - a_v) if invert else (a_v - b_v)
            pct = raw / b_v * 100
            color = OK_COLOR if pct > 0 else (HP_COLOR if pct < 0 else INK_DIM)
            return f"{pct:+.0f}%", color

        a_dur, b_dur = a.duration_s, b.duration_s
        set_row("duration", f"{a_dur/60:.1f}{unit}", f"{b_dur/60:.1f}{unit}")
        set_row("map", a.map_name or "—", b.map_name or "—")

        a_exp, b_exp = a.exp_diff, b.exp_diff
        pct, color = fmt_pct(a_exp, b_exp)
        set_row("exp", fmt_num(a_exp), fmt_num(b_exp), pct, color)

        a_epm, b_epm = a.exp_per_min, b.exp_per_min
        pct, color = fmt_pct(a_epm, b_epm)
        set_row("expmin", fmt_num(a_epm), fmt_num(b_epm), pct, color)

        def total_meso(s: SessionSummary) -> int | None:
            if s.meso_gained is not None:
                return s.meso_gained + (s.sale_meso or 0)
            if s.sale_meso:
                return s.sale_meso
            return None

        a_meso, b_meso = per_min(total_meso(a), a_dur), per_min(total_meso(b), b_dur)
        pct, color = fmt_pct(a_meso, b_meso)
        set_row("mesomin", fmt_num(a_meso), fmt_num(b_meso), pct, color)

        pct, color = fmt_pct(a.meso_gained, b.meso_gained)
        set_row("wild", fmt_num(a.meso_gained), fmt_num(b.meso_gained), pct, color)

        a_sale, b_sale = (a.sale_meso or 0) or None, (b.sale_meso or 0) or None
        pct, color = fmt_pct(a_sale, b_sale)
        set_row("equip", fmt_num(a_sale), fmt_num(b_sale), pct, color)

    def _build_settings_tab(self, parent) -> None:
        # Scrollable: at some WINDOW SCALE values the settings content is
        # taller than the window, and a plain .pack() into the tab would
        # just clip the overflow with no way to reach it -- a scrollbar
        # keeps every option reachable regardless of scale/window size.
        scroll = ctk.CTkScrollableFrame(parent, fg_color=BG, label_text="")
        scroll.pack(fill="both", expand=True)

        window_card = ctk.CTkFrame(scroll, fg_color=SURFACE, corner_radius=12)
        window_card.pack(fill="x", padx=2, pady=(2, 3))

        # Value lives in the section header, not squeezed into the control
        # row -- at narrow window widths (esp. with the scrollbar eating
        # horizontal space) a fixed-width label at the end of a packed row
        # was getting clipped to invisible. The header always has room.
        self._scale_header_label = ctk.CTkLabel(
            window_card, text=self._scale_header_text(),
            anchor="w", text_color=INK_DIM, font=self._font(11, bold=True),
        )
        self._scale_header_label.pack(fill="x", padx=12, pady=(5, 3))
        # Scale is locked at 120% (no +/- stepper) -- see the resize fix in
        # __init__; the header label above states the fixed value.

        self._topmost_var = tk.BooleanVar(value=self._settings.topmost)
        self._i18n(ctk.CTkSwitch(
            window_card, variable=self._topmost_var, text_color=INK,
            progress_color=ACCENT, button_color=INK_DIM, button_hover_color=ACCENT,
            command=self._on_topmost_changed,
        ), "settings_always_on_top", size=11, bold=False).pack(fill="x", padx=12, pady=(0, 3))

        lang_row = ctk.CTkFrame(window_card, fg_color="transparent")
        lang_row.pack(fill="x", padx=12, pady=(0, 4))
        self._i18n(
            ctk.CTkLabel(lang_row, anchor="w", text_color=INK_DIM), "settings_language", size=11, bold=True
        ).pack(side="left")
        self._lang_button = ctk.CTkSegmentedButton(
            lang_row, values=["中文", "EN"], command=self._on_language_button_changed,
            selected_color=ACCENT, selected_hover_color="#7ff2e0", text_color=INK,
        )
        self._lang_button.set("中文" if self._settings.language == "zh" else "EN")
        self._lang_button.pack(side="right")

        card = ctk.CTkFrame(scroll, fg_color=SURFACE, corner_radius=12)
        card.pack(fill="x", padx=2, pady=(0, 0))

        self._interval_header_label = ctk.CTkLabel(
            card, text=self._interval_header_text(),
            anchor="w", text_color=INK_DIM, font=self._font(11, bold=True),
        )
        self._interval_header_label.pack(fill="x", padx=12, pady=(5, 0))
        slider_row = ctk.CTkFrame(card, fg_color="transparent")
        slider_row.pack(fill="x", padx=12, pady=(0, 2))
        slider = ctk.CTkSlider(
            slider_row, from_=1, to=60, number_of_steps=59, command=self._on_interval_changed,
            progress_color=ACCENT, button_color=ACCENT, button_hover_color="#7ff2e0",
        )
        slider.set(self._settings.window_min)
        slider.pack(fill="x", expand=True)

        self._i18n(
            ctk.CTkLabel(card, anchor="w", text_color=INK_DIM), "settings_display", size=11, bold=True
        ).pack(fill="x", padx=12, pady=(2, 0))

        self._switch_vars: dict[str, tk.BooleanVar] = {}
        for key, i18n_key, attr in (
            # HP/MP display switches removed (2026-09-02) -- those rows are
            # gone from the dashboard entirely.
            ("exp", "settings_show_exp", "show_exp"),
            ("exp_pct", "settings_show_exp_pct", "show_exp_pct"),
            ("eta", "settings_show_eta", "show_eta"),
            ("proj_exp", "settings_show_proj_exp", "show_proj_exp"),
        ):
            var = tk.BooleanVar(value=getattr(self._settings, attr))
            self._switch_vars[key] = var
            self._i18n(ctk.CTkSwitch(
                card, variable=var, text_color=INK,
                progress_color=ACCENT, button_color=INK_DIM, button_hover_color=ACCENT,
                command=lambda k=key, a=attr, v=var: self._on_switch_changed(k, a, v),
            ), i18n_key, size=11, bold=False).pack(fill="x", padx=12, pady=0)

        # SESSION: behaviour switches, not display toggles -- neither one
        # hides/shows a widget, so they bypass _on_switch_changed/_apply_visibility
        # entirely (see _on_auto_stop_changed/_on_save_on_restart_changed).
        self._i18n(
            ctk.CTkLabel(card, anchor="w", text_color=INK_DIM), "settings_session", size=11, bold=True
        ).pack(fill="x", padx=12, pady=(3, 0))

        self._auto_stop_var = tk.BooleanVar(value=self._settings.auto_stop)
        self._i18n(ctk.CTkSwitch(
            card, variable=self._auto_stop_var, text_color=INK,
            progress_color=ACCENT, button_color=INK_DIM, button_hover_color=ACCENT,
            command=self._on_auto_stop_changed,
        ), "settings_auto_stop", size=11, bold=False).pack(fill="x", padx=12, pady=0)

        self._save_on_restart_var = tk.BooleanVar(value=self._settings.save_on_restart)
        self._i18n(ctk.CTkSwitch(
            card, variable=self._save_on_restart_var, text_color=INK,
            progress_color=ACCENT, button_color=INK_DIM, button_hover_color=ACCENT,
            command=self._on_save_on_restart_changed,
        ), "settings_save_on_restart", size=11, bold=False).pack(fill="x", padx=12, pady=0)

        self._notify_on_stop_var = tk.BooleanVar(value=self._settings.notify_on_stop)
        self._i18n(ctk.CTkSwitch(
            card, variable=self._notify_on_stop_var, text_color=INK,
            progress_color=ACCENT, button_color=INK_DIM, button_hover_color=ACCENT,
            command=self._on_notify_on_stop_changed,
        ), "settings_notify_on_stop", size=11, bold=False).pack(fill="x", padx=12, pady=(0, 4))

        # MESO: found by the background locator's full-frame detection pass
        # (the same one that locates the stat panel), so it costs nothing
        # extra. Needs the user to open the inventory at both session ends
        # to land the endpoint readings. Default on (2026-08-24).
        self._i18n(
            ctk.CTkLabel(card, anchor="w", text_color=INK_DIM), "settings_track_meso", size=11, bold=True
        ).pack(fill="x", padx=12, pady=(3, 0))

        self._track_meso_var = tk.BooleanVar(value=self._settings.track_meso)
        self._i18n(ctk.CTkSwitch(
            card, variable=self._track_meso_var, text_color=INK,
            progress_color=ACCENT, button_color=INK_DIM, button_hover_color=ACCENT,
            command=self._on_track_meso_changed,
        ), "settings_track_meso", size=11, bold=False).pack(fill="x", padx=12, pady=0)
        self._i18n(
            # NOTE the scaling trap: CTkLabel multiplies wraplength by the
            # widget scaling (1.2) before handing it to Tk, so the value here
            # must be ~1.2x SMALLER than the pixel width you actually want
            # (240 -> Tk sees 288px, which fits the ~292px card content at
            # the 400px window). A value >= 244 would push Tk's wrap point
            # past the card edge and the text would be clipped instead of
            # wrapped (reported 2026-09-02: "字數超過框框看不到").
            ctk.CTkLabel(card, anchor="w", wraplength=240, justify="left", text_color=INK_FAINT),
            "settings_track_meso_hint", size=9, bold=False,
        ).pack(fill="x", padx=12, pady=(0, 3))

        # POTION COST (2026-08-28, simplified 2026-09-03): unit price per
        # potion. When set, the Dashboard shows 藥水成本 = bottles actually
        # consumed from the quickbar (start − end count) × unit price, and
        # 淨收益 (meso − potion). The old per-bottle restore amount is gone:
        # HP/MP readings retired, so a loss-based estimate has no input.
        potion_card = ctk.CTkFrame(scroll, fg_color=SURFACE, corner_radius=12)
        potion_card.pack(fill="x", padx=2, pady=(4, 2))
        self._i18n(
            ctk.CTkLabel(potion_card, anchor="w", text_color=INK_DIM), "settings_potion", size=11, bold=True
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(5, 0))
        self._i18n(
            ctk.CTkLabel(potion_card, anchor="w", wraplength=240, justify="left", text_color=INK_FAINT),
            "settings_potion_hint", size=9, bold=False,
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 3))

        self._potion_entries: dict[str, ctk.CTkEntry] = {}

        def add_potion_entry(row: int, i18n_key: str, attr: str) -> None:
            ctk.CTkLabel(potion_card, text=self._t(i18n_key), font=self._font(10, bold=False),
                         text_color=INK, anchor="w").grid(row=row, column=0, sticky="w", padx=(12, 6), pady=1)
            var = tk.StringVar(value=str(getattr(self._settings, attr) or 0))
            entry = ctk.CTkEntry(
                potion_card, textvariable=var, width=72, height=26,
                fg_color=SURFACE_2, border_color=SURFACE_2, text_color=INK,
                font=_FONT_MONO_SM, justify="right",
            )
            entry.grid(row=row, column=1, sticky="e", padx=(6, 12), pady=1)
            self._potion_entries[attr] = entry

            def on_change(_e=None, a=attr, v=var) -> None:
                try:
                    val = int(v.get().replace(",", ""))
                except ValueError:
                    val = 0
                if getattr(self._settings, a) != val:
                    setattr(self._settings, a, val)
                    self._persist_settings()
                    self._apply_visibility()
                    self._render(self._last)

            entry.bind("<KeyRelease>", on_change)

        add_potion_entry(2, "settings_hp_potion_price", "hp_potion_price")
        add_potion_entry(3, "settings_mp_potion_price", "mp_potion_price")
        potion_card.grid_columnconfigure(0, weight=1)

        # QUICK-SLOT POTION (2026-09-02, reworked 2026-09-03): pick which of
        # the 8 quickbar slots holds the HP potion and which holds the MP
        # potion. Positions auto-detect at the bottom-right; a manual mark
        # overrides that.
        quick_card = ctk.CTkFrame(scroll, fg_color=SURFACE, corner_radius=12)
        quick_card.pack(fill="x", padx=2, pady=(4, 2))
        self._i18n(
            ctk.CTkLabel(quick_card, anchor="w", text_color=INK_DIM), "settings_quick_slot", size=11, bold=True
        ).pack(fill="x", padx=12, pady=(5, 0))
        self._i18n(
            ctk.CTkLabel(quick_card, anchor="w", wraplength=240, justify="left", text_color=INK_FAINT),
            "settings_quick_slot_hint", size=9, bold=False,
        ).pack(fill="x", padx=12, pady=(0, 3))

        slot_values = [self._t("settings_quick_slot_off")] + list(QUICK_SLOT_NAMES)

        def add_slot_picker(i18n_key: str, attr: str) -> None:
            row = ctk.CTkFrame(quick_card, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=(0, 3))
            ctk.CTkLabel(row, text=self._t(i18n_key), font=self._font(10, bold=False),
                         text_color=INK, anchor="w").pack(side="left")
            menu = ctk.CTkOptionMenu(
                row, values=slot_values,
                command=lambda v, a=attr: self._on_quick_slot_changed(a, v),
                fg_color=SURFACE_2, button_color=SURFACE_2, button_hover_color=TRACK_BG,
                text_color=INK, font=self._font(10, bold=False),
                dropdown_fg_color=SURFACE_2, dropdown_hover_color=TRACK_BG,
                dropdown_text_color=INK, dropdown_font=self._font(10, bold=False),
            )
            idx = getattr(self._settings, attr)
            menu.set(self._t("settings_quick_slot_off") if not idx else QUICK_SLOT_NAMES[idx - 1])
            menu.pack(side="right")
            setattr(self, "_quick_menu_" + attr, menu)

        add_slot_picker("settings_quick_slot_hp", "hp_quick_slot_index")
        add_slot_picker("settings_quick_slot_mp", "mp_quick_slot_index")

        # MANUAL POSITION: mark the status bar and the meso counter with the
        # mouse so the HUD OCRs those exact screen regions -- what makes it
        # work under a screen magnifier (Magpie), where the game window's own
        # rect no longer matches what is visible on screen.
        manual_card = ctk.CTkFrame(scroll, fg_color=SURFACE, corner_radius=12)
        manual_card.pack(fill="x", padx=2, pady=(4, 2))

        self._i18n(
            ctk.CTkLabel(manual_card, anchor="w", text_color=INK_DIM), "settings_manual", size=11, bold=True
        ).pack(fill="x", padx=12, pady=(5, 0))

        self._use_manual_var = tk.BooleanVar(value=self._settings.use_manual)
        self._i18n(ctk.CTkSwitch(
            manual_card, variable=self._use_manual_var, text_color=INK,
            progress_color=ACCENT, button_color=INK_DIM, button_hover_color=ACCENT,
            command=self._on_use_manual_changed,
        ), "settings_use_manual", size=11, bold=False).pack(fill="x", padx=12, pady=(0, 3))

        self._stat_region_button = ctk.CTkButton(
            manual_card, command=self._on_set_stat_region,
            fg_color=SURFACE_2, hover_color=TRACK_BG, text_color=INK,
            corner_radius=9, height=28,
        )
        self._i18n(self._stat_region_button, "settings_set_stat_region", size=11, bold=True)
        self._stat_region_button.pack(fill="x", padx=12, pady=(0, 3))

        self._meso_region_button = ctk.CTkButton(
            manual_card, command=self._on_set_meso_region,
            fg_color=SURFACE_2, hover_color=TRACK_BG, text_color=INK,
            corner_radius=9, height=28,
        )
        self._i18n(self._meso_region_button, "settings_set_meso_region", size=11, bold=True)
        self._meso_region_button.pack(fill="x", padx=12, pady=(0, 3))

        # Quickbar position marker lives here too (2026-09-02): it marks a
        # screen region like the stat bar and meso counter, so it belongs
        # with them under 手動設定位置 rather than on the quick-slot card.
        self._quick_bar_button = ctk.CTkButton(
            manual_card, command=self._on_set_quick_bar,
            fg_color=SURFACE_2, hover_color=TRACK_BG, text_color=INK,
            corner_radius=9, height=28,
        )
        self._i18n(self._quick_bar_button, "settings_set_quick_bar", size=11, bold=True)
        self._quick_bar_button.pack(fill="x", padx=12, pady=(0, 3))

        self._manual_status_label = ctk.CTkLabel(
            manual_card, anchor="w", wraplength=240, justify="left", text_color=INK_FAINT,
            font=self._font(9, bold=False),
        )
        self._manual_status_label.pack(fill="x", padx=12, pady=(0, 3))
        self._manual_warning_label = ctk.CTkLabel(
            manual_card, anchor="w", wraplength=240, justify="left", text_color=HP_COLOR,
            font=self._font(9, bold=True),
        )
        self._manual_warning_label.pack(fill="x", padx=12, pady=(0, 5))
        self._refresh_manual_status()

    # ---- settings callbacks ------------------------------------------------

    def _on_topmost_changed(self) -> None:
        self._settings.topmost = self._topmost_var.get()
        self.root.attributes("-topmost", self._settings.topmost)
        self._persist_settings()

    def _on_language_button_changed(self, value: str) -> None:
        self._apply_language("zh" if value == "中文" else "en")

    def _on_interval_changed(self, value: float) -> None:
        self._settings.window_min = round(value)
        self._interval_header_label.configure(text=self._interval_header_text())
        self._persist_settings()
        # Doesn't retroactively affect the currently-running session's
        # already-baked-in target -- takes effect for the *next* session,
        # same as the interval_minutes recorded on SessionSummary.finalize().

    def _on_switch_changed(self, key: str, attr: str, var: tk.BooleanVar) -> None:
        setattr(self._settings, attr, var.get())
        self._persist_settings()
        if key != "exp_pct":  # visibility-affecting; exp_pct only changes rendered text
            self._apply_visibility()
        self._render(self._last)  # immediate feedback

    def _on_auto_stop_changed(self) -> None:
        self._settings.auto_stop = self._auto_stop_var.get()
        self._persist_settings()

    def _on_save_on_restart_changed(self) -> None:
        self._settings.save_on_restart = self._save_on_restart_var.get()
        self._persist_settings()

    def _on_notify_on_stop_changed(self) -> None:
        self._settings.notify_on_stop = self._notify_on_stop_var.get()
        self._persist_settings()

    def _on_track_meso_changed(self) -> None:
        self._settings.track_meso = self._track_meso_var.get()
        self._persist_settings()
        # The live tab's Meso row is gated on this setting (see
        # _apply_visibility) -- show/hide it immediately.
        self._apply_visibility()

    def _rebuild_manual_source(self) -> None:
        """Build/clear the manual-region capture source from the current
        settings. Called whenever use_manual or a region changes."""
        s = self._settings
        if s.use_manual and s.manual_stat_region is not None:
            from .capture import ManualScreenCapture

            self._manual_source = ManualScreenCapture(s.manual_stat_region, s.manual_meso_region)
        else:
            self._manual_source = None
        # Regions/toggle changed: invalidate the one-shot calibration and force
        # the next tick to re-run detection immediately.
        self._manual_calibrated = False
        if s.use_manual:
            self._stat_boxes = None  # manual detection is one-shot; re-detect
        else:
            # Restore the persisted last-known-good auto positions (may be None
            # on first run -> falls back to regions.FIELD_BOXES until detected).
            self._stat_boxes = s.auto_stat_frac
        self._locate_ticks = LOCATE_INTERVAL_TICKS

    def _active_source(self):
        """The capture source for this tick/locate pass: the manual screen
        region source when manual mode is on, otherwise the auto source."""
        return self._manual_source if self._manual_source is not None else self._source

    # ---- persistence -------------------------------------------------------

    def _load_history(self) -> list[SessionSummary]:
        """Load persisted session history from disk; empty list on any error."""
        try:
            with open(_history_path(), encoding="utf-8") as f:
                data = json.load(f)
            return [SessionSummary(**d) for d in data]
        except Exception:
            return []

    def _save_history(self) -> None:
        """Best-effort persist of the session history to disk."""
        try:
            data = [dataclasses.asdict(s) for s in self._session_history]
            with open(_history_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _persist_settings(self) -> None:
        save_settings(self._settings)

    def _refresh_manual_status(self) -> None:
        """Update the settings status line to show whether each manual region
        has been marked (with its screen coordinates when set)."""
        s = self._settings

        def line(region, set_key, unset_key) -> str:
            if region is None:
                return self._t(unset_key)
            l, t, r, b = region
            return f"{self._t(set_key)} ({l},{t})-({r},{b})"

        self._manual_status_label.configure(text="\n".join([
            line(s.manual_stat_region, "settings_stat_region_set", "settings_stat_region_unset"),
            line(s.manual_meso_region, "settings_meso_region_set", "settings_meso_region_unset"),
            line(s.manual_quick_bar_region, "settings_quick_bar_set", "settings_quick_bar_unset"),
            *self._manual_feedback_lines(),
        ]))
        # Red warning when manual mode is on but the stat region (the one
        # required to start) hasn't been marked yet.
        if s.use_manual and s.manual_stat_region is None:
            self._manual_warning_label.configure(text="⚠ " + self._t("settings_manual_missing_prompt"))
        else:
            self._manual_warning_label.configure(text="")

    def _manual_feedback_lines(self) -> list[str]:
        """One extra status line describing the manual-mode detection result,
        so the user knows right away whether their marked box is good."""
        if not (self._settings.use_manual and self._settings.manual_stat_region is not None):
            return []
        if not self._manual_calibrated:
            return [self._t("settings_manual_detecting")]
        if self._stat_boxes:
            return [self._t("settings_manual_detected", n=len(self._stat_boxes))]
        return [self._t("settings_manual_detect_failed")]

    def _apply_detect_button_visibility(self) -> None:
        """The 辨識 button is shown in both modes: manual mode re-runs the
        marked-region detection; auto mode runs a one-shot auto locate so the
        user can verify auto-detection works before falling back to manual."""
        if getattr(self, "_detect_button", None) is None:
            return
        self._detect_button.grid()

    def _on_dashboard_slot_pick(self, event, attr: str) -> None:
        """Dashboard potion-card position value clicked: pop a quickbar-key
        menu (關閉 + the 8 keys) -- no Settings-tab detour (2026-09-02)."""
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(
            label=self._t("settings_quick_slot_off"),
            command=lambda: self._set_quick_slot_from_dashboard(attr, 0),
        )
        for i, name in enumerate(QUICK_SLOT_NAMES, start=1):
            menu.add_command(label=name, command=lambda i=i: self._set_quick_slot_from_dashboard(attr, i))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _set_quick_slot_from_dashboard(self, attr: str, idx: int) -> None:
        setattr(self._settings, attr, idx)
        self._persist_settings()
        # Keep the Settings-tab dropdown in sync if it exists.
        menu = getattr(self, "_quick_menu_" + attr, None)
        if menu is not None:
            menu.set(self._t("settings_quick_slot_off") if not idx else QUICK_SLOT_NAMES[idx - 1])
        self._apply_visibility()
        self._render(self._last)

    def _on_dashboard_count_edit(self, key: str) -> None:
        """Dashboard potion-count value clicked: hand-correct an OCR misread.
        Stores into the UI's last-read slot count (what 辨識 would have set);
        the next scan replaces it."""
        attr = "_last_hp_slot_count" if key == "hpcount" else "_last_mp_slot_count"
        current = getattr(self, attr)
        with self._modal():
            answer = simpledialog.askstring(
                self._t("stat_edit_title"),
                self._t("potion_count_edit_prompt"),
                initialvalue=f"{current:,}" if current is not None else "",
                parent=self.root,
            )
        if answer is None:
            return
        try:
            setattr(self, attr, int(answer.strip().replace(",", "")))
        except ValueError:
            return
        self._render(self._last)

    def _on_map_edit(self) -> None:
        with self._modal():
            name = simpledialog.askstring(
                self._t("map_dialog_title"), self._t("map_dialog_prompt"),
                initialvalue=self._settings.map_name, parent=self.root,
            )
        if name is None:
            return
        self._settings.map_name = name.strip()
        self._persist_settings()
        self._render(self._last)

    def _on_use_manual_changed(self) -> None:
        self._settings.use_manual = self._use_manual_var.get()
        self._rebuild_manual_source()
        self._refresh_manual_status()
        self._apply_detect_button_visibility()
        self._persist_settings()

    def _on_set_stat_region(self) -> None:
        self._select_region(self._on_stat_region_selected, "settings_set_stat_region")

    def _on_set_meso_region(self) -> None:
        self._select_region(self._on_meso_region_selected, "settings_set_meso_region")

    def _on_stat_region_selected(self, region) -> None:
        self._settings.manual_stat_region = region
        self._refresh_manual_status()
        self._rebuild_manual_source()
        self._persist_settings()

    def _on_meso_region_selected(self, region) -> None:
        self._settings.manual_meso_region = region
        self._refresh_manual_status()
        self._rebuild_manual_source()
        self._persist_settings()

    # ---- quick-slot potion (2026-09-02, reworked 2026-09-03) ---------------

    def _on_quick_slot_changed(self, attr: str, value: str) -> None:
        """Quick-slot picker: '關閉' disables, a key name selects the slot
        (1-8). `attr` is "hp_quick_slot_index" / "mp_quick_slot_index"."""
        if value == self._t("settings_quick_slot_off"):
            setattr(self._settings, attr, 0)
        else:
            try:
                idx = QUICK_SLOT_NAMES.index(value) + 1
            except ValueError:
                return
            setattr(self._settings, attr, idx)
        self._persist_settings()
        self._apply_visibility()
        self._render(self._last)

    def _on_set_quick_bar(self) -> None:
        self._select_region(self._on_quick_bar_selected, "settings_set_quick_bar")

    def _on_quick_bar_selected(self, region) -> None:
        self._settings.manual_quick_bar_region = region
        self._refresh_manual_status()  # quickbar status line lives here now
        self._persist_settings()

    def _refresh_quick_bar_status(self) -> None:
        """Quickbar position status now renders inside _manual_status_label
        (the marker moved under 手動設定位置, 2026-09-02); kept as a thin
        alias for existing callers."""
        if getattr(self, "_manual_status_label", None) is not None:
            self._refresh_manual_status()

    def _grab_quick_bar_image(self) -> Image.Image | None:
        """The whole quickbar row as an image: the manually marked screen
        region when set (mss, screen coords), else the auto bottom-right
        region cropped out of the game frame (QUICK_BAR_FRAC)."""
        s = self._settings
        if s.manual_quick_bar_region is not None:
            if sys.platform != "win32":
                return None
            l, t, r, b = s.manual_quick_bar_region
            try:
                import mss

                with mss.mss() as m:
                    shot = m.grab({"left": l, "top": t, "width": r - l, "height": b - t})
                return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            except Exception:
                return None
        try:
            frame = self._active_source().grab_full()
        except Exception:
            return None
        w, h = frame.size
        l, t, r, b = QUICK_BAR_FRAC
        return frame.crop((int(l * w), int(t * h), int(r * w), int(b * h)))

    def _save_quickbar_debug(self, img=None, scale: int = 1, boxes=None) -> None:
        """Dump the quickbar crop + OCR boxes next to the app so a failing
        potion read can be diagnosed from the image instead of guesswork
        (v1.9.3 diagnostic; remove once the misread is fixed)."""
        try:
            if img is not None:
                img.save("quickbar_debug.png")
            with open("quickbar_debug.txt", "w", encoding="utf-8") as f:
                f.write(f"scale={scale}\n")
                if img is not None:
                    f.write(f"size={img.width}x{img.height}\n")
                f.write(f"boxes={boxes!r}\n")
        except Exception:
            pass

    def _read_slot_counts(self, debug_out: bool = False) -> dict[int, int]:
        """Detect the potion count in each of the 8 quickbar slots, returning
        {slot_index: count} for slots whose count was read. Detection (not
        recognition-only) is used because the count sits at a variable spot
        inside each slot; the small region keeps it cheap. The count is
        assigned to its slot by the key labels above each slot
        (_quickbar_slots_from_boxes) -- equal division landed counts in the
        wrong slot because the crop includes blank margins (2026-09-06).
        debug_out saves the crop + boxes so a total miss ({}) can be looked
        at instead of guessed at (2026-09-07)."""
        img = self._grab_quick_bar_image()
        if img is None:
            if debug_out:
                self._save_quickbar_debug()
            return {}
        # Tiny native-resolution quickbars read badly at 1x -- upscale small
        # crops before detection (LANCZOS, cheap on a <300px-wide region).
        scale = 1
        if min(img.size) < 300:
            scale = 2
            img = img.resize(
                (img.width * scale, img.height * scale), Image.Resampling.LANCZOS
            )
        try:
            boxes = self._ocr.detect_text(img)
        except Exception:
            return {}
        if debug_out:
            # Save the crop + boxes at the SAME scale (both pre-downscale)
            # so the dump matches the image pixel-for-pixel when diagnosing.
            self._save_quickbar_debug(img, scale, boxes)
        w, h = img.size
        if w <= 0 or h <= 0:
            return {}
        if scale > 1:
            boxes = [
                (int(x // scale), int(y // scale), int(bw // scale), int(bh // scale), text)
                for x, y, bw, bh, text in boxes
            ]
            w, h = w // scale, h // scale
        return _quickbar_slots_from_boxes(boxes, w, h)

    def _scan_quick_slots_to_last(self) -> None:
        """Read the quick-slot potion counts and stash them in the _last_*
        fields so the potion rows show a value before a session has started
        (辨識 pass, 2026-09-03)."""
        s = self._settings
        if not (s.hp_quick_slot_index or s.mp_quick_slot_index):
            return
        counts = self._read_slot_counts(debug_out=True)
        self._log(f"[{time.strftime('%H:%M:%S')}] quickbar slots={counts}")
        if s.hp_quick_slot_index in counts:
            self._last_hp_slot_count = counts[s.hp_quick_slot_index]
        if s.mp_quick_slot_index in counts:
            self._last_mp_slot_count = counts[s.mp_quick_slot_index]

    def _maybe_scan_quick_slot(self) -> None:
        """Throttled per-tick quickbar read (same cadence as the manual meso
        scan): while running, feed the HP/MP slot counts into Session so
        start/end endpoints land (see Session.record_potion)."""
        s = self._settings
        if not (s.hp_quick_slot_index or s.mp_quick_slot_index):
            return
        if self._run_state != "running":
            return
        self._quick_slot_ticks = getattr(self, "_quick_slot_ticks", 0) + 1
        if self._quick_slot_ticks < QUICK_SLOT_SCAN_INTERVAL_TICKS:
            return
        self._quick_slot_ticks = 0
        counts = self._read_slot_counts()
        if not counts:
            return
        if s.hp_quick_slot_index in counts:
            self._session.record_potion("hp", counts[s.hp_quick_slot_index])
        if s.mp_quick_slot_index in counts:
            self._session.record_potion("mp", counts[s.mp_quick_slot_index])
        self._log(f"[{time.strftime('%H:%M:%S')}] quickbar slots={counts}")
        self._render(self._last)

    def _select_region(self, callback, title_key: str) -> None:
        """Open the fullscreen region selector and hand the result to `callback`
        when the user finishes (None if they cancel). Blocks on wait_window.
        Wrapped in _modal() so a session can't finalize mid-selection."""
        with self._modal():
            selector = RegionSelector(self.root, self._t(title_key), self._t("region_selector_hint"))
            self.root.wait_window(selector.top)
            if selector.result is not None:
                callback(selector.result)

    def _apply_visibility(self) -> None:
        s = self._settings
        visible_stats = {"level": True, "hp": s.show_hp, "mp": s.show_mp, "exp": s.show_exp}
        for key, (lbl, bar, value) in self._stat_rows.items():
            widgets = [lbl, value] + ([bar] if bar else [])
            for w in widgets:
                w.grid() if visible_stats[key] else w.grid_remove()

        visible_kv = {
            "startexp": True, "expdiff": True, "eta": s.show_eta,
            "projexp": s.show_proj_exp,
            "mesostart": s.track_meso, "mesocurrent": s.track_meso,
            "mesosale": s.track_meso,
            # Potion rows are ALWAYS visible (2026-09-02): unset values show
            # '--' and are filled by clicking or by 辨識.
            "hpslot": True, "mpslot": True, "hpcount": True, "mpcount": True,
            # Potion cost/net rows only make sense once prices are configured.
            "potioncost": self._potion_enabled(), "netmeso": self._potion_enabled(),
        }
        for key, (lbl, value) in self._kv_rows.items():
            for w in (lbl, value):
                w.grid() if visible_kv[key] else w.grid_remove()

        # The meso hint only makes sense when meso tracking is on.
        if getattr(self, "_meso_hint_label", None) is not None:
            self._meso_hint_label.grid() if s.track_meso else self._meso_hint_label.grid_remove()

        # Potion card: always shown in full (the four rows are always mapped).
        if getattr(self, "_potion_card", None) is not None:
            self._potion_card.grid()

    # ---- tick loop ---------------------------------------------------------

    def _tick(self) -> None:
        # Every path through this method must reschedule -- this loop is the
        # only thing driving the HUD, so an exception escaping before
        # self.root.after(...) freezes it permanently on stale data. The
        # window itself stays responsive, which makes the failure especially
        # confusing: buttons still click, Restart Session still "works", and
        # nothing ever updates again.
        #
        # Hence both the except *and* the finally. The original try/except
        # wasn't enough on its own: in the release .exe an unencodable OCR
        # character raised UnicodeEncodeError out of _do_tick's debug print,
        # and the handler's own `print(... {e!r})` re-raised on the same
        # unencodable text, so the reschedule below was never reached (see
        # the stdout-sink note at the top of this module for that trigger's
        # actual fix, and _log for why logging can no longer raise at all).
        # `finally` is what makes the loop survive the *next* such bug.
        next_delay = TARGET_MS
        try:
            next_delay = self._do_tick()
        except Exception as e:
            self._log(f"[{time.strftime('%H:%M:%S')}] tick error: {e!r}")
            with contextlib.suppress(Exception):
                self._set_status_error(self._t("status_error_unknown", detail=str(e)))
        finally:
            self.root.after(next_delay, self._tick)

    @staticmethod
    def _log(message: str) -> None:
        """Debug logging must never be able to kill the tick loop -- it is the
        least important thing this app does and has already taken the whole
        HUD down once (see _tick)."""
        with contextlib.suppress(Exception):
            print(message, flush=True)

    def _do_tick(self) -> int:
        t0 = time.perf_counter()

        # Kick the background locator (throttled inside). It re-finds the
        # stat panel fields + meso counter on the full frame so reads stay
        # correct under screen magnifiers (Megapipe) and a dragged
        # inventory window.
        self._try_locate()
        # Warn once if the screen resolution changed (stale manual regions).
        self._check_screen_change()

        try:
            if self._stat_boxes:
                # Located path (Megapipe-safe): crop each detected field box
                # out of a single full-frame grab and OCR them recognition-
                # only, exactly like grab_fields does for the fixed boxes.
                # Detection boxes are tight, so pad each crop slightly.
                frame = self._active_source().grab_full()
                fw, fh = frame.size
                field_images = {}
                for name, (fx, fy, fw2, fh2) in self._stat_boxes.items():
                    if name not in ("LV", "EXP"):
                        continue  # HP/MP no longer tracked (2026-09-02)
                    x = max(0, int(fx * fw) - 2)
                    y = max(0, int(fy * fh) - 2)
                    w = max(1, int(fw2 * fw) + 4)
                    h = max(1, int(fh2 * fh) + 4)
                    field_images[name] = frame.crop((x, y, min(fw, x + w), min(fh, y + h)))
            else:
                field_images = self._active_source().grab_fields()
        except RuntimeError as e:
            # Game window gone (closed/crashed), minimized, or the stat panel
            # is covered by another window -- don't crash the HUD, show it
            # plainly and keep retrying at a slower pace in case it clears.
            #
            # Logged on *transition* only: this path produces no other output,
            # so a persistently obscured panel used to leave a completely
            # empty log with nothing to diagnose from -- but logging every
            # 2s retry would bury the real ticks.
            if str(e) != self._last_capture_error:
                self._log(f"[{time.strftime('%H:%M:%S')}] capture unavailable: {e}")
                self._last_capture_error = str(e)
            self._set_status_error(self._localize_error(str(e)))
            # The session clock is wall-clock time (Session.elapsed()), not
            # tick-driven, so it keeps running even while OCR can't read the
            # panel (game window covered, alt-tabbed away, minimized). Both
            # of these used to be skipped entirely on this path: the timer
            # chip froze at its last-rendered text even though the real
            # countdown kept going underneath, and a session whose window
            # stayed blocked past its interval would never auto-finalize at
            # all, silently overrunning forever.
            self._update_timer_label()
            self._maybe_finalize_on_timeout()
            return 2000
        if self._last_capture_error is not None:
            self._log(f"[{time.strftime('%H:%M:%S')}] capture recovered")
            self._last_capture_error = None

        # Every crop is scaled from the client size (regions.py), so a log
        # without it can't explain a bad read -- and a mid-session resize is
        # exactly the kind of thing that moves the panel out from under the
        # boxes. Logged once at startup and again on any change.
        client_size = getattr(self._active_source(), "client_size", None)
        if client_size is not None and client_size != self._last_client_size:
            self._log(f"[{time.strftime('%H:%M:%S')}] client size: {client_size[0]}x{client_size[1]}")
            self._last_client_size = client_size
        # HP/MP are no longer read on the tick (2026-09-02): quick-slot potion
        # counts replaced them, so recognition runs on LV + EXP only.
        field_images = {k: v for k, v in field_images.items() if k in ("LV", "EXP")}
        field_text = {name: self._ocr.read_field(img) for name, img in field_images.items()}
        snap = parse_fields(field_text)
        self._log(f"[{time.strftime('%H:%M:%S')}] fields={field_text}")
        self._log(f"          -> {snap}")
        # A single tick occasionally misses a field (combat effects/floating
        # damage numbers over the HP/MP bars, transient OCR confidence dips) --
        # observed live: HP briefly read as None while MP/EXP/LV parsed fine on
        # the same frame. Carry forward the last known value per field instead
        # of flickering to '--' on every miss; a field that's genuinely gone
        # (e.g. OCR permanently broken) will just show stale data, which is a
        # more honest failure mode than a blank field for a live number.
        merged = StatSnapshot(*(
            new if new is not None else old
            for new, old in zip(vars(snap).values(), vars(self._last).values())
        ))
        self._last = merged
        # Hand-typed corrections (see _on_stat_edit) fold in on top of the
        # merged OCR snapshot, so both the HUD and Session.record see the
        # player-corrected values instead of the misread ones.
        self._apply_manual_overrides()
        merged = self._last
        # hp_max/mp_max are passed purely so Session can sanity-check them --
        # a tick whose max doesn't match the rest of the session was misparsed
        # (see rate.py's _LossTracker) and is dropped before it can inflate the
        # loss totals.
        #
        # There used to be a "does this frame even look like the stat panel?"
        # gate here (reject the tick unless LV parsed). It was removed after
        # ablating it against both live captures: it changed the totals by
        # exactly zero, because rate.py already rejects those same frames one
        # layer down -- and it carried a real risk of its own, since a broken
        # LV crop would have stopped a session recording anything at all.
        # tests/test_captured_regression.py replays the real failure through
        # this path with no gate in front of it.
        # Gated on run_state rather than relying on Session's own pause/no-op
        # behaviour: while "stopped" the Session may never have been started
        # at all (see _run_state's docstring in __init__), and feeding it
        # ticks here would silently begin calibrating/tracking a session the
        # user hasn't asked for yet.
        if self._run_state == "running":
            self._session.record(
                merged.exp_cur, merged.hp_cur, merged.mp_cur, merged.exp_pct,
                hp_max=merged.hp_max, mp_max=merged.mp_max, level=merged.level,
            )

        self._maybe_finalize_on_timeout()

        self._render(merged)
        self._maybe_refresh_manual_meso()
        self._maybe_scan_quick_slot()
        # While stopped nothing is recorded -- throttle to 0.5Hz to keep idle
        # CPU near zero (see TARGET_MS_IDLE). Running/paused keep the 1Hz rate.
        target = TARGET_MS_IDLE if self._run_state == "stopped" else TARGET_MS
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return max(0, int(target - elapsed_ms))

    def _try_locate(self) -> None:
        """Locate the stat panel + meso counter.

        Manual mode: a one-shot *synchronous* detection of the user-marked
        region, run inline on the main thread. The previous design ran it on a
        daemon thread and delivered the result via root.after(), which is not
        reliably thread-safe -- when that callback failed to fire, the HUD sat
        stuck "偵測中" forever after a relaunch. Inline detection reuses the
        main OCR engine (already loaded) and lands deterministically; it runs
        once (guarded by _manual_calibrated) so the ~600ms cost is paid a
        single time.

        Auto mode: the original throttled background pass (unchanged), which
        re-finds the panel on the full frame so reads stay correct under a
        screen magnifier (Magpie).
        """
        if self._settings.use_manual:
            if not self._manual_calibrated and self._settings.manual_stat_region is not None:
                self._run_manual_detection()
            return

        self._locate_ticks = getattr(self, "_locate_ticks", 0) + 1
        if self._locate_ticks < LOCATE_INTERVAL_TICKS:
            return
        self._locate_ticks = 0
        _thread = getattr(self, "_locate_thread", None)
        if _thread is not None and _thread.is_alive():
            return  # previous pass still running -- skip this round

        def _locate() -> None:
            try:
                # Own OCR engine: the main instance is used by the tick
                # thread concurrently, and RapidOCR is not documented
                # thread-safe. Lazy-init here so the first pass's model
                # load (seconds) happens off the UI thread too.
                locate_ocr = getattr(self, "_locate_ocr", None)
                if locate_ocr is None:
                    locate_ocr = StatPanelOcr()
                    self._locate_ocr = locate_ocr

                # Auto mode: fresh capture on THIS thread. The shared
                # mss/win32 handles live on the tick thread, and re-entering
                # them from a second thread risks races. A new
                # GameWindowCapture is cheap (one EnumWindows + one grab). On
                # non-Windows (dev/tests) the shared source is a pure-PIL
                # stand-in, safe to reuse.
                if sys.platform == "win32":
                    from .capture import GameWindowCapture

                    frame = GameWindowCapture().grab_full()
                else:
                    frame = self._source.grab_full()
                boxes = locate_ocr.detect_text(frame)
                stat = find_stat_fields(boxes)
                meso_found = find_meso_candidate_verified(boxes, frame, frame.size)
                fw, fh = frame.size
                stat_frac = {
                    name: (x / fw, y / fh, w / fw, h / fh)
                    for name, (x, y, w, h) in stat.items()
                }
                meso_frac = None
                meso_value = None
                if meso_found is not None:
                    x, y, w, h, meso_value = meso_found
                    meso_frac = (x / fw, y / fh, w / fw, h / fh)
            except Exception:
                stat_frac, meso_frac, meso_value = {}, None, None
            self.root.after(
                0,
                lambda s=stat_frac, m=meso_frac, v=meso_value: self._apply_locate(s, m, v),
            )

        self._locate_thread = threading.Thread(target=_locate, daemon=True)
        self._locate_thread.start()

    def _run_auto_detection(self) -> None:
        """One-shot auto-mode detection, run synchronously on the main thread
        (same safety model as _run_manual_detection). Grabs the full frame,
        detects the stat fields + meso counter, and applies the result so the
        user can confirm auto-detection works before switching to manual."""
        self._status_pill.configure(
            text=self._t("status_calibrating"), fg_color=SURFACE_2, text_color=EXP_COLOR,
        )
        self.root.update_idletasks()
        stat_frac, meso_frac, meso_value = {}, None, None
        frame = None
        try:
            if sys.platform == "win32":
                from .capture import GameWindowCapture

                frame = GameWindowCapture().grab_full()
            else:
                frame = self._source.grab_full()
            boxes = self._ocr.detect_text(frame)
            stat = find_stat_fields(boxes)
            meso_found = find_meso_candidate_verified(boxes, frame, frame.size) if self._settings.track_meso else None
            fw, fh = frame.size
            stat_frac = {
                name: (x / fw, y / fh, w / fw, h / fh)
                for name, (x, y, w, h) in stat.items()
            }
            if meso_found is not None:
                x, y, w, h, meso_value = meso_found
                meso_frac = (x / fw, y / fh, w / fw, h / fh)
        except Exception:
            stat_frac, meso_frac, meso_value = {}, None, None
        self._apply_locate(stat_frac, meso_frac, meso_value)
        self._set_detect_result(stat_frac, meso_frac)
        # A fresh 辨識 pass re-reads the game, so any hand-typed corrections
        # from before are cleared (they'd fight the new detection), and the
        # values just detected are shown immediately for confirmation.
        self._manual_overrides.clear()
        if frame is not None and stat_frac:
            self._read_detected_values(frame, stat_frac)
        self._scan_quick_slots_to_last()
        self._render(self._last)

    def _run_manual_detection(self) -> None:
        """One-shot manual-mode detection, run synchronously on the main thread.

        Subdivides the marked stat region into LV/HP/MP/EXP via detection OCR
        and finds the meso counter in the marked meso region. Reuses the main
        OCR engine -- safe because this runs on the main thread, so there is no
        concurrency with the per-tick recognition reads. Also the "辨識"
        button's action, so a stuck/incorrect auto-detection can always be
        re-triggered by hand.
        """
        s = self._settings
        if not (s.use_manual and s.manual_stat_region is not None):
            return
        self._manual_calibrated = False
        self._refresh_manual_status()  # surface "偵測中…" in the settings status
        # Show "偵測中…" on the dashboard too, and force a repaint BEFORE the
        # blocking pass so the user sees feedback instead of a frozen window.
        self._status_pill.configure(
            text=self._t("status_calibrating"), fg_color=SURFACE_2, text_color=EXP_COLOR,
        )
        self.root.update_idletasks()
        stat_frac, meso_frac, meso_value = {}, None, None
        frame = None
        try:
            from .capture import ManualScreenCapture

            cap = ManualScreenCapture(s.manual_stat_region, s.manual_meso_region)
            frame = cap.grab_full()
            boxes = self._ocr.detect_text(frame)
            stat = find_stat_fields(boxes)
            fw, fh = frame.size
            stat_frac = {
                name: (x / fw, y / fh, w / fw, h / fh)
                for name, (x, y, w, h) in stat.items()
            }
            if s.track_meso and s.manual_meso_region is not None:
                meso_frame = cap.grab_meso()
                mboxes = self._ocr.detect_text(meso_frame)
                meso_found = find_meso_in_region(mboxes)
                if meso_found is not None:
                    x, y, w, h, meso_value = meso_found
                    mw, mh = meso_frame.size
                    meso_frac = (x / mw, y / mh, w / mw, h / mh)
        except Exception:
            stat_frac, meso_frac, meso_value = {}, None, None
        self._apply_locate(stat_frac, meso_frac, meso_value)
        self._set_detect_result(stat_frac, meso_frac)
        # Same as auto mode: clear hand-typed corrections and show the freshly
        # detected values immediately (see _read_detected_values).
        self._manual_overrides.clear()
        if frame is not None and stat_frac:
            self._read_detected_values(frame, stat_frac)
        self._scan_quick_slots_to_last()
        self._render(self._last)

    def _on_detect_clicked(self) -> None:
        """辨識 button: manual mode re-detects the marked regions; auto mode runs
        a one-shot full-frame locate to confirm auto-detection works."""
        if self._settings.use_manual:
            self._run_manual_detection()
        else:
            self._run_auto_detection()

    # ---- manual stat correction (2026-08-27) ------------------------------

    def _on_stat_edit(self, key: str) -> None:
        """Let the player correct an OCR misread by typing the real value of a
        stat field (LV/HP/MP/EXP). Empty input clears the manual value and
        reverts to the OCR stream. The typed value overrides the tick stream
        (see _apply_manual_overrides) until cleared by another edit or a 辨識
        pass re-reads the game. HP/MP edit the *current* value only -- the max
        is left untouched."""
        prompts = {
            "level": ("stat_edit_prompt_level", "level"),
            "hp": ("stat_edit_prompt_hp", "hp_cur"),
            "mp": ("stat_edit_prompt_mp", "mp_cur"),
            "exp": ("stat_edit_prompt_exp", "exp_cur"),
        }
        if key not in prompts:
            return
        prompt_key, snap_attr = prompts[key]
        current = self._manual_overrides.get(key)
        if current is None:
            current = getattr(self._last, snap_attr)
        initial = f"{current:,}" if current is not None else ""
        with self._modal():
            result = simpledialog.askstring(
                self._t("stat_edit_title"),
                self._t(prompt_key) + "\n\n" + self._t("stat_edit_hint"),
                initialvalue=initial,
                parent=self.root,
            )
        if result is None:
            return  # cancelled
        result = result.strip().replace(",", "")
        if result == "":
            self._manual_overrides.pop(key, None)
        else:
            try:
                self._manual_overrides[key] = int(result)
            except ValueError:
                return  # not a number -- keep the previous state
        self._apply_manual_overrides()
        self._render(self._last)

    def _apply_manual_overrides(self) -> None:
        """Fold the player's manual stat corrections into self._last so the
        HUD (and Session.record on the next tick) see the corrected values.
        No-op when no override is active."""
        ov = self._manual_overrides
        if not ov:
            return
        self._last = StatSnapshot(
            level=ov.get("level", self._last.level),
            hp_cur=ov.get("hp", self._last.hp_cur),
            hp_max=self._last.hp_max,
            mp_cur=ov.get("mp", self._last.mp_cur),
            mp_max=self._last.mp_max,
            exp_cur=ov.get("exp", self._last.exp_cur),
            exp_pct=self._last.exp_pct,
        )

    def _read_detected_values(self, frame, stat_frac: dict) -> None:
        """Immediately show the values OCR'd from the detected field boxes
        after a 辨識 pass, so the player can confirm every stat was caught
        (and correct any misread by hand). Merges into _last exactly like a
        normal tick would; a failed read leaves the previous values in place."""
        try:
            fw, fh = frame.size
            field_images = {}
            for name, (fx, fy, fw2, fh2) in stat_frac.items():
                if name not in ("LV", "EXP"):
                    continue  # HP/MP no longer tracked (2026-09-02)
                x = max(0, int(fx * fw) - 2)
                y = max(0, int(fy * fh) - 2)
                w = max(1, int(fw2 * fw) + 4)
                h = max(1, int(fh2 * fh) + 4)
                field_images[name] = frame.crop((x, y, min(fw, x + w), min(fh, y + h)))
            field_text = {name: self._ocr.read_field(img) for name, img in field_images.items()}
            snap = parse_fields(field_text)
            merged = StatSnapshot(*(
                new if new is not None else old
                for new, old in zip(vars(snap).values(), vars(self._last).values())
            ))
            self._last = merged
        except Exception:
            pass

    def _set_detect_result(self, stat_frac: dict, meso_frac) -> None:
        """Set the timed status-pill text after a 辨識 pass, reflecting both the
        stat-field result and -- when meso tracking is on -- whether the meso
        counter was found. The meso counter only exists while the inventory is
        open, so a miss prompts the user to open it."""
        n = len(stat_frac)
        missing = [f for f in ("LV", "HP", "MP", "EXP") if f not in stat_frac]
        if not stat_frac:
            # Auto mode has nothing to "re-mark" -- the user can only make
            # sure the game window is visible; manual mode's instruction is
            # genuinely "re-mark the box".
            self._detect_result_text = (
                self._t("detect_result_fail")
                if self._settings.use_manual
                else self._t("detect_result_fail_auto")
            )
        elif missing:
            # Partial: name the missing field(s) -- "3/4" alone doesn't tell
            # the user which one failed (reported 2026-08-25).
            self._detect_result_text = self._t(
                "detect_result_ok_partial", n=n, missing="、".join(missing)
            )
        elif not self._settings.track_meso:
            self._detect_result_text = self._t("detect_result_ok", n=n)
        elif meso_frac is not None:
            self._detect_result_text = self._t("detect_result_ok_meso", n=n)
        elif self._settings.use_manual and self._settings.manual_meso_region is None:
            # Manual mode, meso position never marked -- the real cause isn't a
            # closed inventory, it's an unset position.
            self._detect_result_text = self._t("detect_result_meso_need_mark", n=n)
        else:
            self._detect_result_text = self._t("detect_result_meso_missing", n=n)
        self._detect_result_ok = bool(stat_frac)
        self._detect_result_until = time.time() + 3.0

    def _apply_locate(
        self,
        stat_frac: dict[str, tuple[float, float, float, float]],
        meso_frac: tuple[float, float, float, float] | None,
        meso_value: int | None,
    ) -> None:
        """Main-thread half of the locator pass (see _try_locate)."""
        if stat_frac:
            self._stat_boxes = stat_frac
            if not self._settings.use_manual:
                # Persist the last-known-good auto positions so a restart (or
                # a transient OCR miss) reuses the real detected boxes instead
                # of the stale fixed reference boxes in regions.py.
                self._settings.auto_stat_frac = dict(stat_frac)
                self._persist_settings()
        elif self._settings.use_manual:
            # Manual one-shot detection found nothing: no boxes, tick shows '--'.
            self._stat_boxes = None
        # else: auto mode with a transient OCR miss -- keep the last-known-good
        # boxes rather than flapping back to the fixed reference boxes.

        if meso_frac is not None:
            self._meso_box = meso_frac
            if meso_value is not None:
                self._last_meso = meso_value
            if self._run_state == "running" and meso_value is not None:
                self._session.record_meso(meso_value)
                self._render(self._last)  # show the updated meso rows promptly

        if self._settings.use_manual:
            # One-shot manual calibration complete (success or not -- don't
            # retry every 5s and keep dropping the game's frames). The user
            # sees the result in the settings status line and can re-mark.
            self._manual_calibrated = True
            self._refresh_manual_status()

    def _maybe_refresh_manual_meso(self) -> None:
        """Manual-mode meso read: cheap recognition-only OCR on the marked
        meso region every MESO_SCAN_INTERVAL_TICKS (~15ms, no detection). The
        locator no longer runs periodically in manual mode, so this is what
        keeps the meso counter fresh when the user opens the inventory."""
        if not (self._settings.use_manual and self._manual_source is not None
                and self._settings.manual_meso_region is not None
                and self._settings.track_meso and self._run_state == "running"):
            return
        self._meso_scan_ticks += 1
        if self._meso_scan_ticks < MESO_SCAN_INTERVAL_TICKS:
            return
        self._meso_scan_ticks = 0
        try:
            img = self._manual_source.grab_meso()
            text = self._ocr.read_field(img)
            value = parse_meso(text)
            if value is not None:
                self._last_meso = value
                self._session.record_meso(value)
                self._render(self._last)
        except Exception:
            pass

    def _update_timer_label(self) -> None:
        """Split out of _render so the capture-error path in _do_tick can
        keep the countdown moving without running a full render against
        stale/absent OCR data."""
        if self._run_state == "stopped":
            # A stopped session (including the very first one, before Start
            # is ever clicked) has no countdown running -- showing a static
            # "10:00" the whole time would look like a stuck timer rather
            # than a genuinely inactive one.
            self._timer_label.configure(text="--:--")
            return
        remaining = max(0.0, self._settings.window_min * 60 - self._session.elapsed())
        remaining_s = f"{int(remaining // 60)}:{int(remaining % 60):02d}"
        self._timer_label.configure(text=self._t("timer_left", time=remaining_s))

    def _maybe_finalize_on_timeout(self) -> None:
        # Skipped while a rename dialog is open: simpledialog.askstring blocks
        # via a nested Tk event loop but doesn't stop self.root.after() timers
        # from firing, so without this guard a session could finalize and
        # insert a new history card underneath the open modal mid-edit. Also
        # skipped outright unless actually running: elapsed() is frozen while
        # paused/stopped anyway, so this wouldn't fire either way, but being
        # explicit here means it can't ever race a state change mid-tick.
        #
        # Called from both branches of _do_tick (capture success and capture
        # failure) -- Session.elapsed() is wall-clock time, not tick-driven,
        # so a session must still be able to hit its interval and finalize
        # even while the game window is covered/minimized for the whole
        # window, not just while OCR happens to be succeeding.
        if not self._modal_open and self._run_state == "running" \
                and self._session.elapsed() >= self._settings.window_min * 60:
            self._finalize_and_maybe_stop()

    def _commit_session_to_history(self) -> None:
        # Shared by the timer rollover and a manual restart with
        # save_on_restart on -- exactly one code path commits, so two
        # triggers landing on the same tick can't double-log.
        # Skip logging if the session never got a real EXP reading (restart
        # clicked immediately after launch, before OCR produced anything --
        # a '? -> ?' entry would just be noise), or if essentially no time
        # passed (rapid double-click on the restart button after real data
        # already exists -- start() carries the last known values forward,
        # so a second click 50ms later would otherwise log a valid-looking
        # but meaningless 0-duration, 0-diff entry).
        if self._session.start_exp is not None and self._session.elapsed() >= 1.0:
            summary = self._session.finalize(self._settings.window_min)
            # Stamp the session's map (typed or auto-OCR'd) onto the record so
            # History cards can show it -- the engine (rate.py) doesn't know
            # about maps, this is purely a UI-layer enrichment at commit time.
            # potion_cost is stamped the same way: prices live in Settings,
            # so the UI computes the spend while the session is still alive
            # (before start() clears its counters).
            summary = dataclasses.replace(
                summary,
                map_name=self._settings.map_name or None,
                potion_cost=self._potion_cost(),
            )
            self._session_history.append(summary)
            self._log(f"[{time.strftime('%H:%M:%S')}] {_fmt_summary(summary, len(self._session_history))}")
            self._append_history_card(summary, len(self._session_history))
            self._save_history()
            self._update_history_summary()
            self._refresh_compare_tab()

    def _finalize_and_maybe_stop(self) -> None:
        """The timer rolling over. With auto_stop (default) the session stops
        into a *pending* state -- frozen but not yet committed -- so the user
        can sell equipment and record the proceeds (see _on_record_sale_clicked)
        before the next session commits it. With auto_stop off it commits and
        immediately starts the next session (the pre-sale-recording behaviour:
        no stop, no chance to sell in between)."""
        if self._settings.auto_stop:
            self._stop_into_pending()
        else:
            self._commit_session_to_history()
            self._start_session_with_current_values()

    def _stop_into_pending(self) -> None:
        """Freeze the session WITHOUT committing it to History, leaving it
        pending so the user can sell equipment and record the proceeds against
        it. Shared by the manual Stop button and the auto-stop timer rollover.
        The session is committed when the next one starts (or on app close)."""
        self._session.pause()
        self._run_state = "stopped"
        self._session_pending = True
        self._sale_recorded = False
        self._apply_run_state()
        self._notify_session_end()

    def _commit_pending_session(self) -> None:
        """Finalize + commit the stopped-but-pending session to History (if
        any), clearing the pending flag. No-op when nothing is pending."""
        if not self._session_pending:
            return
        self._session_pending = False
        self._sale_recorded = False
        self._commit_session_to_history()

    def _confirm_start_without_sale(self) -> bool:
        """Ask before discarding the chance to record equipment revenue."""
        with self._modal():
            return messagebox.askyesno(
                self._t("sale_pending_title"),
                self._t("sale_pending_prompt"),
                parent=self.root,
            )

    def _on_restart_clicked(self) -> None:
        if self._settings.save_on_restart:
            self._commit_session_to_history()
        self._start_session_with_current_values()  # resets pause state too, so a restart from "paused" lands in "running"
        self._run_state = "running"
        self._apply_run_state()
        self._render(self._last)  # immediate feedback, don't wait for next tick

    def _on_stop_clicked(self) -> None:
        # End the session WITHOUT starting a new one, and WITHOUT committing
        # yet: leave it pending so the user can sell equipment and record the
        # proceeds against it. Commit happens on the next Start (or app close).
        self._stop_into_pending()
        self._render(self._last)  # immediate feedback, don't wait for next tick

    def _start_session_with_current_values(self) -> None:
        """Begin a new session (restart / auto-stop rollover path, where the
        session is continuous so no confirm dialog is needed), seeding the
        engine with the UI's latest OCR'd readings first (sync_known) and
        recording those readings as the compact window's 初始 baseline."""
        self._baseline = _Baseline(
            exp_cur=self._last.exp_cur, exp_pct=self._last.exp_pct,
            level=self._last.level,
            # meso/potion NOT seeded here: unlike the confirm-dialog path the
            # inventory is usually closed mid-grind, so a stale reading would
            # poison the baseline. They stay None until the inventory reopens.
        )
        self._session.sync_known(
            exp_cur=self._last.exp_cur, exp_pct=self._last.exp_pct,
            level=self._last.level,
        )
        self._session.start()

    def _confirm_start_baseline(self) -> _Baseline | None:
        """Open the pre-start confirm dialog; None when the player cancels."""
        with self._modal():
            dlg = _StartBaselineDialog(self)
            self.root.wait_window(dlg.top)
        return dlg.result

    def _start_session_with_baseline(self, b: _Baseline) -> None:
        """Start a session from the confirmed baseline: engine carries the
        confirmed EXP/HP/MP; meso/potion counts seed the endpoints directly
        (inventory will likely be closed once grinding starts)."""
        self._session.sync_known(
            exp_cur=b.exp_cur, exp_pct=b.exp_pct, level=b.level,
        )
        self._session.start(
            initial_meso=b.meso,
            initial_hp_potion=b.hp_potion,
            initial_mp_potion=b.mp_potion,
        )

    def _on_pause_button_clicked(self) -> None:
        """One button, three roles depending on _run_state -- see
        _apply_run_state for how its label/command follow that state."""
        if self._run_state == "running":
            self._session.pause()
            self._run_state = "paused"
        elif self._run_state == "paused":
            self._session.resume()
            self._run_state = "running"
        else:  # "stopped"
            # Manual mode requires the stat region before starting -- block with
            # a prompt instead of silently starting on unmarked positions.
            if self._settings.use_manual and self._settings.manual_stat_region is None:
                messagebox.showwarning(
                    self._t("settings_manual"), self._t("settings_manual_missing_prompt"),
                    parent=self.root,
                )
                return
            # A previous session is still pending (stopped, uncommitted). If its
            # equipment revenue hasn't been recorded yet, ask before discarding
            # the chance to record it.
            if (self._session_pending and not self._sale_recorded
                    and self._settings.track_meso):
                if not self._confirm_start_without_sale():
                    return
            # Pre-start confirm dialog (2026-09-02): shows every value that
            # will become this session's baseline -- EXP/HP/MP, meso and
            # quick-slot counts (the latter only readable while the inventory
            # is open, which is why they're captured BEFORE Start). The player
            # confirms, re-runs 辨識, or hand-edits a misread. Cancel = stay
            # stopped. This replaces the earlier per-source warning popups:
            # missing values show inline as 未偵測到.
            baseline = self._confirm_start_baseline()
            if baseline is None:
                return
            self._commit_pending_session()
            self._baseline = baseline
            self._start_session_with_baseline(baseline)
            self._sale_done = False  # new session: 賣裝收益/淨收益 reset
            self._run_state = "running"
        self._apply_run_state()
        self._render(self._last)  # immediate feedback, don't wait for next tick

    def _read_meso_now(self) -> int | None:
        """One-shot meso read for the 記錄賣裝 button. Auto mode prefers the
        cached meso box (cheap recognition-only read) and falls back to a
        full-frame detection scan; manual mode OCRs the marked meso region.
        None when the counter isn't found (inventory closed / not marked)."""
        try:
            if self._settings.use_manual:
                if self._manual_source is None or self._settings.manual_meso_region is None:
                    return None
                img = self._manual_source.grab_meso()
                return parse_meso(self._ocr.read_field(img))
            if self._meso_box is not None:
                frame = self._active_source().grab_full()
                fw, fh = frame.size
                fx, fy, fw2, fh2 = self._meso_box
                x = max(0, int(fx * fw) - 2)
                y = max(0, int(fy * fh) - 2)
                w = max(1, int(fw2 * fw) + 4)
                h = max(1, int(fh2 * fh) + 4)
                crop = frame.crop((x, y, min(fw, x + w), min(fh, y + h)))
                value = parse_meso(self._ocr.read_field(crop))
                if value is not None:
                    return value
            frame = self._active_source().grab_full()
            boxes = self._ocr.detect_text(frame)
            found = find_meso_candidate_verified(boxes, frame, frame.size)
            return found[4] if found is not None else None
        except Exception:
            return None

    def _on_record_sale_clicked(self) -> None:
        """Record the meso from selling the pending session's equipment drops.
        Reads the counter now (inventory must be open) and books the increase
        against the pending session, showing a timed status with the result."""
        if not self._session_pending:
            return
        value = self._read_meso_now()
        if value is None:
            self._detect_result_text = self._t("record_sale_need_inventory")
            self._detect_result_ok = False
            self._detect_result_until = time.time() + 3.0
            self._render(self._last)
            return
        delta = self._session.record_sale(value)
        self._sale_recorded = True
        self._sale_done = True
        booked = delta if (delta is not None and delta > 0) else 0
        self._detect_result_text = self._t("record_sale_done", n=f"{booked:,}")
        self._detect_result_ok = True
        self._detect_result_until = time.time() + 3.0
        # Commit the session to History right away (per user request
        # 2026-08-27) -- the record shows up on the History tab immediately
        # instead of waiting for the next Start.
        self._commit_pending_session()
        self._apply_run_state()  # hides the 記錄賣裝 button once committed
        self._render(self._last)

    def _apply_run_state(self) -> None:
        label_key = {"running": "pause_button", "paused": "resume_button", "stopped": "start_button"}[self._run_state]
        self._pause_button.configure(text=self._t(label_key), font=self._font(12, bold=True))
        # Stopped: Start is the only meaningful action -- Restart/Stop have
        # nothing to act on. As the sole button it's centered and shrunk.
        if self._run_state == "stopped":
            self._restart_button.grid_remove()
            self._stop_button.grid_remove()
            self._pause_button.configure(width=STOPPED_BUTTON_WIDTH, height=BUTTON_HEIGHT)
            self._pause_button.grid(row=0, column=0, columnspan=3, sticky="", padx=0)
        elif self._run_state == "paused":
            # Paused: Resume / Stop / Restart all three. Stop is the way to end
            # the session without immediately starting the next one.
            self._pause_button.configure(width=140, height=BUTTON_HEIGHT)
            self._pause_button.grid(row=0, column=0, columnspan=1, sticky="ew", padx=(0, 3))
            self._stop_button.grid(row=0, column=1, columnspan=1, sticky="ew", padx=(0, 3))
            self._restart_button.grid(row=0, column=2, columnspan=1, sticky="ew", padx=(3, 0))
        else:  # running
            self._pause_button.configure(width=140, height=BUTTON_HEIGHT)
            self._pause_button.grid(row=0, column=0, columnspan=1, sticky="ew", padx=(0, 3))
            self._stop_button.grid_remove()
            self._restart_button.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(3, 0))
        # 記錄賣裝 button is only meaningful while a session is stopped-but-
        # pending AND meso tracking is on (the user can still record their
        # equipment-sale proceeds against it).
        if (getattr(self, "_record_sale_button", None) is not None
                and getattr(self, "_record_sale_hint", None) is not None):
            show_sale = (
                self._run_state == "stopped" and self._session_pending
                and self._settings.track_meso
            )
            self._record_sale_button.grid() if show_sale else self._record_sale_button.grid_remove()
            self._record_sale_hint.grid() if show_sale else self._record_sale_hint.grid_remove()
        self._update_compact_visibility()

    # ---- compact 2x2 overlay --------------------------------------------

    def _centered_on_main(self, w: int, h: int) -> str:
        """'WxH+X+Y' geometry that centres the window on the MAIN window's
        actual screen rect. Multi-monitor safe: uses winfo_rootx/y of the
        main window (which may live on a secondary monitor), never a
        hardcoded primary-screen offset -- the old +120+120 / +60+60 put
        dialogs on the primary display even when the user played on another
        screen (reported 2026-09-02)."""
        try:
            x = self.root.winfo_rootx()
            y = self.root.winfo_rooty()
            mw = self.root.winfo_width()
            mh = self.root.winfo_height()
        except Exception:
            x, y, mw, mh = 60, 60, 400, 660
        cx = max(0, x + (mw - w) // 2)
        cy = max(0, y + (mh - h) // 2)
        return f"{w}x{h}+{cx}+{cy}"

    def _ensure_compact_win(self) -> None:
        """Create the small always-on-top overlay (once). Shows the *derived*
        metrics the game itself doesn't display -- HP/MP consumption, meso
        income, EXP change + level-up ETA -- rather than duplicating the live
        HP/MP bars the game already renders."""
        if self._compact_win is not None:
            return
        win = ctk.CTkToplevel(self.root)
        win.title("MsStatTractor")
        win.attributes("-topmost", True)
        win.configure(fg_color=BG)
        # 360x410 (was 360x350): cells now carry four lines each (title /
        # 初始 / 變化 / sub) after the 2026-09-02 request to show both the
        # session-start value and its change in every cell. The extra height
        # keeps the Pause/Stop/Restore row at full size (it was squeezed to
        # ~11px -- invisible -- at 360x300).
        x, y = self._settings.compact_x, self._settings.compact_y
        if x is not None and y is not None:
            win.geometry(f"360x410+{x}+{y}")
        else:
            # First run (no saved placement): centre on the main window so a
            # secondary-monitor setup opens the overlay on the screen the
            # user is actually using (2026-09-02). Once dragged, the spot is
            # remembered in compact_x/y.
            win.geometry(self._centered_on_main(360, 410))
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", self._on_restore_main)
        self._compact_win = win

        grid = ctk.CTkFrame(win, fg_color=BG)
        # No expand: the 2x2 grid keeps its natural height and the timer +
        # button row below stay visible (the bug where the buttons got pushed
        # out of the 300px window by expand=True + taller 3-line cells).
        grid.pack(fill="x", padx=6, pady=(6, 0))
        for c in (0, 1):
            grid.grid_columnconfigure(c, weight=1)
        for r in (0, 1):
            grid.grid_rowconfigure(r, weight=1)

        def add_cell(title_key, key, r, c, color):
            cell = ctk.CTkFrame(grid, fg_color=SURFACE, corner_radius=8)
            cell.grid(row=r, column=c, sticky="nsew", padx=3, pady=3)
            cell.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(cell, text=self._t(title_key), font=self._font(9, bold=True), text_color=color, anchor="w").grid(
                row=0, column=0, sticky="w", padx=(8, 0), pady=(4, 0)
            )
            # Line 2: the session-START value (grey, small) -- the baseline the
            # player confirmed in the pre-start dialog.
            initial = ctk.CTkLabel(cell, text="--", font=_FONT_MONO_SM, text_color=INK_DIM, anchor="e")
            initial.grid(row=1, column=0, sticky="e", padx=(0, 8), pady=(0, 0))
            self._compact_initial[key] = initial
            # Line 3: the CHANGE (coloured, large) -- this is the headline.
            val = ctk.CTkLabel(cell, text="--", font=_FONT_MONO_BOLD, text_color=INK, anchor="e")
            val.grid(row=2, column=0, sticky="e", padx=(0, 8), pady=(0, 0))
            self._compact_labels[key] = val
            # Line 4: sub -- EXP carries the level-up ETA, meso the net
            # income, others a space placeholder (keeps all cells equal height).
            sub = ctk.CTkLabel(cell, text=" ", font=_FONT_MONO_SM, text_color=INK_DIM, anchor="e")
            sub.grid(row=3, column=0, sticky="e", padx=(0, 8), pady=(0, 4))
            if key == "exp":
                self._compact_eta = sub
            if key == "meso":
                self._compact_meso_sub = sub
            return cell

        add_cell("compact_hp_potion", "hpcount", 0, 0, HP_COLOR)
        add_cell("compact_mp_potion", "mpcount", 0, 1, MP_COLOR)
        add_cell("compact_meso", "meso", 1, 0, EXP_COLOR)
        add_cell("compact_exp", "exp", 1, 1, EXP_COLOR)

        self._compact_timer = ctk.CTkLabel(win, text="--:--", font=self._font(11, bold=True), text_color=INK)
        self._compact_timer.pack(pady=(4, 2))

        btn_row = ctk.CTkFrame(win, fg_color="transparent")
        btn_row.pack(fill="x", padx=6, pady=(0, 8))
        self._compact_pause_btn = ctk.CTkButton(
            btn_row, text=self._t("pause_button"), command=self._on_pause_button_clicked,
            fg_color=SURFACE_2, hover_color=TRACK_BG, text_color=INK,
            height=34, font=self._font(12, bold=True),
        )
        self._compact_pause_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self._compact_stop_btn = ctk.CTkButton(
            btn_row, text=self._t("stop_button"), command=self._on_stop_clicked,
            fg_color=SURFACE_2, hover_color=TRACK_BG, text_color=HP_COLOR,
            height=34, font=self._font(12, bold=True),
        )
        self._compact_stop_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self._compact_restore_btn = ctk.CTkButton(
            btn_row, text=self._t("compact_restore"), command=self._on_restore_main,
            fg_color=SURFACE_2, hover_color=TRACK_BG, text_color=INK,
            height=34, font=self._font(12, bold=True),
        )
        self._compact_restore_btn.pack(side="left", expand=True, fill="x")

    def _update_compact_visibility(self) -> None:
        """Running/paused -> minimize the full window and show the 2x2 overlay;
        stopped -> hide the overlay and restore the full window."""
        if self._run_state in ("running", "paused"):
            self._ensure_compact_win()
            self._compact_win.deiconify()
            self._compact_pause_btn.configure(
                text=self._t("pause_button" if self._run_state == "running" else "resume_button")
            )
            self.root.iconify()
        else:
            if self._compact_win is not None:
                self._save_compact_pos()
                self._compact_win.withdraw()
            self.root.deiconify()

    def _save_compact_pos(self) -> None:
        """Persist the compact overlay's current position so a restart restores
        the user's dragged placement."""
        if self._compact_win is None or not self._compact_win.winfo_exists():
            return
        try:
            self._settings.compact_x = self._compact_win.winfo_x()
            self._settings.compact_y = self._compact_win.winfo_y()
            self._persist_settings()
        except Exception:
            pass

    def _on_close(self) -> None:
        """App close: commit any pending (uncommitted) session and persist the
        compact position before tearing down."""
        self._commit_pending_session()
        self._save_compact_pos()
        self.root.destroy()

    def _on_restore_main(self) -> None:
        """Toggle the full window between minimized and normal (the compact
        overlay stays up either way)."""
        if self.root.state() == "iconic":
            self.root.deiconify()
        else:
            self.root.iconify()

    def _render_compact(self, snap) -> None:
        if self._compact_win is None or not self._compact_win.winfo_exists():
            return
        b = self._baseline
        # HP potion: 初始 = confirmed start count; 變化 = bottles consumed
        # (start - end, negative). '--' until the inventory is reopened and
        # the end count is read.
        hp_used = self._session.hp_potion_consumed
        self._compact_initial["hpcount"].configure(
            text=f"{b.hp_potion:,}" if b.hp_potion is not None else "--",
        )
        self._compact_labels["hpcount"].configure(
            text=f"-{hp_used:,}" if hp_used is not None else "--",
            text_color=HP_COLOR if hp_used else INK_FAINT,
        )
        mp_used = self._session.mp_potion_consumed
        self._compact_initial["mpcount"].configure(
            text=f"{b.mp_potion:,}" if b.mp_potion is not None else "--",
        )
        self._compact_labels["mpcount"].configure(
            text=f"-{mp_used:,}" if mp_used is not None else "--",
            text_color=MP_COLOR if mp_used else INK_FAINT,
        )
        # Meso: 初始 = confirmed counter value; 變化 = net delta.
        total = self._session.total_meso
        self._compact_initial["meso"].configure(
            text=f"{b.meso:,}" if b.meso is not None else "--",
        )
        if total is not None:
            sign = "+" if total >= 0 else "-"
            self._compact_labels["meso"].configure(
                text=f"{sign}{abs(total):,}", text_color=EXP_COLOR if total >= 0 else HP_COLOR,
            )
        else:
            self._compact_labels["meso"].configure(text="--", text_color=INK_FAINT)
        # Meso cell's sub line: net income (meso − potion cost) when potion
        # prices are configured.
        if getattr(self, "_compact_meso_sub", None) is not None:
            cost = self._potion_cost()
            if self._potion_enabled() and total is not None:
                net = total - cost
                self._compact_meso_sub.configure(
                    text=self._t("compact_net", n=f"{net:+,}"),
                    text_color=OK_COLOR if net >= 0 else HP_COLOR,
                )
            else:
                self._compact_meso_sub.configure(text=" ", text_color=INK_DIM)
        # EXP: 初始 = confirmed raw (+%); 變化 = raw delta only (the % change
        # lives on the History record, per user request 2026-09-02).
        if b.exp_cur is not None:
            pct_s = f"  ({b.exp_pct:.2f}%)" if b.exp_pct is not None else ""
            self._compact_initial["exp"].configure(text=f"{b.exp_cur:,}{pct_s}")
        else:
            self._compact_initial["exp"].configure(text="--")
        exp_diff = self._session.exp_diff
        if exp_diff is not None:
            self._compact_labels["exp"].configure(
                text=f"+{exp_diff:,}", text_color=EXP_COLOR if exp_diff >= 0 else HP_COLOR,
            )
        else:
            self._compact_labels["exp"].configure(text="--")
        eta_s = self._levelup_eta_s(snap)
        if getattr(self, "_compact_eta", None) is not None:
            self._compact_eta.configure(
                text=(self._t("compact_eta") + " " + _fmt_duration(eta_s)) if eta_s is not None else "--"
            )
        if self._run_state == "stopped":
            timer_text = "--:--"
        else:
            remaining = max(0.0, self._settings.window_min * 60 - self._session.elapsed())
            timer_text = self._t("timer_left", time=f"{int(remaining // 60)}:{int(remaining % 60):02d}")
        self._compact_timer.configure(text=timer_text)

    def _rebuild_history_cards(self) -> None:
        for card in self._history_cards:
            card.destroy()
        self._history_cards.clear()
        if not self._session_history:
            # _append_history_card only ever pack_forget()s this label (on
            # the first card added) -- nothing re-packs it once the list is
            # emptied again (e.g. via Clear History), so do it explicitly.
            self._history_empty_label.pack(pady=24)
            self._update_history_summary()
            return
        # Cards are always inserted at the top (newest-first) -- rebuilding
        # oldest-first via _append_history_card reproduces the exact same
        # final order without needing separate "rebuild" layout logic.
        for index, summary in enumerate(self._session_history, start=1):
            self._append_history_card(summary, index)
        self._update_history_summary()

    def _append_history_card(self, summary: SessionSummary, index: int) -> None:
        self._history_empty_label.pack_forget()

        card = ctk.CTkFrame(self._history_frame, fg_color=SURFACE, corner_radius=10)
        # Newest-first: pack before the current top card (if any) rather than
        # appending, so the most recently finalized session is always the
        # first thing visible in the scrollable frame.
        if self._history_cards:
            card.pack(fill="x", pady=(0, 8), before=self._history_cards[0])
        else:
            card.pack(fill="x", pady=(0, 8))
        self._history_cards.insert(0, card)

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=12, pady=(10, 0))
        # Two columns: title/badges left, duration + delete button right
        # (duration sits in the top-right corner per user request 2026-08-28).
        head.grid_columnconfigure(0, weight=1)
        head.grid_columnconfigure(1, weight=0)

        head_left = ctk.CTkFrame(head, fg_color="transparent")
        head_left.grid(row=0, column=0, sticky="w")
        # Title (2026-09-02 long-list layout): the map name replaces 紀錄 when
        # one was set -- "鱷魚沼澤III #3" -- otherwise SESSION #n. A manual
        # rename (click) overrides both.
        base_title = self._t("history_session", n=index)
        if summary.map_name:
            base_title = f"{summary.map_name} #{index}"
        title_label = ctk.CTkLabel(
            head_left, text=summary.name or base_title, font=self._font(11, bold=True),
            text_color=INK, cursor="hand2",
        )
        title_label.pack(side="left")
        title_label.bind("<Button-1>", lambda _e, i=index, lbl=title_label: self._on_rename_clicked(i, lbl))

        head_right = ctk.CTkFrame(head, fg_color="transparent")
        head_right.grid(row=0, column=1, sticky="e")
        # Delete button packed first so it lands rightmost -- pack(side="right")
        # stacks from the outer edge inward in packing order.
        ctk.CTkButton(
            head_right, text="×", width=22, height=18, command=lambda i=index: self._on_delete_history_clicked(i),
            fg_color="transparent", hover_color=SURFACE_2, text_color=INK_FAINT, font=_FONT_UI_BOLD,
        ).pack(side="right")

        meta = ctk.CTkFrame(card, fg_color="transparent")
        meta.pack(fill="x", padx=12, pady=(0, 2))
        start_ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(summary.start_time))
        end_ts = time.strftime("%H:%M", time.localtime(summary.end_time))
        dur_min = summary.duration_s / 60
        unit = self._t("unit_min_short")
        ctk.CTkLabel(
            meta, text=f"{start_ts} → {end_ts}   ·   {dur_min:.1f}{unit}",
            font=_FONT_MONO_SM, text_color=INK_FAINT,
        ).pack(side="left")

        # Long-list layout (2026-09-02): labelled rows under section dividers,
        # replacing the old 2x2 mini-grid -- easier to read at a glance.
        def divider() -> None:
            ctk.CTkFrame(card, fg_color=SURFACE_2, height=1).pack(fill="x", padx=12, pady=3)

        def section(title: str) -> None:
            ctk.CTkLabel(card, text=title, font=self._font(9, bold=True),
                         text_color=INK_DIM, anchor="w").pack(fill="x", padx=12, pady=(1, 0))

        def row(label: str, value: str, color: str = INK, font=_FONT_MONO) -> None:
            r = ctk.CTkFrame(card, fg_color="transparent")
            r.pack(fill="x", padx=12, pady=0)
            r.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(r, text=label, font=self._font(10, bold=False),
                         text_color=INK_FAINT, anchor="w").grid(row=0, column=0, sticky="w")
            ctk.CTkLabel(r, text=value, font=font, text_color=color, anchor="e").grid(row=0, column=1, sticky="e")

        divider()
        # EXP block: raw gain is the headline, % change second (per request).
        section(self._t("history_exp"))
        exp_diff = summary.exp_diff
        row(self._t("history_exp_label"),
            f"{exp_diff:+,}" if exp_diff is not None else "--",
            color=EXP_COLOR if (exp_diff or 0) >= 0 else HP_COLOR,
            font=_FONT_MONO_BOLD)
        pct_diff = summary.exp_pct_diff
        row(self._t("history_exp_pct_label"),
            f"+{pct_diff:.2f}%" if pct_diff is not None else "--", color=INK_DIM)

        divider()
        # Potion consumption block.
        section(self._t("history_potions_section"))
        hp_used = summary.hp_potion_used
        mp_used = summary.mp_potion_used
        bottle = self._t("history_bottle_unit")
        def potion_s(used: int | None) -> str:
            if used is None:
                return "--"
            s = f"-{used:,}"
            return f"{s} {bottle}" if bottle else s
        row(self._t("history_hp_potion"), potion_s(hp_used),
            color=HP_COLOR if hp_used else INK_FAINT, font=self._font(11))
        row(self._t("history_mp_potion"), potion_s(mp_used),
            color=MP_COLOR if mp_used else INK_FAINT, font=self._font(11))

        divider()
        # Meso block: pure drops / item sales / potion cost (red) / total.
        section(self._t("history_meso"))
        meso_g = summary.meso_gained
        sale_m = summary.sale_meso or 0
        cost = summary.potion_cost or 0
        net_total = _history_net_total(summary)
        row(self._t("history_pure_meso"),
            f"{meso_g:+,}" if meso_g is not None else "--",
            color=EXP_COLOR if (meso_g or 0) >= 0 else HP_COLOR)
        if sale_m:
            row(self._t("history_item_sales"), f"+{sale_m:,}", color=EXP_COLOR)
        if cost:
            row(self._t("kv_potion_cost"), f"-{cost:,}", color=HP_COLOR)
        row(self._t("history_total_income"),
            f"{net_total:+,}" if net_total is not None else "--",
            color=OK_COLOR if (net_total or 0) >= 0 else HP_COLOR,
            font=_FONT_MONO_BOLD)

    @contextlib.contextmanager
    def _modal(self):
        """Run a blocking dialog. Two things have to happen around one:

        1. `_modal_open` tells _do_tick not to finalize a session while a
           dialog is up -- askstring/askyesno block on a *nested* Tk event
           loop, which does not stop self.root.after() timers from firing,
           so a session could otherwise roll over and insert a history card
           underneath the open modal mid-edit.
        2. -topmost has to come off for the duration. Tk dialogs are not
           topmost themselves, so with the HUD pinned above everything the
           dialog renders *behind* it -- while still holding a grab on all
           input. The app looks frozen (clicks on the HUD, including Restart
           Session, do nothing) with no visible cause, and stays that way
           until the invisible dialog is found and dismissed.
        """
        self._modal_open = True
        was_topmost = self._settings.topmost
        if was_topmost:
            self.root.attributes("-topmost", False)
        try:
            yield
        finally:
            self._modal_open = False
            if was_topmost:
                self.root.attributes("-topmost", True)

    def _on_rename_clicked(self, index: int, label: ctk.CTkLabel) -> None:
        # index is 1-based. session_history is no longer strictly append-only
        # (see _on_delete_history_clicked), but deleting any entry rebuilds
        # every card from scratch via _rebuild_history_cards(), so a *live*
        # card's index - 1 is always still correct: it can only go stale by
        # having its own card destroyed and recreated with the new one first.
        current = self._session_history[index - 1]
        with self._modal():
            new_name = simpledialog.askstring(
                self._t("rename_dialog_title"), self._t("rename_dialog_prompt"),
                initialvalue=current.name or self._t("history_session", n=index),
                parent=self.root,
            )
        if new_name is None:
            return  # cancelled
        new_name = new_name.strip()
        updated = dataclasses.replace(current, name=new_name or None)
        self._session_history[index - 1] = updated
        label.configure(text=updated.name or self._t("history_session", n=index))

    def _on_delete_history_clicked(self, index: int) -> None:
        summary = self._session_history[index - 1]
        with self._modal():  # see _do_tick's guard comment on _modal()
            confirmed = messagebox.askyesno(
                self._t("history_delete_confirm_title"),
                self._t(
                    "history_delete_confirm_prompt",
                    name=summary.name or self._t("history_session", n=index),
                ),
                parent=self.root,
            )
        if not confirmed:
            return
        del self._session_history[index - 1]
        # Every remaining card's 1-based index shifts once one entry is
        # removed -- rebuild from scratch rather than patching indices in
        # place, same as _on_clear_history_clicked already does.
        self._rebuild_history_cards()
        self._save_history()
        self._refresh_compare_tab()

    def _on_clear_history_clicked(self) -> None:
        if not self._session_history:
            return
        with self._modal():  # see _do_tick's guard comment on _modal()
            confirmed = messagebox.askyesno(
                self._t("history_clear_confirm_title"),
                self._t("history_clear_confirm_prompt", n=len(self._session_history)),
                parent=self.root,
            )
        if not confirmed:
            return
        self._session_history.clear()
        self._rebuild_history_cards()
        self._save_history()
        self._refresh_compare_tab()

    def _set_status_error(self, text: str) -> None:
        self._status_pill.configure(text=text, fg_color=SURFACE_2, text_color=HP_COLOR)

    # ---- utility / background checks ------------------------------------

    @staticmethod
    def _query_screen_size() -> tuple[int, int] | None:
        """Current primary-screen size in pixels, or None when unavailable."""
        try:
            if sys.platform == "win32":
                import ctypes

                return (
                    int(ctypes.windll.user32.GetSystemMetrics(0)),
                    int(ctypes.windll.user32.GetSystemMetrics(1)),
                )
            import mss

            with mss.mss() as m:
                mon = m.monitors[0]
            return (int(mon["width"]), int(mon["height"]))
        except Exception:
            return None

    def _check_screen_change(self) -> None:
        """Warn once when the screen resolution changes -- manual regions are
        stored as absolute screen pixels, so a resolution/monitor change makes
        them stale without any other signal."""
        if self._screen_warned or not self._settings.use_manual:
            return
        current = self._query_screen_size()
        if current is None or self._screen_size is None or current == self._screen_size:
            return
        self._screen_size = current
        self._screen_warned = True
        with self._modal():
            messagebox.showwarning(
                self._t("settings_manual"),
                self._t("screen_changed_prompt"),
                parent=self.root,
            )

    def _check_for_updates(self) -> None:
        """Query the GitHub API on a background thread for the latest release;
        show a hint in the dashboard when a newer version exists."""
        def _worker() -> None:
            try:
                import urllib.request

                with urllib.request.urlopen(
                    "https://api.github.com/repos/aaaagent0614-art/MsStatTractor/releases/latest",
                    timeout=6,
                ) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                latest = str(data.get("tag_name", "")).lstrip("v")
                if latest and _version_is_newer(latest, __version__):
                    self._update_available = latest
                    self.root.after(0, self._show_update_hint)
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def _show_update_hint(self) -> None:
        if self._update_hint_label is None or self._update_available is None:
            return
        self._update_hint_label.configure(
            text=self._t("update_available", ver=self._update_available)
        )
        self._update_hint_label.grid()

    def _notify_session_end(self) -> None:
        """Optional alert when a session ends (sound + brief topmost flash)."""
        if not self._settings.notify_on_stop:
            return
        try:
            if sys.platform == "win32":
                import winsound

                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            else:
                self.root.bell()
        except Exception:
            with contextlib.suppress(Exception):
                self.root.bell()

    def _update_history_summary(self) -> None:
        """Refresh the History tab's summary strip: total sessions, today's EXP,
        and the current map's average EXP/min -- plus a cleanup hint once the
        history grows large."""
        if getattr(self, "_history_summary_label", None) is None:
            return
        sessions = self._session_history
        if not sessions:
            self._history_summary_label.configure(text="")
            return
        today = time.strftime("%Y-%m-%d")
        today_exp = 0
        rates: list[float] = []
        cur_map = self._settings.map_name
        for s in sessions:
            if time.strftime("%Y-%m-%d", time.localtime(s.start_time)) == today:
                d = s.exp_diff
                if d:
                    today_exp += d
            if cur_map and s.map_name == cur_map:
                r = s.exp_per_min
                if r is not None:
                    rates.append(r)
        parts = [self._t("history_summary_count", n=len(sessions))]
        if today_exp:
            parts.append(self._t("history_summary_today", exp=f"{today_exp:,}"))
        if rates:
            avg = sum(rates) / len(rates)
            parts.append(self._t("history_summary_avg", rate=f"{avg:,.0f}"))
        text = "   ·   ".join(parts)
        if len(sessions) >= _HISTORY_CLEANUP_THRESHOLD:
            text += "\n" + self._t("history_cleanup_hint", n=len(sessions))
        self._history_summary_label.configure(text=text)

    # ---- render --------------------------------------------------------

    def _potion_enabled(self) -> bool:
        """True when the player has configured potion prices (see the
        Settings tab's 藥水成本 card). A price >0 turns a stat's cost on."""
        s = self._settings
        return s.hp_potion_price > 0 or s.mp_potion_price > 0

    def _potion_cost(self) -> int:
        """Session potion spend, from the quick-slot counter only (2026-09-03:
        HP/MP readings retired, so the loss-based estimate is gone).

        For each stat the player picked a quickbar slot for and whose counter
        was read at both session ends, cost = bottles consumed (start − end)
        × unit price. Stats without a slot picked contribute nothing."""
        s = self._settings
        hp_consumed = self._session.hp_potion_consumed if s.hp_quick_slot_index else None
        mp_consumed = self._session.mp_potion_consumed if s.mp_quick_slot_index else None
        cost = 0
        if hp_consumed is not None:
            cost += hp_consumed * s.hp_potion_price
        if mp_consumed is not None:
            cost += mp_consumed * s.mp_potion_price
        return cost

    def _levelup_eta_s(self, snap: StatSnapshot) -> float | None:
        """Seconds until level-up at the current session EXP rate. None until
        a few seconds of positive gain make the rate stable (extrapolating off
        a 1-2s sample swings wildly)."""
        total_exp = snap.exp_cur / (snap.exp_pct / 100) if snap.exp_cur and snap.exp_pct else None
        exp_diff = self._session.exp_diff
        elapsed = self._session.elapsed()
        if not (exp_diff and exp_diff > 0 and elapsed > 3 and total_exp and snap.exp_cur):
            return None
        rate_per_sec = exp_diff / elapsed
        remaining_exp = total_exp - snap.exp_cur
        if rate_per_sec <= 0:
            return None
        return remaining_exp / rate_per_sec

    def _render(self, snap: StatSnapshot) -> None:
        # Map field: when unset show an obvious "click to enter" placeholder in
        # the accent colour (distinct from the plain '--' every other field
        # shows), so it's clear this field is user-fillable -- not just another
        # unread value. The hand cursor is already bound at build time.
        if self._settings.map_name:
            self._map_value_label.configure(text=self._settings.map_name, text_color=INK)
        else:
            self._map_value_label.configure(text=self._t("kv_map_placeholder"), text_color=ACCENT)

        self._value_labels["level"].configure(text=str(snap.level) if snap.level is not None else "--")

        # HP/MP value rows were removed (2026-09-02) -- potion counts replaced
        # them. The labels no longer exist; nothing to render here.

        pct = f"  ({snap.exp_pct:.2f}%)" if snap.exp_pct is not None and self._settings.show_exp_pct else ""
        if snap.exp_cur is not None:
            # Show cumulative EXP (session start + gained) instead of the
            # per-level counter the game resets to ~0 on level-up -- a reset
            # read "0" and made the session look like it had lost its place.
            # start_exp + exp_diff is monotonic across level-ups: before one
            # it equals snap.exp_cur, after one it keeps climbing (exp_diff
            # already banks the finished level via rate.py). The percentage
            # suffix and the bar stay per-level: they describe the *current*
            # level's progress, which is exactly what resets by design.
            start_exp = self._session.start_exp
            exp_diff = self._session.exp_diff
            display_exp = (start_exp + exp_diff) if (start_exp is not None and exp_diff is not None) else snap.exp_cur
            self._value_labels["exp"].configure(text=f"{display_exp:,}{pct}")
            if snap.exp_pct is not None:
                self._bars["exp"].set(max(0.0, min(1.0, snap.exp_pct / 100)))
        else:
            self._value_labels["exp"].configure(text="--")

        start_exp = self._session.start_exp
        # Before a session has started (stopped), there is no committed start
        # value -- show the latest read instead, so a 辨識 pass has something
        # meaningful on the 起始經驗值 row (per user request 2026-08-27).
        if start_exp is None:
            start_exp = snap.exp_cur
        self._value_labels["startexp"].configure(text=f"{start_exp:,}" if start_exp is not None else "--")

        self._update_timer_label()

        exp_diff = self._session.exp_diff
        # Total EXP required for the current level isn't shown directly by the
        # game, but can be derived from any single tick that has both the
        # absolute value and percentage: total = cur / (pct/100). Anchoring
        # off the current tick (rather than diffing OCR'd percentages
        # directly) is more robust since per-level EXP totals are constant,
        # while independently-read percentages carry their own OCR noise on
        # top of the cur value's.
        total_exp = snap.exp_cur / (snap.exp_pct / 100) if snap.exp_cur and snap.exp_pct else None

        if exp_diff is not None:
            pct_s = f"  (+{exp_diff / total_exp * 100:.2f}%)" if total_exp and self._settings.show_exp_pct else ""
            self._value_labels["expdiff"].configure(text=f"+{exp_diff:,}{pct_s}")
        else:
            self._value_labels["expdiff"].configure(text="--")

        # ETA to level up: current session's EXP/sec rate, projected against
        # the EXP still needed (total - cur). Needs a few seconds of session
        # data first -- extrapolating off a 1-2 second sample swings wildly.
        eta_s = self._levelup_eta_s(snap)
        self._value_labels["eta"].configure(text=_fmt_duration(eta_s) if eta_s is not None else "--")

        # Projected session total: current rate extrapolated across the full
        # window setting, not just what's elapsed so far -- see
        # Session.projected_exp (same 3s/positive-gain guard as ETA above,
        # for the same reason).
        proj = self._session.projected_exp(self._settings.window_min * 60)
        if proj is not None:
            proj_pct_s = f"  (+{proj / total_exp * 100:.2f}%)" if total_exp and self._settings.show_exp_pct else ""
            self._value_labels["projexp"].configure(text=f"+{proj:,}{proj_pct_s}")
        else:
            self._value_labels["projexp"].configure(text="--")

        # Potion card (2026-09-03): slot positions + live potion counts.
        s = self._settings
        if s.hp_quick_slot_index:
            self._value_labels["hpslot"].configure(
                text=QUICK_SLOT_NAMES[s.hp_quick_slot_index - 1], text_color=INK,
            )
        else:
            self._value_labels["hpslot"].configure(text="--", text_color=INK)
        if s.mp_quick_slot_index:
            self._value_labels["mpslot"].configure(
                text=QUICK_SLOT_NAMES[s.mp_quick_slot_index - 1], text_color=INK,
            )
        else:
            self._value_labels["mpslot"].configure(text="--", text_color=INK)
        hp_count = self._session.hp_slot_count
        if hp_count is None:
            hp_count = self._last_hp_slot_count
        self._value_labels["hpcount"].configure(
            text=f"{hp_count:,}" if hp_count is not None else "--", text_color=INK,
        )
        mp_count = self._session.mp_slot_count
        if mp_count is None:
            mp_count = self._last_mp_slot_count
        self._value_labels["mpcount"].configure(
            text=f"{mp_count:,}" if mp_count is not None else "--", text_color=INK,
        )

        # Meso block: "起始楓幣" shows the session baseline; "當前楓幣" shows
        # the latest reading with the net delta, e.g. "155 (+55)" (spending
        # shows as a negative delta in red). Matches the EXP block's
        # start/diff split per user request 2026-08-24.
        meso_start = self._session.start_meso
        # Same as 起始經驗值: while stopped, show the latest meso reading
        # (from a 辨識 pass or the last inventory scan) instead of '--'.
        if meso_start is None:
            meso_start = self._last_meso
        meso_end = self._session.end_meso
        meso = self._session.meso_gained
        self._value_labels["mesostart"].configure(
            text=f"{meso_start:,}" if meso_start is not None else "--", text_color=INK
        )
        if meso is not None and meso_end is not None:
            sign = "+" if meso >= 0 else "-"
            self._value_labels["mesocurrent"].configure(
                text=f"{meso_end:,} ({sign}{abs(meso):,})",
                text_color=EXP_COLOR if meso >= 0 else HP_COLOR,
            )
        else:
            self._value_labels["mesocurrent"].configure(text="--", text_color=INK)

        # 賣裝收益 + 淨收益 only calculate after 記錄賣裝 (2026-09-03): before
        # that the sale revenue is unknown, so net income would be wrong.
        sale = self._session.sale_revenue
        if self._sale_done:
            self._value_labels["mesosale"].configure(
                text=f"+{sale:,}", text_color=EXP_COLOR if sale > 0 else INK_FAINT,
            )
        else:
            self._value_labels["mesosale"].configure(text="--", text_color=INK)

        # Potion cost + net income (2026-08-28): visible only when the
        # player configured potion prices on the Settings tab.
        cost = self._potion_cost()
        if self._potion_enabled():
            self._value_labels["potioncost"].configure(
                text=f"-{cost:,}" if cost > 0 else "0",
                text_color=HP_COLOR if cost > 0 else INK_FAINT,
            )
            if self._sale_done:
                total = self._session.total_meso
                if total is not None:
                    net = total - cost
                    self._value_labels["netmeso"].configure(
                        text=f"{net:+,}",
                        text_color=OK_COLOR if net >= 0 else HP_COLOR,
                    )
                else:
                    self._value_labels["netmeso"].configure(text="--", text_color=INK)
            else:
                self._value_labels["netmeso"].configure(text="--", text_color=INK)

        # Pause/stop/calibration are user- or engine-driven states that take
        # priority over the activity-based idle/tracking read below -- e.g. a
        # paused session with real HP/MP/EXP movement in its history isn't
        # "Idle", it's "Paused". A timed manual-detection result (see
        # _run_manual_detection) overrides all of these for a few seconds.
        if time.time() < getattr(self, "_detect_result_until", 0.0):
            self._status_pill.configure(
                text=self._detect_result_text, fg_color=SURFACE_2,
                text_color=OK_COLOR if self._detect_result_ok else HP_COLOR,
            )
        elif self._run_state == "paused":
            self._status_pill.configure(text=self._t("status_paused"), fg_color=SURFACE_2, text_color=EXP_COLOR)
        elif self._run_state == "stopped":
            self._status_pill.configure(text=self._t("status_stopped"), fg_color=SURFACE_2, text_color=INK_DIM)
        elif self._session.is_calibrating:
            self._status_pill.configure(text=self._t("status_calibrating"), fg_color=SURFACE_2, text_color=EXP_COLOR)
        else:
            # Idle when EXP hasn't moved in this session (HP/MP aren't tracked
            # anymore -- 2026-09-02 -- so EXP is the only activity signal).
            idle = (exp_diff or 0) == 0
            if idle:
                self._status_pill.configure(text=self._t("status_idle"), fg_color=SURFACE_2, text_color=INK_DIM)
            else:
                self._status_pill.configure(text=self._t("status_tracking"), fg_color=TRACK_BG, text_color=OK_COLOR)
        # Compatibility-capture warning: WGC is the occlusion-proof path; if
        # the last grab degraded to PrintWindow/mss, say so (manual mode has no
        # capture_mode, defaulting to "wgc" = no warning -- the user chose
        # screen-region capture there deliberately).
        if getattr(self, "_compat_hint_label", None) is not None:
            mode = getattr(self._active_source(), "capture_mode", "wgc")
            if mode != "wgc":
                self._compat_hint_label.configure(text=self._t("compat_mode_hint"))
                self._compat_hint_label.grid()
            else:
                self._compat_hint_label.grid_remove()
        self._render_compact(snap)

    def run(self) -> None:
        self.root.mainloop()


class _StartBaselineDialog:
    """Pre-start confirmation (2026-09-02). Shows every value that will become
    the new session's baseline -- EXP/HP/MP from the live OCR stream, meso and
    quick-slot potion counts read while the inventory was open. The player can
    confirm, press 重新偵測 (e.g. they just opened the inventory), or click a
    value to type a manual correction for an OCR misread. `result` is a
    _Baseline on confirm, None on cancel."""

    # (baseline key, i18n label key, app attribute for the auto value)
    _ROWS = [
        ("exp", "start_confirm_exp", "_last", "exp_cur"),
        ("meso", "start_confirm_meso", None, "_last_meso"),
        ("hp_potion", "start_confirm_hp_potion", None, "_last_hp_slot_count"),
        ("mp_potion", "start_confirm_mp_potion", None, "_last_mp_slot_count"),
    ]

    def __init__(self, app: "OverlayApp"):
        self.app = app
        self.result: _Baseline | None = None
        self._draft: dict[str, int] = {}  # manual edits, by row key

        self.top = ctk.CTkToplevel(app.root)
        self.top.title("MsStatTractor")
        self.top.attributes("-topmost", True)
        self.top.configure(fg_color=BG)
        # Tall enough that the button row keeps full size, centred on the main
        # window (dual-monitor: opens on the screen the user is actually on).
        self.top.geometry(app._centered_on_main(380, 460))
        self.top.resizable(False, False)
        self.top.transient(app.root)
        self.top.protocol("WM_DELETE_WINDOW", self._cancel)

        pad = ctk.CTkFrame(self.top, fg_color=BG)
        pad.pack(fill="both", expand=True, padx=10, pady=10)
        ctk.CTkLabel(pad, text=app._t("start_confirm_title"), font=app._font(14, bold=True),
                     text_color=INK, anchor="w").pack(fill="x")
        ctk.CTkLabel(pad, text=app._t("start_confirm_hint"), font=app._font(10, bold=False),
                     text_color=INK_DIM, anchor="w", justify="left",
                     wraplength=320).pack(fill="x", pady=(2, 8))

        self._value_labels: dict[str, ctk.CTkLabel] = {}
        for key, label_key, _src_obj, _src_attr in self._ROWS:
            row = ctk.CTkFrame(pad, fg_color="transparent")
            row.pack(fill="x", pady=1)
            ctk.CTkLabel(row, text=app._t(label_key), font=app._font(11, bold=False),
                         text_color=INK_DIM, anchor="w").pack(side="left")
            val = ctk.CTkLabel(row, text="--", font=_FONT_MONO, text_color=INK,
                               anchor="e", cursor="hand2")
            val.pack(side="right")
            val.bind("<Button-1>", lambda _e, k=key: self._edit(k))
            self._value_labels[key] = val

        btns = ctk.CTkFrame(pad, fg_color="transparent")
        btns.pack(fill="x", pady=(10, 0))
        # Grid 3 equal columns instead of pack(side=left): CTkButton's default
        # min width (140, *1.2 scale = 168px) made three packed buttons total
        # ~504px > the ~356px row, so the last one got squeezed to 12px and
        # looked missing (reported 2026-09-03). grid + sticky="ew" stretches
        # each button to its column regardless of min width.
        for c in range(3):
            btns.grid_columnconfigure(c, weight=1, uniform="btns")
        def mk(text_key, command, color=INK, fg=SURFACE_2, hover=TRACK_BG):
            return ctk.CTkButton(
                btns, text=app._t(text_key), command=command,
                fg_color=fg, hover_color=hover, text_color=color,
                height=32, font=app._font(12, bold=True),
                # Explicit width ~96 (scaled ~115px) beats the 140 default so
                # three columns of ~116px actually fit the row (see above).
                width=96,
            )
        mk("start_confirm_redetect", self._redetect).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        mk("cancel", self._cancel, color=INK_DIM).grid(row=0, column=1, sticky="ew", padx=(0, 4))
        mk("start_confirm_start", self._confirm, color=ACCENT_INK, fg=ACCENT, hover="#7ff2e0").grid(row=0, column=2, sticky="ew")

        self._refresh()

    # ---- values ---------------------------------------------------------

    def _auto_value(self, key: str):
        """Current app reading for a row (draft overrides it)."""
        if key in self._draft:
            return self._draft[key]
        for k, _lk, obj, attr in self._ROWS:
            if k == key:
                src = self.app if obj is None else getattr(self.app, obj)
                return getattr(src, attr, None)
        return None

    def _refresh(self) -> None:
        app = self.app
        missing = app._t("start_confirm_missing")
        for key, _lk, _o, _a in self._ROWS:
            v = self._auto_value(key)
            lbl = self._value_labels[key]
            if v is None:
                lbl.configure(text=missing, text_color=INK_FAINT)
                continue
            if key == "exp":
                pct = self.app._last.exp_pct
                pct_s = f"  ({pct:.2f}%)" if pct is not None else ""
                lbl.configure(text=f"{v:,}{pct_s}", text_color=EXP_COLOR)
            elif key == "meso":
                lbl.configure(text=f"{v:,}", text_color=EXP_COLOR)
            else:
                lbl.configure(text=f"{v:,}", text_color=INK)
        self.top.update_idletasks()

    def _edit(self, key: str) -> None:
        app = self.app
        current = self._auto_value(key)
        with app._modal():
            answer = simpledialog.askstring(
                app._t("start_confirm_edit_title"),
                app._t("start_confirm_edit_prompt"),
                initialvalue=f"{current:,}" if current is not None else "",
                parent=self.top,
            )
        if answer is None:
            return
        answer = answer.strip().replace(",", "")
        if answer == "":
            self._draft.pop(key, None)  # revert to auto
        else:
            try:
                self._draft[key] = int(answer)
            except ValueError:
                return
        self._refresh()

    def _redetect(self) -> None:
        app = self.app
        app._on_detect_clicked()  # runs the mode-appropriate 辨識 pass
        self._draft.clear()
        self._refresh()

    def _confirm(self) -> None:
        app = self.app
        last = app._last
        b = _Baseline(
            exp_cur=self._draft.get("exp", last.exp_cur),
            exp_pct=last.exp_pct,
            level=last.level,
            meso=self._draft.get("meso", app._last_meso),
            hp_potion=self._draft.get("hp_potion", app._last_hp_slot_count),
            mp_potion=self._draft.get("mp_potion", app._last_mp_slot_count),
        )
        self.result = b
        self.top.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.top.destroy()
