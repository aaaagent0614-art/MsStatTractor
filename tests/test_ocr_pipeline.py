"""End-to-end golden-value regression: real capture crop -> real RapidOCR ->
real parser, against a real screenshot in samples/. This is the only test that
exercises actual OCR inference (slow-ish, one-time model load) -- it exists to
catch regressions in FIELD_BOXES, the OCR wrapper, or the parser regexes that
unit tests on synthetic strings wouldn't catch, since those don't touch real
recognition output.

Ground truth re-verified 2026-09-10 against
samples/maple_story_ui_patched_1366x768.png (LV.47, HP[629/812],
MP[3024/3024], EXP 456903[82.22%]) -- the reference sample was replaced after
the game patched its HUD, since the old 1351x800 sample describes a layout the
game no longer renders.

The pipeline is brittle to crop padding here: recognition-only OCR on a
slightly-too-generous box drops digits and separators ('LV. 47' -> 'LV. @7',
'EXP 456903[82.22%]' -> 'EXP 456903[82.22X]'), which parses to WRONG VALUES
rather than failing loudly. That is why the boxes in regions.FIELD_BOXES are
tight and why ocr.read_field upscales small crops -- both are load-bearing.
"""
import pytest

from maple_analyzer.capture import StaticImageCapture
from maple_analyzer.ocr import StatPanelOcr
from maple_analyzer.parser import parse_fields

from conftest import SAMPLE_IMAGE


@pytest.fixture(scope="module")
def ocr_engine():
    return StatPanelOcr()  # loads the ONNX model once, reused across tests in this file


@pytest.fixture(scope="module")
def snapshot(ocr_engine):
    capture = StaticImageCapture(SAMPLE_IMAGE)
    fields = capture.grab_fields()
    field_text = {name: ocr_engine.read_field(img) for name, img in fields.items()}
    return parse_fields(field_text), field_text


def test_level(snapshot):
    snap, text = snapshot
    assert snap.level == 47, text


def test_hp(snapshot):
    snap, text = snapshot
    assert (snap.hp_cur, snap.hp_max) == (629, 812), text


def test_mp(snapshot):
    snap, text = snapshot
    assert (snap.mp_cur, snap.mp_max) == (3024, 3024), text


def test_exp(snapshot):
    snap, text = snapshot
    assert snap.exp_cur == 456903, text
    assert snap.exp_pct == 82.22, text


def test_field_crops_are_nonempty(ocr_engine):
    capture = StaticImageCapture(SAMPLE_IMAGE)
    fields = capture.grab_fields()
    assert set(fields.keys()) == {"LV", "HP", "MP", "EXP"}
    for name, img in fields.items():
        assert img.width > 0 and img.height > 0, f"{name} crop is empty"
