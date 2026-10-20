"""Test sites built from link graphs, and the output a correct crawl prints for them."""

import asyncio
from collections.abc import Mapping

from aiohttp import web

from tests.support.bench import bfs
from tests.support.site import Handler, Resource, page


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


def graph_site(
    origin: str,
    site: list[list[int]],
    external: bool = True,
    delays: Mapping[int, float] | None = None,
) -> dict[str, Resource | Handler]:
    """A page per node, linking to its targets in order. Each page also links off the host,
    which a correct crawl prints and never requests. A node in delays answers that many
    seconds late."""
    resources: dict[str, Resource | Handler] = {}
    for source, targets in enumerate(site):
        hrefs = [href(origin, source, target) for target in targets]
        if external:
            hrefs.append(f"https://external.example/{source}")
        resource = page(*hrefs)
        delay = (delays or {}).get(source, 0.0)
        resources[path(source)] = _late(resource, delay) if delay else resource
    return resources


def _late(resource: Resource, delay: float) -> Handler:
    async def handle(request: web.Request) -> web.StreamResponse:
        await asyncio.sleep(delay)
        return web.Response(status=resource.status, body=resource.body, headers=resource.headers)

    return handle


def expected_records(
    origin: str, site: list[list[int]], external: bool = True, max_depth: int | None = None
) -> list[bytes]:
    """The record of each page reachable from the home page, within max_depth links of it
    if that's given, sorted: the ground truth."""
    records = []
    for source, depth in bfs(site).items():
        if max_depth is not None and depth > max_depth:
            continue
        printed = list(dict.fromkeys(origin + path(target) for target in site[source]))
        if external:
            printed.append(f"https://external.example/{source}")
        lines = [origin + path(source), *(f"  {url}" for url in printed), "", ""]
        records.append("\n".join(lines).encode())
    return sorted(records)
