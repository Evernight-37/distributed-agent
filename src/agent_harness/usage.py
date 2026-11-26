"""Normalize executor token usage and receiver timings for execution details."""

import math
from datetime import datetime


USAGE_FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_tokens")
CODEX_APP_FIELDS = dict(
    zip(USAGE_FIELDS, ("inputTokens", "outputTokens", "cachedInputTokens", "cacheWriteInputTokens"))
)
CODEX_EXEC_FIELDS = dict(zip(USAGE_FIELDS, (*USAGE_FIELDS[:3], "cache_write_input_tokens")))


def _count(value):
    return type(value) is int and 0 <= value <= 2**53 - 1


def _counts(raw, field_names):
    if not isinstance(raw, dict):
        return {}
    return {key: raw[name] for key, name in field_names.items() if _count(raw.get(name))}


def _token_counts(raw):
    counts = _counts(raw, {key: key for key in USAGE_FIELDS})
    if "input_tokens" in counts:
        # The input total includes both cache categories, when reported.
        if counts.get("cached_input_tokens", 0) + counts.get("cache_write_tokens", 0) > counts["input_tokens"]:
            counts.pop("cached_input_tokens", None)
            counts.pop("cache_write_tokens", None)
    return counts


def _timestamp(value):
    if isinstance(value, str):
        try:
            date = datetime.fromisoformat(value)
            value = date.timestamp() if date.tzinfo is not None else None
        except (OSError, OverflowError, ValueError):
            return None
    if type(value) in (float, int) and 0 < value <= 253402300799 and math.isfinite(value):
        return value
    return None


def execution_telemetry(result):
    fields = {}
    for name in ("started_at", "finished_at"):
        if (value := _timestamp(result.get(name))) is not None:
            fields[name] = value
    if fields.get("finished_at", float("inf")) < fields.get("started_at", 0):
        fields.pop("finished_at")
    if usage := _token_counts(result.get("usage")):
        fields["usage"] = usage
    return fields


class CodexThreadUsage:
    """Account for this turn using its first baseline and latest thread total."""

    def __init__(self):
        self.baseline = None
        self.latest = {}

    def observe(self, usage):
        if not isinstance(usage, dict):
            return
        total = _counts(usage.get("total"), CODEX_APP_FIELDS)
        last = _counts(usage.get("last"), CODEX_APP_FIELDS)
        if not total or not last:
            return
        if self.baseline is None:
            self.baseline = {key: total[key] - last[key] for key in total if key in last and total[key] >= last[key]}
        for key in self.latest.keys() & total.keys():
            if total[key] < self.latest[key]:
                self.baseline.pop(key, None)
        self.latest = total

    def result(self):
        return _token_counts(
            {key: self.latest[key] - value for key, value in (self.baseline or {}).items() if key in self.latest}
        )


class CliUsage:
    """Accumulate one standalone CLI invocation's accounting events."""

    def __init__(self, kind, resumed):
        self.kind = kind
        self.resumed = resumed
        self.codex = {}
        self.expected_steps = set()
        self.steps = {}
        self.conflict = False

    def observe(self, event):
        if self.kind == "codex":
            # Exec reports a thread total, which can include prior turns after resume.
            if not self.resumed and event.get("type") == "turn.completed":
                counts = _counts(event.get("usage"), CODEX_EXEC_FIELDS)
                if any(counts.values()):
                    self.codex = _token_counts(counts)
            return
        if self.kind != "dsh" or event.get("type") != "status" or event.get("phase") not in ("step_start", "step_end"):
            return
        turn, step = event.get("turn"), event.get("step")
        if not _count(turn) or not _count(step):
            self.conflict = True
            return
        key = (turn, step)
        if event["phase"] == "step_start":
            self.expected_steps.add(key)
            return
        counts = self.dsh_step(event.get("usage"))
        if key in self.steps and self.steps[key] != counts:
            self.conflict = True
        self.steps[key] = counts

    @staticmethod
    def dsh_step(raw):
        # DSH reports disjoint uncached/read/write counts per model call.
        counts = _counts(
            raw,
            {
                "uncached": "inputTokens",
                "output_tokens": "outputTokens",
                "cached_input_tokens": "cacheReadTokens",
                "cache_write_tokens": "cacheWriteTokens",
                "total": "totalTokens",
            },
        )
        total, output, uncached = counts.pop("total", None), counts.get("output_tokens"), counts.pop("uncached", None)
        cache = counts.get("cached_input_tokens", 0) + counts.get("cache_write_tokens", 0)
        if total is not None and output is not None and total >= output + cache + (uncached or 0):
            counts["input_tokens"] = total - output
        elif total is None and uncached is not None and all(k in counts for k in USAGE_FIELDS[2:]):
            counts["input_tokens"] = uncached + cache
        return _token_counts(counts)

    def result(self):
        if self.kind == "codex":
            return self.codex
        if self.conflict or not self.steps or not self.expected_steps.issubset(self.steps):
            return {}
        return _token_counts(
            {
                key: sum(step[key] for step in self.steps.values())
                for key in USAGE_FIELDS
                if all(key in step for step in self.steps.values())
            }
        )
