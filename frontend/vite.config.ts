import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build output lands in frontend/dist; FastAPI serves it in production.
// In dev (`npm run dev`), /api and /hooks proxy to the running FastAPI on :8787
// so you get HMR against the real backend.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://localhost:8787",
      "/hooks": "http://localhost:8787",
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
