"""What happened during a crawl: counters and the summary on stderr (DESIGN.md §10).

Pure: the engine records events and passes the time in.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field


@dataclass(slots=True)
class Stats:
    started: float  # the clock's reading when the crawl began
    pages: int = 0  # pages printed
    links_found: int = 0  # links printed, summed over pages
    not_html: int = 0  # 2xx responses that weren't pages
    statuses: Counter[int] = field(default_factory=Counter)  # every response, redirects too
    failures: Counter[str] = field(default_factory=Counter)  # items that ended without a page
    retries: Counter[str] = field(default_factory=Counter)  # tries again, by what failed
    internal_errors: int = 0  # unexpected exceptions while processing an item
    body_bytes: int = 0  # pages' bodies as they arrived, compressed or not
    decoded_bytes: int = 0  # the bodies of the pages printed, decompressed
    truncated: int = 0  # pages printed whose bodies were cut short

    def summary(
        self,
        unique_urls: int,
        now: float,
        rejected: Mapping[str, int] | None = None,
        examples: Mapping[str, Sequence[str]] | None = None,
    ) -> str:
        """A few lines for stderr. unique_urls is the number of in-scope URLs found;
        rejected counts those a rule kept from being crawled, by rule, and examples has a
        few of each."""
        elapsed = max(now - self.started, 0.0)
        rate = self.pages / elapsed if elapsed > 0 else 0.0
        lines = [
            f"{_count(self.pages, 'page')} in {elapsed:.1f} s ({rate:,.1f} pages/s), "
            f"{_count(self.links_found, 'link')} found, "
            f"{_count(unique_urls, 'unique in-scope URL')}"
        ]
        if self.statuses:
            lines.append(f"statuses: {_counts(sorted(self.statuses.items()))}")
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
        if self.internal_errors:
            lines.append(f"internal errors: {self.internal_errors:,}, logged above")
        return "\n".join(lines)


def _counts(pairs: list[tuple[str, int]] | list[tuple[int, int]]) -> str:
    return ", ".join(f"{name} ({count:,})" for name, count in pairs)


def _count(n: int, noun: str) -> str:
    return f"{n:,} {noun}" if n == 1 else f"{n:,} {noun}s"


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
