import assert from "node:assert/strict";
import test from "node:test";

import { buildArtifactLayerGroups, buildLanguageSummary } from "./layerViewModel.js";

const singleArtifact = {
  artifact_count: "single",
  artifacts: [
    {
      name: "library",
      type: "library",
      path: "/",
      primary: true,
      subordinate_to: null,
    },
  ],
};

test("empty analysis does not render an empty layer panel", () => {
  assert.deepEqual(buildArtifactLayerGroups({}, singleArtifact), []);
});

test("all-null layer analysis does not render an empty layer panel", () => {
  const stack = {
    library: [
      {
        name: "utility",
        architectural_layer: null,
        belongs_to_artifact: "library",
      },
    ],
  };
  assert.deepEqual(buildArtifactLayerGroups(stack, singleArtifact), []);
});

test("single artifact groups its deterministic layers without special casing", () => {
  const stack = {
    frameworks: [
      {
        name: "FastAPI",
        belongs_to_artifact: "library",
        usage_scope: "runtime",
        architectural_layer: {
          primary: "backend",
          assignment_method: "deterministic",
          confidence: 0.95,
        },
      },
    ],
  };
  const groups = buildArtifactLayerGroups(stack, singleArtifact);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].artifact.name, "library");
  assert.equal(groups[0].layers[0].layer, "backend");
  assert.equal(groups[0].layers[0].techs[0].name, "FastAPI");
});

test("language summary separates the configured primary language from the others", () => {
  const summary = buildLanguageSummary({
    primary_language: "Rust",
    languages: [
      { name: "Shell", byte_share: 0.1 },
      { name: "Rust", byte_share: 0.8 },
      { name: "Python", byte_share: 0.1 },
    ],
  });

  assert.equal(summary.primary.name, "Rust");
  assert.deepEqual(summary.otherLanguages.map((language) => language.name), ["Shell", "Python"]);
});

test("language summary falls back to the measured primary language", () => {
  const summary = buildLanguageSummary({
    languages: [
      { name: "Go", is_primary: true },
      { name: "Shell", is_primary: false },
    ],
  });

  assert.equal(summary.primary.name, "Go");
  assert.deepEqual(summary.otherLanguages.map((language) => language.name), ["Shell"]);
});
