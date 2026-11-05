import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import { buildIdFrom, versionJsonPlugin } from "../../packages/shared/src/versionCheck";

// Each mind-probe frontend is an independent Vite app on its own port.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  // One id for both the bundle and dist/version.json; open tabs compare the two.
  const buildId = buildIdFrom(env);
  const target = env.MINDPROBE_SERVER_ORIGIN || "http://localhost:8000";

  return {
    plugins: [react(), versionJsonPlugin(buildId)],
    define: { __APP_BUILD_ID__: JSON.stringify(buildId) },
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
