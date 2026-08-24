from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
PRIVACY_RESERVED_ALIAS_PREFIX = "__privacy_"
PRIVACY_GROUP_COUNT_ALIAS = f"{PRIVACY_RESERVED_ALIAS_PREFIX}group_count"


class FilterOperator(str, Enum):
    EQ = "="
    NE = "!="
    GT = ">"
    GTE = ">="
    LT = "<"
    LTE = "<="
    IN = "in"
    NOT_IN = "not_in"
    BETWEEN = "between"
    LIKE = "like"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"


class Aggregation(str, Enum):
    COUNT = "count"
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"


class SortDirection(str, Enum):
    ASC = "asc"
    DESC = "desc"


class FilterSpec(BaseModel):
    field: str
    op: FilterOperator
    value: Any | None = None

    model_config = ConfigDict(extra="forbid")


class MetricSpec(BaseModel):
    field: str
    agg: Aggregation
    alias: str

    model_config = ConfigDict(extra="forbid")


class OrderBySpec(BaseModel):
    field: str
    direction: SortDirection = SortDirection.ASC

    model_config = ConfigDict(extra="forbid")


class QuerySpec(BaseModel):
    table: str
    select: list[str] = Field(default_factory=list)
    filters: list[FilterSpec] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    metrics: list[MetricSpec] = Field(default_factory=list)
    order_by: list[OrderBySpec] = Field(default_factory=list)
    limit: int = DEFAULT_LIMIT

    model_config = ConfigDict(extra="forbid")

    @field_validator("limit")
    @classmethod
    def validate_limit(cls, value: int) -> int:
        if value < 1:
            raise ValueError("limit must be greater than 0")
        if value > MAX_LIMIT:
            raise ValueError(f"limit must be less than or equal to {MAX_LIMIT}")
        return value
