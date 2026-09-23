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

"""Chainlit FIDAA Agent Client (OpenAI-native streaming + MCP knowledge server).

Run:        docker compose up
Create user: docker compose exec app python -c "import app; app.add_user('email', 'password')"
Delete user: docker compose exec app python -c "import app; app.delete_user('email')"
"""

# pyright: reportUnknownMemberType=false
# Chainlit Funktionen returnen immer Callable ohne Parameter anstatt z.B. Callable[[str, str]].
# Das regt pyright auf. Darum ignorieren.

import asyncio
import json
import logging
import os
import re
import secrets
import time
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path

import bcrypt
import chainlit as cl
import chevron
import httpx
import peewee as pw
import playhouse.db_url as ph_url  # pyright: ignore[reportMissingTypeStubs]  # bundled with peewee, no stubs
from email_validator import EmailNotValidError, validate_email
from openai import AsyncOpenAI

# FIDAA knowledge server (MCP, D1.5): backend-activated connection.
# `streamablehttp_client` is the mcp 1.x-line name (Chainlit pins mcp<2);
# the SDK v2 line renamed it to `streamable_http_client`.
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from chainlit.config import StreamableHttpMcpServer, config
from chainlit.mcp import HttpMcpConnection
from chainlit.session import McpSession, stop_mcp_task

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration — hard fail on secrets, SQLite fallback for DATABASE_URL
# ---------------------------------------------------------------------------
LLM_URL = os.environ["LLM_URL"]
LLM_KEY = os.environ["LLM_KEY"]
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "Qwen-3.8")
ASSISTANT_NAME = os.getenv("ASSISTANT_NAME", "Assistant")

if not os.environ.get("CHAINLIT_AUTH_SECRET"):
    os.environ["CHAINLIT_AUTH_SECRET"] = secrets.token_urlsafe(32)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///users.db")

# The knowledge base (system prompt, starter prompts) now lives in the FIDAA
# submodule; embeddings and retrieval run in the fidaa MCP server, so the
# EMBEDDING_MODEL / USE_EMBED_INSTRUCTIONS / DEFAULT_TASK_INSTRUCTION /
# DOCUMENTS_PATH env vars are consumed there (fidaa/secrets.env.example).
KNOWLEDGE_DIR = Path("fidaa/knowledge")

MAX_AGENT_STEPS = 7

MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 300  # 5 minutes
MAX_PASSWORD_LENGTH = int(os.getenv("MAX_PASSWORD_LENGTH", "128"))
MAX_MESSAGE_LENGTH = int(os.getenv("MAX_MESSAGE_LENGTH", "4000"))

# Instrument OpenAI
# https://docs.chainlit.io/integrations/openai
# Macht effektiv fast alles, was die App im Frontend ausmacht, bei OpenAI backend.
cl.instrument_openai()


def _make_openai_client(timeout: float = 120.0):
    """Erstelle AsyncOpenAI client."""
    return AsyncOpenAI(
        base_url=LLM_URL,
        api_key=LLM_KEY,
        timeout=httpx.Timeout(timeout, connect=10.0),
    )


# ------------------------------------------------
# Peewee Database Management
# ------------------------------------------------
# Playhouse wechselt je nach URL den Datenbank-Typ.
#  Default: Postgres via DATABASE_URL env variable
#  Fallback: Sqlite users.db file
#
# 2 Tabellen:
#   User (email + bcrypt hash passwort)
#   LoginAttempt (email + failed attempts + last attempt)

_db: pw.Database = ph_url.connect(DATABASE_URL)


class User(pw.Model):
    email: pw.CharField = pw.CharField(primary_key=True)
    password: pw.CharField = pw.CharField()

    class Meta:
        database = _db


class LoginAttempt(pw.Model):
    email = pw.CharField(primary_key=True)
    attempts = pw.IntegerField(default=0)
    last_attempt = pw.FloatField(default=0)

    class Meta:
        database = _db


