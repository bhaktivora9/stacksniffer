from backend.models.taxonomy import (
    SOFTWARE_TYPE_DEFINITIONS,
    SoftwareType,
    canonicalize_software_type,
    normalize_specific_identity,
)
from backend.services.ai_pipeline import _CLASSIFICATION_RULES
from backend.services.storage_service import BUILTIN_SOFTWARE_TYPES
from backend.models.schemas import StackAnalysis


def test_software_type_consumers_share_one_canonical_vocabulary():
    canonical = [definition.software_type.value for definition in SOFTWARE_TYPE_DEFINITIONS]

    assert canonical == [software_type.value for software_type in SoftwareType]
    assert canonical == [record["_id"] for record in BUILTIN_SOFTWARE_TYPES]
    assert all(f'"{software_type}"' in _CLASSIFICATION_RULES for software_type in canonical)
    assert "ml_platform" not in _CLASSIFICATION_RULES


def test_historical_software_type_aliases_normalize_without_becoming_canonical():
    assert canonicalize_software_type("web_app") is SoftwareType.WEB_APPLICATION
    assert canonicalize_software_type("web_api") is SoftwareType.DEPLOYABLE_SERVICE
    assert canonicalize_software_type("infra_tool") is SoftwareType.INFRASTRUCTURE_TOOL
    assert canonicalize_software_type("desktop_app") is SoftwareType.DESKTOP_APPLICATION
    assert canonicalize_software_type("ml_platform") is SoftwareType.UNKNOWN


def test_specific_identity_is_open_but_canonical_values_are_not_observations():
    assert normalize_specific_identity("Model Serving") == "model_serving"
    assert normalize_specific_identity("model-serving") == "model_serving"
    assert normalize_specific_identity("deployable_service") is None
    assert normalize_specific_identity("unknown") is None
    assert normalize_specific_identity("") is None
    assert "specific_identity" in StackAnalysis.model_fields
