"""Scripted discovery sequence for the Midwest Federal Servicing Console.

Emits the exact tool calls a real LLM would emit to complete the goal
"Open a sub-account for member 12345". The sequence is annotated with
notes so the reviewer can see WHY each step exists.

This lets us demo the full pipeline without an API key. The seam between
this and cua.agent.llm is: both produce an ordered stream of tool-call
dicts of shape {"tool": name, "args": {...}}.
"""
from __future__ import annotations

from typing import Iterator


def script_open_sub_account(target_url: str, member_id: str = "12345") -> Iterator[dict]:
    """Yield tool-call dicts the driver executes in order."""
    yield {"tool": "navigate", "args": {"url": target_url + "/"}}
    yield {"tool": "get_snapshot", "args": {}}

    # Type member ID into the "Member ID" field. Primary = label_relative so
    # it survives the target app's untitled, un-idea'd input. Fallbacks add
    # role_name and CSS in case the anchor label changes.
    yield {
        "tool": "type",
        "args": {
            "primary": {"by": "label_relative", "anchor": "Member ID", "direction": "next_input"},
            "fallbacks": [
                {"by": "css", "selector": "input[name='q']"},
                {"by": "role_name", "role": "textbox"},
            ],
            "value": "{{member_id}}",
            "notes": "Enter member ID into lookup form.",
        },
    }

    # Submit lookup.
    yield {
        "tool": "click",
        "args": {
            "primary": {"by": "role_name", "role": "button", "name": "Search"},
            "fallbacks": [
                {"by": "css", "selector": "input[type=submit][value='Search']"},
            ],
            "notes": "Submit member lookup.",
            "risk": "safe",
        },
    }
    yield {"tool": "get_snapshot", "args": {}}

    # Open sub-account — mutating action → risky.
    yield {
        "tool": "click",
        "args": {
            "primary": {"by": "role_name", "role": "button", "name": "Open Sub-Account"},
            "fallbacks": [
                {"by": "css", "selector": "input[type=submit][value='Open Sub-Account']"},
            ],
            "notes": "Open a new sub-account. IRREVERSIBLE.",
            "risk": "risky",
        },
    }
    yield {"tool": "get_snapshot", "args": {}}

    # Extract new sub-account number from the confirmation page.
    yield {
        "tool": "mark_output",
        "args": {
            "name": "sub_account_id",
            "from": "url",
            "regex": r"/confirm/(?P<sub_account_id>SA\d+)",
        },
    }

    yield {
        "tool": "finish",
        "args": {
            "outcome_code": "SUCCESS",
            "checkpoint_url_pattern": r"^.*/member/\d+/confirm/SA\d+$",
        },
    }
