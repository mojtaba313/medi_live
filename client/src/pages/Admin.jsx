import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, adminToken, API_BASE } from "../lib/api.js";

export default function Admin() {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") || "rooms";
  const [token, setToken] = useState(adminToken.get());
  const [authed, setAuthed] = useState(false);
  const [authErr, setAuthErr] = useState("");
  const [corrector, setCorrector] = useState(null);

  async function doLogin(e) {
    e?.preventDefault();
    setAuthErr("");
    try {
      const r = await api.login(token.trim());
      adminToken.set(token.trim());
      setAuthed(true);
      setCorrector(r.corrector);
    } catch (err) {
      setAuthErr(String(err.message || err));
      setAuthed(false);
    }
  }

  useEffect(() => {
    if (!adminToken.get()) return;
    api.login(adminToken.get()).then((r) => {
      setAuthed(true);
      setCorrector(r.corrector);
    }).catch(() => setAuthed(false));
  }, []);

  function logout() {
    adminToken.set("");
    setToken("");
    setAuthed(false);
  }

  return (
    <>
      <div className="live-head">
        <h2 style={{ margin: 0 }}>🛡️ پنل ادمین</h2>
        <span className="spacer" />
        {authed && <span className="pill live">● وارد شده</span>}
      </div>

      {!authed ? (
        <div className="card" style={{ maxWidth: 480 }}>
          <h3>ورود با توکن ادمین</h3>
          <p className="desc">
            توکن همان <code className="inline">ADMIN_TOKEN</code> سرور است (پیش‌فرض{" "}
            <code className="inline">admin123</code>). در هدر{" "}
            <code className="inline">X-Admin-Token</code> ارسال می‌شود.
          </p>
          <form onSubmit={doLogin}>
            <label className="lbl">توکن</label>
            <input className="input" type="password" value={token} onChange={(e) => setToken(e.target.value)} placeholder="admin token…" dir="ltr" />
            {authErr && <p style={{ color: "#fda4af" }}>{authErr}</p>}
            <div className="row mt">
              <button className="btn primary" type="submit">ورود</button>
            </div>
          </form>
        </div>
      ) : (
        <>
          <div className="row">
            <div className="tabs" style={{ margin: 0 }}>
              {[["rooms", "🏠 اتاق‌ها"], ["perms", "🔑 مجوز ضبط"], ["glossary", "📚 واژگان"], ["archive", "🗄️ آرشیو"], ["live", "🎛️ کنترل زنده"]].map(([k, label]) => (
                <button key={k} className={tab === k ? "on" : ""} onClick={() => setParams({ tab: k })}>
                  {label}
                </button>
              ))}
            </div>
            <span className="spacer" />
            <button className="btn ghost" onClick={logout}>خروج</button>
          </div>
          {tab === "rooms" && <RoomsTab />}
          {tab === "perms" && <PermsTab />}
          {tab === "glossary" && <GlossaryTab />}
          {tab === "archive" && <ArchiveTab />}
          {tab === "live" && <LiveTab corrector={corrector} />}
        </>
      )}
    </>
  );
}

