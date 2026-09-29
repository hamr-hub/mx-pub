# mx-pub: Multi-platform video publisher

Publishing to douyin, kuaishou, weixin through a real logged-in Chrome over CDP.
XHS is force-skipped by default (account risk control; enable explicitly per
task). Playbook: `.claude/skills/mx-publish/SKILL.md`.

## Features

- **Stable batch driver** (`batch_publish.py`): one CDP connection + one reused
  tab per platform; platforms run concurrently, videos sequentially per
  platform. Signal-driven per video: upload-finish API → form/topics/AI
  declaration/collection → real publish API response (DOM fallback).
- **Platform features used**: Douyin AI-generated-content declaration ON +
  auto-attach to daily `AI合集_YYYYMMDD`; Kuaishou topics + auto-attach to daily
  `AI合集MMDD` 合集 via verified creator REST API; Weixin short title, topics,
  collection/schedule controls.
- **Safety**: native beforeunload/dialog suppression (multi-client CDP accept
  race otherwise kills the node driver); dead-transport fast-abort; queue saved
  per video under lock; every `ok` is server-confirmed.
- **AI content generation**: Claude/OpenAI/Ollama/Mock providers with auto-fallback.
- **Environment portability**: auto-detect Chrome, auto-launch headless if missing.

## Quick start

```bash
# Local (Chrome already running with --remote-debugging-port=9222)
python3 batch_publish.py                      # all platforms except xhs
ONLY_PLATFORMS=douyin python3 batch_publish.py
LIMIT_PER_PLATFORM=2 python3 batch_publish.py

# Preview AI content (no publishing)
python3 preview.py /path/to/video.mp4

# Dry-run AI with mock provider
MOCK_AI=1 python3 preview.py /path/to/video.mp4

# Docker (auto-launches headless Chrome; needs logged-in profile cookies)
docker-compose up
```

## Environment variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `ANTHROPIC_API_KEY` | Claude API key | none |
| `OPENAI_API_KEY` | OpenAI API key | none |
| `OLLAMA_HOST` | Ollama endpoint | `http://localhost:11434` |
| `MOCK_AI=1` | Force mock provider | off |
| `CHROME_PATH` | Chrome binary path | auto-detect |
| `HEADLESS=1` | Run headless | off (detected) |
| `CDP_URL` | Chrome CDP URL | `http://127.0.0.1:9222` |
| `ONLY_PLATFORMS` | Whitelist, comma-separated | all except xhs |
| `SKIP_PLATFORMS` | Extra platforms to skip | none |
| `LIMIT_PER_PLATFORM` | Cap pending videos per platform | no cap |
| `RETRY_FAILED=1` | Also re-try **permanent** failures (see below) | off |
| `MX_DEBUG=1` | Log every captured POST | off |
| `MX_QUEUE_FILE` | Alternate queue file | `publish_queue.json` |
| `MX_HUMAN=0` | Disable anti-ban jitter (CI) | on |
| `MX_PACING=1` | Enforce the adaptive pacing gate | off |
| `MX_PACING_MAX_WAIT_S` | How long the pacing gate waits before skipping | `300` |
| `MX_PACING_STATE` | Alternate pacing state file | `pacing_state.json` |
| `MX_MAX_RECOVERY` | CDP re-attach budget per run | `6` |
| `MX_HARD_RESTART=1` | Kill+relaunch Chrome on a wedged CDP (single-platform runs only) | off |
| `MX_PLUGINS_DIR` | Directory scanned for hook plugins | `plugins/` |
| `MX_SAFETY_ABORT_BATCH=1` | A safety-gate veto halts the platform's whole batch | off (skip only the video) |
| `MX_SAFETY=1` | Enable the content safety gate | off |
| `MX_INSIGHTS_MD` | Insights markdown output path | `insights_report.md` |
| `MX_INSIGHTS_MODEL` | LLM used for the insights narrative | none (heuristic) |
| `MX_MIN_INTERVAL_S` / `MX_BURST_LIMIT` / `MX_BURST_REST_S` / `MX_FAIL_BACKOFF` | Learned pacing parameters | `120` / `5` / `300` / `0.6` |

## Retry semantics

A row is re-attempted only if its last attempt left it in a state that a retry
can plausibly fix:

| Last state | Re-attempted? |
|---|---|
| `ok` | never — terminal |
| `blocked` (short clip / safety veto) | never — terminal |
| `fail`, transient (timeout, dead CDP, target closed) | yes, by default |
| `fail`, permanent (content rejection, account limit, ban) | only with `RETRY_FAILED=1` |

`RETRY_FAILED=1` force-re-arms permanent failures. Use it deliberately: a
content rejection will not become publishable by trying harder, and repeatedly
re-submitting banned content is how an account gets flagged.

`is_retryable_failure()` in [`scripts/publisher.py`](scripts/publisher.py) is
the single source of truth for that decision — both the queue filter and the
in-run retry use it, and it delegates to the table-driven
`classify()` in [`scripts/core/errors.py`](scripts/core/errors.py), so the two
cannot drift apart.

Classification normalises case and separators (`fatal_connection_closed`,
`FATAL-CONNECTION-CLOSED` and `Connection closed` are one signal), and knows
the driver's own machine-readable markers (`safety:`, `duration_…below_min_5s`)
plus real platform rejection wording (视频违规 / 审核不通过 / 存在争议).

