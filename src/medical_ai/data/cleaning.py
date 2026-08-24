from __future__ import annotations

from collections.abc import Mapping
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

# 原始 SPARCS 字段名到项目统一字段名的映射。
RAW_TO_CANONICAL: dict[str, str] = {
    "Hospital Service Area": "HospitalServiceArea",
    "Hospital County": "HospitalCounty",
    "Operating Certificate Number": "OperatingCertificateNumber",
    "Permanent Facility Id": "PermanentFacilityId",
    "Facility Name": "FacilityName",
    "Age Group": "AgeGroup",
    "Zip Code - 3 digits": "ZipCode3Digits",
    "Gender": "Gender",
    "Race": "Race",
    "Ethnicity": "Ethnicity",
    "Length of Stay": "LengthOfStay",
    "Type of Admission": "AdmissionType",
    "Patient Disposition": "PatientDisposition",
    "Discharge Year": "DischargeYear",
    "CCSR Diagnosis Code": "CCSRDiagnosisCode",
    "CCSR Diagnosis Description": "CCSRDiagnosisDescription",
    "CCSR Procedure Code": "CCSRProcedureCode",
    "CCSR Procedure Description": "CCSRProcedureDescription",
    "APR DRG Code": "APRDRGCode",
    "APR DRG Description": "APRDRGDescription",
    "APR MDC Code": "APRMDCCode",
    "APR MDC Description": "APRMDCDescription",
    "APR Severity of Illness Code": "APRSeverityOfIllnessCode",
    "APR Severity of Illness Description": "APRSeverityOfIllnessDescription",
    "APR Risk of Mortality": "APRRiskOfMortality",
    "APR Medical Surgical Description": "APRMedicalSurgicalDescription",
    "Payment Typology 1": "PaymentTypology1",
    "Payment Typology 2": "PaymentTypology2",
    "Payment Typology 3": "PaymentTypology3",
    "Birth Weight": "BirthWeight",
    "Emergency Department Indicator": "EmergencyDepartmentIndicator",
    "Total Charges": "TotalCharges",
    "Total Costs": "TotalCosts",
}

# 清洗后 CSV 和数据库表统一使用的字段顺序。
CANONICAL_COLUMNS: list[str] = [
    "HospitalServiceArea",
    "HospitalCounty",
    "OperatingCertificateNumber",
    "PermanentFacilityId",
    "FacilityName",
    "AgeGroup",
    "ZipCode3Digits",
    "Gender",
    "Race",
    "Ethnicity",
    "RaceEthnicity",
    "LengthOfStay",
    "AdmissionType",
    "PatientDisposition",
    "DischargeYear",
    "CCSRDiagnosisCode",
    "CCSRDiagnosisDescription",
    "CCSRProcedureCode",
    "CCSRProcedureDescription",
    "APRDRGCode",
    "APRDRGDescription",
    "APRMDCCode",
    "APRMDCDescription",
    "APRSeverityOfIllnessCode",
    "APRSeverityOfIllnessDescription",
    "APRRiskOfMortality",
    "APRMedicalSurgicalDescription",
    "PaymentTypology1",
    "PaymentTypology2",
    "PaymentTypology3",
    "BirthWeight",
    "EmergencyDepartmentIndicator",
    "TotalCharges",
    "TotalCosts",
]

# 需要转换为整数的字段。
INTEGER_COLUMNS = {
    "LengthOfStay",
    "DischargeYear",
    "APRDRGCode",
    "APRMDCCode",
    "APRSeverityOfIllnessCode",
    "BirthWeight",
}
# 需要转换为 Decimal，便于费用计算且避免浮点误差的字段。
DECIMAL_COLUMNS = {"TotalCharges", "TotalCosts"}
# 需要统计高频值的数据分类字段。
CATEGORICAL_PROFILE_COLUMNS = [
    "AgeGroup",
    "Gender",
    "Race",
    "Ethnicity",
    "AdmissionType",
    "PaymentTypology1",
    "EmergencyDepartmentIndicator",
    "APRRiskOfMortality",
    "DischargeYear",
]
# 需要统计最小值和最大值的数值字段。
NUMERIC_PROFILE_COLUMNS = [
    "LengthOfStay",
    "TotalCharges",
    "TotalCosts",
    "BirthWeight",
]

# 将原始年龄分组转换为适合程序和查询使用的规范值。
AGE_GROUP_MAP = {
    "0 to 17": "0to17",
    "18 to 29": "18to29",
    "30 to 49": "30to49",
    "50 to 69": "50to69",
    "70 or Older": "70orOlder",
}

GENDER_MAP = {
    "F": "Female",
    "M": "Male",
    "U": "Unknown",
}

ED_MAP = {
    "Y": "Yes",
    "N": "No",
}

# 原始数据中表示数值缺失的特殊文本。
NUMERIC_MISSING_VALUES = {"NA", "N/A", "NONE", "NULL", "UNKNOWN", "UNKN"}


