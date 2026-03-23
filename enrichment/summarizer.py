"""Profile content summarizer.

Decides what to include verbatim vs. summarize vs. skip when building
the raw_text that the LLM judge reads.

The principle: the judge's token budget is ~3000 chars per profile.
Every token should help the judge score the person. Verbose self-reported
text (pitches, essays) should be compressed to a one-liner. First-person
observations (call notes) should be mostly preserved.

Field type priorities (from v2/models.py):
  first_person     → call notes, interview transcripts, meeting notes (HIGH VALUE — preserve)
  expert_assessment → recommendations, evaluations (HIGH VALUE — preserve)
  linkedin         → enriched profile data (MEDIUM — keep structured, compress experience list)
  self_reported    → pitches, bios, about sections, essays (LOW — summarize to one line)
  metadata         → excluded from raw_text entirely
"""

from __future__ import annotations

import os
import re
from typing import Optional


# Thresholds for deciding whether to summarize
SHORT_THRESHOLD = 200    # chars: below this, include verbatim
MEDIUM_THRESHOLD = 600   # chars: below this, light trim
LONG_THRESHOLD = 1500    # chars: above this, aggressive summarize

# Field name → type classification heuristics
_FIRST_PERSON_PATTERNS = re.compile(
    r"(?i)(call.?notes?|meeting.?notes?|interview|transcript|conversation|debrief|1.?on.?1)"
)
_EXPERT_PATTERNS = re.compile(
    r"(?i)(assessment|evaluation|recommendation|review|reference|endorsement)"
)
_LINKEDIN_PATTERNS = re.compile(
    r"(?i)(linkedin|experience|education|profile)"
)
_SELF_REPORTED_PATTERNS = re.compile(
    r"(?i)(pitch|bio|about|description|summary|essay|proposal|statement|solution|problem|motivation|application)"
)


def classify_field_type(field_name: str, text: str) -> str:
    """Classify a content field's type for prioritization."""
    if _FIRST_PERSON_PATTERNS.search(field_name):
        return "first_person"
    if _EXPERT_PATTERNS.search(field_name):
        return "expert_assessment"
    if _LINKEDIN_PATTERNS.search(field_name):
        return "linkedin"
    if _SELF_REPORTED_PATTERNS.search(field_name):
        return "self_reported"
    # Default based on text length — long text is more likely self-reported
    if len(text) > LONG_THRESHOLD:
        return "self_reported"
    return "first_person"  # short unclassified text is likely notes


def summarize_field_local(field_name: str, text: str, field_type: str) -> str:
    """Compress a field without an LLM call. Fast, deterministic, free.

    Rules:
    - first_person/expert: keep up to MEDIUM_THRESHOLD, truncate with "..."
    - linkedin: keep structured, trim experience to top 5 roles
    - self_reported: if long, reduce to first sentence + "Essay about [topic from first sentence]"
    """
    text = text.strip()
    if not text:
        return ""

    if len(text) <= SHORT_THRESHOLD:
        return text

    if field_type in ("first_person", "expert_assessment"):
        # These are high value — keep more
        if len(text) <= MEDIUM_THRESHOLD:
            return text
        # Trim to MEDIUM_THRESHOLD, try to cut at sentence boundary
        cut = text[:MEDIUM_THRESHOLD]
        last_period = cut.rfind(".")
        if last_period > MEDIUM_THRESHOLD * 0.6:
            return cut[:last_period + 1]
        return cut.rstrip() + "..."

    if field_type == "linkedin":
        # Keep structured but trim long experience lists
        lines = text.split("\n")
        kept = []
        exp_count = 0
        for line in lines:
            if line.strip().startswith(("Experience:", "Education:")):
                kept.append(line)
                exp_count = 0
                continue
            # Under Experience/Education sections, keep first 5 entries
            if exp_count < 5:
                kept.append(line)
            exp_count += 1
        result = "\n".join(kept).strip()
        if len(result) > MEDIUM_THRESHOLD:
            return result[:MEDIUM_THRESHOLD].rstrip() + "..."
        return result

    if field_type == "self_reported":
        # Aggressive compression — extract the topic
        first_sentence = text.split(".")[0].strip()
        if len(first_sentence) > 120:
            first_sentence = first_sentence[:120].rstrip() + "..."
        clean_name = field_name.replace("_", " ").title()
        return f"{clean_name}: {first_sentence}."

    return text[:MEDIUM_THRESHOLD].rstrip() + "..." if len(text) > MEDIUM_THRESHOLD else text


def summarize_field_llm(field_name: str, text: str, field_type: str) -> str:
    """Compress a field using an LLM call. More accurate but costs ~$0.001/field.

    Only called for long self_reported fields where local summarization
    loses too much information.
    """
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

        prompt = (
            f"Summarize this {field_name} in ONE sentence (max 30 words). "
            f"Focus on: what is the person's main claim or proposal?\n\n"
            f"{text[:2000]}"
        )

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        summary = response.content[0].text.strip()
        clean_name = field_name.replace("_", " ").title()
        return f"{clean_name}: {summary}"
    except Exception:
        # Fall back to local
        return summarize_field_local(field_name, text, field_type)


def build_profile_card(
    name: str,
    title: str,
    organization: str,
    content_fields: dict[str, str],
    linkedin_enriched: dict | None = None,
    use_llm: bool = False,
) -> tuple[str, dict[str, str]]:
    """Build a compact profile card from a profile's data.

    Returns:
        (raw_text, field_summaries) where field_summaries is a cache
        of {field_name: summarized_text} for reuse.
    """
    parts = []
    summaries = {}

    # Header line
    header_parts = [name or "Unknown"]
    if title and organization:
        header_parts.append(f"{title} at {organization}")
    elif title:
        header_parts.append(title)
    elif organization:
        header_parts.append(organization)
    parts.append(" | ".join(header_parts))

    # LinkedIn (if enriched)
    if linkedin_enriched:
        li_text = linkedin_enriched.get("context_block", "")
        if li_text:
            field_type = "linkedin"
            summary = summarize_field_local("linkedin", li_text, field_type)
            summaries["linkedin"] = summary
            parts.append(f"\n{summary}")

    # Content fields, ordered by priority
    ordered_fields = []
    for field_name, text in content_fields.items():
        if not text or not text.strip():
            continue
        field_type = classify_field_type(field_name, text)
        priority = {"first_person": 0, "expert_assessment": 1, "linkedin": 2, "self_reported": 3}
        ordered_fields.append((priority.get(field_type, 2), field_name, text, field_type))

    ordered_fields.sort(key=lambda x: x[0])

    for _, field_name, text, field_type in ordered_fields:
        if use_llm and field_type == "self_reported" and len(text) > LONG_THRESHOLD:
            summary = summarize_field_llm(field_name, text, field_type)
        else:
            summary = summarize_field_local(field_name, text, field_type)

        summaries[field_name] = summary
        clean_name = field_name.replace("_", " ").title()

        # Don't double-label if summary already starts with the field name
        if summary.lower().startswith(clean_name.lower()):
            parts.append(f"\n{summary}")
        else:
            parts.append(f"\n{clean_name}: {summary}")

    raw_text = "\n".join(parts).strip()
    return raw_text, summaries
