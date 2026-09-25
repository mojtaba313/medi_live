# Medi Live — realtime lecture transcription (Persian)

Lecturer streams mic audio from a phone browser; viewers watch the Persian
transcript live. Two-stage text: **raw** ASR output instantly, then a
**corrected** version that replaces it — with no API key required
(built-in Persian normalizer; optional OpenAI-compatible / Ollama LLM).

- `server/` — FastAPI + sherpa-onnx streaming ASR (`Shenava-Koochik`),
  rooms, recording-permission grants, session archive, admin REST API.
- `client/` — React + Vite web app (RTL, Persian): home, live viewer,
  recorder, admin panel.

## Quickstart

### Server

```bash
git clone --recurse-submodules <your-repo-url> medi-live
cd medi-live/server
python -m venv ../.venv && ../.venv/bin/pip install -r requirements.txt
cp .env.example .env.local  # optional: set ADMIN_TOKEN etc.
../.venv/bin/python server.py   # serves on :8000
```

Model weights live in `server/model/` (git submodule of the HuggingFace
export repo, LFS). Without `--recurse-submodules`:
`git submodule update --init server/model && cd server/model && git lfs pull`.

### Client

```bash
cd client
pnpm install
pnpm dev      # https on :5173 (mic needs a secure context on phones)
pnpm build    # production bundle in client/dist
```

The dev server proxies `/api`, `/ws`, `/archive` to the backend, so no
mixed-content issues on HTTPS pages. Optional overrides in `client/.env`:
`VITE_API_BASE`, `VITE_WS_BASE`, `BACKEND_URL`.

## Key env vars (server)

| var | default | purpose |
|---|---|---|
| `ADMIN_TOKEN` | `admin123` | admin panel login (`X-Admin-Token`) |
| `CORRECTOR_BACKEND` | `auto` | `auto\|local\|openai\|ollama\|off` |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` | — | optional LLM correction |
| `OLLAMA_URL` / `OLLAMA_MODEL` | `localhost:11434 / qwen2.5:1.5b` | optional local LLM |
| `DECODE_BLOCK_S` / `DECODE_FLUSH_S` | `0.5 / 0.25` | decode batching (latency) |
| `CORRECT_MIN_CHARS` / `CORRECT_MAX_CHARS` / `CORRECT_TIMEOUT_S` | `50 / 400 / 5.0` | correction chunking |

Demo audio (`server/class.mp3`) is intentionally not shipped — drop any
lecture mp3 there for the offline `recorded.py` transcription script.

## Layout

```
client/src/{App,main,styles.css,components/Layout,pages/{Home,Live,Record,Admin},lib/api}
server/{server.py,corrector.py,recorded.py,main.py,model/,archive/}
```
