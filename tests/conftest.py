import os

from hypothesis import settings

# CI runs a fixed sequence of examples, so a red build always reproduces; local runs explore
# new examples each time. Select with HYPOTHESIS_PROFILE.
settings.register_profile("ci", derandomize=True, deadline=None, print_blob=True)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))
