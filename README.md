# FIDAA -- **F**ach**i**nformation **D**igitale **A**ufsuchende **A**rbeit

Chainlit Agent für Information Retrieval im Chat-Format aus einer vorgegebenen, geprüften Wissensdatenbank.
Verwendet OpenAI-kompatibles Backend für Chat-Streaming (mit Thinking & Tooluse).
Wissenszugriff übernimmt der FIDAA-Knowledge-Server (MCP, Git-Submodul [`fidaa/`](fidaa)):
die Tools `search_context` / `search_bibliography` (+ optional `search_documents`)
mit In-Memory-Vektorindex, der bei jedem Start neu aufgebaut wird.
Containerisiert mit Docker.

## Project

FIDAA ist das wissensbasierte KI-Tool des von der VolkswagenStiftung geförderten
Forschungs- und Transferprojekts [DEMO-WORK](https://demo-work.h2.de) – einer Kooperation
zwischen der Hochschule Magdeburg-Stendal, der Amadeu Antonio Stiftung und der
Katholischen Hochschule Nordrhein-Westfalen (katho), assoziiert mit dem Institut für
demokratische Kultur (IdK) der Hochschule Magdeburg-Stendal.

DEMO-WORK sammelt und systematisiert das verstreute Fach- und Praxiswissen der digitalen
Radikalisierungsprävention (Digital Streework) und macht es über FIDAA zugänglich.
Projektinformationen, das Team und der Kontakt (Impressum) sind auf der Projektseite
veröffentlicht: <https://demo-work.h2.de> (Englisch: <https://demo-work.h2.de/en/>)

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

Siehe [`secrets.env.example`](secrets.env.example) für Einstellungen durch Environment Variablen.
Siehe [`fidaa/knowledge`](fidaa/knowledge) (im Submodul, eigenes Repo) für die
Markdown-Dateien, die Wissen und Verhalten des Chatbots steuern.

## Admin-Panel

Ein eigenes, kleines Web-Panel ([`admin.py`](admin.py), ohne LLM) zum Anlegen von
Testnutzern, von Caddy unter `https://<host>/admin-<ADMIN_SALT>` geroutet.

* `ADMIN_SALT` in `secrets.env`: zufälliger Pfad-Suffix als URL-Versteckung
  (z. B. `7493` → `https://<host>/admin-7493`). Leer → Panel deaktiviert.
  Falsche Pfade (`/admin`, `/admin-1234`, …) zeigen eine identische,
  aber nicht-funktionsfähige Tarn-Login-Page – für Angreifer nicht
  unterscheidbar vom echten Panel.
* `ADMIN_USERS` in `secrets.env`: wer sich anmelden darf (gleiche
  `E-Mail:Passwort`-Form wie `SEED_USERS`, wird wie diese beim Start angelegt).
* `ADMIN_SESSION_SECRET` in `secrets.env`: HMAC-Schlüssel für das
  Session-Cookie.
* Im Panel E-Mail-Adressen einfügen (eine pro Zeile), optional ein
  gemeinsames Passwort setzen (leer = zufällig pro Nutzer, einmalig angezeigt).
  Angelegte Nutzer können sich direkt im Chat anmelden.

## License

Dieses Projekt benutzt ein Dual-Lizenz-Modell, um Software- und Textinhalte mit entsprechend angemessenen Lizenzen zu versehen.

| Component                                                        | License          | File                                   |
|------------------------------------------------------------------|------------------|----------------------------------------|
| Source code (`app.py`, `Dockerfile`, `docker-compose.yml`, etc.) | **EUPL 1.2**     | [LICENSE](LICENSE)                     |
| Knowledge content (`fidaa/knowledge/*.md`)                       | **CC BY-SA 4.0** | [fidaa/knowledge/LICENSE](fidaa/knowledge/LICENSE) |

*Both licenses are open source (OSI-approved) and permit commercial use with attribution. The EUPL closes the SaaS loophole. Modified versions hosted as a service must remain open source.*