def add_user(email: str, password: str):
    """Füge Nutzer zur Datenbank (z.B. Postgres) hinzu.
    on_conflict() überschreibt User, wenn bereits verfügbar. Kann also für Passwort-reset benutzt werden.

    bcrypt generiert einen eigenen Salt pro User. Der Salt wird gemeinsam mit dem Hash gespeichert.
    Scheint so industriestandard zu sein, um rainbowtable-attacks zu verhindern.

    Aufruf extern:
        docker compose exec app python -c "import app; app.add_user('david@email.de', 'password')"
    """
    try:
        vmail = validate_email(email, check_deliverability=False)
    except EmailNotValidError as exc:
        raise ValueError(str(exc)) from exc
    hashed = bcrypt.hashpw(
        password[:MAX_PASSWORD_LENGTH].encode(), bcrypt.gensalt()
    ).decode()
    User.insert(email=email, password=hashed).on_conflict(
        conflict_target=[User.email],
        update={User.password: hashed},
    ).execute()


def delete_user(email: str):
    """
    Aufruf extern:
        docker compose exec app python -c "import app; app.delete_user('email')"
    """
    User.delete().where(User.email == email).execute()
    LoginAttempt.delete().where(LoginAttempt.email == email).execute()


def _check_rate_limit(email: str) -> tuple[bool, int]:
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


def _record_failed_attempt(email: str):
    now = time.time()
    LoginAttempt.insert(email=email, attempts=1, last_attempt=now).on_conflict(
        conflict_target=[LoginAttempt.email],
        update={
            LoginAttempt.attempts: LoginAttempt.attempts + 1,
            LoginAttempt.last_attempt: now,
        },
    ).execute()


def _record_successful_attempt(email: str):
    LoginAttempt.delete().where(LoginAttempt.email == email).execute()


def login(email: str, password: str) -> tuple[bool, str]:
    """Verify credentials with rate limiting. Returns (success, error_message)."""
    if len(password) > MAX_PASSWORD_LENGTH:
        return False, "Invalid credentials"

    allowed, seconds_remaining = _check_rate_limit(email)
    if not allowed:
        return False, f"Account locked. Try again in {seconds_remaining} seconds."

    user = User.get_or_none(User.email == email)
    if not user or not bcrypt.checkpw(password.encode(), user.password.encode()):
        _record_failed_attempt(email)
        return False, "Invalid credentials"

    _record_successful_attempt(email)
    return True, ""


# ---------------------------------------------------------------------------
# FIDAA knowledge server (MCP) — D1.5 backend-activated connection
# ---------------------------------------------------------------------------
_FIDAA_MCP_NAME = "fidaa"
# The fidaa server builds its in-memory index at boot (usually well under a
# minute); give the initial MCP connection a generous budget.
_MCP_CONNECT_TIMEOUT = 60.0

# German display names for the tool steps in the UI (keyed by tool name;
# unknown tools fall back to the raw name).
_MCP_STEP_NAMES = {
    "search_context": "🔍 Kontextsuche",
    "search_bibliography": "📚 Bibliographiesuche",
    "search_documents": "📄 Dokumentensuche",
}

# Populated at startup from the MCP server's list_tools() — the server is
# the single source of truth for tool names/descriptions. Read-only after
# startup.
_tools: list[dict] = []
# The one shared MCP session for the process lifetime. Each chat registers
# a lightweight copy in its mcp_sessions dict (see
# _register_mcp_for_session), so per-chat cleanup cannot kill a connection
# other chats still use.
_mcp_shared: McpSession | None = None
# System prompt text (fidaa/knowledge/systemprompt.md), read at startup —
# the same file the MCP server serves as the fidaa_systemprompt prompt.
_system_prompt: str | None = None
# Populated at startup; read-only after that
_startup_error: str | None = None
_available_models: list[str] = []


