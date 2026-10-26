# email-pipeline

Drag an Outlook email (or any file) into your inbox folder. The pipeline reads
it, works out which business process it belongs to, lets Claude extract what
that process needs, checks and computes the numbers with ordinary code, and
posts a summary to Teams.

```
inbox item ─► job folder ─► preprocess ─► classify ─► route agent ─► check & compute ─► Teams
              (copy)        (no AI:       (Claude)    (Claude, only   (no AI: schema,
                             text, CSV)                the job folder)  amounts, figures)
```

Everything runs on your own PC with your own Claude subscription: no API key,
no server. Developer guide and design rules: [CLAUDE.md](CLAUDE.md).

## What you need

- Windows 10/11 (macOS/Linux should work but are untested)
- Access to this GitHub repository and `git`
- A Claude subscription (Pro, Max, Team or Enterprise). The Claude Code engine
  ships inside the Python package, so nothing else to install.
- Python 3.11 or newer from python.org (`winget install -e --id Python.Python.3.13`).
  The project deliberately uses this installed Python: Defender blocks the
  Python builds uv would otherwise download.
- [uv](https://docs.astral.sh/uv/), the package manager

## Setup (once, about 10 minutes)

Open PowerShell or Windows Terminal.

1. Install uv, then open a **new** terminal:
   ```powershell
   winget install --id=astral-sh.uv -e
   ```
   (Or `pip install uv` if you already have Python.)
2. Clone the repository **outside OneDrive** (OneDrive would sync the
   ~300 MB environment and can lock files):
   ```powershell
   git clone https://github.com/bre-epillon/actuarial-workflow.git C:\dev\actuarial-workflow
   cd C:\dev\actuarial-workflow\email-pipeline
   ```
3. Install the dependencies (creates `.venv` with Python and the Claude Code engine):
   ```powershell
   uv sync
   ```
4. Create your personal config. The defaults work; edit it later for Teams:
   ```powershell
   copy config.example.toml config.toml
   ```
5. Log in with your Claude account (a browser window opens; if asked, pick the Barents organisation):
   ```powershell
   uv run python -m pipeline login
   ```
6. Check everything, then run the self-test (about one minute, a few cents of your Claude quota):
   ```powershell
   uv run python -m pipeline doctor
   uv run python -m pipeline selftest
   ```

Why `python -m pipeline` and not `email-pipeline`? Microsoft Defender on our
laptops blocks the small launcher `.exe` that uv generates ("Access is
denied"), so every command here goes through Python itself. On other machines
`uv run email-pipeline ...` is equivalent.

## First run

1. Copy `tests\fixtures\smoke_test.eml` into your inbox folder
   (`%USERPROFILE%\claude_pipeline\inbox` by default).
2. Run one pass (it waits for files that are still being copied, then
   processes every item in the inbox one by one):
   ```powershell
   uv run python -m pipeline --once
   ```
3. Look at `%USERPROFILE%\claude_pipeline\jobs\<date>_<id>\output\smoke_test\`
   (and the Teams channel, if configured).

Then try a real email: drag it from Outlook into the inbox folder.

## Daily use

- **Start** the pipeline with `scripts\start-pipeline.cmd` (double-click) and
  leave the window open. To start it at logon: Win+R, `shell:startup`, and put
  a shortcut to that file in the folder that opens.
- **Drop** emails from Outlook into the inbox folder; pin the folder to Quick
  Access in Explorer to make that easy. Files that belong together: put them
  in one folder and drop the folder. Each item becomes one job; dropping the
  same item twice is detected and skipped. An item that cannot be read (or
  takes longer than `preprocess_timeout`) fails on its own; the others carry on.
- **Results** arrive as a Teams card and in the job folder.

| Command (`uv run python -m pipeline ...`) | What it does |
|---|---|
| *(nothing)* / `--once` | watch the inbox / process it once |
| `status` | the last 20 jobs and their state (`--all` for all) |
| `retry <job>` | run a failed job again (`--from new` redoes everything) |
| `assign <job> <route>` | send a job to a route by hand (e.g. after `needs_review`) |
| `doctor` | check install, login, config, folders, routes (`--notify-test` posts a test card) |
| `login` | log in to Claude |
| `selftest` | live check that agents work and cannot leave their job folder |
| `eval-classifier` | live check of the classifier on the test emails |

`<job>` can be the full id or its last 8 characters.

### Job states

`new -> preprocessed -> classified -> running -> done | failed | needs_review`

- `needs_review`: no process matched with enough confidence. Look at the
  Teams card, then `assign` it or ignore it.
- `failed`: the agent or a check failed (reason in the card and in
  `manifest.json`). Fix the cause, then `retry`.
- If Claude is unavailable (usage limit reached, logged out, outage) the
  pipeline pauses and resumes by itself; jobs are not marked failed.

### What is in a job folder

```
<jobs_dir>/2026-10-26_ab12cd34/
  original/            the item as dropped (never modified)
  extracted/           email text, attachments, CSV/TXT versions, summary.json
  output/<route>/      what the route produced: summary.md, extracted.json, computed.json, ...
  logs/                full transcript of every Claude run (audit trail)
  manifest.json        state, history, classification, models and versions used
```

## Teams notifications (optional)

1. In Teams, open the channel: **...** > **Workflows** > *"Post to a channel
   when a webhook request is received"*, finish the wizard, copy the URL.
2. Put it in `config.toml` as `teams_webhook_url = "..."` (never in a
   committed file: anyone with the URL can post to the channel).
3. `uv run python -m pipeline doctor --notify-test`

Without a webhook, notifications only go to the console and `pipeline.log`.

## Processes ("routes")

Each folder in `routes/` is one process, described in its `ROUTE.md`. Routes
are shared through git and reloaded on every poll.

| Route | Status |
|---|---|
| `large_loss` | Extracts the facts of a loss advice / update / cash call into `extracted.json` (schema-checked), verifies every amount against the text it was copied from and every quote against the file it cites, computes the movement against the previous estimate. Netting through LargeLossesNetting: not wired in yet. |
| `smoke_test` | Only for `[PIPELINE TEST]` emails, to test the installation. |

Adding a process is described in [CLAUDE.md](CLAUDE.md#adding-a-route).

## Usage, cost and data

- Every run uses your own Claude subscription: Sonnet to classify, Opus for
  the routes by default (change in `config.toml`). The "API-equivalent cost" on the cards
  is informational; a subscription is not billed per run.
- If `ANTHROPIC_API_KEY` is set in your environment, Claude Code bills that
  API key instead of your subscription; `doctor` warns about it.
- Email content is sent to Claude under the company's Claude plan. Job folders
  stay on your PC. Inbox, jobs, `config.toml` and any `.msg/.eml/.xlsx/.pdf`
  files are gitignored; never force-add them. Test emails in `tests/fixtures`
  are invented.
- Claude agents only see their job folder, write only to `output/<route>/`,
  have no web access and no shell, and never do arithmetic on the figures.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `uv sync`: *invalid peer certificate: UnknownIssuer*, or uv complains about `system-certs` | The corporate proxy re-signs TLS; `pyproject.toml` tells uv to use the Windows certificate store, which needs a recent uv. Update it (`winget upgrade astral-sh.uv` or `uv self update`); with an old uv, `uv sync --native-tls` also works. |
| *Access is denied* starting `email-pipeline` or `pytest` | Defender blocks uv's launcher .exe files; use `uv run python -m pipeline ...` / `uv run python -m pytest`. |
| *Access is denied* for `.venv\Scripts\python.exe` | The environment was built on a uv-downloaded Python. Install Python from python.org, delete `.venv`, run `uv sync` again. |
| Errors mentioning long paths / files not found deep in a job | Windows' 260-character limit: keep `jobs_dir` short, or ask IT to enable long paths. |
| Nothing happens after dropping an email | Items wait until unchanged for `settle_seconds`; check the console and `status`. Is the pipeline running? |
| Card says *Pipeline paused* | Claude unavailable: usage limit (resumes automatically) or login expired (`login`). Run `doctor`. |
| Repository already inside OneDrive | Move it, or set the environment variable `UV_PROJECT_ENVIRONMENT` to a folder outside OneDrive before `uv sync`. |

Without uv: `python -m venv .venv`, `.venv\Scripts\python -m pip install -e . pytest`,
then use `.venv\Scripts\python -m pipeline ...` (dependency versions are then
not pinned by `uv.lock`).

## Development

```powershell
uv run python -m pytest                      # offline tests, never call Claude
uv run python -m pipeline eval-classifier    # live: classifier on tests/classifier_cases.yaml
uv run python -m pipeline selftest           # live: agent sandbox
```
