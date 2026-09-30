#!/usr/bin/env python3
import os
import subprocess
import yaml
from datetime import datetime

from notifier import (
    get_notifier,
    NEW_KG_VERSION,
    build_new_version_payload,
)

# Path to the folder containing all KG folders
KGS_ROOT = os.path.join(os.path.dirname(__file__), "..", "knowledge-graphs")

notifier = get_notifier()


def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")


def _collect_versions(metadata):
    """Return a set of all (artifact_id, version) tuples in the metadata."""
    versions = set()
    for artifact in metadata.get("artifacts", []):
        artifact_id = artifact.get("artifact", "unknown")
        for ver in artifact.get("versions", []):
            versions.add((artifact_id, str(ver.get("version", ""))))
    return versions


def _latest_version_str(metadata):
    """Return the version string of the last entry in the first artifact."""
    artifacts = metadata.get("artifacts", [])
    if not artifacts:
        return None
    versions = artifacts[0].get("versions", [])
    if not versions:
        return None
    return str(versions[-1].get("version", ""))


def run_daily_check():
    if not os.path.isdir(KGS_ROOT):
        log(f"Knowledge graphs root folder not found: {KGS_ROOT}")
        return

    for kg_name in sorted(os.listdir(KGS_ROOT)):
        kg_path = os.path.join(KGS_ROOT, kg_name)
        if not os.path.isdir(kg_path):
            continue

        metadata_file = os.path.join(kg_path, "metadata.yaml")
        if not os.path.isfile(metadata_file):
            log(f"No metadata.yaml found in {kg_name}, skipping.")
            continue

        try:
            with open(metadata_file, "r") as f:
                metadata = yaml.safe_load(f)
        except Exception as e:
            log(f"Error reading {metadata_file}: {e}")
            continue

        script_name = metadata.get("check-new-release")
        if not script_name:
            log(f"No 'check-new-release' script for {kg_name}, skipping.")
            continue

        script_path = os.path.join(kg_path, script_name)
        if not os.path.isfile(script_path):
            log(f"Referenced script not found: {script_path}, skipping.")
            continue

        old_versions = _collect_versions(metadata)
        old_latest = _latest_version_str(metadata)

        log(f"Running {script_name} for {kg_name}...")
        try:
            subprocess.run(["python3", script_path], check=True)
            log(f"Finished {script_name} for {kg_name}.")
        except subprocess.CalledProcessError as e:
            log(f"Error running {script_path}: {e}")
            continue

        try:
            with open(metadata_file, "r") as f:
                updated_metadata = yaml.safe_load(f)
        except Exception as e:
            log(f"Error re-reading {metadata_file}: {e}")
            continue

        new_versions = _collect_versions(updated_metadata)
        added = new_versions - old_versions

        if added:
            new_latest = _latest_version_str(updated_metadata)
            release_url = None
            for artifact in updated_metadata.get("artifacts", []):
                for ver in artifact.get("versions", []):
                    key = (artifact.get("artifact", ""), str(ver.get("version", "")))
                    if key in added:
                        dists = ver.get("distributions", [])
                        if dists:
                            release_url = dists[0].get("file")
                        break

            payload = build_new_version_payload(
                kg_name=kg_name,
                old_version=old_latest or "none",
                new_version=new_latest or "unknown",
                release_url=release_url,
            )
            notifier.notify(NEW_KG_VERSION, payload)


if __name__ == "__main__":
    run_daily_check()
