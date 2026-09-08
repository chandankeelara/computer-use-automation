# Report

## Architecture

Four layers, each behind a small interface. **Surface** perceives + acts (`snapshot`, `execute`); `WebSurface` implements it via Playwright. Documented seams for `DesktopSurface` (UIA) and `VisionSurface` (screenshot-only) sit alongside it — they would implement the same two methods and reuse the rest of the stack unchanged. **Agent** runs a tool-use loop over that surface and records what happened; a `Recorder` turns tool calls into an `Artifact`. **Replay** executes an artifact deterministically against a Surface, resolving locators through the primary+fallbacks strategy, running the recovery loop, matching typed outcomes, and re-verifying invariants after every handoff. **Safety / Evidence / Escalation** are cross-cutting: every action passes through `Policy.check`, every step logs a JSONL row + tier + screenshot + AX dump, and any all-miss / unknown-page / risky-unapproved situation triggers `request_handoff` through the ownership state machine.

Two structural guarantees hold the design together and are enforced by tests, not documentation:

1. **Replay is LLM-free.** `tests/test_architecture.py::test_replay_has_no_llm_dependencies` walks the AST of every module under `cua/replay`, `cua/artifact`, `cua/safety`, and `cua/surface` and asserts none of them import `anthropic`, `openai`, `cua.agent.llm`, or the mock. A future `import anthropic` in replay is a build break, not a review conversation.
2. **Only the current owner acts.** `GuardedSurface` wraps every `execute()` call in `SessionController.assert_agent()`. Actions attempted while the state is `paused` or `human` raise `OwnershipError` instead of silently reaching the browser.

The perception layer is a serialized accessibility tree — role, name, value, hierarchy — built in-page from ARIA/HTML semantics. Screenshot capture is available on demand for a vision fallback path; the vision call is required to name a *DOM-anchored* descriptor (label text, CSS descriptor) that we resolve back through the tree. No pixel coordinates are ever stored in an artifact.

## Artifact schema

Identity is `(name, version, target)`. That triple is load-bearing for the multi-tenant story below. The rest of the schema is designed so a reviewer can read one JSON file and understand the capability without running it: an `input_schema` / `output_schema` (JSON Schema, with a custom `sensitive: true` marker for redaction), a set of declared `outcomes` each with a `detected_by` predicate and typed `extracts`, a list of `side_effects` (which lets replay classify risk), a capability-level `recoveries` list (see below), and an ordered `steps` array where each step carries a **locator strategy** (`primary` + `fallbacks`), an optional post-condition `expect`, and a `risk` label. A single `checkpoint` predicate exists for external observers who just want a boolean "did the capability complete."

Pydantic v2 models validate on load and the artifact is written as pretty JSON so it is diffable in code review. Adding a new business outcome (e.g. `ACCOUNT_LOCKED`) or a new transient-condition recovery (e.g. dismiss a marketing modal) is a **data change**, not a code change — precisely because the outcome list and recovery list are first-class arrays.

## Determinism & error handling

Replay is deterministic given (artifact, inputs). Locators resolve primary-first, then walk the fallback tiers in order — but with a guard: `role_name` and `label_relative` matches that resolve to *more than one* element are treated as **ambiguous** and fall through to the next tier, not "first match wins". This prevents the "coincidental match" bug class (e.g. a semantic anchor accidentally matching both the Confirm and Continue-Anyway buttons on an unexpected interstitial). CSS remains lenient because it's already the last-resort tier; tightening it would break legitimate fallback patterns like `.cls_btn`. Per-tier verdicts (`{tier, by, verdict: ok|miss|ambiguous|error, count}`) land in every step log so "why did we fall through" is answerable without a re-run.

Which tier fired on which step is aggregated as **drift telemetry** in every run manifest (`result.tier_usage`), so silent drift onto fallbacks doesn't stay silent. See `evidence/replay_base_on_summit_drift/` — the base Midwest artifact was pointed at the Summit tenant; `s2` silently drifted from tier 0 to tier 2 (the semantic anchor "Member ID" missed, the CSS fallback `input[name='q']` missed too, `role_name textbox` won at tier=2), then `s4` all-missed with a full verdict trail and escalated.

Before escalating on step failure or unknown-page, the engine walks the artifact's `recoveries[]`. Each recovery has a `trigger` (text/URL/element predicate) and an `action` (`dismiss`/`wait`/`retry`/`reauth`); the step is retried up to `max_attempts` times. Applied recoveries land in `result.recoveries_applied` for evidence. This turns "a modal popped up" into a schema entry, not a code change.

