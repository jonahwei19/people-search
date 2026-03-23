"""Conversational questioning: probes underdefined terms one at a time. Uses Gemini."""

from __future__ import annotations

from typing import Optional

from v2.gemini_helpers import call_gemini, call_gemini_json
from v2.models import DefinedSearch, GlobalRule

QUESTIONER_SYSTEM = """You are helping a team define a people search. The user has described who they're looking for. Your job is to have a short conversation to understand exactly what they mean.

Your approach:
- Look at the user's query and find the MOST UNDERDEFINED term or concept
- Ask ONE question that probes the boundaries of that term
- Use concrete examples to test what counts: "Would someone who did Y count as X?"
- After they answer, find the NEXT underdefined term and probe that

Good questions:
- "You said 'exceptional' — would a PhD student who published groundbreaking work count, or does 'exceptional' require real-world operational experience?"
- "When you say 'biosecurity,' does that include someone working on agricultural biosecurity, or are you specifically thinking pandemic/bioweapons?"
- "Would someone from RAND count as having 'government experience,' or do you mean people who were actually inside government?"
- "You mentioned 'senior' — is a 30-year-old who founded and scaled a company senior enough, or are you thinking 15+ years in the field?"

Bad questions:
- "Is this for advisory or hiring?" (either-or when it might be neither)
- "What qualifications are you looking for?" (lazy, generic)
- "Could you provide more details?" (vague)

Key principles:
- ONE question at a time
- Each question tests the BOUNDARY of a specific term
- Use concrete examples to make the question precise
- Don't ask about things already clarified in the conversation

Given the conversation so far, decide:
- If there is a SPECIFIC term in the query that is still genuinely ambiguous and hasn't been discussed yet, ask about it.
- If the user just confirmed or agreed (e.g. "yup", "yes", "ya"), that means you're DONE. Do not rephrase your understanding back at them — just set done=true.
- If you find yourself wanting to summarize what you've learned, that means you're DONE.

IMPORTANT: When done, set done=true. Do NOT output a summary as a question.

Respond as JSON:
{"done": true, "summary": "concise description of what they want"}
or
{"done": false, "question": "your next question about a specific underdefined term"}"""


def next_question(
    query: str,
    conversation: list[dict],  # [{"role": "user"/"assistant", "text": "..."}]
    global_rules: list[GlobalRule] = None,  # not used — kept for signature compat
    existing_search: Optional[DefinedSearch] = None,
) -> dict:
    """Generate the next clarifying question based on conversation so far.

    Returns {"question": "...", "done": false} or {"question": "", "done": true, "summary": "..."}
    """
    parts = [f"SEARCH QUERY: {query}"]

    # NOTE: global rules are intentionally NOT passed to the questioner.
    # The questioner's job is to clarify what the USER means.
    # Global rules get injected later by the judge (after relevance filtering).

    if existing_search and existing_search.search_rules:
        parts.append("\nEXISTING SEARCH RULES:")
        for r in existing_search.search_rules:
            parts.append(f"- {r}")

    if conversation:
        parts.append("\nCONVERSATION SO FAR:")
        for msg in conversation:
            prefix = "You asked" if msg["role"] == "assistant" else "User answered"
            parts.append(f"{prefix}: {msg['text']}")

    num_exchanges = len([m for m in conversation if m["role"] == "assistant"])
    parts.append(f"\nQuestions asked so far: {num_exchanges}")
    parts.append("Look at the conversation. Is there still a term or concept in the query that is genuinely ambiguous and would change which profiles rank highly? If yes, ask about it. If every important term has been clarified, set done=true and summarize. Do NOT repeat topics already covered. Do NOT ask questions just to be thorough — stop when you have enough to score well.")

    try:
        data = call_gemini_json(QUESTIONER_SYSTEM, "\n".join(parts))

        # If the "question" field doesn't contain a question mark, it's a summary — not a question
        q = data.get("question", "")
        if q and "?" not in q:
            data["done"] = True
            data["summary"] = q
            data["question"] = ""

        return {
            "question": data.get("question", ""),
            "done": data.get("done", False),
            "summary": data.get("summary", ""),
        }
    except Exception as e:
        print(f"Questioner error: {e}")
        return {"question": "", "done": True, "summary": query}


def conversation_to_context(conversation: list[dict], summary: str = "") -> str:
    """Convert conversation history into context string for the judge prompt."""
    if summary:
        return f"Search clarification summary:\n{summary}"
    parts = []
    for msg in conversation:
        if msg["role"] == "assistant":
            parts.append(f"Q: {msg['text']}")
        else:
            parts.append(f"A: {msg['text']}")
    return "\n".join(parts)
