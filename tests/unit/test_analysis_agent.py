import json

import pytest

from medical_ai.agent.analysis_agent import MedicalDataAgent, normalize_agent_plan
from medical_ai.agent.intent import IntentType
from medical_ai.agent.llm_client import LLMResponseError
from medical_ai.db.executor import QueryResult
from medical_ai.query import PRIVACY_GROUP_COUNT_ALIAS, QueryValidationError


class FakeAgentClient:
    def __init__(self, plan_payload: dict | None = None) -> None:
        self.calls = 0
        self.messages = []
        self.plan_payload = plan_payload or _valid_plan_payload()

    def chat_json(self, messages, max_tokens=1200):
        self.calls += 1
        self.messages.append(messages)
        if self.calls == 1:
            return json.dumps(self.plan_payload)
        return json.dumps(
            {
                "summary": "70岁以上组的平均总费用更高。",
                "chart_reading": "柱状图按年龄组展示平均总费用，柱越高表示平均收费越高。",
                "observations": ["70orOlder 高于 50to69", "结果包含 2 个年龄组"],
                "limitations": ["这是开发样例数据，不能代表完整医疗结论"],
                "follow_up_questions": ["是否按入院类型进一步拆分？"],
            },
            ensure_ascii=False,
        )


class FakeExecutor:
    def __init__(self, privacy_result: QueryResult | None = None) -> None:
        self.executed_queries = []
        self.distinct_requests = []
        self.privacy_result = privacy_result

    def execute(self, query):
        self.executed_queries.append(query)
        if PRIVACY_GROUP_COUNT_ALIAS in query.sql:
            if self.privacy_result is not None:
                return self.privacy_result
            return QueryResult(
                columns=[
                    "AgeGroup",
                    "avg_total_charges",
                    "patient_count",
                    PRIVACY_GROUP_COUNT_ALIAS,
                ],
                rows=[
                    {
                        "AgeGroup": "70orOlder",
                        "avg_total_charges": 78913.1,
                        "patient_count": 202,
                        PRIVACY_GROUP_COUNT_ALIAS: 202,
                    },
                    {
                        "AgeGroup": "50to69",
                        "avg_total_charges": 73057.5,
                        "patient_count": 284,
                        PRIVACY_GROUP_COUNT_ALIAS: 284,
                    },
                ],
                row_count=2,
                query_time_ms=7.5,
                metadata={"table": "inpatient", "limit": 100},
            )
        return QueryResult(
            columns=["AgeGroup", "avg_total_charges", "patient_count"],
            rows=[
                {"AgeGroup": "70orOlder", "avg_total_charges": 78913.1, "patient_count": 202},
                {"AgeGroup": "50to69", "avg_total_charges": 73057.5, "patient_count": 284},
            ],
            row_count=2,
            query_time_ms=7.5,
            metadata={"table": "inpatient", "limit": 100},
        )

    def fetch_distinct_values(self, table: str, field: str, limit: int):
        self.distinct_requests.append((table, field, limit))
        return QueryResult(
            columns=[field],
            rows=[{field: "legacy-value"}],
            row_count=1,
            query_time_ms=1.0,
        )


class FailingInsightClient(FakeAgentClient):
    def chat_json(self, messages, max_tokens=1200):
        if self.calls == 0:
            return super().chat_json(messages, max_tokens=max_tokens)
        self.calls += 1
        raise LLMResponseError("LLM API returned an empty response")


def test_medical_data_agent_runs_llm_plan_execute_and_interpret() -> None:
    client = FakeAgentClient()
    run = MedicalDataAgent(client=client, executor=FakeExecutor()).run(
        "比较2021年50到69岁和70岁以上患者的平均总费用",
        execute=True,
        interpret=True,
    )

    assert client.calls == 2
    assert run.plan.intent.intent_type is IntentType.COMPARISON
    assert run.plan.query_spec is not None
    assert run.compiled_query is not None
    assert run.result is not None
    assert run.insight is not None
    assert run.plan.chart_spec is not None
    assert run.plan.chart_spec.chart_type.value == "bar"
    assert "70岁以上" in run.insight.summary


def test_agent_returns_query_result_when_insight_generation_is_unavailable() -> None:
    client = FailingInsightClient()
    run = MedicalDataAgent(client=client, executor=FakeExecutor()).run(
        "比较2021年50到69岁和70岁以上患者的平均总费用",
        execute=True,
        interpret=True,
    )

    assert client.calls == 2
    assert run.result is not None
    assert run.result.row_count == 2
    assert run.insight is None
    assert run.warnings[0].code == "INSIGHT_UNAVAILABLE"
    assert run.to_dict()["warnings"][0]["message"].startswith("查询已完成")


