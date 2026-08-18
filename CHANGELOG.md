# Changelog

All notable changes to StackSniffer are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com);
versioning follows [Semantic Versioning](https://semver.org).

## [0.1.0] — 2026-08-17

First public release (MVP). Pre-1.0: the taxonomy is still evolving and
the API is not yet promised stable.

### Added
- Deterministic technology detection across npm, PyPI, Maven, Cargo, and
  Go manifests, with alias canonicalization and confidence tiers.
- MAP-vs-AI provenance tagging: every signal marked by whether it was
  manifest-verified or model-inferred; verified facts outrank inference.
- 13-type canonical `software_type` taxonomy as a single source of truth,
  derived into the classifier prompt, API, and UI.
- `specific_identity` capture — records a narrower identity (e.g.
  `model_serving`, `ci_cd_engine`) when a repo fits a canonical type only
  loosely, without changing the shipped label.
- Novelty routing — repos fitting no canonical type (language runtimes,
  operating systems) are flagged `is_new` and queued for maintainer
  review instead of being coerced.
- Append-only correction ledger with full provenance; corrections apply
  as overlays, never overwriting the original classification.
- Advisory Layer 0 classifier (shadow mode) — trained from user feedback
  snapshots, runs on every analysis, agreement with the primary path
  tracked; does not yet override.
- Maintainer review queue distinguishing inactive-canonical roles
  (activate) from genuinely emergent ones (promote).
- Architectural-layer assignment by execution role; corpus vector search
  for similar-stack discovery.

### Known limitations
- Layer assignment is conservative: ambiguous-ownership technologies in
  multi-artifact repos are left unassigned rather than guessed.
- Dependency classification can time out on very large manifests, yielding
  a lower-confidence verdict (flagged, not hidden).
- The advisory classifier is not yet promoted to deciding classifications.

[0.1.0]: https://github.com/bhaktivora9/stacksniffer/releases/tag/v0.1.0