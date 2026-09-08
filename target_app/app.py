"""Stand-in for a legacy bank back-office app.

Intentionally hostile-ish: table-based layout, no test IDs, no semantic class
names, full-page postbacks. Just enough to exercise the interesting parts of
the discovery / artifact / replay loop.
"""
from __future__ import annotations

import random
from flask import Flask, render_template, request, redirect, url_for, abort

app = Flask(__name__)

MEMBERS: dict[str, dict] = {
    "12345": {"name": "Jane Doe", "savings_balance": "1,234.56"},
}

SUB_ACCOUNTS: dict[str, list[str]] = {}


def _new_sub_account_number() -> str:
    return "SA" + "".join(str(random.randint(0, 9)) for _ in range(8))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/search", methods=["POST"])
def search():
    member_id = (request.form.get("q") or "").strip()
    if not member_id:
        return render_template("index.html", error="Enter a member ID.")
    if member_id not in MEMBERS:
        return render_template("not_found.html", member_id=member_id), 404
    return redirect(url_for("member_detail", member_id=member_id))


@app.route("/member/<member_id>")
def member_detail(member_id: str):
    m = MEMBERS.get(member_id)
    if not m:
        return render_template("not_found.html", member_id=member_id), 404
    return render_template("member.html", member_id=member_id, member=m)


@app.route("/member/<member_id>/open", methods=["POST"])
def open_sub_account(member_id: str):
    if member_id not in MEMBERS:
        abort(404)
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
