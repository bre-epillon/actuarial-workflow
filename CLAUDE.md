# Email pipeline — developer guide for Claude Code

This file is for *developing* this repo. It is never shown to the pipeline's
runtime agents (they get only their route's `ROUTE.md`).

## What this is
A local, folder-watching pipeline for actuarial work at a small reinsurer.
The user drags relevant Outlook emails (`.msg`) or files into an inbox folder.
The pipeline turns each item into a *job*, preprocesses it deterministically,
classifies it into one or more *routes* with Claude, then runs a route agent
(Claude Agent SDK) per route, and sends a Teams notification.

Runs on Windows. Python >= 3.11. Managed with `uv`.

## Flow
inbox item -> job dir (copy of original) -> preprocess -> classify -> run route(s) -> notify

Job states (stored in `manifest.json`): `new` -> `preprocessed` -> `classified`
-> `running` -> `done` | `failed` | `needs_review`. Every step is resumable from
the manifest; on startup, jobs stuck in `running` go back to `classified`.

## Layout
- `src/pipeline/config.py`    — loads `config.toml` (per user, gitignored)
- `src/pipeline/jobs.py`      — job dirs, manifest, content hashing, dedup
- `src/pipeline/preprocess.py`— .msg/.eml parsing, attachments, Excel -> CSV
- `src/pipeline/routes.py`    — discovers `routes/*/ROUTE.md` (YAML frontmatter + instructions)
- `src/pipeline/agent.py`     — thin wrapper around `claude_agent_sdk.query`
- `src/pipeline/classifier.py`— builds the classification prompt from the routes
- `src/pipeline/runner.py`    — runs one route agent inside a job dir
- `src/pipeline/notify.py`    — Teams Workflows webhook (Adaptive Card)
- `src/pipeline/main.py`      — polling loop, orchestration
- `routes/<name>/ROUTE.md`    — one folder per business process

## Non-negotiable rules
1. **No arithmetic by the LLM for actuarial numbers.** Agents extract inputs
   into JSON validated against a schema; deterministic code computes results.
2. **Data never enters git.** Inbox, jobs, reports, `.env`, `config.toml` are gitignored.
   Test fixtures must be anonymized.
3. **Runtime agents load no filesystem settings**: always `setting_sources=[]`,
   instructions passed explicitly via `system_prompt`.
4. **Least privilege per route**: `allowed_tools` comes from the route's
   frontmatter; no web tools; no Bash unless the route explicitly needs it.
   Agent `cwd` is the job directory.
5. **Original files are read-only.** Agents write only to `output/<route>/`.
6. **Pin external tools** (e.g. LargeLossesNetting) to a tag/commit and record
   the version in the manifest.
7. The `claude_agent_sdk` API evolves quickly. Before relying on a class,
   option or field, check it against the installed version
   (`uv pip show claude-agent-sdk`, read the package source/changelog).

## Adding a route
Create `routes/<name>/ROUTE.md` with frontmatter:
```yaml
---
name: <name>            # must equal the folder name
description: <one paragraph: what emails belong here and which do NOT>
examples: [<typical subject lines>]
allowed_tools: [Read, Write, Edit, Glob, Grep]
---
```
followed by the instructions for the agent. Routes are reloaded every poll;
no restart needed. Add an anonymized sample to `tests/fixtures/` and a
classifier regression case.

## Conventions
- Paths via `pathlib`, UTF-8 everywhere, Windows-safe file names (see `jobs.safe_name`).
- Log to console and `<jobs_dir>/pipeline.log`.
- Tests: `uv run pytest`. Tests must not call the Claude API.
