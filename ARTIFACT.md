# Artifact Architecture

The **capability artifact** is the load-bearing data structure of the whole
system. Discovery produces it. Replay consumes it. The API publishes it. The
operator console displays it. This document is the canonical reference for
what an artifact is, what shapes it can take, and how to author or extend one.

If you only read one file to understand the design, read this and one example
artifact side-by-side:
- Reference: this document
- Example: [`artifacts/open_sub_account.v1.0.0.midwest_federal.json`](artifacts/open_sub_account.v1.0.0.midwest_federal.json)

Source of truth: [`cua/artifact/schema.py`](cua/artifact/schema.py) (Pydantic v2 models).

---

## 1. What an artifact is

An artifact is a **typed, versioned, JSON-serialisable recipe** that a
deterministic engine can replay against a real UI to complete a specific
task, without any LLM in the loop. It captures:

- What inputs the caller supplies (typed)
- What outputs the caller gets back (typed)
- The ordered steps the engine executes
- Every declared end-state (SUCCESS + business outcomes + failures)
- How to auto-recover from known transient conditions
- What's risky, what needs a human, what needs approval
- Where the artifact came from (provenance)

A single JSON file is enough for a human reviewer to understand the whole
capability without running it. That reviewability is a first-class design
constraint — not a nice-to-have.

## 2. Identity: `(name, version, target)`

Every artifact is addressed by a triple, not just a name:

```jsonc
"id": {
  "name":    "open_sub_account",   // capability family
  "version": "1.0.0",              // semver of THIS artifact's contract
  "target":  "midwest_federal"     // the vendor / tenant this runs against
}
```

The triple is what makes multi-tenant work: **the same capability name**
(`open_sub_account`) can be served by **multiple distinct implementations**
across tenants and vendors, each addressable independently. The current
catalog:

| name | version | target | kind |
|---|---|---|---|
| open_sub_account | 1.0.0 | midwest_federal | base artifact |
| open_sub_account | 1.0.0 | summit_credit_union | overlay of midwest_federal |
| open_sub_account | 1.0.0 | acme_bancorp | base artifact (different vendor) |
| open_sub_account | 1.0.0 | midwest_with_2fa | base artifact (human_required demo) |
| lookup_member_balance | 1.0.0 | midwest_federal | base artifact |
| lookup_member_balance | 1.0.0 | summit_credit_union | overlay |
| lookup_member_balance | 1.0.0 | acme_bancorp | base artifact |
| lookup_member_balance | 1.0.0 | midwest_with_reauth | base artifact (reauth demo) |

Version semantics: bump the minor for a compatible schema tweak (added
outcome, added recovery); bump the major for a breaking change to inputs
or outputs.

## 3. Top-level fields

```jsonc
{
  "id":              ArtifactId,     // (name, version, target)
  "title":           str,            // human-readable
  "description":     str,            // one paragraph, reviewer-facing
  "input_schema":    JsonSchema,     // typed caller inputs, with "sensitive": true markers
  "output_schema":   JsonSchema,     // typed caller outputs
  "outcomes":        Outcome[],      // every declared end-state
  "side_effects":    SideEffect[],   // what mutates on the target
  "steps":           Step[],         // ordered actions
  "checkpoint":      Checkpoint?,    // single boolean "did we complete"
  "recoveries":      Recovery[],     // capability-level auto-recovery
  "approval_state":  "draft" | "approved" | "deprecated",
  "approved_by":     str?,
  "approved_at":     str?,           // ISO timestamp
  "provenance":      Provenance      // goal, model, run_id, target_url
}
```

## 4. Input / output schemas

Standard JSON Schema, with one custom marker:

```jsonc
"input_schema": {
  "type": "object",
  "required": ["member_id"],
  "properties": {
    "member_id": { "type": "string", "example": "12345" },
    "pin":       { "type": "string", "sensitive": true }   // ← redacted everywhere
  }
}
```

`"sensitive": true` on a field means:
- The **schema-driven redaction** layer scrubs any log/detail line where the
  value appears
- The Recorder never persists the literal value into an artifact; it
  parameterises to `{{member_id}}`

Output schema is also enforced — `extract_outputs` raises `RuntimeError` if
the extracted result fails validation (e.g. a numeric field with no digits
becomes a hard failure, not a silent zero).

## 5. Steps

Ordered list. Each step:

