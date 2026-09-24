# SPDX-License-Identifier: EUPL-1.2-only
# Copyright (C) 2026 FIDAA contributors
# SPDX-FileCopyrightText: 2026 FIDAA contributors
#
# Licensed under the EUPL, Version 1.2 only (the "Licence");
# You may not use this work except in compliance with the Licence.
# You may obtain a copy of the Licence at:
#   https://eupl.eu/1.2/en/
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the Licence is distributed on an "AS IS" basis,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""FIDAA Admin-Panel – separate FastAPI app zum Anlegen von Testnutzern.

Läuft als eigener Container neben der Chat-App. Caddy reicht die ganze
/admin*-Pfadfamilie an dieses Panel weiter (siehe Caddyfile); das Panel
bedient nur /admin-<ADMIN_SALT> (z. B. /admin-7493). Alle anderen Pfade
zeigen eine identische, aber nicht-funktionsfähige Login-Seite
(Tarnfunktion: ein Angreifer bleibt daran hängen, ohne zu erfahren,
ob und wo es das Panel gibt). Der Salt aus secrets.env ist ein
URL-Versteck, kein echtes Access-Token.
Kein Chainlit und kein LLM: Nutzer anlegen ist ein deterministischer Vorgang.

Nur die in ADMIN_USERS gelisteten Accounts (E-Mail:Passwort-Paare,
gleiches Format wie SEED_USERS) können sich anmelden. Das Panel legt
Nutzer in derselben `user`-Tabelle an, die die Chat-App (app.py) nutzt
– angelegte Accounts können sich also direkt im Chat anmelden.

