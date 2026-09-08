# Report

> **Companion docs:** the deep artifact schema reference lives in
> [ARTIFACT.md](ARTIFACT.md); the setup and demo commands live in
> [README.md](README.md). This report is the design write-up.

## Architecture

Four layers, each behind a small interface. **Surface** perceives + acts (`snapshot`, `execute`); `WebSurface` implements it via Playwright. Documented seams for `DesktopSurface` (UIA) and `VisionSurface` (screenshot-only) sit alongside it — they implement the same two methods and reuse the rest of the stack unchanged. **Agent** runs a tool-use loop over that surface and records what happened; a `Recorder` turns tool calls into an `Artifact`. **Replay** executes an artifact deterministically against a Surface, resolving locators through the primary+fallbacks strategy, running the recovery loop, matching typed outcomes, and re-verifying invariants after every handoff. **Safety / Evidence / Escalation** are cross-cutting: every action passes through `Policy.check`, every step logs a JSONL row + per-tier verdicts + screenshot + AX dump, and any all-miss / unknown-page / risky-unapproved / human-required situation triggers `request_handoff` through the ownership state machine.

Two structural guarantees hold the design together and are enforced by tests, not documentation:

1. **Replay is LLM-free.** `tests/test_architecture.py` walks the AST of every module under `cua/replay`, `cua/artifact`, `cua/safety`, and `cua/surface` and asserts none import `anthropic`, `openai`, or the discovery-time LLM adapters. A future `import anthropic` in replay is a build break, not a review conversation.
2. **Only the current owner acts.** `GuardedSurface` wraps every `execute()` call in `SessionController.assert_agent()`. Actions attempted while the state is `paused` or `human` raise `OwnershipError`.

The perception layer is a serialized accessibility tree built in-page from ARIA/HTML semantics. Screenshot capture is available for a vision-fallback path; any vision call must return a *DOM-anchored* descriptor (label text, CSS descriptor) — no pixel coordinates ever land in an artifact.

## Artifact schema

Identity is `(name, version, target)` — the triple lets the catalog host multiple distinct implementations of the same capability across tenants and vendors, each addressable independently. Full field-by-field reference is in [ARTIFACT.md](ARTIFACT.md); the load-bearing design decisions:

- **Ranked locator strategies** (`primary` + `fallbacks`), each with `by ∈ {role_name, label_relative, css, text}`. Role/label matches with count > 1 are treated as ambiguous, not first-match-wins — prevents the "coincidental match" bug class.
- **Outcomes as first-class array**, each with a `detected_by` predicate and typed `extracts`. `SUCCESS` is just a normal declared outcome; `MEMBER_NOT_FOUND` / `ACCESS_DENIED` / `VALIDATION_ERROR` are equal-citizens, not crashes. Adding a new outcome is a data change.
- **Recoveries as first-class array** (`dismiss` / `wait` / `retry` / `reauth`) with trigger predicates. Auto-recovery for known transient conditions before escalation.
- **Two independent step gates**: `risk: risky` (policy signal, bypassable via `--auto-approve-risky`) and `human_required: true` (functional requirement, non-bypassable — for 2FA / OTP / dual-control).
- **Lifecycle**: `approval_state: draft | approved | deprecated`. Unattended replay of a draft is refused before the browser opens.

Pydantic v2 validates on load; JSON is written pretty for code review.

## Determinism & error handling

Replay is deterministic given `(artifact, inputs)`. Locators walk `primary → fallbacks[0..n]`; per-tier verdicts (`{tier, by, verdict: ok|miss|ambiguous|error, count}`) land in every step's evidence line so drift is post-hoc diagnosable without a re-run. Which tier fired on which step is aggregated as **drift telemetry** in `result.tier_usage` — `evidence/replay_base_on_summit_drift/` shows the base Midwest artifact pointed at Summit: `s2` silently drifted to tier=2, `s4` all-missed with the full verdict trail and escalated.

Before escalating, the engine walks the artifact's `recoveries[]`. **Reauth is a first-class recovery**: on a "Session expired" trigger the engine clicks the Sign-in locator, then restarts the flow from step 0 (positional state is lost after re-auth). `max_attempts` bounds cascades. Recoveries fire both on step failure *and* post-step — a click that returns 200 for a "Session expired" HTML body wouldn't fail at Playwright but the trigger still matches and the flow restarts. `evidence/replay_reauth_session_expire/` shows this end-to-end.