```jsonc
{
  "id":              "s4",                    // stable within artifact
  "action":          "click",                 // navigate|click|type|press|read|assert
  "target":          LocatorStrategy?,        // required for click/type/read/assert
  "value":           "{{member_id}}"?,        // for type; {{param}} substituted at runtime
  "url":             "http://..."?,           // for navigate
  "expect":          Expectation?,            // optional post-condition
  "risk":            "safe" | "risky",        // policy signal
  "human_required":  false,                   // functional: needs a human on every run
  "notes":           "..."                    // reviewer-facing
}
```

### 5.1 `action` — a closed set

Six kinds only. Each maps to a Playwright primitive:

| action | Playwright behavior | Uses `target`? | Uses `value`? | Uses `url`? |
|---|---|---|---|---|
| `navigate` | `page.goto(url)` | — | — | ✔ |
| `click`    | `handle.click()`  | ✔ | — | — |
| `type`     | `handle.fill(value)` | ✔ | ✔ | — |
| `press`    | `page.keyboard.press(value)` | — | ✔ | — |
| `read`     | `handle.inner_text()` returned as `read_value` | ✔ | — | — |
| `assert`   | `handle.is_visible()` returns ok/not-ok | ✔ | — | — |

Closed vocabulary is a deliberate constraint: adding a new action requires
extending both the schema and the engine. The tradeoff is "you can't do
arbitrary things" vs "the surface is small, portable, and cheap to
implement on a Desktop or Vision surface later."

### 5.2 `risk` vs `human_required` — two independent axes

They look similar but have different semantics:

|  | `risk: "risky"` | `human_required: true` |
|---|---|---|
| **Nature** | Policy signal | Functional requirement |
| **Fires when?** | Only if `risky_actions_require_approval: true` in policy | Always |
| **Bypassable?** | Yes, via `--auto-approve-risky` | **No** |
| **After operator resume, engine executes step?** | Yes (approve = "run it") | No (human already did it) |
| **Canonical example** | "Mutating action, might need review" | "OTP entry — only human has the code" |
| **Evidence reason** | `"risky action requires approval: ..."` | `"step is human_required-by-design (e.g. 2FA / OTP)"` |

A step can be both (a risky 2FA prompt is both `risky: risky` AND
`human_required: true`) — the `human_required` gate fires first.

### 5.3 `expect` — post-step invariant

Optional. If set, the engine checks it after executing the step; a failed
`expect` triggers the unknown-page path (recovery loop → escalation).

```jsonc
"expect": {
  "kind":    "url_pattern" | "text_present" | "element_visible",
  "pattern": "^.*/confirm/.*$",   // for url_pattern
  "text":    "Confirmed",         // for text_present
  "locator": LocatorStrategy?     // for element_visible
}
```

## 6. Locator strategies

Every actionable step carries a `LocatorStrategy`:

```jsonc
"target": {
  "primary":   { "by": "role_name", "role": "button", "name": "Search" },
  "fallbacks": [
    { "by": "css",             "selector": "input[type=submit][value='Search']" },
    { "by": "label_relative",  "anchor": "Search:", "direction": "next_button" }
  ]
}
```

### 6.1 The four locator kinds

| `by` | Required fields | Playwright call | Best for |
|---|---|---|---|
| `role_name` | `role`, optional `name` | `page.get_by_role(role, name=name)` | Semantic identity (buttons, headings, textboxes) |
| `label_relative` | `anchor` text, `direction` | XPath: text-match label → next `input`\|`button`\|`cell` | Legacy `<td>label</td><td><input></td>` patterns |
| `css` | `selector` | `page.locator(selector)` | Attribute-based fallback (`input[name=q]`, `#member_field`) |
| `text` | `text`, optional `exact` | `page.get_by_text(text, exact=exact)` | Free-form text anchors |

`label_relative` directions: `next_input`, `next_button`, `next_cell`.

### 6.2 Primary + fallback resolution

The resolver walks tiers in order:

```
tier 0: primary
tier 1: fallbacks[0]
tier 2: fallbacks[1]
...
```

Each tier produces a `verdict`:

```jsonc
{ "tier": 0, "by": "role_name", "verdict": "ok",         "count": 1 }
{ "tier": 1, "by": "css",       "verdict": "miss",       "count": 0 }
{ "tier": 2, "by": "text",      "verdict": "ambiguous",  "count": 3 }
{ "tier": 3, "by": "css",       "verdict": "error",      "detail": "..." }
```

Verdicts land in every step's evidence line, so drift is post-hoc
diagnosable without a re-run.

### 6.3 The ambiguity guard (structural correctness)

