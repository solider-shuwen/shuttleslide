"""
XML namespace constants for OpenXML parsing.

Centralizes all XML namespace definitions used throughout the project.
"""

# Full namespace set (DrawingML + PresentationML + Relationships)
NAMESPACES = {
    'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
    'p': 'http://schemas.openxmlformats.org/presentationml/2006/main',
    'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
}

# Commonly used subset - DrawingML only
NS_A = {'a': NAMESPACES['a']}

# Clark notation constants for direct attribute/element access
# Usage: elem.get(f"{NS_R}embed") or elem.find(f".//{{{NS_P}}}show")
NS_A_CLARK = f"{{{NAMESPACES['a']}}}"
NS_P_CLARK = f"{{{NAMESPACES['p']}}}"
NS_R_CLARK = f"{{{NAMESPACES['r']}}}"
