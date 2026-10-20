"""``python -m web_crawler``: the same entry point as the ``crawl`` command."""

import sys

from web_crawler.cli import main

# The guard matters: a process pool started with spawn re-imports the main module in each
# worker, and a worker must not start a crawl of its own.
if __name__ == "__main__":
    sys.exit(main())
