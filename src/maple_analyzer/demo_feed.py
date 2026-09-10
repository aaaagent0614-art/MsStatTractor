"""Synthetic frame generator standing in for a live game window.

This repo's dev environment has no Windows/game window to capture from, so this
module fabricates plausible-looking successive frames by redrawing the EXP text
in the sample screenshot with an incrementing value, using the EXP field's real
pixel box (measured off that screenshot -- see regions.py). Everything
downstream of this (OCR, parsing, rate calc, overlay) is the real pipeline
running against real inference, not mocked -- only the "camera" is fake.

Swap `DemoExpFeed` for `capture.GameWindowCapture` on Windows with the game
running and nothing else changes.
"""
from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .regions import FIELD_BOXES, STAT_PANEL_BOX

# EXP text box in the reference (post-2026-09-10 patch) client frame, measured
# off samples/maple_story_ui_patched_1366x768.png via OCR -- absolute coords,
# same reference frame as FIELD_BOXES, and kept tight for the same reason.
_EXP_TEXT_BOX = (807, 729, 917, 742)


class DemoExpFeed:
    def __init__(self, sample_path: str | Path, start_exp: int = 162950, exp_per_tick: tuple[int, int] = (40, 220)):
        self._base = Image.open(sample_path).convert("RGB")
        self._exp = start_exp
        self._exp_per_tick = exp_per_tick
        try:
            self._font = ImageFont.truetype("DejaVuSans-Bold.ttf", 11)
        except OSError:
            self._font = ImageFont.load_default()

    def _render_frame(self) -> Image.Image:
        frame = self._base.copy()
        draw = ImageDraw.Draw(frame)
        l, t, r, b = _EXP_TEXT_BOX
        bg = frame.getpixel((l - 3, t + 2))  # sample background just left of the text
        draw.rectangle((l, t, r + 40, b), fill=bg)  # +40: new text may be wider
        pct = (self._exp % 10000) / 100  # synthetic, just needs to move plausibly
        draw.text((l, t - 1), f"EXP{self._exp}[{pct:05.2f}%]", font=self._font, fill=(210, 255, 90))
        return frame

    def grab_panel(self) -> Image.Image:
        self._exp += random.randint(*self._exp_per_tick)
        frame = self._render_frame()
        pl, pt, pr, pb = STAT_PANEL_BOX
        return frame.crop((pl, pt, pr, pb))

    def grab_fields(self) -> dict[str, Image.Image]:
        self._exp += random.randint(*self._exp_per_tick)
        frame = self._render_frame()
        return {name: frame.crop(box) for name, box in FIELD_BOXES.items()}

    def grab_full(self) -> Image.Image:
        return self._render_frame()
