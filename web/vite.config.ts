import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Builds into server/static/dist, which FastAPI serves (Node is build-time only).
// `npm run dev` proxies /api to the FastAPI port. changeOrigin stays false so the
// Host header matches the page's Origin, which the app's CSRF guard requires.
declare const process: { env: Record<string, string | undefined> }; // Node global (no @types/node needed)
const apiPort = process.env.JOBCOPILOT_API_PORT || "8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    outDir: "../server/static/dist",
    emptyOutDir: true,
    // The CSP allows fonts and scripts from 'self' only: never inline assets as data: URIs.
    assetsInlineLimit: 0,
    sourcemap: false,
    modulePreload: { polyfill: false },
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": { target: `http://127.0.0.1:${apiPort}`, changeOrigin: false },
    },
  },
});
