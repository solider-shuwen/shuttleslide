/**
 * extract_theme.js — Playwright injection script
 *
 * Extracts theme information from the rendered DOM:
 * - primary_color: the first gradient color (usually the brand color)
 * - accent_color: the most frequent non-gray / non-black / non-white color
 * - bg_color: background color of the .ppt-slide container
 * - text_color: the most common text color
 * - font_title / font_body: the most-used fonts
 */
(() => {
    function rgbToHex(rgb) {
        if (!rgb || rgb === 'transparent' || rgb === 'rgba(0, 0, 0, 0)') return null;
        const m = rgb.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
        if (!m) return rgb;
        const r = parseInt(m[1]).toString(16).padStart(2, '0');
        const g = parseInt(m[2]).toString(16).padStart(2, '0');
        const b = parseInt(m[3]).toString(16).padStart(2, '0');
        return `#${r}${g}${b}`;
    }

    // Determine whether a color is gray/black/white (to be ignored)
    function isNeutralColor(hex) {
        if (!hex || hex.length < 7) return true;
        const r = parseInt(hex.slice(1, 3), 16);
        const g = parseInt(hex.slice(3, 5), 16);
        const b = parseInt(hex.slice(5, 7), 16);
        const max = Math.max(r, g, b);
        const min = Math.min(r, g, b);
        // Very low saturation, or very bright/very dark
        if (max - min < 30) return true; // near gray
        if (max > 230 && min > 230) return true; // near white
        if (max < 30) return true; // near black
        return false;
    }

    const slideEl = document.querySelector('.ppt-slide') || document.body;
    const slideStyle = getComputedStyle(slideEl);

    const gradientColors = [];
    const colorCounts = {};
    const fontFamilies = {};

    for (const el of slideEl.querySelectorAll('*')) {
        const s = getComputedStyle(el);

        // Collect gradient colors → primary_color candidates
        if (s.backgroundImage && s.backgroundImage.includes('linear-gradient')) {
            const hexColors = s.backgroundImage.match(/#[0-9a-fA-F]{6,8}/g);
            if (hexColors) gradientColors.push(...hexColors.slice(0, 6).map(c => c.slice(0, 7)));
            // Also extract rgba colors
            const rgbaMatches = s.backgroundImage.matchAll(/rgba?\((\d+),\s*(\d+),\s*(\d+)/g);
            for (const m of rgbaMatches) {
                const hex = '#' +
                    parseInt(m[1]).toString(16).padStart(2, '0') +
                    parseInt(m[2]).toString(16).padStart(2, '0') +
                    parseInt(m[3]).toString(16).padStart(2, '0');
                gradientColors.push(hex);
            }
        }

        // Tally background colors (non-gray / non-white)
        const bgColor = rgbToHex(s.backgroundColor);
        if (bgColor && !isNeutralColor(bgColor)) {
            colorCounts[bgColor] = (colorCounts[bgColor] || 0) + 1;
        }

        // Tally text colors
        const textColor = rgbToHex(s.color);
        if (textColor && !isNeutralColor(textColor)) {
            colorCounts[textColor] = (colorCounts[textColor] || 0) + 1;
        }

        // Tally fonts
        const ff = s.fontFamily;
        if (ff) {
            const mainFont = ff.split(',')[0].trim().replace(/"/g, '');
            fontFamilies[mainFont] = (fontFamilies[mainFont] || 0) + 1;
        }
    }

    // primary_color: first gradient color
    const primaryColor = gradientColors[0] || '#133EFF';

    // accent_color: most frequent non-neutral color
    const sortedColors = Object.entries(colorCounts)
        .sort((a, b) => b[1] - a[1])
        .map(([c]) => c);
    const accentColor = sortedColors.find(c => c !== primaryColor) || '#00CD82';

    // bg_color: slide container background color
    const bgColor = rgbToHex(slideStyle.backgroundColor) || '#FEFEFE';

    // Most-used font
    const sortedFonts = Object.entries(fontFamilies)
        .sort((a, b) => b[1] - a[1])
        .map(([f]) => f);
    const mainFont = sortedFonts[0] || 'Roboto';

    return {
        primary_color: primaryColor,
        accent_color: accentColor,
        bg_color: bgColor,
        text_color: '#1F2937',
        font_title: mainFont,
        font_body: mainFont,
    };
})();
