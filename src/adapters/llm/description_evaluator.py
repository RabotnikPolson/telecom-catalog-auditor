import html
import re
from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from src.domain.entities import DescriptionDiscrepancyItem


def clean_html_description(raw_html: str | None) -> str:
    """
    Strips HTML tags, decodes HTML entities (&nbsp;, &quot;, &amp;),
    and collapses multiple whitespace/newlines into clean plain text.
    Prevents token waste on dirty HTML markup.
    """
    if not raw_html:
        return ""
    # Strip script and style blocks
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", "", raw_html)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", "", text)
    # Strip HTML comments
    text = re.sub(r"(?s)<!--.*?-->", "", text)
    # Convert block/break tags to newlines
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</?(p|div|li|ul|ol|h[1-6]|tr|td|th)[^>]*>", "\n", text)
    # Inline formatting tags: remove without adding spaces
    text = re.sub(r"(?i)</?(b|i|u|s|strong|em|span|small|mark|sub|sup|font|a)[^>]*>", "", text)
    # Strip any other remaining tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Decode entities like &nbsp;, &quot;, etc.
    text = html.unescape(text)
    # Replace non-breaking spaces with regular space
    text = text.replace("\xa0", " ")
    # Fix whitespace before punctuation
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)
    # Clean whitespace line by line
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    cleaned = "\n".join(line for line in lines if line)
    return cleaned.strip()


def build_description_prompt_section(description: str | None) -> str:
    """
    Formats the product description section for the LLM audit prompt.
    Returns empty string if description is absent or trivially short.
    """
    clean_desc = clean_html_description(description)
    if not clean_desc or len(clean_desc) < 15:
        return ""
    return (
        "## SHOP PRODUCT DESCRIPTION (TO BE AUDITED FOR FACTUAL ACCURACY):\n"
        f"```text\n{clean_desc[:3500]}\n```\n\n"
    )


class LLMDescriptionConflict(BaseModel):
    model_config = ConfigDict(extra="ignore")
    claim_in_description: str = Field(default="")
    factual_truth: str = Field(default="")
    contradiction_type: str = Field(default="CONFLICT_WITH_SPECS")
    severity: str = Field(default="critical")


def parse_description_audit(
    raw_conflicts: list[Any],
    raw_phantom: list[Any],
    raw_quality: str | None,
    original_description: str | None,
) -> tuple[list[DescriptionDiscrepancyItem], str]:
    """
    Pure helper: parses and validates LLM output into domain DescriptionDiscrepancyItem objects.
    Performs zero network calls.
    """
    clean_desc = clean_html_description(original_description)
    if not clean_desc or len(clean_desc) < 15:
        return [], "EMPTY"

    valid_items: list[DescriptionDiscrepancyItem] = []

    # 1. Parse conflicts with specifications
    for item in raw_conflicts or []:
        if isinstance(item, dict):
            claim = str(item.get("claim_in_description", "")).strip()
            fact = str(item.get("factual_truth", "")).strip()
            if claim and fact:
                valid_items.append(
                    DescriptionDiscrepancyItem(
                        claim_in_description=claim,
                        factual_truth=fact,
                        contradiction_type="CONFLICT_WITH_SPECS",
                        severity="critical",
                    )
                )

    # 2. Parse phantom features
    for item in raw_phantom or []:
        if isinstance(item, dict):
            claim = str(item.get("claim_in_description", "")).strip()
            fact = str(item.get("factual_truth", "")).strip()
            if claim and fact:
                valid_items.append(
                    DescriptionDiscrepancyItem(
                        claim_in_description=claim,
                        factual_truth=fact,
                        contradiction_type="PHANTOM_FEATURE",
                        severity="critical",
                    )
                )

    quality = (raw_quality or "GOOD").strip().upper()
    if quality not in ("RICH_MARKETING", "SPEC_DUMP", "POOR", "EMPTY", "GOOD"):
        quality = "GOOD"

    return valid_items, quality
