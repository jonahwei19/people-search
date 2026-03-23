"""Shared Gemini client + helper for all LLM calls (questioning, synthesis, global filter)."""

from __future__ import annotations

import json
import os
from typing import Optional

from google import genai

MODEL = "gemini-3.1-flash-lite-preview"

_client: Optional[genai.Client] = None


def get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.environ.get("GOOGLE_API_KEY", "")
        _client = genai.Client(api_key=api_key)
    return _client


def call_gemini(system: str, user: str, max_tokens: int = 2048, json_mode: bool = True) -> str:
    """Make a Gemini API call and return the text response."""
    client = get_client()
    config = genai.types.GenerateContentConfig(
        system_instruction=system,
        max_output_tokens=max_tokens,
        temperature=0.0,
    )
    if json_mode:
        config.response_mime_type = "application/json"

    response = client.models.generate_content(
        model=MODEL,
        contents=user,
        config=config,
    )
    return response.text.strip()


def call_gemini_json(system: str, user: str, max_tokens: int = 2048) -> dict | list:
    """Make a Gemini call and parse JSON response."""
    text = call_gemini(system, user, max_tokens, json_mode=True)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try extracting JSON from text
        for ch in ["{", "["]:
            start = text.find(ch)
            end_ch = "}" if ch == "{" else "]"
            end = text.rfind(end_ch) + 1
            if start >= 0 and end > start:
                try:
                    return json.loads(text[start:end])
                except json.JSONDecodeError:
                    pass
        return {}