/* ---------------- rooms ---------------- */
function RoomsTab() {
  const [rooms, setRooms] = useState([]);
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [gated, setGated] = useState(false);
  const [msg, setMsg] = useState("");

  async function refresh() {
    try {
      const r = await api.listRooms();
      setRooms(r.rooms || []);
    } catch (e) {
      setMsg(String(e.message || e));
    }
  }
  useEffect(() => { refresh(); }, []);

  async function create(e) {
    e.preventDefault();
    setMsg("");
    try {
      await api.createRoom({ name: name || "کلاس جدید", description: desc, require_permission: gated });
      setName(""); setDesc(""); setGated(false);
      setMsg("✅ اتاق ساخته شد.");
      refresh();
    } catch (err) { setMsg("⚠️ " + err.message); }
  }

  async function toggleGate(r) {
    await api.patchRoom(r.id, { require_permission: !r.require_permission });
    refresh();
  }
  async function remove(id) {
    if (!confirm(`اتاق ${id} حذف شود؟`)) return;
    try { await api.deleteRoom(id); refresh(); }
    catch (e) { setMsg("⚠️ " + e.message); }
  }
  async function clear(id) {
    await api.clearRoom(id); refresh();
  }
  async function kick(id) {
    const r = await api.kickRoom(id);
    setMsg(r.kicked ? "✅ مدرس قطع شد." : "ℹ️ مدرسی متصل نیست.");
    refresh();
  }

  return (
    <>
      <div className="card mt">
        <h3>＋ ساخت اتاق جدید</h3>
        <form onSubmit={create}>
          <div className="grid cols-2">
            <div>
              <label className="lbl">نام اتاق</label>
              <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="مثلاً ریاضی ۱۰۱" />
            </div>
            <div>
              <label className="lbl">توضیح</label>
              <input className="input" value={desc} onChange={(e) => setDesc(e.target.value)} placeholder="توضیح کوتاه…" />
            </div>
          </div>
          <label className="checkbox-row">
            <input type="checkbox" checked={gated} onChange={(e) => setGated(e.target.checked)} />
            🔒 از ابتدا نیازمند مجوز ضبط باشد (گیت‌شده)
          </label>
          <div className="row mt">
            <button className="btn primary" type="submit">ساختن اتاق</button>
            {msg && <span className="small muted">{msg}</span>}
          </div>
        </form>
      </div>

      <div className="grid cols-2 mt">
        {rooms.map((r) => (
          <div className="card" key={r.id}>
            <div className="row">
              <h3 style={{ margin: 0 }}>{r.name}</h3>
              <span className="spacer" />
              {r.lecturer_connected ? <span className="pill live"><span className="rec-dot on" /> زنده</span> : <span className="pill idle">آفلاین</span>}
            </div>
            <p className="small muted">🆔 <code className="inline">{r.id}</code> · 👁️ {r.viewers} · 🔑 {r.grants_n} مجوز · 📝 {r.history_n} پیام</p>
            <p className="desc">{r.description || "—"}</p>
            <div className="row">
              <button className="btn" onClick={() => toggleGate(r)} title="تغییر وضعیت گیت">
                {r.require_permission ? "🔓 آزادسازی ضبط" : "🔒 گیت کردن ضبط"}
              </button>
              <button className="btn" onClick={() => kick(r.id)} disabled={!r.lecturer_connected}>⏏ قطع مدرس</button>
              <button className="btn" onClick={() => clear(r.id)}>🧹 پاک‌سازی متن</button>
              <button className="btn danger" onClick={() => remove(r.id)} disabled={r.id === "live"}>حذف</button>
            </div>
          </div>
        ))}
      </div>
      {rooms.length === 0 && <div className="empty mt">اتاقی نیست.</div>}
    </>
  );
}