def test_normalize_agent_plan_accepts_common_llm_aliases() -> None:
    normalized = normalize_agent_plan(
        {
            "intent": "ranking",
            "intent_confidence": 0.82,
            "tool": "query_medical_data",
            "querySpec": {
                "table": "inpatient",
                "filters": [{"field": "DischargeYear", "operator": "=", "value": 2021}],
                "group_by": [{"field": "AdmissionType"}],
                "metrics": [{"field": "*", "function": "count", "alias": "patient_count"}],
                "order_by": [{"field": "patient_count", "dir": "desc"}],
                "limit": 10,
            },
            "chart": {"type": "bar", "x": "AdmissionType", "y": "patient_count"},
        }
    )

    assert normalized["intent"]["intent_type"] == "ranking"
    assert normalized["intent"]["route"] == "query"
    assert normalized["tool_name"] == "query_medical_data"
    assert normalized["query_spec"]["filters"][0]["op"] == "="
    assert normalized["query_spec"]["group_by"] == ["AdmissionType"]
    assert normalized["query_spec"]["metrics"][0]["agg"] == "count"
    assert normalized["chart_spec"]["chart_type"] == "bar"


def test_agent_plan_accepts_aggregation_alias_and_null_order_by() -> None:
    payload = _valid_plan_payload()
    first_metric = payload["query_spec"]["metrics"][0]
    first_metric["aggregation"] = first_metric.pop("agg")
    payload["query_spec"]["order_by"] = None
    payload["chart_spec"]["dimension"] = payload["chart_spec"].pop("x_field")
    payload["chart_spec"]["metric"] = payload["chart_spec"].pop("y_field")

    plan, _ = MedicalDataAgent(client=FakeAgentClient(payload), executor=FakeExecutor()).plan(
        "比较2021年不同年龄组的平均总费用"
    )

    assert plan.query_spec is not None
    assert plan.query_spec.metrics[0].agg.value == "avg"
    assert plan.query_spec.order_by == []
    assert plan.chart_spec is not None
    assert plan.chart_spec.x_field == "AgeGroup"
    assert plan.chart_spec.y_field == "avg_total_charges"


def test_agent_recommends_chart_when_llm_chart_references_missing_field() -> None:
    payload = _valid_plan_payload()
    payload["chart_spec"] = {
        "chart_type": "bar",
        "x_field": "AgeGroup",
        "y_field": "not_in_result",
        "title": "Bad chart",
        "reason": "bad field",
    }
    run = MedicalDataAgent(client=FakeAgentClient(payload), executor=FakeExecutor()).run(
        "比较2021年不同年龄组的平均总费用",
        execute=True,
        interpret=False,
    )

    assert run.plan.chart_spec is not None
    assert run.plan.chart_spec.y_field == "avg_total_charges"


def test_product_agent_rejects_patient_level_query_plan() -> None:
    payload = _valid_plan_payload()
    payload["query_spec"] = {"table": "inpatient", "select": ["AgeGroup"], "limit": 10}

    with pytest.raises(QueryValidationError, match="仅允许返回聚合结果"):
        MedicalDataAgent(
            client=FakeAgentClient(payload),
            executor=FakeExecutor(),
            require_aggregate=True,
        ).plan("列出患者年龄组")


@pytest.mark.parametrize("trusted_facility_ids", [None, [], (), frozenset()])
def test_agent_rejects_explicit_empty_trusted_facility_scope(
    trusted_facility_ids,
) -> None:
    with pytest.raises(QueryValidationError, match="scope must not be empty"):
        MedicalDataAgent(
            client=FakeAgentClient(),
            executor=FakeExecutor(),
            trusted_facility_ids=trusted_facility_ids,
        )


def test_scoped_agent_compiles_execute_false_query_outside_query_spec_and_prompt() -> None:
    trusted_facility = "facility-secret-42"
    client = FakeAgentClient()
    executor = FakeExecutor()
    run = MedicalDataAgent(
        client=client,
        executor=executor,
        require_aggregate=True,
        trusted_facility_ids=[trusted_facility],
    ).run(
        "比较2021年不同年龄组的平均总费用",
        execute=False,
        interpret=False,
    )

    assert run.compiled_query is not None
    assert "`PermanentFacilityId` IN" in run.compiled_query.sql
    assert PRIVACY_GROUP_COUNT_ALIAS in run.compiled_query.sql
    assert trusted_facility in run.compiled_query.params.values()
    assert executor.executed_queries == []
    assert run.tool_result is None
    assert run.plan.query_spec is not None
    assert all(
        filter_spec.field != "PermanentFacilityId"
        for filter_spec in run.plan.query_spec.filters
    )
    prompt_payload = json.dumps(
        [message.content for call in client.messages for message in call],
        ensure_ascii=False,
    )
    assert trusted_facility not in prompt_payload
    assert PRIVACY_GROUP_COUNT_ALIAS not in json.dumps(run.to_dict(), ensure_ascii=False)


