"""Central governed-query capabilities for all inpatient fields.

These single-query rules complement facility scope and minimum-group privacy.
They intentionally do not claim to prevent differencing across overlapping
queries; that requires a separate audit, rate-limit, and privacy-budget layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping

from medical_ai.query.models import Aggregation, FilterOperator, QuerySpec
from medical_ai.query.validator import QueryValidationError


class SensitivityLevel(StrEnum):
    """Coarse sensitivity classification used by governance policy."""

    STANDARD = "standard"
    SENSITIVE = "sensitive"
    HIGH = "high"


class CardinalityClass(StrEnum):
    """Expected field cardinality for governed analytical use."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CONTINUOUS = "continuous"


@dataclass(frozen=True, slots=True)
class FieldCapability:
    """Governed operations allowed for one physical inpatient field."""

    governed_distinct: bool
    governed_dimension: bool
    governed_aggregations: frozenset[Aggregation]
    governed_filter_ops: frozenset[FilterOperator]
    sensitivity: SensitivityLevel
    cardinality: CardinalityClass


COUNT_ONLY = frozenset({Aggregation.COUNT})
SAFE_NUMERIC_AGGREGATIONS = frozenset(
    {Aggregation.COUNT, Aggregation.SUM, Aggregation.AVG}
)
NO_GOVERNED_AGGREGATIONS: frozenset[Aggregation] = frozenset()
NO_GOVERNED_FILTERS: frozenset[FilterOperator] = frozenset()
CATEGORY_FILTERS = frozenset(
    {
        FilterOperator.EQ,
        FilterOperator.NE,
        FilterOperator.IN,
        FilterOperator.NOT_IN,
        FilterOperator.IS_NULL,
        FilterOperator.IS_NOT_NULL,
    }
)
COHORT_FILTERS = frozenset(
    {
        FilterOperator.EQ,
        FilterOperator.IN,
        FilterOperator.NOT_IN,
    }
)
YEAR_FILTERS = frozenset(
    {
        *CATEGORY_FILTERS,
        FilterOperator.GT,
        FilterOperator.GTE,
        FilterOperator.LT,
        FilterOperator.LTE,
        FilterOperator.BETWEEN,
    }
)
MAX_GOVERNED_DIMENSIONS = 3
MAX_GOVERNED_FILTERS = 6


def _category(
    *,
    sensitivity: SensitivityLevel = SensitivityLevel.SENSITIVE,
    cardinality: CardinalityClass = CardinalityClass.LOW,
) -> FieldCapability:
    return FieldCapability(
        governed_distinct=True,
        governed_dimension=True,
        governed_aggregations=COUNT_ONLY,
        governed_filter_ops=CATEGORY_FILTERS,
        sensitivity=sensitivity,
        cardinality=cardinality,
    )


def _blocked(
    *,
    sensitivity: SensitivityLevel,
    cardinality: CardinalityClass,
) -> FieldCapability:
    return FieldCapability(
        governed_distinct=False,
        governed_dimension=False,
        governed_aggregations=NO_GOVERNED_AGGREGATIONS,
        governed_filter_ops=NO_GOVERNED_FILTERS,
        sensitivity=sensitivity,
        cardinality=cardinality,
    )


def _numeric(
    *,
    sensitivity: SensitivityLevel,
) -> FieldCapability:
    return FieldCapability(
        governed_distinct=False,
        governed_dimension=False,
        governed_aggregations=SAFE_NUMERIC_AGGREGATIONS,
        governed_filter_ops=NO_GOVERNED_FILTERS,
        sensitivity=sensitivity,
        cardinality=CardinalityClass.CONTINUOUS,
    )


def _cohort_only(
    *,
    sensitivity: SensitivityLevel = SensitivityLevel.HIGH,
    cardinality: CardinalityClass = CardinalityClass.HIGH,
) -> FieldCapability:
    """Allow exact cohort selection without exposing a dimension or values."""

    return FieldCapability(
        governed_distinct=False,
        governed_dimension=False,
        governed_aggregations=NO_GOVERNED_AGGREGATIONS,
        governed_filter_ops=COHORT_FILTERS,
        sensitivity=sensitivity,
        cardinality=cardinality,
    )


def _year_dimension() -> FieldCapability:
    """Allow bounded range filters for the coarse public discharge year."""

    return FieldCapability(
        governed_distinct=True,
        governed_dimension=True,
        governed_aggregations=COUNT_ONLY,
        governed_filter_ops=YEAR_FILTERS,
        sensitivity=SensitivityLevel.STANDARD,
        cardinality=CardinalityClass.LOW,
    )


