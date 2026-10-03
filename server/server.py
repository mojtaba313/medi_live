#!/usr/bin/env python3
"""Live lecture server: lecturers stream mic audio, viewers read text.

  Lecturer:  WS /ws/lecturer?room=ROOM&grant=CODE -> JSON hello {"type":"hello","sampleRate":16000},
             then binary Int16 mono 16kHz frames (exact recognizer input, zero decode).
  Viewers:   WS /ws?room=ROOM -> transcript/correction/status messages.
  Admin REST (header X-Admin-Token: $ADMIN_TOKEN, default "admin123"):
    GET    /api/rooms
    POST   /api/rooms {name, description?, require_permission?}
    PATCH  /api/rooms/{id} {name?, description?, require_permission?}
    DELETE /api/rooms/{id}
    GET    /api/rooms/{id}/grants            (recording-permission codes)
    POST   /api/rooms/{id}/grants {label?}   (create a grant code)
    DELETE /api/rooms/{id}/grants/{code}     (revoke)
    POST   /api/rooms/{id}/clear             (clear live transcript history)
    POST   /api/rooms/{id}/kick              (disconnect lecturer)
    GET    /api/rooms/{id}/export            (transcript JSON download)
    GET    /api/archive                      (recorded sessions)
    GET    /api/archive/{id}                 (session detail + transcript)
  Static:  GET /archive/{file}               (recorded .wav playback)
  GET /health -> viewers, lecturer state, corrector backend, rooms summary.

Rooms + grants persist in rooms.json, sessions in archive/archive.json.
Each lecturer session is recorded to archive/*.wav (16k mono) + *.json.

Run from server/:  ../.venv/bin/python server.py
"""

import asyncio
import collections
import json
import logging
import os
import secrets
import threading
import time
import wave
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
import uvicorn
from fastapi.staticfiles import StaticFiles

from recorded import init_recognizer, fmt_ts, SAMPLE_RATE
from db import GlossaryStore
from corrector import (
    load_corrector_from_env,
    looks_sentence_complete,
    CORRECT_MIN_CHARS,
    CORRECT_MAX_CHARS,
    CORRECT_TIMEOUT_S,
)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("server")

# ============ CONFIG ============
HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "8000"))  # 8000 dev (plain) / 8443 docker (TLS)
HISTORY_N = 200  # lines replayed to late-joining viewers
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "admin123")  # admin panel token (X-Admin-Token)
# Decode granularity: accumulate mic audio into blocks before each
# accept_waveform/decode/get_result cycle. Per-micro-chunk decoding keeps the
# event loop spinning on C++ overhead and runs slower than realtime
# (measured: 20s of audio -> 16s delay, growing). 0.5s blocks decode each
# forward pass once per block and stay ahead of realtime on weak CPUs.
DECODE_BLOCK_S = float(os.getenv("DECODE_BLOCK_S", "0.5"))
DECODE_FLUSH_S = float(os.getenv("DECODE_FLUSH_S", "0.25"))  # max wait before flushing a partial block
# Kill a session whose socket is alive but no audio arrives (dead mic /
# suspended phone). Real silence still streams frames, so a long gap means
# the client is gone; without this the session hangs forever.
IDLE_TIMEOUT_S = float(os.getenv("IDLE_TIMEOUT_S", "120"))
# ================================

HERE = Path(__file__).resolve().parent
# Runtime state (rooms, glossaries db, recordings) lives in DATA_DIR so a Docker
# deployment can keep it in one mounted volume outside the container.
#   dev  : DATA_DIR unset -> server/ next to this file (original behavior)
#   docker: DATA_DIR=/data -> single volume, survives `docker rm`
DATA_DIR = Path(os.getenv("DATA_DIR", str(HERE)))
ROOMS_FILE = Path(os.getenv("ROOMS_FILE", str(DATA_DIR / "rooms.json")))
ARCHIVE_DIR = Path(os.getenv("ARCHIVE_DIR", str(DATA_DIR / "archive")))
ARCHIVE_META = ARCHIVE_DIR / "archive.json"
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
# Built web client (client/dist), mounted into the image by the Dockerfile.
CLIENT_DIST = Path(os.getenv("CLIENT_DIST", str(HERE.parent / "dist")))

