/**
 * extract_background.js — Playwright injection script
 *
 * Extracts background properties of the .ppt-slide container:
 * - background color
 * - gradient (parses linear-gradient)
 * - background image URL
 */
(() => {
    const slideEl = document.querySelector('.ppt-slide') || document.body;
    const style = getComputedStyle(slideEl);

    function rgbToHex(rgb) {
        if (!rgb || rgb === 'transparent' || rgb === 'rgba(0, 0, 0, 0)') return null;
        const m = rgb.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
        if (!m) return rgb;
        const r = parseInt(m[1]).toString(16).padStart(2, '0');
        const g = parseInt(m[2]).toString(16).padStart(2, '0');
        const b = parseInt(m[3]).toString(16).padStart(2, '0');
        return `#${r}${g}${b}`;
    }

    function parseGradient(bgImage) {
        if (!bgImage || bgImage === 'none') return null;
        const gradMatch = bgImage.match(/linear-gradient\(([^)]+)\)/);
        if (!gradMatch) return null;
        const inner = gradMatch[1];

        let direction = 'horizontal';
        if (inner.includes('135deg')) direction = 'diagonal_135';
        else if (inner.includes('45deg')) direction = 'diagonal_45';
        else if (inner.includes('to right') || inner.includes('90deg')) direction = 'horizontal';
        else if (inner.includes('to bottom') || inner.includes('180deg')) direction = 'vertical';

        const colorRegex = /#(?:[0-9a-fA-F]{3,8})|rgba?\([^)]+\)/g;
        const colors = inner.match(colorRegex);
        if (!colors || colors.length < 2) return null;

        const stops = colors.map((color, i) => ({
            color: rgbToHex(color) || color,
            position: colors.length > 1 ? i / (colors.length - 1) : 0,
            opacity: 1.0,
        }));

        return { direction, stops };
    }

    // Extract background image URL (if url(...) is present)
    function extractImageUrl(bgImage) {
        if (!bgImage || bgImage === 'none') return null;
        const urlMatch = bgImage.match(/url\(["']?([^"')]+)["']?\)/);
        return urlMatch ? urlMatch[1] : null;
    }

    const bgColor = rgbToHex(style.backgroundColor);
    const gradient = parseGradient(style.backgroundImage);
    const imageUrl = extractImageUrl(style.backgroundImage);

    // Also check inline style and class-level background
    const inlineBg = slideEl.style.background || slideEl.style.backgroundColor;
    const inlineBgImage = slideEl.style.backgroundImage;

    return {
        color: bgColor,
        gradient: gradient,
        image_url: imageUrl,
        raw_background: style.background,
        raw_backgroundColor: style.backgroundColor,
        raw_backgroundImage: style.backgroundImage,
    };
})();
