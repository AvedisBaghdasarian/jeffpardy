import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  use: {
    baseURL: "http://127.0.0.1:5173",
  },
  webServer: [
    {
      command: "PYTHONPATH=../backend/src ../.venv/bin/python -m uvicorn jeffpardy.main:app --host 127.0.0.1 --port 8123",
      port: 8123,
      reuseExistingServer: true,
      timeout: 60_000,
    },
    {
      command: "npm run dev -- --host 127.0.0.1 --port 5173",
      port: 5173,
      reuseExistingServer: true,
      timeout: 60_000,
    },
  ],
});
