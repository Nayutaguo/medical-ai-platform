import pytest

from medical_ai.query import QueryValidationError, validate_query_spec


def base_query() -> dict:
    return {
        "table": "inpatient",
        "filters": [{"field": "DischargeYear", "op": "=", "value": 2021}],
        "group_by": ["AgeGroup"],
        "metrics": [
            {"field": "TotalCharges", "agg": "avg", "alias": "avg_total_charges"},
            {"field": "*", "agg": "count", "alias": "patient_count"},
        ],
        "order_by": [{"field": "avg_total_charges", "direction": "desc"}],
        "limit": 100,
    }


def test_valid_query_spec_passes() -> None:
    spec = validate_query_spec(base_query())
    assert spec.table == "inpatient"
    assert spec.metrics[0].alias == "avg_total_charges"


def test_invalid_table_rejected() -> None:
    query = base_query()
    query["table"] = "inpatient; DROP TABLE inpatient"

    with pytest.raises(QueryValidationError):
        validate_query_spec(query)


def test_invalid_field_rejected() -> None:
    query = base_query()
    query["filters"][0]["field"] = "NotAField"

    with pytest.raises(QueryValidationError):
        validate_query_spec(query)


def test_invalid_operator_rejected() -> None:
    query = base_query()
    query["filters"][0]["op"] = "contains"

    with pytest.raises(QueryValidationError):
        validate_query_spec(query)


def test_limit_enforced() -> None:
    query = base_query()
    query["limit"] = 1001

    with pytest.raises(QueryValidationError):
        validate_query_spec(query)


def test_raw_sql_extra_field_rejected() -> None:
    query = base_query()
    query["sql"] = "DROP TABLE inpatient"

    with pytest.raises(QueryValidationError):
        validate_query_spec(query)


def test_only_count_can_use_star_metric() -> None:
    query = base_query()
    query["metrics"] = [{"field": "*", "agg": "avg", "alias": "bad_avg"}]

    with pytest.raises(QueryValidationError):
        validate_query_spec(query)


def test_avg_requires_numeric_field() -> None:
    query = base_query()
    query["metrics"] = [{"field": "AgeGroup", "agg": "avg", "alias": "bad_avg"}]

    with pytest.raises(QueryValidationError):
        validate_query_spec(query)


def test_in_filter_requires_non_empty_list() -> None:
    query = base_query()
    query["filters"] = [{"field": "AgeGroup", "op": "in", "value": []}]

    with pytest.raises(QueryValidationError):
        validate_query_spec(query)


@pytest.mark.parametrize(
    "alias",
    ["__privacy_group_count", "__PRIVACY_override", "__privacy_custom"],
)
def test_reserved_privacy_metric_alias_prefix_cannot_be_smuggled(alias: str) -> None:
    query = base_query()
    query["metrics"] = [{"field": "*", "agg": "count", "alias": alias}]

    with pytest.raises(QueryValidationError, match="reserved"):
        validate_query_spec(query)


def test_privacy_threshold_cannot_be_supplied_through_query_spec() -> None:
    query = base_query()
    query["privacy_min_group_size"] = 1

    with pytest.raises(QueryValidationError, match="privacy_min_group_size"):
        validate_query_spec(query)


@pytest.mark.parametrize(
    ("field", "operator", "value"),
    [
        ("AgeGroup", "in", [["nested"]]),
        ("AgeGroup", "in", [None]),
        ("DischargeYear", "=", "2021"),
        ("TotalCharges", "=", float("inf")),
        ("AgeGroup", "=", "x" * 513),
    ],
)
def test_filter_values_reject_nested_type_confused_or_unbounded_inputs(
    field: str,
    operator: str,
    value: object,
) -> None:
    query = base_query()
    query["filters"] = [{"field": field, "op": operator, "value": value}]

    with pytest.raises(QueryValidationError):
        validate_query_spec(query)


def test_between_requires_an_ascending_typed_range() -> None:
    query = base_query()
    query["filters"] = [
        {"field": "DischargeYear", "op": "between", "value": [2022, 2021]}
    ]

    with pytest.raises(QueryValidationError, match="ascending range"):
        validate_query_spec(query)


@pytest.mark.parametrize("pattern", ["%", "_", "%" + "x" * 160])
def test_like_pattern_is_bounded_and_must_be_selective(pattern: str) -> None:
    query = base_query()
    query["filters"] = [{"field": "AgeGroup", "op": "like", "value": pattern}]

    with pytest.raises(QueryValidationError):
        validate_query_spec(query)