Result shape is `{outcome, outputs, error, evidence_dir, tier_usage, recoveries_applied, resolved_via_handoff}`. Declared non-SUCCESS outcomes are typed business results, not crashes — the caller gets a code and can branch. Only genuinely unknown state with no declared outcome produces `outcome=null`; escalation then either resolves to `SUCCESS` / a business outcome, or reports `ESCALATED_UNRESOLVED` with a structured error. Every distinction is a discriminator the calling agent can dispatch on.

`cua stability --n N` replays repeatedly and reports pass rate + tier distribution — the same telemetry, aggregated. `cua eval evals/replay.json` runs 13 golden scenarios across two capabilities and three targets (currently 13/13 green) — the natural promotion gate before `cua approve` fires.

## Heterogeneity & multi-tenant

The identity triple `(name, version, target)` hosts three kinds of tenants:

- **Base artifact** — `open_sub_account.v1.0.0.midwest_federal.json` and `open_sub_account.v1.0.0.acme_bancorp.json`. Same input/output schema, structurally different steps. Different vendor products entirely.
- **Overlay** — `open_sub_account.v1.0.0.summit_credit_union.overlay.json` patches the base with new label anchors, retargeted CSS, and a new entry URL. `apply_overlay(base, overlay)` merges into a concrete artifact at load time. The overlay does real work: Summit renames `input[name=q]` to `input[name=cif]`, so a base artifact structurally breaks against Summit — the overlay retargets both the semantic anchor and the CSS fallback so all tiers survive.
- **Structurally forbidden**: overlays cannot change step count, step ids, actions, or `output_schema` — those are unrepresentable in the type system (`StepPatch` doesn't expose the fields) or rejected at merge (`OverlayValidationError`). This is what makes the identity triple a real promise to callers, not a naming convention.

Two literal target apps ship with the repo — `target_app/` (Midwest, table layout, no test IDs) and `target_app_v2/` (ACME, wizard-style, semantic HTML, `data-role` buttons) — proving the abstraction survives a genuinely different vendor UI.

The Surface abstraction extends this across UI *technologies*: a Windows desktop tenant would carry UIA-style locators and be executed by `DesktopSurface`; the rest of the pipeline (recorder, replay, policy, evidence, ownership guard) is unchanged.

## Escalation & handoff

Handoff is a **state machine**, not a callback. `SessionController` tracks `agent | paused | human`, records every transition with `{ts, from, to, reason, actor}`, and enforces three invariants:

1. **Ownership on the wire.** `GuardedSurface.execute()` raises `OwnershipError` unless `state == agent`.
2. **Resume gate.** `SessionController.resume()` refuses (HTTP 409) unless at least one click/input/keydown was recorded on the page since the operator took control. `force=true` bypasses but is audited in the transition log. Human actions are counted via `expose_binding` on the page.
3. **Invariant re-verification.** After resume, the engine re-evaluates the step's `expect` or the capability `checkpoint`; if neither holds it re-escalates rather than proceeding on faith.

**Four escalation triggers**:
- **(a) Locator all-miss** — resolver exhausted every tier
- **(b) Unknown page** — post-condition failed and no outcome matched
- **(c) Risky-step gate** — `risk: "risky"` without `--auto-approve-risky`
- **(d) `human_required`** — step marked by the artifact author, **non-bypassable** (2FA / OTP / dual-control)

Trigger (d) is distinct from (c) in evidence — its reason string reads `"step is human_required-by-design (e.g. 2FA / OTP)"` vs `"risky action requires approval"`, so post-hoc audit shows which class of pause fired. After resume, `human_required` steps are NOT re-executed by the engine (the human already did them in the paused tab).

Transports: a **FastAPI operator console** (started with `--operator-port`) with `/status`, `/screenshot`, `/take-control`, `/resume`, `/abort` and a minimal HTML UI; plus a **stdin fallback** for offline demos. Both go through the same controller — the resume gate applies regardless of channel. An **on-page banner** is injected into the automation tab itself during handoff (`AGENT PAUSED — YOU HAVE CONTROL`) so the operator sees the state signal even without looking at the console.

**Escalation notifications** (`cua/escalation/notify.py`): terminal bell + generic HTTP webhook + Slack-shaped payload. All opt-in via `policy.yaml`, all best-effort — a broken sink never surfaces into a replay failure.

## Safety

`policy.yaml` gates every action. `allowed_domains` prevents wandering off the target; `allowed_actions` locks the vocabulary; `risky_actions_require_approval` interacts with per-step `risk` labels and `side_effects`. Discovery marks any step mapping to an irreversible side effect as `risky`; replay refuses to run silently.

**Redaction is two-layer.** *Schema-driven*: fields whose input schema carries `"sensitive": true` are replaced with `<redacted>`. *Shape-based*: regex patterns for credit card / SSN / routing / email / bearer tokens scrub matching substrings whether or not the schema flagged them. Idempotent, applied at every evidence sink.

**Operator console HTTP Basic Auth** via `CUA_OPERATOR_USER` + `CUA_OPERATOR_PASS` env vars (constant-time comparison). Opt-in-to-secure so the demo path stays one command; loud warning at startup if unset.

## Stretch goals

Four of six from the brief, all wired end-to-end:

- ✅ **Agent-facing capability interface** — HTTP API in `cua/api.py`. `GET /capabilities/tools` returns Anthropic tool_use schemas; `POST /capabilities/{n}/{v}/{t}/invoke` runs the artifact and returns the same result contract the CLI does. HTTP status mirrors outcome (200/409/500).
- ✅ **Approval state (partial confidence & approval)** — `approval_state: draft|approved|deprecated` + `cua approve` promotion; unattended replay of draft refuses before browser opens. Confidence *scoring* (reliability-per-artifact) not implemented.
- ✅ **Cross-tenant reuse** — overlay system + second literal vendor (ACME). See §Heterogeneity.
- ✅ **Multi-run stability** — `cua stability --n N` reports pass rate, outcome distribution, per-step tier distribution.
- ❌ **Code generation** — not implemented. Would emit a Playwright test file from an artifact.
- ❌ **Assisted LLM fallback on replay failure** — deliberately cut. Any LLM in the replay path breaks the "deterministic, cheap, cache-friendly" property that makes the artifact worth having; the architecture test enforces this structurally.

## Bugs found by running

Four real bugs surfaced while walking end-to-end paths — none would have been caught by unit tests alone. All are called out in the code with comments and pinned open by regression tests:

1. **Ambiguous-body 422 under curl but not TestClient.** POST `/invoke` returned validation errors on every live request because FastAPI's auto-body-inference gets confused by `dict[str, Any]` fields without an explicit `Body(...)` marker.
2. **`TypeAdapter is not fully defined` on every live POST.** `InvokeBody` had to be hoisted to module scope for Pydantic v2's ForwardRef resolution — TestClient masked this by serialising through a different path.
3. **Risky-step gate firing on flows that terminated at a business outcome.** ACME `member_not_found` was escalating because the risky gate ran before we noticed we were already at the terminal page. Fix: check for matching outcome at the top of each step.
4. **`--tenant` shorthand ambiguous when multiple capabilities share a tenant.** `lookup_member_balance` sorted before `open_sub_account` in the artifacts dir; `--tenant midwest_federal` silently replayed the wrong capability. Fix: require `--capability=NAME` to disambiguate.

Plus a fifth caught by GitHub's push protection — a bearer-token test string matched a Stripe key pattern. Fixed to a clearly-fake literal.

## Cuts

Deliberate, and I would flag every one to a reviewer:

- **DesktopSurface and VisionSurface are documented seams.** The Surface interface is small enough to add them mechanically, but I chose to prove the *shape* rather than build perception stacks I couldn't test end-to-end.
- **Operator UI is a minimal single-page console.** FastAPI endpoints + state machine + on-page banner are real; the HTML is one page with four buttons. A production console would stream JPEG frames of the live tab.
- **No canonicalized routes across tenants.** Overlay patches labels + entry URLs; a full canonicalization story (`/item/12345 → /item/:id` at record time) is not implemented but the schema accepts it as data.
- **Live LLM path is scaffolded, not exercised.** `cua/agent/llm.py` documents the Anthropic tool-use integration seam; the mock emits the identical `{tool, args}` shape a real model would.
- **Extraction is regex + JSON Schema validation only.** Good enough for the demo; a serious system would want typed transforms and per-outcome extract validation reporting.
- **Confidence scoring** (stretch goal §8) — stability rate would be a natural signal but I didn't wire per-artifact confidence attribution.
- **Code generation** (stretch goal §8) — an artifact-to-Playwright emitter is straightforward given the closed action set, but adds surface without depth; I preferred to make what shipped robust.

**Test count**: 34 unit tests + 13 golden eval scenarios, all green.
