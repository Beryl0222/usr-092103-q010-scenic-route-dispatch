import json
import unittest
from pathlib import Path

from src.validator import AGGREGATE_TYPES, EVENT_TYPES, validate_event


class ContractTest(unittest.TestCase):
    def test_sample_matches_envelope(self) -> None:
        sample = json.loads(
            (Path(__file__).parents[1] / "data" / "sample.json").read_text(encoding="utf-8")
        )
        self.assertEqual(validate_event(sample), [])

    def test_schema_enums_match_validator(self) -> None:
        schema = json.loads(
            (Path(__file__).parents[1] / "contracts" / "domain.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(tuple(schema["properties"]["event_type"]["enum"]), EVENT_TYPES)
        self.assertEqual(tuple(schema["properties"]["aggregate_type"]["enum"]), AGGREGATE_TYPES)

    def test_reject_unknown_event_type(self) -> None:
        errors = validate_event({
            "event_id": "x", "event_type": "NOPE", "aggregate_type": "visitor",
            "aggregate_id": "v1", "occurred_at": "2026-10-04T08:00:00+08:00",
            "version": 1, "summary": "x",
        })
        self.assertTrue(any("未知事件类型" in e for e in errors))

    def test_payload_must_be_object(self) -> None:
        base = {
            "event_id": "x", "event_type": "ROUTE_PUBLISHED", "aggregate_type": "route_revision",
            "aggregate_id": "r1", "occurred_at": "2026-10-04T08:00:00+08:00",
            "version": 1, "summary": "x", "payload": "not-object",
        }
        self.assertTrue(any("payload" in e for e in validate_event(base)))


if __name__ == "__main__":
    unittest.main()
