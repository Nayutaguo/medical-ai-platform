from collections.abc import Iterable

import pytest

from medical_ai.query import (
    PRIVACY_GROUP_COUNT_ALIAS,
    QueryValidationError,
    compile_query,
    compile_scoped_query,
    filter_minimum_group_rows,
)


def test_compile_aggregate_query_to_parameterized_mysql_sql() -> None:
    query = compile_query(
        {
            "table": "inpatient",
            "filters": [
                {"field": "DischargeYear", "op": "=", "value": 2021},
                {"field": "AgeGroup", "op": "in", "value": ["50to69", "70orOlder"]},
            ],
            "group_by": ["AgeGroup"],
            "metrics": [
                {"field": "TotalCharges", "agg": "avg", "alias": "avg_total_charges"},
                {"field": "*", "agg": "count", "alias": "patient_count"},
            ],
            "order_by": [{"field": "avg_total_charges", "direction": "desc"}],
            "limit": 100,
        }
    )

    assert "SELECT" in query.sql
    assert "FROM inpatient" in query.sql
    assert "WHERE" in query.sql
    assert "GROUP BY" in query.sql
    assert "ORDER BY" in query.sql
    assert "LIMIT" in query.sql
    assert 2021 in query.params.values()
    assert "50to69" in query.params.values()
    assert "70orOlder" in query.params.values()


def test_compile_does_not_inline_filter_values() -> None:
    dangerous = "x'; DROP TABLE inpatient; --"
    query = compile_query(
        {
            "table": "inpatient",
            "select": ["FacilityName"],
            "filters": [{"field": "FacilityName", "op": "=", "value": dangerous}],
            "limit": 5,
        }
    )

    assert "DROP TABLE" not in query.sql
    assert dangerous in query.params.values()


def test_compile_between_and_null_filters() -> None:
    query = compile_query(
        {
            "table": "inpatient",
            "select": ["FacilityName", "TotalCharges"],
            "filters": [
                {"field": "TotalCharges", "op": "between", "value": [10000, 50000]},
                {"field": "PaymentTypology1", "op": "is_not_null"},
            ],
            "order_by": [{"field": "TotalCharges", "direction": "asc"}],
            "limit": 10,
        }
    )

    assert "BETWEEN" in query.sql
    assert "IS NOT NULL" in query.sql
    assert 10000 in query.params.values()
    assert 50000 in query.params.values()


def test_compile_scoped_query_adds_bound_trusted_facility_predicate() -> None:
    trusted_facility = "facility-x'; DROP TABLE inpatient; --"
    query = compile_scoped_query(
        {
            "table": "inpatient",
            "group_by": ["AgeGroup"],
            "metrics": [{"field": "*", "agg": "count", "alias": "patient_count"}],
            "limit": 10,
        },
        trusted_facility_ids=[trusted_facility],
    )

    assert "PermanentFacilityId" in query.sql
    assert "DROP TABLE" not in query.sql
    assert trusted_facility in query.params.values()


@pytest.mark.parametrize(
    ("operator", "value", "operator_sql"),
    [
        ("=", "facility-denied", " = "),
        ("in", ["facility-allowed", "facility-denied"], " IN "),
        ("not_in", ["facility-allowed"], " NOT IN "),
    ],
)
def test_compile_scoped_query_stacks_scope_with_user_facility_filter(
    operator: str,
    value: object,
    operator_sql: str,
) -> None:
    query = compile_scoped_query(
        {
            "table": "inpatient",
            "filters": [
                {
                    "field": "PermanentFacilityId",
                    "op": operator,
                    "value": value,
                }
            ],
            "group_by": ["AgeGroup"],
            "metrics": [{"field": "*", "agg": "count", "alias": "patient_count"}],
            "limit": 10,
        },
        trusted_facility_ids=["facility-allowed"],
    )

    assert query.sql.count("`PermanentFacilityId`") == 2
    assert " AND " in query.sql
    assert operator_sql in query.sql
    assert "facility-allowed" in query.params.values()