For `role_name` and `label_relative`, **more than one match is treated as
a miss** (verdict = `ambiguous`), not "first match wins". Semantic
strategies must uniquely identify a control; if they don't, we might be
about to click the wrong thing.

CSS remains lenient (accepts count > 1) because it's already the
last-resort tier — tightening it would break legitimate patterns like
`.cls_btn`.

This is enforced by `tests/test_locator_ambiguity.py`, not documentation.

## 7. Outcomes — every ending is typed

An outcome is a declared end-state with a predicate that detects it and
optional data to extract. The engine matches outcomes at multiple points:
after a failing step, on unknown-page, and after the last step.

```jsonc
{
  "code":        "MEMBER_NOT_FOUND",       // caller-facing dispatch key
  "terminal":    true,                     // stops the flow (currently always true)
  "detected_by": {
    "kind":    "text_present",             // url_pattern | text_present | element_visible
    "text":    "No member found"
  },
  "extracts":    []                        // no data to pull for this outcome
}
```

### 7.1 The three predicate kinds

| `kind` | Additional fields | Matches when |
|---|---|---|
| `url_pattern` | `pattern` (regex) | `re.match(pattern, page.url)` |
| `text_present` | `text` | `text in page.locator("body").inner_text()` |
| `element_visible` | `locator` | Locator resolves to > 0 elements |

### 7.2 SUCCESS is just a declared outcome

There's nothing special about the `SUCCESS` code — it goes through the same
matcher. Convention: use `"SUCCESS"` for the happy path; use domain-
specific codes for business outcomes (`MEMBER_NOT_FOUND`,
`ACCESS_DENIED`, `VALIDATION_ERROR`, `ACCOUNT_LOCKED`, etc.).

The most common design mistake in this class of system is **conflating
"MEMBER_NOT_FOUND" with a crash**. It is not a crash. It is a legitimate
answer the caller needs to branch on. The engine returns:

```jsonc
{ "outcome": "MEMBER_NOT_FOUND", "outputs": null, "error": null }
```

Not:

```jsonc
{ "outcome": null, "error": {...} }   // ← this is reserved for actual failures
```

### 7.3 Extracts — typed data pulled from the terminal page

```jsonc
"extracts": [
  {
    "name":    "sub_account_id",              // matches an output_schema key
    "from":    "url",                          // "url" | "text"
    "locator": LocatorStrategy?,               // scope for "text" — walks tiers
    "regex":   "/confirm/(?P<sub_account_id>SA\\d+)"  // optional; named groups preferred
  }
]
```

Rules:
- `from: "url"` — source is `page.url`
- `from: "text"` with `locator` — source is `handle.inner_text()` on the
  resolved locator (scoped; keeps regex from roaming the whole page)
- `from: "text"` without `locator` — source is full body text (loose)
- Without a regex, the raw source string is returned
- Named regex groups matching `name` win over positional groups
- All outputs are validated against `output_schema` — a mismatch is a
  hard `RuntimeError`, not a silent success

## 8. Recoveries — auto-recover from known transient conditions

Capability-level list. Each recovery has a trigger and an action.

```jsonc
{
  "name":            "reauth_on_session_expire",
  "trigger":         {
    "kind":    "text_present",
    "text":    "Session expired"
  },
  "action":          "reauth",                // dismiss | wait | retry | reauth
  "dismiss_locator": LocatorStrategy?,        // for dismiss
  "reauth_locator":  LocatorStrategy?,        // for reauth
  "reauth_wait_ms":  200,
  "wait_ms":         500,                     // for wait
  "max_attempts":    2,
  "notes":           "..."
}
```

### 8.1 The four recovery actions

| `action` | Behavior | Retries current step? | Restarts flow? |
|---|---|---|---|
| `dismiss` | Click `dismiss_locator`, wait for page load | ✔ | ✘ |
| `wait` | Sleep `wait_ms` | ✔ | ✘ |
| `retry` | Do nothing extra; just try again | ✔ | ✘ |
| `reauth` | Click `reauth_locator`, wait, restart from step 0 | ✘ | ✔ |

`reauth` is the mid-flow session-timeout pattern. After re-auth the
session is healthy but positional state is lost, so restarting from the
entry step is the only correct semantic. `max_attempts` bounds restart
cascades so a permanently-broken session cannot loop forever.

### 8.2 When recoveries fire

Two entry points:
- **On step failure** — before escalating for locator all-miss
- **Post-step** — even if the step succeeded technically, if the current
  page matches a recovery trigger (e.g. HTML body says "Session expired"
  but Playwright returned 200), the recovery fires

Each match is logged into `result.recoveries_applied`:

