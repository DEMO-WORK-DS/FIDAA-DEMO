# AGENTS.md

Instructions for AI coding agents working in this repository.

## Project

FIDAA: Chainlit RAG chat app (Python 3.13, uv, Docker Compose) in front of an
OpenAI-compatible, self-hosted LLM API. RAG via LangChain into PGVector
(Postgres); the knowledge base is `knowledge/*.md` (German content).

## Commands

- Dependencies: `uv sync` (the repo ships a `.venv`)
- Local run: `cp secrets.env.example secrets.env`, fill it in, then
  `docker compose up -d`
- App logs: `docker compose logs -f app`
- No test suite exists. Verify changes by starting the app and checking the
  startup log (RAG build, model capability check) plus a manual chat test.

## Conventions

- `secrets.env` (gitignored) is the single source of truth for secrets
  (`LLM_URL`, `LLM_KEY`, `EMBEDDING_MODEL`, `SEED_USERS`, ...).
  Do NOT re-add conflicting `${VAR:-default}` entries to the `environment:`
  section of `docker-compose.yml` — in Compose, `environment:` overrides
  `env_file:`, so a host-side default silently shadows `secrets.env`.
- The LLM API is OpenAI-compatible but exposes a custom `/model/info`
  endpoint; `_fetch_models()` in `app.py` filters it for chat + function
  calling + reasoning. Keep the two in sync if the API changes.
- The embedding model has an 8192-token context window: markdown sections
  longer than that make the embeddings call fail with HTTP 400. When
  restructuring `knowledge/*.md`, keep chapters under that limit.
- The variant with a VPN sidecar (app reaches the LLM API through a
  GlobalProtect tunnel) is archived on branch `legacy-vpn-sidecar`.
  Do not reintroduce proxy/VPN handling into `main`.

## Change workflow (required)

- For major changes, always propose a structured plan to the user (maintainer). 
  Explain what changes should be made, what's their purpose and how it will be
  kept minimal, to avaoid an explosion in complexity.
- Before implementing big parts of new functionality, check online, if you can
  find well-known and widely used libraries that allready could fulfill that 
  purpose. Evaluate the libraries against each other and against potential 
  custom code. Present the finding to the user/maintainer and support them 
  making an informed decision, before proceding with implementation.
- For every code change, add a short comment at the changed location that
  documents its purpose (what it does and why).
- Show the diff to the maintainer and wait for review. Never `git push` to
  `origin` before the maintainer has approved the change.
