"""Appendix A.9: ProcessPoolExecutor behaviour behind DESIGN.md §4.3 and §5.4.

Usage: python bench/pool_check.py ondemand [--method M]          # are workers started only when needed?
       python bench/pool_check.py crash    [--method M]          # one worker dies: what happens to the rest?
       python bench/pool_check.py sigint   [--method M] [D ...]  # Ctrl-C D seconds after starting 8 workers

M is the multiprocessing start method: spawn (the macOS default) or forkserver (the Linux default).
sigint runs each trial in a child process that leads its own process group, so the SIGINT reaches that
child and its workers but not your shell. Each delay is tried without and with the fix: SIGINT blocked
around submit(), then ignored and unblocked by the worker's initializer.
"""
import argparse
import multiprocessing
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool


def ignore_sigint():
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def ignore_sigint_and_unblock():
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT})


def job(i, crash=False, t=0.3):
    time.sleep(t)
    if crash:
        os._exit(1)  # stands in for a segfault or an OOM kill while parsing one page
    return i


def new_pool(method, initializer=ignore_sigint):
    ctx = multiprocessing.get_context(method)
    return ProcessPoolExecutor(max_workers=8, mp_context=ctx, initializer=initializer)


def started(pool):
    return len(pool._processes)  # private, but the only way to count the workers started so far


def check_ondemand(method):
    pool = new_pool(method)
    pool.submit(job, 0).result()
    print(f"[{method}] after 1 job: {started(pool)} worker(s)")
    for i in range(3):
        pool.submit(job, i).result()
    print(f"[{method}] after 3 more jobs, one at a time: {started(pool)} worker(s)")
    wait([pool.submit(job, i) for i in range(8)])
    print(f"[{method}] after 8 jobs at once: {started(pool)} worker(s)")
    pool.shutdown()


def check_crash(method):
    pool = new_pool(method)
    wait([pool.submit(job, i) for i in range(8)])  # start all 8 workers
    # 16 jobs on 8 workers: 8 running, 8 queued. Job 3 kills its worker early; the rest take 0.5 s.
    futs = [pool.submit(job, i, crash=(i == 3), t=(0.05 if i == 3 else 0.5)) for i in range(16)]
    wait(futs)
    broken = sum(isinstance(f.exception(), BrokenProcessPool) for f in futs)
    print(f"[{method}] 1 of 16 jobs killed its worker -> {broken} of 16 futures failed with BrokenProcessPool")
    try:
        pool.submit(job, 99).result()
        print(f"[{method}] the pool still accepts jobs")
    except BrokenProcessPool:
        print(f"[{method}] the pool refuses every later job with BrokenProcessPool")
    pool.shutdown()


def sigint_trial(method, delay, fixed):
    """One trial, run in a child process that leads its own process group."""
    signal.signal(signal.SIGINT, lambda *_: None)  # stands in for the coordinator's own handler
    pool = new_pool(method, ignore_sigint_and_unblock if fixed else ignore_sigint)
    futs = []
    for i in range(8):  # no worker is idle, so each submit starts one
        old = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT}) if fixed else None
        futs.append(pool.submit(job, i))  # a new worker inherits the signal mask
        if old is not None:
            signal.pthread_sigmask(signal.SIG_SETMASK, old)
    time.sleep(delay)
    os.killpg(os.getpgrp(), signal.SIGINT)  # what the terminal does on Ctrl-C
    wait(futs)
    ok = sum(f.exception() is None for f in futs)
    broken = sum(isinstance(f.exception(), BrokenProcessPool) for f in futs)
    label = "with fix   " if fixed else "without fix"
    print(f"[{method}] {label} SIGINT at +{delay * 1e3:3.0f} ms: {ok}/8 jobs finished, {broken}/8 BrokenProcessPool")
    pool.shutdown()


def check_sigint(method, delays):
    for fixed in (False, True):
        for d in delays:
            r = subprocess.run(
                [sys.executable, __file__, "sigint-trial", "--method", method, str(d), str(int(fixed))],
                start_new_session=True, capture_output=True, text=True, timeout=60, check=False,
            )
            print(r.stdout.strip(), f"| KeyboardInterrupt tracebacks: {r.stderr.count('KeyboardInterrupt')}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="ProcessPoolExecutor checks for DESIGN.md Appendix A.9")
    ap.add_argument("mode", choices=["ondemand", "crash", "sigint", "sigint-trial"])
    ap.add_argument("--method", choices=["spawn", "forkserver"], default="spawn")
    ap.add_argument("args", nargs="*", type=float)
    a = ap.parse_args()
    if a.mode == "ondemand":
        check_ondemand(a.method)
    elif a.mode == "crash":
        check_crash(a.method)
    elif a.mode == "sigint":
        check_sigint(a.method, a.args or [0.005, 0.01, 0.02, 0.03, 0.05])
    else:
        sigint_trial(a.method, a.args[0], fixed=a.args[1] == 1)
