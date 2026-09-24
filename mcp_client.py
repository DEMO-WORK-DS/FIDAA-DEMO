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

"""FIDAA knowledge server (MCP) client — D1.5 backend-activated connection.

The app-side half of the connection to the FIDAA MCP server (the server
itself lives in the `fidaa` submodule): connect once at startup inside a
background task (the cross-task cancel-scope workaround for Chainlit
issue #2182), keep one shared McpSession for the process lifetime, and
hand each chat a lightweight copy so per-chat cleanup cannot kill a
connection other chats still use. app.py keeps the UI side (tool
dispatch, step rendering).
"""

import asyncio
import logging
from contextlib import AsyncExitStack

import chainlit as cl
from chainlit.config import StreamableHttpMcpServer, config
from chainlit.mcp import HttpMcpConnection
from chainlit.session import McpSession, stop_mcp_task
from mcp import ClientSession
# `streamablehttp_client` is the mcp 1.x-line name (Chainlit pins mcp<2);
# the SDK v2 line renamed it to `streamable_http_client`.
from mcp.client.streamable_http import streamablehttp_client

logger = logging.getLogger(__name__)

FIDAA_MCP_NAME = "fidaa"
# The fidaa server builds its in-memory index at boot (usually well under
# a minute); give the initial MCP connection a generous budget.
MCP_CONNECT_TIMEOUT = 60.0

# German display names for the tool steps in the UI (keyed by tool name;
# unknown tools fall back to the raw name).
MCP_STEP_NAMES = {
    "search_context": "🔍 Kontextsuche",
    "search_bibliography": "📚 Bibliographiesuche",
    "search_documents": "📄 Dokumentensuche",
    "list_sections": "📑 Inhaltsverzeichnis",
}

# German display names for the FIDAA collections inside the TOC step
# (the server returns the internal names; display-only mapping for the UI).
MCP_COLLECTION_LABELS = {
    "context": "Kontextdatenbank",
    "bibliography": "Bibliografie",
    "documents": "Dokumentenarchiv",
}

# Populated at startup from the MCP server's list_tools() — the server is
# the single source of truth for tool names/descriptions. Read-only after
# startup.
tools: list[dict] = []
# The one shared MCP session for the process lifetime. Each chat registers
# a lightweight copy in its mcp_sessions dict (see register_for_session),
# so per-chat cleanup cannot kill a connection other chats still use.
shared_session: McpSession | None = None


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
                "Error closing MCP exit stack for %r", FIDAA_MCP_NAME, exc_info=True
            )


async def connect() -> None:
    """Connect the shared FIDAA MCP session and build the tool schemas.

    D1.5: native, backend-activated MCP connection — same code path and
    semantics as a UI click: uses the developer config ([[features.mcp.servers]]
    in .chainlit/config.toml), wraps the ClientSession in a Chainlit
    McpSession, and fires the standard on_mcp_connect callback (tearing the
    connection down on callback failure, like the /mcp handler does).
    """
    global tools, shared_session

    if not config.features.mcp.enabled:
        raise RuntimeError(
            "features.mcp.enabled ist deaktiviert — der FIDAA-Server "
            "kann nicht verbunden werden."
        )
    server_cfg = next(
        (s for s in config.features.mcp.servers if s.name == FIDAA_MCP_NAME),
        None,
    )
    if server_cfg is None:
        raise RuntimeError(
            f"MCP-Server {FIDAA_MCP_NAME!r} fehlt in [[features.mcp.servers]] "
            "(.chainlit/config.toml)."
        )
    if not isinstance(server_cfg, StreamableHttpMcpServer):
        raise RuntimeError(
            f"MCP-Server {FIDAA_MCP_NAME!r} muss type = 'streamable-http' sein."
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
        name=f"mcp-shared-{FIDAA_MCP_NAME}",
    )

    try:
        await asyncio.wait_for(ready_event.wait(), timeout=MCP_CONNECT_TIMEOUT)
    except asyncio.TimeoutError:
        result_holder.setdefault(
            "error",
            asyncio.TimeoutError(
                f"timed out after {MCP_CONNECT_TIMEOUT:.0f}s waiting for "
                "the MCP connection to initialize"
            ),
        )

    if "error" in result_holder:
        # Bounded wait-then-cancel, same as the /mcp handler.
        await stop_mcp_task(task, stop_event, FIDAA_MCP_NAME)
        raise RuntimeError(
            f"Verbindung zum FIDAA-Server fehlgeschlagen: {result_holder['error']!s}"
        ) from result_holder["error"]

    client: ClientSession = result_holder["client"]

    # Tool schemas for the OpenAI tool-calling API; the FIDAA server is the
    # single source of truth (German names/descriptions).
    tool_list = await client.list_tools()
    tools = [
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

    shared_session = McpSession(
        name=FIDAA_MCP_NAME, client=client, task=task, stop_event=stop_event
    )

    # Fire the standard connect callback (parity with the UI connect flow).
    if config.code.on_mcp_connect:
        try:
            await config.code.on_mcp_connect(
                HttpMcpConnection(
                    name=FIDAA_MCP_NAME,
                    url=server_cfg.url,
                    headers=server_cfg.headers,
                ),
                client,
            )
        except Exception:
            await stop_mcp_task(task, stop_event, FIDAA_MCP_NAME)
            shared_session = None
            raise

    logger.info("MCP %s connected: %d tools", FIDAA_MCP_NAME, len(tools))


def register_for_session() -> None:
    """Attach the shared FIDAA MCP session to the current chat session.

    A lightweight copy is stored under the server name: on session cleanup
    (WebsocketSession.delete) close() is called on the copy, which is a
    no-op (its task is already finished), while the real shared connection
    keeps running for all other chats. Tool dispatch reads the session's
    mcp_sessions entry, so a connection the user establishes via the UI
    (POST /mcp) transparently takes precedence for that chat.
    """
    if shared_session is None:
        return

    async def _noop() -> None:
        # Pre-completed task: makes McpSession.close() a no-op for the copy.
        return None

    session = cl.context.session
    session.mcp_sessions[FIDAA_MCP_NAME] = McpSession(
        name=FIDAA_MCP_NAME,
        client=shared_session.client,
        task=asyncio.get_running_loop().create_task(_noop()),
        stop_event=asyncio.Event(),
    )


async def close() -> None:
    """Close the shared FIDAA MCP connection (called at process shutdown)."""
    if shared_session is not None:
        await shared_session.close()
