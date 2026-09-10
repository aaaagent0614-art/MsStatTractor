"""Thin wrapper around RapidOCR (PP-OCR recognition, ONNX/CPU).

See VERSIONS.md for why rapidocr-onnxruntime stands in for the spec's originally
named PP-OCRv6-tiny ONNX model -- same OCR family, bundled models, no manual
model-file wiring.

Recognition-only, not detection+recognition: benchmarked live against the real
game (2026-08-17), detection (finding text regions in an image) was ~600-680ms
per call -- the entire OCR bottleneck the rest of the pipeline was tuned around.
Recognition alone (reading a pre-cropped, known-to-contain-one-line-of-text
image) was ~15ms. Since regions.py's FIELD_BOXES already pins down exactly
where each field's text is, running detection to *re-discover* that on every
tick was pure waste -- see capture.py's grab_fields() for the cropping side of
this change.
"""
from __future__ import annotations

import numpy as np
from PIL import Image
from rapidocr_onnxruntime import RapidOCR

# How many CPU threads the OCR engine may use. onnxruntime defaults to every
# physical core, and the per-tick recognition + periodic full-frame detection
# would otherwise spin up N threads that compete with the game for CPU --
# the reported "game lags after opening" residual. Recognition on the small
# pre-cropped fields is ~15ms and detection ~600ms even on 2 threads, so this
# costs the HUD nothing measurable while removing the all-cores contention.
_OCR_INTRA_OP_THREADS = 2
_OCR_INTER_OP_THREADS = 1

# read_field() upscales crops narrower than this before recognition -- see its
# docstring. 200px covers the stat fields (80-125px wide) and the quickbar
# slots, and leaves genuinely wide crops (full-width meso strips) untouched.
_UPSCALE_BELOW_PX = 200
_UPSCALE_FACTOR = 2


class StatPanelOcr:
    def __init__(self) -> None:
        # intra_op_num_threads/inter_op_num_threads are forwarded by RapidOCR
        # through its config into onnxruntime's SessionOptions (see
        # rapidocr_onnxruntime's infer_engine._init_sess_opts) -- the only
        # supported way to bound OCR CPU usage without patching the library.
        self._engine = RapidOCR(
            intra_op_num_threads=_OCR_INTRA_OP_THREADS,
            inter_op_num_threads=_OCR_INTER_OP_THREADS,
        )

    def read_field(self, image: Image.Image) -> str:
        """Recognition-only OCR on a small pre-cropped single-line field crop.

        Small crops are upscaled first: the patched (2026-09-10) HUD renders
        the stat text smaller than before, and the engine drops digits and
        separators at that size -- 'LV. 47' came back as 'LV. 07' and
        'EXP 456903[82.22%]' lost the '3' and the '.', both of which corrupt
        the parsed values rather than merely lowering confidence. LANCZOS
        doubling costs ~10ms on a <200px crop and recovers all of them.
        """
        if image.width < _UPSCALE_BELOW_PX:
            image = image.resize(
                (image.width * _UPSCALE_FACTOR, image.height * _UPSCALE_FACTOR),
                Image.Resampling.LANCZOS,
            )
        result, _elapse = self._engine(np.array(image), use_det=False, use_cls=False)
        if not result:
            return ""
        return result[0][0]

    def detect_text(self, image: Image.Image) -> list[tuple[int, int, int, int, str]]:
        """Full detection over an arbitrary frame: returns (x, y, w, h, text)
        tuples in image-pixel coordinates.

        Expensive (~600ms measured on the game's panel-sized crops, more on a
        full frame) -- the module docstring explains why the per-tick stat
        path avoids detection entirely. For meso the box genuinely isn't
        known in advance (the inventory is draggable), so a full scan is the
        only reliable way to find it; callers are expected to throttle it
        (see overlay's MESO_SCAN_INTERVAL_TICKS)."""
        result, _elapse = self._engine(np.array(image), use_det=True, use_cls=False)
        if not result:
            return []
        boxes: list[tuple[int, int, int, int, str]] = []
        for box, text, _score in result:
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            boxes.append(
                (int(min(xs)), int(min(ys)), int(max(xs) - min(xs)), int(max(ys) - min(ys)), text)
            )
        return boxes
