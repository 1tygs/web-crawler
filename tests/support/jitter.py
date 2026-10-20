"""Random sources whose jitter sits at one end of its range, so that backoffs are known."""

import random


class Lowest(random.Random):
    """No jitter at all: every backoff is 0, so retries happen at once."""

    def uniform(self, a: float, b: float) -> float:
        return a


class Highest(random.Random):
    """Jitter at the top of its range: every backoff is its cap."""

    def uniform(self, a: float, b: float) -> float:
        return b
