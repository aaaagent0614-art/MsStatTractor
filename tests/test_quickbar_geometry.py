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
    _quickbar_slots_from_boxes,
)
from maple_analyzer.parser import find_stat_fields
from maple_analyzer.regions import quick_bar_box_from_stat

SAMPLES = [SAMPLE_IMAGE, SAMPLE_IMAGE_1920, SAMPLE_IMAGE_2560]
SAMPLE_IDS = ["1366", "1920", "2560"]


@pytest.fixture(scope="module")
def ocr_engine():
    return StatPanelOcr()


def _read_slots(image: Image.Image, box, ocr: StatPanelOcr) -> dict[int, int]:
    """Mirror of OverlayApp._read_slot_counts' crop + upscale behaviour."""
    crop = image.crop(box)
    scale = 3 if min(crop.size) < 300 else 1
    if scale > 1:
        crop = crop.resize(
            (crop.width * scale, crop.height * scale), Image.Resampling.LANCZOS
        )
    boxes = [
        (x // scale, y // scale, w // scale, h // scale, t)
        for x, y, w, h, t in ocr.detect_text(crop)
    ]
    return _quickbar_slots_from_boxes(boxes, crop.width // scale, crop.height // scale)


@pytest.mark.parametrize("path", SAMPLES, ids=SAMPLE_IDS)
def test_derived_box_reads_the_potion_count(ocr_engine, path):
    image = Image.open(path).convert("RGB")
    found = find_stat_fields(ocr_engine.detect_text(image))
    assert {"LV", "EXP"} <= set(found), found
    lv, ex = found["LV"], found["EXP"]
    box = quick_bar_box_from_stat(lv[0], ex[0] + ex[2], image.size)
    assert box is not None
    slots = _read_slots(image, box, ocr_engine)
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
