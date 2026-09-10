import sys
from pathlib import Path

# Same src-on-path convention as scripts/run_overlay*.py -- this repo has no
# packaging config (no pyproject/setup.py), so tests need the same manual
# sys.path insert to import maple_analyzer.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# tests/ is not a package (no __init__.py), so a sibling fixture module like
# captured_frames.py isn't importable by default -- add tests/ itself too.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Samples from AFTER the 2026-09-10 game patch, which reworked the bottom HUD
# (fixed pixel size, horizontally centred status strip). The older 1351x800 and
# 1920x1077 samples no longer describe what the game renders and are kept only
# for reference. All three patched samples are native screenshots with the
# window title bar already cropped off, so their size IS the client size.
_SAMPLES = Path(__file__).resolve().parent.parent / "samples"
SAMPLE_IMAGE = _SAMPLES / "maple_story_ui_patched_1366x768.png"
SAMPLE_IMAGE_1920 = _SAMPLES / "maple_story_ui_patched_1920x1080.png"
SAMPLE_IMAGE_2560 = _SAMPLES / "maple_story_ui_patched_2560x1440.png"
