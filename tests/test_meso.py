"""Meso counter selection tests.

The meso counter lives in the draggable inventory window, so its position
isn't known in advance. The pipeline finds it by running full-frame
*detection* OCR and picking the largest pure-digit text blob that isn't the
bottom stat-panel strip (parser.find_meso_from_boxes). These tests cover
the selection logic directly with synthetic boxes, plus an end-to-end run
through the real OCR engine on a synthetic frame drawn like the actual
game screenshot (white digits on dark background -- the counter is NOT
gold in the real client).
"""
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from maple_analyzer.parser import (
    _GOLD_MIN_PX,
    _count_gold_left_of,
    find_meso_candidate,
    find_meso_candidate_verified,
    find_meso_from_boxes,
    find_meso_in_region,
    find_stat_fields,
)

_DEJAVU = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
_BG = (10, 12, 18)
_WHITE = (255, 255, 255)

pytestmark = pytest.mark.skipif(
    not _DEJAVU.exists(), reason="DejaVu font not available for synthetic frames"
)


# ---- pure selection logic ------------------------------------------------

def test_picks_largest_digit_blob():
    boxes = [
        (100, 100, 75, 21, "154,821"),   # the meso counter
        (50, 50, 43, 18, "320"),         # an item stack count
        (10, 10, 37, 23, "32"),          # the LV value
    ]
    assert find_meso_from_boxes(boxes, (1351, 800)) == 154_821


def test_excludes_stat_panel_strip():
    """LV/HP/MP/EXP live at the bottom of the client -- their LV value is
    also pure digits and must never be picked as meso."""
    boxes = [
        (488, 770, 37, 23, "32"),        # LV, inside the bottom strip
        (1615, 596, 75, 21, "154,821"),  # meso, well above it
    ]
    assert find_meso_from_boxes(boxes, (2045, 1151)) == 154_821


def test_tie_break_lower_blob_wins():
    """Item counts can tie the meso on digit count -- the counter sits at
    the bottom edge of the inventory window, below the item grid."""
    boxes = [
        (100, 100, 43, 18, "850"),   # item stack count (higher on screen)
        (100, 300, 43, 18, "750"),   # meso (lower on screen)
    ]
    assert find_meso_from_boxes(boxes, (1351, 800)) == 750


def test_non_digit_boxes_ignored():
    boxes = [
        (100, 100, 80, 21, "楓幣點數"),
        (100, 200, 80, 21, "獲得 5 楓幣"),
        (100, 300, 75, 21, "154,821"),
    ]
    assert find_meso_from_boxes(boxes, (1351, 800)) == 154_821


def test_no_digit_blobs_returns_none():
    assert find_meso_from_boxes([(10, 10, 50, 20, "ITEMINVENTORY")], (1351, 800)) is None
    assert find_meso_from_boxes([], (1351, 800)) is None


def test_only_stat_strip_digits_returns_none():
    # LV at its real screenshot position: y=1116 in a 1151-high client.
    boxes = [(488, 1116, 37, 23, "32")]
    assert find_meso_from_boxes(boxes, (2045, 1151)) is None


def test_candidate_returns_box_and_value():
    """find_meso_candidate returns the detected box too, so the overlay can
    cache the position and re-read it with cheap recognition-only OCR."""
    boxes = [
        (100, 100, 75, 21, "154,821"),
        (50, 50, 43, 18, "320"),
    ]
    found = find_meso_candidate(boxes, (1351, 800))
    assert found is not None
    x, y, w, h, value = found
    assert value == 154_821
    assert (x, y, w, h) == (100, 100, 75, 21)


def test_absurd_digit_blob_rejected():
    boxes = [(100, 100, 200, 21, "99999999999999999999")]
    assert find_meso_from_boxes(boxes, (1351, 800)) is None


# ---- stat panel location (find_stat_fields) ------------------------------

def test_find_stat_fields_matches_panel_text():
    """Detection boxes from a real-style frame: the four panel fields plus
    the meso counter. Only the panel fields must be located."""
    boxes = [
        (436, 1115, 89, 24, "LV. 32"),
        (750, 1101, 108, 24, "HP[602/602]"),
        (913, 1101, 129, 24, "MP[2100/2100]"),
        (1083, 1103, 148, 21, "EXP38829[3183%]"),
        (1615, 596, 75, 21, "154,821"),
    ]
    found = find_stat_fields(boxes)
    assert set(found) == {"LV", "HP", "MP", "EXP"}
    assert found["LV"] == (436, 1115, 89, 24)
    assert found["EXP"] == (1083, 1103, 148, 21)


def test_find_stat_fields_ignores_unrelated_text():
    boxes = [
        (1509, 144, 122, 18, "ITEMINVENTORY"),
        (1694, 626, 71, 22, "楓幣點數"),
        (1200, 262, 131, 27, "MapleStory"),
    ]
    assert find_stat_fields(boxes) == {}


def test_find_stat_fields_partial_panel():
    """If some fields are obscured, the visible ones are still located."""
    boxes = [(750, 1101, 108, 24, "HP[602/602]")]
    assert find_stat_fields(boxes) == {"HP": (750, 1101, 108, 24)}


def test_find_stat_fields_merges_split_lv():
    """Detection splits 'LV. 32' into 'LV.' + '32' boxes on some frames --
    the label must be merged with the digit box to its right."""
    boxes = [
        (79, 831, 41, 27, "LV."),
        (131, 833, 39, 23, "32"),
        (394, 821, 104, 20, "HP[602/602]"),
    ]
    found = find_stat_fields(boxes)
    assert "LV" in found
    x, y, w, h = found["LV"]
    # Merged box spans both the label and the digits.
    assert x <= 79 and x + w >= 170
    assert y <= 831 and y + h >= 858


