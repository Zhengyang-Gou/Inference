"""Binding windows on the single simulation clock; no endpoint selection."""

import math
from collections import OrderedDict


class BindingWindow:
    """Dispatch eligible IDs; callback returns IDs handled or dispatched.

    Unhandled requests remain pending. Expired early triggers are consumed once
    until the next regular retry, preventing a zero-time deferral loop.
    """

    def __init__(self, config, clock, on_window, time_limit_seconds, on_schedule=None):
        self.config, self.clock, self.on_window = config, clock, on_window
        self.on_schedule = on_schedule
        self.interval = max(1, round(config.window_ms*1e6))
        self.margin = round(config.early_trigger_margin_ms*1e6)
        self.max_wait = max(1, round(config.max_wait_ms*1e6))
        self.limit = round(time_limit_seconds*1e9)
        self.pending = OrderedDict()
        self.generation, self.scheduled_at = 0, None
        self.last_fired = -1
        self.stats = dict(windows=0, dispatched_requests=0, deferred_checks=0, peak_pending=0, reasons={})

    def add_request(self, request_id, ttft_deadline_seconds, deferred=False):
        now = round(self.clock.now_seconds()*1e9)
        if not isinstance(request_id, str) or not request_id or request_id in self.pending:
            raise ValueError("binding window requires a new nonempty request ID")
        if not math.isfinite(ttft_deadline_seconds) or ttft_deadline_seconds < 0:
            raise ValueError("TTFT deadline must be finite and nonnegative")
        self.pending[request_id] = (now, round(ttft_deadline_seconds*1e9), now if deferred else -1)
        self.stats["peak_pending"] = max(self.stats["peak_pending"], len(self.pending))
        self._arm()

    def _next(self):
        now = round(self.clock.now_seconds()*1e9)
        candidates = [((now//self.interval+1)*self.interval, 2, "regular")]
        for arrived, deadline, consumed in self.pending.values():
            for target, priority, reason in ((deadline-self.margin, 0, "ttft_deadline"),
                                             (arrived+self.max_wait, 1, "max_wait")):
                if target > consumed:
                    candidates.append((max(now, target), priority, reason))
        return min(candidates)

    def _arm(self):
        if not self.pending:
            return
        at, _, reason = self._next()
        if at > self.limit or self.scheduled_at is not None and self.scheduled_at <= at:
            return
        self.generation += 1
        generation = self.generation
        self.scheduled_at = at
        def fire():
            if generation != self.generation:
                return
            self.scheduled_at = None
            ids = tuple(self.pending)
            if not ids:
                return
            handled = tuple(self.on_window(ids, reason))
            if len(set(handled)) != len(handled) or not set(handled) <= set(ids):
                raise ValueError("binding callback returned duplicate or unknown IDs")
            for rid in handled:
                self.pending.pop(rid)
            now = round(self.clock.now_seconds()*1e9)
            for rid, (arrived, deadline, _) in list(self.pending.items()):
                self.pending[rid] = (arrived, deadline, now)
            self.last_fired = now
            self.stats["windows"] += 1
            self.stats["dispatched_requests"] += len(handled)
            self.stats["deferred_checks"] += len(ids)-len(handled)
            self.stats["reasons"][reason] = self.stats["reasons"].get(reason, 0)+1
            self._arm()
        if self.on_schedule:
            self.on_schedule(at/1e9, generation)
        self.clock.call_at(at/1e9, fire)

    def snapshot(self):
        return dict(self.stats, pending_request_ids=list(self.pending),
                    scheduled_at_seconds=self.scheduled_at/1e9 if self.scheduled_at is not None else None)