/* ---------------- permissions ---------------- */
function PermsTab() {
  const [rooms, setRooms] = useState([]);
  const [room, setRoom] = useState("live");
  const [grants, setGrants] = useState([]);
  const [label, setLabel] = useState("");
  const [msg, setMsg] = useState("");

  async function refreshRooms() {
    const r = await api.listRooms();
    setRooms(r.rooms || []);
    if (!(r.rooms || []).some((x) => x.id === room) && (r.rooms || []).length) setRoom(r.rooms[0].id);
  }
  async function refreshGrants(rid = room) {
    try {
      const g = await api.listGrants(rid);
      setGrants(g.grants || []);
    } catch (e) { setMsg(String(e.message || e)); }
  }
  useEffect(() => { refreshRooms().then(() => refreshGrants()); /* eslint-disable-next-line */ }, []);
  useEffect(() => { refreshGrants(); /* eslint-disable-next-line */ }, [room]);

  async function create(e) {
    e.preventDefault();
    setMsg("");
    try {
      const r = await api.createGrant(room, label || "مدرس");
      setLabel("");
      setMsg(`✅ کد ساخته شد: ${r.code} — آن را به مدرس بدهید.`);
      refreshGrants(); refreshRooms();
    } catch (err) { setMsg("⚠️ " + err.message); }
  }
  async function revoke(code) {
    await api.revokeGrant(room, code);
    refreshGrants(); refreshRooms();
  }
  async function toggleGate() {
    await api.patchRoom(room, { require_permission: !(roomInfo?.require_permission) });
    refreshRooms();
  }
  function copy(code) {
    navigator.clipboard?.writeText(code);
    setMsg(`📋 کپی شد: ${code}`);
  }

  const roomInfo = rooms.find((r) => r.id === room);

  return (
    <>
      <div className="card mt">
        <h3>🔑 اعطای مجوز ضبط</h3>
        <p className="desc">
          وقتی اتاق <b>گیت‌شده</b> باشد، فقط کسی که <b>کد مجوز</b> دارد می‌تواند در صفحه ضبط،
          استریم را شروع کند (به‌صورت <code className="inline">?grant=CODE</code> به سوکت مدرس
          می‌رود و سرور اعتبارسنجی می‌کند). برای هر مدرس/گوشی یک کد جدا بسازید؛ هر کد قابل
          لغو است و تعداد استفاده آن ثبت می‌شود.
        </p>
        <div className="row">
          <select className="select" style={{ maxWidth: 280 }} value={room} onChange={(e) => setRoom(e.target.value)}>
            {rooms.map((r) => <option key={r.id} value={r.id}>{r.name} ({r.id})</option>)}
          </select>
          {roomInfo && (roomInfo.require_permission ? <span className="pill">🔒 گیت‌شده</span> : <span className="pill live">🔓 آزاد</span>)}
          <button className="btn" onClick={toggleGate}>
            {roomInfo?.require_permission ? "🔓 آزادسازی ضبط (بدون کد)" : "🔒 گیت کردن ضبط"}
          </button>
        </div>
        <form onSubmit={create} className="row mt">
          <input className="input" style={{ maxWidth: 260 }} value={label} onChange={(e) => setLabel(e.target.value)} placeholder="نام مدرس / برچسب…" />
          <button className="btn primary" type="submit">＋ ساخت کد مجوز</button>
        </form>
        {msg && <p className="small">{msg}</p>}
      </div>

      <div className="grid cols-2 mt">
        {grants.map((g) => (
          <div className="card" key={g.code}>
            <div className="row">
              <code className="inline" dir="ltr" style={{ fontSize: 16, letterSpacing: 2 }}>{g.code}</code>
              <span className="spacer" />
              <button className="btn" onClick={() => copy(g.code)}>📋 کپی</button>
              <button className="btn danger" onClick={() => revoke(g.code)}>لغو</button>
            </div>
            <p className="small muted">🏷️ {g.label} · 👁️ {g.uses || 0} بار استفاده · 📅 {new Date(g.created_at * 1000).toLocaleString("fa-IR")}</p>
          </div>
        ))}
      </div>
      {grants.length === 0 && <div className="empty mt">هنوز کدی برای این اتاق ساخته نشده — یکی بسازید تا ضبط گیت شود.</div>}
    </>
  );
}

