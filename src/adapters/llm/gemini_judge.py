"""
Backwards-compatibility re-export module.
The project has migrated entirely to OpenAIJudge in `openai_judge.py`.
"""

from .openai_judge import (
    OpenAIJudge,
    GeminiJudge,
    LLMDiscrepancy,
    LLMMissingSpec,
    LLMJudgeOutput,
    _normalize_tech_val,
    _check_metric_unit_equivalence,
    _is_equivalent_value,
)

__all__ = [
    "OpenAIJudge",
    "GeminiJudge",
    "LLMDiscrepancy",
    "LLMMissingSpec",
    "LLMJudgeOutput",
    "_normalize_tech_val",
    "_check_metric_unit_equivalence",
    "_is_equivalent_value",
]
