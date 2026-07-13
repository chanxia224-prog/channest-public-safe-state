#!/usr/bin/env python3
"""Sync safe state from ChanNest Worker to GitHub Pages."""

import json, hashlib, os, sys, secrets
from datetime import datetime, timezone
import requests

SOURCES = ["https://channest.cloud/state_public", "https://channest-api.channest.workers.dev/state_public"]
DOCS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs")
TIMEOUT = 20

WMO_CODES = {
    0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
}
SENSITIVE = {"latitude", "longitude", "token", "secret", "capability", "authorization", "cookie", "address", "api_key"}

def fetch():
    for src in SOURCES:
        try:
            resp = requests.get(src, timeout=TIMEOUT, headers={"Cache-Control": "no-store"})
            if resp.status_code == 200:
                return resp.json(), src
        except Exception as e:
            print(f"  FAIL {src}: {e}")
    return None, None

def check_sensitive(obj, path=""):
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in SENSITIVE:
                found.append(f"{path}.{k}")
            found.extend(check_sensitive(v, f"{path}.{k}"))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found.extend(check_sensitive(v, f"{path}[{i}]"))
    return found

def main():
    os.makedirs(DOCS_DIR, exist_ok=True)
    gen_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z"
    bridge_ver = secrets.token_hex(8)
    probe_id = secrets.token_hex(16)
    nonce = secrets.token_hex(16)
    errors = []

    data, src = fetch()
    if not data:
        print("Fetch failed, keeping existing state.")
        return 1

    st = data.get("state", {})
    loc = st.get("location", {})
    wea = st.get("weather", {})
    rf = data.get("resource_freshness", {})
    loc_fresh = rf.get("location", {}) if isinstance(rf, dict) else {}
    wea_fresh = rf.get("weather", {}) if isinstance(rf, dict) else {}

    wcode = wea.get("weather_code", 0)
    safe = {
        "city": loc.get("city", ""),
        "district": loc.get("district", ""),
        "weather_condition": WMO_CODES.get(wcode, f"Unknown ({wcode})"),
        "temperature_c": wea.get("temperature_c"),
        "feels_like_c": wea.get("feels_like_c"),
        "humidity": wea.get("humidity"),
        "location_source_updated_at": loc_fresh.get("latest_source_item_at", ""),
        "weather_source_updated_at": wea_fresh.get("latest_source_item_at", ""),
        "location_freshness_status": loc_fresh.get("content_freshness_status", "unknown"),
        "weather_freshness_status": wea_fresh.get("content_freshness_status", "unknown"),
        "last_attempt_at": data.get("phone_last_upload_attempt_at", ""),
        "last_successful_push_at": data.get("phone_last_upload_success_at", ""),
        "last_http_code": 200,
        "last_error_code": data.get("phone_last_error"),
        "generated_at": gen_at,
        "bridge_version": bridge_ver,
    }

    # Validate
    if not safe["city"] or not safe["district"]:
        errors.append("city or district empty")
    if safe["location_freshness_status"] == "fresh" and not safe["location_source_updated_at"]:
        errors.append("location marked fresh but no source_updated_at")

    # Sensitive check
    sens = check_sensitive(safe)
    if sens:
        errors.append(f"sensitive fields: {sens}")

    # Compute hash
    payload = {k: safe[k] for k in sorted(safe) if k not in ("payload_hash", "generated_at", "bridge_version")}
    safe["payload_hash"] = hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()[:16]

    if errors:
        print(f"Validation errors: {errors}")
        # Don't overwrite existing state on validation failure
        old_state = os.path.join(DOCS_DIR, "state.json")
        if not os.path.exists(old_state):
            safe["last_error_code"] = "; ".join(errors)
            safe["last_http_code"] = 422
        else:
            print("Keeping previous valid state.")
            return 1

    # Write state.json
    state_json = json.dumps(safe, ensure_ascii=False, indent=2)
    with open(os.path.join(DOCS_DIR, "state.json"), "w", encoding="utf-8") as f:
        f.write(state_json)

    # Write probe.json
    probe = {
        "probe_id": probe_id,
        "nonce": nonce,
        "generated_at": gen_at,
        "bridge_version": bridge_ver,
        "state_payload_hash": hashlib.sha256(state_json.encode()).hexdigest()[:16],
        "instruction": "请迟复述 nonce，并读取 state.json 中的 city、district、weather_condition、temperature_c、两个 source_updated_at 和两个 freshness"
    }
    with open(os.path.join(DOCS_DIR, "probe.json"), "w", encoding="utf-8") as f:
        json.dump(probe, f, ensure_ascii=False, indent=2)

    print(f"Sync OK from {src}")
    print(f"  city={safe['city']}, district={safe['district']}")
    print(f"  weather={safe['weather_condition']}, temp={safe['temperature_c']}C")
    print(f"  loc_fresh={safe['location_freshness_status']}, wea_fresh={safe['weather_freshness_status']}")
    print(f"  probe_id={probe_id}, nonce={nonce}")
    return 0

if __name__ == "__main__":
    sys.exit(main())