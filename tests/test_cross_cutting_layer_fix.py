from services.cross_cutting_layer_fix import (
    guard_cross_cutting_layer,
    is_cross_cutting_utility,
    normalize_identity,
)


def test_coordinate_and_display_names_share_utility_identity():
    assert normalize_identity("com.ibm.icu:icu4j") == "icu4j"
    assert is_cross_cutting_utility("Apache Commons Math")
    assert is_cross_cutting_utility("HPPC")


def test_guard_overrides_concrete_layer_but_preserves_null():
    overrides = []
    assert guard_cross_cutting_layer(
        "HPPC", "backend", on_override=overrides.append,
    ) is None
    assert overrides[0]["reason"] == "cross_cutting_utility"
    assert guard_cross_cutting_layer("ICU4J", None) is None


def test_guard_uses_manifest_package_alias_and_does_not_touch_real_backend():
    assert guard_cross_cutting_layer(
        "High Performance Primitive Collections",
        "backend",
        packages=["com.carrotsearch:hppc"],
    ) is None
    assert guard_cross_cutting_layer("FastAPI", "backend") == "backend"
