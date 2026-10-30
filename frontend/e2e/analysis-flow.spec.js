// The main flow in a real browser: paste a URL, follow the progress screen, land on the analysis.
// The event streams are the backend's own (backend/tests/fixtures/contracts/analysis-event-stream.json,
// written by tests/test_analysis_event_contract.py), so these tests replay what the server sends.
import { test as base, expect } from "@playwright/test";
import { readFileSync } from "node:fs";

const { stages, streams } = JSON.parse(readFileSync(
  new URL("../../backend/tests/fixtures/contracts/analysis-event-stream.json", import.meta.url), "utf8"));
const ANALYSIS_ID = streams.ready[0].analysis_id;

const sse = (events) => events.map((event) => `id: ${event.sequence}\ndata: ${JSON.stringify(event)}\n\n`).join("");

function overview(status, extra = {}) {
  return {
    analysis: { analysis_id: ANALYSIS_ID, status, failure_stage: null, failure_code: null, awaiting_stage: null,
      last_completed_stage: null, waiting_reason: null, degraded: [], ...extra },
    repository: { canonical_key: "github:pallets/flask", owner: "pallets", name: "flask",
      commit_sha: "d086db856be187255b8ec61ef409357393020f32", requested_reference: "main" },
    counts: { files: 230, entities: 3887, relationships: 5308, chunks: 1200 },
    languages: [{ language: "python", files: 83 }],
    graph: null,
    communities: [],
    answers_available: false,
    answers: { available: false, reason: "Answers need a text model, and none is configured (GEMINI_API_KEY is not set). "
      + "Set GEMINI_API_KEY or ANALYSIS_LLM_PROVIDER=offline, then restart the API." },
  };
}

/**
 * Mocks the API. `streams` is a list of bodies, one per connection to the events endpoint (the
 * last one repeats). Fails the test on any page error, console error, or unmocked API call.
 */
