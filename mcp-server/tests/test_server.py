"""Tests for MCP server tools.

Tests vocabulary management tools and error handling.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# conftest sets env vars before this import
import server as srv
import server_heisig
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(status_code: int, json_body: dict) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=json_body,
        request=httpx.Request("POST", "http://test-backend/vocabulary"),
    )


class TestAddWordsRegistration:
    def test_add_words_tool_registered(self):
        """Test that add_words tool is registered with MCP server."""
        tools = asyncio.run(srv.mcp.list_tools())
        assert any(t.name == "add_words" for t in tools)

    def test_schema_requires_words(self):
        """Test that words array with word and definition are required."""
        tools = asyncio.run(srv.mcp.list_tools())
        tool = next(t for t in tools if t.name == "add_words")
        schema = tool.inputSchema
        assert "words" in schema["required"]
        items = schema["properties"]["words"]["items"]
        # FastMCP may inline properties or use a $ref into $defs
        if "properties" in items:
            word_props = items["properties"]
        else:
            ref = items["$ref"].split("/")[-1]
            word_props = schema["$defs"][ref]["properties"]
        assert "word" in word_props
        assert "definition" in word_props
        # word and definition should be required in the item schema
        if "required" in items:
            item_required = items["required"]
        else:
            ref = items["$ref"].split("/")[-1]
            item_required = schema["$defs"][ref].get("required", [])
        assert "word" in item_required
        assert "definition" in item_required
        # pinyin has its own required field so it is never packed into the definition
        assert "pinyin" in word_props
        assert "pinyin" in item_required


class TestAddWordsSuccess:
    def test_returns_summary_message(self):
        """Test bulk add returns summary message with insertion count."""
        fake = _make_response(201, {"inserted": [{"word": "a"}, {"word": "b"}], "skipped_count": 0})
        with patch.object(srv._http_client, "post", new=AsyncMock(return_value=fake)):
            result = asyncio.run(
                srv.add_words(
                    [
                        {"word": "a", "pinyin": "pīnyīn", "definition": "a"},
                        {"word": "b", "pinyin": "pīnyīn", "definition": "b"},
                    ]
                )
            )
        assert "2" in result
        assert "Saved" in result

    def test_reports_skipped_duplicates(self):
        """Test bulk add reports duplicates/skipped words."""
        fake = _make_response(201, {"inserted": [{"word": "b"}], "skipped_count": 1})
        with patch.object(srv._http_client, "post", new=AsyncMock(return_value=fake)):
            result = asyncio.run(
                srv.add_words(
                    [
                        {"word": "a", "pinyin": "pīnyīn", "definition": "a"},
                        {"word": "b", "pinyin": "pīnyīn", "definition": "b"},
                    ]
                )
            )
        assert "1" in result and ("skip" in result.lower() or "duplicate" in result.lower())

    def test_calls_bulk_endpoint(self):
        """Test bulk add calls /vocabulary/bulk endpoint."""
        fake = _make_response(201, {"inserted": [], "skipped_count": 0})
        mock_post = AsyncMock(return_value=fake)
        with patch.object(srv._http_client, "post", new=mock_post):
            asyncio.run(srv.add_words([{"word": "x", "pinyin": "pīnyīn", "definition": "y"}]))
        args, kwargs = mock_post.call_args
        assert "/vocabulary/bulk" in args[0]

    def test_uses_correct_api_key_header(self):
        """Test API key is included in bulk endpoint request."""
        fake = _make_response(201, {"inserted": [], "skipped_count": 0})
        mock_post = AsyncMock(return_value=fake)
        with patch.object(srv._http_client, "post", new=mock_post):
            asyncio.run(srv.add_words([{"word": "x", "pinyin": "pīnyīn", "definition": "y"}]))
        _, kwargs = mock_post.call_args
        assert kwargs["headers"]["X-API-Key"] == "test-key"


class TestAddWordsErrors:
    def test_http_error_returns_error_message(self):
        """Test bulk add HTTP errors are converted to error messages."""
        error_resp = _make_response(422, {"detail": "too many words"})
        with patch.object(
            srv._http_client,
            "post",
            new=AsyncMock(
                side_effect=httpx.HTTPStatusError("err", request=MagicMock(), response=error_resp)
            ),
        ):
            result = asyncio.run(srv.add_words([{"word": "x", "pinyin": "pīnyīn", "definition": "y"}]))
        assert "Failed" in result
        assert "422" in result

    def test_network_error_returns_error_message(self):
        """Test bulk add network errors are gracefully converted."""
        with patch.object(
            srv._http_client,
            "post",
            new=AsyncMock(side_effect=Exception("connection refused")),
        ):
            result = asyncio.run(srv.add_words([{"word": "x", "pinyin": "pīnyīn", "definition": "y"}]))
        assert "Failed" in result


# ---------------------------------------------------------------------------
# HTTP auth middleware (covered by test_server_integration.py)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# section_name forwarding
# ---------------------------------------------------------------------------


class TestAddWordsSection:
    def test_section_name_injected_into_all_words(self):
        """Test add_words sends the section name as session_name on every word."""
        fake = _make_response(201, {"inserted": [], "skipped_count": 0})
        mock_post = AsyncMock(return_value=fake)
        with patch.object(srv._http_client, "post", new=mock_post):
            asyncio.run(
                srv.add_words(
                    words=[{"word": "a", "pinyin": "pīnyīn", "definition": "a"}, {"word": "b", "pinyin": "pīnyīn", "definition": "b"}],
                    section_name="HSK 3 travel",
                )
            )
        _, kwargs = mock_post.call_args
        assert all(w["session_name"] == "HSK 3 travel" for w in kwargs["json"]["words"])

    def test_no_section_name_sends_none(self):
        """Test add_words sends session_name=None when no section is given."""
        fake = _make_response(201, {"inserted": [], "skipped_count": 0})
        mock_post = AsyncMock(return_value=fake)
        with patch.object(srv._http_client, "post", new=mock_post):
            asyncio.run(srv.add_words(words=[{"word": "a", "pinyin": "pīnyīn", "definition": "a"}]))
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["words"][0]["session_name"] is None

    def test_pinyin_forwarded_as_separate_field(self):
        """Test add_words sends pinyin separately from the definition."""
        mock_post = AsyncMock(return_value=_make_response(201, {"inserted": [], "skipped_count": 0}))
        with patch.object(srv._http_client, "post", new=mock_post):
            asyncio.run(srv.add_words([{"word": "变化", "pinyin": "biànhuà", "definition": "change"}]))
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["words"][0]["pinyin"] == "biànhuà"
        assert kwargs["json"]["words"][0]["definition"] == "change"

    def test_old_tool_names_are_gone(self):
        """The superseded tool names are no longer registered."""
        names = {t.name for t in asyncio.run(srv.mcp.list_tools())}
        assert names.isdisjoint({"add_vocabulary", "bulk_add_vocabulary", "delete_session", "add_hanzi"})
        assert {"add_words", "save_character_cards", "list_sections", "list_words", "delete_section"} <= names


# ---------------------------------------------------------------------------
# delete_section helpers
# ---------------------------------------------------------------------------


def _make_sessions_response(sessions: list[dict]) -> httpx.Response:
    return httpx.Response(
        status_code=200,
        json=sessions,
        request=httpx.Request("GET", "http://test-backend/sessions"),
    )


def _make_delete_section_response(status_code: int, json_body: dict) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=json_body,
        request=httpx.Request("DELETE", "http://test-backend/vocabulary/session/1"),
    )


_SESSIONS = [
    {"id": 1, "name": "Spanish 1", "date": "2026-06-28", "created_at": "2026-06-28 00:00:00"}
]


class TestDeleteSection:
    def test_delete_section_tool_is_registered(self):
        """Test that delete_section tool is registered with MCP server."""
        tools = asyncio.run(srv.mcp.list_tools())
        assert any(t.name == "delete_section" for t in tools)

    def test_returns_success_message_with_section_name_and_count(self):
        """Test delete_section returns message containing section name and deleted word count."""
        sessions_resp = _make_sessions_response(_SESSIONS)
        delete_resp = _make_delete_section_response(200, {"deleted_words": 3})
        with (
            patch.object(srv._http_client, "get", new=AsyncMock(return_value=sessions_resp)),
            patch.object(srv._http_client, "delete", new=AsyncMock(return_value=delete_resp)),
        ):
            result = asyncio.run(srv.delete_section("Spanish 1"))
        assert "Spanish 1" in result
        assert "3" in result
        assert "Deleted" in result

    def test_returns_not_found_when_section_name_absent(self):
        """Test delete_section returns not-found message when name not in GET /sessions."""
        sessions_resp = _make_sessions_response(_SESSIONS)
        with patch.object(srv._http_client, "get", new=AsyncMock(return_value=sessions_resp)):
            result = asyncio.run(srv.delete_section("French 1"))
        assert "not found" in result.lower()
        assert "French 1" in result

    def test_http_error_on_delete_returns_error_message(self):
        """Test delete_section returns error message on HTTP error from delete endpoint."""
        sessions_resp = _make_sessions_response(_SESSIONS)
        error_resp = _make_delete_section_response(500, {"detail": "server error"})
        with (
            patch.object(srv._http_client, "get", new=AsyncMock(return_value=sessions_resp)),
            patch.object(
                srv._http_client,
                "delete",
                new=AsyncMock(
                    side_effect=httpx.HTTPStatusError(
                        "err", request=MagicMock(), response=error_resp
                    )
                ),
            ),
        ):
            result = asyncio.run(srv.delete_section("Spanish 1"))
        assert "Failed" in result
        assert "500" in result

    def test_network_error_returns_error_message(self):
        """Test delete_section returns error message on network failure during GET /sessions."""
        with patch.object(
            srv._http_client, "get", new=AsyncMock(side_effect=Exception("connection refused"))
        ):
            result = asyncio.run(srv.delete_section("Spanish 1"))
        assert "Failed" in result


def _hanzi_bulk_response(created, enriched, unchanged) -> httpx.Response:
    """Build a fake /vocabulary/hanzi/bulk response."""
    return httpx.Response(
        status_code=201,
        json={"created": created, "enriched": enriched, "unchanged": unchanged, "cards": []},
        request=httpx.Request("POST", "http://test-backend/vocabulary/hanzi/bulk"),
    )


_CARD = {
    "hanzi": "明",
    "keyword": "bright",
    "pinyin": "míng",
    "tone": 2,
}


class TestSaveCharacterCards:
    """Tests for the save_character_cards MCP tool."""

    def test_tool_registered(self):
        """save_character_cards tool is registered."""
        tools = asyncio.run(srv.mcp.list_tools())
        names = {t.name for t in tools}
        assert "save_character_cards" in names

    def test_posts_to_hanzi_bulk_endpoint(self):
        """save_character_cards calls the /vocabulary/hanzi/bulk endpoint."""
        mock_post = AsyncMock(return_value=_hanzi_bulk_response(1, 0, 0))
        with patch.object(srv._http_client, "post", new=mock_post):
            asyncio.run(srv.save_character_cards([_CARD]))
        args, _ = mock_post.call_args
        assert "/vocabulary/hanzi/bulk" in args[0]

    def test_report_mentions_created_and_enriched(self):
        """The returned message reports created/enriched/unchanged counts."""
        with patch.object(
            srv._http_client, "post", new=AsyncMock(return_value=_hanzi_bulk_response(3, 5, 2))
        ):
            result = asyncio.run(srv.save_character_cards([_CARD]))
        assert "3" in result and "5" in result and "2" in result

    def test_maps_hanzi_field_to_word(self):
        """The 'hanzi' field is sent to the backend as 'word'."""
        mock_post = AsyncMock(return_value=_hanzi_bulk_response(1, 0, 0))
        with patch.object(srv._http_client, "post", new=mock_post):
            asyncio.run(srv.save_character_cards([_CARD]))
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["cards"][0]["word"] == "明"

    def test_invalid_tone_returns_error_without_calling_backend(self):
        """Invalid input fails validation and never hits the backend."""
        mock_post = AsyncMock()
        bad = {**_CARD, "tone": 9}
        with patch.object(srv._http_client, "post", new=mock_post):
            result = asyncio.run(srv.save_character_cards([bad]))
        assert "Invalid" in result or "valid" in result.lower()
        mock_post.assert_not_called()


class TestHanziInputModel:
    """Tests for the HanziInput Pydantic model."""

    def test_valid_hanzi_input(self):
        """A well-formed HanziInput validates."""
        m = server_heisig.HanziInput(hanzi="明", keyword="bright", pinyin="míng", tone=2)
        assert m.tone == 2

    def test_tone_out_of_range_rejected(self):
        """tone outside 1..5 raises ValidationError."""
        with pytest.raises(ValidationError):
            server_heisig.HanziInput(hanzi="x", keyword="k", pinyin="p", tone=0)


class TestListSections:
    def test_lists_names_and_dates(self):
        """list_sections returns one line per section."""
        resp = _make_sessions_response(_SESSIONS)
        with patch.object(srv._http_client, "get", new=AsyncMock(return_value=resp)):
            result = asyncio.run(srv.list_sections())
        assert "Spanish 1" in result and "2026-06-28" in result

    def test_empty(self):
        """list_sections reports when nothing exists."""
        resp = _make_sessions_response([])
        with patch.object(srv._http_client, "get", new=AsyncMock(return_value=resp)):
            assert "No sections" in asyncio.run(srv.list_sections())

    def test_error(self):
        """list_sections converts failures to a message."""
        with patch.object(srv._http_client, "get", new=AsyncMock(side_effect=Exception("down"))):
            assert "Failed" in asyncio.run(srv.list_sections())


def _words_response(words: list[dict], total: int | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=200,
        json={"total": len(words) if total is None else total, "words": words},
        request=httpx.Request("GET", "http://test-backend/vocabulary"),
    )


_WORD = {"word": "变化", "pinyin": "biànhuà", "definition": "change", "session_name": "Spanish 1"}


class TestListWords:
    def test_lists_words_with_total(self):
        """list_words shows total and one line per word."""
        mock_get = AsyncMock(return_value=_words_response([_WORD], total=7))
        with patch.object(srv._http_client, "get", new=mock_get):
            result = asyncio.run(srv.list_words())
        assert "7 words total" in result
        assert "变化 (biànhuà): change" in result

    def test_section_name_resolved_to_session_id(self):
        """list_words resolves the section name and filters by session_id."""
        mock_get = AsyncMock(side_effect=[_make_sessions_response(_SESSIONS), _words_response([])])
        with patch.object(srv._http_client, "get", new=mock_get):
            asyncio.run(srv.list_words(section_name="Spanish 1"))
        _, kwargs = mock_get.call_args
        assert kwargs["params"]["session_id"] == 1

    def test_unknown_section_not_found(self):
        """list_words reports an unknown section without querying words."""
        mock_get = AsyncMock(return_value=_make_sessions_response(_SESSIONS))
        with patch.object(srv._http_client, "get", new=mock_get):
            result = asyncio.run(srv.list_words(section_name="French 1"))
        assert "not found" in result.lower()
        assert mock_get.call_count == 1

    def test_limit_is_clamped(self):
        """list_words never asks the backend for more than 200 words."""
        mock_get = AsyncMock(return_value=_words_response([]))
        with patch.object(srv._http_client, "get", new=mock_get):
            asyncio.run(srv.list_words(limit=5000))
        _, kwargs = mock_get.call_args
        assert kwargs["params"]["limit"] == 200

    def test_error(self):
        """list_words converts failures to a message."""
        with patch.object(srv._http_client, "get", new=AsyncMock(side_effect=Exception("down"))):
            assert "Failed" in asyncio.run(srv.list_words())

    def test_multi_character_word_rejected(self):
        """hanzi must be a single character; words belong in add_words."""
        with pytest.raises(ValidationError):
            server_heisig.HanziInput(hanzi="明天", keyword="tomorrow", pinyin="míngtiān", tone=2)
