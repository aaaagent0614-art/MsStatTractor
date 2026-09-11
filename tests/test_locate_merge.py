"""A partial locate pass must not delete the fields it did not re-find.

The 2026-09-11 2560 log shows EXP missing from every tick for four minutes
after a pass that detected LV but not EXP: `_apply_locate` replaced the whole
box set with the partial one. The derived quickbar box needs LV *and* EXP, so
the potion reads lost their anchor at the same time.
"""
from maple_analyzer.overlay import _merge_stat_boxes

_OLD = {"LV": (0.01, 0.95, 0.05, 0.03), "EXP": (0.59, 0.95, 0.14, 0.02)}
_NEW_LV = (0.02, 0.96, 0.05, 0.03)


def test_partial_pass_keeps_the_fields_it_missed():
    merged = _merge_stat_boxes(_OLD, {"LV": _NEW_LV})
    assert merged["LV"] == _NEW_LV          # the fresh box wins
    assert merged["EXP"] == _OLD["EXP"]     # the missed one survives


def test_refound_fields_are_overwritten():
    merged = _merge_stat_boxes(_OLD, {"LV": _NEW_LV, "EXP": (0.6, 0.95, 0.14, 0.02)})
    assert merged["EXP"] == (0.6, 0.95, 0.14, 0.02)


def test_first_ever_pass_is_taken_verbatim():
    assert _merge_stat_boxes(None, {"LV": _NEW_LV}) == {"LV": _NEW_LV}
    assert _merge_stat_boxes({}, {"LV": _NEW_LV}) == {"LV": _NEW_LV}