@pytest.mark.parametrize("trusted_facility_ids", [None, [], (), frozenset()])
def test_compile_scoped_query_rejects_empty_scope(
    trusted_facility_ids: Iterable[str] | None,
) -> None:
    with pytest.raises(QueryValidationError, match="scope must not be empty"):
        compile_scoped_query(
            {
                "table": "inpatient",
                "group_by": ["AgeGroup"],
                "metrics": [{"field": "*", "agg": "count", "alias": "patient_count"}],
            },
            trusted_facility_ids=trusted_facility_ids,
        )


def test_compile_scoped_query_supports_more_than_one_thousand_trusted_facilities() -> None:
    trusted_facilities = [f"facility-{index:04d}" for index in range(1001)]
    query = compile_scoped_query(
        {
            "table": "inpatient",
            "group_by": ["AgeGroup"],
            "metrics": [{"field": "*", "agg": "count", "alias": "patient_count"}],
        },
        trusted_facility_ids=trusted_facilities,
    )

    assert query.sql.count("`PermanentFacilityId` IN") == 2
    assert " OR " in query.sql
    assert not any(facility_id in query.sql for facility_id in trusted_facilities)
    assert set(trusted_facilities).issubset(set(query.params.values()))


def test_trusted_scope_cannot_be_smuggled_through_query_spec() -> None:
    with pytest.raises(QueryValidationError, match="trusted_facility_ids"):
        compile_query(
            {
                "table": "inpatient",
                "group_by": ["AgeGroup"],
                "metrics": [{"field": "*", "agg": "count", "alias": "patient_count"}],
                "trusted_facility_ids": ["facility-untrusted"],
            }
        )


def test_compile_scoped_privacy_adds_hidden_count_to_global_aggregate() -> None:
    query = compile_scoped_query(
        {
            "table": "inpatient",
            "metrics": [
                {
                    "field": "TotalCharges",
                    "agg": "avg",
                    "alias": "avg_total_charges",
                }
            ],
        },
        trusted_facility_ids=["facility-a"],
        privacy_min_group_size=5,
    )

    assert f"AS {PRIVACY_GROUP_COUNT_ALIAS}" in query.sql
    assert "GROUP BY" not in query.sql
    assert "HAVING count(*) >=" in query.sql
    assert query.sql.index("HAVING") < query.sql.index("LIMIT")
    assert 5 in query.params.values()


def test_compile_scoped_privacy_adds_hidden_count_to_distinct_grouping() -> None:
    query = compile_scoped_query(
        {
            "table": "inpatient",
            "select": ["AgeGroup"],
            "group_by": ["AgeGroup"],
        },
        trusted_facility_ids=["facility-a"],
        privacy_min_group_size=5,
    )

    assert f"AS {PRIVACY_GROUP_COUNT_ALIAS}" in query.sql
    assert "GROUP BY inpatient.`AgeGroup`" in query.sql
    assert "HAVING count(*) >=" in query.sql
    assert query.sql.index("HAVING") < query.sql.index("LIMIT")
    assert 5 in query.params.values()


@pytest.mark.parametrize("privacy_min_group_size", [1, 101, True])
def test_compile_scoped_privacy_rejects_invalid_threshold(
    privacy_min_group_size,
) -> None:
    with pytest.raises(QueryValidationError, match="between 2 and 100"):
        compile_scoped_query(
            {
                "table": "inpatient",
                "group_by": ["AgeGroup"],
                "metrics": [
                    {"field": "*", "agg": "count", "alias": "patient_count"}
                ],
            },
            trusted_facility_ids=["facility-a"],
            privacy_min_group_size=privacy_min_group_size,
        )


def test_filter_minimum_group_rows_removes_hidden_counts_and_suppresses() -> None:
    filtered = filter_minimum_group_rows(
        [
            {"AgeGroup": "safe", PRIVACY_GROUP_COUNT_ALIAS: 5},
            {"AgeGroup": "small", PRIVACY_GROUP_COUNT_ALIAS: 4},
        ],
        ["AgeGroup", PRIVACY_GROUP_COUNT_ALIAS],
        5,
    )

    assert filtered.columns == ["AgeGroup"]
    assert filtered.rows == [{"AgeGroup": "safe"}]
    assert PRIVACY_GROUP_COUNT_ALIAS not in str(filtered)
