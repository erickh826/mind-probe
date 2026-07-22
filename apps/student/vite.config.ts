import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Each mind-probe frontend is an independent Vite app on its own port.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, host: true },
});