recognizer = None
corrector = None
glossary_store = None
seg_counter = [0]
stop_event = threading.Event()


# ---------------------------------------------------------------- rooms

def _now_ts() -> float:
    return time.time()


def _slug(name: str) -> str:
    base = "".join(c if c.isalnum() else "-" for c in (name or "room").strip())[:24].strip("-")
    return (base or "room").lower()


class Room:
    def __init__(self, rid, name, description="", require_permission=False):
        self.id = rid
        self.name = name
        self.description = description or ""
        self.created_at = _now_ts()
        self.require_permission = bool(require_permission)
        self.grants: dict[str, dict] = {}  # code -> {label, created_at, uses}
        self.viewers: set[WebSocket] = set()
        self.history: collections.deque = collections.deque(maxlen=HISTORY_N)
        self.broadcast_queue: asyncio.Queue = asyncio.Queue()
        self.lecturer_frames: asyncio.Queue = asyncio.Queue()
        self.fed_s = 0.0
        self.lecturer_connected = False
        self.session_start: float | None = None
        self.corrected_tail = ""
        self.rec_wav = None          # wave.Wave_write open handle
        self.rec_path: Path | None = None
        self.rec_transcript: list[dict] = []
        self.kick_requested = False
        self.broadcaster_task = None
        self.last_frame_ts = 0.0  # wall time of last received audio chunk
        self.last_end = None      # {"code","fed_s","ts"} of last session end
        # perf monitor (updated during a live session, exposed via /health):
        self.lag_s = 0.0      # wall_elapsed - fed_s; >2s means slower than realtime
        self.decode_ms = 0.0  # EMA of one accept+decode+get_result cycle
        self.queue_max = 0    # max pending lecturer_frames depth seen this session

    def to_dict(self, with_grants=False):
        d = {
            "id": self.id, "name": self.name,
            "description": self.description,
            "created_at": self.created_at,
            "require_permission": self.require_permission,
            "viewers": len(self.viewers),
            "lecturer_connected": self.lecturer_connected,
            "fed_s": round(self.fed_s, 1),
            "history_n": len(self.history),
            "grants_n": len(self.grants),
            "lag_s": round(self.lag_s, 1),
            "decode_ms": round(self.decode_ms, 1),
            "queue_max": self.queue_max,
            "last_end": self.last_end,
        }
        try:
            if glossary_store is not None:
                d["glossary_n"] = len(glossary_store.get_terms(self.id))
        except Exception:
            pass
        if with_grants:
            d["grants"] = [
                {"code": c, **g} for c, g in self.grants.items()
            ]
        return d


ROOMS: dict[str, Room] = {}


def save_rooms():
    try:
        data = {rid: {"name": r.name, "description": r.description,
                      "created_at": r.created_at,
                      "require_permission": r.require_permission,
                      "grants": r.grants} for rid, r in ROOMS.items()}
        ROOMS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("save_rooms failed: %s", e)


def load_rooms():
    ROOMS.clear()
    if ROOMS_FILE.exists():
        try:
            data = json.loads(ROOMS_FILE.read_text(encoding="utf-8"))
            for rid, v in data.items():
                r = Room(rid, v.get("name", rid), v.get("description", ""),
                         v.get("require_permission", False))
                r.created_at = v.get("created_at", _now_ts())
                r.grants = v.get("grants", {})
                ROOMS[rid] = r
        except Exception as e:
            log.warning("load_rooms failed: %s", e)
    if "live" not in ROOMS:
        ROOMS["live"] = Room("live", "کلاس اصلی", "پخش زنده پیش‌فرض", False)
        save_rooms()


def get_room(rid: str | None) -> Room:
    rid = (rid or "live").strip() or "live"
    if rid not in ROOMS:
        raise HTTPException(404, f"room '{rid}' not found")
    return ROOMS[rid]


def check_admin(req: Request):
    tok = req.headers.get("x-admin-token", "") or req.query_params.get("token", "")
    if not ADMIN_TOKEN or secrets.compare_digest(tok, ADMIN_TOKEN):
        return True
    raise HTTPException(401, "bad admin token (header X-Admin-Token)")


# ---------------------------------------------------------------- archive

