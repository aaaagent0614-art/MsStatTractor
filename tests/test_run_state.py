"""OverlayApp's run-state machine: running/paused/stopped, auto-stop,
restart-with-save, deleting a History entry, and the timer/auto-finalize
staying alive while OCR capture is failing.

Same technique as test_tick_loop.py: poke OverlayApp's unbound methods
against a stub `self` rather than constructing the real app (which needs a
live Tk display) -- what's under test here is control flow and Session
wiring, not widgets. _StubWidget stands in for every CTk widget OverlayApp
touches (.configure/.cget/.grid/.grid_remove/.grid_info/.set), which is
enough for every method exercised below; none of them inspect real Tk
geometry or fonts beyond what _t()/_font() compute from Settings.language.
"""
from __future__ import annotations

import time
from collections import defaultdict

import pytest

from maple_analyzer import overlay as overlay_module
from maple_analyzer.overlay import OverlayApp
from maple_analyzer.parser import StatSnapshot
from maple_analyzer.rate import Session, SessionSummary
from maple_analyzer.settings import Settings


@pytest.fixture(autouse=True)
def _auto_confirm_dialogs(monkeypatch):
    """Auto-confirm every messagebox so run-state tests don't open real Tk
    dialogs on the stub root. The pre-start missing-source checks (2026-09-02)
    and the sale/delete confirmations all route through messagebox; tests that
    assert a specific dialog answer override this locally with their own
    monkeypatch.setattr."""
    monkeypatch.setattr(overlay_module.messagebox, "askyesno", lambda *a, **kw: True)
    monkeypatch.setattr(overlay_module.messagebox, "showwarning", lambda *a, **kw: None)


class _StubWidget:
    """Minimal stand-in for any CTkLabel/CTkButton/CTkFrame OverlayApp calls
    .configure/.cget/.grid/.grid_remove/.grid_info/.set on."""

    def __init__(self):
        self._opts: dict = {}
        self._gridded = True

    def configure(self, **kw):
        self._opts.update(kw)

    def cget(self, key):
        return self._opts.get(key)

    def grid(self, **kw):
        self._gridded = True

    def grid_remove(self):
        self._gridded = False

    def grid_info(self):
        return {"in": "parent"} if self._gridded else {}

    def set(self, value):  # CTkProgressBar
        self._opts["value"] = value


class _StubRoot:
    def attributes(self, *_a, **_kw):
        pass

    def destroy(self):
        pass

    def update_idletasks(self):
        pass


class _StubSource:
    """Fakes capture.py's PanelSource: grab_fields() returns already-"OCR'd"
    text directly (see _StubApp.__init__, which points _ocr.read_field at an
    identity function), so no real screen capture or OCR model is needed."""

    TOTAL = 500_000.0

    def __init__(self):
        self.hp, self.mp, self.exp, self.level = 800, 2800, 100, 44
        self.blocked = False

    def _pct(self) -> float:
        return self.exp / self.TOTAL * 100

    def grab_fields(self):
        if self.blocked:
            raise RuntimeError("stat panel is obscured")
        return {
            "LV": f"LV. {self.level}",
            "HP": f"HP[{self.hp}/824]",
            "MP": f"MP[{self.mp}/2816]",
            "EXP": f"EXP {self.exp}[{self._pct():.2f}%]",
        }


class _StubOcr:
    @staticmethod
    def read_field(img):
        return img  # _StubSource already hands back text, not an image


