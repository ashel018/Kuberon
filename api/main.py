from __future__ import annotations

import json
import os
from pathlib import Path
from datetime import datetime
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agent.graph import OpsAssistant


PROJECT_ROOT = Path(__file__).resolve().parent.parent
assistant = OpsAssistant(PROJECT_ROOT)
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_REDIRECT_URI = (os.getenv("GOOGLE_REDIRECT_URI") or os.getenv("REDIRECT_URI") or "http://127.0.0.1:8000/api/auth/google/callback").strip()
FRONTEND_URL = (os.getenv("FRONTEND_URL") or "http://localhost:5173").strip()
_google_auth_states: dict[str, str] = {}


def utcnow() -> str:
    return datetime.utcnow().isoformat()


def normalize_url(value: str) -> str:
    return value.rstrip("/")


GOOGLE_REDIRECT_URI = normalize_url(GOOGLE_REDIRECT_URI)
FRONTEND_URL = normalize_url(FRONTEND_URL)

app = FastAPI(title="KubeOps Agent API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class FixRequest(BaseModel):
    resource: str
    namespace: str
    patch: dict
    dry_run: bool = True
    confirmed: bool = False


class AuthRequest(BaseModel):
    name: str | None = None
    email: str
    password: str


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/")
async def root() -> dict:
    return {
        "name": "Kuberon API",
        "status": "ok",
        "message": "Backend is running. Use /health for a health check or connect through the frontend on port 5173.",
        "frontend": FRONTEND_URL,
        "websocket": "/ws/chat",
    }


@app.get("/api/auth/google/start")
async def google_auth_start(frontend_redirect: str = Query(default=FRONTEND_URL)):
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(
            status_code=500,
            detail="Google auth is not configured. Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and GOOGLE_REDIRECT_URI (or REDIRECT_URI).",
        )
    frontend_redirect = normalize_url(frontend_redirect or FRONTEND_URL)
    parsed = urlsplit(frontend_redirect)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="frontend_redirect must be a valid absolute URL.")
    state = str(uuid4())
    _google_auth_states[state] = frontend_redirect
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    return RedirectResponse(url=f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}")


@app.get("/api/auth/google/callback")
async def google_auth_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    frontend_redirect = _google_auth_states.pop(state or "", FRONTEND_URL)
    if error:
        return RedirectResponse(url=f"{frontend_redirect}?auth_error={error}")
    if not code or not state:
        return RedirectResponse(url=f"{frontend_redirect}?auth_error=missing_code")
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return RedirectResponse(url=f"{frontend_redirect}?auth_error=google_not_configured")

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            token_response = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "redirect_uri": GOOGLE_REDIRECT_URI,
                    "grant_type": "authorization_code",
                },
            )
            token_response.raise_for_status()
            access_token = token_response.json()["access_token"]
            userinfo_response = await client.get(
                "https://openidconnect.googleapis.com/v1/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            userinfo_response.raise_for_status()
            userinfo = userinfo_response.json()
    except Exception:
        return RedirectResponse(url=f"{frontend_redirect}?auth_error=google_exchange_failed")

    user = await assistant.db.get_or_create_google_user(
        google_id=userinfo.get("sub", ""),
        name=userinfo.get("name") or userinfo.get("given_name") or "Google User",
        email=userinfo.get("email", ""),
        created_at=utcnow(),
    )
    token = await assistant.db.create_auth_session(user["id"], created_at=utcnow())
    return RedirectResponse(url=f"{frontend_redirect}?auth_token={token}")


@app.post("/api/auth/signup")
async def signup(request: AuthRequest) -> dict:
    if not request.email.strip() or not request.password.strip():
        raise HTTPException(status_code=400, detail="Email and password are required.")
    try:
        user = await assistant.db.create_user(
            name=(request.name or "Kuberon User").strip() or "Kuberon User",
            email=request.email.strip(),
            password=request.password,
            created_at=utcnow(),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Account already exists or could not be created.") from exc
    token = await assistant.db.create_auth_session(user["id"], created_at=utcnow())
    return {"user": user, "token": token}


@app.post("/api/auth/signin")
async def signin(request: AuthRequest) -> dict:
    user = await assistant.db.authenticate_user(request.email.strip(), request.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    token = await assistant.db.create_auth_session(user["id"], created_at=utcnow())
    return {"user": user, "token": token}


@app.get("/api/auth/me")
async def auth_me(authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing authorization token.")
    token = authorization.removeprefix("Bearer ").strip()
    user = await assistant.db.get_user_by_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired session.")
    return {"user": user}


@app.post("/api/auth/logout")
async def logout(authorization: str | None = Header(default=None)) -> dict:
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        await assistant.db.delete_auth_session(token)
    return {"ok": True}


@app.get("/api/runbooks/search")
async def search_runbooks(query: str) -> dict:
    matches = assistant.runbooks.search(query)
    return {"items": [item.__dict__ for item in matches]}


@app.get("/api/fixes/suggest")
async def suggest_fixes(query: str, namespace: str = "default") -> dict:
    fixes = assistant.fixer.suggest(query, namespace=namespace)
    return {"items": assistant.fixer.serialize(fixes)}


@app.post("/api/fixes/apply")
async def apply_fix(request: FixRequest) -> dict:
    if not request.dry_run and not request.confirmed:
        return {
            "ok": False,
            "dry_run": False,
            "phase": "apply",
            "command": "",
            "output": "Apply requests must be explicitly confirmed.",
            "message": "Confirmation required before a live patch can be applied.",
        }
    result = assistant.fixer.apply(
        resource=request.resource,
        namespace=request.namespace,
        patch=request.patch,
        dry_run=request.dry_run,
    )
    return result


@app.post("/api/fixes/verify")
async def verify_fix(request: FixRequest) -> dict:
    result = assistant.fixer.verify(
        resource=request.resource,
        namespace=request.namespace,
    )
    return result


@app.get("/api/sessions")
async def list_sessions() -> dict:
    sessions = await assistant.memory.list_sessions()
    return {"sessions": sessions}


@app.get("/api/sessions/{session_id}/history")
async def session_history(session_id: str) -> dict:
    return {"items": await assistant.memory.get_turns(session_id, limit=20)}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str) -> dict:
    await assistant.memory.delete_session(session_id)
    return {"ok": True, "session_id": session_id}


@app.get("/api/cluster/snapshot")
async def cluster_snapshot(namespace: str = "default") -> dict:
    snapshot = await assistant.tools.snapshot(namespace=namespace)
    return {"snapshot": snapshot.__dict__ | {"workloads": [item.__dict__ for item in snapshot.workloads]}}


@app.websocket("/ws/chat")
async def chat_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    session_id = websocket.query_params.get("session_id") or str(uuid4())
    await websocket.send_json({"type": "session", "payload": {"session_id": session_id}})

    try:
        while True:
            raw = await websocket.receive_text()
            body = json.loads(raw)
            message = body["message"]
            namespace = body.get("namespace", "default")
            async for event in assistant.stream_chat(session_id=session_id, question=message, namespace=namespace):
                await websocket.send_json(event)
    except WebSocketDisconnect:
        return
