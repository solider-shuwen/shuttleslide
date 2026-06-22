"""
Rule-based HTML-to-PPTX transformer (no LLM required).

Provides deterministic element classification using spatial analysis
and pattern matching rules for slide conversion.
"""

from shuttleslide.html_to_pptx.rule.transformer import RuleSlideTransformer

__all__ = ["RuleSlideTransformer"]