def load_archive() -> list[dict]:
    if ARCHIVE_META.exists():
        try:
            return json.loads(ARCHIVE_META.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_archive(items: list[dict]):
    try:
        ARCHIVE_META.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("save_archive failed: %s", e)


def archive_session(room: Room, duration_s: float):
    """Close current recording, write metadata. Called at session end."""
    items = load_archive()
    try:
        if room.rec_wav is not None:
            try:
                room.rec_wav.close()
            except Exception:
                pass
            room.rec_wav = None
        if room.rec_path is None:
            return None
        wav_name = room.rec_path.name
        sid = room.rec_path.stem
        meta = {
            "id": sid, "room_id": room.id, "room_name": room.name,
            "started_at": room.session_start or _now_ts(),
            "duration_s": round(duration_s, 1),
            "file": f"/archive/{wav_name}",
            "size": room.rec_path.stat().st_size if room.rec_path.exists() else 0,
            "segments": len(room.rec_transcript),
            "preview": " ".join(
                s.get("text", "") for s in room.rec_transcript[-6:])[:300],
        }
        (ARCHIVE_DIR / f"{sid}.json").write_text(
            json.dumps({"meta": meta, "transcript": room.rec_transcript},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        items.insert(0, meta)
        save_archive(items)
        log.info("archived session %s (%ss, %d segs)", sid, meta["duration_s"], meta["segments"])
        return meta
    except Exception as e:
        log.warning("archive_session failed: %s", e)
        return None
    finally:
        room.rec_path = None
        room.rec_transcript = []


# ---------------------------------------------------------------- live pipeline

def push_from_thread(room: Room, msg: dict, loop: asyncio.AbstractEventLoop):
    room.history.append(msg)
    room.rec_transcript.append(msg)
    asyncio.run_coroutine_threadsafe(room.broadcast_queue.put(msg), loop)


async def publish(room: Room, msg: dict):
    room.history.append(msg)
    room.rec_transcript.append(msg)
    await room.broadcast_queue.put(msg)


async def run_session(room: Room, frames: asyncio.Queue, loop: asyncio.AbstractEventLoop):
    """Transcribe one lecturer session until disconnect.

    Two-stage output:
      - raw transcript segments broadcast IMMEDIATELY (stage="raw");
      - buffered + corrected async; corrected chunk broadcast later
        (type="correction", stage="corrected") with raw ids it replaces.
    """
    global recognizer, corrector
    stream = recognizer.create_stream()
    fed = 0
    last_text = ""

    # ---- start wav recording (16k mono int16) ----
    stamp = time.strftime("%Y%m%d-%H%M%S")
    room.rec_path = ARCHIVE_DIR / f"{room.id}_{stamp}.wav"
    room.rec_transcript = []
    room.session_start = _now_ts()
    try:
        w = wave.open(str(room.rec_path), "wb")
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        room.rec_wav = w
    except Exception as e:
        log.warning("wav record open failed: %s", e)
        room.rec_wav = None

    pending_ids: list[int] = []
    pending_texts: list[str] = []
    pending_pos: str = "00:00"
    pending_since: float = 0.0

    def pending_joined() -> str:
        return " ".join(pending_texts).strip()

    async def do_correct(ids: list[int], raw_joined: str, audio_pos: str):
        if corrector is None or not corrector.enabled or not raw_joined.strip():
            return
        try:
            fixed = await corrector.correct(raw_joined, context=room.corrected_tail,
                                            room_id=room.id)
        except Exception as e:
            log.warning("correction failed: %s", e)
            return
        if not fixed or not fixed.strip():
            return
        room.corrected_tail = (room.corrected_tail + " " + fixed).strip()[-600:]
        await publish(room, {"type": "correction", "ids": ids, "stage": "corrected",
                             "text": fixed.strip(), "raw": raw_joined,
                             "audio_pos": audio_pos, "ts": time.time()})

    def maybe_correct(force: bool = False):
        if not pending_ids or corrector is None or not corrector.enabled:
            if force:
                pending_ids.clear()
                pending_texts.clear()
            return
        joined = pending_joined()
        if not joined:
            return
        complete = looks_sentence_complete(joined)
        aged = (time.time() - pending_since) >= CORRECT_TIMEOUT_S if pending_since else False
        if force or len(joined) >= CORRECT_MAX_CHARS or \
                ((len(joined) >= CORRECT_MIN_CHARS) and (complete or aged)):
            ids = list(pending_ids)
            pos = pending_pos
            pending_ids.clear()
            pending_texts.clear()
            asyncio.create_task(do_correct(ids, joined, pos))

    def dedup_new(tail_words: list[str], new_text: str) -> str:
        """Strip leading words of new_text already emitted (streaming partials
        revise their tail, so a naive prefix-diff re-emits words -> duplicates)."""
        nw = new_text.split()
        if not tail_words or not nw:
            return new_text
        for k in range(min(len(tail_words), len(nw), 12), 0, -1):
            if nw[:k] == tail_words[-k:]:
                return " ".join(nw[k:])
        return new_text

    emitted_tail: list[str] = []  # last words actually sent, for overlap dedup

    def emit(text: str):
        nonlocal pending_since, pending_pos
        text = dedup_new(emitted_tail, text.strip())
        if not text:
            return
        emitted_tail.extend(text.split())
        del emitted_tail[:-40]
        seg_counter[0] += 1
        seg_id = seg_counter[0]
        pos = fmt_ts(fed / SAMPLE_RATE)
        push_from_thread(room, {"type": "transcript", "id": seg_id, "stage": "raw",
                                "text": text, "audio_pos": pos,
                                "ts": time.time()}, loop)
        if corrector is not None and corrector.enabled:
            if not pending_ids:
                pending_since = time.time()
                pending_pos = pos
            pending_ids.append(seg_id)
            pending_texts.append(text)
            maybe_correct()

    block_samples = max(800, int(SAMPLE_RATE * DECODE_BLOCK_S))  # ~0.5s
    buf = bytearray()
    wall_start = time.time()
    last_decode = wall_start
    room.lag_s = 0.0
    room.queue_max = 0
    end_reason = "disconnect"

    def decode_block():
        """Accept one buffered block, decode once, emit new hypothesis delta."""
        nonlocal fed, last_text, last_decode
        t0 = time.time()
        n = len(buf) // 2
        pcm = np.frombuffer(bytes(buf), dtype=np.int16)
        del buf[:]
        stream.accept_waveform(SAMPLE_RATE, (pcm.astype(np.float32) / 32768.0))
        fed += n
        room.fed_s = fed / SAMPLE_RATE
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
        cur = recognizer.get_result(stream).strip()
        if cur and cur != last_text:
            new = cur[len(last_text):].strip() if cur.startswith(last_text) else cur
            if new:
                emit(new)
            last_text = cur
        else:
            maybe_correct()
        dt_ms = (time.time() - t0) * 1000.0
        room.decode_ms = room.decode_ms * 0.85 + dt_ms * 0.15 if room.decode_ms else dt_ms
        last_decode = time.time()
        room.lag_s = max(0.0, (last_decode - wall_start) - fed / SAMPLE_RATE)

    while True:
        # admin kick?
        if room.kick_requested:
            room.kick_requested = False
            end_reason = "kick"
            break
        if len(buf) // 2 >= block_samples:
            decode_block()
            continue
        try:
            chunk = await asyncio.wait_for(frames.get(), timeout=0.05)
        except asyncio.TimeoutError:
            if buf and (time.time() - last_decode) >= DECODE_FLUSH_S:
                decode_block()  # flush partial block so tail audio never stalls
            else:
                maybe_correct()
            room.lag_s = max(0.0, (time.time() - wall_start) - fed / SAMPLE_RATE)
            if fed > 0 and room.last_frame_ts and \
                    (time.time() - room.last_frame_ts) > IDLE_TIMEOUT_S:
                log.warning("[SRV] session idle %.0fs with no audio room=%s — finalizing",
                            time.time() - room.last_frame_ts, room.id)
                end_reason = "idle"
                break
            continue
        if chunk is None:
            break
        room.last_frame_ts = time.time()
        room.queue_max = max(room.queue_max, frames.qsize())
        if room.rec_wav is not None:
            try:
                room.rec_wav.writeframes(chunk)
            except Exception:
                pass
        buf.extend(chunk)
        if len(buf) // 2 >= block_samples:
            decode_block()
    if buf:  # trailing partial block at session end
        try:
            decode_block()
        except Exception as e:
            log.warning("final decode_block failed: %s", e)
    stream.input_finished()
    while recognizer.is_ready(stream):
        recognizer.decode_stream(stream)
    final = recognizer.get_result(stream).strip()
    if final and final != last_text:
        new = final[len(last_text):].strip() if final.startswith(last_text) else final
        if new:
            emit(new)
    maybe_correct(force=True)
    # give the last correction task a moment, then archive
    await asyncio.sleep(0.5)
    archive_session(room, fed / SAMPLE_RATE if fed else 0.0)
    room.fed_s = fed / SAMPLE_RATE
    await publish(room, {"type": "done", "ts": time.time()})
    return end_reason


async def broadcaster(room: Room):
    while True:
        msg = await room.broadcast_queue.get()
        dead = []
        for ws in list(room.viewers):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            room.viewers.discard(ws)


async def heartbeat():
    while True:
        await asyncio.sleep(5.0)
        for room in list(ROOMS.values()):
            msg = {"type": "status", "ts": time.time(),
                   "viewers": len(room.viewers),
                   "lecturer_connected": room.lecturer_connected,
                   "fed_s": room.fed_s, "room": room.id,
                   "lag_s": round(room.lag_s, 1),
                   "decode_ms": round(room.decode_ms, 1)}
            for ws in list(room.viewers):
                try:
                    await ws.send_json(msg)
                except Exception:
                    room.viewers.discard(ws)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global recognizer, corrector, glossary_store
    load_rooms()
    recognizer = init_recognizer()
    corrector = load_corrector_from_env()
    log.info("corrector: %s", corrector.describe())
    # Per-class glossaries: SQLite store, warmed into the corrector cache.
    glossary_store = GlossaryStore()
    try:
        for rid, terms in glossary_store.get_all().items():
            corrector.set_room_terms(rid, terms)
        log.info("glossaries loaded for %d rooms", len(corrector._room_terms))
    except Exception as e:
        log.warning("glossary warm-up failed: %s", e)
    log.info("admin token: %s", "***" if ADMIN_TOKEN else "(none — open admin!)")
    stop_event.clear()
    for room in ROOMS.values():
        room.broadcaster_task = asyncio.create_task(broadcaster(room))
    h = asyncio.create_task(heartbeat())
    yield
    stop_event.set()
    h.cancel()
    for room in ROOMS.values():
        if room.broadcaster_task:
            room.broadcaster_task.cancel()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# Built web client: hashed assets get a real static mount (fast, cacheable).
# The SPA catch-all route is registered at the very END of this file so that
# /api, /ws and /archive always win over it.
if (CLIENT_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=str(CLIENT_DIST / "assets")),
              name="assets")


@app.get("/health")
def health():
    info = {"ok": True,
            "rooms": {rid: r.to_dict() for rid, r in ROOMS.items()},
            "viewers": sum(len(r.viewers) for r in ROOMS.values()),
            "lecturer_connected": any(r.lecturer_connected for r in ROOMS.values())}
    try:
        info["corrector"] = corrector.describe() if corrector else {"backend": "none"}
    except Exception:
        info["corrector"] = {"backend": "unknown"}
    return info


@app.get("/", response_class=HTMLResponse)
def index():
    # When the built client is present (Docker image / after `pnpm build`),
    # hand the SPA over so react-router owns the root path. Otherwise fall back
    # to the standalone-server help text.
    built_index = CLIENT_DIST / "index.html"
    if built_index.is_file():
        return FileResponse(str(built_index))
    return ("<h3>Medi Live server</h3>"
            "<p>Lecturer: <code>/record</code> route of the React app. "
            "Viewers: <code>/</code> route. Admin: <code>/admin</code>.</p>"
            "<p>Two-stage text: <code>transcript(stage=raw)</code> instantly, "
            "<code>correction(stage=corrected)</code> replaces it. See <code>/health</code>.</p>"
            "<p>Client not built — run <code>pnpm build</code> in <code>client/</code>, "
            "or use <code>pnpm dev</code>.</p>")


# ---------------------------------------------------------------- admin REST

@app.get("/api/rooms")
def api_list_rooms():
    return {"rooms": [r.to_dict() for r in ROOMS.values()]}


@app.post("/api/rooms")
async def api_create_room(req: Request):
    check_admin(req)
    body = await req.json()
    name = (body.get("name") or "کلاس جدید").strip()[:80]
    desc = (body.get("description") or "").strip()[:300]
    req_perm = bool(body.get("require_permission", False))
    rid = _slug(name) or "room"
    base, i = rid, 2
    while rid in ROOMS:
        rid = f"{base}-{i}"
        i += 1
    room = Room(rid, name, desc, req_perm)
    ROOMS[rid] = room
    room.broadcaster_task = asyncio.create_task(broadcaster(room))
    save_rooms()
    log.info("room created: %s (%s)", rid, name)
    return {"ok": True, "room": room.to_dict()}


@app.patch("/api/rooms/{rid}")
async def api_patch_room(rid: str, req: Request):
    check_admin(req)
    room = get_room(rid)
    body = await req.json()
    if "name" in body:
        room.name = str(body["name"])[:80]
    if "description" in body:
        room.description = str(body["description"])[:300]
    if "require_permission" in body:
        room.require_permission = bool(body["require_permission"])
    save_rooms()
    return {"ok": True, "room": room.to_dict()}


@app.delete("/api/rooms/{rid}")
def api_delete_room(rid: str, req: Request):
    check_admin(req)
    if rid == "live":
        raise HTTPException(400, "cannot delete default room 'live'")
    room = get_room(rid)
    if room.lecturer_connected:
        raise HTTPException(409, "room is live — kick the lecturer first")
    if room.broadcaster_task:
        room.broadcaster_task.cancel()
    del ROOMS[rid]
    save_rooms()
    try:
        if glossary_store is not None:
            glossary_store.delete_room(rid)
        if corrector is not None:
            corrector.set_room_terms(rid, [])
    except Exception as e:
        log.warning("room glossary cleanup failed %s: %s", rid, e)
    return {"ok": True}


@app.get("/api/rooms/{rid}/grants")
def api_list_grants(rid: str, req: Request):
    check_admin(req)
    room = get_room(rid)
    return {"room": rid, "require_permission": room.require_permission,
            "grants": [{"code": c, **g} for c, g in room.grants.items()]}


@app.post("/api/rooms/{rid}/grants")
async def api_create_grant(rid: str, req: Request):
    """Grant recording permission: returns a code the lecturer pastes on /record."""
    check_admin(req)
    room = get_room(rid)
    try:
        body = await req.json()
    except Exception:
        body = {}
    label = str(body.get("label") or "مدرس").strip()[:80]
    code = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8]
    room.grants[code] = {"label": label, "created_at": _now_ts(), "uses": 0}
    room.require_permission = True  # creating a grant enables gated mode
    save_rooms()
    return {"ok": True, "code": code, "label": label,
            "room": rid, "require_permission": room.require_permission}


@app.delete("/api/rooms/{rid}/grants/{code}")
def api_revoke_grant(rid: str, code: str, req: Request):
    check_admin(req)
    room = get_room(rid)
    room.grants.pop(code, None)
    save_rooms()
    return {"ok": True}


# ---------------------------------------------------------------- per-class glossaries

def _refresh_room_glossary(rid: str):
    """Push the room's current DB terms into the corrector cache."""
    try:
        if glossary_store is not None and corrector is not None:
            corrector.set_room_terms(rid, glossary_store.get_terms(rid))
    except Exception as e:
        log.warning("glossary cache refresh failed room=%s: %s", rid, e)


@app.get("/api/rooms/{rid}/glossary")
def api_get_glossary(rid: str, req: Request):
    """Room's own terms + counts (global terms apply to every room)."""
    check_admin(req)
    get_room(rid)
    return {"room": rid,
            "terms": glossary_store.get_terms(rid),
            "room_n": len(glossary_store.get_terms(rid))}


@app.post("/api/rooms/{rid}/glossary")
async def api_add_glossary(rid: str, req: Request):
    """Add one term {term} or many {terms:[...]} to the room's set."""
    check_admin(req)
    get_room(rid)
    body = await req.json()
    if isinstance(body.get("terms"), list):
        added = glossary_store.add_many(rid, body["terms"])
    else:
        term = (body.get("term") or "").strip()
        if not term:
            raise HTTPException(400, "provide 'term' or 'terms'")
        added = 1 if glossary_store.add_term(rid, term) else 0
    _refresh_room_glossary(rid)
    return {"ok": True, "added": added, "room": rid}


@app.put("/api/rooms/{rid}/glossary")
async def api_replace_glossary(rid: str, req: Request):
    """Replace the room's whole set — paste the class's term list at once."""
    check_admin(req)
    get_room(rid)
    body = await req.json()
    terms = body.get("terms")
    if not isinstance(terms, list):
        raise HTTPException(400, "'terms' must be a list of strings")
    n = glossary_store.replace_terms(rid, terms)
    _refresh_room_glossary(rid)
    return {"ok": True, "room": rid, "terms_n": n}


@app.delete("/api/rooms/{rid}/glossary/{term}")
def api_delete_glossary_term(rid: str, term: str, req: Request):
    check_admin(req)
    get_room(rid)
    ok = glossary_store.delete_term(rid, term)
    _refresh_room_glossary(rid)
    return {"ok": ok}


@app.post("/api/rooms/{rid}/clear")
def api_clear(rid: str, req: Request):
    check_admin(req)
    room = get_room(rid)
    room.history.clear()
    return {"ok": True}


@app.post("/api/rooms/{rid}/kick")
def api_kick(rid: str, req: Request):
    check_admin(req)
    room = get_room(rid)
    if not room.lecturer_connected:
        return {"ok": True, "kicked": False}
    room.kick_requested = True
    return {"ok": True, "kicked": True}


@app.get("/api/rooms/{rid}/export")
def api_export(rid: str, req: Request, format: str = "json"):
    room = get_room(rid)
    items = list(room.history)
    if format == "txt":
        txt = "\n".join(f"[{m.get('audio_pos','--:--')}] {m.get('text','')}" for m in items)
        return JSONResponse({"ok": True, "txt": txt})
    return {"ok": True, "room": rid, "items": items}


@app.get("/api/archive")
def api_archive():
    return {"sessions": load_archive()}


@app.get("/api/archive/{sid}")
def api_archive_one(sid: str):
    f = ARCHIVE_DIR / f"{sid}.json"
    if not f.exists():
        raise HTTPException(404, "session not found")
    return json.loads(f.read_text(encoding="utf-8"))


@app.get("/archive/{fname}")
def serve_archive(fname: str):
    f = (ARCHIVE_DIR / fname).resolve()
    if not str(f).startswith(str(ARCHIVE_DIR.resolve())) or not f.exists():
        raise HTTPException(404, "not found")
    return FileResponse(str(f))


@app.post("/api/admin/login")
async def api_login(req: Request):
    body = {}
    try:
        body = await req.json()
    except Exception:
        pass
    tok = (body.get("token") or req.headers.get("x-admin-token") or
           req.query_params.get("token") or "")
    if ADMIN_TOKEN and not secrets.compare_digest(str(tok), ADMIN_TOKEN):
        raise HTTPException(401, "wrong token")
    c = corrector.describe() if corrector else {"backend": "none"}
    return {"ok": True, "corrector": c}


# ---------------------------------------------------------------- websockets

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    rid = (ws.query_params.get("room") or "live").strip() or "live"
    room = ROOMS.get(rid)
    if room is None:
        await ws.send_json({"type": "error", "text": f"room '{rid}' not found"})
        await ws.close()
        return
    room.viewers.add(ws)
    try:
        for msg in list(room.history):
            await ws.send_json(msg)
        await ws.send_json({"type": "status", "ts": time.time(),
                            "viewers": len(room.viewers),
                            "lecturer_connected": room.lecturer_connected,
                            "fed_s": room.fed_s, "room": room.id})
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        room.viewers.discard(ws)


@app.websocket("/ws/lecturer")
async def ws_lecturer(ws: WebSocket):
    await ws.accept()
    rid = (ws.query_params.get("room") or "live").strip() or "live"
    grant = (ws.query_params.get("grant") or ws.query_params.get("token") or "").strip()
    room = ROOMS.get(rid)
    if room is None:
        try:
            await ws.send_json({"type": "denied", "text": f"room '{rid}' not found"})
            await ws.close()
        except Exception:
            pass
        return
    # ---- recording permission gate ----
    if room.require_permission and grant not in room.grants:
        try:
            await ws.send_json({"type": "denied",
                                "text": "recording permission required — ask admin for a grant code"})
            await ws.close()
        except Exception:
            pass
        return
    if room.lecturer_connected:
        try:
            await ws.send_json({"type": "busy", "text": "another lecturer is live"})
            await ws.close()
        except Exception:
            pass
        return
    try:
        hello = await ws.receive_text()
        log.info("[SRV] lecturer hello room=%s: %s", rid, hello)
    except WebSocketDisconnect:
        # Client went away before sending hello (e.g. timeout/close) — not an error.
        log.info("[SRV] lecturer disconnected before hello room=%s", rid)
        return
    except Exception as e:
        log.info("[SRV] lecturer hello failed room=%s: %s", rid, e)
        try:
            await ws.close()
        except Exception:
            pass
        return
    if grant in room.grants:
        room.grants[grant]["uses"] = room.grants[grant].get("uses", 0) + 1
        save_rooms()
    room.lecturer_connected = True
    room.fed_s = 0.0
    while not room.lecturer_frames.empty():
        try:
            room.lecturer_frames.get_nowait()
        except asyncio.QueueEmpty:
            break
    loop = asyncio.get_running_loop()
    session = asyncio.create_task(run_session(room, room.lecturer_frames, loop))
    await ws.send_json({"type": "ready", "room": rid})
    log.info("[SRV] lecturer live room=%s.", rid)
    end_code = None

    async def recv_loop():
        nonlocal end_code
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    end_code = message.get("code")  # 1000=user stop, 1001=page gone, 1006=timeout/drop
                    return
                data = message.get("bytes")
                if data:
                    if len(data) % 2 == 1:
                        data = data[:-1]
                    await room.lecturer_frames.put(bytes(data))
        finally:
            try:
                await room.lecturer_frames.put(None)  # end session if still running
            except Exception:
                pass

    recv_task = asyncio.create_task(recv_loop())
    try:
        done, _ = await asyncio.wait({session, recv_task},
                                     return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        session.cancel()
        recv_task.cancel()
        raise
    reason = "error"
    try:
        if session in done:
            reason = session.result()
            if not recv_task.done():
                # Session ended first (idle timeout / kick): the socket is
                # useless now — close it so the phone reconnects immediately.
                recv_task.cancel()
                try:
                    await ws.send_json({"type": "idle",
                                        "text": "session ended — reconnect to start a new one"})
                    await ws.close(code=4000)
                except Exception:
                    pass
        else:
            reason = await session  # disconnect path: None queued above ends it
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("[SRV] session task failed room=%s: %s", rid, e)
    room.lecturer_connected = False
    room.lag_s = 0.0
    room.last_end = {"code": end_code, "reason": reason,
                     "fed_s": round(room.fed_s, 1), "ts": time.time()}
    log.info("[SRV] lecturer disconnected room=%s code=%s reason=%s fed=%.0fs.",
             rid, end_code, reason, room.fed_s)


# --------------------------------------------------------- built web client
# Registered LAST on purpose: FastAPI matches routes in declaration order, so
# every /api, /ws and /archive route above takes precedence. Only unmatched
# paths fall through here, which is what a client-side-routed SPA (react-router
# paths like /record, /admin) needs. Without this, a browser refresh on /admin
# would 404.
if (CLIENT_DIST / "index.html").is_file():
    _SPA_INDEX = CLIENT_DIST / "index.html"

    @app.get("/{spa_path:path}", include_in_schema=False)
    def spa(spa_path: str = ""):
        if spa_path.startswith(("api/", "archive/", "ws", "health")):
            raise HTTPException(404, "not found")
        # Serve a real file if the build emitted one (favicon, manifest, ...).
        candidate = (CLIENT_DIST / spa_path).resolve() if spa_path else None
        if candidate and candidate.is_file() and str(candidate).startswith(
                str(CLIENT_DIST.resolve())):
            return FileResponse(str(candidate))
        return FileResponse(str(_SPA_INDEX))


if __name__ == "__main__":
    assert (HERE / "model" / "model.onnx").exists(), "run from server/"
    uvicorn.run(app, host=HOST, port=PORT)