async def _mcp_runner(
    url: str,
    headers: dict[str, str] | None,
    ready_event: asyncio.Event,
    stop_event: asyncio.Event,
    result_holder: dict,
) -> None:
    """Background task that owns the MCP transport and ClientSession.

    Mirrors Chainlit's own /mcp connect handler: the task enters all context
    managers, calls initialize(), signals ready, then blocks on stop_event
    and closes the exit stack in the same task that opened it (avoids the
    cross-task cancel-scope corruption, Chainlit issue #2182).
    """
    exit_stack = AsyncExitStack()
    try:
        try:
            transport = await exit_stack.enter_async_context(
                streamablehttp_client(url=url, headers=headers)
            )
            read, write = transport[:2]
            client = await exit_stack.enter_async_context(
                ClientSession(read, write, sampling_callback=None)
            )
            await client.initialize()
            result_holder["client"] = client
        except BaseException as exc:
            # First error wins; the outer finally always signals ready.
            result_holder.setdefault("error", exc)
            return
        finally:
            ready_event.set()

        await stop_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        try:
            await exit_stack.aclose()
        except BaseException:
            logger.debug(
                "Error closing MCP exit stack for %r", _FIDAA_MCP_NAME, exc_info=True
            )


async def _connect_fidaa_mcp() -> None:
    """Connect the shared FIDAA MCP session and build the tool schemas.

    D1.5: native, backend-activated MCP connection — same code path and
    semantics as a UI click: uses the developer config ([[features.mcp.servers]]
    in .chainlit/config.toml), wraps the ClientSession in a Chainlit
    McpSession, and fires the standard on_mcp_connect callback (tearing the
    connection down on callback failure, like the /mcp handler does).
    """
    global _tools, _mcp_shared

    if not config.features.mcp.enabled:
        raise RuntimeError(
            "features.mcp.enabled ist deaktiviert — der FIDAA-Server "
            "kann nicht verbunden werden."
        )
    server_cfg = next(
        (s for s in config.features.mcp.servers if s.name == _FIDAA_MCP_NAME),
        None,
    )
    if server_cfg is None:
        raise RuntimeError(
            f"MCP-Server {_FIDAA_MCP_NAME!r} fehlt in [[features.mcp.servers]] "
            "(.chainlit/config.toml)."
        )
    if not isinstance(server_cfg, StreamableHttpMcpServer):
        raise RuntimeError(
            f"MCP-Server {_FIDAA_MCP_NAME!r} muss type = 'streamable-http' sein."
        )

    ready_event = asyncio.Event()
    stop_event = asyncio.Event()
    result_holder: dict = {}
    task = asyncio.create_task(
        _mcp_runner(
            server_cfg.url,
            server_cfg.headers,
            ready_event,
            stop_event,
            result_holder,
        ),
        name=f"mcp-shared-{_FIDAA_MCP_NAME}",
    )

    try:
        await asyncio.wait_for(ready_event.wait(), timeout=_MCP_CONNECT_TIMEOUT)
    except asyncio.TimeoutError:
        result_holder.setdefault(
            "error",
            asyncio.TimeoutError(
                f"timed out after {_MCP_CONNECT_TIMEOUT:.0f}s waiting for "
                "the MCP connection to initialize"
            ),
        )

    if "error" in result_holder:
        # Bounded wait-then-cancel, same as the /mcp handler.
        await stop_mcp_task(task, stop_event, _FIDAA_MCP_NAME)
        raise RuntimeError(
            f"Verbindung zum FIDAA-Server fehlgeschlagen: {result_holder['error']!s}"
        ) from result_holder["error"]

    client: ClientSession = result_holder["client"]

    # Tool schemas for the OpenAI tool-calling API; the FIDAA server is the
    # single source of truth (German names/descriptions).
    tool_list = await client.list_tools()
    _tools = [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.inputSchema,
            },
        }
        for t in tool_list.tools
    ]

    _mcp_shared = McpSession(
        name=_FIDAA_MCP_NAME, client=client, task=task, stop_event=stop_event
    )

    # Fire the standard connect callback (parity with the UI connect flow).
    if config.code.on_mcp_connect:
        try:
            await config.code.on_mcp_connect(
                HttpMcpConnection(
                    name=_FIDAA_MCP_NAME,
                    url=server_cfg.url,
                    headers=server_cfg.headers,
                ),
                client,
            )
        except Exception:
            await stop_mcp_task(task, stop_event, _FIDAA_MCP_NAME)
            _mcp_shared = None
            raise

    logger.info("MCP %s connected: %d tools", _FIDAA_MCP_NAME, len(_tools))