/* ---------------- per-class glossary ---------------- */
function GlossaryTab() {
  const [rooms, setRooms] = useState([]);
  const [room, setRoom] = useState("live");
  const [terms, setTerms] = useState([]);
  const [one, setOne] = useState("");
  const [bulk, setBulk] = useState("");
  const [msg, setMsg] = useState("");
  const [q, setQ] = useState("");

  async function refreshRooms() {
    const r = await api.listRooms();
    setRooms(r.rooms || []);
    if (!(r.rooms || []).some((x) => x.id === room) && (r.rooms || []).length) setRoom(r.rooms[0].id);
  }
  async function refreshTerms(rid = room) {
    try {
      const g = await api.getGlossary(rid);
      setTerms(g.terms || []);
    } catch (e) { setMsg("⚠️ " + e.message); }
  }
  useEffect(() => { refreshRooms().then(() => refreshTerms()); /* eslint-disable-next-line */ }, []);
  useEffect(() => { refreshTerms(); setBulk(""); setQ(""); /* eslint-disable-next-line */ }, [room]);

  async function addOne(e) {
    e?.preventDefault();
    if (!one.trim()) return;
    const r = await api.addGlossary(room, one.trim());
    setOne("");
    setMsg(r.added ? "✅ اضافه شد." : "ℹ️ تکراری بود.");
    refreshTerms();
  }
  async function remove(t) {
    await api.deleteGlossaryTerm(room, t);
    refreshTerms();
  }
  async function replaceAll() {
    const list = bulk.split("\n").map((s) => s.trim()).filter(Boolean);
    if (!list.length) { setMsg("⚠️ لیست خالی است."); return; }
    if (!confirm(`کل واژگان اتاق ${room} با ${list.length} اصطلاح جایگزین شود؟`)) return;
    const r = await api.replaceGlossary(room, list);
    setBulk("");
    setMsg(`✅ ${r.terms_n} اصطلاح ثبت شد و از همین حالا اعمال می‌شود.`);
    refreshTerms(); refreshRooms();
  }

  const visible = terms.filter((t) => !q.trim() || t.includes(q.trim()));
  const roomInfo = rooms.find((r) => r.id === room);

  return (
    <>
      <div className="card mt">
        <h3>📚 واژگان هر کلاس</h3>
        <p className="desc">
          هر اتاق (کلاس) مجموعه اصطلاحات خودش را دارد — مثلاً چشم/گوش برای یک کلاس، قلب برای
          کلاس دیگر. این اصطلاحات موقع اصلاح متن همان اتاق اعمال می‌شوند (نیازی به ری‌استارت
          سرور نیست). واژگان فایل سراسری سرور هم برای همه اتاق‌ها اعمال می‌شود.
        </p>
        <div className="row">
          <select className="select" style={{ maxWidth: 280 }} value={room} onChange={(e) => setRoom(e.target.value)}>
            {rooms.map((r) => <option key={r.id} value={r.id}>{r.name} ({r.id})</option>)}
          </select>
          {roomInfo && <span className="pill">📚 {roomInfo.glossary_n ?? terms.length} اصطلاح</span>}
        </div>
        <form onSubmit={addOne} className="row mt">
          <input className="input" style={{ maxWidth: 320 }} value={one} onChange={(e) => setOne(e.target.value)} placeholder="تک‌اصطلاح… مثلاً شبکیه یا Retina" />
          <button className="btn primary" type="submit">＋ افزودن</button>
        </form>
        {msg && <p className="small">{msg}</p>}
      </div>

      <div className="grid cols-2 mt">
        <div className="card">
          <h3>📝 جایگزینی گروهی (از جزوه کلاس)</h3>
          <p className="desc">هر سطر یک اصطلاح. با ثبت، کل مجموعه این اتاق جایگزین می‌شود.</p>
          <textarea className="input" rows={10} dir="auto" value={bulk} onChange={(e) => setBulk(e.target.value)} placeholder={"شبکیه\nقرنیه\nRetina\nOptic"} />
          <div className="row mt">
            <button className="btn primary" onClick={replaceAll}>ثبت گروهی</button>
          </div>
        </div>
        <div className="card">
          <div className="row">
            <h3 style={{ margin: 0 }}>اصطلاحات این اتاق ({terms.length})</h3>
            <span className="spacer" />
            <input className="input" style={{ maxWidth: 160 }} value={q} onChange={(e) => setQ(e.target.value)} placeholder="🔍 جست‌وجو…" />
          </div>
          <div style={{ maxHeight: 320, overflowY: "auto", marginTop: 8 }}>
            {visible.map((t) => (
              <div className="row" key={t} style={{ padding: "4px 0", borderBottom: "1px solid rgba(255,255,255,.06)" }}>
                <span>{t}</span>
                <span className="spacer" />
                <button className="btn danger" onClick={() => remove(t)}>حذف</button>
              </div>
            ))}
            {visible.length === 0 && <div className="empty">اصطلاحی نیست — از کادر بالا اضافه کنید.</div>}
          </div>
        </div>
      </div>
    </>
  );
}

