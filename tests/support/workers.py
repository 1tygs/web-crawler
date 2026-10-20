"""Targets for the parser pool in tests: parse_in_worker, with misbehaviour scripted by the
page's URL, which is all a job carries to the worker that runs it.

?sleep=S                 sleep S seconds first, as a big page's parse takes time
?kill=always             SIGKILL the worker, as a crash in a parser library would
?kill=once&marker=PATH   SIGKILL it the first time only, as a kill -9 from outside does
marker=PATH              with sleep, created as the parse starts
"""

import os
import signal
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from web_crawler.pool import parse_in_worker
from web_crawler.types import ParseJob, ParseResult


def scripted(job: ParseJob) -> ParseResult:
    query = {name: values[-1] for name, values in parse_qs(urlsplit(job.url).query).items()}
    marker = Path(query["marker"]) if "marker" in query else None
    kill = query.get("kill")
    if kill == "always" or (kill == "once" and marker is not None and not marker.exists()):
        if marker is not None:
            marker.touch()
        os.kill(os.getpid(), signal.SIGKILL)
    if "sleep" in query:
        if marker is not None:
            marker.touch()
        time.sleep(float(query["sleep"]))
    return parse_in_worker(job)


def worker_pid(job: ParseJob) -> ParseResult:
    """The worker's process ID, as the record, after any sleep the URL asks for.

    With rendezvous=DIR&count=N, it first waits for N jobs to be running at once, each in a
    worker of its own: it leaves its process ID in DIR, and waits for N of them there.
    """
    query = {name: values[-1] for name, values in parse_qs(urlsplit(job.url).query).items()}
    if "sleep" in query:
        time.sleep(float(query["sleep"]))
    if "rendezvous" in query:
        place = Path(query["rendezvous"])
        (place / str(os.getpid())).touch()
        deadline = time.monotonic() + 10
        while len(list(place.iterdir())) < int(query["count"]) and time.monotonic() < deadline:
            time.sleep(0.01)
    return ParseResult(str(os.getpid()).encode(), [], 0, 0, None)