def test_scoped_agent_distinct_uses_scoped_compiler_not_unrestricted_fetch() -> None:
    trusted_facility = "facility-secret-42"
    client = FakeAgentClient(_distinct_plan_payload())
    executor = FakeExecutor(
        privacy_result=QueryResult(
            columns=["AgeGroup", PRIVACY_GROUP_COUNT_ALIAS],
            rows=[
                {"AgeGroup": "safe-category", PRIVACY_GROUP_COUNT_ALIAS: 9},
                {
                    "AgeGroup": "suppressed-secret-category",
                    PRIVACY_GROUP_COUNT_ALIAS: 2,
                },
            ],
            row_count=2,
            query_time_ms=1.0,
        )
    )
    run = MedicalDataAgent(
        client=client,
        executor=executor,
        trusted_facility_ids=[trusted_facility],
    ).run("年龄组有哪些值", interpret=False)

    assert executor.distinct_requests == []
    assert len(executor.executed_queries) == 1
    compiled = executor.executed_queries[0]
    assert "`PermanentFacilityId` IN" in compiled.sql
    assert PRIVACY_GROUP_COUNT_ALIAS in compiled.sql
    assert trusted_facility in compiled.params.values()
    assert run.tool_result is not None
    assert run.tool_result["values"] == ["safe-category"]
    assert run.tool_result["metadata"]["privacy_applied"] is True
    assert "suppressed_group_count" not in run.tool_result["metadata"]
    assert "trusted_facility_ids" not in run.tool_result
    assert trusted_facility not in json.dumps(run.tool_result, ensure_ascii=False)
    assert PRIVACY_GROUP_COUNT_ALIAS not in json.dumps(
        run.tool_result,
        ensure_ascii=False,
    )
    assert "suppressed-secret-category" not in json.dumps(run.tool_result)


def test_scoped_agent_suppresses_before_secondary_interpretation() -> None:
    suppressed_name = "suppressed-secret-category"
    executor = FakeExecutor(
        privacy_result=QueryResult(
            columns=[
                "AgeGroup",
                "avg_total_charges",
                "patient_count",
                PRIVACY_GROUP_COUNT_ALIAS,
            ],
            rows=[
                {
                    "AgeGroup": "safe-category",
                    "avg_total_charges": 100.0,
                    "patient_count": 6,
                    PRIVACY_GROUP_COUNT_ALIAS: 6,
                },
                {
                    "AgeGroup": suppressed_name,
                    "avg_total_charges": 999.0,
                    "patient_count": 2,
                    PRIVACY_GROUP_COUNT_ALIAS: 2,
                },
            ],
            row_count=2,
            query_time_ms=3.0,
        )
    )
    client = FakeAgentClient()
    run = MedicalDataAgent(
        client=client,
        executor=executor,
        require_aggregate=True,
        trusted_facility_ids=["facility-a"],
        privacy_min_group_size=5,
    ).run("compare age groups", execute=True, interpret=True)

    assert run.result is not None
    assert run.result.rows == [
        {
            "AgeGroup": "safe-category",
            "avg_total_charges": 100.0,
            "patient_count": 6,
        }
    ]
    assert "suppressed_group_count" not in run.result.metadata
    insight_messages = json.dumps(
        [message.content for message in client.messages[1]],
        ensure_ascii=False,
    )
    assert suppressed_name not in insight_messages
    assert "suppressed_group_count" not in insight_messages
    public_payload = json.dumps(run.to_dict(), ensure_ascii=False)
    assert suppressed_name not in public_payload
    assert PRIVACY_GROUP_COUNT_ALIAS not in public_payload


def test_scoped_agent_and_insight_cannot_distinguish_empty_from_small_only() -> None:
    common_columns = [
        "AgeGroup",
        "avg_total_charges",
        "patient_count",
        PRIVACY_GROUP_COUNT_ALIAS,
    ]
    empty_raw = QueryResult(
        columns=common_columns,
        rows=[],
        row_count=0,
        query_time_ms=2.0,
        truncated=False,
        metadata={"table": "inpatient", "limit": 100},
    )
    small_raw = QueryResult(
        columns=common_columns,
        rows=[
            {
                "AgeGroup": "suppressed-secret-category",
                "avg_total_charges": 999.0,
                "patient_count": 2,
                PRIVACY_GROUP_COUNT_ALIAS: 2,
            }
        ],
        row_count=1,
        query_time_ms=2.0,
        truncated=True,
        metadata={
            "table": "inpatient",
            "limit": 100,
            "source_row_count": 1,
        },
    )

    def run_with(raw_result: QueryResult):
        client = FakeAgentClient()
        run = MedicalDataAgent(
            client=client,
            executor=FakeExecutor(privacy_result=raw_result),
            require_aggregate=True,
            trusted_facility_ids=["facility-a"],
            privacy_min_group_size=5,
        ).run("compare age groups", execute=True, interpret=True)
        return run.to_dict(), client.messages[1][1].content

    empty_payload, empty_insight_input = run_with(empty_raw)
    small_payload, small_insight_input = run_with(small_raw)

    assert empty_payload == small_payload
    assert empty_insight_input == small_insight_input
    assert "suppressed-secret-category" not in small_insight_input


