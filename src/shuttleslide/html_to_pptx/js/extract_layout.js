/**
 * extract_layout.js — Playwright 注入脚本
 *
 * 遍历 .ppt-slide 内的所有可视元素，提取布局信息：
 * - getBoundingClientRect() → 精确像素位置
 * - getComputedStyle() → 字体、颜色、背景等
 *
 * 所有坐标已转为 .ppt-slide 渲染尺寸的百分比。幻灯片尺寸由
 * 模板通过 .ppt-slide 的 CSS 宽高决定（默认 1280x720；调用方
 * 可以覆盖为 9:16 / 1:1 / 3:4 等），脚本直接读取实际渲染尺寸，
 * 所以天然支持任意画布比例而不需要额外参数。
 */
(() => {
    const slideEl = document.querySelector('.ppt-slide') || document.body;
    const slideRect = slideEl.getBoundingClientRect();
    // Use the actual rendered dimensions; fall back to 1280x720 only when
    // the slide hasn't been laid out yet (e.g. display:none) — keeps the
    // script safe to inject in any render state.
    const SLIDE_W = slideRect.width || 1280;
    const SLIDE_H = slideRect.height || 720;

    // 辅助：rgb(r, g, b) / rgba(r, g, b, a) → { hex: "#RRGGBB", alpha: 0.0~1.0 }
    function parseRgba(rgb) {
        if (!rgb || rgb === 'transparent' || rgb === 'rgba(0, 0, 0, 0)') return null;
        const m = rgb.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)/);
        if (!m) return { hex: rgb, alpha: 1.0 }; // 已经是 hex 或其他格式
        const r = parseInt(m[1]).toString(16).padStart(2, '0');
        const g = parseInt(m[2]).toString(16).padStart(2, '0');
        const b = parseInt(m[3]).toString(16).padStart(2, '0');
        const a = m[4] !== undefined ? parseFloat(m[4]) : 1.0;
        return { hex: `#${r}${g}${b}`, alpha: a };
    }

    // 辅助：rgb(r, g, b) → #RRGGBB, rgba(r,g,b,a) → #RRGGBBAA
    function rgbToHex(rgb) {
        const parsed = parseRgba(rgb);
        if (!parsed) return null;
        // Preserve alpha as #RRGGBBAA when < 1.0
        if (parsed.alpha < 1.0) {
            const a = Math.round(parsed.alpha * 255).toString(16).padStart(2, '0');
            return parsed.hex + a;
        }
        return parsed.hex;
    }

    // 辅助：解析 linear-gradient(...)
    function parseGradient(bgImage) {
        if (!bgImage || bgImage === 'none') return null;
        // Use greedy match to handle nested parentheses in rgba() colors
        const gradMatch = bgImage.match(/linear-gradient\((.+)\)$/);
        if (!gradMatch) return null;
        const inner = gradMatch[1];

        // 提取方向：保留原始 CSS 角度（css_<deg> 协议），让下游
        // `gradient_angle_deg` 用通用公式转换。无显式角度时回退到
        // 命名方向，便于向后兼容。
        let direction = 'css_90';  // 默认 'to right'
        const degMatch = inner.match(/(?:^|,)\s*(-?\d+(?:\.\d+)?)deg\s*,/);
        if (degMatch) {
            direction = 'css_' + parseFloat(degMatch[1]);
        } else if (inner.includes('to right')) {
            direction = 'css_90';
        } else if (inner.includes('to bottom')) {
            direction = 'css_180';
        } else if (inner.includes('to left')) {
            direction = 'css_270';
        } else if (inner.includes('to top')) {
            direction = 'css_0';
        }

        // 提取颜色 stop + 可选的显式位置（color 后跟 <num>%）。
        // 用全局正则扫描颜色 token，然后对每个匹配查它后面是否紧跟
        // 百分比。所有 stop 都有显式位置时按位置存；否则均匀分布。
        const colorRegex = /(#(?:[0-9a-fA-F]{3,8})|rgba?\([^)]+\))\s*(\d+(?:\.\d+)?)?%/g;
        const raw = [];
        let m;
        while ((m = colorRegex.exec(inner)) !== null) {
            const parsed = parseRgba(m[1]);
            raw.push({
                color: parsed ? parsed.hex : m[1],
                opacity: parsed ? parsed.alpha : 1.0,
                posStr: m[2],  // undefined when no explicit %
            });
        }
        if (raw.length < 2) return null;

        const allHavePos = raw.every(r => r.posStr !== undefined && r.posStr !== null);
        const stops = raw.map((r, i) => ({
            color: r.color,
            opacity: r.opacity,
            position: allHavePos
                ? Math.max(0, Math.min(1, parseFloat(r.posStr) / 100))
                : (raw.length > 1 ? i / (raw.length - 1) : 0),
        }));

        return { direction, stops };
    }

    // 辅助：px 字符串 → 数值
    function pxToNum(px) {
        if (!px || px === 'auto') return 0;
        return parseFloat(px) || 0;
    }

    // 判断元素是否有视觉内容
    function hasVisualContent(el, style) {
        // SVG (inline <svg data-slot="...">) — always visual even without
        // text/background, otherwise pure-shape diagrams get dropped.
        // Note: tagName is lowercased 'svg' for SVG elements inside HTML
        // documents (foreign content rule), but uppercase 'SVG' in pure
        // XML documents. Accept both.
        if (el.tagName === 'svg' || el.tagName === 'SVG') return true;
        // 有文字
        if (el.textContent && el.textContent.trim().length > 0) return true;
        // 有图片
        if (el.tagName === 'IMG' || el.src || el.getAttribute('src')) return true;
        // 有背景色（非透明）
        const bgColor = style.backgroundColor;
        if (bgColor && bgColor !== 'rgba(0, 0, 0, 0)' && bgColor !== 'transparent') return true;
        // 有渐变背景
        if (style.backgroundImage && style.backgroundImage !== 'none') return true;
        // 有边框
        if (parseFloat(style.borderWidth) > 0) return true;
        // Material Icon
        if (el.classList && (
            el.classList.contains('material-icons') ||
            el.classList.contains('material-symbols-outlined')
        )) return true;

        return false;
    }

    // Walk the DOM parent chain and return the product of every ancestor's
    // CSS opacity. CSS opacity is multiplicative through the DOM: a
    // <div style="opacity:0.25"> wrapping an <svg> makes every shape inside
    // render at 25% — regardless of the SVG's own opacity attribute.
    //
    // We capture this product on every element because pure-container
    // ancestors (e.g. a wrapping <div> with no visual content of its own)
    // are filtered out by isPureContainer below, so the Python-side
    // containment tree can't reconstruct the chain. Storing it here is
    // the only way downstream converters can recover the effective opacity.
    const _opacityMemo = new WeakMap();
    function cumulativeAncestorOpacity(el) {
        // Memoize on DOM nodes so an N-deep DOM doesn't cost N getComputedStyle
        // calls per element — N elements × N depth = O(N²) without this.
        if (_opacityMemo.has(el)) return _opacityMemo.get(el);
        const parent = el.parentElement;
        if (!parent || parent.tagName === 'HTML') {
            _opacityMemo.set(el, 1.0);
            return 1.0;
        }
        const parentOwn = parseFloat(getComputedStyle(parent).opacity);
        const parentCumulative = cumulativeAncestorOpacity(parent);
        const result = (isNaN(parentOwn) ? 1.0 : parentOwn) * parentCumulative;
        _opacityMemo.set(el, result);
        return result;
    }

    // 判断是否为纯容器（无视觉内容，只是布局用）
    function isPureContainer(el, style) {
        // SVG tagName is 'svg' (lowercase) in HTML documents but 'SVG' in XML.
        if (el.tagName === 'IMG' || el.tagName === 'svg' || el.tagName === 'SVG' || el.tagName === 'VIDEO') return false;
        if (el.tagName === 'I') return false; // icon elements
        if (el.children.length === 0) return false; // leaf elements are never pure containers
        return !hasVisualContent(el, style);
    }

    // 内联标签白名单：这些标签的文字会合并进父元素的 inlineRuns，
    // 保留各自样式（颜色/粗细），并保留 <br> 换行结构。
    const INLINE_TAGS = new Set([
        'SPAN', 'STRONG', 'EM', 'B', 'I', 'A', 'U', 'MARK',
        'SMALL', 'SUB', 'SUP', 'BIG', 'S', 'CODE', 'FONT', 'LABEL',
    ]);
    const INLINE_SELECTOR = 'span, strong, em, b, i, a, u, mark, small, sub, sup, big, s, code, font, label';

    // Compute textContent with material-icons / material-symbols-outlined
    // spans removed. Those spans contain ligature names ('auto_awesome',
    // 'science', 'check_circle', ...) that render as icon glyphs via the
    // Material Icons font — NOT display text. Python consumers use this
    // field directly so they don't need a text-shape heuristic to guess
    // whether a short lowercase token ('cu129', 'json', 'v1') is an icon
    // name or real content. Heuristic filtering was observed dropping
    // CUDA version labels 'cu129' / 'cu130' in 6.html.
    //
    // Clone + remove so we don't disturb the live DOM during measurement.
    // Whitespace is collapsed+trimmed to match how `text` field (built
    // from el.textContent) gets normalised downstream.
    function textWithoutIconSpans(el) {
        const clone = el.cloneNode(true);
        const iconEls = clone.querySelectorAll(
            '.material-icons, .material-symbols-outlined'
        );
        for (const iconEl of iconEls) iconEl.remove();
        return (clone.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 500);
    }

    // 递归遍历元素的 inline 子节点，构造 [[run, run], [run]] 结构：
    // 外层数组 = 行（<br> 切分），内层数组 = 同一行的 styled run。
    // block 级子元素会被跳过（它们是独立元素，已自行提取）。
    function extractInlineRuns(el, parentStyle) {
        const lines = [[]];
        const cs = parentStyle || getComputedStyle(el);
        // CSS `white-space: pre` (or pre-wrap/break-spaces) — typically a
        // <pre> element or `whitespace-pre` utility. In this mode whitespace
        // is significant: newlines separate code lines and indentation must
        // survive. HTML's default whitespace-collapsing rules would destroy
        // both, which is what we used to do — turning well-formatted code
        // into a single run-on line.
        const isPreMode = cs.whiteSpace === 'pre'
                       || cs.whiteSpace === 'pre-wrap'
                       || cs.whiteSpace === 'break-spaces';

        function baseRun() {
            return {
                color: rgbToHex(cs.color),
                bold: parseFloat(cs.fontWeight) >= 600 || cs.fontWeight === 'bold',
                italic: cs.fontStyle === 'italic',
                font_size_pt: Math.round((parseFloat(cs.fontSize) || 0) * 0.75 * 10) / 10,
                font_name: cs.fontFamily,
            };
        }

        for (const node of el.childNodes) {
            if (node.nodeType === Node.TEXT_NODE) {
                if (isPreMode) {
                    // <pre> semantics: preserve all whitespace literally.
                    // Mirror the HTML spec's "first newline after <pre>" rule
                    // — a single leading \n on the first child text node is
                    // ignored (it's an artefact of how people format <pre>
                    // source).
                    let text = node.textContent;
                    if (node === el.firstChild && text.startsWith('\n')) {
                        text = text.slice(1);
                    }
                    if (text.length === 0) continue;
                    // Split on \n; each part becomes a new line. Empty parts
                    // (blank lines in the source) just push an empty line.
                    const parts = text.split('\n');
                    for (let i = 0; i < parts.length; i++) {
                        if (i > 0) lines.push([]);
                        if (parts[i].length > 0) {
                            const r = baseRun();
                            r.text = parts[i];
                            lines[lines.length - 1].push(r);
                        }
                    }
                } else {
                    // Normal HTML: collapse runs of whitespace to a single
                    // space. DON'T trim per text node — inter-element
                    // whitespace is significant. The space between
                    // `</span>` and following text in
                    // `<span>--editable</span> flag enables...` must survive
                    // so the runs don't concatenate as "--editableflag".
                    // Block-edge trimming (leading whitespace at the start
                    // of the inline content, trailing at the end) is applied
                    // once, after all children are processed, via
                    // `trimLineEdges` below.
                    const t = node.textContent.replace(/\s+/g, ' ');
                    if (t.length > 0) {
                        const r = baseRun();
                        r.text = t;
                        lines[lines.length - 1].push(r);
                    }
                }
            } else if (node.nodeName === 'BR') {
                lines.push([]);
            } else if (node.nodeType === Node.ELEMENT_NODE
                       && INLINE_TAGS.has(node.nodeName)
                       && !node.classList.contains('material-icons')
                       && !node.classList.contains('material-symbols-outlined')) {
                // 递归内联子元素，合并其行到当前行流。
                // white-space is an inherited CSS property, so child spans
                // inside <pre> also report `pre` and preserve whitespace.
                const childLines = extractInlineRuns(node, getComputedStyle(node));
                childLines.forEach((lr, i) => {
                    if (i > 0) lines.push([]);
                    lines[lines.length - 1].push(...lr);
                });
            }
            // block 子元素（DIV、P、UL 等）跳过：它们是独立元素，已自行提取
        }
        // HTML "trim at block edges" rule: leading whitespace at the start
        // of each line's inline content and trailing whitespace at the end
        // are removed. Inter-element whitespace (a single space between two
        // inline children) is preserved above; only the outermost edges of
        // each line are trimmed here. Without this, a text node like
        // "\n  --editable" would render with a leading space.
        for (const line of lines) {
            // Drop leading whitespace-only (or empty) runs; trim leading
            // whitespace from the first run that has non-whitespace content.
            while (line.length > 0) {
                const t = String(line[0].text || '');
                if (t.length === 0 || /^\s+$/.test(t)) {
                    line.shift();
                } else {
                    line[0].text = t.replace(/^\s+/, '');
                    break;
                }
            }
            // Same for trailing.
            while (line.length > 0) {
                const t = String(line[line.length - 1].text || '');
                if (t.length === 0 || /^\s+$/.test(t)) {
                    line.pop();
                } else {
                    line[line.length - 1].text = t.replace(/\s+$/, '');
                    break;
                }
            }
        }
        // 去掉尾部空行
        while (lines.length > 1 && lines[lines.length - 1].length === 0) lines.pop();
        return lines;
    }

    // Canvas for text measurement
    const _measureCanvas = document.createElement('canvas');
    const _measureCtx = _measureCanvas.getContext('2d');

    function measureTextWidth(text, font) {
        if (!text || !font) return 0;
        _measureCtx.font = font;
        // Return the maximum line width for multi-line text
        const lines = text.split('\n');
        let maxW = 0;
        for (const line of lines) {
            const w = _measureCtx.measureText(line).width;
            if (w > maxW) maxW = w;
        }
        return maxW;
    }

    const elements = [];
    const allEls = slideEl.querySelectorAll('*');

    for (const el of allEls) {
        // Skip descendants of inline <svg>: the entire <svg> is captured
        // as svg_markup (see the SVG branch below) and converted to a
        // single native <p:grpSp> shape by the vendored svg_to_pptx
        // library. Without this filter, every <text>/<rect>/<circle>/
        // <g>/<path>/… inside the SVG would ALSO be emitted as a
        // standalone element — producing one duplicate textbox/shape
        // per SVG child on top of the SVG group's own rendering.
        // Verified on tmp/agent_gen_output/5.html where six <text>
        // labels each rendered twice in verify_5.pptx.
        //
        // el.closest('svg') returns the nearest <svg> ancestor (or el
        // itself if el is the <svg>). Skip only when an ancestor exists
        // and it isn't el — i.e. descendants only, never the <svg>
        // itself. This is the root-cause fix for the bug class "SVG
        // descendants double-counted as standalone elements"; defense
        // in depth at classifier level would mask future regressions
        // in this extraction layer.
        const svgAncestor = el.closest('svg');
        if (svgAncestor && svgAncestor !== el) continue;

        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);

        // 过滤不可见元素
        if (rect.width === 0 || rect.height === 0) continue;
        if (style.display === 'none' || style.visibility === 'hidden') continue;
        if (parseFloat(style.opacity) === 0) continue;

        // 过滤纯容器
        if (isPureContainer(el, style)) continue;

        // 相对于 slide 的位置，转为百分比
        const relX = rect.left - slideRect.left;
        const relY = rect.top - slideRect.top;
        const xPct = Math.round((relX / slideRect.width) * 10000) / 100;
        const yPct = Math.round((relY / slideRect.height) * 10000) / 100;
        const wPct = Math.round((rect.width / slideRect.width) * 10000) / 100;
        const hPct = Math.round((rect.height / slideRect.height) * 10000) / 100;

        // 字体大小 px → pt
        const fontSizePx = parseFloat(style.fontSize) || 0;
        const fontSizePt = Math.round(fontSizePx * 0.75 * 10) / 10;

        // Cumulative ancestor opacity: product of every DOM ancestor's
        // CSS opacity. Combined with the element's own opacity below to
        // produce styles.cumulative_opacity — the effective opacity the
        // browser actually renders this element at. Downstream converters
        // read this instead of the own-only opacity so wrapping
        // <div style="opacity:…"> is honoured.
        const ownOpacity = parseFloat(style.opacity);
        const ancestorOpacity = cumulativeAncestorOpacity(el);
        const cumulativeOpacity = (isNaN(ownOpacity) ? 1.0 : ownOpacity) * ancestorOpacity;

        // 构建元素数据
        const elemData = {
            tag: el.tagName,
            // Use getAttribute('class') instead of el.className:
            // SVG elements expose className as SVGAnimatedString (not a
            // string), so .split() fails. getAttribute returns a string
            // (or null) for both HTML and SVG elements.
            classes: (el.getAttribute('class') || '').split(/\s+/).filter(c => c),
            text: (el.textContent || '').trim().slice(0, 500),
            text_no_icons: textWithoutIconSpans(el), // textContent 去掉 material-icons span 的 ligature 名
            directText: '', // 直接子文本（不含嵌套元素的文字）
            rect_pct: { x: xPct, y: yPct, w: wPct, h: hPct },
            rect_px: { x: Math.round(relX), y: Math.round(relY), w: Math.round(rect.width), h: Math.round(rect.height) },
            styles: {
                fontSize_px: fontSizePx,
                fontSize_pt: fontSizePt,
                fontFamily: style.fontFamily,
                color: rgbToHex(style.color),
                fontWeight: style.fontWeight,
                fontStyle: style.fontStyle,
                textAlign: style.textAlign,
                backgroundColor: rgbToHex(style.backgroundColor),
                backgroundGradient: parseGradient(style.backgroundImage),
                backgroundImage: style.backgroundImage !== 'none' ? style.backgroundImage : null,
                borderRadius: style.borderRadius,
                opacity: parseFloat(style.opacity),
                ancestor_opacity: ancestorOpacity,
                cumulative_opacity: cumulativeOpacity,
                borderWidth: parseFloat(style.borderWidth) || 0,
                borderColor: rgbToHex(style.borderColor),
                borderStyle: style.borderStyle,
                // Per-side border (extracted separately because border shorthand
                // returns "" when sides differ — e.g. CSS `border-left: 4px solid`).
                borderLeftWidth: parseFloat(style.borderLeftWidth) || 0,
                borderLeftColor: rgbToHex(style.borderLeftColor),
                borderTopWidth: parseFloat(style.borderTopWidth) || 0,
                borderTopColor: rgbToHex(style.borderTopColor),
                borderRightWidth: parseFloat(style.borderRightWidth) || 0,
                borderRightColor: rgbToHex(style.borderRightColor),
                borderBottomWidth: parseFloat(style.borderBottomWidth) || 0,
                borderBottomColor: rgbToHex(style.borderBottomColor),
                boxShadow: style.boxShadow !== 'none' ? style.boxShadow : null,
                filter: style.filter !== 'none' ? style.filter : null,
                position: style.position,
                display: style.display,
                objectFit: style.objectFit,
            },
            attrs: {},
            child_count: el.children.length,
            is_icon: !!(el.classList && (
                el.classList.contains('material-icons') ||
                el.classList.contains('material-symbols-outlined')
            )),
            icon_font: el.classList.contains('material-icons') ? 'material-icons'
                     : el.classList.contains('material-symbols-outlined') ? 'material-symbols-outlined'
                     : null,
        };

        // 提取直接文本（排除子元素的文本）
        for (const node of el.childNodes) {
            if (node.nodeType === Node.TEXT_NODE) {
                const t = node.textContent.trim();
                if (t) elemData.directText += t + ' ';
            }
        }
        elemData.directText = elemData.directText.trim();

        // 提取结构化 inlineRuns：<br> 换行 + 内联 span/strong 等的样式化文字
        // 只有当确实有多行或多 run 时才存储，避免给每个元素都增加无意义体积。
        const inlineLines = extractInlineRuns(el);
        const hasBR = inlineLines.length > 1;
        const hasMultiRun = inlineLines.some(l => l.length > 1);
        if (hasBR || hasMultiRun) {
            elemData.inlineRuns = inlineLines;
        }

        // 提取关键属性
        if (el.src) elemData.attrs.src = el.src;
        if (el.href) elemData.attrs.href = el.href;
        if (el.alt) elemData.attrs.alt = el.alt;
        if (el.id) elemData.attrs.id = el.id;

        // SVG: 捕获原始 markup + slot_id + viewBox，供 Phase 2 转 DrawingML
        // tagName is 'svg' (lowercase) in HTML, 'SVG' in XML — accept both.
        if (el.tagName === 'svg' || el.tagName === 'SVG') {
            elemData.attrs.svg_markup = el.outerHTML;
            const slot = el.getAttribute('data-slot');
            if (slot) elemData.attrs['data-slot'] = slot;
            const vb = el.getAttribute('viewBox') || el.getAttribute('viewbox');
            if (vb) elemData.attrs.viewBox = vb;
        }

        // 真实行数 + 最长行宽度：Range.getClientRects 在 block 元素上返回
        // 每个视觉行一个 rect（不同于 Element.getClientRects 只返回 block box）。
        // 这是判断"单行 vs 多行"和"实际文字宽度"的权威信号，供 Stage 2
        // _widen_text_position 用真实渲染结果替代 canvas.measureText 的理论
        // 单行宽度猜测。
        const range = document.createRange();
        range.selectNodeContents(el);
        const lineRects = Array.from(range.getClientRects())
            .filter(r => r.width > 0 && r.height > 0);
        const rangeLineCount = lineRects.length;
        const rangeMaxLineWidthPx = lineRects.length
            ? Math.max(...lineRects.map(r => r.width))
            : 0;
        elemData.range_line_count = rangeLineCount;
        elemData.range_max_line_width_px = Math.round(rangeMaxLineWidthPx);
        elemData.range_max_line_width_pct = Math.round(
            (rangeMaxLineWidthPx / slideRect.width) * 10000
        ) / 100;

        // 文本自然宽度测量（用 canvas.measureText）
        // 保留作为 fallback：当 Range 拿不到数据（如纯图标、空文本）时使用。
        const measureText = elemData.directText || elemData.text.slice(0, 200);
        if (measureText && fontSizePx > 0) {
            const fontStr = `${style.fontWeight} ${style.fontStyle} ${fontSizePx}px ${style.fontFamily}`;
            const naturalWidthPx = measureTextWidth(measureText, fontStr);
            elemData.textNaturalWidth_px = Math.round(naturalWidthPx);
            elemData.textNaturalWidth_pct = Math.round((naturalWidthPx / slideRect.width) * 10000) / 100;
        } else {
            elemData.textNaturalWidth_px = 0;
            elemData.textNaturalWidth_pct = 0;
        }

        // Tag element with unique index for screenshot selection
        const ssIdx = elements.length;
        el.setAttribute('data-ss-idx', String(ssIdx));
        elemData.ss_idx = ssIdx;

        elements.push(elemData);
    }

    // --- 标记被父元素 inline 合并的子元素（span 等）---
    // 这些子元素的文字已经合并进父元素的 inlineRuns，不应再独立渲染，
    // 否则会与父元素文字重复。classifier 会跳过 absorbedByParent=true 的元素。
    for (let i = 0; i < elements.length; i++) {
        if (!elements[i].inlineRuns) continue;
        const parentEl = slideEl.querySelector(`[data-ss-idx="${i}"]`);
        if (!parentEl) continue;
        // flex/grid 容器把子元素按空间位置排布——内联子元素（span）是各自
        // 独立的定位单元格（如 div-table 的一行），而不是内联流的文本 run。
        // 把它们合并进父元素 inlineRuns（并标记 absorbed）会把一整行表格
        // 压成一条挤在一起的文本。跳过吸收，让每个单元格按自身 rect 渲染。
        const parentDisplay = getComputedStyle(parentEl).display;
        if (parentDisplay === 'flex' || parentDisplay === 'inline-flex'
            || parentDisplay === 'grid' || parentDisplay === 'inline-grid') continue;
        // 查找所有内联后代（已被父元素 inlineRuns 合并）
        const inlines = parentEl.querySelectorAll(INLINE_SELECTOR);
        for (const inlineEl of inlines) {
            // 跳过内联后代中的图标（它们由 icon_text 单独处理）
            if (inlineEl.classList.contains('material-icons')
                || inlineEl.classList.contains('material-symbols-outlined')) continue;
            const childIdxAttr = inlineEl.getAttribute('data-ss-idx');
            if (childIdxAttr !== null) {
                const childIdx = parseInt(childIdxAttr, 10);
                if (!isNaN(childIdx) && elements[childIdx]) {
                    elements[childIdx].absorbedByParent = true;
                }
            }
        }
    }

    // --- 第二遍：用 elementsFromPoint 计算真实层叠顺序 ---
    for (let i = 0; i < elements.length; i++) {
        const ssIdx = elements[i].ss_idx;
        const el = slideEl.querySelector(`[data-ss-idx="${ssIdx}"]`);
        if (!el) { elements[i].z_order = i; continue; }

        const rect = el.getBoundingClientRect();
        const cx = rect.left + rect.width / 2;
        const cy = rect.top + rect.height / 2;

        // elementsFromPoint 返回该坐标处所有元素，最上层在前
        const stack = document.elementsFromPoint(cx, cy);

        // 统计有多少已提取元素在当前元素之上
        // 跳过后代元素——它们是当前元素的子内容，不算独立遮挡层
        let aboveCount = 0;
        for (const stackEl of stack) {
            if (stackEl === el) break;
            if (stackEl.hasAttribute && stackEl.hasAttribute('data-ss-idx')) {
                if (el.contains(stackEl)) continue; // 跳过后代
                aboveCount++;
            }
        }
        // aboveCount=0 → 最上层 → z_order 最高（最后渲染，在最前）
        // aboveCount=3 → 上面有3层 → z_order 较低（先渲染，在后面）
        elements[i].z_order = -aboveCount;
    }

    return {
        slide_rect_px: {
            x: Math.round(slideRect.left),
            y: Math.round(slideRect.top),
            w: Math.round(slideRect.width),
            h: Math.round(slideRect.height),
        },
        element_count: elements.length,
        elements: elements,
    };
})();
