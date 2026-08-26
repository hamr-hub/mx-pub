# mx-pub: Multi-platform video publisher

AI-driven publishing to xhs, douyin, kuaishou, weixin. Works in any environment via auto-detect / auto-launch Chrome.

## Features

- **Parallel publishing**: 4 platforms in parallel, ~25s total wall time
- **AI content generation**: Claude/OpenAI/Ollama/Mock providers with auto-fallback
- **Environment portability**: auto-detect Chrome, auto-launch headless if missing
- **Robust**: retry with exponential backoff, transient error categorization, tab self-healing
- **Verified**: 10/10 unit tests pass

## Quick start

```bash
# Local (Chrome already running with --remote-debugging-port=9222)
python3 loop_publish_v2.py

# Preview AI content (no publishing)
python3 preview.py /path/to/video.mp4

# Dry-run AI with mock provider
MOCK_AI=1 python3 preview.py /path/to/video.mp4

# Docker (auto-launches headless Chrome)
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

## Architecture

```
loop_publish_v2.py        # Orchestrator (env detect → AI → publish → save)
├── ai/content.py         # Multi-provider AI generation
├── core/parallel.py       # ThreadPoolExecutor, each platform in own thread
├── core/browser.py        # Shared utilities (connect_chrome, find_file_input)
├── core/environment.py    # Chrome auto-detect / auto-launch
└── platforms/*.py        # Per-platform publish_on_page(...)
```

## Per-platform title limits

| Platform | Limit | Cap |
|----------|-------|-----|
| xhs | 20 chars | enforced in xhs.py |
| douyin | 30 chars | enforced in douyin.py |
| kuaishou | 30 chars | enforced in kuaishou.py |
| weixin | 14 chars | enforced in weixin.py |

## Tests

```bash
python3 tests/test_ai_providers.py
```

10 tests covering: constants, JSON parsing, mock provider, heuristic fallback, prompts.csv lookup, platform-specific content.