class _StubApp:
    """Assembles just enough of OverlayApp's instance state for the unbound
    methods under test to run. run_state defaults to "stopped", matching
    OverlayApp.__init__ (see overlay.py's _run_state docstring)."""

    def __init__(self):
        self._settings = Settings()
        self._session = Session(require_calibration=False)  # mirrors OverlayApp (HP/MP retired 2026-09-02)
        self._session_history: list[SessionSummary] = []
        self._history_cards: list = []
        self._last = StatSnapshot(None, None, None, None, None, None, None)
        self._source = _StubSource()
        self._manual_source = None
        self._ocr = _StubOcr()
        self._modal_open = False
        self._run_state = "stopped"
        self._session_pending = False
        self._sale_recorded = False
        self._last_capture_error: str | None = None
        self._last_client_size = None
        self.root = _StubRoot()
        # Locator state: run-state tests don't exercise the background
        # locator thread; _do_tick reads _stat_boxes and calls _try_locate.
        self._stat_boxes = None
        self._meso_box = None
        self._consecutive_fail_ticks = 0
        self._auto_meso_misses = 0
        self._force_locate = False
        self._last_locate_attempt = 0.0
        self._last_locate_client_size: tuple[int, int] | None = None
        self._meso_scan_ticks = 0

        self._status_pill = _StubWidget()
        self._timer_label = _StubWidget()
        self._pause_button = _StubWidget()
        self._restart_button = _StubWidget()
        self._stop_button = _StubWidget()
        self._value_labels: dict = defaultdict(_StubWidget)
        self._bars: dict = defaultdict(_StubWidget)
        self._map_value_label = _StubWidget()
        self._compact_win = None
        self._manual_overrides: dict = {}
        self._last_meso: int | None = None
        self._last_hp_slot_count: int | None = None
        self._last_mp_slot_count: int | None = None
        self._sale_done = False
        self._baseline = overlay_module._Baseline()

        self.rebuild_calls = 0

    def _append_history_card(self, _summary, _index):
        pass  # card rendering is out of scope -- only session_history matters here

    def _rebuild_history_cards(self):
        self.rebuild_calls += 1

    def _try_locate(self):
        pass  # background locator thread is out of scope for run-state tests

    def _save_history(self):
        pass  # no disk I/O in run-state tests

    def _persist_settings(self):
        pass  # no disk I/O in run-state tests

    def _update_history_summary(self):
        pass  # UI summary strip -- out of scope for run-state tests

    def _notify_session_end(self):
        pass  # sound/flash -- out of scope for run-state tests

    def _check_screen_change(self):
        pass  # screen-size warning -- out of scope for run-state tests

    def _update_compact_visibility(self):
        pass  # compact 2x2 overlay -- out of scope for run-state tests

    def _render_compact(self, _snap):
        pass  # compact 2x2 overlay -- out of scope for run-state tests

    # Bound to the real implementations -- these are the actual behaviour
    # under test, not stand-ins.
    _t = OverlayApp._t
    _font = OverlayApp._font
    _localize_error = OverlayApp._localize_error
    _set_status_error = OverlayApp._set_status_error
    _log = staticmethod(OverlayApp._log)
    _modal = OverlayApp._modal
    _active_source = OverlayApp._active_source
    _do_tick = OverlayApp._do_tick
    _render = OverlayApp._render
    _update_timer_label = OverlayApp._update_timer_label
    _maybe_finalize_on_timeout = OverlayApp._maybe_finalize_on_timeout
    _maybe_refresh_manual_meso = OverlayApp._maybe_refresh_manual_meso
    _maybe_scan_auto_meso = OverlayApp._maybe_scan_auto_meso
    _want_relocate = OverlayApp._want_relocate
    _commit_session_to_history = OverlayApp._commit_session_to_history
    _finalize_and_maybe_stop = OverlayApp._finalize_and_maybe_stop
    _start_session_with_current_values = OverlayApp._start_session_with_current_values
    _start_session_with_baseline = OverlayApp._start_session_with_baseline

    def _confirm_start_baseline(self):
        # Run-state tests don't drive the real confirm dialog; auto-confirm an
        # empty baseline. Tests that exercise cancel/confirm override this.
        return overlay_module._Baseline()
    _refresh_compare_tab = OverlayApp._refresh_compare_tab
    _stop_into_pending = OverlayApp._stop_into_pending
    _commit_pending_session = OverlayApp._commit_pending_session
    _confirm_start_without_sale = OverlayApp._confirm_start_without_sale
    _on_record_sale_clicked = OverlayApp._on_record_sale_clicked
    _read_meso_now = OverlayApp._read_meso_now
    _levelup_eta_s = OverlayApp._levelup_eta_s
    _on_close = OverlayApp._on_close
    _save_compact_pos = OverlayApp._save_compact_pos
    _on_restart_clicked = OverlayApp._on_restart_clicked
    _on_stop_clicked = OverlayApp._on_stop_clicked
    _on_pause_button_clicked = OverlayApp._on_pause_button_clicked
    _apply_run_state = OverlayApp._apply_run_state
    _on_delete_history_clicked = OverlayApp._on_delete_history_clicked
    _apply_manual_overrides = OverlayApp._apply_manual_overrides
    _read_detected_values = OverlayApp._read_detected_values
    _potion_enabled = OverlayApp._potion_enabled
    _potion_cost = OverlayApp._potion_cost
    _maybe_scan_quick_slot = OverlayApp._maybe_scan_quick_slot
    _read_slot_counts = OverlayApp._read_slot_counts
    _grab_quick_bar_image = OverlayApp._grab_quick_bar_image
    _scan_quick_slots_to_last = OverlayApp._scan_quick_slots_to_last


