#!/usr/bin/env python3
"""Realtime Persian transcription from the microphone (Shenava-Koochik streaming ASR).

Captures at the PipeWire native rate (48kHz stereo), downmixes + resamples to
16kHz mono, and streams it through the sherpa-onnx online recognizer, printing
Persian transcript lines as they are decoded. Ctrl-C stops.

File-based version of this tool is recorded.py in the same directory.
"""

import queue
import sys
import threading
import time
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly
import sherpa_onnx
import sounddevice as sd

# ============ CONFIG ============
HERE = Path(__file__).resolve().parent
MODEL_DIR = HERE / "model"
LOGFILE = HERE / "transcripts.log"
DEVICE = "pulse"      # PipeWire/Pulse virtual device (name, not index: indices shift)
NATIVE_SR = 48000     # PipeWire source rate; avoids server-side resampling
CHANNELS = 2
TARGET_SR = 16000
FEED_BLOCK_S = 0.5    # mic read granularity
NUM_THREADS = 4
# ================================

GCD = __import__("math").gcd(NATIVE_SR, TARGET_SR)  # 48k->16k = exact 3:1


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
        sample_rate=TARGET_SR,
        feature_dim=80,
        decoding_method="greedy_search",
        provider="cpu",
    )
    print("[INIT] Model loaded.")
    return recognizer


def fmt_ts(sec: float) -> str:
    return f"{int(sec // 60):02d}:{int(sec % 60):02d}"


def main():
    recognizer = init_recognizer()
    stream = recognizer.create_stream()
    audio_queue: queue.Queue = queue.Queue(maxsize=64)
    stop_event = threading.Event()
    t_start = time.time()
    logf = open(LOGFILE, "a", encoding="utf-8")

    def emit(text: str):
        line = f"[{fmt_ts(time.time() - t_start)}] {text}"
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    # ---- Capture Thread: mic 48k stereo -> 16k mono blocks ----
    def capture_main():
        instream = sd.InputStream(samplerate=NATIVE_SR, channels=CHANNELS,
                                  dtype="float32",
                                  blocksize=int(NATIVE_SR * FEED_BLOCK_S),
                                  device=DEVICE)
        instream.start()
        try:
            while not stop_event.is_set():
                try:
                    block, _ = instream.read(int(NATIVE_SR * FEED_BLOCK_S))
                except Exception as e:
                    print(f"[CAPTURE] read error (continuing): {e}", flush=True)
                    time.sleep(0.1)
                    continue
                mono48 = block.mean(axis=1).astype(np.float32)
                mono16 = resample_poly(mono48, TARGET_SR // GCD, NATIVE_SR // GCD)
                try:
                    audio_queue.put(np.asarray(mono16, dtype=np.float32), timeout=1.0)
                except queue.Full:
                    drop_count[0] += 1  # recognizer is behind; drop block, stay realtime
        finally:
            try:
                instream.stop()
                instream.close()
            except Exception:
                pass

    cap_thread = threading.Thread(target=capture_main, daemon=True)
    cap_thread.start()
    print("[READY] speak now (Ctrl-C to stop).")

    last_text = ""
    drop_count = [0]
    try:
        while True:
            try:
                chunk = audio_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            stream.accept_waveform(TARGET_SR, chunk)
            while recognizer.is_ready(stream):
                recognizer.decode_stream(stream)
            cur = recognizer.get_result(stream).strip()
            if cur and cur != last_text:
                emit(cur[len(last_text):].strip() if cur.startswith(last_text) else cur)
                last_text = cur
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        cap_thread.join(timeout=5)
        stream.input_finished()
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
        final = recognizer.get_result(stream).strip()
        if final and final != last_text:
            emit(final[len(last_text):].strip() if final.startswith(last_text) else final)
        logf.close()
        if drop_count[0]:
            print(f"[WARN] dropped {drop_count[0]} blocks (recognizer slower than realtime)")
        print("[DONE] stopped.")


if __name__ == "__main__":
    main()
