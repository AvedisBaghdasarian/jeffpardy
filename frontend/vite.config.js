import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.js"],
  },
  server: {
    port: 5173,
    // localtunnel serves the app under a foreign Host header (e.g. *.loca.lt)
    allowedHosts: true,
    proxy: {
      "/api": "http://127.0.0.1:8123",
      "/ws": {
        target: "ws://127.0.0.1:8123",
        ws: true,
      },
    },
  },
  preview: {
    port: 5173,
  },
});
