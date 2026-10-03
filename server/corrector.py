#!/usr/bin/env python3
"""Two-stage transcript correction: raw ASR text first, corrected text later.

Strategy (weak-system friendly):
  1. Raw Shenava output is shown immediately (no added latency).
  2. Raw segments are buffered into sentence-ish chunks and corrected
     asynchronously; when the corrected version is ready it REPLACES the raw
     one(s) on the client (matched by segment ids).

Backends (no heavy local model required):
  - "local"  : instant pure-Python Persian normalizer (arabic->persian chars,
               ZWNJ fixes for mi-/nem- prefixes and -ha suffixes, punctuation
               spacing, whitespace collapse). Always available, ~0ms on CPU.
  - "openai" : any OpenAI-compatible chat API (OpenAI, OpenRouter, DeepSeek,
               Gemini OpenAI-endpoint, self-hosted vLLM, ...) via
               OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL.
  - "ollama" : local Ollama server (OpenAI-compatible /v1/chat/completions
               if present, else legacy /api/chat) via OLLAMA_URL/OLLAMA_MODEL.
               This is the "local text model" option when the machine can run
               e.g. qwen2.5:1.5b. Falls back to "local" per-call on failure.
  - "off"    : no correction messages emitted.
  - "auto"   : OPENAI_API_KEY set -> openai (+local pre/post normalize);
               BACKEND=ollama explicitly -> ollama; otherwise -> local only.

Only dependency beyond stdlib is httpx (already required by the server env).
"""

from __future__ import annotations

import logging
import os
import re
import time
from difflib import get_close_matches
from pathlib import Path

log = logging.getLogger("corrector")

# ------------------------------------------------------------------ config

CORRECT_MIN_CHARS = int(os.getenv("CORRECT_MIN_CHARS", "50"))
CORRECT_MAX_CHARS = int(os.getenv("CORRECT_MAX_CHARS", "400"))
CORRECT_TIMEOUT_S = float(os.getenv("CORRECT_TIMEOUT_S", "5.0"))
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "30.0"))
LLM_MAX_CONTEXT = int(os.getenv("LLM_MAX_CONTEXT", "600"))  # chars of prior corrected text

SYSTEM_PROMPT = (
    "You are a Persian (Farsi) lecture ASR post-processor. "
    "Fix misrecognized words using surrounding context, restore correct Persian spelling "
    "with proper ZWNJ (نیم‌فاصله), punctuation (، ؟ .) and sentence boundaries. "
    "Complete a cut-off final sentence ONLY if the intent is obvious, otherwise end it cleanly. "
    "Do NOT add new facts, do NOT translate, do NOT explain. "
    "Return ONLY the corrected Persian text, no quotes, no preamble."
)

# ------------------------------------------------------- local normalizer

_AR2FA = {
    "\u064a": "\u06cc",  # ي -> ی
    "\u0649": "\u06cc",  # ى -> ی
    "\u0643": "\u06a9",  # ك -> ک
    "\u0629": "\u0647",  # ة -> ه
    "\u0640": "",        # tatweel (ـ) -> remove
}
_AR2FA_RE = re.compile("|".join(re.escape(k) for k in _AR2FA))

_PREFIX_ZWNJ = re.compile(r"\b(می|نمی)\s+(?=\S)")
_SUFFIX_ZWNJ = re.compile(r"(?<=\S)\s+(ها|های|هایی|تر|ترین|ام|ای|ایم|اید|اند)\b")
_WS_BEFORE_PUNCT = re.compile(r"\s+([،؛؟!.:?!,)])")
_WS_AFTER_PUNCT = re.compile(r"([،؛؟!?:])(?=\S)")
_MULTI_SPACE = re.compile(r"\s+")


def local_normalize(text: str) -> str:
    """Fast deterministic Persian cleanup. Pure python, no model."""
    if not text:
        return text
    ZWNJ = "‌"
    t = _AR2FA_RE.sub(lambda m: _AR2FA[m.group(0)], text)
    t = t.replace(ZWNJ + ZWNJ, ZWNJ)
    t = _PREFIX_ZWNJ.sub(lambda m: m.group(1) + ZWNJ, t)
    t = _SUFFIX_ZWNJ.sub(lambda m: ZWNJ + m.group(1), t)
    t = _WS_BEFORE_PUNCT.sub(lambda m: m.group(1), t)
    t = _WS_AFTER_PUNCT.sub(lambda m: m.group(1) + " ", t)
    t = _MULTI_SPACE.sub(" ", t).strip()
    return t


_SENT_END = re.compile(r"[.؟?!…]+$|.*[.؟?!…]['\"»)\]]?\s*$")


def looks_sentence_complete(text: str) -> bool:
    return bool(_SENT_END.match(text.strip()))


# ------------------------------------------------- medical glossary + ITN

_HERE = Path(__file__).resolve().parent
_GLOSSARY_PATH = Path(os.getenv("MEDICAL_GLOSSARY", str(_HERE / "medical_glossary.json")))