def test_find_stat_fields_lv_label_only_fallback():
    """Detection reads a bare 'LV.' label with NO digit box at all (seen on
    the native 1366x768 client, 2026-09-06) -- the label box is widened to
    the right so the tick's recognition OCR still covers the level digits."""
    boxes = [
        (288, 777, 43, 21, "LV."),
        (721, 770, 106, 16, "EXP 153914[35 94%]"),
    ]
    found = find_stat_fields(boxes)
    assert "LV" in found
    x, y, w, h = found["LV"]
    # Label box widened rightward to swallow the digits.
    assert (x, y, h) == (288, 777, 21)
    assert w > 100
    assert "EXP" in found


# ---- end-to-end through the real OCR engine ------------------------------

def _game_like_frame():
    """White digits on dark background, laid out like the user's real
    screenshot: meso '154,821' top-right inside the inventory, item count
    '320' above it, LV '32' at the bottom strip."""
    img = Image.new("RGB", (2045, 1151), _BG)
    draw = ImageDraw.Draw(img)
    font_big = ImageFont.truetype(str(_DEJAVU), 22)
    font_small = ImageFont.truetype(str(_DEJAVU), 16)
    draw.text((1615, 596), "154,821", font=font_big, fill=_WHITE)
    draw.text((1707, 261), "320", font=font_small, fill=_WHITE)
    draw.text((488, 1116), "32", font=font_small, fill=_WHITE)
    return img


def test_ocr_roundtrip_reads_the_meso_number():
    """Full chain against the real OCR engine (detection over the whole
    frame, exactly like overlay._try_read_meso): the meso value is found
    even though the item count and the LV are also pure digits."""
    from maple_analyzer.ocr import StatPanelOcr

    frame = _game_like_frame()
    boxes = StatPanelOcr().detect_text(frame)
    assert find_meso_from_boxes(boxes, frame.size) == 154_821


def test_find_meso_in_region_picks_largest_digit_blob():
    """Within a user-marked meso region, the counter is the biggest pure-digit
    blob; smaller item stack counts must not win."""
    boxes = [
        (5, 10, 30, 12, "12"),            # item stack count
        (60, 8, 90, 16, "154,821"),       # the meso counter
        (200, 20, 20, 10, "3"),           # another stack count
    ]
    assert find_meso_in_region(boxes) == (60, 8, 90, 16, 154_821)


def test_find_meso_in_region_has_no_bottom_strip_filter():
    """Unlike find_meso_candidate, a digit blob near the bottom of the marked
    region is still eligible -- the region is already scoped to the counter."""
    # The counter sits in the last few rows of the marked region.
    boxes = [(10, 55, 60, 20, "99,999")]
    assert find_meso_in_region(boxes) == (10, 55, 60, 20, 99_999)


def test_find_meso_in_region_ignores_non_digit_text():
    boxes = [(0, 0, 40, 12, "楓幣"), (50, 0, 40, 12, "MESO")]
    assert find_meso_in_region(boxes) is None


def test_find_meso_in_region_returns_none_when_empty():
    assert find_meso_in_region([]) is None


# ---- coin-icon verification (method A, 2026-09-06) ------------------------

_COIN = (230, 180, 50)  # golden-yellow, inside the _count_gold_left_of mask


def _frame_with_coin(coin_x: int = 60, coin_y: int = 60) -> Image.Image:
    """Dark frame with a drawn golden coin at the given position."""
    img = Image.new("RGB", (600, 200), _BG)
    d = ImageDraw.Draw(img)
    d.ellipse((coin_x, coin_y, coin_x + 30, coin_y + 30), fill=_COIN)
    return img


def test_gold_left_count():
    img = _frame_with_coin(coin_x=60, coin_y=60)  # coin spans x 60-90, y 60-90
    # Box at x=150: lookbehind window is x 56..146, y 48..113 -> covers the coin.
    assert _count_gold_left_of(img, 150, 60, 75, 21) >= _GOLD_MIN_PX
    # Box far to the right: window never reaches the coin.
    assert _count_gold_left_of(img, 500, 60, 75, 21) == 0


def test_gold_verify_rejects_number_without_coin():
    """A stray larger number (chat / damage / other window) has no coin icon
    to its left -> must NOT be accepted as meso."""
    img = _frame_with_coin(coin_x=400, coin_y=150)  # coin far from everything
    boxes = [(100, 80, 100, 21, "999999999")]
    assert find_meso_candidate_verified(boxes, img, img.size) is None


def test_gold_verify_accepts_real_meso():
    img = _frame_with_coin()  # coin at x 60-90
    boxes = [(150, 80, 75, 21, "1,371,339")]
    found = find_meso_candidate_verified(boxes, img, img.size)
    assert found is not None
    assert found[4] == 1_371_339


def test_gold_verify_prefers_verified_over_larger():
    """The old logic would have picked the 9-digit blob. With verification the
    larger digit-less-coin number is rejected and the coin-backed meso wins."""
    img = _frame_with_coin()  # coin at x 60-90 (left of the meso box)
    boxes = [
        (400, 80, 100, 21, "999999999"),   # bigger, but no coin to its left
        (150, 80, 75, 21, "1,371,339"),    # the real meso counter
    ]
    found = find_meso_candidate_verified(boxes, img, img.size)
    assert found is not None
    assert found[4] == 1_371_339


def test_gold_verify_all_rejected_returns_none():
    """Several pure-digit blobs but none backed by a coin -> None (inventory
    closed is the realistic case: no coin, no counter)."""
    img = _frame_with_coin(coin_x=450, coin_y=150)
    boxes = [
        (100, 60, 100, 21, "1234567"),
        (250, 80, 75, 21, "888"),
    ]
    assert find_meso_candidate_verified(boxes, img, img.size) is None
