#!/usr/bin/env python3
"""Sync safe state with proper UTC timestamp handling (no external deps)."""

import json, hashlib, os, sys, secrets, urllib.request
from datetime import datetime, timezone

SOURCES = ["https://channest.cloud/state_public", "https://channest-api.channest.workers.dev/state_public"]
TIMEOUT = 20
FRESH_THRESHOLD_S = 900

WMO_CODES = {
    0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
}

def parse_utc_epoch_ms(ts):
    if not ts or not isinstance(ts, str) or not ts.strip():
        return None
    ts = ts.strip()
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            return None
        utc_dt = dt.astimezone(timezone.utc)
        return int(utc_dt.timestamp() * 1000)
    except (ValueError, OverflowError):
        return None

def compute_freshness(source_updated_at):
    now = datetime.now(timezone.utc)
    now_utc_iso = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    now_ms = int(now.timestamp() * 1000)

    if not source_updated_at:
        return ("unknown", 0, now_utc_iso, "")

    source_ms = parse_utc_epoch_ms(source_updated_at)
    if source_ms is None:
        return ("read_failed", 0, now_utc_iso, source_updated_at)

    age_ms = now_ms - source_ms
    age_s = age_ms / 1000.0

    try:
        source_dt = datetime.fromtimestamp(source_ms / 1000.0, tz=timezone.utc)
        source_utc_iso = source_dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{source_dt.microsecond // 1000:03d}Z"
    except:
        source_utc_iso = source_updated_at

    if age_ms < 0:
        status = "read_failed"
    elif age_s <= FRESH_THRESHOLD_S:
        status = "fresh"
    else:
        status = "stale"

    return (status, age_s, now_utc_iso, source_utc_iso)

def fetch():
    for src in SOURCES:
        try:
            req = urllib.request.Request(src, method="GET")
            req.add_header("Cache-Control", "no-store")
            resp = urllib.request.urlopen(req, timeout=TIMEOUT)
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8")), src
        except Exception as e:
            print(f"  FAIL {src}: {e}")
    return None, None

def main():
    docs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs")
    os.makedirs(docs_dir, exist_ok=True)

    bridge_ver = secrets.token_hex(8)
    probe_id = secrets.token_hex(16)
    nonce = secrets.token_hex(16)

    data, src = fetch()
    if not data:
        print("Fetch failed, keeping existing state.")
        return 1

    st = data.get("state", {})
    loc = st.get("location", {})
    wea = st.get("weather", {})
    rf = data.get("resource_freshness", {})

    loc_source_at = (
        data.get("location_source_updated_at") or
        st.get("location_source_updated_at") or
        loc.get("source_updated_at") or
        (rf.get("location") or {}).get("latest_source_item_at") or
        data.get("phone_last_upload_success_at") or
        ""
    )

    wea_source_at = (
        data.get("weather_source_updated_at") or
        st.get("weather_source_updated_at") or
        wea.get("source_updated_at") or
        (rf.get("weather") or {}).get("latest_source_item_at") or
        data.get("phone_last_upload_success_at") or
        ""
    )

    loc_status, loc_age, now_utc, loc_utc = compute_freshness(loc_source_at)
    wea_status, wea_age, _, wea_utc = compute_freshness(wea_source_at)

    print(f"now_utc={now_utc}")
    print(f"LOC source_utc={loc_utc} age_s={loc_age:.0f} threshold_s={FRESH_THRESHOLD_S} status={loc_status}")
    print(f"WEA source_utc={wea_utc} age_s={wea_age:.0f} threshold_s={FRESH_THRESHOLD_S} status={wea_status}")

    wcode = wea.get("weather_code", 0)
    weather_condition = WMO_CODES.get(wcode, f"Unknown ({wcode})")

    gen_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z"

    safe_state = {
        "city": loc.get("city", ""),
        "district": loc.get("district", ""),
        "weather_condition": weather_condition,
        "temperature_c": wea.get("temperature_c"),
        "feels_like_c": wea.get("feels_like_c"),
        "humidity": wea.get("humidity"),
        "location_source_updated_at": loc_source_at,
        "weather_source_updated_at": wea_source_at,
        "location_freshness_status": loc_status,
        "weather_freshness_status": wea_status,
        "location_age_seconds": round(loc_age),
        "weather_age_seconds": round(wea_age),
        "freshness_threshold_seconds": FRESH_THRESHOLD_S,
        "now_utc": now_utc,
        "last_attempt_at": data.get("phone_last_upload_attempt_at", ""),
        "last_successful_push_at": data.get("phone_last_upload_success_at", ""),
        "last_http_code": 200,
        "last_error_code": data.get("phone_last_error"),
        "generated_at": gen_at,
        "bridge_version": bridge_ver,
    }

    payload = {k: safe_state[k] for k in sorted(safe_state) if k not in ("payload_hash",)}
    safe_state["payload_hash"] = hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()[:16]

    safe_json = json.dumps(safe_state, ensure_ascii=False, indent=2)
    with open(os.path.join(docs_dir, "state.json"), "w", encoding="utf-8") as f:
        f.write(safe_json)

    state_sha = hashlib.sha256(safe_json.encode()).hexdigest()

    probe = {
        "probe_id": probe_id,
        "nonce": nonce,
        "generated_at": gen_at,
        "bridge_version": bridge_ver,
        "state_payload_hash": state_sha[:16],
        "freshness_threshold_seconds": FRESH_THRESHOLD_S,
        "instruction": "请迟复述 nonce，读取 state.json 中的 city、district、weather_condition、temperature_c、两个 source_updated_at、两个 freshness、两个 age_seconds"
    }
    with open(os.path.join(docs_dir, "probe.json"), "w", encoding="utf-8") as f:
        json.dump(probe, f, ensure_ascii=False, indent=2)

    print(f"\nprobe_id={probe_id}")
    print(f"nonce={nonce}")
    print(f"bridge_ver={bridge_ver}")
    return 0

if __name__ == "__main__":
    sys.exit(main())