"""Fixed-epoch session tracking: values accumulate from an explicit start
point until the session is finalized and a new one begins, rather than a
sliding window.

A rolling window (the original design) shrinks in a confusing way once it's
been running longer than the window: e.g. a big EXP gain 4 minutes ago ages
out of a 5-min window even though nothing changed just now, making the
displayed diff decrease with no corresponding in-game event. A session fixes
that -- the start values (EXP, HP, MP) are set once and held constant until
`finalize()` is called, so 'EXP diff' unambiguously means 'since the session
started', full stop.
"""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class SessionSummary:
    """A finalized, immutable record of one completed session.

    Every field is a JSON/SQLite-primitive (float, int, or None) and there
    are no references to Session, StatSnapshot, or anything OCR/UI-related --
    this is deliberately the shape a future persistence layer would store and
    a future UI would read, decoupled from how it's produced or displayed.
    See ~/.claude/notes/maplestory-analyzer/ui-plan-2026-08-17.md for the
    plan to add that persistence layer and a session-history browser on top
    of this struct, without touching the capture/OCR/parser engine.
    """

    start_time: float
    end_time: float
    start_exp: int | None
    end_exp: int | None
    hp_loss: int
    mp_loss: int
    # EXP required for the whole current level, derived once during the
    # session from a tick that had both cur and pct (cur / (pct/100)) -- see
    # Session._total_exp. None if no such tick occurred during the session.
    total_exp: float | None
    # The session-length *setting* in effect when this session ended -- not
    # necessarily equal to duration_s. They differ whenever a session is
    # manually restarted before the timer fires, or (once the settings UI
    # exists) the interval setting is changed between sessions -- recording
    # it here means a saved/displayed session stays self-describing even if
    # the live setting has since changed.
    interval_minutes: float | None
    # EXP actually gained over the session, accumulated tick by tick so it
    # stays correct across level-ups -- end_exp - start_exp is wrong the moment
    # a session spans one, since the game's counter resets to ~0. None only for
    # summaries built before this existed, which fall back to the subtraction.
    exp_gained: int | None = None
    # Total wall-clock seconds the session spent paused (frozen via
    # Session.pause() -- the stop-into-pending flow pauses a session that is
    # then committed later, possibly minutes/hours afterwards). 0 for sessions
    # that never paused. duration_s subtracts this so a summary's duration is
    # the *active* session time, matching Session.elapsed() -- otherwise the
    # wait between Stop and the eventual commit inflates duration and dilutes
    # every per-minute metric. 0 by default, so summaries persisted before
    # this field existed keep their old (unpaused) durations.
    paused_s: float = 0.0
    # Quick-slot potion bottles actually consumed this session (start count -
    # end count per the quickbar OCR), when the player picked a slot. None for
    # sessions without quick-slot tracking -- History shows '--' rather than a
    # fabricated 0. Set by Session.finalize from hp_potion_consumed.
    hp_potion_used: int | None = None
    mp_potion_used: int | None = None
    # Total potion spend in meso, computed at commit time by the UI layer
    # (rate.py has no prices; overlay stamps it alongside map_name). 0 when no
    # prices were configured or nothing was consumed. History's 總收益 row is
    # meso_gained + sale_meso - potion_cost.
    potion_cost: int = 0
    # Meso delta over the session, from the inventory counter (see
    # parser.find_meso_from_boxes). Both endpoints are real OCR readings: the
    # first valid meso value seen after the session started, and the last
    # one seen before it finalized. None when the inventory was never
    # opened (or track_meso was off) during the session. Signed: buying
    # potions/items shows up as a negative delta, which is correct data.
    start_meso: int | None = None
    end_meso: int | None = None
    meso_gained: int | None = None
    # Meso from selling accumulated equipment, recorded *after* the session
    # stopped (see Session.record_sale). Not part of meso_gained -- that is
    # the session's live delta, which never sees a sale because selling
    # happens between sessions. 0 by default, None only for summaries built
    # before this field existed. The session's true meso total is
    # meso_gained + sale_meso.
    sale_meso: int = 0
    # User-assigned label, e.g. "grinding spot A". None until renamed via the
    # History tab -- UI-layer concern only, the engine never sets this.
    name: str | None = None
    # Map the session was recorded on (typed by hand or auto-OCR'd from the
    # map banner). UI-layer concern, set by overlay from Settings.map_name at
    # finalize time -- the engine never reads the game's map itself.
    map_name: str | None = None

    @property
    def exp_diff(self) -> int | None:
        if self.exp_gained is not None:
            return self.exp_gained
        if self.start_exp is None or self.end_exp is None:
            return None
        return self.end_exp - self.start_exp

    @property
    def exp_pct_diff(self) -> float | None:
        diff = self.exp_diff
        if diff is None or not self.total_exp:
            return None
        return diff / self.total_exp * 100

    @property
    def duration_s(self) -> float:
        # Active time: wall-clock span minus whatever the session spent
        # paused. A session that stopped (paused) and was committed later
        # must not count the wait as grinding time.
        return max(0.0, (self.end_time - self.start_time) - self.paused_s)

    @property
    def exp_per_min(self) -> float | None:
        """Efficiency metric: EXP gained per minute of session time. None
        when no EXP data was captured."""
        diff = self.exp_diff
        if diff is None:
            return None
        dur = self.duration_s
        if dur <= 0:
            return None
        return diff / dur * 60