```jsonc
{ "name": "reauth_on_session_expire", "action": "reauth", "step_id": "s3",
  "restart_from_step_0": true, "ok": true }
```

## 9. Side effects

```jsonc
"side_effects": [
  { "kind": "creates_sub_account", "reversible": false }
]
```

Declared for reviewability and future policy hooks. The current engine
uses these implicitly: any artifact with a non-reversible side effect
should have at least one `risk: "risky"` step; the Recorder enforces
this. A production policy could refuse to promote to `approved` any
artifact that has a non-reversible side effect without a risky-labelled
step.

## 10. Checkpoint

A single boolean predicate for external observers who don't want to parse
the outcome list:

```jsonc
"checkpoint": {
  "kind":    "url_pattern",
  "pattern": "^.*/confirm/SA\\d+$"
}
```

Different from `step.expect`:
- `step.expect` — post-condition for a specific step
- `checkpoint` — "did the whole capability complete"

The engine uses `checkpoint` for the escalation resume-gate: if the
operator "approves" a risky step, the engine only skips the step if the
checkpoint now holds (i.e. the operator did it manually). Otherwise the
engine executes the step normally.

## 11. Approval state (lifecycle)

```jsonc
"approval_state": "draft" | "approved" | "deprecated",
"approved_by":    "chandan",
"approved_at":    "2026-09-08T05:35:00Z"
```

- **draft** — freshly recorded, not yet human-reviewed. Unattended replay
  refuses with `outcome=DRAFT_ARTIFACT_REFUSED` before touching the
  browser.
- **approved** — signed off. Unattended replay is permitted.
- **deprecated** — retired. Unattended replay refuses.

Escape hatch: `allow_draft=True` runs draft artifacts, but the run
manifest records it as an audit trail. Promote via `cua approve
--artifact PATH --by NAME --to approved`.

## 12. Provenance

```jsonc
"provenance": {
  "goal":        "Open a sub-account for member 12345",
  "model":       "mock-llm@1",
  "recorded_at": "2026-09-04T10:51:52.717725Z",
  "run_id":      "discovery_12345",
  "target_url":  "http://127.0.0.1:5000"
}
```

Non-load-bearing for replay, but critical for reviewability: a reviewer
can trace any artifact back to the discovery run + model + goal that
produced it, and cross-reference against `evidence/<run_id>/`.

---

## 13. Overlays — cross-tenant reuse without re-recording

An **overlay** is a small patch layered on a base artifact for a different
tenant running the same vendor product.

```jsonc
{
  "id":       { "name": "open_sub_account", "version": "1.0.0", "target": "summit_credit_union" },
  "based_on": { "name": "open_sub_account", "version": "1.0.0", "target": "midwest_federal" },
  "title":       "Open sub-account (Summit Credit Union)",
  "description": "...",
  "entry_url":   "http://127.0.0.1:5000/?tenant=summit",   // replaces base's first navigate URL
  "step_patches": [
    {
      "step_id": "s2",
      "target":  { ... new locator strategy ... },
      "value":   "..."?,        // optional override
      "expect":  { ... }?,      // optional override
      "notes":   "..."?         // prefixed with '[overlay]' when applied
    }
  ],
  "extra_outcomes":    Outcome[],
  "extra_recoveries":  Recovery[],
  "extra_allowed_domains": string[],
  "notes":             "..."
}
```

`apply_overlay(base, overlay)` merges into a concrete Artifact. The
loader (`load_artifact("overlay.json")`) does this transparently.

### 13.1 Structural invariants

Overlays **cannot** change:
1. Step count
2. Step ids
3. Step actions
4. `output_schema`

`StepPatch` doesn't even *expose* `id` or `action` fields — those changes
are unrepresentable in the type system, not merely rejected at runtime.
`output_schema` and step count are validated at merge time; violations
raise `OverlayValidationError`. This is enforced by
`tests/test_architecture.py::test_overlay_cannot_change_output_schema`.

Overlays **can**:
1. Rewrite the entry-URL of the first navigate step
2. Replace target locator, value, expect, notes on any step (by id)
3. Append additional outcomes and recoveries
4. Extend the allowed-domains list

The result is a promise to callers: an overlay for tenant X of
capability `(name, version)` is guaranteed to have the same input
schema, the same output schema, the same step count, and the same
action sequence as the base. Only anchors and values differ.

### 13.2 When to overlay vs. ship a second base artifact

- **Overlay** when the tenant runs the same vendor product with cosmetic
  or minor structural differences (relabelled fields, renamed
  `input[name=...]`, retagged buttons).
