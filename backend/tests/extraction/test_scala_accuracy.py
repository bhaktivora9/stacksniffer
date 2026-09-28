"""SS-BE-203: Scala extraction accuracy against the gold manifests."""

import pytest

import accuracy_checks as checks
from extraction_benchmark_support import requires_grammars

pytestmark = requires_grammars
LANGUAGE = "scala"


@pytest.mark.parametrize("gate", checks.GATE_NAMES)
def test_quality_gate(gate):
    checks.check_quality_gate(LANGUAGE, gate)


def test_resolution_is_reported_by_locality():
    checks.check_resolution_reported_by_locality(LANGUAGE)


def test_unresolved_placeholders_are_not_counted_as_internal_links():
    checks.check_unresolved_placeholders_are_not_internal_links(LANGUAGE)


def test_every_error_is_categorized():
    checks.check_every_error_is_categorized(LANGUAGE)


def test_fixtures_cover_the_required_cases():
    checks.check_fixture_coverage(LANGUAGE)


def test_capabilities_are_reported_accurately():
    checks.check_capability_reporting(LANGUAGE)


def test_malformed_files_stay_isolated():
    checks.check_malformed_files_isolated(LANGUAGE)


def test_published_baseline_matches_a_fresh_run():
    checks.check_published_baseline_is_current(LANGUAGE)


def test_resolution_basis_and_certainty_are_evaluated():
    checks.check_basis_and_certainty_are_evaluated(LANGUAGE)


def test_generated_and_vendored_results_are_reported_separately():
    checks.check_origin_is_reported_separately(LANGUAGE)


def test_results_are_reported_by_capability_level():
    checks.check_capability_levels_are_reported(LANGUAGE)


def test_fixtures_label_typed_receivers_and_origins():
    checks.check_typed_receivers_and_origins_are_labelled(LANGUAGE)
