# AGENTS.md

Instructions for AI coding agents working in this repository.

## Project

FIDAA-DEMO: Chainlit agent app (Python 3.13, uv, Docker Compose) in front of an
OpenAI-compatible, self-hosted LLM API. Knowledge access is NOT embedded in
this app: the FIDAA knowledge server (git submodule `fidaa/`, compose service
`fidaa`) serves the knowledge base as MCP tools (`search_context`,
`search_bibliography`, optionally `search_documents`) with an in-memory
vector index that is rebuilt at every boot. `app.py` activates that
connection at startup (D1.5 backend activation, Chainlit 2.12-native MCP
surface) and dispatches tool calls generically. There is no LangChain and no
PGVector in this repo.

## Commands

- Submodule: after clone or pull, `git submodule update --init fidaa`
  (the app reads `fidaa/knowledge/*.md`; the `fidaa` compose service builds
  from the submodule).
- Dependencies: `uv sync` (the repo ships a `.venv`)
- Local run: `cp secrets.env.example secrets.env`, fill it in, then
  `docker compose up -d`
- App logs: `docker compose logs -f app`
- Knowledge-server logs: `docker compose logs -f fidaa`
- No test suite exists. Verify changes by starting the app and checking the
  startup log (MCP connection + tool count, model capability check) plus a
  manual chat test.

## Conventions

- `secrets.env` (gitignored) is the single source of truth for secrets
  (`LLM_URL`, `LLM_KEY`, `EMBEDDING_MODEL`, `SEED_USERS`, ...). It is shared
  by BOTH the `app` and the `fidaa` service (each reads what it needs):
  the app uses LLM_URL/LLM_KEY for chat, the fidaa service uses
  LLM_URL/LLM_KEY/EMBEDDING_MODEL for embeddings.
  Do NOT re-add conflicting `${VAR:-default}` entries to the `environment:`
  section of `docker-compose.yml` — in Compose, `environment:` overrides
  `env_file:`, so a host-side default silently shadows `secrets.env`.
- The knowledge base (content files + MCP server + AGENTS.md + skill) lives
  in the git submodule `fidaa/` (the FIDAA repo). Content or server changes:
  make and commit them inside `fidaa/` first, then update the submodule
  pointer in this repo. Never hand-edit `fidaa/` and commit the edits here.
- `app.py` reads `fidaa/knowledge/systemprompt.md` (system prompt) and
  `fidaa/knowledge/prompts.md` (chat starters) directly; the retrievable
  content (kontext.md, bibliography.md) is indexed only by the fidaa server.
- The LLM API is OpenAI-compatible but exposes a custom `/model/info`
  endpoint; `_fetch_models()` in `app.py` filters it for chat + function
  calling + reasoning. Keep the two in sync if the API changes.
- The embedding model has an 8192-token context window: markdown sections in
  the knowledge files longer than that make the embeddings call fail with
  HTTP 400 (in the fidaa service). When restructuring `fidaa/knowledge/*.md`,
  keep chapters under that limit.
- Chainlit is pinned to 2.12.0 and the MCP config in
  `.chainlit/config.toml` uses the 2.12-standard `[[features.mcp.servers]]`
  block. The pre-2.12 legacy keys (`[features.mcp.sse]` etc.) are rejected
  loudly by this version — do not reintroduce them.
- The `app` service starts only after the `fidaa` service is healthy
  (`/healthz` = index built). If `fidaa` is unhealthy, check
  `docker compose logs fidaa` first.
- The variant with a VPN sidecar (app reaches the LLM API through a
  GlobalProtect tunnel) is archived on branch `legacy-vpn-sidecar` of
  this repo. Do not reintroduce proxy/VPN handling into `main`.

## Change workflow (required)

- For major changes, always propose a structured plan to the user (maintainer). 
  Explain what changes should be made, what's their purpose and how it will be
  kept minimal, to avoid an explosion in complexity.
- Before implementing big parts of new functionality, check online, if you can
  find well-known and widely used libraries that already could fulfill that 
  purpose. Evaluate the libraries against each other and against potential 
  custom code. Present the finding to the user/maintainer and support them 
  making an informed decision, before proceeding with implementation.
- For every code change, add a short comment at the changed location that
  documents its purpose (what it does and why).
- Show the diff to the maintainer and wait for review. Never `git push` to
  `origin` before the maintainer has approved the change.
