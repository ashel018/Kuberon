# Kuberon

Kuberon is a local-first AI workspace for Kubernetes troubleshooting. It combines a FastAPI backend, a streaming React frontend, runbook-guided diagnostics, safe fix previews, and optional local cluster fault injection for demos and testing.

## Overview

- AI chat interface for Kubernetes investigation
- FastAPI backend with REST and WebSocket endpoints
- Tool-driven diagnostics using `kubectl` and optional Prometheus queries
- Runbook retrieval from local markdown files
- Fix suggestion, preview, apply, and verify workflow
- Session memory with in-memory fallback and optional Redis

## Project Structure

- `agent/` core orchestration, LLM routing, tools, runbooks, memory, and fix logic
- `api/` FastAPI application and WebSocket chat endpoint
- `frontend/` Vite + React app for the Kuberon UI
- `cluster/` local kind config and fault injection helpers
- `runbooks/` markdown troubleshooting guides
- `logs/` structured runtime logs

## How It Works

1. The frontend sends a question through WebSocket.
2. The backend builds context from recent session memory, runbooks, and tool outputs.
3. The agent runs a graph-style flow:
   `intent -> retrieve -> plan -> execute -> reason -> respond`
4. Responses stream back into the UI in real time.
5. If a fix is available, Kuberon can preview it before any live apply.

## Requirements

- Python 3.11+ recommended
- Node.js + npm
- Optional:
  - `kubectl`
  - `kind`
  - `helm`
  - Redis
  - Prometheus
  - Anthropic API key or local Ollama

## Backend Setup

From the project root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn api.main:app --port 8000
```

Backend health check:

```text
http://127.0.0.1:8000/health
```

OpenAPI docs:

```text
http://127.0.0.1:8000/docs
```

## Frontend Setup

In PowerShell, use `npm.cmd`:

```powershell
cd frontend
npm.cmd install
npm.cmd run dev
```

Frontend runs at:

```text
http://localhost:5173
```

## Run Both Together

Use 2 terminals.

Terminal 1:

```powershell
cd c:\Users\Acer\OneDrive\Desktop\PROJECTS\kubeops-agent
.\.venv\Scripts\python.exe -m uvicorn api.main:app --port 8000
```

Terminal 2:

```powershell
cd c:\Users\Acer\OneDrive\Desktop\PROJECTS\kubeops-agent\frontend
npm.cmd run dev
```

## Optional Environment Variables

```powershell
$env:ANTHROPIC_API_KEY="..."
$env:ANTHROPIC_MODEL="claude-3-7-sonnet-latest"
$env:OLLAMA_BASE_URL="http://localhost:11434"
$env:OLLAMA_MODEL="mistral"
$env:REDIS_URL="redis://localhost:6379/0"
$env:PROMETHEUS_URL="http://localhost:9090"
```

## AI Model Routing

Kuberon supports:

- Anthropic if `ANTHROPIC_API_KEY` is configured
- Ollama if available locally
- deterministic fallback mode if no external model is configured

This means the app still runs even without cloud AI credentials, but the reasoning quality will be more limited.

## Cluster Setup with kind

```powershell
kind create cluster --name kubeops --config .\cluster\kind-config.yaml
kubectl apply -f https://raw.githubusercontent.com/GoogleCloudPlatform/microservices-demo/main/release/kubernetes-manifests.yaml
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm install kube-prometheus prometheus-community/kube-prometheus-stack
```

## Fault Injection

Examples:

```powershell
.\.venv\Scripts\python.exe .\cluster\faults.py --namespace default --fault crashloop --target deployment/cartservice
.\.venv\Scripts\python.exe .\cluster\faults.py --namespace default --fault svc_mismatch --target service/cartservice
```

Use `--dry-run` first if you want to preview changes.

## Available API Endpoints

- `GET /health`
- `GET /api/sessions`
- `GET /api/sessions/{session_id}/history`
- `GET /api/cluster/snapshot`
- `GET /api/runbooks/search`
- `GET /api/fixes/suggest`
- `POST /api/fixes/apply`
- `POST /api/fixes/verify`
- `WS /ws/chat`

## Current Behavior

- If Redis is not available, memory falls back to in-process storage
- If Anthropic or Ollama is unavailable, the app falls back to heuristic reasoning
- If Prometheus is unavailable, metrics queries return a diagnostic message instead of crashing
- Fixes are preview-first before live application
- Verification is available after apply

## Notes

- The current login/account UI is frontend-level product styling, not secure backend authentication
- Some UI controls are intentionally placeholder interactions until deeper backend support is added
- The current app is optimized for local development and demo workflows

## Next Logical Upgrades

- real authentication and user storage
- persistent database-backed sessions
- richer model selection and provider configuration
- real file upload processing
- real voice input
- multi-user or shared incident workspaces
