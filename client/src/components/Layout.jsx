import { NavLink } from "react-router-dom";
import { useEffect, useState } from "react";
import { api } from "../lib/api.js";

export default function Layout({ children }) {
  const [health, setHealth] = useState(null);

  useEffect(() => {
    let alive = true;
    async function poll() {
      try {
        const h = await api.health();
        if (alive) setHealth(h);
      } catch {
        if (alive) setHealth(null);
      }
    }
    poll();
    const t = setInterval(poll, 8000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  const rooms = health?.rooms ? Object.keys(health.rooms).length : "…";
  const online = health != null;

  return (
    <>
      <nav className="nav">
        <div className="brand">
          <div className="logo-orb">
            <span>🎙️</span>
          </div>
          <span>
            مدی
            <span className="muted small" style={{ fontWeight: 400 }}>
              {" "}
              · Medi Live
            </span>
          </span>
        </div>
        <div className="nav-links">
          <NavLink to="/" end className={({ isActive }) => (isActive ? "active" : "")}>
            خانه
          </NavLink>
          <NavLink to="/live" className={({ isActive }) => (isActive ? "active" : "")}>
            پخش زنده
          </NavLink>
          <NavLink to="/record" className={({ isActive }) => (isActive ? "active" : "")}>
            ضبط
          </NavLink>
          <NavLink to="/admin" className={({ isActive }) => (isActive ? "active" : "")}>
            🛡️ ادمین
          </NavLink>
        </div>
        <div className="health-pill" title="وضعیت سرور">
          <span className={`dot ${online ? "ok" : "bad"}`} />
          {online ? `متصل · ${rooms} اتاق` : "قطع"}
        </div>
      </nav>
      <div className="shell">{children}</div>
    </>
  );
}
