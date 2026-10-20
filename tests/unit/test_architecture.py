"""A pure core and an async shell (docs/implementation-plan.md §1).

Modules that decide do no I/O and don't use asyncio; modules that act wire them to the
world. Every module not listed in SHELL is core, so a new module is held to the rule until
someone decides otherwise.
"""

import ast
from pathlib import Path

import pytest

import web_crawler

PACKAGE = Path(web_crawler.__file__).parent

SHELL = frozenset({"__main__", "cli", "engine", "fetcher", "output", "pool", "transport"})

# Imports that mean a module does I/O or runs concurrently. yarl isn't I/O, but URLs in the
# core go through ada-url: yarl rewrites them (DESIGN.md D5), so only the transport uses it.
FORBIDDEN = frozenset(
    {
        "aiohttp",
        "asyncio",
        "concurrent",
        "multiprocessing",
        "selectors",
        "socket",
        "ssl",
        "subprocess",
        "threading",
        "uvloop",
        "yarl",
    }
)

CORE = sorted(path.stem for path in PACKAGE.glob("*.py") if path.stem not in SHELL)


def imported_modules(source: str) -> set[str]:
    """Every module that source imports, with relative imports made absolute."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # Level 1 is the package itself: core modules are all top-level.
            module = (
                node.module
                if node.level == 0
                else ".".join(filter(None, ["web_crawler", node.module]))
            )
            assert module is not None
            found.add(module)
            # `from web_crawler import engine` imports a module, not a name.
            found.update(f"{module}.{alias.name}" for alias in node.names)
    return found


def violations(source: str) -> list[str]:
    bad = []
    for module in sorted(imported_modules(source)):
        parts = module.split(".")
        if parts[0] in FORBIDDEN or (
            parts[0] == "web_crawler" and parts[1:2] and parts[1] in SHELL
        ):
            bad.append(module)
    return bad


def test_the_core_is_every_module_outside_the_shell() -> None:
    assert {"__init__", "config", "types"} <= set(CORE)
    assert set(CORE).isdisjoint(SHELL)


@pytest.mark.parametrize("module", CORE)
def test_core_module_does_no_io(module: str) -> None:
    assert violations((PACKAGE / f"{module}.py").read_text()) == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import asyncio", ["asyncio"]),
        ("import concurrent.futures", ["concurrent.futures"]),
        ("from threading import Thread", ["threading", "threading.Thread"]),
        ("from web_crawler import engine", ["web_crawler.engine"]),
        (
            "from web_crawler.transport import Response",
            ["web_crawler.transport", "web_crawler.transport.Response"],
        ),
        ("from .fetcher import fetch", ["web_crawler.fetcher", "web_crawler.fetcher.fetch"]),
        ("from . import output", ["web_crawler.output"]),
        ("import web_crawler.cli", ["web_crawler.cli"]),
        ("from web_crawler.types import CrawlItem\nimport re, math", []),
    ],
)
def test_the_checker_catches_shell_and_io_imports(source: str, expected: list[str]) -> None:
    assert violations(source) == expected
