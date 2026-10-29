import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { DEFAULT_STAGES, normalizeEvent, railStages, repositoryLabel, stageIndex, validStages } from "./analysisEvents.js";

// The backend writes this file from its own event builder and stage list (tests/test_analysis_event_contract.py).
const { stages, streams } = JSON.parse(readFileSync(
  new URL("../../../backend/tests/fixtures/contracts/analysis-event-stream.json", import.meta.url), "utf8"));

const replay = (name) => streams[name].map(normalizeEvent);
const rail = railStages(stages);
const position = (event) => rail[stageIndex(event, rail)]?.state ?? null;

test("every recorded backend event normalizes", () => {
  for (const [name, events] of Object.entries(streams)) {
    for (const event of replay(name)) {
      assert.ok(event, `${name}: an event was dropped`);
      assert.equal(typeof event.message, "string");
    }
  }
});

test("the rail comes from the backend's stage list and ends at ready", () => {
  assert.deepEqual(validStages(stages), stages);
  assert.deepEqual(rail.map((s) => s.state), [...stages, "READY"]);
  assert.deepEqual(DEFAULT_STAGES, stages, "the fallback list matches what the backend serves");
  assert.deepEqual(railStages(["INGESTING", "NEW_STAGE"]).map((s) => s.label), ["ingest", "new_stage", "ready"]);
  for (const raw of [null, [], ["INGESTING", 3], "INGESTING"]) assert.equal(validStages(raw), null);
});

test("each stream moves forward along the rail and ends where its terminal state says", () => {
  for (const name of Object.keys(streams)) {
    const positions = replay(name).map((event) => stageIndex(event, rail));
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
    const odd = normalizeEvent({ sequence: 2, state: "REVIEWING", message: { text: "x" } });
    assert.equal(odd.message, "REVIEWING");
    assert.equal(stageIndex(odd, rail), -1);
  } finally {
    console.warn = warn;
  }
});
