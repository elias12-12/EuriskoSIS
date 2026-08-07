import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    // 0.0.0.0 so the dev server is reachable from outside its container.
    // Binding to localhost inside Docker means the published port answers nothing.
    host: true,
    port: 5173,
    // Docker bind mounts on Windows do not deliver filesystem events, so HMR
    // needs polling to notice edits. Costs a little CPU; the alternative is
    // editing a file and wondering why nothing reloads.
    watch: { usePolling: true },
  },
});
