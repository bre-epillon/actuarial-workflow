# Email pipeline — developer guide for Claude Code

This file is for *developing* this repo. It is never shown to the pipeline's
runtime agents (they get only their route's `ROUTE.md`). User-facing setup
and usage: `README.md`.

## What this is
A local, folder-watching pipeline for actuarial work at a small reinsurer.
The user drags relevant Outlook emails (`.msg`) or files into an inbox folder.
The pipeline turns each item into a *job*, preprocesses it deterministically,
classifies it into one or more *routes* with Claude, runs a route agent
(Claude Agent SDK) per route, checks and post-processes the agent's output
with deterministic code, and sends a Teams notification.

Runs on Windows. Python >= 3.11. Managed with `uv`. Each team member runs it
on their own PC with their own Claude subscription (Claude Code login via the
CLI bundled in `claude-agent-sdk`; no API key).

## Flow
inbox item -> job dir (copy of original) -> preprocess -> classify -> run route(s)
-> validate `extracted.json` (one repair round) -> route postprocess -> notify

Job states (stored in `manifest.json`): `new` -> `preprocessed` -> `classified`
-> `running` -> `done` | `failed` | `needs_review`. Every step is resumable from
the manifest; on startup, jobs stuck in `running` go back to `classified`.
`AgentUnavailable` (logged out, usage limit, outage) pauses the loop without
touching job states. Everything else is isolated per item: an inbox item that
cannot be ingested stays in the inbox, and preprocessing runs in a child process
(`preprocess_isolated`, `preprocess_timeout`) so a parser that hangs or crashes
fails only its own job. `--once` first waits for items still being copied.

## Layout
- `src/pipeline/config.py`    — loads `config.toml` (per user, gitignored)
- `src/pipeline/jobs.py`      — job dirs, manifest, content hashing, dedup, `safe_name`
- `src/pipeline/preprocess.py`— .msg/.eml parsing (nested emails, zip), Excel -> CSV,
                                PDF/Word -> TXT, HTML bodies -> text; `extracted/summary.json`
- `src/pipeline/routes.py`    — discovers `routes/*/ROUTE.md` (YAML frontmatter + instructions)
- `src/pipeline/agent.py`     — thin wrapper around `claude_agent_sdk.query`: isolation
                                defaults, transcript logging, error classification
- `src/pipeline/classifier.py`— classification prompt from the routes; structured output
- `src/pipeline/runner.py`    — runs one route agent, validates, repairs, postprocesses
- `src/pipeline/guard.py`     — PreToolUse hook: reads inside the job, writes inside output/<route>/
- `src/pipeline/amounts.py`   — deterministic reading of amounts/percentages as written
- `src/pipeline/evidence.py`  — checks that a quoted text really occurs in the cited file
- `src/pipeline/versions.py`  — versions for the manifest (incl. git commits of pinned tools)
- `src/pipeline/notify.py`    — Teams Workflows webhook (Adaptive Card)
- `src/pipeline/main.py`      — polling loop, orchestration, CLI; `commands.py` — CLI subcommands
- `routes/<name>/`            — one folder per business process: `ROUTE.md`, optional
                                `schema.json` and `postprocess.py`
- `tests/`                    — offline tests (fake agents); `fixtures/` synthetic emails made
                                by `fixtures/make_fixtures.py`; `classifier_cases.yaml`

## Non-negotiable rules
1. **No arithmetic by the LLM for actuarial numbers.** Agents extract inputs
   into JSON validated against a schema; deterministic code computes results.
   Amounts are extracted twice (`value` and verbatim `as_written`) and
   `amounts.check_amount` must agree, and every quote must occur in the file it
   cites (`evidence.quote_problem`), else the run is flagged for review.
2. **Data never enters git.** Inbox, jobs, reports, `.env`, `config.toml`,
   `samples/` and data file types are gitignored. Test fixtures must be
   synthetic or anonymized (rewrite real samples in `make_fixtures.py`).
3. **Runtime agents load no filesystem settings**: build options with
   `agent.isolated_options()` (`setting_sources=[]`, `strict_mcp_config`,
   `skills=[]`, `verbatim_prompts`, no session persistence); instructions are
   passed explicitly via `system_prompt`.
4. **Least privilege per route**: `allowed_tools` comes from the route's
   frontmatter and is passed as both `tools` (what exists) and `allowed_tools`
   (what runs unprompted), with `permission_mode="dontAsk"`, the guard hook and
   the CLI's `--restricted` mode. No web tools (rejected at load); no Bash unless
   the route explicitly needs it (the guard cannot police shell commands).
   Agent `cwd` is the job directory, always as a fully resolved path.
5. **Original files are read-only.** Agents write only to `output/<route>/`
   (enforced by `guard.py`, not just by the prompt).
6. **Pin external tools** (e.g. LargeLossesNetting) to a tag/commit in
   `pyproject.toml` and report `versions.package_version(...)` from the route's
   postprocess, so the version lands in the manifest.
7. The `claude_agent_sdk` API evolves quickly. Before relying on a class,
   option or field, check it against the installed version
   (`uv pip show claude-agent-sdk`, read the package source/changelog).
   Last checked: claude-agent-sdk 0.2.174 with bundled Claude Code CLI 2.1.310.
   After upgrading: `uv run python -m pytest`, then `selftest` and `eval-classifier`.

## Adding a route
Create `routes/<name>/ROUTE.md` with frontmatter:
```yaml
---
name: <name>            # must equal the folder name
description: <one paragraph: what emails belong here and which do NOT>
examples: [<typical subject lines>]
allowed_tools: [Read, Write, Glob, Grep]   # Edit too if it rewrites its own files
model: sonnet           # optional; overrides config route_model
output_schema: schema.json     # optional: agent must write output/<name>/extracted.json
postprocess: postprocess.py    # optional (needs output_schema)
---
```
followed by the instructions for the agent (under 20k characters: they travel
on the command line). The runner adds the common task (folder layout,
`summary.md` contract, the schema). `postprocess.py` defines
`run(extracted: dict, out_dir: Path) -> dict` returning optional `facts`
(shown on the Teams card), `flags` (findings for the reviewer) and `versions`;
it may write files to `out_dir` (e.g. `computed.json`). Raise to fail the run.

Routes are reloaded every poll; no restart needed. Add a synthetic sample to
`tests/fixtures/make_fixtures.py` (regenerate with
`uv run python tests/fixtures/make_fixtures.py`) and a case in
`tests/classifier_cases.yaml`; `pytest` checks every route has one. Then
`uv run python -m pipeline eval-classifier`.

## Conventions
- Paths via `pathlib`, UTF-8 everywhere, Windows-safe file names (`jobs.safe_name`).
- Keep paths short (MAX_PATH is 260 on our laptops): flat `extracted/`, names truncated.
- Log to console and `<jobs_dir>/pipeline.log`; agent transcripts to `<job>/logs/*.jsonl`.
- Tests: `uv run python -m pytest`. Tests must not call the Claude API (see `FakeAgents` in
  `tests/conftest.py`). Live checks are explicit commands: `selftest`, `eval-classifier`.
- Docs and messages say `uv run python -m pipeline ...` and `uv run python -m pytest`: Defender's ASR rule
  blocks the launcher .exe files uv generates (`email-pipeline.exe`, `pytest.exe`) on company laptops.

## Environment gotchas (Barents laptops)
- Corporate TLS interception: `[tool.uv] system-certs = true` in `pyproject.toml`.
- Defender ASR ("block executables unless prevalent/trusted") blocks uv-generated
  launchers and the python.exe of uv-downloaded Pythons: hence `python -m ...`
  everywhere and `python-preference = "only-system"` (python.org Python, signed).
- `%TEMP%` can contain an 8.3 short name (`C:\Users\FIRSTN~1\...`); `Job` resolves its
  directory because a short-name cwd makes Claude Code deny writes inside it.
- Live experiments: use a short folder (e.g. `%TEMP%\ep-live`) for inbox/jobs.
