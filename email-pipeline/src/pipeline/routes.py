"""Route discovery: every routes/<name>/ROUTE.md is a route."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

DEFAULT_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep"]
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)


@dataclass(frozen=True)
class Route:
    name: str
    description: str
    instructions: str
    dir: Path
    examples: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=lambda: list(DEFAULT_TOOLS))


def parse_route_file(path: Path) -> Route:
    text = path.read_text(encoding="utf-8")
    m = _FRONTMATTER.match(text)
    if not m:
        raise ValueError(f"{path}: missing YAML frontmatter")
    meta = yaml.safe_load(m.group(1)) or {}
    name = meta.get("name")
    if name != path.parent.name:
        raise ValueError(f"{path}: frontmatter name '{name}' must equal folder name '{path.parent.name}'")
    if not meta.get("description"):
        raise ValueError(f"{path}: 'description' is required")
    return Route(
        name=name,
        description=str(meta["description"]).strip(),
        instructions=m.group(2).strip(),
        dir=path.parent,
        examples=list(meta.get("examples") or []),
        allowed_tools=list(meta.get("allowed_tools") or DEFAULT_TOOLS),
    )


def load_routes(routes_dir: Path) -> dict[str, Route]:
    routes: dict[str, Route] = {}
    for f in sorted(Path(routes_dir).glob("*/ROUTE.md")):
        try:
            r = parse_route_file(f)
            routes[r.name] = r
        except Exception as exc:
            log.error("skipping invalid route %s: %s", f, exc)
    return routes
