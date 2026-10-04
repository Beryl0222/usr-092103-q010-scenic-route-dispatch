"""校验领域事件公共字段（公共信封）。

本模块只校验跨事件类型稳定的公共约定；各事件类型的 payload 语义由
src/dispatch 目录内的领域代码负责。事件类型与聚合类型的允许集合与
contracts/domain.schema.json 保持一致，改动时两边必须同步。
"""

REQUIRED = ("event_id", "event_type", "aggregate_type", "aggregate_id", "occurred_at", "version", "summary")

EVENT_TYPES = (
    "ROUTE_PUBLISHED",
    "CAPACITY_RESERVED",
    "RISK_CLEARED",
    "ROUTE_REROUTED",
    "INCIDENT_REVIEWED",
    "FACILITY_STATUS_CHANGED",
    "WEATHER_RISK_ISSUED",
    "WEATHER_RISK_CLEARED",
    "ENTRY_SLOT_OPENED",
    "VISITOR_CAPABILITY_DECLARED",
    "VISITOR_CONSENT_UPDATED",
    "PARTY_REGISTERED",
    "PARTY_SPLIT",
    "RESERVATION_PLACED",
    "RESERVATION_CONFIRMED",
    "RESERVATION_TIMED_OUT",
    "RESERVATION_RELEASED",
    "SEGMENT_USED",
    "QUALIFICATION_VERIFIED",
    "MANUAL_APPROVAL_DECIDED",
    "EVACUATION_ORDERED",
    "EVACUATION_STOOD_DOWN",
    "DISPATCH_NOTE_ISSUED",
)

AGGREGATE_TYPES = (
    "route_revision",
    "capacity_window",
    "visitor_party",
    "dispatch_decision",
    "facility",
    "weather_advisory",
    "entry_slot",
    "visitor",
    "reservation",
    "qualification",
    "evacuation",
)


def validate_event(record: dict) -> list[str]:
    """返回错误信息列表；空列表表示通过公共信封校验。"""
    errors = [f"缺少字段：{name}" for name in REQUIRED if name not in record]
    if "version" in record and (not isinstance(record["version"], int) or record["version"] < 1):
        errors.append("version 必须是正整数")
    if "event_type" in record and record["event_type"] not in EVENT_TYPES:
        errors.append(f"未知事件类型：{record['event_type']}")
    if "aggregate_type" in record and record["aggregate_type"] not in AGGREGATE_TYPES:
        errors.append(f"未知聚合类型：{record['aggregate_type']}")
    if "payload" in record and not isinstance(record["payload"], dict):
        errors.append("payload 必须是对象")
    return errors
