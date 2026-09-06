"""Crop-box definitions for the stat panel and its fields.

These fixed pixel boxes (measured off samples/maple_story_ui.jpg at 1351x800)
are a LAST-RESORT fallback only: auto mode normally locates the panel by OCR
detection every pass and persists the detected fractional positions (see
settings.auto_stat_frac), so it self-corrects under resolution/DPI/magnifier
changes. FIELD_BOXES below are used only before the first successful detection
(or if the user never runs auto mode). Manual mode doesn't use these at all --
it OCRs the user-marked rectangles directly.
"""
from __future__ import annotations

from dataclasses import dataclass

REFERENCE_CLIENT_SIZE = (1351, 800)  # size of samples/maple_story_ui.jpg

# Whole stat panel, in absolute pixels at REFERENCE_CLIENT_SIZE. Grabbed once per
# tick with a single mss.grab() call; FIELD_BOXES below are sliced out of it
# in-memory (no extra screen captures).
STAT_PANEL_BOX = (260, 758, 900, 800)  # (left, top, right, bottom)

# Per-field boxes, same reference frame as STAT_PANEL_BOX, generously padded
# around each field's label+value text (measured off samples/maple_story_ui.jpg,
# see commit history for the crop-and-inspect process). Recognition-only OCR
# (no detection) runs on each of these individually -- see ocr.py's read_field()
# docstring for why this replaced running detection over the whole panel
# (detection was ~600ms/call, the actual OCR bottleneck; recognition-only on a
# small pre-cropped box is ~15ms).
FIELD_BOXES = {
    "LV": (278, 774, 362, 799),
    "HP": (486, 767, 600, 787),
    "MP": (600, 767, 712, 787),
    "EXP": (712, 767, 858, 787),
}

# The quickbar (8 slots, two rows of four) sits in the bottom-right of the
# client area, separate from the bottom-centre stat panel. These fractions
# are the whole quickbar row (key labels + potion counts), measured off
# samples/maple_story_ui.jpg (x≈933-1063 of 1351, y≈666-711 of 800) and the
# 1920x1077 sample. Used as the auto-detect position when the user hasn't
# marked the quickbar manually (see Settings.manual_quick_bar_region).
QUICK_BAR_FRAC = (0.60, 0.78, 0.92, 0.94)  # (left, top, right, bottom) as fractions


@dataclass(frozen=True)
class Box:
    left: int
    top: int
    right: int
    bottom: int

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.right, self.bottom)


def scale_box(box: tuple[int, int, int, int], client_size: tuple[int, int]) -> Box:
    """Scale a box defined at REFERENCE_CLIENT_SIZE to an arbitrary client size.

    Naive linear scaling -- fine as a stopgap since the game UI panel is
    bottom-anchored and roughly resolution-proportional, but not a substitute for
    real anchor template-matching (fonts/icons don't scale linearly in practice).
    """
    ref_w, ref_h = REFERENCE_CLIENT_SIZE
    cw, ch = client_size
    sx, sy = cw / ref_w, ch / ref_h
    l, t, r, b = box
    return Box(round(l * sx), round(t * sy), round(r * sx), round(b * sy))
