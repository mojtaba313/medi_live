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

### Docker (recommended — one command on any machine)

Prerequisite: **Docker** only (Docker Desktop, or Docker Engine + the Compose
plugin). No Python, Node, pnpm or SSL setup required.

```bash
git clone --recurse-submodules <your-repo-url> medi-live
cd medi-live
# The ASR weights are a git submodule with LFS (~438MB) and must be present
# before the build:
git submodule update --init server/model && git lfs pull

cp .env.docker.example .env
# Edit .env: set ADMIN_TOKEN and TLS_SAN (this machine's LAN IP).
# Find your IP with:  ip -4 -o addr show scope global
#   (on macOS: ifconfig | grep "inet ")

docker compose up -d --build     # first build ~5-10 min
docker compose logs -f            # watch it start
```

Open **`https://<this-machine-ip>:8443`** on the phone and accept the
certificate warning once per device (required before the browser will grant
microphone access).

`TLS_SAN` matters: the container generates its own certificate on first boot,
listing `localhost` plus whatever you put in `TLS_SAN`. Without your machine's
LAN IP in that list, the phone shows a name-mismatch error on every page and
**the microphone will not work**.

| Command | Purpose |
|---|---|
| `docker compose up -d --build` | build (if needed) and start |
| `docker compose logs -f` | follow logs |
| `docker compose restart` | restart without rebuilding |
| `docker compose down` | stop and remove the container (**your data survives**) |

**Where your data lives** — everything the app writes is bind-mounted out of the
container, so rebuilding or removing it never loses a lecture:

```
server/data/rooms.json   rooms + recording-permission grants
server/data/app.db       per-class vocabularies (SQLite)
server/data/archive/     recorded WAVs + transcripts
certs/                   generated TLS certificate (reused across restarts)
```

Back up = copy those two folders. On Linux you can set
`network_mode: host` in `docker-compose.yml` to skip `TLS_SAN` entirely.

### Offline / USB deployment (no build on the target machine)

To hand the app to a lecture PC that should not clone, build, or need internet,
package the finished image once and carry it across:

```bash
./docker/bundle.sh              # -> dist/medi-live-<version>-<date>-<arch>/
```

The bundle holds the image (`medi-live-image.tar.gz`, ~690MB), a `compose`
file **without** a `build:` section, a `.env.example`, and `INSTALL.txt` with
these instructions. Copy the folder to a USB stick. On the target machine:

```bash
gunzip -c medi-live-image.tar.gz | docker load   # ~40s, writes ~2GB
cp .env.example .env      # set ADMIN_TOKEN + TLS_SAN (that machine's LAN IP)
docker compose up -d
```

Nothing is compiled on the target, and `pull_policy: never` guarantees it never
reaches for a registry. `certs/` ships **empty** on purpose, so every machine
generates its own certificate from its own `TLS_SAN`.

| Bundle variable | Effect |
|---|---|
| `RAW=1 ./docker/bundle.sh` | uncompressed `.tar` (2GB, fastest to load) |
| `ZSTD=1 ./docker/bundle.sh` | `.tar.zst` (smallest, needs `zstd`) |
| `VERSION=... IMAGE=... ./docker/bundle.sh` | override tag/name |

Runtime state (`server/data/`, `certs/`) is **not** in the bundle — copy those
two folders separately to carry recordings and vocabularies to another machine.

### Server (manual development)

```bash
git clone --recurse-submodules <your-repo-url> medi-live
cd medi-live/server
python -m venv ../.venv && ../.venv/bin/pip install -r requirements.txt
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

Note: in Docker you do **not** run this — the image builds the client itself
and `server.py` serves `client/dist` alongside the API on the same port.

## Per-class vocabularies

Each room has its own term list (eye anatomy for one lecture, cardiology for
the next) stored in SQLite and edited from **Admin → 📚 واژگان**. Terms are
applied when correcting that room's transcript, on top of the shared
`server/medical_glossary.json`. Paste a whole term list at once (one term per
line) from the lecture slides; changes apply immediately without a restart.

## Key env vars (server)

Read from the process environment. In Docker these live in `.env` (Compose
loads it automatically); running `server.py` directly, export them in your shell
(note: `.env.local` is *not* read by the app).

| var | default | purpose |
|---|---|---|
| `PORT` | `8000` (`8443` in Docker) | listen port |
| `ADMIN_TOKEN` | `admin123` | admin panel login (`X-Admin-Token`) |
| `DATA_DIR` | `server/` (`/data`) | where `rooms.json`, `app.db`, `archive/` live |
| `APP_DB` | `<DATA_DIR>/app.db` | per-class glossary SQLite file |
| `CLIENT_DIST` | `../dist` (`/app/dist`) | built web client to serve |
| `CORRECTOR_BACKEND` | `auto` | `auto\|local\|openai\|ollama\|off` |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` | — | optional LLM correction |
| `OLLAMA_URL` / `OLLAMA_MODEL` | `localhost:11434 / qwen2.5:1.5b` | optional local LLM |
| `DECODE_BLOCK_S` / `DECODE_FLUSH_S` | `0.5 / 0.25` | decode batching (latency) |
| `CORRECT_MIN_CHARS` / `CORRECT_MAX_CHARS` / `CORRECT_TIMEOUT_S` | `50 / 400 / 5.0` | correction chunking |
| `TLS_SAN` | — (Docker) | extra certificate SANs; set to this machine's LAN IP |

Demo audio (`server/class.mp3`) is intentionally not shipped — drop any
lecture mp3 there for the offline `recorded.py` transcription script.

## Layout

```
client/src/{App,main,styles.css,components/Layout,pages/{Home,Live,Record,Admin},lib/api}
server/{server.py,corrector.py,db.py,recorded.py,main.py,model/,archive/}
docker/entrypoint.sh        # first-boot TLS cert + uvicorn
Dockerfile                  # client build stage + python runtime stage
docker-compose.yml          # builds the image; serves https on :8443
docker-compose.deploy.yml   # same, but prebuilt image only (offline/USB)
docker/bundle.sh            # package image + config for offline deployment
```
