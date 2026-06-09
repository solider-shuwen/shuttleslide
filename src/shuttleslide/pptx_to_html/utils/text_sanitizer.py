"""
Text Sanitizer - handles PowerPoint special Unicode characters.
PowerPoint uses private Unicode area characters for special symbols.
"""

from typing import Dict

# Mapping of PowerPoint private Unicode characters to standard Unicode
PPT_SPECIAL_CHARS: Dict[str, str] = {
    # Arrows
    "\uf0e0": "→",  # Right arrow
    "\uf0d8": "←",  # Left arrow
    "\uf0d9": "↑",  # Up arrow
    "\uf0da": "↓",  # Down arrow
    "\uf0db": "↔",  # Left-right arrow
    "\uf0dc": "↕",  # Up-down arrow

    # Bullets
    "\uf0a7": "•",  # Bullet point
    "\uf0b7": "•",  # Bullet point (alternative)
    "\uf022": "◦",  # White bullet
    "\uf023": "▪",  # Square bullet
    "\uf024": "▫",  # White square bullet

    # Checkmarks and crosses
    "\uf0fc": "✓",  # Check mark
    "\uf0fb": "✗",  # Cross mark
    "\uf0fd": "✔",  # Heavy check mark
    "\uf0fe": "✖",  # Heavy cross mark

    # Stars and symbols
    "\uf0a5": "★",  # Star
    "\uf0a6": "☆",  # White star
    "\uf0a8": "✦",  # Four-pointed star
    "\uf0a9": "✶",  # Black four-pointed star

    # Mathematical symbols
    "\uf0b2": "≠",  # Not equal
    "\uf0b3": "≤",  # Less than or equal
    "\uf0b4": "≥",  # Greater than or equal
    "\uf0b5": "±",  # Plus-minus
    "\uf0b6": "×",  # Multiplication
    "\uf0b7": "÷",  # Division

    # Common symbols
    "\uf020": "©",  # Copyright
    "\uf021": "®",  # Registered trademark
    "\uf025": "™",  # Trademark
    "\uf02d": "€",  # Euro
    "\uf02e": "£",  # Pound
    "\uf02f": "¥",  # Yen
    "\uf030": "¢",  # Cent

    # Box drawing characters
    "\uf040": "─",  # Horizontal line
    "\uf041": "│",  # Vertical line
    "\uf042": "┌",  # Top-left corner
    "\uf043": "┐",  # Top-right corner
    "\uf044": "└",  # Bottom-left corner
    "\uf045": "┘",  # Bottom-right corner

    # Geometric shapes
    "\uf050": "■",  # Black square
    "\uf051": "□",  # White square
    "\uf052": "▲",  # Black triangle
    "\uf053": "△",  # White triangle
    "\uf054": "▼",  # Black triangle down
    "\uf055": "▽",  # White triangle down
    "\uf056": "◆",  # Black diamond
    "\uf057": "◇",  # White diamond
    "\uf058": "●",  # Black circle
    "\uf059": "○",  # White circle

    # Additional common mappings
    "\uf000": "",  # Null character - remove
    "\uf001": "",  # Control character - remove
    "\uf002": "",  # Control character - remove
}


def sanitize_pptx_text(text: str) -> str:
    """
    Replace PowerPoint special Unicode characters with standard Unicode equivalents.

    Args:
        text: String that may contain PowerPoint special characters

    Returns:
        String with special characters replaced by standard Unicode

    Examples:
        >>> sanitize_pptx_text("Item\\uf0a7 First")  # PowerPoint bullet
        'Item• First'
        >>> sanitize_pptx_text("Next\\uf0e0")  # PowerPoint arrow
        'Next→'
    """
    if not text:
        return text

    result = text
    for ppt_char, replacement in PPT_SPECIAL_CHARS.items():
        result = result.replace(ppt_char, replacement)

    return result


def has_special_chars(text: str) -> bool:
    """
    Check if text contains PowerPoint special characters.

    Args:
        text: String to check

    Returns:
        True if text contains PowerPoint special characters, False otherwise
    """
    if not text:
        return False

    return any(char in PPT_SPECIAL_CHARS for char in text)


def get_special_char_count(text: str) -> int:
    """
    Count the number of PowerPoint special characters in text.

    Args:
        text: String to analyze

    Returns:
        Number of PowerPoint special characters found
    """
    if not text:
        return 0

    count = 0
    for char in text:
        if char in PPT_SPECIAL_CHARS:
            count += 1

    return count