Safety-gate vetoes skip only the flagged video by default. Set
`MX_SAFETY_ABORT_BATCH=1` to halt the whole platform for the day instead.

Queue counters are computed over every platform the queue has records for, not
just the ones in the current run — a single-platform run can no longer report
`remaining: 0` over real pending work. `by_platform` in the queue file and the
run summary show the per-platform breakdown.

## Development

```bash
python3 -m pytest            # 125 tests
python3 -m ruff check .      # lint gate (config in pyproject.toml)
```

`pyproject.toml` holds the ruff and pytest configuration. The lint gate is
deliberately narrow (`E,F,W,B,SIM`): pyupgrade/isort rewrites are off because
they churn ~100 lines of a battle-tested publisher for no functional gain, and
import sorting would reorder the `sys.path`-insert-then-import blocks these
files depend on.

## Architecture

```
batch_publish.py            # Stable entrypoint: concurrent platforms, per-video save
build_publish_queue.py      # Build publish_queue.json from videos + prompts.csv
scripts/
├── publisher.py            # PublishResult, transient-error classification
├── mx_collections.py       # Douyin 合集 UI helpers + Kuaishou 合集 REST API
├── core/watch.py           # ApiWatch: POST capture, publish patterns, fatal-error detect
├── core/dialog_guard.py    # beforeunload/dialog suppression (CDP race protection)
├── core/environment.py     # Chrome auto-detect / auto-launch
└── platforms/*.py          # Per-platform publish_on_page(...)
```

Legacy one-offs live in `archive/2026-09/` (including the old
`loop_publish_v2.py` orchestrator and `collections_driver.py`).

## Per-platform title rules (verified 2026-09)

| Platform | Rule |
|----------|------|
| douyin | title field + description |
| kuaishou | no title field — description only; 合集 title capped at 12 chars |
| weixin | 短标题 6–14 chars required (`_build_short_title()` extends from description) |
| xhs | not run (force-skipped, account risk) |

## Tests

```bash
python3 -m pytest            # everything
python3 -m pytest tests/test_batch_policy.py   # batch policy regressions
```

| File | Covers |
|---|---|
| `tests/test_batch_policy.py` | retry eligibility, mxpub→driver hand-off, pacing gate, queue totals, ApiWatch lifecycle |
| `tests/test_safety_gate_flow.py` | end-to-end control flow of the safety gate in `run_platform` (fake Chrome, real loop) |
| `tests/test_error_taxonomy.py` | classifier/retry-filter agreement across separator styles and driver markers |
| `tests/test_state_io.py` | atomic + concurrent state file writes, corrupt-file recovery |
| `tests/test_self_healing.py` | error taxonomy, recovery, health, hooks, safety gate |
| `tests/test_integration.py` | end-to-end pipeline: event log → risk → insights |
| `tests/test_curation.py` | content selection primitives |
| `tests/test_ai_providers.py` | provider fallback + platform-specific content |
## Self-healing & self-learning layer (2026-09)

On top of the stable CDP driver, mx-pub now ships a self-healing +
self-learning + extensibility layer that makes the publisher AI-driven and
robust to almost anything:

| Module | Purpose |
|---|---|
| `scripts/core/errors.py` | Classifies every failure into TRANSIENT/RECOVERABLE/CONTENT/ACCOUNT/PLATFORM/FATAL and recommends an action |
| `scripts/core/recovery.py` | Auto-runs the recommended action (refresh page, re-login, cool down) |
| `scripts/core/health.py` | Pre-flight health gate (cookie alive, captcha, warning banner) |
| `scripts/core/behavior.py` | Human-like jitter (form pauses, micro mouse moves, previews look) |
| `scripts/core/hooks.py` | Plugin hook system: `batch:before`, `video:after_publish`, `video:on_error`, etc. |
| `scripts/core/pacing.py` | Adaptive pacing per platform (min interval, burst limit, cool-down) |
| `scripts/self_learn/stats.py` | JSONL event log of every publish attempt |
| `scripts/self_learn/optimizer.py` | Recommends best hours, pacing params based on history |
| `scripts/self_learn/risk.py` | Detects ban-risk signatures (consecutive fails, bursts, repeated classes) |
| `scripts/extension/registry.py` | Plug-in registry for new platforms / content providers |
| `scripts/intelligence/report.py` | AI-driven insights report (Markdown + JSON, optional LLM) |
| `mxpub.py` | New unified entrypoint: `publish` / `insights` / `health` / `stats` / `extensions` |

Adding a new platform (e.g. bilibili, youtube shorts) is now a 2-step operation:
1. Drop a publisher module under `scripts/platforms/<name>.py` with the same
   `publish_on_page(page, ...)` signature as the existing ones.
2. Drop a one-file plugin under `scripts/extension/<name>.py` that calls
   `register_platform(PlatformSpec(...))`.

No edits to `batch_publish.py` or any other core file are required.

Full guide: [`docs/NEXT_GEN.md`](docs/NEXT_GEN.md).

```bash
python3 mxpub.py publish            # full batch (auto-runs risk gate)
python3 mxpub.py publish --platform douyin
python3 mxpub.py publish --force    # ignore risk warnings
python3 mxpub.py health
python3 mxpub.py stats
python3 mxpub.py insights           # write insights_report.md + .json
python3 mxpub.py extensions         # list registered platforms
```
