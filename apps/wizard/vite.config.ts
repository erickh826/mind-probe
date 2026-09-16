import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// Each mind-probe frontend is an independent Vite app on its own port.
//
// The Wizard console additionally proxies the Session Server, so the dev origin
// and the API origin are the same. That is not cosmetic: the role-bound
// session token is an HttpOnly cookie (ADR-0001 §8) and a WebSocket upgrade is
// not covered by CORS, so a genuinely cross-site `/ws/...` handshake would
// arrive with no cookie and be closed with 4401. Override the backend with
// MINDPROBE_SERVER_ORIGIN when it does not run on localhost:8000.
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