def _register_mcp_for_session() -> None:
    """Attach the shared FIDAA MCP session to the current chat session.

    A lightweight copy is stored under the server name: on session cleanup
    (WebsocketSession.delete) close() is called on the copy, which is a
    no-op (its task is already finished), while the real shared connection
    keeps running for all other chats. Tool dispatch reads the session's
    mcp_sessions entry, so a connection the user establishes via the UI
    (POST /mcp) transparently takes precedence for that chat.
    """
    if _mcp_shared is None:
        return

    async def _noop() -> None:
        # Pre-completed task: makes McpSession.close() a no-op for the copy.
        return None

    session = cl.context.session
    session.mcp_sessions[_FIDAA_MCP_NAME] = McpSession(
        name=_FIDAA_MCP_NAME,
        client=_mcp_shared.client,
        task=asyncio.get_running_loop().create_task(_noop()),
        stop_event=asyncio.Event(),
    )


def strip_reasoning_tags(text: str) -> str:
    """Remove <thinking>...</thinking> tags from displayed text."""
    return re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL).strip()


# ---------------------------------------------------------------------------
# Model list
# ---------------------------------------------------------------------------
async def _fetch_models():
    """Fetch models usable for agentic chat (mode='chat' + function calling + reasoning)."""
    try:
        client = _make_openai_client(timeout=10.0)
        response = await client.get("/model/info", cast_to=object)
        agentic_models = [
            entry["model_name"]
            for entry in response.get("data", [])
            if (info := entry.get("model_info", {})).get("mode") == "chat"
            and info.get("supports_function_calling")
            and info.get("supports_reasoning")
        ]
        return agentic_models or [DEFAULT_MODEL]
    except Exception as e:
        logger.error("Model List not loaded")
        logger.error(e)
        return [DEFAULT_MODEL]


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------
@cl.author_rename
def rename(orig_author: str):
    """Rename the assistant in the UI."""
    if orig_author in ["Assistant", "on_message"]:
        return ASSISTANT_NAME
    return orig_author


@cl.on_stop
async def on_stop():
    """Handle user clicking the stop button."""
    cl.user_session.set("stop_requested", True)


# ---------------------------------------------------------------------------
# User seeding
# ---------------------------------------------------------------------------
def _seed_users():
    """Populate users from SEED_USERS env var. Format: email:password,email:password"""
    raw = os.getenv("SEED_USERS", "").strip()
    if not raw:
        return
    count = 0
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        email, _, password = pair.partition(":")
        email, password = email.strip(), password.strip()
        if not email or not password:
            logger.warning("Unmatched user/password pair. Not seeded.")
            continue
        try:
            add_user(email, password)
            count += 1
            logger.info("Seeded user %s", email)
        except Exception:
            logger.warning("Failed to seed user: %s", email)
    if count:
        logger.info("Seeded %d users from SEED_USERS", count)


# ---------------------------------------------------------------------------
# Session initialisation — shared by on_chat_start and on_chat_resume
# ---------------------------------------------------------------------------
async def _init_session():
    """Set up model, client, api_messages, Modes picker and transcript command."""
    model = (
        DEFAULT_MODEL
        if DEFAULT_MODEL in _available_models
        else (_available_models[0] if _available_models else DEFAULT_MODEL)
    )
    cl.user_session.set("model", model)
    cl.user_session.set("client", _make_openai_client())
    cl.user_session.set("stop_requested", False)
    cl.user_session.set("api_messages", [{"role": "system", "content": _system_prompt}])

    await cl.context.emitter.set_modes(
        [
            cl.Mode(
                id="model",
                name="Modell",
                options=[
                    cl.ModeOption(id=m, name=m, default=(m == model))
                    for m in (_available_models or [DEFAULT_MODEL])
                ],
            )
        ]
    )
    await cl.context.emitter.set_commands(
        [
            {
                "id": "transcript",
                "icon": "file-text",
                "description": "Konversation als HTML exportieren",
                "button": True,
                "persistent": True,
            }
        ]
    )

    # D1.5: attach the shared FIDAA MCP session to this chat (covers both
    # on_chat_start and on_chat_resume).
    _register_mcp_for_session()