- **Second base artifact** when the tenant runs a *different* vendor
  product entirely (different flow shape, different pages, different
  action sequence).

The catalog surfaces both:

```
open_sub_account v1.0.0 target=midwest_federal     kind=artifact
open_sub_account v1.0.0 target=summit_credit_union kind=overlay  overlay-of=midwest_federal
open_sub_account v1.0.0 target=acme_bancorp        kind=artifact
```

---

## 14. Structural invariants (enforced by tests, not docs)

| Invariant | Test file |
|---|---|
| Replay/artifact/safety/surface never import an LLM SDK | `tests/test_architecture.py` |
| `Recovery.action` is a closed Literal (dismiss/wait/retry/reauth) | `tests/test_architecture.py` |
| Overlay cannot rewrite `output_schema` | `tests/test_architecture.py` |
| Overlay `StepPatch` has no `id` / `action` fields | `tests/test_architecture.py` |
| `role_name` / `label_relative` with >1 match is ambiguous, not first-match | `tests/test_locator_ambiguity.py` |
| Draft artifact unattended replay refuses before browser open | `tests/test_lifecycle_gate.py` |
| `human_required` step escalates even with `--auto-approve-risky` | `tests/test_human_required.py` |
| `human_required` step is NOT re-executed after operator resume | `tests/test_human_required.py` |
| Shape-based redaction catches SSN/card/email/token, is idempotent | `tests/test_safety_hardening.py` |
| Operator console Basic Auth rejects missing/bad creds | `tests/test_safety_hardening.py` |
| Early-outcome detection short-circuits the risky-step gate | `tests/test_early_outcome_detection.py` |

---

## 15. How to author a new artifact

### 15.1 Automated (discovery-driven)

```bash
cua discover \
  --goal "Look up member 12345 and read their balance" \
  --target http://127.0.0.1:5000 \
  --mock-llm \
  --out artifacts/lookup_member_balance.v1.0.0.midwest_federal.json
```

The `Recorder` (`cua/agent/recorder.py`) captures every LLM tool call
and emits a validated `Artifact`. Starts in `approval_state: draft`.

### 15.2 By hand (author-annotated)

Copy the shape of `artifacts/lookup_member_balance.v1.0.0.acme_bancorp.json`.
Author the fields directly. Validate by loading:

```python
from cua.artifact.store import load_artifact
a = load_artifact("artifacts/your_capability.v1.0.0.your_target.json")
print(a.id, len(a.steps), [o.code for o in a.outcomes])
```

### 15.3 Add a new outcome to an existing artifact

Purely a data change. Add an entry to `"outcomes"`:

```jsonc
{
  "code": "ACCOUNT_LOCKED",
  "terminal": true,
  "detected_by": { "kind": "text_present", "text": "Account temporarily locked" },
  "extracts": []
}
```

No code change needed. The engine's outcome matcher walks the list at
every step. Callers immediately see the new code in the result contract.

### 15.4 Add a new recovery

Same pattern. Add an entry to `"recoveries"`:

```jsonc
{
  "name": "dismiss_marketing_modal",
  "trigger": { "kind": "text_present", "text": "Introducing our new dashboard!" },
  "action": "dismiss",
  "dismiss_locator": { "primary": { "by": "role_name", "role": "button", "name": "Not now" } },
  "max_attempts": 1
}
```

### 15.5 Add a new tenant via overlay

1. Author the overlay JSON (see §13 template)
2. Save as `artifacts/<name>.v<version>.<new_target>.overlay.json`
3. Verify: `cua replay --tenant <new_target> --capability <name> --inputs '...'`

The loader auto-resolves the base from `based_on` + directory
convention.

---

## 16. Full annotated example

See:
- `artifacts/open_sub_account.v1.0.0.midwest_federal.json` — full base artifact, 4 outcomes, 1 recovery, 1 side effect, 4 steps including a risky one
- `artifacts/open_sub_account.v1.0.0.summit_credit_union.overlay.json` — minimal overlay with 2 step patches
- `artifacts/open_sub_account.v1.0.0.acme_bancorp.json` — second-vendor base with structurally different steps
- `artifacts/open_sub_account.v1.0.0.midwest_with_2fa.json` — same as base but s4 marked `human_required: true`
- `artifacts/lookup_member_balance.v1.0.0.midwest_with_reauth.json` — reauth recovery demo

Every one of these ships in the repo and is exercised by the golden
eval suite (`evals/replay.json`, `cua eval`).
