"""Notification sink tests.

We don't actually hit Slack — we point the sink at a local HTTP server
running in a thread and assert the shape of the POST body. This is what
matters: that our Slack-formatted payload is Slack-shaped, and that the
generic webhook posts the raw event+payload as JSON. Best-effort
semantics (network failure never surfaces) is covered by pointing the
sink at a bogus URL and asserting no exception.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import List

import pytest

from cua.escalation.notify import notify_all


class _Recorder(BaseHTTPRequestHandler):
    received: List[dict] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            self.received.append(json.loads(body))
        except Exception:
            self.received.append({"raw": body.decode("utf-8", "replace")})
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args, **kwargs):
        pass  # keep test output clean


@pytest.fixture()
def sink_server():
    _Recorder.received = []
    srv = HTTPServer(("127.0.0.1", 0), _Recorder)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield port
    srv.shutdown()


def test_generic_webhook_posts_event_and_payload(sink_server):
    verdicts = notify_all(
        "intervention_requested",
        {"capability": {"name": "cap", "version": "1.0.0", "target": "t"}, "step_id": "s1", "reason": "why"},
        bell=False,
        webhook_url=f"http://127.0.0.1:{sink_server}/hook",
    )
    assert verdicts["webhook"].startswith("ok"), verdicts
    assert _Recorder.received, "expected the sink to have received a POST"
    body = _Recorder.received[0]
    assert body["event"] == "intervention_requested"
    assert body["payload"]["step_id"] == "s1"


def test_slack_shape_has_attachments_with_fields(sink_server):
    verdicts = notify_all(
        "intervention_requested",
        {"capability": {"name": "cap", "version": "1.0.0", "target": "t"}, "step_id": "s3", "reason": "risky"},
        bell=False,
        slack_url=f"http://127.0.0.1:{sink_server}/slack",
    )
    assert verdicts["slack"].startswith("ok")
    body = _Recorder.received[0]
    assert "attachments" in body
    fields = body["attachments"][0]["fields"]
    field_titles = {f["title"] for f in fields}
    assert {"Capability", "Step", "Reason"} <= field_titles


def test_dead_url_does_not_raise():
    """Best-effort property: a broken sink must never surface as a real
    failure. Escalation notifications are observability, not control flow."""
    verdicts = notify_all(
        "intervention_requested",
        {"capability": {}, "step_id": "s1"},
        bell=False,
        webhook_url="http://127.0.0.1:1/dead",  # nothing listens
        timeout=0.5,
    )
    assert verdicts["webhook"].startswith("error") or verdicts["webhook"].startswith("http_error")