class _LossTracker:
    """Accumulates the downward side of one stat's per-tick deltas, with two
    guards against OCR noise.

    Why guards are needed here specifically: HP/MP loss is a *running total*
    that only ever increases, so a single bad reading is baked in permanently
    -- unlike EXP, which is end-minus-start and self-corrects on the next
    tick. Observed live: an idle character (zero real MP spend) accumulated
    142,258 MP of "loss", ~50x the character's max MP, purely from misreads.

    Guard 1 -- max stability. `max` is constant except at level-up, so a tick
    reporting a different one was misparsed and is discarded whole. This
    catches the expensive modes, where a misread '/' shifts a digit across the
    divider: '1663/2816' -> '16632/816' reads *high*, costs nothing that tick,
    then books a 14,969 phantom loss when the next correct read "drops" back.
    A genuinely new max (level-up) is accepted once a second tick corroborates
    it, so this can't wedge the tracker permanently -- or, without a level
    bump alongside it, once a third tick does (see _accept_max: a level-up
    IS the corroboration that a max change is real, so it needs one less tick
    than a max change with no level movement at all).

    Guard 2 -- outlier hold. A reading within OUTLIER_FRACTION of max from the
    last accepted value is normal play and is taken immediately (no lag). A
    reading further out than that has to be corroborated by a second reading
    near it before it becomes the new baseline; otherwise it is held aside and
    forgotten the moment a normal reading arrives.

    Two earlier designs failed here and are worth not repeating:

    - Guarding only *drops* leaks badly (48k of phantom loss in a 3-minute
      idle simulation), because a phantom *high* read poisons the baseline
      instantly and the true values then "drop" back to reality and
      corroborate each other perfectly. The guard must be symmetric.
    - A median-of-3 despike is symmetric but only survives *isolated* spikes.
      Under the alternating good/bad pattern real OCR produces
      (1663, 3, 1663, 16, ...) the median window itself is half garbage and
      the filter passes the noise straight through.

    Outlier hold survives both: the alternating case never corroborates, and
    a genuine large change (a one-shot, a full-heal) simply lands one tick
    late. Normal-sized changes aren't delayed at all.

    Cost: a real, large, single-tick dip that fully recovers before the next
    read isn't counted -- but at 2Hz such a dip was already invisible half the
    time. A *persistently* misread value is booked once, then becomes the
    baseline; bounded, unlike the old unbounded ratchet.

    Establishing `max` in the first place is a separate concern, owned by
    Session's calibration (see its docstring) -- this class never adopts a
    max on its own. `confirm_max()` is the only way `_max` is set from
    nothing; before that call, `record()` treats every tick as unusable and
    is a no-op, exactly like a tick with no maximum at all.
    """

    # Fraction of max MP/HP a single tick (1Hz) may move before the reading
    # is treated as suspect. Generous on purpose: this only decides what needs
    # corroboration, not what counts, so a real big hit still lands (one tick
    # late) while digit-truncation misreads -- which are always wrong by most
    # of the bar -- are the ones held.
    OUTLIER_FRACTION = 0.5

    def __init__(self) -> None:
        self.loss = 0
        self._last: int | None = None       # last accepted value
        self._max: int | None = None        # established max for this stat
        self._max_candidate: int | None = None
        self._max_candidate_count = 0
        self._max_candidate_level_bumped = False
        self._last_level: int | None = None
        self._candidate: int | None = None  # outlier awaiting corroboration

    def confirm_max(self, maximum: int) -> None:
        """Called once by Session's calibration, when `maximum` has been
        corroborated by independent means (see Session._calibrate_hp_max /
        _calibrate_mp_max) -- this is the only place `_max` is ever set from
        nothing. `_last` is deliberately left alone: the very next record()
        call baselines off its `cur` with no loss booked, same as the first
        tick of a stat that's never seen a max at all."""
        self._max = maximum

    def reset(self, last: int | None) -> None:
        """New session: zero the total, keep the established max (it survives
        session boundaries), baseline off the last known value."""
        self.loss = 0
        self._last = last
        self._candidate = None

    def rebaseline(self, cur: int | None) -> None:
        """Re-anchor without booking a loss -- used when resuming from a
        pause (see Session.resume): whatever happened while paused is
        invisible to the session, so the first post-resume reading must not
        be diffed against the pre-pause value."""
        if cur is not None:
            self._last = cur
        self._candidate = None

    def record(self, cur: int | None, maximum: int | None = None, level: int | None = None) -> bool:
        """Feed one reading. Returns True when `cur` was accepted (it became
        the tracker's new baseline / was committed into the loss math), False
        when it was rejected as garbage or held aside awaiting corroboration.

        Session.record relies on this to decide whether the reading is safe
        to carry across a session boundary as the next start() baseline: a
        rejected tick must NOT update Session._hp_cur/_mp_cur, or a garbage
        final tick of one session poisons the next session's baseline (the
        tracker's own guarded `_last` is discarded by reset())."""
        if cur is None:
            return False
        if maximum is not None:
            accepted = self._accept_max(maximum, level)
            if level is not None:
                self._last_level = level
            if not accepted:
                return False  # misparsed tick -- don't let it near the loss math
            if cur > maximum:
                return False  # cur can never exceed max; this reading is garbage
        if self._last is None:
            self._last = cur
            return True
        tolerance = (self._max or self._last) * self.OUTLIER_FRACTION
        if abs(cur - self._last) <= tolerance:
            self._commit(cur)
            return True
        elif self._candidate is not None and abs(cur - self._candidate) <= tolerance:
            self._commit(cur)  # corroborated -- a real jump after all
            return True
        else:
            self._candidate = cur  # hold; a normal reading next tick discards it
            return False

    def _commit(self, cur: int) -> None:
        if cur < self._last:
            self.loss += self._last - cur
        self._last = cur
        self._candidate = None

    # A level-up nudges max HP/MP; nothing in the game multiplies it. A
    # proposed max outside this factor of the established one is garbage and
    # is never adopted, no matter how many ticks repeat it -- corroboration
    # alone is not enough, because when a window covers the panel the OCR
    # garbage is *static*: the identical wrong text every tick. That is how a
    # max of 281616 (from '281616' misread out of '2816') got installed in a
    # live capture, after which a bogus cur of 28163 passed every check and
    # booked 25,347 of phantom loss when the panel came back.
    MAX_CHANGE_FACTOR = 2.0

    def _accept_max(self, maximum: int, level: int | None) -> bool:
        if maximum <= 0:
            return False
        if self._max is None:
            # Session's calibration (require_calibration=True) always calls
            # confirm_max() before this tracker ever sees a real record()
            # call, so this branch only fires with calibration off -- same
            # unconditional first-adopt as before that feature existed.
            self._max = maximum
            return True
        if maximum == self._max:
            self._max_candidate = None
            self._max_candidate_count = 0
            return True
        if not (self._max / self.MAX_CHANGE_FACTOR <= maximum <= self._max * self.MAX_CHANGE_FACTOR):
            return False  # implausible -- never adopt, however often it repeats
        level_bumped = (
            level is not None and self._last_level is not None and level > self._last_level
        )
        if maximum != self._max_candidate:
            self._max_candidate = maximum
            self._max_candidate_count = 1
            # Captured at the moment the candidate first appears, not
            # re-evaluated every tick after -- a level-up IS the
            # corroboration, so it only counts for the change it accompanies,
            # not for an unrelated max blip that happens to follow one.
            self._max_candidate_level_bumped = level_bumped
        else:
            self._max_candidate_count += 1
        required = 2 if self._max_candidate_level_bumped else 3
        if self._max_candidate_count >= required:
            self._max = maximum
            self._max_candidate = None
            self._max_candidate_count = 0
            return True
        return False