# ---------------------------------------------------------------------------
# App startup
# ---------------------------------------------------------------------------
@cl.on_app_startup
async def startup():
    """Initialize database, FIDAA knowledge server (MCP), and model list.

    On failure, sets _startup_error so on_chat_start can surface it to users.
    The GUI still starts so users get a visible error instead of a dead server.
    """
    global _startup_error, _tools, _available_models, _system_prompt

    if not isinstance(_db, pw.PostgresqlDatabase):
        logger.warning(
            "Not using PostgreSQL — SQLite fallback active. "
            "Not recommended for production."
        )

    try:
        _db.create_tables([User, LoginAttempt], safe=True)
    except Exception:
        logger.exception("STARTUP FAILED: database initialization error")
        _startup_error = "Datenbankfehler beim Start. Bitte Administrator kontaktieren."
        return

    _seed_users()

    # FIDAA knowledge server: system prompt + tool schemas + shared MCP
    # session. Hard-fail like the old RAG build — the app is useless without
    # the knowledge base.
    try:
        _system_prompt = (KNOWLEDGE_DIR / "systemprompt.md").read_text(encoding="utf-8")
        await _connect_fidaa_mcp()
    except Exception:
        logger.exception("STARTUP FAILED: FIDAA knowledge server error")
        _startup_error = (
            "Wissensdatenbank-Server nicht erreichbar. "
            "Bitte Administrator kontaktieren."
        )
        return

    _available_models = await _fetch_models()
    if not _available_models:
        logger.error("STARTUP FAILED: no models available from LLM API")
        _startup_error = (
            "Keine Sprachmodelle verfügbar. Bitte LLM-API-Verbindung prüfen."
        )


@cl.on_app_shutdown
async def shutdown():
    """Close the shared FIDAA MCP connection at process shutdown."""
    if _mcp_shared is not None:
        await _mcp_shared.close()


# ---------------------------------------------------------------------------
# Step functions
# ---------------------------------------------------------------------------
@cl.step(type="llm", name="💭 Denkprozess…")
async def _thinking_step(message_content):
    """Stream reasoning tokens and accumulate text/tool calls from the LLM.

    cl.instrument_openai() records the API call as a nested step automatically,
    so only reasoning streaming and tool call accumulation are done manually here.
    Returns (text_acc, tool_calls_acc).
    """
    current_step = cl.context.current_step
    current_step.input = message_content

    model = cl.user_session.get("model")
    client = cl.user_session.get("client")
    api_messages = cl.user_session.get("api_messages")

    stream = await client.chat.completions.create(
        model=model,
        messages=api_messages,
        tools=_tools,
        stream=True,
    )

    text_acc = ""
    tool_calls_acc = []
    current_tool_call = None

    async for chunk in stream:
        if cl.user_session.get("stop_requested"):
            break

        delta = chunk.choices[0].delta

        if getattr(delta, "reasoning_content", None):
            await current_step.stream_token(delta.reasoning_content)

        if delta.content:
            text_acc += delta.content

        tc_list = getattr(delta, "tool_calls", None)
        if tc_list:
            for tc in tc_list:
                if tc.id:
                    current_tool_call = {
                        "id": tc.id,
                        "name": tc.function.name or "",
                        "args": tc.function.arguments or "",
                    }
                    tool_calls_acc.append(current_tool_call)
                elif current_tool_call is not None and tc.function.arguments:
                    current_tool_call["args"] += tc.function.arguments

    return text_acc, tool_calls_acc


