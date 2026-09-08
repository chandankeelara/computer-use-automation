"""FastAPI operator console for human handoff.

Contract:
  GET  /status          -> current session state + intervention payload
  GET  /screenshot      -> latest failure screenshot
  POST /take-control    -> transitions paused -> human
  POST /resume          -> body {decision, force?}, transitions human -> agent
                          Refused (409) if no human action recorded AND force!=true.
  POST /abort           -> gives up; run reports outcome=ESCALATED_UNRESOLVED

The console runs in a background thread inside the replay process (no
separate service to start). The paused replay thread waits on a
threading.Event; POST /resume sets it. On resume the engine re-verifies
the current step's expectation before continuing (see engine.py).

Design intent: this is a *thin but real* operator surface. The transport,
the state machine, and the resume gate are all the shape a production
co-browsing console would have. What's stubbed is the UI polish and the
video streaming.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger(__name__)


@dataclass
class ConsoleState:
    payload: dict[str, Any] = field(default_factory=dict)
    resume_event: threading.Event = field(default_factory=threading.Event)
    decision: Optional[str] = None          # "approve" | "decline"
    forced: bool = False
    aborted: bool = False
    controller: Any = None                  # SessionController (avoid cycle)


class OperatorConsole:
    def __init__(self, port: int = 8765):
        self.port = port
        self.state = ConsoleState()
        self._server_thread: Optional[threading.Thread] = None
        self._app = None

    # -- lifecycle -------------------------------------------------------
    def start(self, controller) -> None:
        self.state.controller = controller
        try:
            from fastapi import Depends, FastAPI, HTTPException, Request, Response
            from fastapi.responses import FileResponse
            from fastapi.security import HTTPBasic, HTTPBasicCredentials
            import secrets as _secrets
            import uvicorn
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "operator console requires 'fastapi' and 'uvicorn' — "
                "install with `pip install fastapi uvicorn`"
            ) from e

        # Basic Auth: whoever can reach this port can approve an
        # irreversible financial action, so requiring credentials is
        # baseline safety even for a "just a demo" console. Credentials
        # come from env vars CUA_OPERATOR_USER / CUA_OPERATOR_PASS.
        # If EITHER is unset the console runs *open* — same as before,
        # but the startup log warns clearly. This is deliberately
        # opt-in-to-secure because forcing creds on the demo would
        # gate the smoke test behind two env vars.
        op_user = os.environ.get("CUA_OPERATOR_USER")
        op_pass = os.environ.get("CUA_OPERATOR_PASS")
        auth_enabled = bool(op_user and op_pass)
        if not auth_enabled:
            log.warning(
                "operator console starting WITHOUT Basic Auth. "
                "Set CUA_OPERATOR_USER + CUA_OPERATOR_PASS to enable."
            )
        _basic = HTTPBasic(auto_error=False)

        def _require_auth(creds: Optional[HTTPBasicCredentials] = Depends(_basic)):
            if not auth_enabled:
                return
            if creds is None:
                raise HTTPException(401, "auth required", headers={"WWW-Authenticate": "Basic"})
            ok_u = _secrets.compare_digest(creds.username or "", op_user or "")
            ok_p = _secrets.compare_digest(creds.password or "", op_pass or "")
            if not (ok_u and ok_p):
                raise HTTPException(401, "bad credentials", headers={"WWW-Authenticate": "Basic"})

        app = FastAPI(title="CUA Operator Console")
        state = self.state

        @app.get("/status", dependencies=[Depends(_require_auth)] if auth_enabled else [])
        def status():
            ctrl = state.controller
            return {
                "state": ctrl.state if ctrl else "unknown",
                "human_actions_recorded": getattr(ctrl, "human_actions_recorded", 0),
                "payload": state.payload,
            }

        @app.get("/screenshot", dependencies=[Depends(_require_auth)] if auth_enabled else [])
        def screenshot():
            p = state.payload.get("screenshot_path")
            if not p or not os.path.exists(p):
                raise HTTPException(404, "no screenshot")
            return FileResponse(p, media_type="image/png")

        @app.post("/take-control", dependencies=[Depends(_require_auth)] if auth_enabled else [])
        def take_control():
            ctrl = state.controller
            try:
                ctrl.take_control(actor="operator")
            except Exception as e:
                raise HTTPException(409, str(e))
            return {"state": ctrl.state}

        @app.post("/resume", dependencies=[Depends(_require_auth)] if auth_enabled else [])
        def resume(body: dict):
            ctrl = state.controller
            decision = body.get("decision", "approve")
            force = bool(body.get("force", False))
            try:
                ctrl.resume(force=force, actor="operator")
            except Exception as e:
                raise HTTPException(409, str(e))
            state.decision = decision
            state.forced = force
            state.resume_event.set()
            return {"state": ctrl.state, "decision": decision, "forced": force}

        @app.post("/abort", dependencies=[Depends(_require_auth)] if auth_enabled else [])
        def abort():
            state.aborted = True
            state.decision = "decline"
            state.resume_event.set()
            return {"aborted": True}

        @app.get("/", dependencies=[Depends(_require_auth)] if auth_enabled else [])
        def index():
            # Minimal operator UI — one page. Real system would be React.
            ctrl = state.controller
            return Response(_INDEX_HTML.format(
                state=ctrl.state if ctrl else "unknown",
                actions=getattr(ctrl, "human_actions_recorded", 0),
                payload_json=_json_pretty(state.payload),
            ), media_type="text/html")

        self._app = app

        def _serve():
            import uvicorn
            uvicorn.run(app, host="127.0.0.1", port=self.port, log_level="warning")

        self._server_thread = threading.Thread(target=_serve, daemon=True)
        self._server_thread.start()
        # Give the server a moment to bind.
        time.sleep(0.4)
        log.info("operator console listening on http://127.0.0.1:%s/", self.port)

    # -- driver-side API -------------------------------------------------
    def publish(self, payload: dict[str, Any]) -> None:
        self.state.payload = payload
        self.state.resume_event.clear()
        self.state.aborted = False
        self.state.decision = None

    def wait_for_resume(self, timeout: Optional[float] = None) -> dict[str, Any]:
        signaled = self.state.resume_event.wait(timeout=timeout)
        return {
            "signaled": signaled,
            "aborted": self.state.aborted,
            "decision": self.state.decision,
            "forced": self.state.forced,
        }


def _json_pretty(obj) -> str:
    import json
    try:
        return json.dumps(obj, indent=2)
    except Exception:
        return str(obj)


_INDEX_HTML = """<!doctype html>
<html><head><title>CUA Operator Console</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:900px;margin:20px auto;padding:0 20px}}
h1{{margin-bottom:0}}
.state{{padding:4px 10px;border-radius:4px;display:inline-block;font-weight:bold}}
.state-agent{{background:#dfd;color:#040}}
.state-paused{{background:#ffd;color:#640}}
.state-human{{background:#fdd;color:#600}}
pre{{background:#f4f4f4;padding:10px;border-radius:4px;overflow:auto}}
button{{padding:8px 14px;margin-right:8px;font-size:14px;cursor:pointer}}
img{{max-width:100%;border:1px solid #ccc;margin-top:10px}}
</style></head><body>
<h1>CUA Operator Console</h1>
<p>Session state: <span class="state state-{state}">{state}</span>
&nbsp; Human actions recorded: <b>{actions}</b></p>
<p>
  <button onclick="fetch('/take-control',{{method:'POST'}}).then(_=>location.reload())">Take control</button>
  <button onclick="fetch('/resume',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{decision:'approve'}})}}).then(r=>r.json()).then(j=>alert(JSON.stringify(j))).then(_=>location.reload())">Resume (approve)</button>
  <button onclick="fetch('/resume',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{decision:'approve',force:true}})}}).then(r=>r.json()).then(j=>alert(JSON.stringify(j))).then(_=>location.reload())">Resume (force)</button>
  <button onclick="fetch('/abort',{{method:'POST'}}).then(_=>location.reload())">Abort</button>
</p>
<h3>Intervention payload</h3>
<pre>{payload_json}</pre>
<h3>Live screenshot at pause</h3>
<img src="/screenshot?_={actions}" onerror="this.style.display='none'"/>
</body></html>"""
