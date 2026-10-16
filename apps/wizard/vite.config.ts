import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// Each mind-probe frontend is an independent Vite app on its own port.
//
// The Wizard console proxies the Session Server during `vite dev` so the
// console can use relative URLs. Production sends Bearer tokens and the
// WebSocket subprotocol to `VITE_API_BASE_URL` directly. Override the dev
// backend with MINDPROBE_SERVER_ORIGIN when it does not run on localhost:8000.
export default defineConfig(({ mode }) => {
  // Env dir is resolved relative to the Vite root, i.e. this package.
  const env = loadEnv(mode, ".", "");
  const target = env.MINDPROBE_SERVER_ORIGIN || "http://localhost:8000";

  return {
    plugins: [react()],
    server: {
      port: 5174,
      host: true,
      proxy: {
        "/sessions": { target, changeOrigin: false },
        "/cases": { target, changeOrigin: false },
        "/health": { target, changeOrigin: false },
        "/ws": { target, ws: true, changeOrigin: false },
      },
    },
  };
});