class Session:
    """See the module docstring for the fixed-epoch design.

    Calibration (require_calibration=True, the default): max HP, max MP, and
    the starting EXP are not trusted from a single reading. Each is
    established independently, once 3 consecutive ticks corroborate it (2 if
    the ticks visibly moved -- see _calib_liveness), and nothing is recorded
    into the session until all three are confirmed. is_calibrating reports
    this state; while true, elapsed() reads 0 and the HUD should show
    "Calibrating...".

    This exists because the first screenshot after launch/focus can be wrong
    -- see mp-loss-investigation-2026-08-17.md for the live case: a covered
    panel misread max MP as 281616 from 2816, which then locked out the real
    value forever (MAX_CHANGE_FACTOR rejects anything more than 2x away from
    an established max, and there was nothing to re-establish it with).

    Repetition alone is a weak acceptance test. When a window covers the
    panel the OCR garbage is *static* -- the identical wrong text every tick
    -- so N-identical-reads certifies it exactly as happily as it certifies
    the truth. Two earlier guard designs in _LossTracker failed this exact
    way (see its docstring). _calib_liveness raises the bar somewhat: a tick
    where nothing in the whole snapshot moved counts for less than one where
    something did, so a frozen stream needs the full 3 ticks (never fewer --
    see test_repetition_alone_never_confirms_faster_than_the_minimum) while
    ordinary live play confirms in 2. It is a tiebreaker, not a requirement --
    a genuinely idle character moves nothing either, and confirmation must
    still complete for them.

    This is NOT a claim that 3 static ticks of well-formed-but-wrong data can
    never calibrate -- nothing at this layer has ground truth to check a
    number against in isolation, that's what parser.py's structural filters
    (a missing '[' etc.) are for, upstream of this. What calibration actually
    protects against, and
    is tested against real captured frames for
    (test_calibration_locks_onto_the_truth_before_real_garbage_arrives): a
    single bad frame arriving among otherwise-good ones, and a bad max
    change post-calibration outliving its own corroboration window -- the
    two concrete bugs this whole mechanism exists to fix.

    Once confirmed, calibration does not repeat for the lifetime of this
    Session object -- start() (restart, timer rollover) carries the
    already-confirmed max/baseline forward exactly as before, with no delay.
    A level-up's max change goes through _LossTracker's own (separate,
    always-on) corroboration instead, never through this calibration path
    again.
    """

    # How many corroborating ticks establish a value during calibration. A
    # tick where the whole snapshot moved (see _calib_liveness) counts double,
    # so 2 live ticks (2+2=4) or 3 static ones (1+1+1=3) both clear this.
    CALIB_TARGET = 3

    def __init__(self, require_calibration: bool = True) -> None:
        self._require_calibration = require_calibration
        self._calibrated = not require_calibration

        self._start_time: float | None = None
        self._start_exp: int | None = None
        self._hp = _LossTracker()
        self._mp = _LossTracker()
        self._exp_cur: int | None = None
        self._hp_cur: int | None = None
        self._mp_cur: int | None = None
        self._total_exp: float | None = None
        # EXP is measured per *level segment* -- see _record_exp for why it is
        # deliberately not a tick-by-tick accumulator.
        self._banked = 0                      # gain from levels completed this session
        self._segment_start: int | None = None  # EXP at the start of the current level
        self._last_exp: int | None = None
        self._last_level: int | None = None
        self._last_implied_total: float | None = None  # see _exp_reading_is_trusted
        # Pending level-up reset, [prev_level_total, segment_start, low_count]
        # -- see _record_exp's level-up detection (2026-09-07).
        self._reset_candidate: list | None = None

        # Calibration state -- see the class docstring. Inert (never
        # consulted) when require_calibration is False.
        self._hp_max_calibrated = self._calibrated
        self._mp_max_calibrated = self._calibrated
        self._exp_calibrated = self._calibrated
        self._hp_calib_max: int | None = None
        self._hp_calib_count = 0
        self._mp_calib_max: int | None = None
        self._mp_calib_count = 0
        self._exp_calib_baseline: int | None = None
        self._exp_calib_total: float | None = None
        self._exp_calib_count = 0
        self._calib_last_snapshot: tuple | None = None

        # Pause state (see pause()/resume()).
        self._paused = False
        self._pause_started_at: float | None = None
        self._paused_total = 0.0
        self._resume_pending = False

        # Meso tracking (see record_meso). Deliberately NOT part of
        # calibration: the meso counter only exists while the inventory is
        # open, which also obscures the stat panel -- so meso readings
        # arrive on a completely different schedule from HP/MP/EXP and
        # can't be corroborated tick-by-tick the same way.
        self._start_meso: int | None = None
        self._end_meso: int | None = None
        # Equipment-sale revenue (see record_sale) -- accumulated after the
        # session stops, baseline diffed against _end_meso (or the previous
        # sale reading). Reset by start().
        self._sale_revenue = 0
        self._last_sale_meso: int | None = None
        # Quick-slot potion counter (2026-09-02) -- same first=baseline /
        # last=end shape as meso (see record_potion). Reset by start().
        self._hp_slot_start: int | None = None
        self._hp_slot_end: int | None = None
        self._mp_slot_start: int | None = None
        self._mp_slot_end: int | None = None

    def start(
        self,
        now: float | None = None,
        initial_meso: int | None = None,
        initial_hp_potion: int | None = None,
        initial_mp_potion: int | None = None,
    ) -> None:
        """Begin a new session. Carries forward whatever EXP/HP/MP values are
        already known as the new baseline (so a level-up-triggered or
        timer-triggered restart doesn't wait a tick to re-establish it) --
        unless calibration has never completed, in which case there is
        nothing yet to carry forward and the clock stays off until it does.

        initial_meso / initial_hp_potion / initial_mp_potion come from the
        pre-start confirm dialog (2026-09-02): the meso counter and quick-slot
        counts only exist while the inventory is open, and the player opens it
        BEFORE Start -- after Start the inventory may be closed and those
        baselines could never be re-read. Setting them here survives the
        calibration window: while _start_time is still None record_meso /
        record_potion no-op, but the seeded endpoints already stand, so the
        first reading after calibration lands as the END value."""
        if self._require_calibration and not self._calibrated:
            self._start_time = None
        else:
            self._start_time = now if now is not None else time.time()
        self._start_exp = self._exp_cur
        self._hp.reset(self._hp_cur)
        self._mp.reset(self._mp_cur)
        self._total_exp = None  # re-derived fresh -- could differ after a level-up
        self._banked = 0
        self._segment_start = self._exp_cur
        self._last_exp = self._exp_cur
        self._reset_candidate = None
        self._paused = False
        self._pause_started_at = None
        self._paused_total = 0.0
        self._resume_pending = False
        # A fresh session starts with no meso endpoints -- the user opens
        # the inventory after Start to establish the baseline (see
        # record_meso's docstring). The pre-start confirm dialog's readings
        # (inventory was open for 辨識) override that: see the docstring.
        self._start_meso = initial_meso
        self._end_meso = None
        self._sale_revenue = 0
        self._last_sale_meso = None
        # Quick-slot potion counter (2026-09-02): first reading after start is
        # the baseline, every later reading updates the end value, so the last
        # reading before finalize is the end point (same shape as meso).
        self._hp_slot_start = initial_hp_potion
        self._hp_slot_end = None
        self._mp_slot_start = initial_mp_potion
        self._mp_slot_end = None

    def sync_known(
        self,
        exp_cur: int | None = None,
        hp_cur: int | None = None,
        mp_cur: int | None = None,
        exp_pct: float | None = None,
        hp_max: int | None = None,
        mp_max: int | None = None,
        level: int | None = None,
    ) -> None:
        """Hand the UI's latest OCR'd readings to the engine BEFORE start().

        The UI keeps OCRing while stopped, but the Session only receives ticks
        once running -- so its internal _exp_cur/_hp_cur/_mp_cur stay at the
        previous session's end values (or None on first launch). start() then
        carries THOSE stale values forward as the new baseline, making the new
        session's numbers look re-detected from scratch (reported 2026-09-02).
        Call this with the UI's current _last snapshot immediately before
        start() so the baseline is the CURRENT screen state. Only non-None
        fields are updated; this is a pre-start hand-off, not a record."""
        if exp_cur is not None:
            self._exp_cur = exp_cur
            self._last_exp = exp_cur
        if hp_cur is not None:
            self._hp_cur = hp_cur
        if mp_cur is not None:
            self._mp_cur = mp_cur
        if level is not None:
            self._last_level = level
        if exp_cur and exp_pct:
            self._total_exp = exp_cur / (exp_pct / 100)

    # ---- pause/resume -------------------------------------------------

    def pause(self, now: float | None = None) -> None:
        """Freeze the session: elapsed() stops advancing and record() becomes
        a no-op, but the caller keeps polling/rendering live OCR values
        independently of Session -- pausing never stops the HUD updating,
        only what gets counted. No-op if not running or already paused."""
        if self._paused or self._start_time is None:
            return
        self._paused = True
        self._pause_started_at = now if now is not None else time.time()

    def resume(self, now: float | None = None) -> None:
        """Unfreeze. The next record() call re-baselines HP/MP/EXP off
        whatever it reads rather than diffing against the pre-pause values --
        see _rebaseline_after_resume. A level-up that happened entirely
        during the pause can't be reconstructed (no ticks were seen); it is
        absorbed into the new baseline with nothing banked, the same known
        gap as a session that misses a level-up's percentage reading."""
        if not self._paused:
            return
        now = now if now is not None else time.time()
        if self._pause_started_at is not None:
            self._paused_total += now - self._pause_started_at
        self._paused = False
        self._pause_started_at = None
        self._resume_pending = True

    @property
    def is_paused(self) -> bool:
        return self._paused

    def _rebaseline_after_resume(self, exp_cur: int | None, hp_cur: int | None, mp_cur: int | None) -> None:
        if exp_cur is not None:
            gained_during_pause = (exp_cur - self._exp_cur) if self._exp_cur is not None else 0
            # Only ever push segment_start forward -- a level-up during the
            # pause makes this look like a large drop, which the max(0, ...)
            # here correctly does NOT treat as a negative gain to claw back;
            # it just leaves the stale segment_start behind, i.e. the
            # documented "nothing banked" gap for that case.
            if self._segment_start is not None:
                self._segment_start += max(0, gained_during_pause)
            self._exp_cur = exp_cur
            self._last_exp = exp_cur
        if hp_cur is not None:
            self._hp.rebaseline(hp_cur)
            self._hp_cur = hp_cur
        if mp_cur is not None:
            self._mp.rebaseline(mp_cur)
            self._mp_cur = mp_cur

    # ---- calibration ----------------------------------------------------

    def _calib_liveness(self, hp_cur, mp_cur, exp_cur, level) -> bool:
        """True if anything in the snapshot differs from the previous clean
        tick's -- the tiebreaker that lets ordinary live play calibrate in 2
        ticks while static OCR garbage (identical every field, every tick,
        see the class docstring) needs the full 3."""
        current = (hp_cur, mp_cur, exp_cur, level)
        previous = self._calib_last_snapshot
        self._calib_last_snapshot = current
        if previous is None:
            return False
        return any(a is not None and b is not None and a != b for a, b in zip(previous, current))

    def _calibrate_hp_max(self, hp_cur, hp_max, live: bool) -> None:
        if hp_cur is None or hp_max is None or hp_max <= 0 or hp_cur > hp_max:
            return
        weight = 2 if live else 1
        if hp_max == self._hp_calib_max:
            self._hp_calib_count += weight
        else:
            self._hp_calib_max = hp_max
            self._hp_calib_count = weight
        if self._hp_calib_count >= self.CALIB_TARGET:
            self._hp_max_calibrated = True

    def _calibrate_mp_max(self, mp_cur, mp_max, live: bool) -> None:
        if mp_cur is None or mp_max is None or mp_max <= 0 or mp_cur > mp_max:
            return
        weight = 2 if live else 1
        if mp_max == self._mp_calib_max:
            self._mp_calib_count += weight
        else:
            self._mp_calib_max = mp_max
            self._mp_calib_count = weight
        if self._mp_calib_count >= self.CALIB_TARGET:
            self._mp_max_calibrated = True

    def _calibrate_exp_baseline(self, exp_cur, exp_pct, live: bool) -> None:
        """Corroborate by *consistency*, not equality -- EXP moves every tick
        during real play, so unlike max HP/MP the candidate value itself
        isn't expected to repeat. What must repeat is the implied level total
        (cur / (pct/100)), the same cross-check _exp_reading_is_trusted uses
        post-calibration. The baseline stays pinned to the FIRST reading of a
        corroborating streak, not the latest -- so once confirmed, EXP gained
        between that first reading and the confirming one is still counted
        rather than lost to the calibration wait."""
        if exp_cur is None:
            return
        implied = exp_cur / (exp_pct / 100) if exp_pct else None
        weight = 2 if live else 1
        if self._exp_calib_baseline is None:
            self._exp_calib_baseline = exp_cur
            self._exp_calib_total = implied
            self._exp_calib_count = weight
        else:
            ok = exp_cur >= self._exp_calib_baseline
            if ok and implied is not None and self._exp_calib_total is not None:
                band = Session.EXP_TOTAL_BAND + (0.005 / exp_pct)
                ok = abs(implied / self._exp_calib_total - 1) <= band
            if ok:
                self._exp_calib_count += weight
                if implied is not None:
                    self._exp_calib_total = implied
            else:
                self._exp_calib_baseline = exp_cur
                self._exp_calib_total = implied
                self._exp_calib_count = weight
        if self._exp_calib_count >= self.CALIB_TARGET:
            self._exp_calibrated = True

    @property
    def is_calibrating(self) -> bool:
        return self._require_calibration and not self._calibrated

    def record(
        self, exp_cur: int | None, hp_cur: int | None, mp_cur: int | None, exp_pct: float | None = None,
        hp_max: int | None = None, mp_max: int | None = None, level: int | None = None,
    ) -> None:
        """hp_max/mp_max/level are optional but strongly recommended: the maxes
        enable _LossTracker's max-stability guard (which catches the
        high-magnitude OCR misreads), and the level is what tells a level-up
        apart from a misread when EXP drops -- see _record_exp."""
        if self._paused:
            return

        if self._resume_pending:
            self._resume_pending = False
            self._rebaseline_after_resume(exp_cur, hp_cur, mp_cur)
            return  # this tick only re-anchors; accounting resumes next tick

        if self._require_calibration and not self._calibrated:
            live = self._calib_liveness(hp_cur, mp_cur, exp_cur, level)
            if not self._hp_max_calibrated:
                self._calibrate_hp_max(hp_cur, hp_max, live)
            if not self._mp_max_calibrated:
                self._calibrate_mp_max(mp_cur, mp_max, live)
            if not self._exp_calibrated:
                self._calibrate_exp_baseline(exp_cur, exp_pct, live)
            if not (self._hp_max_calibrated and self._mp_max_calibrated and self._exp_calibrated):
                return
            self._calibrated = True
            self._hp.confirm_max(self._hp_calib_max)
            self._mp.confirm_max(self._mp_calib_max)
            self._start_exp = self._exp_calib_baseline
            self._segment_start = self._exp_calib_baseline
            self._last_exp = self._exp_calib_baseline
            self._start_time = time.time()
            # Fall through -- this confirming tick's own readings are still
            # real data, not spent purely on calibration.

        if self._start_time is None:
            self.start()
        if self._start_exp is None and exp_cur is not None:
            self._start_exp = exp_cur
        # Only an ACCEPTED reading updates _hp_cur/_mp_cur. These two fields
        # become the next session's baseline via start() -> _LossTracker.reset()
        # (see _LossTracker.record's docstring), so an unguarded store would
        # let a rejected garbage tick (max mismatch / cur>max / uncorroborated
        # outlier) poison the *next* session with a phantom loss on its very
        # first clean reading -- the guard only protected within a session.
        if self._hp.record(hp_cur, hp_max, level):
            self._hp_cur = hp_cur
        if self._mp.record(mp_cur, mp_max, level):
            self._mp_cur = mp_cur
        self._record_exp(exp_cur, exp_pct, level)

    # A reading may deviate this far from the level total established by the
    # previous tick before it is treated as garbage. Deliberately loose: it is
    # meant to catch order-of-magnitude nonsense, not to police OCR jitter.
    EXP_TOTAL_BAND = 0.25

    def _exp_reading_is_trusted(self, exp_cur: int, exp_pct: float | None, level: int | None) -> bool:
        """Cross-check `cur` against `pct`.

        They are two independent OCR readings of the same quantity, so their
        ratio -- the level's total EXP, constant within a level -- validates
        them against each other. 'EXP S255[1 12%]' (a 5 read as an S) implies a
        total of 22,768 where every good reading agrees on ~468,500.

        Compared against the *previous accepted tick*, never against a total
        learned from the data: in the live capture the bad value was the
        majority (379 of 429 ticks), so anything that learned would have
        learned 22,768 and rejected every correct reading afterwards.

        This mainly protects finalize(). Segments (see _record_exp) already
        make a bad frame transient, but a summary freezes one instant, and a
        garbage frame captured there is written to History permanently.
        """
        if level is not None and level != self._last_level:
            self._last_implied_total = None  # new level, new total: re-baseline
        if exp_pct:
            implied = exp_cur / (exp_pct / 100)
            if self._last_implied_total:
                # pct is rounded to 2dp, so at small pct the implied total is
                # numerically unstable -- half a least-significant digit is
                # 0.005/pct in relative terms, i.e. +-50% at pct=0.01. A fixed
                # band would reject every legitimate reading after a level-up.
                band = self.EXP_TOTAL_BAND + (0.005 / exp_pct)
                if abs(implied / self._last_implied_total - 1) > band:
                    return False
            self._last_implied_total = implied
            return True
        # No percentage to check against: fall back to the one bound that needs
        # no cross-reference -- a single 1s tick cannot gain a whole level.
        if self._total_exp and self._last_exp is not None:
            if abs(exp_cur - self._last_exp) > self._total_exp:
                return False
        return True

    def _record_exp(self, exp_cur: int | None, exp_pct: float | None, level: int | None) -> None:
        """Track EXP gained, per level *segment*.

        Within a level this is plain end-minus-start, which is the important
        property: it depends only on the current reading, so a garbage frame
        shows a wrong number for one tick and then self-corrects. Only a
        level-up banks a segment and opens a new one.

        It is deliberately NOT a tick-by-tick accumulator. That was tried
        (2026-08-19) to handle level-ups and it regressed badly: summing every
        rise means one absurd reading is baked in forever, exactly the ratchet
        that makes HP/MP loss fragile. A single garbage frame -- 'EXP101332182',
        no brackets, no percentage -- booked +101,322,049 of phantom gain in
        one tick and it never came back. end-minus-start showed +16,058 over
        the same run.

        HP/MP cannot be done this way and must accumulate: loss is a path
        integral, not a difference. A character who takes 5,000 damage and
        potions back to full has the same endpoints as one who stood still.
        That is why the guards in _LossTracker exist there and are not needed
        here.
        """
        if exp_cur is None:
            return
        if not self._exp_reading_is_trusted(exp_cur, exp_pct, level):
            return  # garbage -- treat it as an unreadable field and carry forward
        # Captured before the update below: _total_exp is re-derived every tick
        # from cur/pct, so by the time we see the reset it already describes
        # the *new* level. The level just finished has to be measured with the
        # pre-update value, or every level-up under-counts by a level.
        previous_total = self._total_exp

        if self._segment_start is None:
            self._segment_start = exp_cur

        # Level-up detection (2026-09-07). The LV field and the EXP field OCR
        # at different moments, so a real level-up rarely shows both changes
        # in the SAME frame (observed live: level still read 45 while EXP had
        # already reset to 32). Requiring the old level-jump + EXP-drop pair
        # in one frame meant the reset frame was classified as a misread, the
        # segment never banked, and exp_diff sat at 0 forever -- the reported
        # "EXP 歸零後就停住" bug. Now an EXP value far below the established
        # level total ARMS a candidate; it confirms on a second low frame OR
        # when the level actually jumps. A misread recovers high the next
        # frame and cancels (that protection is test_exp_drop_without_a_level_
        # change_is_ignored).
        level_jumped = (
            level is not None
            and self._last_level is not None
            and level > self._last_level
        )
        if self._reset_candidate is None:
            # Arms only on a DROP into the low band (exp < last reading AND
            # far below the level total) -- steady low-level grinding at the
            # start of a level never drops, so it must not arm. previous_total
            # here is the level total from the last frame (before this frame
            # re-derives it from the reset value).
            if (
                previous_total
                and self._last_exp is not None
                and exp_cur < self._last_exp
                and exp_cur < previous_total * 0.15
            ):
                self._reset_candidate = [previous_total, self._segment_start, 1]
        else:
            cand_total, _cand_seg_start, _count = self._reset_candidate
            if exp_cur < cand_total * 0.15:
                self._reset_candidate[2] += 1
            else:
                self._reset_candidate = None  # recovered high: it was a misread
        if self._reset_candidate is not None and (
            self._reset_candidate[2] >= 2 or level_jumped
        ):
            cand_total, cand_seg_start, _ = self._reset_candidate
            if cand_total:
                self._banked += max(0, int(cand_total - (cand_seg_start or 0)))
            self._segment_start = 0
            self._reset_candidate = None

        self._last_exp = exp_cur
        self._exp_cur = exp_cur
        if level is not None:
            self._last_level = level
        if exp_cur and exp_pct:
            self._total_exp = exp_cur / (exp_pct / 100)

    def elapsed(self, now: float | None = None) -> float:
        """Pause-adjusted: frozen at the pause instant while paused, and
        resumes counting from where it left off once resume() runs -- see
        pause()/resume()."""
        if self._start_time is None:
            return 0.0
        now = now if now is not None else time.time()
        end = self._pause_started_at if (self._paused and self._pause_started_at is not None) else now
        return max(0.0, (end - self._start_time) - self._paused_total)

    @property
    def start_exp(self) -> int | None:
        return self._start_exp

    @property
    def exp_diff(self) -> int | None:
        """EXP gained since the session started, spanning level-ups (see
        _record_exp). Clamped at 0: within a level EXP only rises, so a
        negative here means a misread, and showing 0 for a tick beats
        rendering a negative behind the '+' the HUD prints."""
        if self._start_exp is None or self._exp_cur is None or self._segment_start is None:
            return None
        return max(0, self._banked + (self._exp_cur - self._segment_start))

    def record_meso(self, meso: int) -> None:
        """Feed one meso counter reading into the session.

        The inventory counter is the only reliable meso source, and it only
        exists while the inventory window is open (which also obscures the
        stat panel -- see parser.find_meso_from_boxes). So unlike HP/MP/EXP
        there is no continuous stream to filter: the user opens the
        inventory once after Start and once before the session ends, and
        each valid reading becomes the corresponding endpoint. First reading
        after start = baseline, every later reading keeps updating the end
        value, so the last reading before finalize is the end point.

        No-op while paused or before the session has actually started
        (elapsed() needs a real start to anchor a baseline to)."""
        if self._paused or self._start_time is None:
            return
        if self._start_meso is None:
            # First reading of the session is the baseline only -- setting
            # end here too would make a single inventory opening report a
            # misleading "+0" instead of "no end reading yet".
            self._start_meso = meso
            self._end_meso = None
        else:
            self._end_meso = meso

    def record_potion(self, kind: str, count: int) -> None:
        """Feed one quick-slot potion count into the session (2026-09-02,
        reworked 2026-09-03). `kind` is "hp" or "mp".

        Same shape as record_meso: the first reading after start becomes the
        baseline, every later reading keeps updating the end value, so the
        last reading before finalize is the end point. The difference is the
        real bottles consumed -- more accurate than the HP/MP-loss estimate
        when the potion sits in a visible quickbar slot.

        No-op while paused or before the session has actually started."""
        if self._paused or self._start_time is None:
            return
        if kind == "hp":
            if self._hp_slot_start is None:
                self._hp_slot_start = count
                self._hp_slot_end = None
            else:
                self._hp_slot_end = count
        else:
            if self._mp_slot_start is None:
                self._mp_slot_start = count
                self._mp_slot_end = None
            else:
                self._mp_slot_end = count

    @property
    def hp_potion_consumed(self) -> int | None:
        """HP bottles consumed per the quickbar counter: start − end, clamped
        at 0. None until both endpoints read. A refill mid-session makes end
        >= start; the unseen refill means the true consumption is
        under-reported, which is the honest reading."""
        if self._hp_slot_start is None or self._hp_slot_end is None:
            return None
        return max(0, self._hp_slot_start - self._hp_slot_end)

    @property
    def mp_potion_consumed(self) -> int | None:
        """MP bottles consumed per the quickbar counter (see
        hp_potion_consumed)."""
        if self._mp_slot_start is None or self._mp_slot_end is None:
            return None
        return max(0, self._mp_slot_start - self._mp_slot_end)

    @property
    def hp_slot_count(self) -> int | None:
        """Latest HP-slot count read (the running end value)."""
        return self._hp_slot_end

    @property
    def mp_slot_count(self) -> int | None:
        """Latest MP-slot count read (the running end value)."""
        return self._mp_slot_end

    @property
    def start_meso(self) -> int | None:
        """The session's meso baseline (first valid inventory reading after
        start). None until the user opens the inventory once."""
        return self._start_meso

    @property
    def end_meso(self) -> int | None:
        """The latest valid meso reading of the session. None until the user
        opens the inventory at least twice (start + at least one more)."""
        return self._end_meso

    @property
    def meso_gained(self) -> int | None:
        """Net meso change over the session (end - start). None until BOTH
        endpoints exist -- one inventory opening isn't enough to diff
        against. Signed: spending (potions, items) shows as negative."""
        if self._start_meso is None or self._end_meso is None:
            return None
        return self._end_meso - self._start_meso

    def record_sale(self, meso: int) -> int | None:
        """Feed one post-sale meso reading into the current session, for the
        equipment the user sells *after* the session has stopped.

        record_meso is deliberately a no-op while paused (the session clock is
        frozen, so the user is no longer grinding) -- but selling happens
        exactly then, and its proceeds belong to the session whose drops are
        being sold. So this is a separate path, callable in the stopped state:
        it diffs the reading against the session's end meso (or the previous
        sale reading) and accumulates every positive delta as sale revenue.

        Returns the delta booked this call, or None when there was no prior
        meso reading to diff against (the inventory was never read during the
        session -- the first sale reading then just becomes the baseline for
        the next one)."""
        base = self._last_sale_meso if self._last_sale_meso is not None else self._end_meso
        if base is None:
            self._last_sale_meso = meso
            return None
        delta = meso - base
        if delta > 0:
            self._sale_revenue += delta
        self._last_sale_meso = meso
        return delta

    @property
    def sale_revenue(self) -> int:
        """Total meso attributed to equipment sales recorded for this session
        (see record_sale). 0 until the user records a sale."""
        return self._sale_revenue

    @property
    def total_meso(self) -> int | None:
        """The session's true meso total: live drops (meso_gained) plus
        equipment-sale revenue. None until meso_gained itself is known."""
        if self.meso_gained is None:
            return None
        return self.meso_gained + self._sale_revenue

    @property
    def hp_loss(self) -> int:
        return self._hp.loss

    @property
    def mp_loss(self) -> int:
        return self._mp.loss

    @property
    def total_exp(self) -> float | None:
        return self._total_exp

    def projected_exp(self, window_s: float, now: float | None = None) -> int | None:
        """EXP this session would total if the current rate held for the
        whole `window_s` -- rate is exp_diff / elapsed(), which is already
        pause-adjusted, so pausing doesn't deflate the projection. None
        before there's enough signal to extrapolate from (same 3s/positive
        gain guard the level-up ETA uses -- a 1-2s sample swings wildly)."""
        diff = self.exp_diff
        elapsed = self.elapsed(now)
        if diff is None or diff <= 0 or elapsed <= 3:
            return None
        return int(diff / elapsed * window_s)

    def finalize(self, interval_minutes: float | None = None, now: float | None = None) -> SessionSummary:
        now = now if now is not None else time.time()
        end_time = now
        # Total pause time this session accumulated, including the *ongoing*
        # pause when finalize is called while paused (the stop-into-pending
        # flow does exactly that: _stop_into_pending pauses, and the commit
        # happens later, still paused). Mirrors elapsed()'s arithmetic so
        # duration_s == the pause-adjusted active time.
        paused_s = self._paused_total
        if self._paused and self._pause_started_at is not None:
            paused_s += max(0.0, now - self._pause_started_at)
        return SessionSummary(
            start_time=self._start_time if self._start_time is not None else end_time,
            end_time=end_time,
            start_exp=self._start_exp,
            end_exp=self._exp_cur,
            hp_loss=self._hp.loss,
            mp_loss=self._mp.loss,
            total_exp=self._total_exp,
            interval_minutes=interval_minutes,
            exp_gained=self.exp_diff,
            paused_s=paused_s,
            hp_potion_used=self.hp_potion_consumed,
            mp_potion_used=self.mp_potion_consumed,
            start_meso=self._start_meso,
            end_meso=self._end_meso,
            meso_gained=self.meso_gained,
            sale_meso=self._sale_revenue,
        )