def _calibrate(app: _StubApp, gains=(100,)) -> None:
    """Run enough ticks to clear Session's calibration gate -- see
    test_baseline_confirmation.py for what this is actually guarding."""
    app._do_tick()
    for gain in gains:
        app._source.exp += gain
        app._do_tick()


# --- launching stopped, not tracking ------------------------------------


def test_app_launches_stopped_and_does_not_track():
    app = _StubApp()
    for _ in range(5):
        app._do_tick()
    assert app._run_state == "stopped"
    # Never fed while stopped: the session has not started (no calibration
    # gate anymore -- HP/MP retired 2026-09-02 -- so 'not started' is simply
    # a start_exp that was never committed).
    assert app._session.start_exp is None
    assert app._session_history == []


def test_stopped_app_still_shows_live_ocr_readouts():
    """Not tracking a session is not the same as a frozen HUD -- LV/EXP
    keep reading from the game while stopped (HP/MP rows were removed)."""
    app = _StubApp()
    app._do_tick()
    assert app._value_labels["level"].cget("text") == "44"
    assert "100" in app._value_labels["exp"].cget("text")


def test_start_button_begins_tracking():
    app = _StubApp()
    app._on_pause_button_clicked()  # "stopped" role: Start
    assert app._run_state == "running"
    _calibrate(app, gains=(100,))
    assert not app._session.is_calibrating
    assert app._session.start_exp == 100


# --- pause/resume via the same button -------------------------------------


def test_pause_button_cycles_running_paused_running():
    app = _StubApp()
    app._on_pause_button_clicked()  # stopped -> running
    _calibrate(app, gains=(100,))
    app._on_pause_button_clicked()  # running -> paused
    assert app._run_state == "paused"
    assert app._pause_button.cget("text") == app._t("resume_button")
    app._on_pause_button_clicked()  # paused -> running
    assert app._run_state == "running"
    assert app._pause_button.cget("text") == app._t("pause_button")


def test_stop_ends_session_without_starting_a_new_one(monkeypatch):
    app = _StubApp()
    app._on_pause_button_clicked()  # stopped -> running
    _calibrate(app, gains=(100,))
    app._session._start_time -= 2  # ensure elapsed >= 1s so stop commits
    app._on_pause_button_clicked()  # running -> paused
    assert app._run_state == "paused"
    app._on_stop_clicked()
    assert app._run_state == "stopped"
    # Stop now defers the commit: the session is *pending* (so equipment
    # revenue can still be recorded), not yet in History.
    assert app._session_pending is True
    assert len(app._session_history) == 0
    assert app._pause_button.cget("text") == app._t("start_button")
    # The session clock is frozen (paused), not rolled into a fresh session.
    assert app._session.is_calibrating is False
    # Starting the next session commits the pending one (after the prompt).
    monkeypatch.setattr(overlay_module.messagebox, "askyesno", lambda *a, **kw: True)
    app._on_pause_button_clicked()  # Start
    assert app._run_state == "running"
    assert len(app._session_history) == 1
    assert app._session_pending is False