/* ---------------- archive ---------------- */
function ArchiveTab() {
  const [sessions, setSessions] = useState([]);
  const [open, setOpen] = useState(null);
  const [detail, setDetail] = useState(null);

  async function refresh() {
    const r = await api.listArchive();
    setSessions(r.sessions || []);
  }
  useEffect(() => { refresh(); }, []);

  async function show(s) {
    setOpen(s);
    setDetail(null);
    try {
      const d = await api.getArchive(s.id);
      setDetail(d);
    } catch {}
  }

  function download(s) {
    api.getArchive(s.id).then((d) => {
      const blob = new Blob([JSON.stringify(d, null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `${s.id}.json`;
      a.click();
      URL.revokeObjectURL(a.href);
    });
  }

  return (
    <>
      <div className="card mt">
        <div className="row">
          <h3 style={{ margin: 0 }}>🗄️ آرشیو جلسات ضبط‌شده ({sessions.length})</h3>
          <span className="spacer" />
          <button className="btn" onClick={refresh}>↻ تازه‌سازی</button>
        </div>
        <p className="desc">هر جلسه: فایل صوتی WAV (۱۶kHz مونو — ورودی دقیق مدل) + رونوشت کامل خام/اصلاح‌شده.</p>
      </div>
      <div className="grid cols-2 mt">
        {sessions.map((s) => (
          <div className="card" key={s.id}>
            <div className="row">
              <b>{s.room_name}</b>
              <span className="spacer" />
              <span className="pill">⏱️ {Math.round(s.duration_s)}s · 📝 {s.segments}</span>
            </div>
            <p className="small muted">🆔 <code className="inline" dir="ltr">{s.id}</code> · 📅 {new Date(s.started_at * 1000).toLocaleString("fa-IR")} · 💾 {(s.size / 1024).toFixed(0)}KB</p>
            <p className="desc">{s.preview || "—"}</p>
            <audio controls preload="none" src={`${API_BASE}${s.file}`} />
            <div className="row mt">
              <button className="btn" onClick={() => show(s)}>📖 رونوشت کامل</button>
              <button className="btn" onClick={() => download(s)}>⬇ JSON</button>
              <a className="btn ghost" href={`${API_BASE}${s.file}`} download>🎧 دانلود صوت</a>
            </div>
          </div>
        ))}
      </div>
      {sessions.length === 0 && <div className="empty mt">هنوز جلسه‌ای ضبط و آرشیو نشده است.</div>}

      {open && (
        <div className="card mt">
          <div className="row">
            <h3 style={{ margin: 0 }}>📖 {open.id}</h3>
            <span className="spacer" />
            <button className="btn ghost" onClick={() => setOpen(null)}>✕ بستن</button>
          </div>
          {!detail ? <div className="shimmer mt" /> : (
            <>
              <p className="small muted">{detail.transcript?.length || 0} قطعه · خام زرد / اصلاح‌شده سبز</p>
              {(detail.transcript || []).map((m, i) => (
                <div key={i} className={`msg ${m.stage === "corrected" ? "fixed" : "raw"}`}>
                  <div className="meta"><span>[{m.audio_pos}]</span><span className="tag">{m.type}</span></div>
                  <div>{m.text}</div>
                </div>
              ))}
            </>
          )}
        </div>
      )}
    </>
  );
}

/* ---------------- live control ---------------- */
function LiveTab({ corrector }) {
  const [health, setHealth] = useState(null);
  const [msg, setMsg] = useState("");

  async function refresh() {
    setHealth(await api.health().catch(() => null));
  }
  useEffect(() => { refresh(); const t = setInterval(refresh, 4000); return () => clearInterval(t); }, []);

  async function act(fn, id) {
    setMsg("");
    try { const r = await fn(id); setMsg(JSON.stringify(r)); refresh(); }
    catch (e) { setMsg("⚠️ " + e.message); }
  }

  const rooms = health?.rooms ? Object.values(health.rooms) : [];

  return (
    <>
      <div className="card mt">
        <h3>🎛️ کنترل زنده + موتور اصلاح</h3>
        <p>بک‌اند اصلاح: <code className="inline">{corrector?.backend || health?.corrector?.backend || "…"}</code>
          {corrector?.model && <> · مدل <code className="inline">{corrector.model}</code></>}
        </p>
        <pre className="dump" dir="ltr">{JSON.stringify(health?.corrector || corrector || {}, null, 2)}</pre>
        <p className="small muted" style={{ lineHeight: 2 }}>
          • <code className="inline">local</code> = نرمالایزر داخلی، بدون کلید/اینترنت، آنی.
          • <code className="inline">openai/ollama</code> = مدل متنی اختیاری روی همان بافر؛ خرابی ←
          بازگشت خودکار به local. • <code className="inline">off</code> = فقط خام.
          تنظیم در <code className="inline">.env</code> سرور (CORRECTOR_BACKEND).
        </p>
        {msg && <p className="small">{msg}</p>}
      </div>
      <div className="grid cols-2 mt">
        {rooms.map((r) => (
          <div className="card" key={r.id}>
            <div className="row">
              <b>{r.name}</b>
              <span className="spacer" />
              {r.lecturer_connected ? <span className="pill live">● زنده</span> : <span className="pill idle">○ آفلاین</span>}
            </div>
            <p className="small muted">👁️ {r.viewers} بیننده · ⏱️ {r.fed_s}s تغذیه‌شده · 📝 {r.history_n} پیام</p>
            <div className="row">
              <button className="btn" onClick={() => act(api.kickRoom, r.id)} disabled={!r.lecturer_connected}>⏏ قطع مدرس</button>
              <button className="btn" onClick={() => act(api.clearRoom, r.id)}>🧹 پاک‌سازی متن</button>
              <button className="btn" onClick={() => act((id) => api.exportRoom(id, "txt"), r.id)}>⬇ خروجی txt</button>
            </div>
          </div>
        ))}
      </div>
    </>
  );
}
