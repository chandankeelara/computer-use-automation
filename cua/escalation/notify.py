"""Notification sinks for escalation events.

Three sinks, each opt-in via policy.yaml:

  bell:    terminal bell (\x07). Free, works everywhere, catches a human
           who's watching the CLI.
  webhook: generic HTTP POST of the payload as JSON. The URL is the
           policy field `notify_webhook_url`. Use for a self-hosted
           incident router.
  slack:   Slack-formatted "attachment" block (also HTTP POST, but with
           a Slack-shaped body). URL from `notify_slack_webhook_url`.

Fires on:
  * intervention_requested — the engine escalated (three triggers a/b/c)
  * draft_needs_approval   — a caller tried to run a draft under
                              --allow-draft in a supervised context

All sinks are best-effort: notification failures never surface into the
replay result. They're purely observability. If a Slack outage were to
cascade into a replay failure that would be a much worse property than
the operator not hearing about the escalation.
"""
from __future__ import annotations

import json
import logging
import sys
import urllib.error
import urllib.request
from typing import Any, Optional

log = logging.getLogger(__name__)


def notify_all(
    event: str,
    payload: dict[str, Any],
    *,
    bell: bool = True,
    webhook_url: Optional[str] = None,
    slack_url: Optional[str] = None,
    timeout: float = 3.0,
) -> dict[str, Any]:
    """Fan out to enabled sinks. Return per-sink verdicts for evidence."""
    verdicts: dict[str, Any] = {}
    if bell:
        try:
            sys.stdout.write("\a")
            sys.stdout.flush()
            verdicts["bell"] = "ok"
        except Exception as e:  # pragma: no cover
            verdicts["bell"] = f"error: {e}"

    body_generic = json.dumps({"event": event, "payload": payload}).encode("utf-8")
    if webhook_url:
        verdicts["webhook"] = _post(webhook_url, body_generic, timeout=timeout)

    if slack_url:
        slack_body = json.dumps(_slack_shape(event, payload)).encode("utf-8")
        verdicts["slack"] = _post(slack_url, slack_body, timeout=timeout)

    return verdicts


def _post(url: str, body: bytes, *, timeout: float) -> str:
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return f"ok:{resp.status}"
    except urllib.error.HTTPError as e:
        return f"http_error:{e.code}"
    except Exception as e:  # network, timeout, dns, etc.
        return f"error:{type(e).__name__}"


def _slack_shape(event: str, payload: dict[str, Any]) -> dict[str, Any]:
    cap = payload.get("capability", {})
    return {
        "attachments": [
            {
                "color": "#a00" if event == "intervention_requested" else "#f0ad4e",
                "title": f"[cua] {event}",
                "fields": [
                    {"title": "Capability", "value": f"{cap.get('name')}@{cap.get('version')}/{cap.get('target')}", "short": False},
                    {"title": "Step", "value": str(payload.get("step_id", "?")), "short": True},
                    {"title": "URL", "value": str(payload.get("current_url", "?")), "short": True},
                    {"title": "Reason", "value": str(payload.get("reason", "?")), "short": False},
                ],
                "footer": "cua escalation",
            }
        ]
    }
