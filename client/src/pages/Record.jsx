import { useEffect, useRef, useState } from "react";
import { useSearchParams, Link } from "react-router-dom";
import { api, wsUrl, grantStore, fmtClock } from "../lib/api.js";

// Raw 16kHz mono Int16 over the wire: zero server-side decode, exact recognizer input.
// Batched into ~256ms frames: per-message WS + per-chunk recognizer overhead makes
// micro-frames run slower than realtime (measured: 20s audio -> 16s growing delay).
const WORKLET_SRC = `
class Cap extends AudioWorkletProcessor {
  constructor() {
    super();
    this.carry = new Float32Array(0);
    this.step = sampleRate / 16000;
    this.BLOCK = 4096; // 16kHz samples per posted frame (~256ms)
    this.buf = new Int16Array(this.BLOCK);
    this.n = 0;
  }
  pushChunk(out) {
    // append a resampled Int16Array to the block buffer, posting full blocks
    let off = 0;
    while (off < out.length) {
      const room = this.BLOCK - this.n;
      const k = Math.min(room, out.length - off);
      this.buf.set(out.subarray(off, off + k), this.n);
      this.n += k; off += k;
      if (this.n === this.BLOCK) {
        const send = this.buf.slice();
        this.port.postMessage(send.buffer, [send.buffer]);
        this.n = 0;
      }
    }
  }
  process(inputs) {
    const chs = inputs[0];
    if (!chs || !chs.length) return true;
    const n = chs[0].length;
    const mono = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      let s = 0;
      for (let c = 0; c < chs.length; c++) s += chs[c][i];
      mono[i] = s / chs.length;
    }
    const all = new Float32Array(this.carry.length + n);
    all.set(this.carry, 0); all.set(mono, this.carry.length);
    const outLen = Math.floor((all.length - 1) / this.step);
    if (outLen > 0) {
      const out = new Int16Array(outLen);
      for (let i = 0; i < outLen; i++) {
        const pos = i * this.step;
        const i0 = Math.floor(pos), f = pos - i0;
        const v = all[i0] * (1 - f) + (all[i0 + 1] ?? all[i0]) * f;
        out[i] = Math.max(-32768, Math.min(32767, Math.round(v * 32768)));
      }
      this.pushChunk(out);
      this.carry = all.slice(Math.floor(outLen * this.step));
    } else {
      this.carry = all;
    }
    return true;
  }
}
registerProcessor("cap", Cap);
`;