# --- auto-stop (default on) -----------------------------------------------


def test_auto_stop_commits_and_stops_by_default():
    app = _StubApp()
    app._on_pause_button_clicked()
    _calibrate(app, gains=(100,))
    app._settings.window_min = 1
    app._session._start_time -= 61  # force the interval to have elapsed
    app._do_tick()
    assert app._run_state == "stopped"
    # auto-stop now leaves the session pending (deferred finalize).
    assert app._session_pending is True
    assert len(app._session_history) == 0
    assert app._restart_button.grid_info() == {}  # hidden while stopped
    elapsed_at_stop = app._session.elapsed()
    app._do_tick()
    assert app._session.elapsed() == elapsed_at_stop  # frozen, not overrunning


def test_auto_stop_disabled_restarts_instead():
    app = _StubApp()
    app._settings.auto_stop = False
    app._on_pause_button_clicked()
    _calibrate(app, gains=(100,))
    app._settings.window_min = 1
    app._session._start_time -= 61
    app._do_tick()
    assert app._run_state == "running"  # old behaviour: immediately restarts
    assert len(app._session_history) == 1
    assert app._session.start_exp is not None  # carried forward, not calibrating again


# --- restart-with-save (default on) ---------------------------------------


def test_restart_saves_to_history_by_default():
    app = _StubApp()
    assert app._settings.save_on_restart is True
    app._on_pause_button_clicked()
    _calibrate(app, gains=(100,))
    app._session._start_time -= 5  # past the 1s noise guard in _commit_session_to_history
    app._on_restart_clicked()
    assert len(app._session_history) == 1
    assert app._run_state == "running"


def test_restart_can_be_configured_to_discard():
    app = _StubApp()
    app._settings.save_on_restart = False
    app._on_pause_button_clicked()
    _calibrate(app, gains=(100,))
    app._session._start_time -= 5
    app._on_restart_clicked()
    assert app._session_history == []


# --- timer/auto-finalize survive a blocked capture window -----------------


def test_timer_label_keeps_moving_while_capture_is_blocked():
    app = _StubApp()
    app._on_pause_button_clicked()
    _calibrate(app, gains=(100,))
    # Reset the clock to a fixed "4.5s ago" so the remaining time is
    # deterministically 9:55 (600 - 4.5 = 595.5s) rather than subject to how
    # long _calibrate's ticks took -- the old `-= 5` was off-by-one flaky
    # (9:54 vs 9:55 depending on timing).
    app._session._start_time = time.time() - 4.5
    app._source.blocked = True
    app._do_tick()
    assert app._timer_label.cget("text") == app._t("timer_left", time="9:55")


def test_auto_finalize_still_fires_while_capture_is_blocked():
    """The bug being guarded: elapsed() is wall-clock, not tick-driven, so a
    session must still hit its interval and auto-stop even if the game
    window stays covered for the whole window, not just while OCR succeeds."""
    app = _StubApp()
    app._on_pause_button_clicked()
    _calibrate(app, gains=(100,))
    app._settings.window_min = 1
    app._session._start_time -= 61
    app._source.blocked = True
    app._do_tick()
    assert app._run_state == "stopped"
    assert app._session_pending is True  # stopped into pending, not committed


def test_stopped_session_does_not_auto_finalize_again():
    app = _StubApp()
    app._on_pause_button_clicked()
    _calibrate(app, gains=(100,))
    app._settings.window_min = 1
    app._session._start_time -= 61
    app._do_tick()
    assert app._run_state == "stopped"
    history_len = len(app._session_history)
    for _ in range(5):
        app._do_tick()
    assert len(app._session_history) == history_len  # no repeated commits


# --- projected session EXP -------------------------------------------------


