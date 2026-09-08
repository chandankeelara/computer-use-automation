"""Anthropic Claude wrapper for the discovery loop.

Only used when --mock-llm is NOT passed. The wrapper turns Claude's
tool_use responses into the same {"tool", "args"} dict shape the mock
emits, so cua.agent.loop is model-agnostic.

Kept intentionally thin — production would add retries, streaming,
prompt-caching, and structured tracing.
"""
from __future__ import annotations

import os
from typing import Iterator

from .tools import TOOLS

MODEL = "claude-sonnet-4-6"


class LiveLLM:
    def __init__(self, goal: str, target_url: str):
        try:
            import anthropic  # noqa: F401
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "anthropic SDK not installed; run `pip install anthropic` or use --mock-llm"
            ) from e
        import anthropic

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set; use --mock-llm for demo")
        self._client = anthropic.Anthropic(api_key=api_key)
        self._goal = goal
        self._target_url = target_url
        self._history: list[dict] = []
        self._system = (
            "You are a computer-use agent recording a reusable capability. "
            "Use the provided tools to accomplish the goal. Prefer role+name locators; "
            "always include CSS or label_relative fallbacks. Mark irreversible steps as risky. "
            "When the goal is achieved, call finish with a checkpoint_url_pattern."
        )

    def stream(self) -> Iterator[dict]:  # pragma: no cover — integration path
        """Yield {tool, args} dicts until the model emits `finish`."""
        # The concrete tool-use loop is left as a small integration point:
        # each iteration sends self._history to messages.create with TOOLS,
        # extracts tool_use blocks, yields them, then appends tool_result
        # blocks the driver produced. Mock path exercises the same interface.
        raise NotImplementedError(
            "Live LLM path is scaffolded but not used in the offline demo. "
            "Use --mock-llm."
        )
