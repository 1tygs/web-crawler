import pytest


@pytest.fixture(params=[0, 2], ids=["inline", "2 workers"])
def parse_workers(request: pytest.FixtureRequest) -> int:
    """How many parser processes a crawl has. Tests that take this run twice, parsing inline
    and in a pool of two processes, and must print the same either way."""
    return int(request.param)