const test = base.extend({
  api: async ({ page }, use) => {
    const problems = [];
    page.on("pageerror", (error) => problems.push(`pageerror: ${error.message}`));
    page.on("console", (message) => message.type() === "error" && problems.push(`console: ${message.text()}`));
    const api = { connections: [], recent: [], overview: overview("READY"), bodies: [sse(streams.ready)] };

    await page.route("**/api/**", (route) => {
      const url = new URL(route.request().url());
      const json = (body, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
      if (url.pathname === "/api/health") return json({ status: "ok", database: "ok", worker: "running" });
      if (url.pathname === "/api/v1/analyses/stages") return json({ stages });
      if (url.pathname === "/api/v1/analyses" && route.request().method() === "GET") return json(api.recent);
      if (url.pathname === "/api/v1/analyses" && route.request().method() === "POST") {
        return json({ analysis_id: ANALYSIS_ID, status: "QUEUED", reused: false,
          events_url: `/api/v1/analyses/${ANALYSIS_ID}/events` }, 202);
      }
      if (url.pathname === `/api/v1/analyses/${ANALYSIS_ID}/events`) {
        api.connections.push(url.pathname);
        const body = api.bodies[Math.min(api.connections.length - 1, api.bodies.length - 1)];
        return route.fulfill({ status: 200, headers: { "content-type": "text/event-stream", "cache-control": "no-cache" }, body });
      }
      if (url.pathname === `/api/v1/analyses/${ANALYSIS_ID}/overview`) return json(api.overview);
      problems.push(`unmocked API call: ${route.request().method()} ${url.pathname}`);
      return route.fulfill({ status: 599, body: "unmocked" });
    });

    await use(api);
    expect(problems, "page and console errors").toEqual([]);
  },
});

async function analyze(page, repository = "https://github.com/pallets/flask") {
  await page.goto("/");
  await page.getByLabel("GitHub repository URL").fill(repository);
  await page.getByRole("button", { name: "Analyze" }).click();
}

const stageStates = (page) => page.locator(".an-stage").evaluateAll(
  (stages) => Object.fromEntries(stages.map((stage) => [stage.textContent, stage.dataset.state || ""])));

test("READY: the pasted URL streams through to the analysis page, and back returns home", async ({ page, api }) => {
  api.bodies = [sse(streams.ready)];
  await analyze(page);
  await expect(page).toHaveURL(`/analyses/${ANALYSIS_ID}`);
  await expect(page.getByRole("heading", { name: "pallets/flask" })).toBeVisible();
  await expect(page.getByRole("status")).toHaveCount(0);
  await page.goBack();
  await expect(page).toHaveURL("/");
  await expect(page.getByLabel("GitHub repository URL")).toBeVisible();
});

test("DEGRADED: opens the analysis page with a banner saying the results are incomplete", async ({ page, api }) => {
  api.bodies = [sse(streams.degraded)];
  api.overview = overview("DEGRADED", { degraded: [{ capability: "summaries", reason: "OFFLINE_PROVIDER",
    detail: "Summaries are templated placeholders from the offline text provider." }] });
  await analyze(page);
  await expect(page).toHaveURL(`/analyses/${ANALYSIS_ID}`);
  await expect(page.getByRole("status")).toContainText("Completed with degraded results");
  await expect(page.getByRole("status")).toContainText("templated placeholders from the offline text provider");
});

test("AWAITING_STAGE: the parked screen says why and how to resume, and links to the analysis", async ({ page, api }) => {
  const parked = streams.parked.at(-1);
  api.bodies = [sse(streams.parked)];
  api.overview = overview("AWAITING_STAGE", { awaiting_stage: parked.awaiting_stage,
    last_completed_stage: parked.last_completed_stage, waiting_reason: parked.waiting_reason });
  await analyze(page);

  await expect(page.getByRole("heading", { name: "Analysis paused" })).toBeVisible();
  await expect(page.locator(".an-notice")).toContainText(parked.waiting_reason);
  expect(await stageStates(page)).toMatchObject({ ingest: "done", extract: "done", index: "done", graph: "done",
    summarize: "active", ready: "" });
  await expect(page.locator(".an-foot")).toContainText("4 of 6 stages");

  await page.getByRole("link", { name: "open analysis" }).click();
  await expect(page).toHaveURL(`/analyses/${ANALYSIS_ID}`);
  await expect(page.getByRole("status")).toContainText("Paused before the summarizing stage");
  await expect(page.getByRole("status")).toContainText("resumes automatically");
});

test("FAILED: shows the failed stage and offers a retry", async ({ page, api }) => {
  api.bodies = [sse(streams.failed)];
  await analyze(page);
  await expect(page.getByRole("heading", { name: "Analysis failed" })).toBeVisible();
  await expect(page.locator(".an-error")).toContainText("Analysis failed during indexing");
  expect(await stageStates(page)).toMatchObject({ extract: "done", index: "active", graph: "" });
  await expect(page.getByRole("button", { name: "retry analysis" })).toBeVisible();
});

test("a refresh or shared link resumes the progress screen", async ({ page, api }) => {
  api.bodies = [sse(streams.ready.slice(0, 4))]; // still indexing
  await page.goto(`/?analysis=${ANALYSIS_ID}`);
  await expect(page.getByRole("heading", { name: "Analyzing" })).toBeVisible();
  await expect(page.locator(".an-repo")).toContainText("pallets/flask");
  expect(await stageStates(page)).toMatchObject({ extract: "done", index: "active", graph: "" });
  await page.reload();
  await expect(page.getByRole("heading", { name: "Analyzing" })).toBeVisible();
  expect(await stageStates(page)).toMatchObject({ index: "active" });
});

test("a dropped stream, or an API restart, resumes from the last event without repeating it", async ({ page, api }) => {
  const first = streams.parked.slice(0, 4); // through INDEXING, then the connection drops
  // Ids are persisted state versions: after a reconnect (or a restart) the server sends only newer ones.
  api.bodies = [`retry: 100\n\n${sse(first)}`, sse(streams.parked.slice(4))];
  await page.goto(`/?analysis=${ANALYSIS_ID}`);
  await expect(page.getByRole("heading", { name: "Analysis paused" })).toBeVisible();
  // Chrome sends Last-Event-ID on the reconnect, but intercepted requests do not show it; the
  // server's handling of it is tested in tests/test_analysis_event_contract.py.
  expect(api.connections.length).toBeGreaterThanOrEqual(2);
  await expect(page.locator(".an-line", { hasText: "Indexing content" })).toHaveCount(1);
  await expect(page.locator(".an-line")).toHaveCount(streams.parked.length);
});

test("a state that repeats under a new version (a retried stage) is shown again", async ({ page, api }) => {
  const [queued, ingesting, extracting, indexing] = streams.parked;
  const retried = { ...indexing, sequence: 9, message: "Indexing content" };
  api.bodies = [`retry: 100\n\n${sse([queued, ingesting, extracting, indexing])}`,
    sse([retried, ...streams.parked.slice(4).map((event, index) => ({ ...event, sequence: 10 + index }))])];
  await page.goto(`/?analysis=${ANALYSIS_ID}`);
  await expect(page.getByRole("heading", { name: "Analysis paused" })).toBeVisible();
  await expect(page.locator(".an-line", { hasText: "Indexing content" })).toHaveCount(2);
});

test("events of unknown shape are skipped instead of crashing the screen", async ({ page, api }) => {
  const [first, ...rest] = streams.parked;
  api.bodies = [
    `data: not json\n\n${sse([first])}data: {"sequence": "2", "state": 7}\n\n${sse(rest.map((event) => ({ ...event, message: { nested: true } })))}`,
  ];
  await analyze(page);
  await expect(page.getByRole("heading", { name: "Analysis paused" })).toBeVisible();
  await expect(page.locator(".an-line").last()).toContainText("AWAITING_STAGE");
});

test("recent analyses are listed on the home page and open their page", async ({ page, api }) => {
  api.recent = [{ analysis_id: ANALYSIS_ID, canonical_repository_key: "github:pallets/flask", owner_name: "pallets",
    repository_name: "flask", commit_sha: "d086db856be187255b8ec61ef409357393020f32", requested_reference: "main",
    status: "AWAITING_STAGE", created_at: "2026-10-29T18:39:56Z", completed_at: null, awaiting_stage: "SUMMARIZING" }];
  await page.goto("/");
  const row = page.locator(".home-recent li");
  await expect(row).toHaveCount(1);
  await expect(row).toContainText("d086db8");
  await expect(row).toContainText("waiting");
  await row.getByRole("link").click();
  await expect(page).toHaveURL(`/analyses/${ANALYSIS_ID}`);
});

test("the ask box says why answers are unavailable, with the fix, and its controls are disabled", async ({ page, api }) => {
  await page.goto(`/analyses/${ANALYSIS_ID}`);
  const note = page.getByRole("note");
  await expect(note).toContainText("Answers are unavailable on this server");
  await expect(note).toContainText("Set GEMINI_API_KEY or ANALYSIS_LLM_PROVIDER=offline");
  await expect(page.getByLabel("Question about the repository")).toBeDisabled();
  await expect(page.getByLabel("Answer mode")).toBeDisabled();
  await expect(page.getByRole("button", { name: "Ask" })).toBeDisabled();

  api.overview = { ...api.overview, answers: { available: true, reason: null } };
  await page.reload();
  await expect(page.getByLabel("Question about the repository")).toBeEnabled();
  await expect(page.getByRole("note")).toHaveCount(0);
});

test("navigation offers no links to the MVP pages R1 removed", async ({ page, api }) => {
  void api;
  await page.goto("/");
  for (const name of ["Repositories", "Search", "Analytics", "Review Queue"]) {
    await expect(page.getByRole("link", { name: `[${name}]` })).toHaveCount(0);
  }
  await expect(page.getByText("try demo")).toHaveCount(0);
});

test("a followed link against a server that does not name the repository shows a plain placeholder", async ({ page, api }) => {
  api.bodies = [sse(streams.parked.map(({ repository, stages, waiting_reason, ...older }) => older))];
  await page.goto(`/?analysis=${ANALYSIS_ID}`);
  await expect(page.getByRole("heading", { name: "Analysis paused" })).toBeVisible();
  await expect(page.locator(".an-repo")).toHaveText("repository");
  await expect(page.locator(".an-stage")).toHaveCount(6);
});