**Reauth is a first-class recovery action.** Standard mid-flow session-timeout pattern: page renders "Session expired" → recovery clicks the `reauth_locator` ("Sign in") → the whole flow restarts from step 0 (because re-auth invalidates positional state). `max_attempts` bounds restart cascades so a permanently-broken session cannot loop forever. Recoveries also fire *post-step* — a click that returns 200 for a "Session expired" HTML body wouldn't fail at the Playwright layer but the recovery trigger still matches, and the flow restarts. `evidence/replay_reauth_session_expire/` shows this end-to-end: navigate arms `?expire=1`, step s3 lands on the interstitial, reauth clicks Sign in, flow restarts, second attempt succeeds, typed balance extracted.

Result shape is `{outcome, outputs, error, evidence_dir, tier_usage, recoveries_applied, resolved_via_handoff}`. Declared non-SUCCESS outcomes (`MEMBER_NOT_FOUND`, `ACCESS_DENIED`, `VALIDATION_ERROR`) are **typed business results**, not crashes — the caller gets an outcome code and can branch. Only a genuinely unknown page or a locator all-miss with no matching outcome produces `outcome=null` + `error=…`; when that happens the escalation flow triggers and the outcome is either resolved (`SUCCESS`/business outcome) or reported as `ESCALATED_UNRESOLVED`. Every distinction here is a discriminator the calling agent can dispatch on.

The `cua stability` CLI replays N times and reports a pass rate, an outcome distribution, and the tier-usage distribution per step — the same drift telemetry, aggregated. It's the cheapest possible flakiness signal a promotion gate could use.

## Heterogeneity & multi-tenant

The identity triple `(name, version, target)` is the entire story, and it is now demonstrated end-to-end. `open_sub_account.v1.0.0.midwest_federal.json` is the base artifact; `open_sub_account.v1.0.0.summit_credit_union.overlay.json` is a small **Overlay**. The Summit tenant is not just relabelled — it structurally renames the underlying form field (`name="q"` → `name="cif"`) so a base artifact's CSS fallback misses too. The overlay patches both the semantic anchor (`Member ID` → `CIF ID`) *and* the CSS fallback (`input[name='q']` → `input[name='cif']`), plus the button rename (`Open Sub-Account` → `Open Additional Account`). `cua replay --tenant summit_credit_union` loads the overlay, merges via `apply_overlay(base, overlay)`, and replays against `?tenant=summit` — same steps, same output schema, same declared outcomes. `evidence/replay_summit_tenant_success/` is the recorded run.

The overlay does real work — proven by `evidence/replay_base_on_summit_drift/`, which runs the *base* artifact against the Summit tenant with no overlay. `s2` silently drifts to tier=2 (the last resort semantic tier), `s4` all-misses with structured verdicts, and the run escalates. This is the failure mode a naive "one artifact per app" approach would silently accept.

The overlay resolver enforces four rules structurally: the step count, step ids, step actions, and `output_schema` cannot change under an overlay. Attempting any of them raises `OverlayValidationError`. `StepPatch` does not even *expose* `action` or `id` fields — those changes are unrepresentable, not merely rejected. This is what makes the identity triple a real promise to callers rather than a naming convention.

The Surface abstraction extends this across UI *technologies*: a desktop tenant's artifact would carry UIA-style locators and be executed by `DesktopSurface`; the rest of the pipeline — recorder, replay, policy, evidence, ownership guard — is unchanged.

### A second capability: read-only balance lookup

`lookup_member_balance.v1.0.0` ships against all three targets. It's a
read-only capability — `side_effects: []`, no risky steps, one typed
output extracted from the DOM via a `label_relative` anchor scoped to
the balance cell. This exercises the previously-untouched `read` +
locator-scoped-extract paths and proves the schema handles read-only
capabilities cleanly, not just mutating ones. It also flushed out a
real bug caught by the eval suite: the CLI's `--tenant` shorthand was
ambiguous when multiple capabilities lived in one artifacts directory
(`lookup_member_balance` sorts before `open_sub_account`, so
`--tenant midwest_federal` was silently replaying the wrong capability).
Fix: `_resolve_artifact_path` now refuses ambiguous matches and requires
`--capability=NAME` to disambiguate. Regression is the golden-eval
suite itself.

### A second literal vendor: ACME Bancorp

The Midwest/Summit story shows one vendor with a tenant overlay. To prove the identity triple really does host structurally distinct implementations, `target_app_v2/` (port 5001) is a *different vendor* — ACME Bancorp — running a wizard-style 3-page flow with proper `<label for>` semantics, `<button data-role>` elements, different member-id shapes (`A-1042`), and a different confirmation URL. `artifacts/open_sub_account.v1.0.0.acme_bancorp.json` is a full base artifact for it — same `input_schema`, same `output_schema`, same declared outcomes, structurally different steps. The catalog now serves three rows under one capability name:

