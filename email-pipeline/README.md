# email-pipeline

Drag an Outlook email into your inbox folder; the pipeline classifies it,
runs the matching process with Claude, and posts the result to Teams.
Architecture and rules: see `CLAUDE.md`.

## Setup (Windows)
1. Install uv: https://docs.astral.sh/uv/
2. `git clone <repo>` and `cd email-pipeline`
3. `uv sync`
4. Copy `config.example.toml` to `config.toml` and set your folders.
5. Set `TEAMS_WEBHOOK_URL` as a user environment variable (optional; without it
   notifications only go to the log).
6. Make sure you are logged in to Claude Code (`claude` once in a terminal),
   or set `ANTHROPIC_API_KEY`.
7. `uv run pytest` then `uv run email-pipeline --config config.toml --once`

## First end-to-end test
Send yourself an email with subject `[PIPELINE TEST] hello` and an attachment,
drag it from Outlook into the inbox folder, and run with `--once`. Check
`<jobs_dir>/<job>/output/smoke_test/` and Teams.

## Job states
`new -> preprocessed -> classified -> running -> done | failed | needs_review`
(see `manifest.json` in each job folder).