async def _mcp_tool_step(tc: dict) -> str:
    """Run one MCP tool call and render its search step in the UI.

    Generic replacement for the old per-retriever step functions: the tool
    list comes from the FIDAA server (list_tools at startup), so any tool
    the server registers (now or later) is handled here without an app.py
    change. The MCP client is read from the chat session's mcp_sessions
    entry (standard Chainlit MCP pattern), falling back to the shared
    session.
    """
    name = tc["name"]

    raw_args = tc["args"]
    try:
        parsed_args = json.loads(raw_args)
    except Exception:
        parsed_args = {"query": raw_args}
    query = parsed_args.get("query", "")

    # Standard Chainlit MCP lookup: mcp_sessions entries unpack as
    # (client, sentinel).
    entry = cl.context.session.mcp_sessions.get(_FIDAA_MCP_NAME)
    mcp_session = entry if entry is not None else _mcp_shared
    if mcp_session is None:
        return "Fehler: Wissensdatenbank-Server nicht verbunden."
    client = mcp_session.client

    async with cl.Step(name=_MCP_STEP_NAMES.get(name, name), type="tool") as step:
        step.input = query
        try:
            result = await client.call_tool(name, parsed_args)
        except Exception:
            logger.exception("MCP tool call %r failed", name)
            step.output = "Fehler beim Abrufen des Kontexts."
            return "Fehler beim Abrufen des Kontexts."

        if getattr(result, "isError", False):
            step.output = "Fehler beim Abrufen des Kontexts."
            return "Fehler beim Abrufen des Kontexts."

        # The FIDAA server returns structured output
        # {results: [{heading_path, text}]} (parity with the old retriever
        # shape); fall back to raw text content blocks for tools that
        # don't.
        structured = getattr(result, "structuredContent", None)
        if structured and "results" in structured:
            results = structured["results"]
            result_text = "\n\n---\n\n".join(r["text"] for r in results)
            sources = []
            for i, r in enumerate(results, 1):
                heading = " > ".join(filter(None, r.get("heading_path", [])))
                sources.append(f"#### {i}. `{heading}`\n```markdown\n{r['text']}\n```")
        else:
            result_text = "\n\n---\n\n".join(
                b.text for b in result.content if getattr(b, "type", "") == "text"
            )
            sources = [result_text] if result_text else []

        step.output = (
            f"**Suchanfrage:** `{query}`\n### 🔍 Gefundene Quellen\n"
            + "\n".join(sources)
        )

    return result_text


# ---------------------------------------------------------------------------
# Promptvorschläge ("Starters")
# ---------------------------------------------------------------------------
@cl.set_starters
async def set_starters(user: cl.User | None = None):
    """Parse fidaa/knowledge/prompts.md into cl.Starter list.

    Minimal H2 split in place of MarkdownHeaderTextSplitter (langchain is
    gone): prompts.md is under our control — an H1 preamble followed by
    `## title` + body sections, no code fences.
    """
    # ignoring user-parameter, cause everyone get's the same starter
    content = (KNOWLEDGE_DIR / "prompts.md").read_text(encoding="utf-8")
    parts = re.split(r"(?m)^##\s+", content)
    starters = []
    for part in parts[1:]:  # parts[0] is the preamble (H1 + comment)
        lines = part.splitlines()
        title = lines[0].strip()
        message = "\n".join(lines[1:]).strip()
        if title and message:
            starters.append(cl.Starter(label=title, message=message))
    return starters


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
@cl.password_auth_callback
async def auth_callback(username: str, password: str) -> cl.User | None:
    success, error_message = login(username, password)
    if success:
        return cl.User(identifier=username, metadata={"email": username})
    logger.warning("Auth failed for %s: %s", username, error_message)
    return None


# ---------------------------------------------------------------------------
# HTML transcript export
# ---------------------------------------------------------------------------
_STEP_STYLES = {
    "user_message": ("👤 User", "#2563eb"),
    "assistant_message": ("🤖 FIADA", "#059669"),
    "llm": ("💭 Thinking", "#7c3aed"),
    "tool": ("🔧 Recherche", "#d97706"),
    "run": ("▶ Run", "#6b7280"),
}


def _build_export_html(thread_id: str) -> str:
    query = """
    SELECT s.type, s.name, s.input, s.output, s."createdAt",
           f.value AS fb_value, f.comment AS fb_comment
    FROM "Step" s
    LEFT JOIN "Feedback" f ON f."stepId" = s.id
    WHERE s."threadId" = %s
    ORDER BY s."startTime"
    """
    rows = _db.execute_sql(query, (thread_id,)).fetchall()

    steps = []
    for row in rows:
        step_type, step_name, inp, outp, created_at, fb_value, fb_comment = row
        label, color = _STEP_STYLES.get(step_type, (step_type or "?", "#6b7280"))
        has_feedback = fb_value is not None
        positive = has_feedback and fb_value > 0

        steps.append(
            {
                "display_name": step_name or label,
                "color": color,
                "content": outp or inp or "",
                "time": created_at.strftime("%H:%M:%S") if created_at else "",
                "has_feedback": has_feedback,
                "feedback_class": "up" if positive else "down",
                "feedback_icon": "👍" if positive else "👎",
                "feedback_comment": fb_comment or "",
            }
        )

    with open("templates/export.html", encoding="utf-8") as f:
        return chevron.render(
            f,
            {
                "exported_at": datetime.now().strftime("%d.%m.%Y um %H:%M"),
                "step_count": len(rows),
                "thread_id": thread_id,
                "steps": steps,
            },
        )