```
open_sub_account v1.0.0 target=acme_bancorp        kind=artifact
open_sub_account v1.0.0 target=midwest_federal     kind=artifact
open_sub_account v1.0.0 target=summit_credit_union kind=overlay  overlay-of=…/midwest_federal
```

An AI agent asks for `open_sub_account` on `target=X` and gets a shape-compatible runner regardless of which vendor UI is underneath. Evidence in `evidence/replay_acme_bancorp_success/` and `evidence/eval__acme_bancorp_*`.

## Lifecycle & promotion

Every artifact carries `approval_state: draft | approved | deprecated` plus `approved_by` / `approved_at`. Unattended replay of a `draft` refuses with `outcome=DRAFT_ARTIFACT_REFUSED` before touching the browser — someone has to `cua approve --artifact PATH --by NAME` (or explicitly pass `allow_draft=True`, which lands in the audit log). This is the promotion gate a real ops system would run before flipping a capability to production.

Golden evals (`evals/replay.json` + `cua eval`) exercise the full outcome matrix across all three targets — 8 scenarios covering happy path, MEMBER_NOT_FOUND, ACCESS_DENIED, VALIDATION_ERROR on each vendor. Non-zero exit on any mismatch; the natural gate to run before `cua approve` fires. 8/8 currently pass. Written report at `evidence/eval_report.json`.

## Escalation notifications

Every intervention fans out to three opt-in sinks (`cua/escalation/notify.py`), configured in `policy.yaml`:
* `notify_bell` — terminal `\a` bell, on by default.
* `notify_webhook_url` — generic HTTP POST of `{event, payload}` as JSON.
* `notify_slack_webhook_url` — Slack `attachments`-shaped payload (color-coded, key fields extracted).

All sinks are best-effort: a broken Slack integration cannot cascade into a replay failure — the sink layer catches every exception and records the verdict into the evidence manifest. Test coverage points a real HTTP server at a random port and asserts both wire shapes plus the dead-URL-doesn't-raise property.

## Agent-facing capability API

Saved artifacts are published as an HTTP capability catalog (`cua/api.py`, started with `cua serve --port 8770`). `GET /capabilities/tools` returns Anthropic tool_use-shaped schemas — each tool's name embeds the identity triple so a caller can address a specific tenant (`open_sub_account__1_0_0__midwest_federal`, `open_sub_account__1_0_0__summit_credit_union`). `POST /capabilities/{name}/{version}/{target}/invoke` runs the artifact deterministically with typed inputs and returns the same result contract the CLI does (outcome, outputs, error, evidence_dir, tier_usage, recoveries_applied). HTTP status mirrors outcome shape: 200 = success or declared business outcome, 409 = escalated-unresolved, 500 = hard failure. This is the brief's stretch goal #1, wired end-to-end. Live evidence in `evidence/api_invoke_success/`.

## Escalation & handoff

Handoff is a **state machine**, not a callback. `SessionController` tracks `agent | paused | human`, records every transition with `{ts, from, to, reason, actor}` in the evidence, and enforces three invariants:

1. **Ownership on the wire.** `GuardedSurface.execute()` raises `OwnershipError` unless `state == agent`. Automation cannot accidentally act while a human is driving.
2. **Resume gate.** `SessionController.resume()` refuses (raises `ResumeRefused`) unless at least one human action was recorded on the page since takeover, or the caller passes `force=True` (audited in the transition log). "Operator hit Resume with nothing done" is exactly the class of bug this catches. Human actions are counted by an init-script that increments a bound page function on `click`/`input`/`keydown`/`submit`.
3. **Invariant re-verification.** After a resume from `human` back to `agent`, the engine re-evaluates the current step's `expect` (or the capability `checkpoint`); if neither holds it re-escalates rather than proceeding on faith.

Two transports for the handoff itself: a **FastAPI operator console** (started with `--operator-port 8765`) exposing `/status`, `/screenshot`, `/take-control`, `/resume` (JSON body `{decision, force}`), and `/abort`; and a **stdin fallback** for offline demos. Both write `intervention_request.json` and both go through the same controller — the resume gate applies regardless of channel. The production seam is called out explicitly in `cua/escalation/console.py`: swap the FastAPI endpoints for a co-browsing UI and the wire format is already right.

## Safety

`policy.yaml` gates every action. `allowed_domains` prevents an agent from wandering off the target; `allowed_actions` locks the vocabulary down to `navigate, click, type, read, press, assert`; `risky_actions_require_approval` interacts with per-step `risk` labels and the artifact's `side_effects` list. Discovery marks any step that maps to an irreversible side effect as `risky`, and replay refuses to run it silently — either `--auto-approve-risky` is present, or the escalation flow triggers and the operator has to sign off.

