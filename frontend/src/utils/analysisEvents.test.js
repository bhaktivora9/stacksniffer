import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { normalizeEvent, railStages, repositoryLabel, stageIndex } from "./analysisEvents.js";

// The backend writes this file from its own event builder (tests/test_analysis_event_contract.py).
const { streams } = JSON.parse(readFileSync(
  new URL("../../../backend/tests/fixtures/contracts/analysis-event-stream.json", import.meta.url), "utf8"));

const replay = (name) => streams[name].map(normalizeEvent);
const position = (event) => railStages(event)[stageIndex(event)]?.state ?? null;

test("every recorded backend event normalizes", () => {
  for (const [name, events] of Object.entries(streams)) {
    for (const event of replay(name)) {
      assert.ok(event, `${name}: an event was dropped`);
      assert.equal(typeof event.message, "string");
      assert.ok(event.stages, `${name}: the backend sends its stage list`);
    }
  }
});

test("the rail comes from the backend's stage list and ends at ready", () => {
  const [first] = replay("ready");
  assert.deepEqual(railStages(first).map((s) => s.state), [...first.stages, "READY"]);
  assert.deepEqual(railStages({ stages: ["INGESTING", "NEW_STAGE"] }).map((s) => s.label), ["ingest", "new_stage", "ready"]);
});

test("each stream moves forward along the rail and ends where its terminal state says", () => {
  for (const name of Object.keys(streams)) {
    const positions = replay(name).map((event) => stageIndex(event));
    assert.deepEqual(positions, [...positions].sort((a, b) => a - b), `${name} moved backwards: ${positions}`);
  }
  assert.equal(position(replay("ready").at(-1)), "READY");
  assert.equal(position(replay("degraded").at(-1)), "READY");
  assert.equal(position(replay("parked").at(-1)), "SUMMARIZING");
  assert.equal(position(replay("failed").at(-1)), "INDEXING");
});

test("a parked event carries its reason and repository", () => {
  const parked = replay("parked").at(-1);
  assert.match(parked.waitingReason, /resumes automatically/);
  assert.equal(repositoryLabel(parked.repository), "github.com/pallets/flask");
});

test("events of unknown shape are dropped, not thrown", () => {
  const warn = console.warn;
  console.warn = () => {};
  try {
    for (const raw of [null, "text", {}, { sequence: 1 }, { sequence: 0, state: "READY" }, { sequence: "1", state: "READY" },
      { sequence: 1, state: 5 }]) {
      assert.equal(normalizeEvent(raw), null, JSON.stringify(raw));
    }
    const odd = normalizeEvent({ sequence: 2, state: "REVIEWING", message: { text: "x" }, stages: "INGESTING" });
    assert.equal(odd.message, "REVIEWING");
    assert.equal(odd.stages, null);
    assert.equal(stageIndex(odd), -1);
  } finally {
    console.warn = warn;
  }
});
