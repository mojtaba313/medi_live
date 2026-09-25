#!/usr/bin/env python3
"""Transcribe class.mp3 in real time with Shenava-Koochik streaming ASR.

Runs the sherpa-onnx streaming recognizer over the lecture audio, printing
Persian transcript lines as they are decoded. Set TEST_SECONDS = 0 for the
whole 73-minute file.
"""

import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import sherpa_onnx
import sounddevice as sd
import soundfile as sf

# ============ CONFIG ============
HERE = Path(__file__).resolve().parent
MODEL_DIR = HERE / "model"
MP3_PATH = HERE / "class.mp3"
WAV16K_PATH = HERE / "class_16k.wav"  # auto-converted from MP3 on first run
SAMPLE_RATE = 16000
BLOCK_S = 0.5        # feed chunks; 500ms paces the realtime simulation
TEST_SECONDS = 120   # transcribe only the first N seconds; 0 = whole file
PLAY_AUDIO = True    # play through speakers too; falls back to silent feed
NUM_THREADS = 4
# ================================


def ensure_wav16k():
    if WAV16K_PATH.exists() and WAV16K_PATH.stat().st_mtime >= MP3_PATH.stat().st_mtime:
        return
    if not MP3_PATH.exists():
        sys.exit(f"[ERROR] audio file not found: {MP3_PATH}")
    print(f"[SETUP] converting to 16kHz mono: {WAV16K_PATH.name} ...")
    r = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(MP3_PATH),
         "-ac", "1", "-ar", str(SAMPLE_RATE), "-sample_fmt", "s16",
         str(WAV16K_PATH)])
    if r.returncode != 0 or not WAV16K_PATH.exists():
        sys.exit("[ERROR] ffmpeg conversion failed")
    print("[SETUP] conversion done.")


def init_recognizer():
    model_path = MODEL_DIR / "model.onnx"
    tokens_path = MODEL_DIR / "tokens.txt"
    if not model_path.exists() or model_path.stat().st_size < 1_000_000:
        sys.exit(f"[ERROR] missing model weights: {model_path} "
                 "(run `git lfs pull` inside server/model/)")
    if not tokens_path.exists():
        sys.exit(f"[ERROR] missing tokens file: {tokens_path}")
    print("[INIT] Loading model...")
    recognizer = sherpa_onnx.OnlineRecognizer.from_nemo_ctc(
        model=str(model_path),
        tokens=str(tokens_path),
        num_threads=NUM_THREADS,
        sample_rate=SAMPLE_RATE,
        feature_dim=80,
        decoding_method="greedy_search",
        provider="cpu",
    )
    print("[INIT] Model loaded.")
    return recognizer


def fmt_ts(sec: float) -> str:
    return f"{int(sec // 60):02d}:{int(sec % 60):02d}"


def main():
    ensure_wav16k()
    recognizer = init_recognizer()

    f = sf.SoundFile(str(WAV16K_PATH), "r")
    assert f.samplerate == SAMPLE_RATE and f.channels == 1, \
        f"unexpected wav format: {f.samplerate}Hz/{f.channels}ch"
    total_frames = f.frames
    limit_frames = total_frames if not TEST_SECONDS else min(total_frames, TEST_SECONDS * SAMPLE_RATE)
    print(f"[INFO] Audio: {total_frames / SAMPLE_RATE / 60:.1f} min total, "
          f"transcribing first {limit_frames / SAMPLE_RATE:.0f}s")
    block = int(SAMPLE_RATE * BLOCK_S)

    stream = recognizer.create_stream()
    audio_queue: queue.Queue = queue.Queue(maxsize=64)
    stop_flag = threading.Event()
    accepted = 0

    def emit(text: str):
        pos = accepted / SAMPLE_RATE
        print(f"[{fmt_ts(pos)}] {text}", flush=True)

    # ---- ASR Thread ----
    def asr_worker():
        nonlocal accepted
        last_text = ""
        print("[ASR] Listening...")
        while not stop_flag.is_set() or not audio_queue.empty():
            try:
                chunk = audio_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            stream.accept_waveform(SAMPLE_RATE, chunk)
            accepted += len(chunk)
            while recognizer.is_ready(stream):
                recognizer.decode_stream(stream)
            cur = recognizer.get_result(stream).strip()
            if cur and cur != last_text:
                emit(cur[len(last_text):].strip() if cur.startswith(last_text) else cur)
                last_text = cur
        stream.input_finished()
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
        final = recognizer.get_result(stream).strip()
        if final and final != last_text:
            emit(final[len(last_text):].strip() if final.startswith(last_text) else final)
        print("[ASR] done.")

    asr_thread = threading.Thread(target=asr_worker, daemon=True)
    asr_thread.start()

    # ---- Feed (playback or paced) ----
    fed = 0
    try:
        if not PLAY_AUDIO:
            raise sd.PortAudioError("playback disabled by config")
        print("[PLAYBACK] playing + transcribing...")

        state = {"pos": 0}

        def callback(outdata, frames, time_info, status):
            if status:
                print(f"[AUDIO STATUS] {status}", file=sys.stderr)
            n = min(frames, limit_frames - state["pos"])
            chunk = f.read(n, dtype="float32", always_2d=False) if n > 0 else np.empty(0, np.float32)
            outdata[:len(chunk), 0] = chunk
            if len(chunk) < frames:
                outdata[len(chunk):, 0] = 0
                state["pos"] = limit_frames
                raise sd.CallbackStop()
            audio_queue.put(np.asarray(chunk, dtype=np.float32))
            state["pos"] += len(chunk)

        with sd.OutputStream(samplerate=SAMPLE_RATE, blocksize=block,
                             channels=1, dtype="float32", callback=callback):
            while state["pos"] < limit_frames:
                time.sleep(0.05)
        fed = state["pos"]
    except (sd.PortAudioError, Exception) as e:
        print(f"[PLAYBACK] unavailable ({e}); silent paced feed instead.")
        f.seek(0)
        while fed < limit_frames:
            n = min(block, limit_frames - fed)
            chunk = f.read(n, dtype="float32", always_2d=False)
            if len(chunk) == 0:
                break
            audio_queue.put(np.asarray(chunk, dtype=np.float32))
            fed += len(chunk)
            time.sleep(len(chunk) / SAMPLE_RATE)
    finally:
        f.close()

    stop_flag.set()
    asr_thread.join(timeout=30)
    print(f"[DONE] fed {fed / SAMPLE_RATE:.0f}s of audio.")


if __name__ == "__main__":
    main()
