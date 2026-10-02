<p align="center">
  <img src="public/logo_light.svg" width="128" alt="FIDAA Logo" />
</p>

# FIDAA -- **F**ach**i**nformation **D**igitale **A**ufsuchende **A**rbeit

[**Click here, to read this page in German**](README.md)

Chainlit agent for chat-format information retrieval from a fixed,
expert-checked knowledge base. Uses an OpenAI-compatible backend for
chat streaming (with thinking & tool use). Knowledge access is handled
by the FIDAA knowledge server (MCP, git submodule [`fidaa/`](fidaa)):
the tools `search_context` / `search_bibliography` (+ optional
`search_documents`) with an in-memory vector index that is rebuilt at
every startup. Containerized with Docker.

## Project

FIDAA is the knowledge-based AI tool of the VolkswagenStiftung-funded
research and transfer project [DEMO-WORK](https://demo-work.h2.de) — a
cooperation between the Hochschule Magdeburg-Stendal, the Amadeu
Antonio Stiftung and the Katholische Hochschule Nordrhein-Westfalen
(katho), affiliated with the Institut für demokratische Kultur (IdK) of
the Hochschule Magdeburg-Stendal.

DEMO-WORK collects and systematizes the scattered expert and practical
knowledge of digital radicalization prevention (Digital Streetwork) and
makes it accessible through FIDAA. Project information, the team and
the contact (imprint) are published on the project page:
<https://demo-work.h2.de> (German: <https://demo-work.h2.de>)

## Quickstart

```bash
# Copy secrets template
cp secrets.env.example secrets.env

# Fill out secrets.env!

# (Re-)initialize the knowledge submodule (after clone / pull)
git submodule update --init fidaa

# Start services
docker compose up -d

# Visit http://localhost:8000
```

## Configuration

See [`secrets.env.example`](secrets.env.example) for
environment-variable settings. See [`fidaa/knowledge`](fidaa/knowledge)
(in the submodule, its own repo) for the markdown files that drive the
chatbot's knowledge and behavior.

## Admin Panel

A small, dedicated web panel ([`admin.py`](admin.py), no LLM) for
creating test users, routed by Caddy under
`https://<host>/admin-<ADMIN_SALT>`.

* `ADMIN_SALT` in `secrets.env`: random path suffix as URL hiding
  (e.g. `7493` → `https://<host>/admin-7493`). Empty → panel
  disabled. Wrong paths (`/admin`, `/admin-1234`, …) show an identical
  but non-functional decoy login page — indistinguishable from the real
  panel for an attacker.
* `ADMIN_USERS` in `secrets.env`: who may log in (same
  `email:password` format as `SEED_USERS`, created at startup like
  these).
* `ADMIN_SESSION_SECRET` in `secrets.env`: HMAC key for the session
  cookie.
* In the panel, paste email addresses (one per line), optionally set a
  shared password (empty = random per user, shown once only). Created
  users can log in to the chat directly.

## License

This project uses a dual-licensing model to license software and text
content with appropriately suitable licenses.

| Component                                                        | License          | File                                   |
|------------------------------------------------------------------|------------------|----------------------------------------|
| Source code (`app.py`, `Dockerfile`, `docker-compose.yml`, etc.) | **EUPL 1.2**     | [LICENSE](LICENSE)                     |
| Knowledge content (`fidaa/knowledge/*.md`)                       | **CC BY-SA 4.0** | [fidaa/LICENSE](fidaa/LICENSE)             |

*Both licenses are open source (OSI-approved) and permit commercial use
with attribution. The EUPL closes the SaaS loophole: modified versions
hosted as a service must remain open source.*