export default function Record() {
  const [params, setParams] = useSearchParams();
  const room = params.get("room") || "live";
  const [rooms, setRooms] = useState([]);
  const [grant, setGrant] = useState(() => grantStore.get(room));
  const [state, setState] = useState("idle"); // idle | recording | error | denied
  const [error, setError] = useState("");
  const [elapsed, setElapsed] = useState(0);
  const [level, setLevel] = useState(0);
  const stopRef = useRef(null);
  const levelRef = useRef(0);
  const wsRef = useRef(null);
  const stoppingRef = useRef(true);
  const wakeRef = useRef(null);
  const zeroBlocks = useRef(0);
  const [conn, setConn] = useState("live"); // live | reconnecting
  const [micWarn, setMicWarn] = useState(false);
  const [wakeHeld, setWakeHeld] = useState(false);

  useEffect(() => {
    api.listRooms().then((r) => setRooms(r.rooms || [])).catch(() => {});
  }, []);

  useEffect(() => {
    setGrant(grantStore.get(room));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [room]);

  // Screen Wake Lock: without it the phone sleeps (~5 min) and the
  // browser suspends the mic + socket, which is exactly the 5-minute stop.
  async function acquireWake() {
    try {
      if (!("wakeLock" in navigator)) return;
      wakeRef.current = await navigator.wakeLock.request("screen");
      setWakeHeld(true);
      wakeRef.current?.addEventListener("release", () => setWakeHeld(false));
    } catch {}
  }
  function releaseWake() {
    try {
      wakeRef.current?.release();
    } catch {}
    wakeRef.current = null;
    setWakeHeld(false);
  }
  useEffect(() => {
    const onVis = () => {
      if (document.visibilityState === "visible" && !stoppingRef.current) acquireWake();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, []);
  useEffect(
    () => () => {
      stoppingRef.current = true;
      stopRef.current?.();
    },
    []
  );
  useEffect(() => {
    if (state !== "recording") return;
    const t = setInterval(() => setLevel(levelRef.current), 120);
    return () => clearInterval(t);
  }, [state]);

  const roomInfo = rooms.find((r) => r.id === room);
  const needsGrant = roomInfo?.require_permission && !grant;

  function persistGrant(v) {
    setGrant(v);
    grantStore.set(room, v.trim());
  }

  async function start() {
    setError("");
    if (roomInfo?.require_permission && !grant.trim()) {
      setError("این اتاق نیازمند مجوز ضبط است — کد اعطا شده توسط ادمین را وارد کنید.");
      setState("denied");
      return;
    }
    try {
      // Raw mic for ASR fidelity: browser echo/noise/AGC processing distorts
      // speech features (hurts Persian recognition). No feedback risk: we never
      // play the mic back (silent gain only).
      const mic = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: false, noiseSuppression: false, autoGainControl: false,
          channelCount: 1, sampleRate: 16000,
        },
      });
      // Ask the browser to do the 48kHz->16kHz resampling natively (high-quality
      // polyphase) instead of the worklet's cheap linear interpolation, which
      // aliases and hurts Persian recognition. Falls back to device rate if 16k
      // is not supported; the worklet still handles any residual ratio.
      let ctx;
      try {
        ctx = new AudioContext({ sampleRate: 16000 });
      } catch {
        ctx = new AudioContext();
      }
      await ctx.resume();
      const url = URL.createObjectURL(new Blob([WORKLET_SRC], { type: "application/javascript" }));
      await ctx.audioWorklet.addModule(url);
      const node = new AudioWorkletNode(ctx, "cap");
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 512;

      stoppingRef.current = false;
      zeroBlocks.current = 0;
      setMicWarn(false);
      setConn("live");
      await acquireWake();

      // Open one socket + hello handshake. Throws {denied, msg} on refusal.
      async function openSocket() {
        const s = new WebSocket(wsUrl("/ws/lecturer", { room, grant: grant.trim() }));
        // Handshake order must match server: client sends hello FIRST, server replies ready.
        await new Promise((resolve, reject) => {
          const to = setTimeout(() => reject(new Error("timeout opening socket")), 8000);
          s.onopen = () => {
            clearTimeout(to);
            resolve();
          };
          s.onerror = () => {
            clearTimeout(to);
            reject(new Error("websocket error"));
          };
        });
        s.send(JSON.stringify({ type: "hello", sampleRate: 16000 }));
        const refused = await new Promise((resolve, reject) => {
          const to = setTimeout(() => reject(new Error("timeout waiting for server")), 8000);
          s.onmessage = (ev) => {
            try {
              const m = JSON.parse(ev.data);
              if (m.type === "ready") {
                clearTimeout(to);
                resolve(null);
              } else if (m.type === "denied" || m.type === "busy" || m.type === "error") {
                clearTimeout(to);
                resolve(m);
              }
            } catch {}
          };
          s.onerror = () => {
            clearTimeout(to);
            reject(new Error("websocket error"));
          };
          s.onclose = () => {
            clearTimeout(to);
            resolve({ type: "denied", text: "اتصال توسط سرور بسته شد (room/grant را بررسی کنید)" });
          };
        });
        if (refused) {
          try {
            s.close();
          } catch {}
          throw { denied: true, msg: refused.text || "دسترسی رد شد" };
        }
        s.onmessage = (ev) => {
          try {
            const m = JSON.parse(ev.data);
            if (m.type === "denied" || m.type === "busy") {
              setError(m.text);
              setState("denied");
              fullStop();
            }
          } catch {}
        };
        s.onerror = () => {};
        s.onclose = () => {
          if (!stoppingRef.current) reconnect();
        };
        return s;
      }

      async function reconnect() {
        if (stoppingRef.current) return;
        setConn("reconnecting");
        for (let i = 0; i < 15 && !stoppingRef.current; i++) {
          await new Promise((r) => setTimeout(r, 2000));
          if (stoppingRef.current) return;
          try {
            wsRef.current = await openSocket();
            setConn("live");
            return;
          } catch (e) {
            if (e?.denied) {
              setError(e.msg);
              setState("denied");
              fullStop();
              return;
            }
          }
        }
        if (!stoppingRef.current) {
          setError("ارتباط با سرور قطع شد و وصل مجدد موفق نبود.");
          setState("error");
          fullStop();
        }
      }

      function teardown() {
        clearInterval(timer);
        clearInterval(meter);
        try {
          wsRef.current?.close();
        } catch {}
        wsRef.current = null;
        try {
          mic?.getTracks().forEach((t) => t.stop());
        } catch {}
        try {
          node?.disconnect();
          src?.disconnect();
          meterSrc?.disconnect();
        } catch {}
        try {
          ctx?.close();
        } catch {}
        if (url) URL.revokeObjectURL(url);
      }
      function fullStop() {
        stoppingRef.current = true;
        releaseWake();
        teardown();
        levelRef.current = 0;
        setLevel(0);
      }

      const ws = await openSocket().catch((e) => {
        if (e?.denied) {
          teardown();
          setError(e.msg);
          setState("denied");
          return null;
        }
        throw e;
      });
      if (!ws) return;
      wsRef.current = ws;

      const t0 = Date.now();
      const timer = setInterval(() => setElapsed(Math.floor((Date.now() - t0) / 1000)), 500);
      const meterSrc = ctx.createMediaStreamSource(mic);
      meterSrc.connect(analyser);
      const buf = new Uint8Array(analyser.frequencyBinCount);
      const meter = setInterval(() => {
        analyser.getByteTimeDomainData(buf);
        let peak = 0;
        for (let i = 0; i < buf.length; i++) peak = Math.max(peak, Math.abs(buf[i] - 128));
        levelRef.current = Math.min(1, peak / 90);
      }, 100);

      node.port.onmessage = (ev) => {
        const cur = wsRef.current;
        if (!cur || cur.readyState !== WebSocket.OPEN) return;
        // Dead-mic detector: a live mic never emits exact zeros; a suspended
        // track (sleeping phone) does. Warn instead of streaming silence.
        const v = new Int16Array(ev.data);
        let peak = 0;
        for (let i = 0; i < v.length; i += 7) {
          const a = Math.abs(v[i]);
          if (a > peak) peak = a;
        }
        if (peak === 0) {
          zeroBlocks.current += 1;
          if (zeroBlocks.current === 12) setMicWarn(true);
        } else {
          if (zeroBlocks.current >= 12) setMicWarn(false);
          zeroBlocks.current = 0;
        }
        cur.send(ev.data);
      };
      const src = ctx.createMediaStreamSource(mic);
      const silent = ctx.createGain();
      silent.gain.value = 0;
      src.connect(node);
      node.connect(silent);
      silent.connect(ctx.destination);
      setState("recording");
      stopRef.current = () => {
        stoppingRef.current = true;
        releaseWake();
        teardown();
        setState("idle");
        setElapsed(0);
        setConn("live");
        setMicWarn(false);
        levelRef.current = 0;
        setLevel(0);
      };
    } catch (e) {
      setError(friendlyWsError(e));
      setState("error");
    }
  }

  function friendlyWsError(e) {
    const m = e?.message || String(e);
    if (/insecure/i.test(m) && /websocket/i.test(m))
      return (
        "مرورگر اتصال ناامن را بست (صفحه https، سوکت ws://). " +
        "این نسخه خودکار از اتصال امن هم‌مبدأ استفاده می‌کند — صفحه را کامل رفرش کنید " +
        "(کش را دور بزنید) و با همان آدرس https وارد شوید. اگر ادامه داشت، گواهی موقت مرورگر " +
        "را پذیرفته باشید (هشدار امنیتی صفحه) و از یک وای‌فای با گوشی باشید."
      );
    return m;
  }

  return (
    <>
      <div className="live-head">
        <h2 style={{ margin: 0 }}>🎙️ ضبط کلاس</h2>
        <span className="spacer" />
        <select className="select" style={{ width: 220 }} value={room} onChange={(e) => setParams({ room: e.target.value })}>
          {rooms.map((r) => (
            <option key={r.id} value={r.id}>
              {r.name} ({r.id})
            </option>
          ))}
          {rooms.length === 0 && <option value="live">live</option>}
        </select>
      </div>

      <div className="grid cols-2">
        <div className="card" style={{ textAlign: "center" }}>
          <div className={`eq ${state === "recording" ? "" : "paused"}`} style={{ justifyContent: "center", height: 34 }}>
            <i />
            <i />
            <i />
            <i />
            <i />
          </div>
          <div style={{ margin: "18px 0" }}>
            {state === "recording" ? (
              <button className="rec-btn stop" onClick={() => stopRef.current?.()}>
                ⏹ توقف
                <br />
                <small>{fmtClock(elapsed)}</small>
              </button>
            ) : (
              <button className="rec-btn" onClick={start}>
                ● شروع ضبط
              </button>
            )}
          </div>
          {state === "recording" ? (
            <>
              <p>
                <span className="rec-dot on" /> در حال ضبط {fmtClock(elapsed)} · اتاق{" "}
                <code className="inline">{room}</code>
              </p>
              <p className="small muted">
                {wakeHeld ? "🔒 صفحه روشن می‌ماند" : "⚠️ قفل صفحه ممکن است ضبط را قطع کند"}
              </p>
              {conn === "reconnecting" && (
                <p style={{ color: "#fcd34d" }}>↻ اتصال قطع شد؛ تلاش برای وصل مجدد…</p>
              )}
              {micWarn && (
                <p style={{ color: "#fda4af" }}>
                  ⚠️ میکروفون سیگنالی نمی‌فرستد (صفحه قفل شده؟) — بررسی کنید
                </p>
              )}
            </>
          ) : (
            <p className="muted small">
              روی گوشی باید از HTTPS استفاده شود تا میکروفون اجازه بگیرد.
            </p>
          )}
          {/* level meter */}
          <div style={{ height: 10, borderRadius: 6, background: "rgba(255,255,255,.08)", overflow: "hidden", marginTop: 12 }}>
            <div
              style={{
                height: "100%",
                width: `${Math.round(level * 100)}%`,
                background: "linear-gradient(90deg,#2dd4bf,#a78bfa,#fbbf24)",
                transition: "width .12s linear",
              }}
            />
          </div>
          {(state === "error" || state === "denied") && (
            <p style={{ color: "#fda4af" }}>⚠️ {error}</p>
          )}
          <p>
            <Link to={`/live?room=${encodeURIComponent(room)}`}>مشاهده متن زنده ←</Link>
          </p>
        </div>

        <div className="card">
          <h3>🔑 مجوز ضبط</h3>
          {roomInfo ? (
            <p className="small">
              اتاق <b>{roomInfo.name}</b>{" "}
              {roomInfo.require_permission ? (
                <span className="pill">🔒 نیازمند کد مجوز ادمین</span>
              ) : (
                <span className="pill live">🔓 ضبط آزاد</span>
              )}
            </p>
          ) : (
            <p className="muted small">در حال بررسی وضعیت اتاق…</p>
          )}
          <label className="lbl">کد مجوز (grant code)</label>
          <input
            className="input"
            placeholder="مثلاً aB3xK9qZ — اگر اتاق آزاد است خالی بگذارید"
            value={grant}
            onChange={(e) => persistGrant(e.target.value)}
            dir="ltr"
            style={{ textAlign: "center", letterSpacing: 2 }}
          />
          <p className="small muted" style={{ lineHeight: 1.9 }}>
            • ادمین از پنل <code className="inline">Permissions</code> برای هر مدرس یک کد می‌سازد و
            همین‌جا وارد می‌کنید (در مرورگر ذخیره می‌شود).
            <br />
            • بدون کد معتبر، سرور پیام <code className="inline">denied</code> می‌دهد و ضبط شروع
            نمی‌شود.
            <br />• اگر هم‌زمان مدرس دیگری زنده باشد، پیام <code className="inline">busy</code>{" "}
            می‌گیرید.
          </p>
          {needsGrant && <p style={{ color: "#fcd34d" }}>⚠️ این اتاق گیت شده است؛ اول کد را وارد کنید.</p>}
        </div>
      </div>
    </>
  );
}
