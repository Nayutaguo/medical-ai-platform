from medical_ai.agent.query_planner import QueryPlanner, extract_json_object, normalize_llm_queryspec


class FakeClient:
    def chat_json(self, messages, max_tokens=1200):
        return """```json
        {
          "table": "inpatient",
          "filters": [{"field": "DischargeYear", "op": "=", "value": 2021}],
          "group_by": ["AgeGroup"],
          "metrics": [{"field": "*", "agg": "count", "alias": "patient_count"}],
          "order_by": [{"field": "patient_count", "direction": "desc"}],
          "limit": 5
        }
        ```"""


def test_extract_json_object_from_markdown() -> None:
    assert extract_json_object("```json\n{\"table\":\"inpatient\"}\n```") == {"table": "inpatient"}


def test_query_planner_validates_llm_output() -> None:
    planned = QueryPlanner(client=FakeClient()).plan("按年龄组统计人数")

    assert planned.query_spec.table == "inpatient"
    assert planned.query_spec.metrics[0].alias == "patient_count"


def test_normalize_llm_queryspec_accepts_operator_alias_and_drops_metric_select_expression() -> None:
    normalized = normalize_llm_queryspec(
        {
            "table": "inpatient",
            "select": ["AgeGroup", "avg(TotalCharges) as avg_total_charges"],
            "filters": [
                {"field": "DischargeYear", "operator": "=", "value": 2021},
                {"field": "AgeGroup", "operator": "in", "value": ["50to69", "70orOlder"]},
            ],
            "group_by": ["AgeGroup"],
            "metrics": [{"field": "TotalCharges", "agg": "avg", "alias": "avg_total_charges"}],
            "order_by": [{"field": "avg_total_charges", "direction": "desc"}],
            "limit": 100,
        }
    )

    assert normalized["select"] == ["AgeGroup"]
    assert normalized["filters"][0]["op"] == "="
    assert "operator" not in normalized["filters"][0]


def test_normalize_llm_queryspec_accepts_function_alias_and_group_by_field_object() -> None:
    normalized = normalize_llm_queryspec(
        {
            "table": "inpatient",
            "filters": [{"field": "DischargeYear", "op": "=", "value": 2021}],
            "group_by": [{"field": "AgeGroup"}],
            "metrics": [{"function": "avg", "field": "TotalCharges", "alias": "avg_total_charges"}],
            "order_by": [{"field": "AgeGroup"}],
        }
    )

    assert normalized["group_by"] == ["AgeGroup"]
    assert normalized["metrics"][0]["agg"] == "avg"
    assert "function" not in normalized["metrics"][0]


def test_normalize_llm_queryspec_accepts_dir_alias_and_drops_grouped_metric_source_field() -> None:
    normalized = normalize_llm_queryspec(
        {
            "table": "inpatient",
            "select": ["AgeGroup", "TotalCharges"],
            "group_by": ["AgeGroup"],
            "metrics": [{"agg": "avg", "field": "TotalCharges", "alias": "avg_total_charges"}],
            "order_by": [{"field": "AgeGroup", "dir": "asc"}],
        }
    )

    assert normalized["select"] == ["AgeGroup"]
    assert normalized["order_by"][0]["direction"] == "asc"
    assert "dir" not in normalized["order_by"][0]


def test_normalize_llm_queryspec_drops_null_limit_and_lowercases_enums() -> None:
    normalized = normalize_llm_queryspec(
        {
            "table": "inpatient",
            "filters": [{"field": "DischargeYear", "op": "=", "value": 2021}],
            "group_by": ["AgeGroup"],
            "metrics": [{"field": "*", "agg": "COUNT", "alias": "patient_count"}],
            "order_by": [{"field": "patient_count", "direction": "DESC"}],
            "limit": None,
        }
    )

    assert "limit" not in normalized
    assert normalized["metrics"][0]["agg"] == "count"
    assert normalized["order_by"][0]["direction"] == "desc"


def test_normalize_llm_queryspec_adds_star_for_count_metric_without_field() -> None:
    normalized = normalize_llm_queryspec(
        {
            "table": "inpatient",
            "group_by": ["AgeGroup"],
            "metrics": [{"agg": "count", "alias": "patient_count"}],
        }
    )

    assert normalized["metrics"][0]["field"] == "*"


def test_normalize_llm_queryspec_accepts_aggregation_alias_and_null_optional_lists() -> None:
    normalized = normalize_llm_queryspec(
        {
            "table": "inpatient",
            "select": None,
            "filters": None,
            "group_by": ["AgeGroup"],
            "metrics": [{"field": "*", "aggregation": "COUNT", "alias": "patient_count"}],
            "order_by": None,
        }
    )

    assert "select" not in normalized
    assert "filters" not in normalized
    assert normalized["metrics"][0]["agg"] == "count"
    assert "aggregation" not in normalized["metrics"][0]
    assert "order_by" not in normalized