def test_legacy_agent_distinct_preserves_executor_helper_path() -> None:
    executor = FakeExecutor()
    run = MedicalDataAgent(
        client=FakeAgentClient(_distinct_plan_payload()),
        executor=executor,
    ).run("年龄组有哪些值", interpret=False)

    assert executor.distinct_requests == [("inpatient", "AgeGroup", 30)]
    assert executor.executed_queries == []
    assert run.tool_result is not None
    assert run.tool_result["values"] == ["legacy-value"]


@pytest.mark.parametrize("aggregation", ["min", "max"])
def test_scoped_agent_rejects_extreme_aggregation_after_plan_validation(
    aggregation: str,
) -> None:
    payload = _valid_plan_payload()
    payload["query_spec"]["metrics"] = [
        {
            "field": "TotalCharges",
            "agg": aggregation,
            "alias": "charge_extreme",
        }
    ]
    payload["query_spec"]["order_by"] = [
        {"field": "charge_extreme", "direction": "desc"}
    ]

    with pytest.raises(QueryValidationError, match="min/max"):
        MedicalDataAgent(
            client=FakeAgentClient(payload),
            executor=FakeExecutor(),
            require_aggregate=True,
            trusted_facility_ids=["facility-a"],
        ).plan("find a charge extreme")


def test_scoped_agent_rejects_unsafe_grouping_after_plan_validation() -> None:
    payload = _valid_plan_payload()
    payload["query_spec"]["group_by"] = ["ZipCode3Digits"]
    payload["query_spec"]["order_by"] = []
    payload["chart_spec"] = None

    with pytest.raises(QueryValidationError, match="分组或选择"):
        MedicalDataAgent(
            client=FakeAgentClient(payload),
            executor=FakeExecutor(),
            require_aggregate=True,
            trusted_facility_ids=["facility-a"],
        ).plan("group by ZIP")


def test_scoped_agent_rejects_sensitive_distinct_before_execution() -> None:
    payload = _distinct_plan_payload()
    payload["tool_args"]["field"] = "TotalCharges"
    executor = FakeExecutor()

    with pytest.raises(QueryValidationError, match="distinct"):
        MedicalDataAgent(
            client=FakeAgentClient(payload),
            executor=executor,
            trusted_facility_ids=["facility-a"],
        ).run("list charge values", interpret=False)

    assert executor.distinct_requests == []
    assert executor.executed_queries == []


def test_legacy_agent_still_allows_extreme_aggregation_plan() -> None:
    payload = _valid_plan_payload()
    payload["query_spec"]["metrics"] = [
        {
            "field": "TotalCharges",
            "agg": "max",
            "alias": "max_total_charges",
        }
    ]
    payload["query_spec"]["order_by"] = [
        {"field": "max_total_charges", "direction": "desc"}
    ]

    plan, _ = MedicalDataAgent(
        client=FakeAgentClient(payload),
        executor=FakeExecutor(),
        require_aggregate=True,
    ).plan("find maximum charge")

    assert plan.query_spec is not None
    assert plan.query_spec.metrics[0].agg.value == "max"


def _valid_plan_payload() -> dict:
    return {
        "intent": {
            "intent_type": "comparison",
            "route": "query",
            "confidence": 0.91,
            "reason": "The user asks to compare average charges across age groups.",
        },
        "tool_name": "query_medical_data",
        "tool_args": {},
        "analysis_goal": "Compare average total charges by age group for 2021.",
        "query_spec": {
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
        },
        "chart_spec": {
            "chart_type": "bar",
            "x_field": "AgeGroup",
            "y_field": "avg_total_charges",
            "title": "Average Total Charges by Age Group",
            "reason": "A bar chart compares average charges across age groups.",
        },
        "assumptions": ["Use TotalCharges for 总费用."],
        "execution_steps": ["Classify intent", "Create QuerySpec", "Execute query", "Interpret result"],
    }


def _distinct_plan_payload() -> dict:
    return {
        "intent": {
            "intent_type": "distinct_values",
            "route": "distinct",
            "confidence": 0.95,
            "reason": "The user asks for the available age groups.",
        },
        "tool_name": "get_distinct_values",
        "tool_args": {"table": "inpatient", "field": "AgeGroup", "limit": 30},
        "analysis_goal": "List available age groups.",
        "query_spec": None,
        "chart_spec": None,
        "assumptions": [],
        "execution_steps": ["Read distinct values"],
    }
