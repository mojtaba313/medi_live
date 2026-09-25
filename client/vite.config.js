import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import fs from 'fs';
import path from 'path';

// Plain (non-TLS) backend. The dev server terminates TLS (phone mic needs HTTPS)
// and proxies /api, /ws, /archive to it, so browsers never see ws://-from-https
// mixed content and the backend needs no certificate.
// Override: BACKEND_URL=http://127.0.0.1:8000 npm run dev
const BACKEND = process.env.BACKEND_URL || 'http://localhost:8000';
const WS_TARGET = BACKEND.replace(/^http/, 'ws');

function loadHttps() {
  try {
    return {
      key: fs.readFileSync(path.resolve(__dirname, './192.168.43.108+3-key.pem')),
      cert: fs.readFileSync(path.resolve(__dirname, './192.168.43.108+3.pem')),
    };
  } catch (e) {
    console.warn(
      '[vite] HTTPS cert files not found, serving plain HTTP. ' +
        'Note: phone microphones require HTTPS (secure context).'
    );
    return undefined;
  }
}

export default defineConfig({
  plugins: [react()],
  server: {
    https: loadHttps(),
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/api': { target: BACKEND, changeOrigin: true },
      '/archive': { target: BACKEND, changeOrigin: true },
      '/ws': { target: WS_TARGET, changeOrigin: true, ws: true },
    },
  },
});
