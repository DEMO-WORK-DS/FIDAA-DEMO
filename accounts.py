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

"""Shared account storage + auth primitives for the chat app and the admin panel.

Both containers (app.py = Chainlit, admin.py = FastAPI) talk to the same
`user` / `loginattempt` tables. All password hashing, login rate-limiting
and e-mail:password pair parsing live here so the security logic has
exactly one reviewed implementation; the entry points (app.py's auth
callback, admin.py's check_credentials) only add their own policies and
messages on top.
"""

import os
import time

import bcrypt
import peewee as pw
import playhouse.db_url as ph_url  # pyright: ignore[reportMissingTypeStubs]  # bundled with peewee, no stubs
from email_validator import EmailNotValidError, validate_email

# Same values the two entry points used to define (secrets.env remains the
# single source of truth for overrides).
MAX_PASSWORD_LENGTH = int(os.getenv("MAX_PASSWORD_LENGTH", "128"))
MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 300  # 5 minutes

# Playhouse wechselt je nach URL den Datenbank-Typ.
#  Default: Postgres via DATABASE_URL env variable
#  Fallback: Sqlite users.db file
#
# 2 Tabellen:
#   User (email + bcrypt hash passwort)
#   LoginAttempt (email + failed attempts + last attempt)
db: pw.Database = ph_url.connect(os.getenv("DATABASE_URL", "sqlite:///users.db"))


class User(pw.Model):
    email: pw.CharField = pw.CharField(primary_key=True)
    password: pw.CharField = pw.CharField()

    class Meta:
        database = db
        # Explizit: die Chat-App und das Admin-Panel teilen dieselben
        # Tabellen (Peewee-Default wäre ohnehin der kleine Klassenname).
        table_name = "user"


class LoginAttempt(pw.Model):
    email = pw.CharField(primary_key=True)
    attempts = pw.IntegerField(default=0)
    last_attempt = pw.FloatField(default=0)

    class Meta:
        database = db
        table_name = "loginattempt"


def ensure_schema() -> None:
    """Create the shared tables if missing (called by both entry points)."""
    db.create_tables([User, LoginAttempt], safe=True)


def hash_password(password: str) -> str:
    """bcrypt-Hash mit eigenem Salt pro User.

    Der Salt wird gemeinsam mit dem Hash gespeichert. Scheint so
    Industriestandard zu sein, um Rainbowtable-Attacks zu verhindern.
    """
    return bcrypt.hashpw(
        password[:MAX_PASSWORD_LENGTH].encode(), bcrypt.gensalt()
    ).decode()


def upsert_user(email: str, password: str) -> None:
    """Nutzer einfügen; existierende E-Mail erhält das neue Passwort
    (on_conflict-Update → dient gleichzeitig als Passwort-Reset)."""
    hashed = hash_password(password)
    User.insert(email=email, password=hashed).on_conflict(
        conflict_target=[User.email],
        update={User.password: hashed},
    ).execute()


def add_user(email: str, password: str):
    """Füge Nutzer zur Datenbank (z.B. Postgres) hinzu.
    on_conflict() überschreibt User, wenn bereits verfügbar. Kann also für Passwort-reset benutzt werden.

    Aufruf extern:
        docker compose exec app python -c "import app; app.add_user('david@email.de', 'password')"
    """
    try:
        vmail = validate_email(email, check_deliverability=False)
    except EmailNotValidError as exc:
        raise ValueError(str(exc)) from exc
    upsert_user(email, password)


def delete_user(email: str):
    """
    Aufruf extern:
        docker compose exec app python -c "import app; app.delete_user('email')"
    """
    User.delete().where(User.email == email).execute()
    LoginAttempt.delete().where(LoginAttempt.email == email).execute()


def check_rate_limit(email: str) -> tuple[bool, int]:
    """Returns (allowed, seconds_remaining)."""
    attempt = LoginAttempt.get_or_none(LoginAttempt.email == email)
    if not attempt:
        return True, 0
    if attempt.attempts >= MAX_LOGIN_ATTEMPTS:
        elapsed = time.time() - attempt.last_attempt
        if elapsed < LOGIN_LOCKOUT_SECONDS:
            return False, int(LOGIN_LOCKOUT_SECONDS - elapsed)
        attempt.delete_instance()
    return True, 0


def record_failed_attempt(email: str):
    now = time.time()
    LoginAttempt.insert(email=email, attempts=1, last_attempt=now).on_conflict(
        conflict_target=[LoginAttempt.email],
        update={
            LoginAttempt.attempts: LoginAttempt.attempts + 1,
            LoginAttempt.last_attempt: now,
        },
    ).execute()


def record_successful_attempt(email: str):
    LoginAttempt.delete().where(LoginAttempt.email == email).execute()


def verify_password(email: str, password: str) -> bool:
    """True iff the e-mail exists and the bcrypt hash matches."""
    user = User.get_or_none(User.email == email)
    return bool(user) and bcrypt.checkpw(password.encode(), user.password.encode())


def parse_email_password_pairs(raw: str) -> list[tuple[str, str]]:
    """'email:pass,email2:pass2' → [(email, pass), …] — stripped; parts
    without a ':' are skipped (SEED_USERS / ADMIN_USERS format)."""
    out: list[tuple[str, str]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        email, _, password = part.partition(":")
        out.append((email.strip(), password.strip()))
    return out
