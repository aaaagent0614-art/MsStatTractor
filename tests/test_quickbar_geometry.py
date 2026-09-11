"""Where the quickbar is after the 2026-09-10 patch, and whether we can read it.

The patch made the bottom status strip a fixed pixel width, horizontally
CENTRED. The quickbar hugs that strip's right end, so its position as a
fraction of the frame moves with the client width -- the old fixed-fraction
crop (QUICK_BAR_FRAC) pointed outside the quickbar on the native 1366 client
and on a 1.875x-magnified frame, returning {} for the potion counts.

The replacement (regions.quick_bar_box_from_stat) measures the frame's UI scale
from the detected LV/EXP boxes and derives the quickbar from the strip's left
edge, so it works on a native client of any width AND under a screen magnifier
-- the two cases a fixed fraction cannot tell apart. These tests run it end to
end on the three real post-patch screenshots.
"""
from __future__ import annotations

import pytest
from PIL import Image

from conftest import SAMPLE_IMAGE, SAMPLE_IMAGE_1920, SAMPLE_IMAGE_2560
from maple_analyzer.ocr import StatPanelOcr
from maple_analyzer.overlay import (
    _divide_quick_counts,
    _quick_bar_detect_size,
    _quickbar_slots_from_boxes,
    _stat_ui_scale,
)
from maple_analyzer.parser import find_stat_fields
from maple_analyzer.regions import quick_bar_box_from_stat

SAMPLES = [SAMPLE_IMAGE, SAMPLE_IMAGE_1920, SAMPLE_IMAGE_2560]
SAMPLE_IDS = ["1366", "1920", "2560"]


@pytest.fixture(scope="module")
def ocr_engine():
    return StatPanelOcr()


def _ui_scale(image: Image.Image, found: dict) -> float:
    """The frame's UI scale, as the overlay measures it for the quickbar."""
    w, h = image.size
    return _stat_ui_scale(
        {n: (b[0] / w, b[1] / h, b[2] / w, b[3] / h) for n, b in found.items()}, w
    )


def _read_slots(
    image: Image.Image, box, ocr: StatPanelOcr, ui_scale: float = 1.0
) -> dict[int, int]:
    """Mirror of OverlayApp._read_slot_counts' crop + scaling behaviour."""
    crop = image.crop(box)
    tw, th, total = _quick_bar_detect_size(crop.size, ui_scale)
    det = crop.resize((tw, th), Image.Resampling.LANCZOS) if (tw, th) != crop.size else crop
    boxes = [
        (int(x / total), int(y / total), int(w / total), int(h / total), t)
        for x, y, w, h, t in ocr.detect_text(det)
    ]
    return _quickbar_slots_from_boxes(
        boxes, int(round(det.width / total)), int(round(det.height / total))
    )


@pytest.mark.parametrize("path", SAMPLES, ids=SAMPLE_IDS)
def test_derived_box_reads_the_potion_count(ocr_engine, path):
    image = Image.open(path).convert("RGB")
    found = find_stat_fields(ocr_engine.detect_text(image))
    assert {"LV", "EXP"} <= set(found), found
    lv, ex = found["LV"], found["EXP"]
    box = quick_bar_box_from_stat(lv[0], ex[0] + ex[2], image.size)
    assert box is not None
    slots = _read_slots(image, box, ocr_engine, _ui_scale(image, found))
    # PgDn (slot 8) holds the 1343-count potion stack on all three samples.
    assert slots.get(8) == 1343, slots


@pytest.mark.parametrize("path", SAMPLES, ids=SAMPLE_IDS)
def test_derived_box_is_inside_the_frame(ocr_engine, path):
    image = Image.open(path).convert("RGB")
    found = find_stat_fields(ocr_engine.detect_text(image))
    lv, ex = found["LV"], found["EXP"]
    box = quick_bar_box_from_stat(lv[0], ex[0] + ex[2], image.size)
    assert box is not None
    left, top, right, bottom = box
    w, h = image.size
    assert 0 <= left < right <= w
    assert 0 <= top < bottom <= h
    # Always in the bottom-right quadrant, never larger than a third of the frame.
    assert top > h * 0.6 and left > w * 0.6
    assert (right - left) < w / 3, (right - left, w)


