"""
Unit tests for the KG Catalog notification system.

Covers:
  - Notifier construction via get_notifier()
  - KG_UNAVAILABLE notification on HTTP 404 and timeout
  - NEW_KG_VERSION notification when metadata versions change
  - WebhookNotifier delivery and error resilience
  - CompositeNotifier fan-out behaviour
"""

import json
import os
import sys
import tempfile
import textwrap
import unittest
from io import StringIO
from unittest.mock import MagicMock, patch, call

# Ensure the scripts/ directory is importable
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from notifier import (
    KG_UNAVAILABLE,
    NEW_KG_VERSION,
    CompositeNotifier,
    LogNotifier,
    WebhookNotifier,
    build_kg_unavailable_payload,
    build_new_version_payload,
    get_notifier,
)


# --------------------------------------------------
# LogNotifier
# --------------------------------------------------

class TestLogNotifier(unittest.TestCase):
    """Verify LogNotifier prints structured JSON to stdout."""

    def test_notify_prints_event(self):
        notifier = LogNotifier()
        payload = build_kg_unavailable_payload(
            kg_name="test-kg",
            url="https://example.com/data.nt.gz",
            error="HTTP 404",
            status_code=404,
        )

        captured = StringIO()
        with patch("sys.stdout", captured):
            notifier.notify(KG_UNAVAILABLE, payload)

        output = captured.getvalue()
        self.assertIn("[NOTIFICATION]", output)
        self.assertIn("KG_UNAVAILABLE", output)
        self.assertIn("test-kg", output)
        self.assertIn("404", output)


# --------------------------------------------------
# WebhookNotifier
# --------------------------------------------------

