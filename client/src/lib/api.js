// Tiny API + WS helper. No dependencies.
//
// Backend resolution (mixed-content safe):
//  - Explicit override via VITE_API_BASE / VITE_WS_BASE always wins.
//  - HTTPS page (phones: mic needs a secure context): same-origin relative URLs.
//    The browser then speaks https/wss to the vite server, which terminates TLS and
//    proxies /api, /ws, /archive to the plain backend. No ws://-from-https, no backend TLS needed.
//  - Plain HTTP page: direct http://host:8000 + ws://host:8000 (allowed on http pages).
const SECURE = window.location.protocol === "https:";
export const API_BASE =
  import.meta.env.VITE_API_BASE ?? (SECURE ? "" : `http://${window.location.hostname}:8000`);
export const WS_BASE =
  import.meta.env.VITE_WS_BASE ??
  (SECURE ? `wss://${window.location.host}` : `ws://${window.location.hostname}:8000`);

const TOKEN_KEY = "medi_admin_token";
const GRANT_PREFIX = "medi_grant_";
// One-time migration from the old "shenava_" keys so saved logins/grants survive the rename.
function migrated(key, legacyKey) {
  try {
    const v = localStorage.getItem(key);
    if (v) return v;
    const old = legacyKey && localStorage.getItem(legacyKey);
    if (old) {
      localStorage.setItem(key, old);
      localStorage.removeItem(legacyKey);
      return old;
    }
  } catch {}
  return "";
}

export const adminToken = {
  get: () => migrated(TOKEN_KEY, "shenava_admin_token"),
  set: (t) => {
    try {
      t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY);
    } catch {}
  },
};

export const grantStore = {
  get: (room) => migrated(GRANT_PREFIX + room, "shenava_grant_" + room),
  set: (room, code) => {
    try {
      code
        ? localStorage.setItem(GRANT_PREFIX + room, code)
        : localStorage.removeItem(GRANT_PREFIX + room);
    } catch {}
  },
};

function headers(extra = {}) {
  const h = { "Content-Type": "application/json", ...extra };
  const t = adminToken.get();
  if (t) h["X-Admin-Token"] = t;
  return h;
}

async function req(path, opts = {}) {
  const r = await fetch(API_BASE + path, {
    ...opts,
    headers: headers(opts.headers),
  });
  const text = await r.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { raw: text };
  }
  if (!r.ok) throw new Error(data?.detail || data?.raw || `HTTP ${r.status}`);
  return data;
}

export const api = {
  health: () => req("/health"),
  listRooms: () => req("/api/rooms"),
  createRoom: (body) => req("/api/rooms", { method: "POST", body: JSON.stringify(body) }),
  patchRoom: (id, body) => req(`/api/rooms/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteRoom: (id) => req(`/api/rooms/${id}`, { method: "DELETE" }),
  listGrants: (id) => req(`/api/rooms/${id}/grants`),
  createGrant: (id, label) =>
    req(`/api/rooms/${id}/grants`, { method: "POST", body: JSON.stringify({ label }) }),
  revokeGrant: (room, code) => req(`/api/rooms/${room}/grants/${code}`, { method: "DELETE" }),
  clearRoom: (id) => req(`/api/rooms/${id}/clear`, { method: "POST" }),
  kickRoom: (id) => req(`/api/rooms/${id}/kick`, { method: "POST" }),
  exportRoom: (id, format = "json") => req(`/api/rooms/${id}/export?format=${format}`),
  listArchive: () => req("/api/archive"),
  getArchive: (sid) => req(`/api/archive/${sid}`),
  login: (token) =>
    req("/api/admin/login", { method: "POST", body: JSON.stringify({ token }) }),
};

export const wsUrl = (path, params = {}) => {
  const q = new URLSearchParams(params).toString();
  return `${WS_BASE}${path}${q ? "?" + q : ""}`;
};

export const fmtClock = (s) => {
  s = Math.max(0, Math.floor(s || 0));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
};