Run (Container): uvicorn admin:app --host 0.0.0.0 --port 8001
"""

import base64
import hashlib
import hmac
import os
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

# Geteiltes Account-Storage + Auth-Primitiven (app.py nutzt dieselben
# Tabellen): Hashing, Ratenbegrenzung und Paaren-Parsing liegen in
# accounts.py — nur noch einmal gepflegt.
from accounts import (
    MAX_PASSWORD_LENGTH,
    User,
    add_user,
    check_rate_limit,
    ensure_schema,
    parse_email_password_pairs,
    record_failed_attempt,
    record_successful_attempt,
    verify_password,
)

# ── Konfiguration (secrets.env ist die Single Source of Truth) ─────────
# E-Mail:Passwort-Paare; nur diese Accounts dürfen sich am Panel anmelden.
ADMIN_USERS = os.getenv("ADMIN_USERS", "")
# HMAC-Schlüssel für Admin-Session-Cookies.
ADMIN_SESSION_SECRET = os.getenv("ADMIN_SESSION_SECRET", "")
# Zufälliger Pfad-Suffix (URL-Versteckung): Panel-URL wird /admin-<ADMIN_SALT>,
# z. B. 7493 → /admin-7493. Leerer Salt = Panel komplett deaktiviert.
ADMIN_SALT = os.getenv("ADMIN_SALT", "")
# Sicherheits-Obergrenze pro Anfrage: niemand pastet 10k Adressen ins Panel.
MAX_USERS_PER_REQUEST = 100
SESSION_TTL_SECONDS = 12 * 60 * 60

# ── Admin-Konten ────────────────────────────────────────────────────────
def _admin_pairs() -> dict[str, str]:
    """ADMIN_USERS → {email: password} (kommagetrennte Paare wie SEED_USERS)."""
    return dict(parse_email_password_pairs(ADMIN_USERS))


def seed_admin_users() -> None:
    """Legt die Admin-Accounts bei jedem Start idempotent an (wie SEED_USERS).

    Hinweis: Liegt eine E-Mail sowohl in SEED_USERS als auch in ADMIN_USERS,
    muss das Passwort in beiden Listen identisch sein (sonst überschreiben
    sich die beiden Services beim Start gegenseitig).
    """
    for email, password in _admin_pairs().items():
        try:
            add_user(email, password)
        except ValueError:
            print(f"admin: ungültige Admin-E-Mail {email!r}, übersprungen", flush=True)
            continue
        print(f"admin: Admin {email} seeded", flush=True)


def check_credentials(email: str, password: str) -> tuple[bool, str]:
    """Login prüfen. Admin-only: nur Accounts aus ADMIN_USERS werden akzeptiert.

    Ratenbegrenzung wie in app.py, über die geteilte LoginAttempt-Tabelle
    (gemeinsame Primitiven in accounts.py).
    """
    if email not in _admin_pairs():
        return False, "Kein Admin-Account."
    allowed, remaining = check_rate_limit(email)
    if not allowed:
        return False, f"Account gesperrt. Erneut versuchen in {remaining} s."
    if not verify_password(email, password):
        record_failed_attempt(email)
        return False, "Ungültige Zugangsdaten."
    record_successful_attempt(email)
    return True, ""


# ── Session-Cookie (signiert, zustandslos) ─────────────────────────────
def _make_session_cookie(email: str) -> str:
    """Signiertes Session-Token: base64(email|Ablaufzeit).HMAC-SHA256."""
    payload = f"{email}|{int(time.time()) + SESSION_TTL_SECONDS}"
    sig = hmac.new(ADMIN_SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(payload.encode()).decode() + "." + sig


def _read_session(request: Request) -> str | None:
    """Liefert die Admin-E-Mail eines gültigen Sessions-Cookies, sonst None."""
    cookie = request.cookies.get("admin_session", "")
    payload_b64, dot, sig = cookie.partition(".")
    if not dot:
        return None
    try:
        payload = base64.urlsafe_b64decode(payload_b64.encode()).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    expected = hmac.new(ADMIN_SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    email, pipe, exp = payload.partition("|")
    # Auch ein gültig signiertes Cookie nix: nicht mehr admin / abgelaufen.
    if not pipe or int(exp) < time.time() or email not in _admin_pairs():
        return None
    return email


# ── HTML (selbstständig, ohne externe Assets) ──────────────────────────
# __PREFIX__ wird bei der Auslieferung durch /admin-<ADMIN_SALT> ersetzt,
# damit die JS-Fetch-URLs immer zum aktuellen Salt passen.
_LOGIN_HTML = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FIDAA Admin – Anmelden</title>
<style>
  body { font-family: system-ui, sans-serif; background: #f4f5f7; display: flex; justify-content: center; padding-top: 10vh; margin: 0; }
  .card { background: #fff; border-radius: 12px; box-shadow: 0 2px 12px rgba(0,0,0,.08); padding: 2rem; width: 22rem; }
  h1 { font-size: 1.2rem; margin: 0 0 1rem; }
  label { display: block; font-size: .85rem; margin: .8rem 0 .2rem; }
  input { width: 100%; box-sizing: border-box; padding: .5rem; border: 1px solid #ccc; border-radius: 6px; }
  button { margin-top: 1rem; width: 100%; padding: .6rem; border: 0; border-radius: 6px; background: #1d4ed8; color: #fff; font-size: .95rem; cursor: pointer; }
  .err { color: #b91c1c; font-size: .85rem; margin-top: .8rem; min-height: 1.2em; }
</style>
</head>
<body>
<form class="card" id="f">
  <h1>FIDAA Admin – Anmelden</h1>
  <label for="email">E-Mail</label>
  <input id="email" name="email" type="email" autocomplete="username" required>
  <label for="password">Passwort</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required>
  <button type="submit">Anmelden</button>
  <div class="err" id="err"></div>
</form>
<script>
document.getElementById("f").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = document.getElementById("err");
  err.textContent = "";
  const r = await fetch("__PREFIX__/login", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      email: document.getElementById("email").value,
      password: document.getElementById("password").value,
    }),
  });
  if (r.ok) { location.reload(); }
  else {
    const d = await r.json().catch(() => ({}));
    err.textContent = d.error || "Anmeldung fehlgeschlagen.";
  }
});
</script>
</body>
</html>
"""