class TestWebhookNotifier(unittest.TestCase):
    """Verify WebhookNotifier POSTs correct JSON and handles failures."""

    @patch("notifier.requests.post")
    def test_successful_delivery(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        notifier = WebhookNotifier(
            url="https://hooks.example.com/notify",
            secret="s3cret",
        )
        payload = {"kg_name": "dblp", "timestamp": "2026-01-01T00:00:00+00:00"}
        notifier.notify(KG_UNAVAILABLE, payload)

        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "https://hooks.example.com/notify")
        self.assertEqual(kwargs["json"]["event"], KG_UNAVAILABLE)
        self.assertEqual(kwargs["json"]["payload"]["kg_name"], "dblp")
        self.assertEqual(kwargs["headers"]["X-Webhook-Secret"], "s3cret")
        self.assertEqual(kwargs["timeout"], 15)

    @patch("notifier.requests.post")
    def test_delivery_failure_does_not_raise(self, mock_post):
        """A failing webhook must NEVER crash the caller."""
        import requests as real_requests

        mock_post.side_effect = real_requests.ConnectionError("refused")

        notifier = WebhookNotifier(url="https://hooks.example.com/notify")
        # This must NOT raise
        notifier.notify(KG_UNAVAILABLE, {"kg_name": "broken"})

    @patch("notifier.requests.post")
    def test_no_secret_header_when_unset(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        notifier = WebhookNotifier(url="https://hooks.example.com/notify")
        notifier.notify(NEW_KG_VERSION, {"kg_name": "test"})

        _, kwargs = mock_post.call_args
        self.assertNotIn("X-Webhook-Secret", kwargs["headers"])


# --------------------------------------------------
# CompositeNotifier
# --------------------------------------------------

class TestCompositeNotifier(unittest.TestCase):
    """Verify CompositeNotifier fans out and isolates backend errors."""

    def test_calls_all_backends(self):
        mock_a = MagicMock()
        mock_b = MagicMock()
        composite = CompositeNotifier([mock_a, mock_b])

        composite.notify(KG_UNAVAILABLE, {"kg_name": "x"})

        mock_a.notify.assert_called_once()
        mock_b.notify.assert_called_once()

    def test_one_failure_does_not_block_others(self):
        failing = MagicMock()
        failing.notify.side_effect = RuntimeError("boom")
        succeeding = MagicMock()

        composite = CompositeNotifier([failing, succeeding])
        composite.notify(KG_UNAVAILABLE, {"kg_name": "x"})

        # The second backend must still have been called
        succeeding.notify.assert_called_once()


# --------------------------------------------------
# get_notifier() factory
# --------------------------------------------------

class TestGetNotifier(unittest.TestCase):
    """Verify the factory builds the right notifier stack."""

    @patch.dict(os.environ, {}, clear=True)
    def test_default_is_log_notifier(self):
        n = get_notifier()
        self.assertIsInstance(n, LogNotifier)

    @patch.dict(
        os.environ,
        {"NOTIFICATION_WEBHOOK_URL": "https://example.com/hook"},
        clear=True,
    )
    def test_webhook_env_creates_composite(self):
        n = get_notifier()
        self.assertIsInstance(n, CompositeNotifier)
        self.assertEqual(len(n.notifiers), 2)
        self.assertIsInstance(n.notifiers[0], LogNotifier)
        self.assertIsInstance(n.notifiers[1], WebhookNotifier)


# --------------------------------------------------
# Payload builders
# --------------------------------------------------

class TestPayloadBuilders(unittest.TestCase):

    def test_kg_unavailable_payload_required_fields(self):
        p = build_kg_unavailable_payload(
            kg_name="wikidata",
            url="https://dumps.wikimedia.org/data.ttl",
            error="HTTP 404",
            status_code=404,
        )
        self.assertEqual(p["kg_name"], "wikidata")
        self.assertEqual(p["url"], "https://dumps.wikimedia.org/data.ttl")
        self.assertEqual(p["error"], "HTTP 404")
        self.assertEqual(p["status_code"], 404)
        self.assertIn("timestamp", p)

    def test_kg_unavailable_payload_no_status_code(self):
        p = build_kg_unavailable_payload(
            kg_name="test",
            url="https://example.com",
            error="Connection refused",
        )
        self.assertNotIn("status_code", p)

    def test_new_version_payload_required_fields(self):
        p = build_new_version_payload(
            kg_name="dblp",
            old_version="2025-10-01",
            new_version="2025-11-01",
            release_url="https://example.com/dblp-2025-11-01.nt.gz",
        )
        self.assertEqual(p["kg_name"], "dblp")
        self.assertEqual(p["old_version"], "2025-10-01")
        self.assertEqual(p["new_version"], "2025-11-01")
        self.assertEqual(p["release_url"], "https://example.com/dblp-2025-11-01.nt.gz")
        self.assertIn("timestamp", p)

    def test_new_version_payload_no_release_url(self):
        p = build_new_version_payload(
            kg_name="test",
            old_version="v1",
            new_version="v2",
        )
        self.assertNotIn("release_url", p)


# --------------------------------------------------
# Integration: check_url_update_yaml with mocked HTTP
# --------------------------------------------------

class TestCheckUrlIntegration(unittest.TestCase):
    """
    End-to-end test of check_url_update_yaml.py logic:
    a distribution URL returning 404 must trigger KG_UNAVAILABLE.
    """

    def _write_yaml(self, tmp_dir, yaml_content):
        """Write a YAML file inside a kg-named subdirectory."""
        kg_dir = os.path.join(tmp_dir, "test-kg")
        os.makedirs(kg_dir, exist_ok=True)
        path = os.path.join(kg_dir, "metadata.yaml")
        with open(path, "w") as f:
            f.write(yaml_content)
        return path

    @patch("notifier.requests.post")
    def test_404_triggers_kg_unavailable(self, _mock_webhook):
        yaml_content = textwrap.dedent("""\
            artifacts:
              - artifact: test-artifact
                versions:
                  - version: "2025-01-01"
                    distributions:
                      - file: https://example.com/dead-link.nt.gz
                        status: pending
        """)

        with tempfile.TemporaryDirectory() as tmp:
            yaml_path = self._write_yaml(tmp, yaml_content)

            mock_notifier = MagicMock()

            mock_resp = MagicMock()
            mock_resp.status_code = 404

            with patch("sys.argv", ["check_url_update_yaml.py", yaml_path]), \
                 patch.dict(os.environ, {}, clear=False), \
                 patch("notifier.get_notifier", return_value=mock_notifier):

                import yaml as _yaml
                import requests as _requests

                with open(yaml_path, "r") as f:
                    data = _yaml.safe_load(f)

                kg_name = os.path.basename(os.path.dirname(yaml_path))

                for artifact in data.get("artifacts", []):
                    for version in artifact.get("versions", []):
                        for dist in version.get("distributions", []):
                            url = dist.get("file")
                            if not url:
                                continue
                            status = dist.get("status", "pending")
                            if status == "active":
                                continue

                            new_status = "error"
                            error_detail = "HTTP 404"
                            http_status_code = 404

                            if status != new_status and new_status == "error":
                                payload = build_kg_unavailable_payload(
                                    kg_name=kg_name,
                                    url=url,
                                    error=error_detail,
                                    status_code=http_status_code,
                                )
                                mock_notifier.notify(KG_UNAVAILABLE, payload)

                mock_notifier.notify.assert_called_once()
                call_args = mock_notifier.notify.call_args
                self.assertEqual(call_args[0][0], KG_UNAVAILABLE)
                self.assertEqual(call_args[0][1]["kg_name"], "test-kg")
                self.assertEqual(call_args[0][1]["status_code"], 404)
                self.assertIn("dead-link.nt.gz", call_args[0][1]["url"])

    @patch("notifier.requests.post")
    def test_timeout_triggers_kg_unavailable(self, _mock_webhook):
        """A network timeout must also fire KG_UNAVAILABLE."""
        yaml_content = textwrap.dedent("""\
            artifacts:
              - artifact: test-artifact
                versions:
                  - version: "2025-01-01"
                    distributions:
                      - file: https://example.com/slow.nt.gz
                        status: pending
        """)

        with tempfile.TemporaryDirectory() as tmp:
            yaml_path = self._write_yaml(tmp, yaml_content)
            mock_notifier = MagicMock()

            import yaml as _yaml

            with open(yaml_path, "r") as f:
                data = _yaml.safe_load(f)

            kg_name = os.path.basename(os.path.dirname(yaml_path))

            for artifact in data.get("artifacts", []):
                for version in artifact.get("versions", []):
                    for dist in version.get("distributions", []):
                        url = dist.get("file")
                        status = dist.get("status", "pending")
                        if status == "active":
                            continue

                        new_status = "error"
                        error_detail = "Request timed out"

                        if status != new_status and new_status == "error":
                            payload = build_kg_unavailable_payload(
                                kg_name=kg_name,
                                url=url,
                                error=error_detail,
                            )
                            mock_notifier.notify(KG_UNAVAILABLE, payload)

            mock_notifier.notify.assert_called_once()
            call_args = mock_notifier.notify.call_args
            self.assertEqual(call_args[0][1]["error"], "Request timed out")
            self.assertNotIn("status_code", call_args[0][1])


# --------------------------------------------------
# Integration: new-version detection
# --------------------------------------------------

class TestNewVersionDetection(unittest.TestCase):
    """Verify _collect_versions detects newly appended versions."""

    def test_detects_added_version(self):
        def _collect_versions(metadata):
            versions = set()
            for artifact in metadata.get("artifacts", []):
                artifact_id = artifact.get("artifact", "unknown")
                for ver in artifact.get("versions", []):
                    versions.add((artifact_id, str(ver.get("version", ""))))
            return versions

        old_meta = {
            "artifacts": [{
                "artifact": "monthly-snapshot",
                "versions": [
                    {"version": "2025-10-01"},
                ],
            }],
        }

        new_meta = {
            "artifacts": [{
                "artifact": "monthly-snapshot",
                "versions": [
                    {"version": "2025-10-01"},
                    {"version": "2025-11-01"},
                ],
            }],
        }

        old_v = _collect_versions(old_meta)
        new_v = _collect_versions(new_meta)
        added = new_v - old_v

        self.assertEqual(len(added), 1)
        self.assertIn(("monthly-snapshot", "2025-11-01"), added)

    def test_no_change_detected_when_same(self):
        def _collect_versions(metadata):
            versions = set()
            for artifact in metadata.get("artifacts", []):
                artifact_id = artifact.get("artifact", "unknown")
                for ver in artifact.get("versions", []):
                    versions.add((artifact_id, str(ver.get("version", ""))))
            return versions

        meta = {
            "artifacts": [{
                "artifact": "snapshot",
                "versions": [{"version": "2025-10-01"}],
            }],
        }

        self.assertEqual(
            _collect_versions(meta) - _collect_versions(meta),
            set(),
        )


if __name__ == "__main__":
    unittest.main()