INPATIENT_FIELD_CAPABILITIES: Mapping[str, FieldCapability] = MappingProxyType(
    {
        "HospitalServiceArea": _blocked(
            sensitivity=SensitivityLevel.SENSITIVE,
            cardinality=CardinalityClass.MEDIUM,
        ),
        "HospitalCounty": _blocked(
            sensitivity=SensitivityLevel.HIGH,
            cardinality=CardinalityClass.MEDIUM,
        ),
        "OperatingCertificateNumber": _blocked(
            sensitivity=SensitivityLevel.HIGH,
            cardinality=CardinalityClass.HIGH,
        ),
        "PermanentFacilityId": _blocked(
            sensitivity=SensitivityLevel.HIGH,
            cardinality=CardinalityClass.HIGH,
        ),
        "FacilityName": _blocked(
            sensitivity=SensitivityLevel.HIGH,
            cardinality=CardinalityClass.HIGH,
        ),
        "AgeGroup": _category(),
        "ZipCode3Digits": _blocked(
            sensitivity=SensitivityLevel.HIGH,
            cardinality=CardinalityClass.HIGH,
        ),
        "Gender": _category(sensitivity=SensitivityLevel.HIGH),
        "Race": _category(sensitivity=SensitivityLevel.HIGH),
        "Ethnicity": _category(sensitivity=SensitivityLevel.HIGH),
        "RaceEthnicity": _category(sensitivity=SensitivityLevel.HIGH),
        "LengthOfStay": _numeric(sensitivity=SensitivityLevel.SENSITIVE),
        "AdmissionType": _category(),
        "PatientDisposition": _category(cardinality=CardinalityClass.MEDIUM),
        "DischargeYear": _year_dimension(),
        "CCSRDiagnosisCode": _cohort_only(),
        "CCSRDiagnosisDescription": _cohort_only(),
        "CCSRProcedureCode": _cohort_only(),
        "CCSRProcedureDescription": _cohort_only(),
        "APRDRGCode": _cohort_only(),
        "APRDRGDescription": _cohort_only(),
        "APRMDCCode": _category(
            sensitivity=SensitivityLevel.HIGH,
            cardinality=CardinalityClass.MEDIUM,
        ),
        "APRMDCDescription": _category(
            sensitivity=SensitivityLevel.HIGH,
            cardinality=CardinalityClass.MEDIUM,
        ),
        "APRSeverityOfIllnessCode": _category(
            sensitivity=SensitivityLevel.HIGH
        ),
        "APRSeverityOfIllnessDescription": _category(
            sensitivity=SensitivityLevel.HIGH
        ),
        "APRRiskOfMortality": _category(sensitivity=SensitivityLevel.HIGH),
        "APRMedicalSurgicalDescription": _category(
            sensitivity=SensitivityLevel.HIGH
        ),
        "PaymentTypology1": _category(cardinality=CardinalityClass.MEDIUM),
        "PaymentTypology2": _category(cardinality=CardinalityClass.MEDIUM),
        "PaymentTypology3": _category(cardinality=CardinalityClass.MEDIUM),
        "BirthWeight": _blocked(
            sensitivity=SensitivityLevel.HIGH,
            cardinality=CardinalityClass.CONTINUOUS,
        ),
        "EmergencyDepartmentIndicator": _category(),
        "TotalCharges": _numeric(sensitivity=SensitivityLevel.HIGH),
        "TotalCosts": _numeric(sensitivity=SensitivityLevel.HIGH),
    }
)


def governed_field_capability(table: str, field: str) -> FieldCapability:
    """Return a governed capability without exposing request values."""

    if table != "inpatient":
        raise QueryValidationError("受治理分析不支持该数据表")
    try:
        return INPATIENT_FIELD_CAPABILITIES[field]
    except KeyError as exc:
        raise QueryValidationError("受治理分析不支持该字段") from exc


def require_governed_distinct_field(table: str, field: str) -> None:
    """Require a low/medium-cardinality categorical distinct field."""

    capability = governed_field_capability(table, field)
    if not capability.governed_distinct:
        raise QueryValidationError(
            f"受治理 distinct 不允许字段 {field!r}"
        )


def require_governed_query(spec: QuerySpec) -> None:
    """Enforce governed dimensions and aggregate capabilities."""

    dimensions = set(spec.select) | set(spec.group_by)
    if len(dimensions) > MAX_GOVERNED_DIMENSIONS:
        raise QueryValidationError(
            f"受治理分析最多允许 {MAX_GOVERNED_DIMENSIONS} 个分组维度"
        )
    if len(spec.filters) > MAX_GOVERNED_FILTERS:
        raise QueryValidationError(
            f"受治理分析最多允许 {MAX_GOVERNED_FILTERS} 个筛选条件"
        )

    for field in dimensions:
        capability = governed_field_capability(spec.table, field)
        if not capability.governed_dimension:
            raise QueryValidationError(
                f"受治理分析不允许按字段 {field!r} 分组或选择"
            )

    for metric in spec.metrics:
        if metric.agg in {Aggregation.MIN, Aggregation.MAX}:
            raise QueryValidationError("受治理分析不允许使用 min/max 聚合")
        if metric.field == "*":
            continue
        capability = governed_field_capability(spec.table, metric.field)
        if metric.agg not in capability.governed_aggregations:
            raise QueryValidationError(
                f"受治理分析不允许对字段 {metric.field!r} "
                f"使用 {metric.agg.value!r} 聚合"
            )

    for filter_spec in spec.filters:
        capability = governed_field_capability(spec.table, filter_spec.field)
        if filter_spec.op not in capability.governed_filter_ops:
            raise QueryValidationError(
                f"受治理分析不允许对字段 {filter_spec.field!r} "
                f"使用 {filter_spec.op.value!r} 筛选"
            )
