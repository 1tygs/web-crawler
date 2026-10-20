"""Random sources whose jitter sits at a known point of its range, so that backoffs are known."""

import random


class Lowest(random.Random):
    """No jitter at all: every backoff is 0, so retries happen at once."""

    def uniform(self, a: float, b: float) -> float:
        return a


class Highest(random.Random):
    """Jitter at the top of its range: every backoff is its cap."""

    def uniform(self, a: float, b: float) -> float:
        return b


class Fixed(random.Random):
    """Jitter at a fixed share of its range: share 0.1 makes every backoff a tenth of its
    cap."""

    def __init__(self, share: float) -> None:
        super().__init__()
        self.share = share

    def uniform(self, a: float, b: float) -> float:
        return a + (b - a) * self.share
