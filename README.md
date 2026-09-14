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
| `RETRY_FAILED=1` | Also retry transient fail rows | off |
| `MX_DEBUG=1` | Log every captured POST | off |

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
python3 tests/test_ai_providers.py
```

10 tests covering: constants, JSON parsing, mock provider, heuristic fallback, prompts.csv lookup, platform-specific content.