def test_projected_exp_is_rendered_once_there_is_a_positive_rate():
    app = _StubApp()
    app._on_pause_button_clicked()
    _calibrate(app, gains=(100,))
    app._session._start_time -= 10  # clear the 3s-elapsed guard
    app._source.exp += 50
    app._do_tick()
    assert app._value_labels["projexp"].cget("text") != "--"


def test_projected_exp_shows_placeholder_before_enough_signal():
    app = _StubApp()
    app._on_pause_button_clicked()
    _calibrate(app, gains=(100,))
    app._do_tick()  # no time has passed yet
    assert app._value_labels["projexp"].cget("text") == "--"


# --- deleting one History entry --------------------------------------------


def _fake_summary(name: str | None = None) -> SessionSummary:
    return SessionSummary(
        start_time=0.0, end_time=60.0, start_exp=100, end_exp=200,
        hp_loss=0, mp_loss=0, total_exp=None, interval_minutes=10, name=name,
    )


def test_delete_history_removes_the_confirmed_entry(monkeypatch):
    app = _StubApp()
    app._session_history[:] = [_fake_summary("keep"), _fake_summary("delete me")]
    monkeypatch.setattr(overlay_module.messagebox, "askyesno", lambda *a, **kw: True)
    app._on_delete_history_clicked(2)  # 1-based -- "delete me" is index 2
    assert [s.name for s in app._session_history] == ["keep"]
    assert app.rebuild_calls == 1


def test_delete_history_cancelled_keeps_the_entry(monkeypatch):
    app = _StubApp()
    app._session_history[:] = [_fake_summary("a"), _fake_summary("b")]
    monkeypatch.setattr(overlay_module.messagebox, "askyesno", lambda *a, **kw: False)
    app._on_delete_history_clicked(1)
    assert [s.name for s in app._session_history] == ["a", "b"]
    assert app.rebuild_calls == 0


def test_delete_history_prompt_names_the_session(monkeypatch):
    app = _StubApp()
    app._session_history[:] = [_fake_summary("grinding spot A")]
    seen = {}

    def _fake_askyesno(title, prompt, **kw):
        seen["title"], seen["prompt"] = title, prompt
        return False

    monkeypatch.setattr(overlay_module.messagebox, "askyesno", _fake_askyesno)
    app._on_delete_history_clicked(1)
    assert "grinding spot A" in seen["prompt"]


# --- equipment-sale revenue (deferred finalize) ---------------------------

def test_start_commits_pending_session_without_prompt_when_sale_recorded():
    app = _StubApp()
    app._on_pause_button_clicked()
    _calibrate(app, gains=(100,))
    app._session._start_time -= 2
    app._on_stop_clicked()  # pending
    app._sale_recorded = True  # user already recorded the sale
    app._on_pause_button_clicked()  # Start: no prompt needed
    assert app._run_state == "running"
    assert len(app._session_history) == 1
    assert app._session_pending is False


def test_start_without_sale_prompts_and_can_stay_stopped(monkeypatch):
    app = _StubApp()
    app._on_pause_button_clicked()
    _calibrate(app, gains=(100,))
    app._session._start_time -= 2
    app._on_stop_clicked()  # pending
    monkeypatch.setattr(overlay_module.messagebox, "askyesno", lambda *a, **kw: False)
    app._on_pause_button_clicked()  # Start -> prompt -> "No"
    assert app._run_state == "stopped"
    assert app._session_pending is True
    assert app._session_history == []


def test_record_sale_records_and_commits_to_history(monkeypatch):
    app = _StubApp()
    app._on_pause_button_clicked()
    _calibrate(app, gains=(100,))
    app._session.record_meso(1_000_000)  # baseline
    app._session.record_meso(1_250_000)  # end -> +250k drops
    app._session._start_time -= 2
    app._on_stop_clicked()  # pending
    monkeypatch.setattr(app, "_read_meso_now", lambda: 1_500_000)
    app._on_record_sale_clicked()
    assert app._session.sale_revenue == 250_000
    assert app._session.total_meso == 500_000  # 250k drops + 250k sale
    # New behaviour (2026-08-27): recording the sale commits the session to
    # History immediately -- no waiting for the next Start.
    assert app._session_pending is False
    assert app._sale_recorded is False
    assert len(app._session_history) == 1
    assert app._session_history[0].sale_meso == 250_000
    assert app._session_history[0].meso_gained == 250_000


