"""Quickbar slot assignment (2026-09-06).

The potion count on each quickbar slot is OCR'd from the whole quickbar
crop. Assigning it to a slot by dividing the crop into equal eighths is
wrong: the crop includes blank margins and slot borders, so counts landed
one slot left of their real slot (measured on
samples/maple_story_ui_20260906_a/b.png -- 181/1056 read fine but were put
in the wrong slot). The key labels above each slot (Shift/Ins/Home/PgUp/
Ctrl/Del/End/PgDn) anchor the real geometry instead.
"""
from maple_analyzer.overlay import _match_quick_key, _quickbar_slots_from_boxes


# ---- key-label normalisation ----------------------------------------------

def test_match_quick_key_exact():
    assert _match_quick_key("Shift") == "Shift"
    assert _match_quick_key("PgDn") == "PgDn"


def test_match_quick_key_ocr_variants():
    assert _match_quick_key("Shit") == "Shift"
    assert _match_quick_key("Pup") == "PgUp"
    assert _match_quick_key("Hm") == "Home"
    assert _match_quick_key("Dell") == "Del"
    assert _match_quick_key("Ctl") == "Ctrl"


def test_match_quick_key_rejects_unrelated_text():
    assert _match_quick_key("楓幣") is None
    assert _match_quick_key("181") is None
    assert _match_quick_key("ITEM") is None  # 'Ins'? no: ITEM doesn't match


# ---- slot geometry --------------------------------------------------------

def _a_style_boxes():
    """Geometry modelled on samples/maple_story_ui_20260906_a.png (the
    wide quickbar crop): two top-row key labels + one bottom-row label
    detected, counts at the top/bottom rightmost slots."""
    return [
        (200, 64, 34, 12, "Shit"),   # top row, slot 1 label
        (245, 65, 23, 11, "Ins"),    # top row, slot 2 label
        (246, 109, 24, 13, "Dell"),  # bottom row, slot 6 label
        (335, 86, 36, 18, "181"),    # potion count, top-right slot (slot 4)
        (335, 130, 42, 18, "1056"),  # potion count, bottom-right slot (slot 8)
    ]


def test_key_anchored_slots_assign_to_real_slot():
    result = _quickbar_slots_from_boxes(_a_style_boxes(), 627, 227)
    assert result.get(4) == 181
    assert result.get(8) == 1056
    assert 3 not in result  # the old equal-division bug put 181 in slot 3
    assert 7 not in result


def test_key_anchored_slots_without_bottom_label_still_rows():
    boxes = [
        (200, 64, 34, 12, "Shit"),
        (245, 65, 23, 11, "Ins"),
        (335, 86, 36, 18, "181"),
        (335, 130, 42, 18, "1056"),
    ]
    result = _quickbar_slots_from_boxes(boxes, 627, 227)
    # row height falls back to slot width; the count below the top row must
    # still land in the bottom row.
    assert result.get(4) == 181
    assert result.get(8) == 1056


def test_non_digit_labels_never_become_counts():
    """Bottom-toolbar text like 'S54%' must not be read as a slot count even
    though parse_meso would extract the '54' run."""
    boxes = _a_style_boxes() + [(0, 206, 20, 12, "S54%")]
    result = _quickbar_slots_from_boxes(boxes, 627, 227)
    assert 54 not in result.values()


def test_fallback_equal_division_when_no_key_labels():
    """No key labels readable (tiny native-res text): equal division over
    the crop, matching the pre-2026-09-06 behaviour."""
    boxes = [(10, 10, 30, 15, "154,821")]
    # 200x100 crop: digit centre (25, 17) -> row 0, col 0 -> slot 1
    assert _quickbar_slots_from_boxes(boxes, 200, 100) == {1: 154_821}


def test_no_readable_boxes_returns_empty():
    assert _quickbar_slots_from_boxes([], 200, 100) == {}
    assert _quickbar_slots_from_boxes([(0, 0, 20, 10, "Shit")], 200, 100) == {}