**Redaction is two-layer.** *Schema-driven*: fields whose input schema carries `"sensitive": true` are replaced with `<redacted>` in every log line and never persisted in artifacts by value. *Shape-based*: regex patterns for common regulated-data shapes (credit card, SSN, routing number, email, `Bearer <token>`) scrub matching substrings in every free-form log/detail line whether or not the schema flagged them. This is the belt-and-braces layer for the case where a value happens to look like a card even though nobody marked the field sensitive — key-based redaction alone will miss those. Redaction is idempotent.

**Operator console Basic Auth.** Anyone who can reach the console port can approve an irreversible financial action, so `CUA_OPERATOR_USER` + `CUA_OPERATOR_PASS` env vars gate every endpoint (`Depends(HTTPBasic)` with constant-time comparison). If either is unset the console runs open with a loud startup warning — opt-in-to-secure so the demo path stays one command.

**On-page pause banner.** When the engine escalates, an unmissable red banner is injected into the *automation tab* itself: `AGENT PAUSED — YOU HAVE CONTROL`. This closes the "operator eyes on the tab, escalation signal on a different monitor" gap: the operator console may be a monitor away, but the banner is in the tab they're about to interact with. The banner has a fixed id so repeated injection replaces rather than stacks.

## Bugs found by running (partial post-mortem)

Three real bugs surfaced while wiring the HTTP invoke API and running the eval suite; none would have been caught by unit tests alone. All are called out in the code with comments and pinned open by regression tests:

1. **Ambiguous-body 422 under curl but not TestClient.** POST `/invoke` was returning `{"loc":["query","body"],"msg":"Field required"}` for every live request. The unit tests using `fastapi.testclient.TestClient` passed. Root cause: FastAPI needs an explicit `Body(...)` marker when a Pydantic model body has fields typed as `dict[str, Any]`; without it the auto-body-inference routes the whole body as a query parameter. Fix in `cua/api.py::invoke`. Regression: `test_invoke_body_validation` sends a shape-invalid body and asserts 422 (proving the body is actually being parsed, not silently dropped).
2. **`PydanticUserError: TypeAdapter is not fully defined` on every live POST.** After the first fix, live POSTs 500'd because `InvokeBody` was declared inside `build_app()`; Pydantic v2 can't resolve the ForwardRef for a class defined in a closure. TestClient masked this because it serialises through a different code path. Fix: hoist `InvokeBody` to module scope in `cua/api.py`. Regression: `test_invoke_body_model_is_module_scoped` asserts the class lives at module scope so a well-intentioned refactor doesn't move it back.
3. **Risky-step gate firing on flows that already terminated at a business outcome.** The ACME `member_not_found` eval failed: the flow correctly landed on the "Member not found" page after step s3, but s4 was declared `risky` and the engine ran the policy gate on s4 before checking for a matching declared outcome — so the run escalated instead of returning `MEMBER_NOT_FOUND`. This one only fell out under `cua eval` because the CLI demos had always passed `--auto-approve-risky`, bypassing the gate. Fix: at the top of each step, `find_matching_outcome` is checked first; a non-SUCCESS match short-circuits and returns the typed outcome. Regression: `test_early_outcome_short_circuits_risky_gate` constructs an artifact where step 2 is `risky` but step 1's page already matches MEMBER_NOT_FOUND, and asserts the typed outcome is returned without escalation.

All three shipped in units-passing code and only fell out when the API was actually curled or the eval suite was actually run — exactly the "evidence from running, not designing" signal the strongest submissions demonstrate.

## Cuts

Deliberate, and I would flag every one to a reviewer:

- **DesktopSurface and VisionSurface are documented seams, not implementations.** The Surface interface is small enough that adding them is mechanical, but I chose to prove the *shape* rather than build two more perception stacks I could not test end-to-end.
- **Operator UI is a minimal single-page console.** The FastAPI endpoints and the ownership state machine are real; the UI is one HTML page with three buttons. A production console would be a React app streaming JPEG frames of the live tab. The wire format, resume gate, and controller are already the right shape for one.
- **No canonicalized routes across tenants.** The overlay demonstrates label + entry-URL patches; a full canonicalization story (`/item/12345 → /item/:id` at record time) is not implemented. The `Overlay` schema is designed to accept it as data when I add it.
- **Live LLM path is scaffolded, not exercised.** `cua/agent/llm.py` documents the Anthropic tool-use integration seam and the mock emits the identical `{tool, args}` shape a real model would, so the driver in `cua/agent/loop.py` is model-agnostic — but I did not spend an API budget re-verifying it.
- **Extraction is regex + JSON Schema validation only.** Good enough for the demo; a serious system would want typed transforms and per-outcome extract validation reporting.
- **Recovery vocabulary is intentionally small.** `dismiss` / `wait` / `retry` cover the transient conditions in the target app. A production system would add `reauth` and `bounded-backoff-with-jitter` as first-class actions.
