"""Running the crawl command, as a user would, and reading what it prints."""

import asyncio
import sys
from pathlib import Path

# The console script that `uv sync` installs next to the interpreter.
CRAWL = str(Path(sys.executable).with_name("crawl"))


async def run(*args: str) -> tuple[int, bytes, str]:
    """Run a command; return its exit status, stdout and stderr."""
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    assert process.returncode is not None
    return process.returncode, stdout, stderr.decode()


def split_records(stdout: bytes) -> list[bytes]:
    """stdout as whole records, sorted. Each record ends with a blank line, and no line
    is blank otherwise."""
    *records, rest = stdout.split(b"\n\n")
    assert rest == b"", "the output ends inside a record"
    return sorted(record + b"\n\n" for record in records)
