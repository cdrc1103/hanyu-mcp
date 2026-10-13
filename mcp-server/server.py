"""Model Context Protocol (MCP) server for vocabulary management.

Exposes vocabulary operations as MCP tools accessible to Claude and other AI clients.
Supports OAuth 2.0 authentication with configurable authorization endpoints.
Heisig-specific tools are registered from server_heisig.py.
"""

import os
from typing import Required, TypedDict

import httpx
import uvicorn
from database import init_db
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from oauth_provider import VocabularyOAuthProvider
from server_heisig import register_tools
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse


class VocabWord(TypedDict, total=False):
    """Schema for vocabulary word in MCP tool requests.

    Attributes:
        word: The vocabulary word (required).
        pinyin: Pinyin romanization with tone marks, e.g. "biànhuà" (required).
        definition: Definition of the word — meaning/usage only, not pinyin (required).
        example: Optional example sentence in hanzi only.
        example_pinyin: Pinyin of the example sentence, with tone marks.
        example_translation: English translation of the example sentence.
        language: Optional language code.
    """

    word: Required[str]
    pinyin: Required[str]
    definition: Required[str]
    example: str
    example_pinyin: str
    example_translation: str
    language: str


VOCAB_API_URL = os.getenv("VOCAB_API_URL", "http://localhost:8000")
VOCAB_API_KEY = os.getenv("VOCAB_API_KEY", "")
if not VOCAB_API_KEY:
    raise RuntimeError("VOCAB_API_KEY environment variable is not set")

MCP_SECRET = os.getenv("MCP_SECRET", "")
if not MCP_SECRET:
    raise RuntimeError("MCP_SECRET environment variable is not set")

ISSUER_URL = os.getenv("ISSUER_URL", "http://localhost:8080")
DATABASE_PATH = os.getenv("DATABASE_PATH", "oauth.db")

# Initialize database
init_db(DATABASE_PATH)

# Module-level client: reuses connection pool and TLS session across calls
_http_client = httpx.AsyncClient()

# ── OAuth provider ────────────────────────────────────────────────────────────

oauth_provider = VocabularyOAuthProvider(
    db_path=DATABASE_PATH,
    secret=MCP_SECRET,
    issuer_url=ISSUER_URL,
)

# ── MCP server with OAuth ─────────────────────────────────────────────────────

mcp = FastMCP(
    "vocabulary",
    auth_server_provider=oauth_provider,
    auth=AuthSettings(
        issuer_url=ISSUER_URL,
        resource_server_url=ISSUER_URL,
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    ),
    host="0.0.0.0",
    port=int(os.getenv("PORT", "8080")),
)

# ── MCP tools ─────────────────────────────────────────────────────────────────


_ADD_WORDS_DESCRIPTION = (
    "Save one or more vocabulary words to the personal study app (max 50 per call). "
    "Use this when the user asks to save words from a conversation, or when you have explained "
    "words and want to offer to save them. Pass section_name to group the words under a named "
    "section (e.g. 'HSK 3 travel'); it is created if new. Words that already exist are skipped. "
    "Always pass pinyin in the separate pinyin field. "
    "The definition field is for meaning/usage only — do NOT put pinyin in it. "
    "Put the example sentence in hanzi only in example, its pinyin in example_pinyin and "
    "its English translation in example_translation — never combine them in one field."
)


@mcp.tool(name="add_words", description=_ADD_WORDS_DESCRIPTION)
async def add_words(
    words: list[VocabWord],
    section_name: str | None = None,
) -> str:
    """Save vocabulary words (one or many) via MCP tool.

    Creates vocabulary entries in bulk, accessible to AI assistants and other MCP clients.
    Supports up to 50 words per request. Skips duplicate words automatically.

    Args:
        words: List of VocabWord entries with word, pinyin, definition, and optional fields.
        section_name: Section applied to all words in the batch. Auto-created if new.

    Returns:
        Success message with count of saved and skipped words, or error message.
    """
    try:
        # The backend still calls sections "sessions".
        words_to_send = [{**w, "session_name": section_name} for w in words]
        response = await _http_client.post(
            f"{VOCAB_API_URL}/vocabulary/bulk",
            json={"words": words_to_send},
            headers={"X-API-Key": VOCAB_API_KEY},
            timeout=30.0,
        )
        response.raise_for_status()
        data = response.json()
        inserted_count = len(data.get("inserted", []))
        skipped_count = data.get("skipped_count", 0)
        msg = f"Saved {inserted_count} words to your vocabulary deck."
        if skipped_count > 0:
            msg += f" {skipped_count} duplicates skipped."
        return msg
    except httpx.HTTPStatusError as e:
        return f"Failed to save words: HTTP {e.response.status_code} — {e.response.text}"
    except Exception as e:
        return f"Failed to save words: {e}"


async def _fetch_sections() -> list[dict]:
    """Fetch all vocabulary sections from the backend (it calls them sessions)."""
    response = await _http_client.get(
        f"{VOCAB_API_URL}/sessions",
        headers={"X-API-Key": VOCAB_API_KEY},
        timeout=10.0,
    )
    response.raise_for_status()
    return response.json()


