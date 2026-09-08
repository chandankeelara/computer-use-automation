"""Stand-in for a legacy bank back-office app.

Intentionally hostile-ish: table-based layout, no test IDs, no semantic class
names, full-page postbacks. Just enough to exercise the interesting parts of
the discovery / artifact / replay loop.

Tenant modes:
  Default = "Midwest Federal" (member id 12345 = happy path).
  `?tenant=summit` re-brands the UI to "Summit Credit Union" and rewrites
  the input label from "Member ID" to "CIF ID" and the primary action
  from "Open Sub-Account" to "Open Additional Account". This is the
  stand-in for a second bank running the same vendor product with a
  different theme. The overlay artifact target=summit_credit_union rides
  on the same underlying steps.

Additional outcomes (typed, not crashes):
  member_id 12345  -> SUCCESS (member exists)
  member_id 99999  -> MEMBER_NOT_FOUND
  member_id 55555  -> ACCESS_DENIED   (member exists but restricted)
  member_id 00000  -> VALIDATION_ERROR (server rejects the id shape)

Recovery demo:
  `?flap=1` toggles a "Session refresh recommended" interstitial that must
  be dismissed on first render. The artifact declares a recovery for this;
  replay auto-dismisses and continues. Without the recovery it would
  escalate.
"""
from __future__ import annotations

import itertools
import random
from typing import Optional

from flask import (
    Flask,
    Response,
    abort,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

app = Flask(__name__)
app.secret_key = "target-app-demo-key"

MEMBERS: dict[str, dict] = {
    "12345": {"name": "Jane Doe", "savings_balance": "1,234.56"},
    "55555": {"name": "Restricted Account", "savings_balance": "0.00", "restricted": True},
}

SUB_ACCOUNTS: dict[str, list[str]] = {}

# Toggle that alternates each request for the recovery demo.
_flap_counter = itertools.count(0)


def _tenant() -> str:
    """Read the tenant flag from query / session (query wins if present)."""
    t = request.args.get("tenant")
    if t:
        session["tenant"] = t
    return session.get("tenant", "midwest_federal")


def _tenant_labels(tenant: str) -> dict[str, str]:
    if tenant == "summit":
        return {
            "brand": "Summit Credit Union Servicing Console",
            "member_id_label": "CIF ID",
            "open_sub_label": "Open Additional Account",
        }
    return {
        "brand": "Midwest Federal Servicing Console",
        "member_id_label": "Member ID",
        "open_sub_label": "Open Sub-Account",
    }


def _new_sub_account_number() -> str:
    return "SA" + "".join(str(random.randint(0, 9)) for _ in range(8))


def _validate_id(mid: str) -> Optional[str]:
    """Return an error string if the id shape is invalid, else None."""
    if not mid:
        return "Enter a member ID."
    if not mid.isdigit():
        return "Member ID must be numeric."
    if mid == "00000":
        return "Member ID '00000' is not a valid member number."
    return None


def _should_show_flap() -> bool:
    if request.args.get("flap") == "1":
        # Show on odd renders only, so a single dismissal + retry works.
        n = next(_flap_counter)
        return n % 2 == 0
    return False


@app.before_request
def _bind_tenant():
    g.tenant = _tenant()
    g.labels = _tenant_labels(g.tenant)
    g.flap = _should_show_flap()
    # Session-timeout demo mode. `?expire=1` on any page sets a session
    # flag. On the next POST /search the app renders the "Session
    # expired" interstitial instead of a result. Clicking "Sign in"
    # (GET /login) clears the flag. This simulates the standard mid-flow
    # re-auth pattern real banking apps have — the recovery vocabulary's
    # `reauth` action is what handles it deterministically.
    # Only arm the expire flag if we've never expired in this session yet.
    # This is what makes the reauth recovery replay: first navigate arms it,
    # first POST /search consumes it, subsequent runs go through.
    if request.args.get("expire") == "1" and not session.get("_had_expired"):
        session["expire_next_search"] = True
    if request.args.get("clear_expire") == "1":
        session.pop("expire_next_search", None)
        session.pop("_had_expired", None)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/login")
def login():
    """Clears the expire flag and redirects back to the entry page."""
    session.pop("expire_next_search", None)
    return redirect(url_for("index"))


@app.route("/search", methods=["POST"])
def search():
    # Session-timeout: if flagged, render expire interstitial and clear
    # the flag on any subsequent GET /login. The flag only fires ONCE per
    # session so a reauth recovery restart succeeds on the second try.
    if session.pop("expire_next_search", False):
        session["_had_expired"] = True
        return render_template("expired.html"), 401
    # Summit renames the query parameter to "cif" (real vendor products
    # often differ in field names across white-labels). Base artifacts
    # that hard-code `name='q'` as a CSS fallback will structurally miss
    # here — the overlay must retarget or the run must degrade cleanly.
    member_id = (request.form.get("q") or request.form.get("cif") or "").strip()
    err = _validate_id(member_id)
    if err is not None:
        # Distinct URL so the outcome matcher can pick it up deterministically.
        return render_template("validation_error.html", err=err, submitted=member_id), 400
    if member_id not in MEMBERS:
        return render_template("not_found.html", member_id=member_id), 404
    if MEMBERS[member_id].get("restricted"):
        return render_template("access_denied.html", member_id=member_id), 403
    return redirect(url_for("member_detail", member_id=member_id))


@app.route("/member/<member_id>")
def member_detail(member_id: str):
    m = MEMBERS.get(member_id)
    if not m:
        return render_template("not_found.html", member_id=member_id), 404
    if m.get("restricted"):
        return render_template("access_denied.html", member_id=member_id), 403
    return render_template("member.html", member_id=member_id, member=m)


@app.route("/member/<member_id>/open", methods=["POST"])
def open_sub_account(member_id: str):
    if member_id not in MEMBERS:
        abort(404)
    if MEMBERS[member_id].get("restricted"):
        return render_template("access_denied.html", member_id=member_id), 403
    sub = _new_sub_account_number()
    SUB_ACCOUNTS.setdefault(member_id, []).append(sub)
    return redirect(url_for("confirm", member_id=member_id, sub_id=sub))


@app.route("/member/<member_id>/confirm/<sub_id>")
def confirm(member_id: str, sub_id: str):
    if member_id not in MEMBERS:
        abort(404)
    return render_template(
        "confirm.html",
        member_id=member_id,
        member=MEMBERS[member_id],
        sub_id=sub_id,
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
