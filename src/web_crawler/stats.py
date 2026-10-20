"""What happened during a crawl: counters, the progress line and the summary on stderr
(DESIGN.md §10).

Pure: the engine records events and passes the time in.
"""

import bisect
import itertools
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field


class Histogram:
    """Durations, in buckets 1% wide on a log scale: fixed memory however many there are,
    and any quantile within 0.5% of the exact one, for durations over 0.1 ms."""

    __slots__ = ("_counts", "count")

    GROWTH = 1.01  # each bucket's bounds, one over the other
    FLOOR = 1e-4  # seconds; a duration this short or shorter goes in the first bucket

    def __init__(self) -> None:
        self._counts: dict[int, int] = {}  # by bucket
        self.count = 0

    def add(self, seconds: float) -> None:
        bucket = int(math.log(seconds / self.FLOOR) / _LOG_GROWTH) if seconds > self.FLOOR else 0
        self._counts[bucket] = self._counts.get(bucket, 0) + 1
        self.count += 1

    def quantile(self, q: float) -> float | None:
        """The q-quantile, for 0 < q <= 1, by nearest rank: the duration ranked ceil(q *
        count) from the shortest; None if there are none. It's the middle of its bucket, on
        the log scale, so within half a bucket of the duration itself."""
        if not self.count:
            return None
        buckets = sorted(self._counts)
        reached = list(itertools.accumulate(self._counts[bucket] for bucket in buckets))
        rank = max(1, math.ceil(q * self.count))
        bucket = buckets[bisect.bisect_left(reached, rank)]
        return self.FLOOR * math.pow(self.GROWTH, bucket + 0.5)


_LOG_GROWTH = math.log(Histogram.GROWTH)


