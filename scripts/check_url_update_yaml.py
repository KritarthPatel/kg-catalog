import os
import yaml
import requests
import sys

from notifier import (
    get_notifier,
    KG_UNAVAILABLE,
    build_kg_unavailable_payload,
)

yaml_file = sys.argv[1]

_kg_name = os.path.basename(os.path.dirname(os.path.abspath(yaml_file)))

notifier = get_notifier()

# --- Step 1: Validate YAML syntax ---
try:
    with open(yaml_file, "r") as f:
        data = yaml.safe_load(f)
except yaml.YAMLError as e:
    print(f"❌ YAML format error in {yaml_file}: {e}")
    sys.exit(1)

if not data:
    print(f"No data loaded from {yaml_file}")
    sys.exit(0)

changed = False  # Track if any updates were made
publish_triggered = False  # Track if Databus publish should be set
REQUEST_HEADERS = {
    "User-Agent": (
        "kg-catalog-url-checker/1.0 "
        "(https://www.dbpedia.org/)"
    ),
    "Accept-Encoding": "gzip",
}

# --- Step 2: Traverse artifacts -> versions -> distributions ---
for artifact in data.get("artifacts", []):
    for version in artifact.get("versions", []):
        for dist in version.get("distributions", []):
            url = dist.get("file")
            if not url:
                continue

            status = dist.get("status", "pending")

            # Skip already active URLs
            if status == "active":
                print(f"✅ Skipping {url}: already active")
                continue

            error_detail = None
            http_status_code = None

            try:
                resp = requests.head(
                    url,
                    headers=REQUEST_HEADERS,
                    allow_redirects=True,
                    timeout=15,
                )
                http_status_code = resp.status_code
                new_status = "active" if resp.status_code == 200 else "error"
                if new_status == "error":
                    error_detail = f"HTTP {resp.status_code}"
            except requests.Timeout:
                new_status = "error"
                error_detail = "Request timed out"
            except requests.RequestException as exc:
                new_status = "error"
                error_detail = str(exc)

            if status != new_status:
                print(f"🔄 Updating {url}: {status} -> {new_status}")
                dist["status"] = new_status
                changed = True
                # Trigger Databus publish if URL became active
                if new_status == "active":
                    publish_triggered = True
                if new_status == "error":
                    payload = build_kg_unavailable_payload(
                        kg_name=_kg_name,
                        url=url,
                        error=error_detail or "Unknown error",
                        status_code=http_status_code,
                    )
                    notifier.notify(KG_UNAVAILABLE, payload)
            else:
                print(f"ℹ️ No change for {url} (still {status})")

# --- Step 3: Set databus-publish if any URL became active ---
if publish_triggered:
    data["databus-publish"] = True
    changed = True  # Mark changed so YAML is saved

# --- Step 4: Save only if something changed ---
if changed:
    with open(yaml_file, "w") as f:
        yaml.dump(data, f, sort_keys=False)
    print(f"💾 Updated {yaml_file} (databus-publish={data.get('databus-publish')})")
else:
    print(f"ℹ️ No changes needed for {yaml_file}")
