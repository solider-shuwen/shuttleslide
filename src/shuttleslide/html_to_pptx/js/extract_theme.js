/**
 * extract_theme.js — Playwright 注入脚本
 *
 * 从渲染后的 DOM 中提取主题信息：
 * - primary_color: 第一个渐变色（通常是品牌色）
 * - accent_color: 出现频率最高的非灰/非黑/非白色
 * - bg_color: .ppt-slide 容器的背景色
 * - text_color: 最常见的文字颜色
 * - font_title / font_body: 最常用的字体
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

    // 判断是否为灰色/黑色/白色（忽略）
    function isNeutralColor(hex) {
        if (!hex || hex.length < 7) return true;
        const r = parseInt(hex.slice(1, 3), 16);
        const g = parseInt(hex.slice(3, 5), 16);
        const b = parseInt(hex.slice(5, 7), 16);
        const max = Math.max(r, g, b);
        const min = Math.min(r, g, b);
        // 饱和度很低 或 很亮/很暗
        if (max - min < 30) return true; // 近灰色
        if (max > 230 && min > 230) return true; // 近白色
        if (max < 30) return true; // 近黑色
        return false;
    }

    const slideEl = document.querySelector('.ppt-slide') || document.body;
    const slideStyle = getComputedStyle(slideEl);

    const gradientColors = [];
    const colorCounts = {};
    const fontFamilies = {};

    for (const el of slideEl.querySelectorAll('*')) {
        const s = getComputedStyle(el);

        // 收集渐变色 → primary_color 候选
        if (s.backgroundImage && s.backgroundImage.includes('linear-gradient')) {
            const hexColors = s.backgroundImage.match(/#[0-9a-fA-F]{6,8}/g);
            if (hexColors) gradientColors.push(...hexColors.slice(0, 6).map(c => c.slice(0, 7)));
            // 也提取 rgba 颜色
            const rgbaMatches = s.backgroundImage.matchAll(/rgba?\((\d+),\s*(\d+),\s*(\d+)/g);
            for (const m of rgbaMatches) {
                const hex = '#' +
                    parseInt(m[1]).toString(16).padStart(2, '0') +
                    parseInt(m[2]).toString(16).padStart(2, '0') +
                    parseInt(m[3]).toString(16).padStart(2, '0');
                gradientColors.push(hex);
            }
        }

        // 统计背景色（非灰/非白）
        const bgColor = rgbToHex(s.backgroundColor);
        if (bgColor && !isNeutralColor(bgColor)) {
            colorCounts[bgColor] = (colorCounts[bgColor] || 0) + 1;
        }

        // 统计文字颜色
        const textColor = rgbToHex(s.color);
        if (textColor && !isNeutralColor(textColor)) {
            colorCounts[textColor] = (colorCounts[textColor] || 0) + 1;
        }

        // 统计字体
        const ff = s.fontFamily;
        if (ff) {
            const mainFont = ff.split(',')[0].trim().replace(/"/g, '');
            fontFamilies[mainFont] = (fontFamilies[mainFont] || 0) + 1;
        }
    }

    // primary_color: 第一个渐变色
    const primaryColor = gradientColors[0] || '#133EFF';

    // accent_color: 出现频率最高的非中性色
    const sortedColors = Object.entries(colorCounts)
        .sort((a, b) => b[1] - a[1])
        .map(([c]) => c);
    const accentColor = sortedColors.find(c => c !== primaryColor) || '#00CD82';

    // bg_color: slide 容器背景色
    const bgColor = rgbToHex(slideStyle.backgroundColor) || '#FEFEFE';

    // 最常用的字体
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
