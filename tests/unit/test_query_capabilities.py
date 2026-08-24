import pytest

from medical_ai.db.schema import INPATIENT_COLUMNS
from medical_ai.query import (
    INPATIENT_FIELD_CAPABILITIES,
    CardinalityClass,
    QueryValidationError,
    SensitivityLevel,
    require_governed_distinct_field,
    require_governed_query,
    validate_query_spec,
)


def test_capability_matrix_covers_all_thirty_four_inpatient_fields() -> None:
    assert len(INPATIENT_FIELD_CAPABILITIES) == 34
    assert set(INPATIENT_FIELD_CAPABILITIES) == set(INPATIENT_COLUMNS)

    charges = INPATIENT_FIELD_CAPABILITIES["TotalCharges"]
    assert charges.cardinality is CardinalityClass.CONTINUOUS
    assert charges.sensitivity is SensitivityLevel.HIGH
    assert charges.governed_distinct is False


@pytest.mark.parametrize(
    "field",
    ["AgeGroup", "Gender", "AdmissionType", "PaymentTypology1"],
)
def test_common_categorical_fields_allow_governed_distinct(field: str) -> None:
    require_governed_distinct_field("inpatient", field)


@pytest.mark.parametrize(
    "field",
    [
        "TotalCharges",
        "TotalCosts",
        "BirthWeight",
        "ZipCode3Digits",
        "CCSRDiagnosisCode",
        "CCSRDiagnosisDescription",
        "CCSRProcedureCode",
        "CCSRProcedureDescription",
        "PermanentFacilityId",
        "OperatingCertificateNumber",
        "FacilityName",
    ],
)
def test_sensitive_or_high_cardinality_fields_reject_governed_distinct(
    field: str,
) -> None:
    with pytest.raises(QueryValidationError, match="distinct"):
        require_governed_distinct_field("inpatient", field)


def test_common_dimensions_and_count_avg_sum_are_governed() -> None:
    spec = validate_query_spec(
        {
            "table": "inpatient",
            "group_by": ["AgeGroup", "Gender", "AdmissionType"],
            "metrics": [
                {"field": "*", "agg": "count", "alias": "patient_count"},
                {
                    "field": "TotalCharges",
                    "agg": "avg",
                    "alias": "avg_total_charges",
                },
                {
                    "field": "TotalCosts",
                    "agg": "sum",
                    "alias": "sum_total_costs",
                },
            ],
        }
    )

    require_governed_query(spec)


def test_governed_query_allows_coarse_year_and_exact_diagnosis_cohort_filters() -> None:
    spec = validate_query_spec(
        {
            "table": "inpatient",
            "filters": [
                {"field": "DischargeYear", "op": "between", "value": [2020, 2021]},
                {
                    "field": "CCSRDiagnosisCode",
                    "op": "in",
                    "value": ["DX001", "DX002"],
                },
            ],
            "group_by": ["AgeGroup"],
            "metrics": [{"field": "*", "agg": "count", "alias": "patient_count"}],
        }
    )

    require_governed_query(spec)


@pytest.mark.parametrize(
    ("field", "operator", "value"),
    [
        ("TotalCharges", ">", 1000),
        ("BirthWeight", "between", [1000, 1200]),
        ("ZipCode3Digits", "=", "100"),
        ("FacilityName", "=", "hidden-facility"),
        ("CCSRDiagnosisDescription", "like", "%rare%"),
    ],
)
def test_governed_query_rejects_sensitive_or_high_cost_filters(
    field: str,
    operator: str,
    value: object,
) -> None:
    spec = validate_query_spec(
        {
            "table": "inpatient",
            "filters": [{"field": field, "op": operator, "value": value}],
            "group_by": ["AgeGroup"],
            "metrics": [{"field": "*", "agg": "count", "alias": "patient_count"}],
        }
    )

    with pytest.raises(QueryValidationError, match="筛选"):
        require_governed_query(spec)


def test_governed_query_limits_dimension_and_filter_composition() -> None:
    too_many_dimensions = validate_query_spec(
        {
            "table": "inpatient",
            "group_by": ["AgeGroup", "Gender", "AdmissionType", "PaymentTypology1"],
            "metrics": [{"field": "*", "agg": "count", "alias": "patient_count"}],
        }
    )
    too_many_filters = validate_query_spec(
        {
            "table": "inpatient",
            "filters": [
                {"field": "AgeGroup", "op": "=", "value": f"group-{index}"}
                for index in range(7)
            ],
            "metrics": [{"field": "*", "agg": "count", "alias": "patient_count"}],
        }
    )

    with pytest.raises(QueryValidationError, match="分组维度"):
        require_governed_query(too_many_dimensions)
    with pytest.raises(QueryValidationError, match="筛选条件"):
        require_governed_query(too_many_filters)


@pytest.mark.parametrize("aggregation", ["min", "max"])
def test_governed_query_rejects_extreme_aggregations(aggregation: str) -> None:
    spec = validate_query_spec(
        {
            "table": "inpatient",
            "group_by": ["AgeGroup"],
            "metrics": [
                {
                    "field": "TotalCharges",
                    "agg": aggregation,
                    "alias": "charge_extreme",
                }
            ],
        }
    )

    with pytest.raises(QueryValidationError, match="min/max"):
        require_governed_query(spec)


@pytest.mark.parametrize(
    "field",
    [
        "ZipCode3Digits",
        "CCSRDiagnosisDescription",
        "CCSRProcedureDescription",
        "PermanentFacilityId",
    ],
)
def test_governed_query_rejects_unsafe_dimensions(field: str) -> None:
    spec = validate_query_spec(
        {
            "table": "inpatient",
            "group_by": [field],
            "metrics": [{"field": "*", "agg": "count", "alias": "patient_count"}],
        }
    )

    with pytest.raises(QueryValidationError, match="分组或选择"):
        require_governed_query(spec)


def test_governed_policy_error_never_echoes_filter_value() -> None:
    secret_value = "rare-diagnosis-value-that-must-not-be-echoed"
    spec = validate_query_spec(
        {
            "table": "inpatient",
            "filters": [
                {
                    "field": "CCSRDiagnosisDescription",
                    "op": "=",
                    "value": secret_value,
                }
            ],
            "group_by": ["ZipCode3Digits"],
            "metrics": [{"field": "*", "agg": "count", "alias": "patient_count"}],
        }
    )

    with pytest.raises(QueryValidationError) as captured:
        require_governed_query(spec)

    assert secret_value not in str(captured.value)
