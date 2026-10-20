"""Test sites built from link graphs, and the output a correct crawl prints for them."""

from tests.support.bench import bfs
from tests.support.site import Resource, page


def path(node: int) -> str:
    return "/" if node == 0 else f"/p/{node}"


def href(origin: str, source: int, target: int) -> str:
    """A link from source to target, written one of three ways so that resolution matters:
    absolute, root-relative, or relative to /p/."""
    match (source + target) % 3:
        case 0:
            return origin + path(target)
        case 1:
            return path(target)
        case _:
            if target == 0:
                return "/" if source == 0 else "../"
            return f"p/{target}" if source == 0 else str(target)


def graph_site(origin: str, site: list[list[int]], external: bool = True) -> dict[str, Resource]:
    """A page per node, linking to its targets in order. Each page also links off the host,
    which a correct crawl prints and never requests."""
    resources = {}
    for source, targets in enumerate(site):
        hrefs = [href(origin, source, target) for target in targets]
        if external:
            hrefs.append(f"https://external.example/{source}")
        resources[path(source)] = page(*hrefs)
    return resources


def expected_records(origin: str, site: list[list[int]], external: bool = True) -> list[bytes]:
    """The record of each page reachable from the home page, sorted: the ground truth."""
    records = []
    for source in bfs(site):
        printed = list(dict.fromkeys(origin + path(target) for target in site[source]))
        if external:
            printed.append(f"https://external.example/{source}")
        lines = [origin + path(source), *(f"  {url}" for url in printed), "", ""]
        records.append("\n".join(lines).encode())
    return sorted(records)
