#!/usr/bin/env python3
"""Build the public-safe ChanNest state and probe from the live Worker state."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


SOURCES = [
    "https://channest-api.channest.workers.dev/state_public_probe",
    "https://channest.cloud/state_public",
    "https://channest-api.channest.workers.dev/state_public",
]
TIMEOUT = 20
FRESH_THRESHOLD_S = 15 * 60
MAX_FUTURE_CLOCK_SKEW_S = 2 * 60

# Runtime freshness fields are deliberately excluded. This is the already
# established business-payload contract: changing age alone must not alter it.
PAYLOAD_HASH_FIELDS = (
    "city",
    "district",
    "weather_condition",
    "temperature_c",
    "feels_like_c",
    "humidity",
    "location_source_updated_at",
    "weather_source_updated_at",
    "last_attempt_at",
    "last_successful_push_at",
    "last_http_code",
    "last_error_code",
)

WMO_CODES = {
    0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
    80: "Slight rain showers", 81: "Moderate rain showers",
    82: "Violent rain showers", 95: "Thunderstorm",
    96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
}


def utc_iso(value: datetime) -> str:
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def parse_utc_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    clean = value.strip()
    if clean.endswith("Z"):
        clean = clean[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(clean)
    except (ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def compute_freshness(
    source_updated_at: object,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    read_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    result: dict[str, object] = {
        "status": "unknown",
        "age_seconds": None,
        "now_utc": utc_iso(read_at),
        "source_utc": "",
    }
    if not isinstance(source_updated_at, str) or not source_updated_at.strip():
        return result

    source = parse_utc_datetime(source_updated_at)
    if source is None:
        result.update(status="read_failed", source_utc=source_updated_at)
        return result

    result["source_utc"] = utc_iso(source)
    age_seconds = int((read_at - source).total_seconds() // 1)
    if age_seconds < -MAX_FUTURE_CLOCK_SKEW_S:
        result["status"] = "read_failed"
        return result

    age_seconds = max(0, age_seconds)
    result["age_seconds"] = age_seconds
    result["status"] = "fresh" if age_seconds <= FRESH_THRESHOLD_S else "stale"
    return result


def fetch() -> tuple[dict[str, object] | None, str | None]:
    for source in SOURCES:
        try:
            request = urllib.request.Request(source, method="GET")
            request.add_header("Cache-Control", "no-store, no-cache, max-age=0")
            request.add_header("Pragma", "no-cache")
            request.add_header("Accept", "application/json")
            request.add_header("User-Agent", "ChanNestSafeState/2.0")
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                if response.status == 200:
                    return json.loads(response.read().decode("utf-8")), source
        except Exception as error:  # network failures are reported, not hidden
            print(f"  FAIL {source}: {error}")
    return None, None


def business_payload_hash(state: dict[str, object]) -> str:
    payload = {field: state.get(field) for field in PAYLOAD_HASH_FIELDS}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def source_timestamp(data: dict[str, object], kind: str) -> str:
    state = data.get("state") if isinstance(data.get("state"), dict) else {}
    resource_freshness = (
        data.get("resource_freshness")
        if isinstance(data.get("resource_freshness"), dict)
        else {}
    )
    section = state.get(kind) if isinstance(state.get(kind), dict) else {}
    resource = (
        resource_freshness.get(kind)
        if isinstance(resource_freshness.get(kind), dict)
        else {}
    )
    candidates = (
        data.get(f"{kind}_source_updated_at"),
        state.get(f"{kind}_source_updated_at"),
        section.get("source_updated_at"),
        resource.get("latest_source_item_at"),
        data.get("phone_last_upload_success_at"),
    )
    return next((value.strip() for value in candidates if isinstance(value, str) and value.strip()), "")


def build_documents(
    data: dict[str, object],
    *,
    now: datetime | None = None,
    bridge_version: str | None = None,
    probe_id: str | None = None,
    nonce: str | None = None,
) -> tuple[dict[str, object], dict[str, object], bytes, bytes] | None:
    read_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    state = data.get("state") if isinstance(data.get("state"), dict) else {}
    location = state.get("location") if isinstance(state.get("location"), dict) else {}
    weather = state.get("weather") if isinstance(state.get("weather"), dict) else {}

    location_source = source_timestamp(data, "location")
    weather_source = source_timestamp(data, "weather")
    location_freshness = compute_freshness(location_source, now=read_at)
    weather_freshness = compute_freshness(weather_source, now=read_at)

    # Prefer the new nested structure; legacy flat fields are only a fallback.
    city = location.get("city") or data.get("city") or ""
    district = location.get("district") or data.get("district") or ""
    weather_code = weather.get("weather_code")
    if weather_code is None:
        weather_code = data.get("weather_code")
    weather_condition = WMO_CODES.get(weather_code, f"Unknown ({weather_code})")
    temperature_c = weather.get("temperature_c")
    if temperature_c is None:
        temperature_c = weather.get("temp") if weather.get("temp") is not None else data.get("temperature_c")
    feels_like_c = weather.get("feels_like_c")
    if feels_like_c is None:
        feels_like_c = data.get("feels_like_c")
    humidity = weather.get("humidity")
    if humidity is None:
        humidity = data.get("humidity")

    missing = [
        name
        for name, value in {
            "city": city,
            "district": district,
            "location_source_updated_at": location_source,
            "weather_source_updated_at": weather_source,
        }.items()
        if not value
    ]
    missing.extend(
        name
        for name, value in {
            "weather_code": weather_code,
            "temperature_c": temperature_c,
            "feels_like_c": feels_like_c,
            "humidity": humidity,
        }.items()
        if value is None
    )
    if missing:
        print(f"Aborting sync: missing required fields: {', '.join(missing)}")
        return None

    generated_at = utc_iso(read_at)
    bridge_version = bridge_version or secrets.token_hex(8)

    safe_state: dict[str, object] = {
        "city": city,
        "district": district,
        "weather_condition": weather_condition,
        "temperature_c": temperature_c,
        "feels_like_c": feels_like_c,
        "humidity": humidity,
        "location_source_updated_at": location_source,
        "weather_source_updated_at": weather_source,
        "location_freshness_status": location_freshness["status"],
        "weather_freshness_status": weather_freshness["status"],
        "location_age_seconds": location_freshness["age_seconds"],
        "weather_age_seconds": weather_freshness["age_seconds"],
        "freshness_threshold_seconds": FRESH_THRESHOLD_S,
        "now_utc": generated_at,
        "last_attempt_at": data.get("phone_last_upload_attempt_at", ""),
        "last_successful_push_at": data.get("phone_last_upload_success_at", ""),
        "last_http_code": 200,
        "last_error_code": data.get("phone_last_error"),
        "generated_at": generated_at,
        "bridge_version": bridge_version,
    }
    safe_state["payload_hash"] = business_payload_hash(safe_state)

    state_bytes = json.dumps(safe_state, ensure_ascii=False, indent=2).encode("utf-8")
    state_file_sha256 = hashlib.sha256(state_bytes).hexdigest()
    probe: dict[str, object] = {
        "probe_id": probe_id or secrets.token_hex(16),
        "nonce": nonce or secrets.token_hex(16),
        "generated_at": generated_at,
        "bridge_version": bridge_version,
        "state_payload_hash": safe_state["payload_hash"],
        "state_file_sha256": state_file_sha256,
        "freshness_threshold_seconds": FRESH_THRESHOLD_S,
        "instruction": (
            "请迟复述 nonce，并从 state.json 验证 payload_hash、两个 age_seconds、"
            "两个 freshness"
        ),
    }
    probe_bytes = json.dumps(probe, ensure_ascii=False, indent=2).encode("utf-8")
    return safe_state, probe, state_bytes, probe_bytes


def atomic_write_pair(
    docs_dir: Path,
    state_bytes: bytes,
    probe_bytes: bytes,
) -> None:
    docs_dir.mkdir(parents=True, exist_ok=True)
    temporary: list[Path] = []
    try:
        for name, content in (("state.json", state_bytes), ("probe.json", probe_bytes)):
            descriptor, temp_name = tempfile.mkstemp(prefix=f".{name}.", dir=docs_dir)
            temp_path = Path(temp_name)
            temporary.append(temp_path)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(temporary[0], docs_dir / "state.json")
        temporary.pop(0)
        os.replace(temporary[0], docs_dir / "probe.json")
        temporary.pop(0)
    finally:
        for path in temporary:
            path.unlink(missing_ok=True)


def main() -> int:
    docs_dir = Path(__file__).resolve().parent.parent / "docs"
    data, source = fetch()
    if not data:
        print("Fetch failed, keeping existing state.")
        return 1

    built = build_documents(data)
    if built is None:
        print("Build aborted, keeping existing state.")
        return 1
    state, probe, state_bytes, probe_bytes = built
    atomic_write_pair(docs_dir, state_bytes, probe_bytes)
    print(f"source={source}")
    print(f"now_utc={state['now_utc']}")
    print(
        "LOC "
        f"source={state['location_source_updated_at']} "
        f"age_s={state['location_age_seconds']} "
        f"status={state['location_freshness_status']}"
    )
    print(
        "WEA "
        f"source={state['weather_source_updated_at']} "
        f"age_s={state['weather_age_seconds']} "
        f"status={state['weather_freshness_status']}"
    )
    print(f"payload_hash={state['payload_hash']}")
    print(f"state_file_sha256={probe['state_file_sha256']}")
    print(f"probe_id={probe['probe_id']}")
    print(f"nonce={probe['nonce']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