def test_needs_both_stat_anchors():
    assert quick_bar_box_from_stat(100.0, 100.0, (1920, 1080)) is None
    assert quick_bar_box_from_stat(100.0, 90.0, (1920, 1080)) is None


def test_manual_mode_can_measure_above_the_marked_strip():
    """Manual mode passes the marked strip's bottom screen edge as the frame
    bottom and disables clamping -- the quickbar sits ABOVE the strip, outside
    the marked region, so clamping would fold it back onto the strip."""
    box = quick_bar_box_from_stat(
        100.0, 1007.0, (800, 60), bottom_y=1000.0, clamp=False
    )
    assert box is not None
    left, top, right, bottom = box
    assert top < 1000 and bottom < 1000, "quickbar must land above the strip"
    assert left > 100 and right > left


def test_equal_division_keeps_counts_when_no_key_label_anchors_them():
    """A crop aligned to the slot grid still gets its counts when only a
    bottom-row key label (or none) was readable -- previously that returned {}."""
    digs = [("108", 106, 19, 28, 12), ("1343", 106, 52, 29, 12)]
    slots = _divide_quick_counts(digs, 151, 80)
    assert slots == {4: 108, 8: 1343}, slots


# ---- detector input scaling (2026-09-11) ---------------------------------
# The quickbar crop grows with the UI, and the detector's sweet spot is the
# reference-1366 pixel size. Upscaling an already-magnified crop 3x is what
# broke the potion counts at max resolution: on Alex's 1.87x frame the shipped
# path returned {} / {8: 126} while the same crop normalised to 151x80 and then
# upscaled 3x read {4: 64, 8: 196} correctly.

_LV_REF = (10, 734, 70, 24)       # FIELD_BOXES LV, as (x, y, w, h) at 1366x768
_EXP_REF = (807, 729, 110, 13)    # FIELD_BOXES EXP, same reference frame
# (regions.FIELD_BOXES stores (left, top, right, bottom) -- 807..917 for EXP --
# while the located boxes the overlay measures are (x, y, w, h).)


def _fracs(frame_w: int, frame_h: int) -> dict:
    return {
        name: (b[0] / frame_w, b[1] / frame_h, b[2] / frame_w, b[3] / frame_h)
        for name, b in (("LV", _LV_REF), ("EXP", _EXP_REF))
    }


def test_ui_scale_is_measured_from_the_stat_span():
    assert _stat_ui_scale(_fracs(1366, 768), 1366) == pytest.approx(1.0, abs=0.01)
    # Same layout on a 2559-wide frame: the UI is 1.87x the reference pixels
    # (this is the measured max-resolution case). The spans are identical as
    # FRACTIONS, so the pixel width is what tells the two apart.
    assert _stat_ui_scale(_fracs(1366, 768), 2559) == pytest.approx(1.874, abs=0.01)
    assert _stat_ui_scale(None, 1366) == 1.0
    assert _stat_ui_scale({"LV": _fracs(1366, 768)["LV"]}, 1366) == 1.0


def test_detect_size_normalises_a_magnified_crop():
    """The 1.87x crop is brought back to reference pixels before the 3x
    upscale: 282x149 -> 151x80 -> 453x240, with the detection coordinates
    divided by 1.605 to land back in the normalised crop's frame."""
    tw, th, total = _quick_bar_detect_size((282, 149), 1.8688)
    assert (tw, th) == (453, 240)
    assert total == pytest.approx(3 / 1.8688)


def test_detect_size_leaves_a_reference_crop_alone():
    """No magnifier, no change: the reference crop is upscaled exactly as
    before (the behaviour the three patched samples were tuned on)."""
    assert _quick_bar_detect_size((151, 80), 1.0) == (453, 240, 3.0)


def test_detect_size_does_not_upscale_a_big_crop():
    tw, th, total = _quick_bar_detect_size((900, 400), 1.0)
    assert (tw, th) == (900, 400)
    assert total == 1.0
