"""ACME Bancorp Servicing Portal — a second literal target.

This is a *different vendor product* from `target_app/` (Midwest Federal
Servicing Console). Same domain (member lookup + open sub-account), but:
  * Wizard-style flow: /wizard/step-1 (lookup) -> /wizard/step-2 (review)
    -> /wizard/step-3/confirm (result)
  * Uses proper <label for=""> semantic markup — no anchor games needed
  * Uses <button> elements with data-role attributes instead of
    submit inputs
  * Members are stored under different IDs (deliberately: proves the
    input schema is portable, not the members)
  * Different confirmation URL shape (`/wizard/step-3/confirm?id=...`)

Point: `open_sub_account.v1.0.0.acme_bancorp.json` and
`open_sub_account.v1.0.0.midwest_federal.json` are TWO distinct artifacts
under the same capability name+version. Same input_schema, same
output_schema, structurally different steps. The identity triple
`(name, version, target)` is what makes this coherent for callers — an
AI agent asks for "open_sub_account for acme_bancorp" and gets the right
runner without knowing anything about the vendor UI.

Runs on http://127.0.0.1:5001 by default.
"""
from __future__ import annotations

import random
from flask import Flask, abort, redirect, render_template, request, url_for

app = Flask(__name__)

# Deliberately different member set from target_app/ — proves the input
# schema (typed member_id string) is portable, not the specific values.
MEMBERS: dict[str, dict] = {
    "A-1042": {"name": "Chris Alvarez", "balance": "8,240.10"},
    "A-9001": {"name": "Suspended Member", "balance": "0.00", "restricted": True},
}
SUB_ACCOUNTS: dict[str, list[str]] = {}


def _new_sub_id() -> str:
    return "SA" + "".join(str(random.randint(0, 9)) for _ in range(8))


@app.route("/")
def index():
    return redirect(url_for("step_1"))


@app.route("/wizard/step-1", methods=["GET", "POST"])
def step_1():
    if request.method == "POST":
        mid = (request.form.get("member") or "").strip()
        if not mid:
            return render_template("acme_step1.html", err="Member ID is required."), 400
        if mid.startswith(" ") or " " in mid:
            return render_template("acme_step1.html", err="Invalid member id format."), 400
        if mid not in MEMBERS:
            return render_template("acme_not_found.html", mid=mid), 404
        if MEMBERS[mid].get("restricted"):
            return render_template("acme_denied.html", mid=mid), 403
        return redirect(url_for("step_2", mid=mid))
    return render_template("acme_step1.html")


@app.route("/wizard/step-2")
def step_2():
    mid = request.args.get("mid", "")
    if mid not in MEMBERS:
        abort(404)
    return render_template("acme_step2.html", mid=mid, member=MEMBERS[mid])


@app.route("/wizard/step-3/open", methods=["POST"])
def step_3_open():
    mid = request.form.get("mid", "")
    if mid not in MEMBERS:
        abort(404)
    if MEMBERS[mid].get("restricted"):
        return render_template("acme_denied.html", mid=mid), 403
    sub = _new_sub_id()
    SUB_ACCOUNTS.setdefault(mid, []).append(sub)
    return redirect(url_for("step_3_confirm", mid=mid, id=sub))


@app.route("/wizard/step-3/confirm")
def step_3_confirm():
    mid = request.args.get("mid", "")
    sub = request.args.get("id", "")
    return render_template("acme_confirm.html", mid=mid, sub=sub)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=True)
