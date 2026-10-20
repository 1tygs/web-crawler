import os

import pytest
from hypothesis import settings

# CI runs a fixed sequence of examples, so a red build always reproduces; local runs explore
# new examples each time. Select with HYPOTHESIS_PROFILE.
settings.register_profile("ci", derandomize=True, deadline=None, print_blob=True)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))


@pytest.fixture(autouse=True)
def _no_proxy_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # The transport reads HTTP_PROXY and friends; a developer's proxy mustn't reach the test
    # servers. Tests of proxy support set the variables themselves.
    for name in list(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)
