import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// Each mind-probe frontend is an independent Vite app on its own port.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const target = env.MINDPROBE_SERVER_ORIGIN || "http://localhost:8000";

  return {
    plugins: [react()],
    server: {
      port: 5173,
      host: true,
      proxy: {
        "/sessions": { target, changeOrigin: false },
        "/cases": { target, changeOrigin: false },
        "/recording": { target, changeOrigin: false },
        "/health": { target, changeOrigin: false },
        "/ws": { target, ws: true, changeOrigin: false },
      },
    },
  };
});
