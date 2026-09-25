import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api.js";

export default function Home() {
  const [rooms, setRooms] = useState([]);
  const [health, setHealth] = useState(null);
  const [archiveN, setArchiveN] = useState(0);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    (async () => {
      try {
        const [r, h, a] = await Promise.all([
          api.listRooms().catch(() => ({ rooms: [] })),
          api.health().catch(() => null),
          api.listArchive().catch(() => ({ sessions: [] })),
        ]);
        setRooms(r.rooms || []);
        setHealth(h);
        setArchiveN((a.sessions || []).length);
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const corrector = health?.corrector;

  return (
    <>
      <section className="hero">
        <div className="orb o1" />
        <div className="orb o2" />
        <h1>
          🎧 مدی لایو: پخش زنده کلاس، <span className="grad">با متن خام فوری + اصلاح هوشمند</span>
        </h1>
        <p>
          مدرس با گوشی ضبط می‌کند، همه بیننده‌ها همان لحظه متن فارسی را می‌بینند:
          ابتدا <b>متن خام</b> مدل گفتار (زرد، بدون تأخیر)، سپس نسخه{" "}
          <b>اصلاح‌شده</b> مدل متنی (سبز) جایگزین آن می‌شود — حتی بدون کلید API.
        </p>
        <div className="row mt">
          <Link className="btn primary" to="/live">
            ▶ تماشای زنده
          </Link>
          <Link className="btn" to="/record">
            🎙️ شروع ضبط
          </Link>
          <Link className="btn ghost" to="/admin">
            🛡️ پنل ادمین
          </Link>
        </div>
      </section>

      <div className="grid cols-3 mt">
        <div className="card">
          <h3>⚡ مرحله ۱ · خام، فوری</h3>
          <p className="desc">
            خروجی مستقیم مدل Shenava روی CPU با WebSocket پخش می‌شود. هیچ انتظاری برای مدل
            متنی وجود ندارد.
          </p>
        </div>
        <div className="card">
          <h3>✨ مرحله ۲ · اصلاح، جایگزین</h3>
          <p className="desc">
            چند جمله خام بافر شده و نرمالایز/بازنویسی می‌شود؛ پیام{" "}
            <code className="inline">correction</code> با همان شناسه‌ها جای متن خام می‌نشیند.
          </p>
        </div>
        <div className="card">
          <h3>🗝️ بدون API Key هم کار می‌کند</h3>
          <p className="desc">
            حالت <code className="inline">local</code> یک نرمالایزر خالص پایتونی است: ی/ک عربی ←
            فارسی، نیم‌فاصله می/نمی/ها، فاصله‌گذاری ، ؟ . — توضیح کامل پایین صفحه.
          </p>
        </div>
      </div>

      <h2 className="mt">🏠 اتاق‌ها {loading ? "…" : `(${rooms.length})`}</h2>
      {loading ? (
        <div className="grid cols-3">
          <div className="shimmer" />
          <div className="shimmer" />
          <div className="shimmer" />
        </div>
      ) : rooms.length === 0 ? (
        <div className="empty">هنوز اتاقی ساخته نشده — از پنل ادمین بسازید.</div>
      ) : (
        <div className="grid cols-3">
          {rooms.map((r, i) => (
            <div className="card" key={r.id} style={{ animationDelay: `${i * 0.06}s` }}>
              <div className="row">
                <h3 style={{ margin: 0 }}>{r.name}</h3>
                <span className="spacer" />
                {r.lecturer_connected ? (
                  <span className="pill live">
                    <span className="rec-dot on" /> زنده
                  </span>
                ) : (
                  <span className="pill idle">○ آفلاین</span>
                )}
              </div>
              <p className="desc">{r.description || "—"}</p>
              <p className="small muted">
                👁️ {r.viewers} بیننده · 🔒 {r.require_permission ? "نیازمند مجوز ضبط" : "ضبط آزاد"} ·
                🆔 <code className="inline">{r.id}</code>
              </p>
              <div className="row">
                <Link className="btn primary" to={`/live?room=${encodeURIComponent(r.id)}`}>
                  تماشا
                </Link>
                <Link className="btn" to={`/record?room=${encodeURIComponent(r.id)}`}>
                  🎙️ ضبط
                </Link>
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="grid cols-2 mt">
        <div className="card">
          <h3>🧠 وضعیت موتور متن</h3>
          {corrector ? (
            <>
              <p>
                بک‌اند: <code className="inline">{corrector.backend}</code>{" "}
                {corrector.model && <code className="inline">{corrector.model}</code>}
              </p>
              <p className="small muted">
                بافر اصلاح: حداقل {corrector.min_chars} · حداکثر {corrector.max_chars} کاراکتر ·
                تایم‌اوت {corrector.timeout_s} ثانیه
              </p>
            </>
          ) : (
            <p className="muted">در حال اتصال به سرور…</p>
          )}
          <p className="small muted">
            💡 اگر کلید API ندارید نگران نباشید: حالت پیش‌فرض <code className="inline">local</code>{" "}
            بدون اینترنت و بدون GPU اصلاح تمیز تحویل می‌دهد.
          </p>
        </div>
        <div className="card">
          <h3>🗄️ آرشیو ضبط‌ها ({archiveN})</h3>
          <p className="desc">
            هر جلسه ضبط هم‌زمان روی سرور به فایل صوتی ۱۶kHz + رونوشت کامل ذخیره می‌شود و از پنل
            ادمین قابل پخش، مرور و دانلود است.
          </p>
          <Link className="btn" to="/admin?tab=archive">
            مشاهده آرشیو
          </Link>
        </div>
      </div>

      <div className="card mt">
        <h3>❓ متن اصلاح‌شده بدون API Key دقیقاً چطور کار می‌کند؟</h3>
        <p className="desc" style={{ fontSize: 14.5 }}>
          سرور دو مرحله‌ای است. <b>مرحله ۱ (خام):</b> خروجی مدل گفتار Shenava بلافاصله با{" "}
          <code className="inline">stage="raw"</code> پخش می‌شود. <b>مرحله ۲ (اصلاح):</b> چند قطعه
          خام (≈ {corrector?.min_chars ?? 60} تا {corrector?.max_chars ?? 400} کاراکتر یا پایان جمله /
          تایم‌اوت) به‌صورت یک تکه به <code className="inline">TextCorrector.correct()</code>{" "}
          داده می‌شود.
          <br />
          • اگر <code className="inline">CORRECTOR_BACKEND=local</code> (پیش‌فرض، بدون کلید) باشد،
          همان‌جا یک <b>نرمالایزر قطعی فارسی</b> اجرا می‌شود: تبدیل ي/ك/ة عربی به ی/ک/ه، حذف تطویل،
          چسباندن نیم‌فاصله در می‌/نمی‌/ها/تر/ترین/ام/ای/ایم/اید/اند، مرتب‌سازی فاصله‌های ، ؛ ؟ !
          و فشرده‌سازی فاصله‌ها — حدود صفر میلی‌ثانیه روی CPU ضعیف.
          <br />
          • اگر کلید OpenAI-compatible یا Ollama بدهید، همان متن نرمالایز شده + کمی زمینه قبلی به
          مدل متنی داده می‌شود و خروجی دوباره نرمالایز و (با نگهبان طول) جایگزین می‌شود؛ اگر مدل
          خطا بدهد، خودکار به همان نرمالایزر محلی برمی‌گردد.
          <br />• کلاینت پیام <code className="inline">correction(ids=[…])</code> را می‌گیرد و
          جعبه‌های زرد همان شناسه‌ها را حذف و یک جعبه سبز جایگزین می‌کند — یعنی «جایگزینی»، نه
          افزودن.
        </p>
      </div>
    </>
  );
}
