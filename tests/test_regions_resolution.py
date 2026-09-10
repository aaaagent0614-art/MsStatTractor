"""Can the crops survive a different resolution and aspect ratio?

Before the 2026-09-10 patch this was a straight question about regions.py's
linear scale_box(): the game scaled its HUD with the client, so boxes measured
at one size scaled to any other. The patch broke that premise -- the bottom
strip is now a FIXED pixel width, horizontally centred, so a linearly scaled
box lands further off the wider the client gets.

That leaves two paths, and this file measures both against real screenshots
(resizing a screenshot proves nothing -- the game re-renders its UI, it does
not rescale a bitmap):

  1. detection (find_stat_fields) -- resolution-independent, the path the HUD
     actually relies on;
  2. scale_box() -- still correct in exactly one case, when the frame is the
     native 1366x768 client *magnified as a whole* (Magpie-style magnifiers,
     which is how the reported setup runs). The 2560x1440 sample is such a
     frame (1366x768 x 1.875), so it pins that case.
"""
from __future__ import annotations

import pytest
from PIL import Image

from conftest import SAMPLE_IMAGE_1920, SAMPLE_IMAGE_2560
from maple_analyzer.ocr import StatPanelOcr
from maple_analyzer.parser import find_stat_fields, parse_fields
from maple_analyzer.regions import (
    FIELD_BOXES,
    REFERENCE_CLIENT_SIZE,
    STAT_PANEL_BOX,
    scale_box,
)

# Ground truth read off the patched samples: all of 1366x768, 1920x1080 and
# 2560x1440 (magnified) show the same values.
TRUTH = {
    "level": 47,
    "hp": (629, 812),
    "mp": (3024, 3024),
    "exp_cur": 456903,
    "exp_pct": 82.22,
}

CLIENT_SIZES = [(1366, 768), (1920, 1080), (2560, 1440), (1280, 720)]


@pytest.fixture(scope="module")
def ocr_engine():
    return StatPanelOcr()


def _read_fields(image: Image.Image, boxes: dict, ocr: StatPanelOcr):
    text = {
        name: ocr.read_field(image.crop(tuple(box)))
        for name, box in boxes.items()
    }
    return parse_fields(text), text


def test_reference_size_is_the_identity_case():
    for box in list(FIELD_BOXES.values()) + [STAT_PANEL_BOX]:
        assert scale_box(box, REFERENCE_CLIENT_SIZE).as_tuple() == box


@pytest.mark.parametrize("client", CLIENT_SIZES)
def test_field_boxes_stay_inside_the_panel_box(client):
    """Catches a scaling regression at any resolution without needing a
    screenshot for each one."""
    panel = scale_box(STAT_PANEL_BOX, client)
    for name, raw in FIELD_BOXES.items():
        box = scale_box(raw, client)
        assert panel.left <= box.left < box.right <= panel.right, name
        assert panel.top <= box.top < box.bottom <= panel.bottom, name
        assert box.right - box.left >= 40, f"{name} too narrow at {client}"
        assert box.bottom - box.top >= 10, f"{name} too short at {client}"


@pytest.mark.parametrize("client", CLIENT_SIZES)
def test_panel_box_stays_inside_the_client(client):
    panel = scale_box(STAT_PANEL_BOX, client)
    assert 0 <= panel.left < panel.right <= client[0]
    assert 0 <= panel.top < panel.bottom <= client[1]


@pytest.mark.parametrize(
    "path", [SAMPLE_IMAGE_1920, SAMPLE_IMAGE_2560], ids=["1920", "2560"]
)
def test_detection_locates_all_four_fields(ocr_engine, path):
    """The path the patched HUD depends on: a full-frame detection pass finds
    every field on native clients of any width."""
    image = Image.open(path).convert("RGB")
    found = find_stat_fields(ocr_engine.detect_text(image))
    assert set(found) == {"LV", "HP", "MP", "EXP"}, found


@pytest.mark.parametrize(
    "path", [SAMPLE_IMAGE_1920, SAMPLE_IMAGE_2560], ids=["1920", "2560"]
)
def test_detected_fields_parse_to_the_right_values(ocr_engine, path):
    """Recognition on the detected boxes -- crop is the box verbatim (no
    padding; padding is what corrupts the digits, see the tick path)."""
    image = Image.open(path).convert("RGB")
    found = find_stat_fields(ocr_engine.detect_text(image))
    boxes = {
        name: (x, y, x + w, y + h) for name, (x, y, w, h) in found.items()
    }
    snap, text = _read_fields(image, boxes, ocr_engine)
    assert snap.level == TRUTH["level"], text
    assert (snap.hp_cur, snap.hp_max) == TRUTH["hp"], text
    assert (snap.mp_cur, snap.mp_max) == TRUTH["mp"], text
    assert snap.exp_cur == TRUTH["exp_cur"], text
    assert snap.exp_pct == TRUTH["exp_pct"], text


def test_scale_box_tracks_a_magnified_whole_frame(ocr_engine):
    """The one case scale_box() is still right for: the 2560x1440 sample is
    the native 1366x768 client magnified 1.875x (Magpie), so linear scaling
    lands the reference boxes on the real text. Asserted as geometry against
    the detection result, not by OCR -- the scaled boxes sit within a couple of
    pixels of the detected text, which is close enough to be the right region
    but not padding-free, and padding is what corrupts a recognition read
    (see regions.FIELD_BOXES). That is exactly why detection supersedes it."""
    image = Image.open(SAMPLE_IMAGE_2560).convert("RGB")
    found = find_stat_fields(ocr_engine.detect_text(image))
    for name, box in FIELD_BOXES.items():
        left, top, _r, _b = scale_box(box, image.size).as_tuple()
        x, y, _w, _h = found[name]
        assert abs(left - x) <= 10, f"{name}: scaled left {left} vs detected {x}"
        assert abs(top - y) <= 10, f"{name}: scaled top {top} vs detected {y}"