_PANEL_HTML = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FIDAA Admin</title>
<style>
  body { font-family: system-ui, sans-serif; background: #f4f5f7; margin: 0; padding: 1.5rem; color: #111; }
  .wrap { max-width: 46rem; margin: 0 auto; }
  header { display: flex; justify-content: space-between; align-items: baseline; }
  h1 { font-size: 1.2rem; margin: 0; }
  .who { font-size: .85rem; color: #555; }
  .card { background: #fff; border-radius: 12px; box-shadow: 0 2px 12px rgba(0,0,0,.08); padding: 1.2rem 1.5rem; margin-top: 1rem; }
  h2 { font-size: 1rem; margin: 0 0 .6rem; }
  textarea { width: 100%; box-sizing: border-box; min-height: 8rem; padding: .5rem; border: 1px solid #ccc; border-radius: 6px; font-family: ui-monospace, monospace; font-size: .85rem; }
  input[type=password] { width: 100%; box-sizing: border-box; padding: .5rem; border: 1px solid #ccc; border-radius: 6px; }
  label { display: block; font-size: .85rem; margin: .7rem 0 .2rem; }
  button { margin-top: .9rem; padding: .55rem 1rem; border: 0; border-radius: 6px; background: #1d4ed8; color: #fff; cursor: pointer; }
  table { width: 100%; border-collapse: collapse; font-size: .85rem; margin-top: .6rem; }
  td, th { text-align: left; padding: .3rem .4rem; border-bottom: 1px solid #eee; }
  .ok { color: #15803d; } .bad { color: #b91c1c; }
  .count { font-size: .8rem; color: #555; }
  code { background: #f1f5f9; padding: 0 .3rem; border-radius: 4px; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>FIDAA Admin</h1>
    <div class="who">__EMAIL__ · <a href="#" id="logout">Abmelden</a></div>
  </header>

  <div class="card">
    <h2>Testnutzer anlegen</h2>
    <label for="emails">E-Mail-Adressen (eine pro Zeile, Kommas gehen auch)</label>
    <textarea id="emails" placeholder="tester1@example.com&#10;tester2@example.com"></textarea>
    <div class="count" id="count">0 Adressen</div>
    <label for="password">Gemeinsames Passwort (optional – leer = zufällig pro Nutzer, unten angezeigt)</label>
    <input id="password" type="password" autocomplete="new-password">
    <button id="create">Anlegen</button>
    <div id="result"></div>
  </div>

  <div class="card">
    <h2>Bestehende Nutzer <span class="count" id="allcount"></span></h2>
    <table id="all"><tr><th>E-Mail</th></tr></table>
  </div>
</div>
<script>
const $ = (id) => document.getElementById(id);
function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }
async function loadUsers() {
  const r = await fetch("__PREFIX__/users");
  if (!r.ok) { location.reload(); return; }
  const d = await r.json();
  $("allcount").textContent = "(" + d.count + ")";
  const t = $("all");
  t.innerHTML = "<tr><th>E-Mail</th></tr>";
  for (const e of d.users) t.innerHTML += "<tr><td>" + esc(e) + "</td></tr>";
}
function updateCount() {
  const n = $("emails").value.split(/[^\\w.@+-]+/).filter(Boolean).length;
  $("count").textContent = n + " Adressen";
}
$("emails").addEventListener("input", updateCount);
$("create").addEventListener("click", async () => {
  const emails = $("emails").value.split(/[^\\w.@+-]+/).filter(Boolean);
  if (!emails.length) return;
  const r = await fetch("__PREFIX__/users", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({emails, password: $("password").value}),
  });
  const d = await r.json();
  let html = "";
  if (d.error) html += '<p class="bad">' + esc(d.error) + "</p>";
  for (const c of d.created || [])
    html += '<p class="ok">✔ ' + esc(c.email) + " – Passwort: <code>" + esc(c.password) + "</code></p>";
  for (const f of d.failed || [])
    html += '<p class="bad">✘ ' + esc(f.email) + " – " + esc(f.error) + "</p>";
  $("result").innerHTML = html || '<p class="ok">Fertig.</p>';
  loadUsers();
});
$("logout").addEventListener("click", async (e) => {
  e.preventDefault();
  await fetch("__PREFIX__/logout", {method: "POST"});
  location.reload();
});
loadUsers();
updateCount();
</script>
</body>
</html>
"""


# ── FastAPI-App ─────────────────────────────────────────────────────────
# Das Panel ist nur aktiv, wenn Salt, Secret und mindestens ein Admin
# gesetzt sind; sonst werden keine Panel-Routes registriert und alle
# /admin*-Pfade laufen in dem Catch-all unten (404 ohne Informationen).
_PANEL_ENABLED = bool(ADMIN_SALT and ADMIN_SESSION_SECRET and _admin_pairs())
_PREFIX = f"/admin-{ADMIN_SALT}" if _PANEL_ENABLED else ""


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Tabellen sicherstellen (geteilt mit app.py) und – nur bei aktivem
    # Panel – die Admin-Accounts seeden.
    ensure_schema()
    if _PANEL_ENABLED:
        seed_admin_users()
    else:
        print("admin: Panel deaktiviert (ADMIN_SALT, ADMIN_USERS oder ADMIN_SESSION_SECRET unvollständig)", flush=True)
    yield


app = FastAPI(title="FIDAA Admin", lifespan=lifespan)


def panel_or_login(request: Request) -> HTMLResponse:
    """Einstiegsseite: Panel für eingeloggte Admins, sonst Login-Page."""
    email = _read_session(request)
    if not email:
        return HTMLResponse(_LOGIN_HTML.replace("__PREFIX__", _PREFIX))
    return HTMLResponse(_PANEL_HTML.replace("__EMAIL__", email).replace("__PREFIX__", _PREFIX))


async def login(request: Request) -> JSONResponse:
    data = await request.json()
    email = str(data.get("email", "")).strip()
    ok, message = check_credentials(email, str(data.get("password", "")))
    if not ok:
        return JSONResponse({"error": message}, status_code=401)
    resp = JSONResponse({"ok": True})
    # Secure: Panel läuft hinter Caddy/TLS; SameSite=Lax reicht (gleiche Origin).
    resp.set_cookie(
        "admin_session",
        _make_session_cookie(email),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=True,
    )
    return resp


def logout() -> JSONResponse:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("admin_session")
    return resp


def list_users(request: Request) -> JSONResponse:
    """Liste der existierenden E-Mails (ohne Hashes) für den Panel-Überblick."""
    if not _read_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    emails = [u.email for u in User.select(User.email).order_by(User.email)]
    return JSONResponse({"users": emails, "count": len(emails)})


async def create_users(request: Request) -> JSONResponse:
    """Testnutzer anlegen (Upsert: existierende Accounts erhalten ein neues Passwort)."""
    if not _read_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    data = await request.json()
    raw = data.get("emails")
    if not isinstance(raw, list):
        return JSONResponse({"error": "Keine E-Mail-Adressen angegeben."}, status_code=400)
    # Deduplizieren, Reihenfolge erhalten; Groß/Kleinschreibung wie in app.py nicht normalisiert.
    emails: list[str] = []
    for item in raw:
        if isinstance(item, str):
            e = item.strip()
            if e and e not in emails:
                emails.append(e)
    if not emails:
        return JSONResponse({"error": "Keine E-Mail-Adressen angegeben."}, status_code=400)
    if len(emails) > MAX_USERS_PER_REQUEST:
        return JSONResponse(
            {"error": f"Maximal {MAX_USERS_PER_REQUEST} Adressen pro Anfrage."}, status_code=400
        )
    shared = str(data.get("password") or "").strip()
    if shared and len(shared) > MAX_PASSWORD_LENGTH:
        return JSONResponse({"error": "Passwort zu lang."}, status_code=400)
    created: list[dict] = []
    failed: list[dict] = []
    for email in emails:
        password = shared or secrets.token_urlsafe(8)
        try:
            # add_user validiert die E-Mail und upsertet den bcrypt-Hash
            # (accounts.py); ungültige Adressen landen in `failed` wie vorher.
            add_user(email, password)
        except ValueError as exc:
            failed.append({"email": email, "error": str(exc)})
            continue
        created.append({"email": email, "password": password})
    return JSONResponse({"created": created, "failed": failed})


if _PANEL_ENABLED:
    # Panel-Routes erst registrieren, wenn konfiguriert – und zwar VOR dem
    # Tarn-Catch-all unten, damit die genauen Salt-Pfade Vorrang haben.
    app.get(_PREFIX)(panel_or_login)
    app.post(f"{_PREFIX}/login")(login)
    app.post(f"{_PREFIX}/logout")(logout)
    app.get(f"{_PREFIX}/users")(list_users)
    app.post(f"{_PREFIX}/users")(create_users)


@app.get("/admin{rest:path}")
def _admin_decoy(rest: str = "") -> HTMLResponse:
    """Tarn-Login-Page für alle übrigen /admin*-Pfade (Basis-/admin,
    falscher Salt, deaktiviertes Panel): sieht exakt wie die echte
    Login-Seite aus, ist aber nicht funktionsfähig – ihr Login-Versuch
    landet in _admin_decoy_login unten."""
    return HTMLResponse(_LOGIN_HTML.replace("__PREFIX__", "/admin"))


@app.post("/admin{rest:path}")
def _admin_decoy_login(rest: str = "") -> JSONResponse:
    """Begleitet die Tarn-Page: beantwortet deren Login-Versuche konstant
    mit genau der Meldung, die ein falsches Passwort am echten Panel
    auslöst – für den Angreifer damit nicht unterscheidbar."""
    return JSONResponse({"error": "Ungültige Zugangsdaten."}, status_code=401)