@mcp.tool(
    description=(
        "List the user's vocabulary sections (named groups of words) with their dates. "
        "Use this to check which sections exist before saving, listing or deleting words."
    )
)
async def list_sections() -> str:
    """List all vocabulary sections via MCP tool.

    Returns:
        One line per section, or a message when there are none, or an error message.
    """
    try:
        sections = await _fetch_sections()
        if not sections:
            return "No sections yet."
        return "\n".join(f"- {s['name']} ({s['date']})" for s in sections)
    except httpx.HTTPStatusError as e:
        return f"Failed to list sections: HTTP {e.response.status_code} — {e.response.text}"
    except Exception as e:
        return f"Failed to list sections: {e}"


@mcp.tool(
    description=(
        "List saved vocabulary words, optionally only those in one section. "
        "Use this to check whether a word is already saved, or to answer questions about "
        "what the user has saved. Returns at most `limit` words (default 50, max 200)."
    )
)
async def list_words(
    section_name: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> str:
    """List saved vocabulary words via MCP tool.

    Args:
        section_name: Optional exact section name to filter by.
        limit: Number of words to return (1-200, default 50).
        offset: Number of words to skip, for paging.

    Returns:
        A total count and one line per word, or a not-found/error message.
    """
    try:
        params: dict[str, int] = {"limit": max(1, min(limit, 200)), "offset": max(0, offset)}
        if section_name is not None:
            section = next((s for s in await _fetch_sections() if s["name"] == section_name), None)
            if section is None:
                return f"Section '{section_name}' not found."
            params["session_id"] = section["id"]
        response = await _http_client.get(
            f"{VOCAB_API_URL}/vocabulary",
            params=params,
            headers={"X-API-Key": VOCAB_API_KEY},
            timeout=10.0,
        )
        response.raise_for_status()
        data = response.json()
        lines = [
            f"- {w['word']} ({w.get('pinyin') or '?'}): {w['definition']} "
            f"[{w.get('session_name', '')}]"
            for w in data["words"]
        ]
        header = (
            f"{data['total']} words total; showing {len(lines)} from offset {params['offset']}."
        )
        return "\n".join([header, *lines])
    except httpx.HTTPStatusError as e:
        return f"Failed to list words: HTTP {e.response.status_code} — {e.response.text}"
    except Exception as e:
        return f"Failed to list words: {e}"


@mcp.tool(
    description=(
        "PERMANENTLY delete a vocabulary section AND all words in it, including their review "
        "progress. Only use this when the user explicitly asks to remove a whole section. "
        "The section is identified by its exact name (see list_sections)."
    )
)
async def delete_section(section_name: str) -> str:
    """Delete a vocabulary section and all its words via MCP tool.

    Resolves section_name to an id via GET /sessions, then calls
    DELETE /vocabulary/session/{id} to remove the words and the section record.

    Args:
        section_name: Exact name of the section to delete.

    Returns:
        Success message with deleted word count, or an error/not-found message.
    """
    try:
        section = next((s for s in await _fetch_sections() if s["name"] == section_name), None)
        if section is None:
            return f"Section '{section_name}' not found."
        delete_response = await _http_client.delete(
            f"{VOCAB_API_URL}/vocabulary/session/{section['id']}",
            headers={"X-API-Key": VOCAB_API_KEY},
            timeout=10.0,
        )
        delete_response.raise_for_status()
        deleted_words = delete_response.json()["deleted_words"]
        return f"Deleted section '{section_name}' and {deleted_words} words."
    except httpx.HTTPStatusError as e:
        return f"Failed to delete section: HTTP {e.response.status_code} — {e.response.text}"
    except Exception as e:
        return f"Failed to delete section: {e}"


# ── Heisig tools (registered from server_heisig) ─────────────────────────────

add_hanzi_cards = register_tools(mcp, _http_client, VOCAB_API_URL, VOCAB_API_KEY)

# ── Custom routes (unprotected) ───────────────────────────────────────────────


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    """Health check endpoint for MCP server.

    Provides a lightweight status check for monitoring and load balancers.

    Args:
        _request: HTTP request (unused).

    Returns:
        JSON response with status "ok".
    """
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/authorize/submit", methods=["GET", "POST"])
async def authorize_submit(request: Request) -> HTMLResponse | RedirectResponse:
    """Password-gated OAuth authorization endpoint.

    Handles MCP OAuth 2.0 authorization flows with password verification.
    GET requests display a login form; POST requests validate the password and
    redirect to the authorization completion URL on success.

    Args:
        request: HTTP request containing auth_params and password (on POST).

    Returns:
        GET: HTML login form with optional error message.
        POST: Redirect to authorization completion URL on success, or error form on failure.

    Raises:
        401 Unauthorized: If password is incorrect.
        400 Bad Request: If authorization session has expired.
    """
    if request.method == "GET":
        auth_params = request.query_params.get("auth_params", "")
        return HTMLResponse(oauth_provider.render_login_page(auth_params))

    # POST: validate password
    form = await request.form()
    auth_params_nonce = str(form.get("auth_params", ""))
    password = str(form.get("password", ""))

    if password != MCP_SECRET:
        return HTMLResponse(
            oauth_provider.render_login_page(auth_params_nonce, error="Invalid password"),
            status_code=401,
        )

    # Look up pending authorization params
    pending = oauth_provider._pending_params.pop(auth_params_nonce, None)
    if pending is None:
        return HTMLResponse(
            oauth_provider.render_login_page(
                auth_params_nonce, error="Session expired. Please try again."
            ),
            status_code=400,
        )

    client, params = pending
    redirect_url = oauth_provider.complete_authorization(client, params)
    return RedirectResponse(url=redirect_url, status_code=302)


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
