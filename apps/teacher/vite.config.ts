import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// Each mind-probe frontend is an independent Vite app on its own port.
// Production builds must name the API host at build time. The check lives
// here so `vite build` fails before a bundle that would call the wrong host
// can be published.
export default defineConfig(({ command, mode }) => {
  const env = loadEnv(mode, ".", "");
  if (command === "build" && mode === "production" && !env.VITE_API_BASE_URL?.trim()) {
    throw new Error(
      "VITE_API_BASE_URL is required for production builds of the teacher app.",
    );
  }

  return {
    plugins: [react()],
    server: { port: 5175, host: true },
  };
});
