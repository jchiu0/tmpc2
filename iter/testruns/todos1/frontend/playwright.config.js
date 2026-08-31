import { defineConfig } from "@playwright/test";

process.env.NO_PROXY = "127.0.0.1,localhost";
process.env.no_proxy = "127.0.0.1,localhost";

const python = process.env.PYTHON || "python";

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  use: {
    baseURL: "http://127.0.0.1:5174",
    trace: "retain-on-failure",
  },
  webServer: [
    {
      command: `"${python}" -m uvicorn main:app --host 127.0.0.1 --port 8011`,
      cwd: "../backend",
      env: { ...process.env },
      url: "http://127.0.0.1:8011/api/health",
      reuseExistingServer: false,
    },
    {
      command: "npm run dev -- --host 127.0.0.1 --port 5174",
      cwd: ".",
      env: { ...process.env, API_PORT: "8011" },
      url: "http://127.0.0.1:5174",
      reuseExistingServer: false,
    },
  ],
});