@dataclass(slots=True)
class Stats:
    started: float  # the clock's reading when the crawl began
    pages: int = 0  # pages printed
    links_found: int = 0  # links printed, summed over pages
    not_html: int = 0  # 2xx responses that weren't pages
    statuses: Counter[int] = field(default_factory=Counter)  # every response, redirects too
    ttfb: Histogram = field(default_factory=Histogram)  # every response's time to first byte
    failures: Counter[str] = field(default_factory=Counter)  # items that ended without a page
    retries: Counter[str] = field(default_factory=Counter)  # tries again, by what failed
    internal_errors: int = 0  # unexpected exceptions while processing an item
    body_bytes: int = 0  # pages' bodies as they arrived, compressed or not
    decoded_bytes: int = 0  # the bodies of the pages printed, decompressed
    truncated: int = 0  # pages printed whose bodies were cut short
    busiest_second: int = 0  # the most pages printed in one second of the crawl
    # The concurrency limit's lowest and highest values, and its last: None until it's set.
    limits: tuple[int, int, int] | None = None
    _second: int = field(default=-1, init=False)  # the second of the crawl now counted
    _in_second: int = field(default=0, init=False)  # pages printed in it

    def printed(self, now: float, links_found: int, decoded_bytes: int, truncated: bool) -> None:
        """A page's record was written, at now."""
        self.pages += 1
        self.links_found += links_found
        self.decoded_bytes += decoded_bytes
        self.truncated += truncated
        if (second := int(now - self.started)) != self._second:
            self._second, self._in_second = second, 0
        self._in_second += 1
        self.busiest_second = max(self.busiest_second, self._in_second)

    def limit(self, value: int) -> None:
        """The concurrency limit is value now."""
        low, high, _ = self.limits or (value, value, value)
        self.limits = (min(low, value), max(high, value), value)

    def summary(
        self,
        unique_urls: int,
        now: float,
        rejected: Mapping[str, int] | None = None,
        examples: Mapping[str, Sequence[str]] | None = None,
        peak_rss: int | None = None,
    ) -> str:
        """A few lines for stderr. unique_urls is the number of in-scope URLs found;
        rejected counts those a rule kept from being crawled, by rule, and examples has a
        few of each. peak_rss is the crawler's own peak resident set size, in bytes, if
        known."""
        elapsed = max(now - self.started, 0.0)
        rate = self.pages / elapsed if elapsed > 0 else 0.0
        lines = [
            f"{_count(self.pages, 'page')} in {elapsed:.1f} s ({rate:,.1f} pages/s), "
            f"{_count(self.links_found, 'link')} found, "
            f"{_count(unique_urls, 'unique in-scope URL')}"
        ]
        if self.statuses:
            lines.append(f"statuses: {_counts(sorted(self.statuses.items()))}")
        if self.ttfb.count:
            quantiles = ", ".join(
                f"p{round(q * 100)} {_duration(self.ttfb.quantile(q) or 0.0)}"
                for q in (0.5, 0.95, 0.99)
            )
            lines.append(f"time to first byte: {quantiles}")
        if self.body_bytes:
            line = (
                f"page bodies: {_size(self.body_bytes)} received, "
                f"{_size(self.decoded_bytes)} decompressed"
            )
            if self.truncated:
                line += f", {self.truncated:,} cut short"
            lines.append(line)
        skipped = sorted(self.failures.items())
        if self.not_html:
            skipped.insert(0, ("not HTML", self.not_html))
        if skipped:
            lines.append(f"not printed: {_counts(skipped)}")
        if self.retries:
            lines.append(f"retried: {_counts(sorted(self.retries.items()))}")
        if rejected:
            examples = examples or {}
            lines.append(
                "not crawled: "
                + ", ".join(
                    f"{rule} ({count:,}"
                    + (f", such as {e[0]})" if (e := examples.get(rule)) else ")")
                    for rule, count in sorted(rejected.items())
                )
            )
        if self.busiest_second:
            lines.append(f"busiest second: {_count(self.busiest_second, 'page')}")
        if self.limits is not None:
            low, high, last = self.limits
            span = f"{low}" if low == high else f"{low} to {high}, {last} at the end"
            lines.append(f"concurrency limit: {span}")
        if peak_rss is not None:
            lines.append(f"peak RSS: {peak_rss / 2**20:,.1f} MiB, parser processes aside")
        if self.internal_errors:
            lines.append(f"internal errors: {self.internal_errors:,}, logged above")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class Snapshot:
    """The crawl at a moment, for the progress line (DESIGN.md §10)."""

    pages: int  # printed
    rate: float  # pages printed a second, lately
    in_flight: int  # requests
    limit: int  # the concurrency limit
    queued: int  # items waiting for a fetch
    retrying: int  # items waiting to be tried again
    errors: int  # items that failed
    byte_rate: float  # bytes of page bodies received a second, lately
    busy: int  # parser processes at work
    workers: int  # parser processes at most, or 0 when pages are parsed inline
    paused: str | None = None  # why no request starts, if none does

    def line(self) -> str:
        """pages 48,210 · 431/s · in-flight 64/64 · queued 12,904 · retrying 3 · errors 17 ·
        7.6 MB/s · workers 2/9"""
        parts = [
            f"pages {self.pages:,}",
            f"{self.rate:,.0f}/s",
            f"in-flight {self.in_flight}/{self.limit}",
            f"queued {self.queued:,}",
            f"retrying {self.retrying:,}",
            f"errors {self.errors:,}",
            f"{_size(round(self.byte_rate))}/s",
        ]
        if self.workers:
            parts.append(f"workers {self.busy}/{self.workers}")
        if self.paused is not None:
            parts.append(f"paused: {self.paused}")
        return " · ".join(parts)


def _counts(pairs: list[tuple[str, int]] | list[tuple[int, int]]) -> str:
    return ", ".join(f"{name} ({count:,})" for name, count in pairs)


def _count(n: int, noun: str) -> str:
    return f"{n:,} {noun}" if n == 1 else f"{n:,} {noun}s"


def _duration(seconds: float) -> str:
    """12.3 ms, or 4.56 s from a second up."""
    return f"{seconds * 1000:.1f} ms" if seconds < 1 else f"{seconds:,.2f} s"


def _size(n: int) -> str:
    """A byte count in decimal units: 950 B, 12.3 kB, 4.5 MB, 1,234.5 GB."""
    if n < _THOUSAND:
        return f"{n} B"
    value = n / _THOUSAND
    for unit in ("kB", "MB"):
        if value < _THOUSAND:
            return f"{value:.1f} {unit}"
        value /= _THOUSAND
    return f"{value:,.1f} GB"


_THOUSAND = 1000
