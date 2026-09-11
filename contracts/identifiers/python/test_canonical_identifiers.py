import json
from pathlib import Path

import pytest

from canonical_identifiers import IdentifierBundle


VECTOR_FILE = Path(__file__).parents[2] / "golden-vectors" / "identifiers.json"


def test_valid_vectors_round_trip():
    vectors = json.loads(VECTOR_FILE.read_text(encoding="utf-8"))
    for vector in vectors["valid"]:
        bundle = IdentifierBundle.from_dict(vector)
        assert bundle.to_dict() == vector
        assert IdentifierBundle.from_json(bundle.to_json()).to_dict() == vector


def test_invalid_vectors_rejected():
    vectors = json.loads(VECTOR_FILE.read_text(encoding="utf-8"))
    valid = vectors["valid"][0]
    for invalid in vectors["invalid"]:
        payload = dict(valid)
        payload[invalid["field"]] = invalid["value"]
        with pytest.raises(ValueError):
            IdentifierBundle.from_dict(payload)


def test_generated_bundle_has_canonical_values():
    bundle = IdentifierBundle.new("github/acme/widgets", "v1.0.0")
    assert IdentifierBundle.from_json(bundle.to_json()) == bundle
