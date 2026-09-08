# Report

## Architecture

Four layers, each behind a small interface. **Surface** perceives + acts (`snapshot`, `execute`); `WebSurface` implements it via Playwright. Documented seams for `DesktopSurface` (UIA) and `VisionSurface` (screenshot-only) sit alongside it — they would implement the same two methods and reuse the rest of the stack unchanged. **Agent** runs a tool-use loop over that surface and records what happened; a `Recorder` turns tool calls into an `Artifact`. **Replay** executes an artifact deterministically against a Surface, resolving locators through the primary+fallbacks strategy and matching typed outcomes. **Safety / Evidence / Escalation** are cross-cutting: every action passes through `Policy.check`, every step logs a JSONL row + screenshot + AX dump, and any all-miss / unknown-page / risky-unapproved situation triggers `request_handoff`.

The perception layer is a serialized accessibility tree — role, name, value, hierarchy — built in-page from ARIA/HTML semantics. Screenshot capture is available on demand for a vision fallback path; the vision call is required to name a *DOM-anchored* descriptor (label text, CSS descriptor) that we resolve back through the tree. No pixel coordinates are ever stored in an artifact.

## Artifact schema

Identity is `(name, version, target)`. That triple is load-bearing for the multi-tenant story below. The rest of the schema is designed so a reviewer can read one JSON file and understand the capability without running it: an `input_schema` / `output_schema` (JSON Schema, with a custom `sensitive: true` marker for redaction), a set of declared `outcomes` each with a `detected_by` predicate and typed `extracts`, a list of `side_effects` (which lets replay classify risk), and an ordered `steps` array where each step carries a **locator strategy** (`primary` + `fallbacks`), an optional post-condition `expect`, and a `risk` label. A single `checkpoint` predicate exists for external observers who just want a boolean "did the capability complete."

Pydantic v2 models validate on load and the artifact is written as pretty JSON so it is diffable in code review.

## Determinism & error handling

Replay is deterministic given (artifact, inputs). Locators resolve primary-first, then walk the fallback tiers in order. The Recorder authors fallbacks at discovery time; if the primary breaks on the next run because the vendor changed a `role` attribute, the CSS or `label_relative` fallback usually catches it.

Result shape is `{outcome, outputs, error, evidence_dir}`. A declared non-SUCCESS outcome (e.g. `MEMBER_NOT_FOUND`) is a **typed business result**, not a crash — the caller gets an outcome code and can branch. Only a genuinely unknown page or a locator all-miss with no matching outcome produces `outcome=null` + `error=…`, which is what triggers escalation.

## Heterogeneity & multi-tenant

The identity triple `(name, version, target)` is the entire story. The same capability name (`open_sub_account`) can be published against multiple targets (`midwest_federal`, `some_other_bank`) with different step lists and different locator strategies but the same `input_schema` / `output_schema`. A caller asks the registry for "`open_sub_account` on target=X" and gets a shape-compatible artifact. The Surface abstraction extends this across UI *technologies*: a desktop tenant's artifact would carry UIA-style locators and be executed by `DesktopSurface`; the rest of the pipeline — recorder, replay, policy, evidence — is unchanged.

## Escalation & handoff

Three triggers: locator all-miss on a step, unknown page (no `detected_by` matches AND expected post-condition fails), risky step without approval. On any of them we (1) snapshot the current page as evidence, (2) write `intervention_request.json` with the capability id, the step id, the reason, the current URL, and paths to the screenshot and AX dump, (3) leave the headed browser open so a human can drive it, and (4) block on stdin for the resume signal. The production seam is called out explicitly in `cua/escalation/handoff.py`: swap stdin for a co-browsing console + webhook and the shape of the payload is already right.

## Safety

`policy.yaml` gates every action. `allowed_domains` prevents an agent from wandering off the target; `allowed_actions` locks the vocabulary down to `navigate, click, type, read, press, assert`; `risky_actions_require_approval` interacts with per-step `risk` labels and the artifact's `side_effects` list. Discovery marks any step that maps to an irreversible side effect as `risky`, and replay refuses to run it silently — either `--auto-approve-risky` is present or the escalation flow triggers.

Redaction is schema-driven: fields whose input schema carries `"sensitive": true` are replaced with `<redacted>` in every log line and never persisted in artifacts by value (only by name). The `redact_value` helper also scans free-form execution details for sensitive input values that might have leaked into a Playwright error message.

## Cuts

Deliberate, and I would flag every one to a reviewer:

- **No login, no iframes, no dynamic IDs on the target app.** Real legacy consoles have all three. The locator strategy is designed to survive dynamic IDs (that is why CSS is only ever a fallback), but I have not demonstrated it against a moving target.
- **DesktopSurface and VisionSurface are documented seams, not implementations.** The Surface interface is small enough that adding them is mechanical, but I chose to prove the *shape* rather than build two more perception stacks I could not test end-to-end.
- **Operator UI is a stdin resume, not a co-browsing console.** The intervention payload is already the right shape for one, and the headed browser stays open so a human really can drive it — but the handoff channel is a `readline()`.
- **No schema drift detection across tenant versions yet.** The identity triple gives you the *addressing* story; a real registry would also check that a new `open_sub_account.v1.1.0` for a tenant is input/output compatible with `v1.0.0` before promoting it.
- **Live LLM path is scaffolded, not exercised.** `cua/agent/llm.py` documents the Anthropic tool-use integration seam and the mock emits the identical `{tool, args}` shape a real model would, so the driver in `cua/agent/loop.py` is model-agnostic — but I did not spend an API budget re-verifying it.
- **Only one business outcome (`MEMBER_NOT_FOUND`).** A production target would have many (`ACCOUNT_LOCKED`, `KYC_REQUIRED`, etc.). The outcome list is a first-class array in the schema exactly so adding more is a data change, not a code change.
- **Extraction is regex + JSON Schema validation only.** Good enough for the demo; a serious system would want typed transforms and per-outcome extract validation reporting.