# ---------------------------------------------------------------------------
# Chat lifecycle
# ---------------------------------------------------------------------------
@cl.on_chat_start
async def on_chat_start():
    """Set up per-user session state. Startup already built the RAG and model list."""
    if _startup_error:
        await cl.ErrorMessage(content=_startup_error).send()
        return
    await _init_session()


@cl.on_chat_resume
async def on_chat_resume(thread: cl.types.ThreadDict):
    """Restore session state when opening an existing chat — no welcome message."""
    if _startup_error:
        await cl.ErrorMessage(content=_startup_error).send()
        return
    await _init_session()
    api_messages = cl.user_session.get("api_messages")
    for step in thread["steps"]:
        if step["type"] == "user_message":
            api_messages.append({"role": "user", "content": step["output"]})
        elif step["type"] == "assistant_message":
            api_messages.append({"role": "assistant", "content": step["output"]})
    cl.user_session.set("api_messages", api_messages)


@cl.on_chat_end
async def on_chat_end():
    """Clean up session data when user disconnects."""
    cl.user_session.set("api_messages", [])


# ---------------------------------------------------------------------------
# Message handler
# ---------------------------------------------------------------------------
@cl.on_message
async def on_message(message: cl.Message):
    if message.command == "transcript":
        thread_id = cl.context.session.thread_id
        html_content = await cl.make_async(_build_export_html)(thread_id)
        await cl.Message(
            content="📄 Konversation exportiert:",
            elements=[
                cl.File(
                    name=f"export_{thread_id[:8]}.html",
                    content=html_content.encode("utf-8"),
                    mime="text/html",
                )
            ],
        ).send()
        return

    if len(message.content) > MAX_MESSAGE_LENGTH:
        await cl.Message(content="Nachricht zu lang.").send()
        return

    try:
        # Sync model from Modes picker (user may change it per message)
        model = (
            (message.modes or {}).get("model")
            or cl.user_session.get("model")
            or DEFAULT_MODEL
        )
        cl.user_session.set("model", model)

        client = cl.user_session.get("client")
        if client is None:
            client = _make_openai_client()
            cl.user_session.set("client", client)

        api_messages = cl.user_session.get("api_messages")
        if api_messages is None:
            api_messages = [{"role": "system", "content": _system_prompt}]
            cl.user_session.set("api_messages", api_messages)

        api_messages.append({"role": "user", "content": message.content})

        for _ in range(MAX_AGENT_STEPS):
            text_acc, tool_calls_acc = await _thinking_step(message.content)

            if tool_calls_acc:
                api_messages.append(
                    {
                        "role": "assistant",
                        "content": text_acc,
                        "tool_calls": [
                            {
                                "id": tc["id"],
                                "type": "function",
                                "function": {
                                    "name": tc["name"],
                                    "arguments": tc["args"],
                                },
                            }
                            for tc in tool_calls_acc
                        ],
                    }
                )
                for tc in tool_calls_acc:
                    # Generic dispatch: the tool list comes from the FIDAA
                    # MCP server (list_tools at startup), so no per-tool
                    # branching here.
                    result = await _mcp_tool_step(tc)
                    api_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": result,
                        }
                    )
                continue

            final_text = strip_reasoning_tags(text_acc)
            api_messages.append({"role": "assistant", "content": text_acc})
            await cl.Message(content=final_text).send()
            return

        await cl.ErrorMessage(content="Fehler: Maximale Iterationen erreicht.").send()

    except Exception:
        logger.exception("Error processing message")
        await cl.ErrorMessage(
            content="Ein Fehler ist aufgetreten. Bitte versuche es erneut."
        ).send()