# Canonical medical/anatomy terms shared by ALL rooms. Loaded from
# medical_glossary.json next to this file; missing file -> empty list
# (normalizer still works). Per-class terms live in SQLite (db.py) and are
# layered on top via TextCorrector.set_room_terms().
GLOSSARY_TERMS: list[str] = []
try:
    if _GLOSSARY_PATH.exists():
        import json as _json
        _data = _json.loads(_GLOSSARY_PATH.read_text(encoding="utf-8"))
        GLOSSARY_TERMS = [str(t).strip() for t in _data.get("terms", []) if str(t).strip()]
except Exception as e:
    log.warning("medical glossary load failed (%s); continuing without it", e)

# Lowercased lookup for Latin terms: ASR often emits them lowercase
# ("cornea") while the glossary holds canonical case ("Cornea").
_GLOSSARY_LOWER = {t.lower(): t for t in GLOSSARY_TERMS if t.isascii()}

# Spoken Persian numbers -> Persian digits (single tokens only; conservative).
_SPOKEN_DIGITS = {
    "صفر": "۰", "یک": "۱", "دو": "۲", "سه": "۳", "چهار": "۴",
    "پنج": "۵", "شش": "۶", "هفت": "۷", "هشت": "۸", "نه": "۹",
    "ده": "۱۰", "یازده": "۱۱", "دوازده": "۱۲", "سیزده": "۱۳",
    "چهارده": "۱۴", "پانزده": "۱۵", "شانزده": "۱۶", "هفده": "۱۷",
    "هجده": "۱۸", "نوزده": "۱۹", "بیست": "۲۰", "سی": "۳۰",
    "چهل": "۴۰", "پنجاه": "۵۰", "شصت": "۶۰", "هفتاد": "۷۰",
    "هشتاد": "۸۰", "نود": "۹۰", "صد": "۱۰۰", "دویست": "۲۰۰",
    "هزار": "۱۰۰۰",
}

_STRIP_PUNCT = "،؛؟!.:?!,…«»\"'\"()[]{}<>-–—/\\"


def apply_medical_glossary(text: str, terms: list[str] | None = None) -> str:
    """Snap near-miss words to glossary terms (fixes CTC mishearing of
    anatomical/Latin terms). Conservative: length-gated words, similarity
    cutoff scaled by length, never touches words already correct.

    `terms`: combined global + per-room list. None -> global list only
    (backward compatible).
    """
    terms = GLOSSARY_TERMS if terms is None else terms
    if not text or not terms:
        return text
    term_set = set(terms)
    lower = {t.lower(): t for t in terms if t.isascii()}
    out = []
    for w in text.split():
        core = w
        # split leading/trailing punctuation properly
        lead, trail = "", ""
        while core and core[0] in _STRIP_PUNCT:
            lead += core[0]
            core = core[1:]
        while core and core[-1] in _STRIP_PUNCT:
            trail = core[-1] + trail
            core = core[:-1]
        # Latin-script words can be short ("Lens", "RPE") — script mismatch
        # with Persian text makes short matches safe; Persian words need >= 6.
        min_len = 4 if core.isascii() and core.isalpha() else 6
        if len(core) >= min_len and core not in term_set:
            # Shorter words need a looser cutoff: one substitution in a
            # 6-char word scores ~0.83, so 0.86 would never fire there.
            cutoff = 0.8 if len(core) < 8 else 0.86
            if core.isascii():
                m = get_close_matches(core.lower(), list(lower),
                                      n=1, cutoff=cutoff)
                if m:
                    core = lower[m[0]]
            else:
                m = get_close_matches(core, terms, n=1, cutoff=cutoff)
                if m:
                    core = m[0]
        out.append(lead + core + trail)
    return " ".join(out)


def spoken_to_digits(text: str) -> str:
    """Map standalone spoken number words to Persian digits (display ITN:
    the acoustic model emits spoken form, e.g. 'پنج' for ۵)."""
    if not text:
        return text
    return " ".join(_SPOKEN_DIGITS.get(w, w) for w in text.split())


# ------------------------------------------------------- LLM backends

def _chat_messages(raw: str, context: str) -> list[dict]:
    user = raw
    if context:
        user = f"زمینه قبلی (برای پیوستگی):\n{context}\n\nمتن جدید ASR:\n{raw}"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


async def _openai_chat(base_url: str, api_key: str, model: str,
                       messages: list[dict], timeout_s: float) -> str:
    import httpx

    url = base_url.rstrip("/") + "/chat/completions"
    async with httpx.AsyncClient(timeout=timeout_s) as cli:
        r = await cli.post(url, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }, json={
            "model": model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 800,
        })
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"].strip()


