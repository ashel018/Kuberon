# Build Notes

## What is working

- Streaming WebSocket chat from the browser to the backend.
- Tool transparency with per-step execution records.
- Structured run logging to both JSONL and SQLite.
- Session persistence abstraction with Redis fallback.
- Fault injection entry points for five common Kubernetes failure modes.
- Local runbook retrieval that enriches reasoning without adding a full vector store yet.
- Safe fix suggestion and patch preview flow for operator confirmation.
- Explicit confirmation-gated live patch application from the UI.
- Post-apply verification that checks rollout status and fresh pod state.
- Graph-style orchestration with explicit intent, retrieve, plan, execute, reason, and respond stages.
- Section-scored runbook retrieval instead of simple keyword-only matching.

## What is intentionally staged

- LangGraph is still not a direct dependency yet, but the code is now structured as explicit graph nodes and state transitions so the swap-in path is straightforward.
- LLM routing supports Claude and Ollama configuration, but falls back locally when those services are not configured.
- Runbook RAG is bootstrapped by loading markdown content directly; a vector store can be added next without refactoring the API contract.
- Fix application is intentionally conservative and preview-first, which is the right safety posture before adding one-click remediation.
- Verification is command-driven today; deeper health checks and SLO-aware validation would be a good next upgrade.

## Recommended next iteration

1. Swap the explicit local graph implementation to real LangGraph primitives if you want framework-native orchestration.
2. Add ChromaDB embeddings for the `runbooks/` directory and attach retrieved snippets to reasoning.
3. Add HTTP or service-level health probes so verification tests actual user-facing recovery, not only Kubernetes state.
4. Add authentication and RBAC-aware tool constraints before exposing this outside a demo environment.