@dataclass
class DataQualityProfile:
    # 保存清洗结果的总体质量统计，不保存完整明细，避免额外占用大量内存。
    row_count: int = 0
    null_counts: Counter[str] = field(default_factory=Counter)
    max_lengths: Counter[str] = field(default_factory=Counter)
    value_counts: dict[str, Counter[Any]] = field(
        default_factory=lambda: {column: Counter() for column in CATEGORICAL_PROFILE_COLUMNS}
    )
    numeric_min: dict[str, Decimal | int] = field(default_factory=dict)
    numeric_max: dict[str, Decimal | int] = field(default_factory=dict)
    invalid_rows: list[dict[str, Any]] = field(default_factory=list)

    def observe(self, row: Mapping[str, Any]) -> None:
        # 每处理一条有效记录，就更新空值、长度、分类分布和数值范围统计。
        self.row_count += 1
        for column in CANONICAL_COLUMNS:
            value = row.get(column)
            if value is None:
                self.null_counts[column] += 1
                continue
            if isinstance(value, str):
                self.max_lengths[column] = max(self.max_lengths[column], len(value))
            if column in self.value_counts:
                self.value_counts[column][value] += 1
            if column in NUMERIC_PROFILE_COLUMNS and isinstance(value, (Decimal, int)):
                current_min = self.numeric_min.get(column)
                current_max = self.numeric_max.get(column)
                self.numeric_min[column] = value if current_min is None or value < current_min else current_min
                self.numeric_max[column] = value if current_max is None or value > current_max else current_max

    def record_invalid_row(self, row_number: int, error: Exception) -> None:
        # 只保留无效行的行号和错误原因，便于后续定位问题。
        self.invalid_rows.append({"row_number": row_number, "error": str(error)})

    def to_dict(self, top_n: int = 20) -> dict[str, Any]:
        # 将 Decimal 等特殊类型转换为可写入 JSON 的普通值。
        return {
            "row_count": self.row_count,
            "null_counts": {column: self.null_counts.get(column, 0) for column in CANONICAL_COLUMNS},
            "max_lengths": {column: self.max_lengths.get(column, 0) for column in CANONICAL_COLUMNS},
            "top_values": {
                column: [
                    {"value": _profile_value(value), "count": count}
                    for value, count in counter.most_common(top_n)
                ]
                for column, counter in self.value_counts.items()
            },
            "numeric_min": {column: _profile_value(value) for column, value in self.numeric_min.items()},
            "numeric_max": {column: _profile_value(value) for column, value in self.numeric_max.items()},
            "invalid_row_count": len(self.invalid_rows),
            "invalid_rows_sample": self.invalid_rows[:50],
        }


def clean_sparcs_row(row: Mapping[str, str]) -> dict[str, Any]:
    # 清洗单条原始记录：统一字段名、处理空值、转换类型并生成派生字段。
    cleaned: dict[str, Any] = {}
    for raw_name, canonical_name in RAW_TO_CANONICAL.items():
        value = _blank_to_none(row.get(raw_name, ""))
        cleaned[canonical_name] = _clean_value(canonical_name, value)

    # 根据 Race 和 Ethnicity 生成便于分析的组合字段。
    race = cleaned.get("Race")
    ethnicity = cleaned.get("Ethnicity")
    cleaned["RaceEthnicity"] = _combine_race_ethnicity(race, ethnicity)
    return {column: cleaned.get(column) for column in CANONICAL_COLUMNS}


def clean_row_for_csv(row: Mapping[str, str]) -> dict[str, str]:
    # 对单条原始记录执行清洗，并直接转换为 CSV 可写入的字符串。
    cleaned = clean_sparcs_row(row)
    return format_cleaned_row_for_csv(cleaned)


def format_cleaned_row_for_csv(cleaned: Mapping[str, Any]) -> dict[str, str]:
    # 将 None 转为空字符串，将 Decimal 转为普通数字文本。
    return {key: _format_for_csv(value) for key, value in cleaned.items()}


def cast_cleaned_row(row: Mapping[str, str]) -> dict[str, Any]:
    # 读取已清洗 CSV 时恢复数据库需要的整数、Decimal 和 NULL 类型。
    casted: dict[str, Any] = {}
    for key in CANONICAL_COLUMNS:
        value = _blank_to_none(row.get(key, ""))
        if value is None:
            casted[key] = None
        elif key in INTEGER_COLUMNS:
            casted[key] = int(value)
        elif key in DECIMAL_COLUMNS:
            casted[key] = Decimal(str(value))
        else:
            casted[key] = str(value)
    return casted


def _clean_value(column: str, value: str | None) -> Any:
    # 根据字段类型选择对应的标准化或数值解析规则。
    if value is None:
        return None
    if column == "AgeGroup":
        return AGE_GROUP_MAP.get(value, value.replace(" ", ""))
    if column == "Gender":
        return GENDER_MAP.get(value, value)
    if column == "EmergencyDepartmentIndicator":
        return ED_MAP.get(value, value)
    if column in INTEGER_COLUMNS:
        return _parse_int(value)
    if column in DECIMAL_COLUMNS:
        return _parse_decimal(value)
    return value


def _parse_int(value: str) -> int | None:
    # 处理整数文本以及末尾带有加号的住院天数，如 "120 +"。
    value = value.strip()
    if not value or value.upper() in NUMERIC_MISSING_VALUES:
        return None
    if value.endswith("+"):
        value = value[:-1].strip()
    if not value:
        return None
    return int(value)


def _parse_decimal(value: str) -> Decimal | None:
    # 去除金额千位分隔符，并将无法解析的金额明确报告为异常。
    value = value.strip().replace(",", "")
    if not value or value.upper() in NUMERIC_MISSING_VALUES:
        return None
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid decimal value {value!r}") from exc


def _blank_to_none(value: str | None) -> str | None:
    # 统一清理首尾空格，并将空字符串转换为 None。
    if value is None:
        return None
    value = value.strip()
    return value or None


def _combine_race_ethnicity(race: Any, ethnicity: Any) -> str | None:
    # 拼接非空的种族和族裔值；两者都为空时返回 None。
    parts = [str(part) for part in (race, ethnicity) if part]
    return " | ".join(parts) if parts else None


def _format_for_csv(value: Any) -> str:
    # 将清洗后的 Python 值转换为 CSV 单元格文本。
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _profile_value(value: Any) -> Any:
    # JSON 不直接支持 Decimal，因此将其转换为字符串保存。
    if isinstance(value, Decimal):
        return str(value)
    return value
