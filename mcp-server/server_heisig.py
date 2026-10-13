"""Single-character card MCP tool and input model.

Registers the save_character_cards tool against a FastMCP instance. General-purpose
vocabulary tools live in server.py.
"""

import httpx
from pydantic import BaseModel, Field


class HanziInput(BaseModel):
    """Validated input for one single-character card arriving from chat (MCP boundary).

    Attributes:
        hanzi: Exactly one hanzi character.
        keyword: Single English keyword for the character's core meaning.
        pinyin: Pinyin with tone mark.
        tone: Tone number 1-5 (5 = neutral).
        definition: Meaning/usage for a new card; ignored for existing cards. NOT for pinyin.
        example: Optional usage sentence (hanzi only) for a new card.
        example_pinyin: Optional pinyin of the example sentence, new cards only.
        example_translation: Optional English translation of the example, new cards only.
    """

    hanzi: str = Field(
        min_length=1, max_length=1, description="ONE hanzi character, e.g. 明 (never a word)."
    )
    keyword: str = Field(
        min_length=1,
        description="One English word for the character's core meaning, e.g. 'bright'.",
    )
    pinyin: str = Field(min_length=1, description="Pinyin with tone mark, e.g. 'míng'.")
    tone: int = Field(ge=1, le=5, description="Tone number 1-4, or 5 for neutral.")
    definition: str | None = Field(
        None,
        description="Meaning/usage for a NEW card (defaults to the keyword). No pinyin. Ignored if the card exists.",
    )
    example: str | None = Field(
        None, description="Example in hanzi only, for a NEW card. Ignored if the card exists."
    )
    example_pinyin: str | None = Field(None, description="Pinyin of the example, NEW cards only.")
    example_translation: str | None = Field(
        None, description="English translation of the example, NEW cards only."
    )


def register_tools(
    mcp,
    http_client: httpx.AsyncClient,
    vocab_api_url: str,
    vocab_api_key: str,
):
    """Register the single-character card MCP tool with the provided FastMCP instance.

    Args:
        mcp: FastMCP server instance to register tools on.
        http_client: Shared async HTTP client.
        vocab_api_url: Base URL of the vocabulary backend API.
        vocab_api_key: API key for backend authentication.

    Returns:
        The save_character_cards tool function, for direct access in tests.
    """

    @mcp.tool(
        description=(
            "Save single-character cards (one hanzi per card, max 50 per call), used to learn the "
            "meaning and tone of individual characters, e.g. 明 = 'bright', míng, tone 2. "
            "For multi-character words (e.g. 明天) use add_words instead. "
            "This is create-or-update, matched on the character alone: if a card for that "
            "character already exists (even one made by add_words), only its keyword, pinyin and "
            "tone are updated; its definition, example, section and review progress are never "
            "changed. If it does not exist, a new card is created. "
            "definition and the example fields are only used when creating. "
            "section_name only applies to newly created cards."
        )
    )
    async def save_character_cards(cards: list[HanziInput], section_name: str | None = None) -> str:
        """Create or update single-character cards via the backend upsert endpoint.

        Validates every card against HanziInput before sending. Maps the 'hanzi' field to
        the backend 'word' field. Returns a created/updated/unchanged breakdown.

        Args:
            cards: List of HanziInput cards (1-50).
            section_name: Optional section for newly created cards only.

        Returns:
            A summary message with created/updated/unchanged counts, or an error message.
        """
        try:
            validated = [HanziInput.model_validate(c) for c in cards]
        except Exception as e:
            return f"Invalid hanzi input, nothing was saved: {e}"

        payload_cards = []
        for c in validated:
            d = c.model_dump()
            d["word"] = d.pop("hanzi")
            payload_cards.append(d)

        try:
            response = await http_client.post(
                f"{vocab_api_url}/vocabulary/hanzi/bulk",
                json={"cards": payload_cards, "session_name": section_name},
                headers={"X-API-Key": vocab_api_key},
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()
            return (
                f"Created {data['created']} new cards · updated {data['enriched']} existing · "
                f"{data['unchanged']} unchanged."
            )
        except httpx.HTTPStatusError as e:
            return f"Failed to save hanzi: HTTP {e.response.status_code} — {e.response.text}"
        except Exception as e:
            return f"Failed to save hanzi: {e}"

    return save_character_cards