def test_record_sale_without_inventory_does_not_mark_recorded(monkeypatch):
    app = _StubApp()
    app._on_pause_button_clicked()
    _calibrate(app, gains=(100,))
    app._session.record_meso(1_000_000)
    app._session.record_meso(1_250_000)
    app._session._start_time -= 2
    app._on_stop_clicked()  # pending
    monkeypatch.setattr(app, "_read_meso_now", lambda: None)
    app._on_record_sale_clicked()
    assert app._sale_recorded is False
    assert app._session.sale_revenue == 0


def test_close_commits_pending_session():
    app = _StubApp()
    app._on_pause_button_clicked()
    _calibrate(app, gains=(100,))
    app._session._start_time -= 2
    app._on_stop_clicked()  # pending
    app._on_close()
    assert app._session_pending is False
    assert len(app._session_history) == 1


def test_start_cancel_from_confirm_dialog_stays_stopped(monkeypatch):
    """Pre-start confirm dialog (2026-09-02): cancelling keeps the app
    stopped and does not touch the pending session."""
    app = _StubApp()
    monkeypatch.setattr(app, "_confirm_start_baseline", lambda: None)  # cancel
    app._on_pause_button_clicked()  # Start -> dialog -> cancelled
    assert app._run_state == "stopped"
    assert not app._session_pending


def test_start_confirmed_baseline_seeds_session(monkeypatch):
    """Confirming the dialog starts the session with the baseline's meso and
    quick-slot counts seeded as endpoints (the inventory closes after)."""
    app = _StubApp()
    b = overlay_module._Baseline(meso=1_234_567, hp_potion=30, mp_potion=10)
    monkeypatch.setattr(app, "_confirm_start_baseline", lambda: b)
    app._on_pause_button_clicked()
    assert app._run_state == "running"
    assert app._baseline is b
    # The seeds survive the calibration window (record_meso/record_potion are
    # no-ops until Session's clock starts, but the endpoints already stand).
    assert app._session.start_meso == 1_234_567
    assert app._session._hp_slot_start == 30
    assert app._session._mp_slot_start == 10
    # After calibration, an end reading diffs against the seeded baseline.
    _calibrate(app)
    app._session.record_meso(1_240_000)
    app._session.record_potion("hp", 25)
    assert app._session.meso_gained == 5_433
    assert app._session.hp_potion_consumed == 5


# --- demand-driven background locator (2026-09-07) -------------------------


def test_want_relocate_is_demand_driven():
    """The full-frame detection pass must NOT run on a fixed cadence while
    grinding is healthy -- it now runs only when the cached boxes stop
    working (user request 2026-09-07: positions don't move while grinding)."""
    app = _StubApp()

    # Never located yet -> relocate (fixed reference boxes are a fallback).
    assert app._want_relocate() is True

    # Located and reading fine -> no relocation during steady grinding.
    app._stat_boxes = {"LV": (0.1, 0.9, 0.2, 0.02), "EXP": (0.1, 0.95, 0.2, 0.02)}
    app._last_locate_client_size = (1366, 768)
    app._consecutive_fail_ticks = 0
    assert app._want_relocate() is False

    # Panel went unreadable for RELOCATE_AFTER_FAILS consecutive ticks.
    app._consecutive_fail_ticks = overlay_module.RELOCATE_AFTER_FAILS
    assert app._want_relocate() is True

    # A settings change forces one relocate even while healthy.
    app._consecutive_fail_ticks = 0
    app._force_locate = True
    assert app._want_relocate() is True
    assert app._force_locate is False  # consumed
