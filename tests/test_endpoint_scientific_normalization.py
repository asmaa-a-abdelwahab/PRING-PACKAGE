from __future__ import annotations

import math

import pytest

from pring.transform.endpoint_normalization import (
    THRESHOLD_LABEL_ENDPOINT_TYPES,
    normalize_endpoint_props,
)
from pring.utils.run_store import (
    LABEL_POLICY_ID,
    _endpoint_supervision_decision,
    _endpoint_supervision_label,
)


@pytest.mark.parametrize(
    ("endpoint_type", "family", "scale"),
    [
        ("IC50", "inhibition_potency", "pIC50"),
        ("Ki", "inhibition_affinity", "pKi"),
        ("Kd", "binding_affinity", "pKd"),
        ("EC50", "functional_potency", "pEC50"),
        ("AC50", "screening_activity", "pAC50"),
    ],
)
def test_supported_endpoint_types_keep_distinct_semantics(endpoint_type, family, scale):
    props = normalize_endpoint_props(
        {"endpoint_type": endpoint_type, "value": "10", "unit": "uM"}
    )
    assert props["endpoint_type"] == endpoint_type
    assert props["endpoint_family"] == family
    assert props["potency_scale_name"] == scale
    assert props["normalization_status"] == "normalized_concentration"
    assert props["threshold_label_eligible"] is True
    assert math.isclose(props["value_molar"], 1e-5)
    assert math.isclose(props["potency_value"], 5.0)


def test_log_endpoint_is_recognized_and_converted_without_cross_endpoint_conversion():
    props = normalize_endpoint_props({"endpoint_type": "PubChem_pKi_endpoint", "value": "7"})
    assert props["endpoint_type"] == "Ki"
    assert props["reported_scale"] == "negative_log10_molar"
    assert math.isclose(props["value_molar"], 1e-7)
    assert props["potency_scale_name"] == "pKi"
    assert props["potency_value"] == 7.0
    assert "IC50" not in props


@pytest.mark.parametrize(
    ("qualifier", "expected_name", "bound_key", "inclusive"),
    [
        ("<", "lt", "value_upper_molar", False),
        ("<=", "le", "value_upper_molar", True),
        (">", "gt", "value_lower_molar", False),
        (">=", "ge", "value_lower_molar", True),
    ],
)
def test_concentration_qualifiers_become_explicit_molar_bounds(
    qualifier, expected_name, bound_key, inclusive
):
    props = normalize_endpoint_props(
        {"endpoint_type": "IC50", "value": "10", "unit": "uM", "qualifier": qualifier}
    )
    assert props["qualifier_normalized"] == expected_name
    assert math.isclose(props[bound_key], 1e-5)
    assert props[f"{bound_key.rsplit('_', 1)[0]}_inclusive"] is inclusive


def test_px_inequality_direction_is_reversed_in_molar_space():
    props = normalize_endpoint_props(
        {"endpoint_type": "pKi", "value": "6", "qualifier": ">"}
    )
    assert props["qualifier_normalized"] == "gt"
    assert math.isclose(props["value_upper_molar"], 1e-6)
    assert props["value_upper_inclusive"] is False


def test_px_range_is_converted_to_an_ordered_molar_interval():
    props = normalize_endpoint_props(
        {"endpoint_type": "pIC50", "value": "5-7"}
    )
    assert props["normalization_status"] == "normalized_log_molar_range"
    assert math.isclose(props["value_lower_molar"], 1e-7)
    assert math.isclose(props["value_upper_molar"], 1e-5)
    assert props["threshold_label_eligible"] is True


def test_range_crossing_threshold_is_preserved_and_abstains():
    props = normalize_endpoint_props(
        {"endpoint_type": "AC50", "value": "5-20", "unit": "uM"}
    )
    assert props["qualifier_normalized"] == "range"
    assert math.isclose(props["value_lower_molar"], 5e-6)
    assert math.isclose(props["value_upper_molar"], 20e-6)
    decision = _endpoint_supervision_decision(
        props,
        activity_threshold_um=10,
        weak_activity_as_negative=True,
    )
    assert decision["label"] is None
    assert decision["reason"] == "measurement_interval_crosses_activity_threshold"


@pytest.mark.parametrize("endpoint_type", sorted(THRESHOLD_LABEL_ENDPOINT_TYPES))
def test_all_supported_endpoints_require_a_declared_threshold(endpoint_type):
    props = normalize_endpoint_props(
        {"endpoint_type": endpoint_type, "value": "5", "unit": "uM"}
    )
    assert _endpoint_supervision_label(props) is None
    assert _endpoint_supervision_label(
        props,
        activity_threshold_um=10,
        weak_activity_as_negative=True,
    ) == 1


def test_boundary_and_weak_negative_rules_are_conservative():
    at_or_above = normalize_endpoint_props(
        {"endpoint_type": "Kd", "value": "10", "unit": "uM", "qualifier": ">="}
    )
    strictly_above = normalize_endpoint_props(
        {"endpoint_type": "Kd", "value": "10", "unit": "uM", "qualifier": ">"}
    )
    below = normalize_endpoint_props(
        {"endpoint_type": "Kd", "value": "10", "unit": "uM", "qualifier": "<"}
    )
    assert _endpoint_supervision_label(
        at_or_above, activity_threshold_um=10, weak_activity_as_negative=True
    ) is None
    assert _endpoint_supervision_label(
        strictly_above, activity_threshold_um=10, weak_activity_as_negative=True
    ) == 0
    assert _endpoint_supervision_label(
        below, activity_threshold_um=10, weak_activity_as_negative=True
    ) == 1


def test_unsupported_units_nonpositive_values_and_km_abstain():
    unsupported_unit = normalize_endpoint_props(
        {"endpoint_type": "IC50", "value": "5", "unit": "mg/L"}
    )
    nonpositive = normalize_endpoint_props(
        {"endpoint_type": "Ki", "value": "0", "unit": "uM"}
    )
    km = normalize_endpoint_props(
        {"endpoint_type": "Km", "value": "1", "unit": "uM"}
    )
    assert unsupported_unit["normalization_status"] == "unsupported_unit"
    assert nonpositive["normalization_status"] == "nonpositive_value"
    assert km["threshold_label_eligible"] is False
    for props in (unsupported_unit, nonpositive, km):
        assert _endpoint_supervision_label(
            props, activity_threshold_um=10, weak_activity_as_negative=True
        ) is None


def test_source_outcomes_are_traceable_and_conflicts_force_abstention():
    source_active = _endpoint_supervision_decision(
        {"outcome_label_normalized": "active"},
        activity_threshold_um=10,
        weak_activity_as_negative=True,
    )
    assert source_active["label"] == 1
    assert source_active["evidence_basis"] == "source_activity_outcome"
    assert source_active["reliability"] == "source_asserted"
    assert source_active["label_policy_id"] == LABEL_POLICY_ID

    conflict = normalize_endpoint_props(
        {
            "endpoint_type": "IC50",
            "value": "1",
            "unit": "uM",
            "outcome": "inactive",
        }
    )
    decision = _endpoint_supervision_decision(
        conflict,
        activity_threshold_um=10,
        weak_activity_as_negative=True,
    )
    assert decision["label"] is None
    assert decision["reliability"] == "conflicting"
