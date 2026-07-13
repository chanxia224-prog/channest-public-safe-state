import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.sync_safe_state import (
    FRESH_THRESHOLD_S,
    atomic_write_pair,
    build_documents,
    business_payload_hash,
    compute_freshness,
)


NOW = datetime(2026, 7, 13, 2, 0, 0, tzinfo=timezone.utc)


def ago(minutes=0, hours=0):
    return (NOW - timedelta(minutes=minutes, hours=hours)).isoformat()


def sample_data(location_at=None, weather_at=None):
    return {
        "state": {
            "location": {
                "city": "Chengdu",
                "district": "Jinniu District",
                "source_updated_at": location_at or ago(minutes=3),
            },
            "weather": {
                "temperature_c": 28.3,
                "humidity": 77,
                "weather_code": 51,
                "source_updated_at": weather_at or ago(minutes=3),
            },
        },
        "phone_last_upload_attempt_at": ago(minutes=2),
        "phone_last_upload_success_at": ago(minutes=3),
        "phone_last_error": None,
    }


class FreshnessTests(unittest.TestCase):
    def test_three_minutes_is_fresh(self):
        result = compute_freshness(ago(minutes=3), now=NOW)
        self.assertEqual((result["status"], result["age_seconds"]), ("fresh", 180))

    def test_fourteen_minutes_is_fresh(self):
        result = compute_freshness(ago(minutes=14), now=NOW)
        self.assertEqual(result["status"], "fresh")
        self.assertLessEqual(result["age_seconds"], FRESH_THRESHOLD_S)

    def test_sixteen_minutes_is_stale(self):
        result = compute_freshness(ago(minutes=16), now=NOW)
        self.assertEqual((result["status"], result["age_seconds"]), ("stale", 960))

    def test_eight_hours_is_stale(self):
        result = compute_freshness(ago(hours=8), now=NOW)
        self.assertEqual(result["status"], "stale")

    def test_future_time_is_read_failed(self):
        result = compute_freshness((NOW + timedelta(minutes=5)).isoformat(), now=NOW)
        self.assertEqual(result["status"], "read_failed")
        self.assertIsNone(result["age_seconds"])

    def test_missing_time_is_unknown(self):
        result = compute_freshness("", now=NOW)
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["age_seconds"])

    def test_location_and_weather_are_independent(self):
        state, _, _, _ = build_documents(
            sample_data(location_at=ago(minutes=3), weather_at=ago(hours=8)),
            now=NOW,
            bridge_version="test",
            probe_id="probe",
            nonce="nonce",
        )
        self.assertEqual(state["location_freshness_status"], "fresh")
        self.assertEqual(state["weather_freshness_status"], "stale")
        self.assertEqual(state["location_age_seconds"], 180)
        self.assertEqual(state["weather_age_seconds"], 28800)


class HashContractTests(unittest.TestCase):
    def test_runtime_freshness_fields_do_not_change_payload_hash(self):
        state, _, _, _ = build_documents(sample_data(), now=NOW, bridge_version="test")
        changed = copy.deepcopy(state)
        changed.update(
            location_age_seconds=999999,
            weather_age_seconds=999999,
            now_utc="2099-01-01T00:00:00.000Z",
            generated_at="2099-01-01T00:00:00.000Z",
            location_freshness_status="stale",
            weather_freshness_status="stale",
        )
        self.assertEqual(business_payload_hash(state), business_payload_hash(changed))

    def test_temperature_change_changes_payload_hash(self):
        state, _, _, _ = build_documents(sample_data(), now=NOW, bridge_version="test")
        changed = copy.deepcopy(state)
        changed["temperature_c"] = 29.1
        self.assertNotEqual(business_payload_hash(state), business_payload_hash(changed))

    def test_probe_hash_contract_and_atomic_pair(self):
        state, probe, state_bytes, probe_bytes = build_documents(
            sample_data(),
            now=NOW,
            bridge_version="bridge",
            probe_id="probe",
            nonce="nonce",
        )
        self.assertEqual(len(state["payload_hash"]), 64)
        self.assertEqual(probe["state_payload_hash"], state["payload_hash"])
        self.assertEqual(probe["state_file_sha256"], hashlib.sha256(state_bytes).hexdigest())
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            atomic_write_pair(target, state_bytes, probe_bytes)
            disk_state = json.loads((target / "state.json").read_text(encoding="utf-8"))
            disk_probe = json.loads((target / "probe.json").read_text(encoding="utf-8"))
            self.assertEqual(disk_state["payload_hash"], disk_probe["state_payload_hash"])
            self.assertFalse(list(target.glob(".*.json.*")))


if __name__ == "__main__":
    unittest.main()
