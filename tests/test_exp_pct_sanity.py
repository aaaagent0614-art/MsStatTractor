"""exp_pct sanity check (2026-09-11).

Live logs show the tiny EXP font misreads a leading '6' in the first decimal
as '8', and only there: over 3248 reads, 182 (5.6%) came back exactly
+0.19/+0.20 high with the second decimal untouched (81.61 -> '81.80', 82.66 ->
'82.86'), confined to the X.6Y bands, while exp_cur was correct every time.

ExpTotalTracker learns the level's EXP requirement from exp_cur/pct (a running
maximum, safe because the error is one-way) and rewrites a read only when it
disagrees with exp_cur/T by that exact signature. The numbers below are the
real ones from the 2026-09-11 11:55:57 window.
"""
from __future__ import annotations

from maple_analyzer.parser import ExpTotalTracker, StatSnapshot, parse_fields

_T = 655_222.0  # level 49's EXP requirement, as implied by the first live read


def _snap(exp_cur: int, exp_pct: float | None, level: int | None = 49) -> StatSnapshot:
    return StatSnapshot(
        level=level, hp_cur=None, hp_max=None, mp_cur=None, mp_max=None,
        exp_cur=exp_cur, exp_pct=exp_pct,
    )


def test_learns_the_level_requirement_from_a_clean_read():
    t = ExpTotalTracker()
    snap = _snap(532_040, 81.20)
    t.sanitize(snap)
    assert snap.exp_pct == 81.20  # a clean read is never touched
    assert t.total is not None and abs(t.total - 655_221.67) < 1


def test_corrects_the_six_read_as_eight_misread():
    """'81.61' came back as '81.80' -- first decimal 6 -> 8, second untouched."""
    t = ExpTotalTracker()
    t.total, t.level = _T, 49
    snap = _snap(534_652, 81.80)
    t.sanitize(snap)
    assert snap.exp_pct == 81.6


def test_leaves_a_clean_read_alone():
    t = ExpTotalTracker()
    t.total, t.level = _T, 49
    snap = _snap(535_267, 81.70)
    t.sanitize(snap)
    assert snap.exp_pct == 81.70


def test_does_not_corrupt_a_clean_read_when_the_seed_was_a_misread():
    """If the very first read was the misread, T starts ~0.24% low. Clean
    reads then sit BELOW what T predicts by -0.19, which is not the bug's
    signature, so they must survive untouched (and fix T on the way)."""
    t = ExpTotalTracker()
    t.sanitize(_snap(534_652, 81.80))          # seeded from the misread
    seed = t.total
    assert seed is not None and seed < _T
    snap = _snap(535_267, 81.70)
    t.sanitize(snap)
    assert snap.exp_pct == 81.70
    assert t.total is not None and t.total > seed  # learned upward, no damage


def test_learns_upward_only_so_a_misread_cannot_raise_t():
    t = ExpTotalTracker()
    t.total, t.level = _T, 49
    t.sanitize(_snap(534_652, 81.80))  # implied T is ~0.24% LOW
    assert t.total == _T  # unchanged: the max only moves up


def test_a_dropped_digit_in_the_percentage_does_not_poison_t():
    """A read like '8.20' for 81.60% implies a 10x T. That is not a level
    change, so it must be ignored rather than adopted."""
    t = ExpTotalTracker()
    t.total, t.level = _T, 49
    snap = _snap(534_652, 8.20)
    t.sanitize(snap)
    assert t.total == _T
    assert snap.exp_pct == 8.20  # left for the display's own guards


def test_a_level_change_invalidates_the_requirement():
    t = ExpTotalTracker()
    t.total, t.level = _T, 49
    snap = _snap(100, 1.00, level=50)
    t.sanitize(snap)
    assert t.level == 50
    assert t.total == 10_000.0  # re-seeded from the new level's first read


def test_ignores_reads_without_both_values():
    t = ExpTotalTracker()
    for snap in (_snap(534_652, None), _snap(0, 0.00), _snap(534_652, 100.0)):
        before = snap.exp_pct
        t.sanitize(snap)
        assert snap.exp_pct == before
    assert t.total is None


def test_end_to_end_from_the_live_ocr_text():
    """The exact strings the 2026-09-11 log carried at 11:55:57."""
    t = ExpTotalTracker()
    first = parse_fields({"LV": "LV. 49  双亿氮a球", "EXP": "EXP 532040[81 20%]"})
    t.sanitize(first)
    bad = parse_fields({"LV": "LV. 49  双亿氮a球", "EXP": "EXP 534652[81 80%]"})
    assert bad.exp_pct == 81.8
    t.sanitize(bad)
    assert bad.exp_cur == 534_652  # exp_cur is trusted verbatim
    assert bad.exp_pct == 81.6
