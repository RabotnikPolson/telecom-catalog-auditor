"""
Backwards-compatibility re-export module.
The project has migrated entirely to OpenAIJudge in `openai_judge.py`.
"""

from .openai_judge import (
    GeminiJudge,
    LLMDiscrepancy,
    LLMJudgeOutput,
    LLMMissingSpec,
    OpenAIJudge,
    _check_metric_unit_equivalence,
    _is_equivalent_value,
    _normalize_tech_val,
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
