import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import { buildIdFrom, versionJsonPlugin } from "../../packages/shared/src/versionCheck";

// Each mind-probe frontend is an independent Vite app on its own port.
// Production builds must name the API host at build time. The check lives
// here so `vite build` fails before a bundle that would call the wrong host
// can be published.
export default defineConfig(({ command, mode }) => {
  const env = loadEnv(mode, ".", "");
  // One id for both the bundle and dist/version.json; open tabs compare the two.
  const buildId = buildIdFrom(env);
  if (command === "build" && mode === "production" && !env.VITE_API_BASE_URL?.trim()) {
    throw new Error("VITE_API_BASE_URL is required for production builds of the op app.");
  }

  return {
    plugins: [react(), versionJsonPlugin(buildId)],
    define: { __APP_BUILD_ID__: JSON.stringify(buildId) },
    server: { port: 5176, host: true },
  };
});
