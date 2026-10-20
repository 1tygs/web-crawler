"""The simulations in bench/, loaded as modules, so that tests and the evidence behind
DESIGN.md Appendix A share one implementation (docs/implementation-plan.md §4)."""

import importlib.util
import random
from pathlib import Path
from types import ModuleType
from typing import cast

BENCH = Path(__file__).resolve().parents[2] / "bench"


def load(name: str) -> ModuleType:
    """bench/<name>.py as a module. The scripts are run as programs, so they aren't on
    the import path."""
    spec = importlib.util.spec_from_file_location(f"bench_{name}", BENCH / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_depth_sim = load("depth_sim")


def make_site(seed: int, pages: int, branching: float, cross_links: int) -> list[list[int]]:
    """A random site as adjacency lists: page i links to the pages in site[i], in order,
    duplicates included. Every page links home. Page 0 is the home page (A.10)."""
    rng = random.Random(seed)
    return cast(list[list[int]], _depth_sim.make_site(rng, pages, branching, cross_links))


def bfs(site: list[list[int]]) -> dict[int, int]:
    """Each page reachable from page 0, with its depth."""
    return cast(dict[int, int], _depth_sim.bfs(site))
