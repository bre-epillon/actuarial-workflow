"""Route discovery: every routes/<name>/ROUTE.md is a route.

Frontmatter (see CLAUDE.md, "Adding a route"):
    name, description, examples, allowed_tools
    model          optional model alias for this route (overrides config route_model)
    output_schema  optional JSON Schema file in the route folder; the agent must write
                   output/<name>/extracted.json that validates against it
    postprocess    optional Python file in the route folder defining
                   run(extracted: dict, out_dir: Path) -> dict; deterministic code the
                   pipeline runs on the validated extracted.json
"""
from __future__ import annotations

import importlib.util
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml
from jsonschema.validators import validator_for

log = logging.getLogger(__name__)

DEFAULT_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep"]
MAX_INSTRUCTIONS = 20000  # passed on the command line; Windows caps it at 32k characters
KNOWN_TOOLS = {"Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "NotebookEdit", "TodoWrite", "Bash", "PowerShell"}
FORBIDDEN_TOOLS = {"WebFetch", "WebSearch"}  # rule 4: route agents never go online
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)


@dataclass(frozen=True)
class Route:
    name: str
    description: str
    instructions: str
    dir: Path
    examples: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=lambda: list(DEFAULT_TOOLS))
    model: str | None = None
    output_schema: dict | None = None
    postprocess: Path | None = None


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
    if len(m.group(2)) > MAX_INSTRUCTIONS:
        raise ValueError(f"{path}: instructions longer than {MAX_INSTRUCTIONS} characters")

    tools = list(meta.get("allowed_tools") or DEFAULT_TOOLS)
    if bad := set(tools) & FORBIDDEN_TOOLS:
        raise ValueError(f"{path}: tools not allowed for route agents: {sorted(bad)}")
    if unknown := set(tools) - KNOWN_TOOLS:
        raise ValueError(f"{path}: unknown tools {sorted(unknown)} (known: {sorted(KNOWN_TOOLS)})")

    schema = None
    if meta.get("output_schema"):
        schema_file = path.parent / meta["output_schema"]
        schema = json.loads(schema_file.read_text(encoding="utf-8"))
        validator_for(schema).check_schema(schema)  # raises SchemaError on a broken schema
        if "Write" not in tools:
            raise ValueError(f"{path}: output_schema needs the Write tool")

    postprocess = None
    if meta.get("postprocess"):
        postprocess = path.parent / meta["postprocess"]
        if not postprocess.is_file():
            raise ValueError(f"{path}: postprocess file {postprocess.name} not found")
        if schema is None:
            raise ValueError(f"{path}: postprocess needs output_schema (it runs on extracted.json)")

    return Route(
        name=name,
        description=str(meta["description"]).strip(),
        instructions=m.group(2).strip(),
        dir=path.parent,
        examples=list(meta.get("examples") or []),
        allowed_tools=tools,
        model=meta.get("model") or None,
        output_schema=schema,
        postprocess=postprocess,
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


def load_postprocess(route: Route) -> Callable[[dict, Path], dict[str, Any]]:
    """Import the route's postprocess file afresh (routes are hot-reloaded)."""
    spec = importlib.util.spec_from_file_location(f"route_postprocess_{route.name}", route.postprocess)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.run
