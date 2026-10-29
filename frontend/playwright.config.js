import { defineConfig, devices } from "@playwright/test";

// The analysis flow, driven in a real browser against a production build (vite preview). The API is mocked per
// test from the backend's recorded event streams (see e2e/analysis-flow.spec.js), so no backend,
// database or network is needed. PW_CHANNEL=chrome uses an installed Chrome instead of the
// downloaded Chromium.
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: "http://localhost:4173",
    trace: "retain-on-failure",
    ...devices["Desktop Chrome"],
    ...(process.env.PW_CHANNEL ? { channel: process.env.PW_CHANNEL } : {}),
  },
  webServer: {
    command: "npx vite build && npx vite preview --port 4173 --strictPort",
    url: "http://localhost:4173",
    reuseExistingServer: !process.env.CI,
    // An empty base URL builds same-origin API calls, which the tests intercept.
    env: { VITE_API_BASE_URL: "" },
  },
});
