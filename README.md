# KnowMemo

A local-first personal memory and knowledge system.

KnowMemo reads your own conversations and notes, normalises them into a
source-agnostic model, turns them into structured knowledge documents, and
answers questions about them with source attribution. Your data stays on your
machine.

> **Status: early.** Milestones M0–M2 are complete: environment checks, the
> WeChat connector (against plaintext snapshots), segmentation, knowledge
> document generation, and the import pipeline with its `--dry-run` report.
> Nothing is uploaded anywhere yet — WeKnora integration is M3. See
> [`docs/plan.md`](docs/plan.md) for the plan and the findings that shaped it.

## Design in one paragraph

KnowMemo is **not** a WeChat RAG application. It is a personal-memory ingestion
and retrieval platform, with source connectors plugged into one side and a
knowledge backend on the other. KnowMemo owns connectors, normalisation,
conversation semantics, personal-memory metadata, ingestion state and privacy.
[WeKnora](https://github.com/Tencent/WeKnora) owns document processing,
chunking, embedding and hybrid retrieval. An LLM owns summarisation and answer
synthesis. Nothing in the core knows which connector produced a record.

## Requirements

- Python 3.11 or newer
- SQLite (bundled with Python)
- Optional: [Ollama](https://ollama.com) for local LLM and embedding models
- Optional: [WeKnora](https://github.com/Tencent/WeKnora) via Docker, for retrieval

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Check your environment

```bash
knowmemo doctor
```

`doctor` reports what is present, what is missing, and what to do about each
gap. It exits non-zero only for genuine failures — an absent WeKnora or a
missing embedding model is a warning, because the ingestion path is required to
work without either.

## Configure

Copy `.env.example` to `knowmemo.yaml` and edit, or export `KNOWMEMO_*`
variables. Nested keys use a double underscore:

```bash
export KNOWMEMO_WECHAT__SNAPSHOT_PATH=/path/to/plaintext/snapshot
```

Environment variables override the YAML file. **No machine path is hard-coded
anywhere** — `wechat.data_path` and `wechat.snapshot_path` ship as `null`.

## Commands

| Command | Status |
|---|---|
| `knowmemo doctor` | working |
| `knowmemo config show` | working |
| `knowmemo source list` | working |
| `knowmemo source add wechat` | working |
| `knowmemo source scan wechat` | working |
| `knowmemo import wechat --dry-run` | working |
| `knowmemo import wechat` | working |
| `knowmemo stats` | working |
| `knowmemo import status` | working |
| `knowmemo search "…"` | M3 |
| `knowmemo reindex wechat` | M3 |
| `knowmemo ask "…"` | M4 |

Commands from a later milestone exit with code `2` and say which milestone will
deliver them. They never silently succeed. The distinction between "not built
yet" and "broken" is load-bearing in this project.

## Importing

```bash
knowmemo source add wechat --snapshot-path ~/snapshots/wechat --account-id wxid_you
knowmemo source scan wechat                  # what is there, without importing
knowmemo import wechat --dry-run             # what would happen, writing nothing
knowmemo import wechat                       # write it
```

The input is a **directory of plaintext SQLite files**. KnowMemo never reads
the live encrypted WeChat store and never touches a running WeChat process;
producing that plaintext snapshot is a separate step performed under your own
judgement. See `docs/plan.md` finding F9 and decision D4 — WeChat ships no
programmatic export, and this is the consequence.

`--dry-run` reads, segments and builds every document **in memory**, then
reports exactly what a real run would create, update or leave alone, and writes
none of it. The only thing it persists is the run manifest, so
`knowmemo import status` shows the dry run happened.

A real run writes two kinds of derived artifact:

| Path | What |
|---|---|
| `data/normalized/<source>/*.jsonl` | the normalised record — one line per message, everything that was read |
| `data/knowledge/<source>/*.md` | one Markdown document per segment, with YAML frontmatter |

Neither is a copy of your data for its own sake: the JSONL exists so a later
change to segmentation or retrieval can be replayed against what was actually
imported, and the Markdown is what will be uploaded in M3.

Re-running is a no-op. Segment IDs are deterministic, so a second import
reports `unchanged` rather than creating duplicates. Changing the segmentation
parameters requires bumping `segmentation.version`, which changes the segment
IDs and turns the change into an explicit, visible re-index instead of a silent
one.

## Development

```bash
.venv/bin/python -m pytest          # unit tests
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format src tests
```

pytest's default `tmp_path_retention_policy` deletes each passing test's
`tmp_path` at teardown, and the whole base temp directory at session end.
`pyproject.toml` sets it to `none` so the suite performs no deletions at all.
That matters in a sandbox that meters deletions per turn — there, the default
turns a green suite into a wall of teardown errors landing on whichever tests
happen to run last, which reads like a code bug and is not one.

Separately, pytest creates its `tmp_path` root as `<tempdir>/pytest-of-<user>`.
Some sandboxed environments route that `mkdir(exist_ok=True)` call through a
broker that ignores `exist_ok` and raises `EEXIST` whenever the directory
already exists — which makes the first test run pass and every later one fail.
If you hit that, point the suite at a writable directory:

```bash
KNOWMEMO_TEST_TMPDIR="$PWD/.tmp" .venv/bin/python -m pytest
```

`tests/conftest.py` honours that variable, and falls back to a session-unique
directory under `.tmp/` when it detects the broken behaviour. On a normal
machine neither path is taken.

## Privacy

- Raw imported conversations live under `data/`, which is git-ignored in full.
  Never commit them.
- Logs carry identifiers and status only. A redaction filter strips message
  content and credentials even if a call site forgets to.
- Nothing is sent to a remote service unless you explicitly configure one.

## Layout

```
src/knowmemo/
├── cli/            command-line entry points
├── config/         settings loading (YAML + environment)
├── connectors/     one package per data source
├── domain/         source-agnostic models and deterministic IDs
├── ingestion/      the connector contract and the pipeline
├── knowledge/      document building and the WeKnora client
├── normalization/  raw records to canonical form
├── processing/     segmentation, metadata, optional AI enrichment
├── retrieval/      query service
└── storage/        metadata database (SQLAlchemy + SQLite)
```

## License

MIT — see [LICENSE](LICENSE).
