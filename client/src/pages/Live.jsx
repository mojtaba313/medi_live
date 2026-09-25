import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams, Link } from "react-router-dom";
import { api, wsUrl, fmtClock } from "../lib/api.js";

let fallbackId = -1;

export default function Live() {
  const [params, setParams] = useSearchParams();
  const room = params.get("room") || "live";
  const [rooms, setRooms] = useState([]);
  const [items, setItems] = useState([]);
  const [status, setStatus] = useState("connecting");
  const [meta, setMeta] = useState({ lecturer: false, viewers: 0, fed_s: 0, total_s: 0 });
  const [finished, setFinished] = useState(false);
  const [filter, setFilter] = useState("all"); // all | raw | corrected
  const [query, setQuery] = useState("");
  const [autoScroll, setAutoScroll] = useState(true);
  const bottomRef = useRef(null);

  useEffect(() => {
    api.listRooms().then((r) => setRooms(r.rooms || [])).catch(() => {});
  }, []);

  useEffect(() => {
    setItems([]);
    setFinished(false);
    let ws;
    let retry;
    let closed = false;

    function applyMessage(msg) {
      if (msg.type === "transcript") {
        const id = msg.id ?? fallbackId--;
        setItems((prev) => [...prev.slice(-499), { key: `raw-${id}`, id, stage: msg.stage || "raw", text: msg.text, audio_pos: msg.audio_pos }]);
      } else if (msg.type === "correction") {
        const ids = new Set(msg.ids || []);
        setItems((prev) => {
          const kept = ids.size ? prev.filter((it) => it.id == null || !ids.has(it.id)) : prev;
          return [...kept.slice(-499), { key: `fixed-${(msg.ids || []).join("-")}-${msg.ts || Date.now()}`, id: null, stage: "corrected", text: msg.text, audio_pos: msg.audio_pos }];
        });
      }
    }

    function connect() {
      if (closed) return;
      setStatus("connecting");
      ws = new WebSocket(wsUrl("/ws", { room }));
      ws.onopen = () => setStatus("connected");
      ws.onclose = () => {
        setStatus("reconnecting");
        if (!closed) retry = setTimeout(connect, 2000);
      };
      ws.onerror = () => ws.close();
      ws.onmessage = (ev) => {
        let msg;
        try {
          msg = JSON.parse(ev.data);
        } catch {
          return;
        }
        if (msg.type === "transcript" || msg.type === "correction") applyMessage(msg);
        else if (msg.type === "status") {
          setMeta({
            lecturer: !!msg.lecturer_connected,
            viewers: msg.viewers ?? 0,
            fed_s: msg.fed_s ?? 0,
            total_s: msg.total_s ?? 0,
          });
        } else if (msg.type === "done") setFinished(true);
        else if (msg.type === "error") setStatus("error:" + msg.text);
      };
    }
    connect();
    return () => {
      closed = true;
      clearTimeout(retry);
      ws?.close();
    };
  }, [room]);

  useEffect(() => {
    if (autoScroll) bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [items, autoScroll]);

  const visible = useMemo(
    () =>
      items.filter((it) => {
        if (filter !== "all" && (filter === "corrected") !== (it.stage === "corrected")) return false;
        if (query.trim() && !it.text.includes(query.trim())) return false;
        return true;
      }),
    [items, filter, query]
  );

  const rawN = items.filter((i) => i.stage !== "corrected").length;
  const fixedN = items.filter((i) => i.stage === "corrected").length;

  function exportTxt() {
    const txt = visible.map((l) => `[${l.audio_pos}] ${l.text}`).join("\n");
    const blob = new Blob([txt], { type: "text/plain;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `transcript-${room}.txt`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  return (
    <>
      <div className="live-head">
        <h2 style={{ margin: 0 }}>📡 پخش زنده {status === "connected" ? "" : `(${status})`}</h2>
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

      <div className="card">
        <div className="row">
          <span className={`rec-dot ${meta.lecturer ? "on" : "off"}`} />
          <b>{meta.lecturer ? "مدرس متصل است" : "در انتظار مدرس…"}</b>
          <span className="pill">👁️ {meta.viewers} بیننده</span>
          {(meta.fed_s > 0 || meta.total_s > 0) && (
            <span className="pill">
              ⏱️ {fmtClock(meta.fed_s)}
              {meta.total_s ? ` / ${fmtClock(meta.total_s)}` : ""}
            </span>
          )}
          <span className="spacer" />
          <div className={`eq ${meta.lecturer ? "" : "paused"}`}>
            <i />
            <i />
            <i />
            <i />
            <i />
          </div>
          <Link className="btn" to={`/record?room=${encodeURIComponent(room)}`}>
            🎙️ رفتن به ضبط
          </Link>
        </div>
        <div className="row mt">
          <span className="tag raw">زرد = خام (مدل گفتار، فوری)</span>
          <span className="tag fixed">سبز = اصلاح‌شده (مدل متنی، جایگزین)</span>
          <span className="spacer" />
          <span className="small muted">
            {items.length} پیام · {rawN} خام · {fixedN} اصلاح‌شده
          </span>
        </div>
        <div className="row mt">
          <input className="input" style={{ maxWidth: 260 }} placeholder="🔍 جست‌وجو در متن…" value={query} onChange={(e) => setQuery(e.target.value)} />
          <div className="tabs" style={{ margin: 0 }}>
            {["all", "raw", "corrected"].map((f) => (
              <button key={f} className={filter === f ? "on" : ""} onClick={() => setFilter(f)}>
                {f === "all" ? "همه" : f === "raw" ? "خام" : "اصلاح‌شده"}
              </button>
            ))}
          </div>
          <span className="spacer" />
          <label className="small muted">
            <input type="checkbox" checked={autoScroll} onChange={(e) => setAutoScroll(e.target.checked)} /> اسکرول خودکار
          </label>
          <button className="btn" onClick={exportTxt} disabled={visible.length === 0}>
            ⬇ خروجی متن
          </button>
        </div>
      </div>

      {finished && (
        <div className="card mt">
          <b>🏁 پایان جلسه.</b> <span className="muted small">فایل صوتی و رونوشت کامل در آرشیو ادمین ذخیره شد.</span>
        </div>
      )}

      <div className="timeline mt">
        {visible.length === 0 && <div className="empty">هنوز متنی نرسیده — وقتی مدرس صحبت کند اینجا ظاهر می‌شود.</div>}
        {visible.map((l) => {
          const corrected = l.stage === "corrected";
          return (
            <div key={l.key} className={`msg ${corrected ? "fixed" : "raw"}`}>
              <div className="meta">
                <span>[{l.audio_pos}]</span>
                <span className={`tag ${corrected ? "fixed" : "raw"}`}>{corrected ? "اصلاح‌شده" : "خام"}</span>
              </div>
              <div style={{ marginTop: 4 }}>{l.text}</div>
            </div>
          );
        })}
        <div ref={bottomRef} />
      </div>
    </>
  );
}
