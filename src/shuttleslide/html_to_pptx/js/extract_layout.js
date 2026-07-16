/**
 * extract_layout.js — Playwright injection script
 *
 * Walks every visible element inside .ppt-slide and extracts layout info:
 * - getBoundingClientRect() → precise pixel positions
 * - getComputedStyle() → font, color, background, etc.
 *
 * All coordinates are normalized to percentages of .ppt-slide's rendered
 * dimensions. Slide dimensions come from the template's CSS width/height on
 * .ppt-slide (default 1280x720; callers may override to 9:16 / 1:1 / 3:4 /
 * etc.). The script reads the actual rendered dimensions directly, so it
 * supports any canvas aspect ratio with no extra parameters.
 */
(() => {
    const slideEl = document.querySelector('.ppt-slide') || document.body;
    const slideRect = slideEl.getBoundingClientRect();
    // Use the actual rendered dimensions; fall back to 1280x720 only when
    // the slide hasn't been laid out yet (e.g. display:none) — keeps the
    // script safe to inject in any render state.
    const SLIDE_W = slideRect.width || 1280;
    const SLIDE_H = slideRect.height || 720;

    // Helper: rgb(r, g, b) / rgba(r, g, b, a) → { hex: "#RRGGBB", alpha: 0.0~1.0 }
    function parseRgba(rgb) {
        if (!rgb || rgb === 'transparent' || rgb === 'rgba(0, 0, 0, 0)') return null;
        const m = rgb.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)/);
        if (!m) return { hex: rgb, alpha: 1.0 }; // already hex or another format
        const r = parseInt(m[1]).toString(16).padStart(2, '0');
        const g = parseInt(m[2]).toString(16).padStart(2, '0');
        const b = parseInt(m[3]).toString(16).padStart(2, '0');
        const a = m[4] !== undefined ? parseFloat(m[4]) : 1.0;
        return { hex: `#${r}${g}${b}`, alpha: a };
    }

    // Helper: rgb(r, g, b) → #RRGGBB, rgba(r,g,b,a) → #RRGGBBAA
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

    // Helper: parse linear-gradient(...)
    function parseGradient(bgImage) {
        if (!bgImage || bgImage === 'none') return null;
        // Use greedy match to handle nested parentheses in rgba() colors
        const gradMatch = bgImage.match(/linear-gradient\((.+)\)$/);
        if (!gradMatch) return null;
        const inner = gradMatch[1];

        // Extract direction: preserve the raw CSS angle (css_<deg> protocol)
        // so downstream `gradient_angle_deg` can convert via the general
        // formula. Fall back to a named direction when no explicit angle
        // is present, for backwards compatibility.
        //
        // The default MUST be ``css_180`` (to bottom) — this is the
        // default direction specified by CSS Images Module Level 4 when
        // ``linear-gradient()`` is called with no direction argument.
        // Chrome normalises ``linear-gradient(180deg, ...)`` and
        // ``linear-gradient(to bottom, ...)`` into the same directionless
        // string (``linear-gradient(<stops>)``), so both forms land in
        // this fallback. If the default were wrongly set to ``css_90``,
        // every top-down fade-in / overlay would render left-to-right.
        let direction = 'css_180';
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

        // Extract color stops + optional explicit positions (color followed
        // by <num>%). Use a global regex to scan color tokens, then for
        // each match check whether a percentage follows. When every stop
        // has an explicit position, store by position; otherwise distribute
        // evenly. colorRegex MUST explicitly match the CSS `transparent`
        // keyword — it's neither #hex nor rgba(), so it would otherwise be
        // silently dropped, stripping the transparent top stop from a
        // fade-in overlay (e.g. slide 1's `linear-gradient(180deg,
        // transparent 0%, #1A6DFF 25%, ...)`).
        const colorRegex = /(transparent|#(?:[0-9a-fA-F]{3,8})|rgba?\([^)]+\))\s*(\d+(?:\.\d+)?)?%/g;
        const raw = [];
        let m;
        while ((m = colorRegex.exec(inner)) !== null) {
            const literal = m[1];
            // CSS `transparent` and `rgba(0,0,0,0)` both denote alpha=0 black.
            // Inside a gradient stop they MUST be preserved as alpha=0,
            // otherwise a fade-in overlay would start from solid bright blue
            // and diverge from the HTML visual. Don't reuse parseRgba here:
            // it returns null for `transparent` (so rgbToHex returns null
            // during bg-color extraction, which the Python-side
            // _has_bg_color filter relies on); the gradient path needs the
            // opposite behaviour.
            let hex, alpha;
            if (literal === 'transparent' || literal === 'rgba(0, 0, 0, 0)' || literal === 'rgba(0,0,0,0)') {
                hex = '#000000';
                alpha = 0.0;
            } else {
                const parsed = parseRgba(literal);
                hex = parsed ? parsed.hex : literal;
                alpha = parsed ? parsed.alpha : 1.0;
            }
            raw.push({
                color: hex,
                opacity: alpha,
                posStr: m[2],  // posStr: undefined when no explicit %
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

    // Helper: px string → numeric value
    function pxToNum(px) {
        if (!px || px === 'auto') return 0;
        return parseFloat(px) || 0;
    }

    // Determine whether an element has visual content
    function hasVisualContent(el, style) {
        // SVG (inline <svg data-slot="...">) — always visual even without
        // text/background, otherwise pure-shape diagrams get dropped.
        // Note: tagName is lowercased 'svg' for SVG elements inside HTML
        // documents (foreign content rule), but uppercase 'SVG' in pure
        // XML documents. Accept both.
        if (el.tagName === 'svg' || el.tagName === 'SVG') return true;
        // Has text
        if (el.textContent && el.textContent.trim().length > 0) return true;
        // Has image
        if (el.tagName === 'IMG' || el.src || el.getAttribute('src')) return true;
        // Has non-transparent background color
        const bgColor = style.backgroundColor;
        if (bgColor && bgColor !== 'rgba(0, 0, 0, 0)' && bgColor !== 'transparent') return true;
        // Has gradient background
        if (style.backgroundImage && style.backgroundImage !== 'none') return true;
        // Has border
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

    // Determine whether an element is a pure container (no visual content, layout only)
    function isPureContainer(el, style) {
        // SVG tagName is 'svg' (lowercase) in HTML documents but 'SVG' in XML.
        if (el.tagName === 'IMG' || el.tagName === 'svg' || el.tagName === 'SVG' || el.tagName === 'VIDEO') return false;
        if (el.tagName === 'I') return false; // icon elements
        if (el.children.length === 0) return false; // leaf elements are never pure containers
        return !hasVisualContent(el, style);
    }

    // Inline-tag whitelist: text inside these tags is merged into the
    // parent element's inlineRuns, preserving per-run styles (color/weight)
    // and the <br> line-break structure.
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

    // Recursively walk an element's inline children, building a
    // [[run, run], [run]] structure: the outer array = lines (split by
    // <br>), the inner array = styled runs on the same line. Block-level
    // children are skipped (they are independent elements and get
    // extracted on their own).
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
                // Recurse into inline children, merging their lines into the current line stream.
                // white-space is an inherited CSS property, so child spans
                // inside <pre> also report `pre` and preserve whitespace.
                const childLines = extractInlineRuns(node, getComputedStyle(node));
                childLines.forEach((lr, i) => {
                    if (i > 0) lines.push([]);
                    lines[lines.length - 1].push(...lr);
                });
            }
            // Skip block children (DIV, P, UL, ...): they are independent elements extracted on their own.
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
        // Drop trailing empty lines
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

        // Filter out invisible elements
        if (rect.width === 0 || rect.height === 0) continue;
        if (style.display === 'none' || style.visibility === 'hidden') continue;
        if (parseFloat(style.opacity) === 0) continue;

        // Filter out pure containers
        if (isPureContainer(el, style)) continue;

        // Position relative to the slide, expressed as percentages
        const relX = rect.left - slideRect.left;
        const relY = rect.top - slideRect.top;
        const xPct = Math.round((relX / slideRect.width) * 10000) / 100;
        const yPct = Math.round((relY / slideRect.height) * 10000) / 100;
        const wPct = Math.round((rect.width / slideRect.width) * 10000) / 100;
        const hPct = Math.round((rect.height / slideRect.height) * 10000) / 100;

        // Font size px → pt
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

        // Build the element data record
        const elemData = {
            tag: el.tagName,
            // Use getAttribute('class') instead of el.className:
            // SVG elements expose className as SVGAnimatedString (not a
            // string), so .split() fails. getAttribute returns a string
            // (or null) for both HTML and SVG elements.
            classes: (el.getAttribute('class') || '').split(/\s+/).filter(c => c),
            text: (el.textContent || '').trim().slice(0, 500),
            text_no_icons: textWithoutIconSpans(el), // textContent with material-icons span ligature names stripped
            directText: '', // direct child text only (excludes nested-element text)
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
                // Computed style.objectFit is the source of truth for HTML
                // <img>/<video>. For inline <svg> elements it's unreliable
                // (browsers don't apply object-fit to inline SVG content —
                // that's preserveAspectRatio's job), so fall back to the
                // data-object-fit attribute stamped by inline_svg_placeholders
                // when the SVG came from a shuttleslide-svg-placeholder <img>.
                objectFit: (style.objectFit && style.objectFit !== 'fill')
                    ? style.objectFit
                    : (el.getAttribute('data-object-fit') || 'fill'),
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

        // Extract direct text (excluding child-element text)
        for (const node of el.childNodes) {
            if (node.nodeType === Node.TEXT_NODE) {
                const t = node.textContent.trim();
                if (t) elemData.directText += t + ' ';
            }
        }
        elemData.directText = elemData.directText.trim();

        // Extract structured inlineRuns: <br> line breaks + styled text from
        // inline span/strong/etc. Only stored when there are actually
        // multiple lines or multiple runs, to avoid bloating every element.
        const inlineLines = extractInlineRuns(el);
        const hasBR = inlineLines.length > 1;
        const hasMultiRun = inlineLines.some(l => l.length > 1);
        if (hasBR || hasMultiRun) {
            elemData.inlineRuns = inlineLines;
        }

        // Extract key attributes
        if (el.src) elemData.attrs.src = el.src;
        if (el.href) elemData.attrs.href = el.href;
        if (el.alt) elemData.attrs.alt = el.alt;
        if (el.id) elemData.attrs.id = el.id;

        // SVG: capture raw markup + slot_id + viewBox for Phase 2 DrawingML conversion
        // tagName is 'svg' (lowercase) in HTML, 'SVG' in XML — accept both.
        if (el.tagName === 'svg' || el.tagName === 'SVG') {
            elemData.attrs.svg_markup = el.outerHTML;
            const slot = el.getAttribute('data-slot');
            if (slot) elemData.attrs['data-slot'] = slot;
            const vb = el.getAttribute('viewBox') || el.getAttribute('viewbox');
            if (vb) elemData.attrs.viewBox = vb;
        }

        // Real line count + widest line: Range.getClientRects on a block
        // element returns one rect per visual line (unlike
        // Element.getClientRects, which only returns the block box). This
        // is the authoritative signal for "single-line vs multi-line" and
        // "actual text width", letting Stage 2 _widen_text_position replace
        // the theoretical single-line-width guess from canvas.measureText
        // with the real rendered result.
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

        // Text natural-width measurement (via canvas.measureText)
        // Kept as a fallback: used when Range yields no data (pure icon, empty text).
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

    // --- Mark children absorbed by a parent's inline merge (span etc.) ---
    // These children's text has already been merged into the parent's
    // inlineRuns, so rendering them independently would duplicate the
    // parent's text. The classifier skips elements with absorbedByParent=true.
    for (let i = 0; i < elements.length; i++) {
        if (!elements[i].inlineRuns) continue;
        const parentEl = slideEl.querySelector(`[data-ss-idx="${i}"]`);
        if (!parentEl) continue;
        // flex/grid containers lay children out by spatial position — inline
        // children (span) are independent placement cells (e.g. a row of a
        // div-table), not runs in an inline flow. Merging them into the
        // parent's inlineRuns (and marking them absorbed) would crush a
        // whole table row into a single cramped text run. Skip absorption
        // so each cell renders at its own rect.
        const parentDisplay = getComputedStyle(parentEl).display;
        if (parentDisplay === 'flex' || parentDisplay === 'inline-flex'
            || parentDisplay === 'grid' || parentDisplay === 'inline-grid') continue;
        // Find all inline descendants (already merged into parent's inlineRuns)
        const inlines = parentEl.querySelectorAll(INLINE_SELECTOR);
        for (const inlineEl of inlines) {
            // Skip icons among inline descendants (they're handled separately as icon_text)
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

    // --- Second pass: compute the real stacking order via elementsFromPoint ---
    for (let i = 0; i < elements.length; i++) {
        const ssIdx = elements[i].ss_idx;
        const el = slideEl.querySelector(`[data-ss-idx="${ssIdx}"]`);
        if (!el) { elements[i].z_order = i; continue; }

        const rect = el.getBoundingClientRect();
        const cx = rect.left + rect.width / 2;
        const cy = rect.top + rect.height / 2;

        // elementsFromPoint returns every element at this coordinate, topmost first
        const stack = document.elementsFromPoint(cx, cy);

        // Count how many extracted elements sit above the current one.
        // Skip descendant elements — they're the current element's child
        // content, not independent occluding layers.
        let aboveCount = 0;
        for (const stackEl of stack) {
            if (stackEl === el) break;
            if (stackEl.hasAttribute && stackEl.hasAttribute('data-ss-idx')) {
                if (el.contains(stackEl)) continue; // skip descendants
                aboveCount++;
            }
        }
        // aboveCount=0 → topmost → highest z_order (rendered last, in front)
        // aboveCount=3 → 3 layers above → lower z_order (rendered earlier, behind)
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
