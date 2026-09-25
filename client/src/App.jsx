import { Routes, Route, Navigate } from "react-router-dom";
import Layout from "./components/Layout.jsx";
import Home from "./pages/Home.jsx";
import Live from "./pages/Live.jsx";
import Record from "./pages/Record.jsx";
import Admin from "./pages/Admin.jsx";

export default function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/live" element={<Live />} />
        <Route path="/record" element={<Record />} />
        <Route path="/admin" element={<Admin />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
      <footer className="muted small mt" style={{ textAlign: "center", opacity: 0.7 }}>
        مدی · Medi Live — خام فوری ⚡ + اصلاح هوشمند ✨ · بدون کلید هم کار می‌کند 🗝️
      </footer>
    </Layout>
  );
}