async def _ollama_chat(base_url: str, model: str,
                       messages: list[dict], timeout_s: float) -> str:
    """Try OpenAI-compatible endpoint first, fall back to legacy /api/chat."""
    import httpx

    base = base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=timeout_s) as cli:
        try:
            r = await cli.post(base + "/v1/chat/completions", json={
                "model": model, "messages": messages,
                "temperature": 0.1, "max_tokens": 800,
            })
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            log.debug("ollama /v1 failed (%s), trying /api/chat", e)
            r = await cli.post(base + "/api/chat", json={
                "model": model, "messages": messages, "stream": False,
                "options": {"temperature": 0.1},
            })
            r.raise_for_status()
            return r.json()["message"]["content"].strip()


# ------------------------------------------------------- main class


class TextCorrector:
    """Always applies local_normalize; optionally enhances with an LLM."""

    def __init__(self, backend: str = "auto"):
        backend = (backend or "auto").lower().strip()
        self.requested = backend
        self.openai_key = os.getenv("OPENAI_API_KEY", "").strip()
        self.openai_base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()
        self.openai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
        self.ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434").strip()
        self.ollama_model = os.getenv("OLLAMA_MODEL", "qwen2.5:1.5b").strip()

        if backend == "off":
            self.backend = "off"
        elif backend == "openai":
            self.backend = "openai" if self.openai_key else "local"
            if self.backend == "local":
                log.warning("CORRECTOR_BACKEND=openai but OPENAI_API_KEY is empty -> local only")
        elif backend == "ollama":
            self.backend = "ollama"
        elif backend == "local":
            self.backend = "local"
        else:  # auto
            self.backend = "openai" if self.openai_key else "local"

        log.info("corrector backend: requested=%s effective=%s", self.requested, self.backend)
        log.info("medical glossary: %d global terms from %s", len(GLOSSARY_TERMS), _GLOSSARY_PATH.name)
        # Per-room (per-class) term sets, filled from SQLite at startup and
        # refreshed on every admin glossary edit. room_id -> list of terms.
        self._room_terms: dict[str, list[str]] = {}

    def set_room_terms(self, room_id: str, terms: list[str]) -> None:
        """Replace the cached per-room set (called at startup + on admin edits)."""
        cleaned = [str(t).strip() for t in (terms or []) if str(t).strip()]
        if cleaned:
            self._room_terms[room_id] = cleaned
        else:
            self._room_terms.pop(room_id, None)

    def terms_for(self, room_id: str | None) -> list[str]:
        """Global terms + this room's terms, deduplicated (global first)."""
        if not room_id or room_id not in self._room_terms:
            return GLOSSARY_TERMS
        seen = set(GLOSSARY_TERMS)
        extra = [t for t in self._room_terms[room_id] if t not in seen]
        return GLOSSARY_TERMS + extra

    @property
    def enabled(self) -> bool:
        return self.backend != "off"

    @property
    def uses_llm(self) -> bool:
        return self.backend in ("openai", "ollama")

    def describe(self) -> dict:
        model = ""
        if self.backend == "openai":
            model = self.openai_model
        elif self.backend == "ollama":
            model = self.ollama_model
        return {"backend": self.backend, "model": model,
                "min_chars": CORRECT_MIN_CHARS, "max_chars": CORRECT_MAX_CHARS,
                "timeout_s": CORRECT_TIMEOUT_S,
                "glossary_global_n": len(GLOSSARY_TERMS),
                "glossary_rooms": {rid: len(t) for rid, t in self._room_terms.items()}}

    async def correct(self, raw: str, context: str = "", room_id: str | None = None) -> str | None:
        """Return corrected text, or None if disabled/empty/unusable."""
        raw = (raw or "").strip()
        if not raw or not self.enabled:
            return None
        terms = self.terms_for(room_id)
        normalized = local_normalize(raw)
        normalized = spoken_to_digits(apply_medical_glossary(normalized, terms))
        if not self.uses_llm:
            return normalized
        ctx = (context or "")[-LLM_MAX_CONTEXT:]
        try:
            t0 = time.time()
            if self.backend == "openai":
                out = await _openai_chat(self.openai_base, self.openai_key,
                                         self.openai_model,
                                         _chat_messages(normalized, ctx),
                                         LLM_TIMEOUT_S)
            else:
                out = await _ollama_chat(self.ollama_url, self.ollama_model,
                                         _chat_messages(normalized, ctx),
                                         LLM_TIMEOUT_S)
            out = local_normalize(out.strip())
            out = spoken_to_digits(apply_medical_glossary(out, terms))
            # Guard: LLM must not balloon or empty the text.
            if not out or len(out) > max(2000, len(normalized) * 3):
                log.warning("LLM correction rejected (len %d -> %d)", len(normalized), len(out))
                return normalized
            log.info("LLM correction %.1fs (%d -> %d chars)", time.time() - t0,
                     len(normalized), len(out))
            return out
        except Exception as e:
            log.warning("LLM correction failed (%s); falling back to local normalize", e)
            return normalized


def load_corrector_from_env() -> TextCorrector:
    return TextCorrector(backend=os.getenv("CORRECTOR_BACKEND", "auto"))
