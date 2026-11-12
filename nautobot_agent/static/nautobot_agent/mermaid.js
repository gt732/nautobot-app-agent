(() => {
    "use strict";

    // Mermaid 12.1.0: https://registry.npmjs.org/mermaid/-/mermaid-12.1.0.tgz
    // sha512-wlVCp+8eTupfCeeFvoZNNiTuHrvag0P2jz/ILgb/f/6jkVokUefOcujefi8qUe/j2asHiSePncVsz/xzzA80LQ==
    const secureMermaidKeys = [
        "secure", "securityLevel", "startOnLoad", "maxTextSize", "suppressErrorRendering", "maxEdges",
        "callback", "dompurifyConfig", "theme", "themeVariables", "themeCSS", "fontFamily", "htmlLabels", "flowchart",
    ];
    const diagramResourceTokens = /(?:url|image|image-set|cross-fade|src)\(/i;

    function sourceIsSafe(source) {
        return !/[\\]|\/\*|@import\b/i.test(source)
            && !diagramResourceTokens.test(source)
            && !/\bimg\b/i.test(source);
    }

    function cssIsSafe(value) {
        if (/[\\]|\/\*|\*\/|@import\b/i.test(value)) return false;
        const functions = /([a-z-]+)\s*\(/gi;
        let match;
        while ((match = functions.exec(value))) {
            const name = match[1].toLowerCase();
            const start = functions.lastIndex;
            let depth = 1;
            let end = start;
            while (end < value.length && depth) {
                if (value[end] === "(") depth += 1;
                else if (value[end] === ")") depth -= 1;
                end += 1;
            }
            if (depth) return false;
            if (name === "url" || /(?:^|-)(?:image|image-set|cross-fade|src)$/.test(name)) {
                const argument = value.slice(start, end - 1).trim();
                if (name !== "url" || !/^(['"]?)#[\w:.-]+\1$/.test(argument)) return false;
            }
            functions.lastIndex = start;
        }
        return true;
    }

    function sanitizedDiagram(svg, purifier) {
        const fragment = purifier.sanitize(svg, {
            RETURN_DOM_FRAGMENT: true,
            USE_PROFILES: { svg: true, svgFilters: true },
            FORBID_TAGS: ["foreignObject", "script", "iframe", "image", "feImage", "a"],
        });
        const rootSvg = fragment.querySelector("svg");
        if (!rootSvg) throw new Error("No SVG returned");
        for (const node of fragment.querySelectorAll("*")) {
            if (node.localName === "style" && !cssIsSafe(node.textContent)) throw new Error("Unsafe SVG style");
            for (const attribute of node.attributes) {
                const name = attribute.name.toLowerCase();
                const value = attribute.value.trim();
                if (!cssIsSafe(value)) throw new Error("Unsafe SVG attribute");
                if ((name === "href" || name === "xlink:href") && !/^#[\w:.-]+$/.test(value)) {
                    throw new Error("Unsafe SVG reference");
                }
            }
        }
        return fragment;
    }

    /** Capture transcript and lazy-scroll callbacks; callers only need the returned enhance operation. */
    function createRenderer(transcript, mermaidUrl, nearBottom, scrollToBottom) {
        const purifier = DOMPurify(window);
        const states = new WeakMap();
        let mermaidPromise = null;
        let diagramQueue = Promise.resolve();
        let diagramId = 0;
        let lastTheme = document.documentElement.dataset.bsTheme === "dark" ? "dark" : "default";

        function loadMermaid() {
            mermaidPromise ||= import(mermaidUrl).then((module) => module.default);
            return mermaidPromise;
        }

        function currentTheme() {
            return document.documentElement.dataset.bsTheme === "dark" ? "dark" : "default";
        }

        function diagramCurrent(wrapper, state, generation, theme) {
            return wrapper.isConnected && state.destination.isConnected
                && state.generation === generation && currentTheme() === theme;
        }

        function scheduleDiagram(wrapper) {
            const state = states.get(wrapper);
            if (!state) return;
            const generation = ++state.generation;
            const theme = currentTheme();
            diagramQueue = diagramQueue.catch(() => {}).then(async () => {
                function ensureCurrent() {
                    if (diagramCurrent(wrapper, state, generation, theme)) return true;
                    if (wrapper.isConnected && currentTheme() !== theme) scheduleDiagram(wrapper);
                    return false;
                }
                if (!ensureCurrent()) return;
                const layout = document.createElement("div");
                layout.style.visibility = "hidden";
                const id = `agent-mermaid-${++diagramId}`;
                try {
                    if (!sourceIsSafe(state.source)) throw new Error("Unsupported source");
                    if (document.fonts?.ready) await document.fonts.ready;
                    if (!ensureCurrent()) return;
                    const mermaid = await loadMermaid();
                    if (!ensureCurrent()) return;
                    mermaid.initialize({
                        startOnLoad: false,
                        securityLevel: "strict",
                        suppressErrorRendering: true,
                        htmlLabels: false,
                        flowchart: { htmlLabels: false },
                        fontFamily: getComputedStyle(document.body).fontFamily,
                        theme,
                        secure: [...new Set([...(mermaid.mermaidAPI.getConfig().secure || []), ...secureMermaidKeys])],
                    });
                    document.body.append(layout);
                    const result = await mermaid.render(id, state.source, layout);
                    const fragment = sanitizedDiagram(result.svg, purifier);
                    if (!ensureCurrent()) return;
                    const stick = nearBottom();
                    state.destination.hidden = false;
                    state.destination.replaceChildren(fragment);
                    const renderedSvg = state.destination.querySelector("svg");
                    if (!renderedSvg.hasAttribute("aria-labelledby") && !renderedSvg.hasAttribute("aria-label")) {
                        renderedSvg.setAttribute("aria-label", renderedSvg.querySelector("title")?.textContent || "Mermaid diagram");
                    }
                    if (!renderedSvg.hasAttribute("role")) renderedSvg.setAttribute("role", "img");
                    state.failure.hidden = true;
                    if (stick) scrollToBottom();
                } catch {
                    if (ensureCurrent()) {
                        state.destination.hidden = true;
                        state.failure.hidden = false;
                        state.sourceDetails.open = true;
                    }
                } finally {
                    layout.remove();
                }
            });
        }

        new MutationObserver(() => {
            const theme = currentTheme();
            if (theme === lastTheme) return;
            lastTheme = theme;
            transcript.querySelectorAll(".agent-mermaid").forEach(scheduleDiagram);
        }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-bs-theme"] });

        function enhance(block, source) {
            const wrapper = document.createElement("div");
            wrapper.className = "agent-mermaid";
            const destination = document.createElement("div");
            destination.className = "agent-mermaid-svg";
            destination.hidden = true;
            const sourceDetails = document.createElement("details");
            sourceDetails.className = "agent-mermaid-source";
            const summary = document.createElement("summary");
            summary.textContent = "Mermaid source";
            block.replaceWith(wrapper);
            sourceDetails.append(summary, block);
            const failure = document.createElement("div");
            failure.className = "agent-mermaid-failure";
            failure.textContent = "Diagram could not be rendered. Mermaid source is shown below.";
            failure.hidden = true;
            const state = { source, generation: 0, destination, sourceDetails, failure };
            states.set(wrapper, state);
            wrapper.append(destination, failure, sourceDetails);
            scheduleDiagram(wrapper);
        }

        return { enhance };
    }

    window.NautobotAgentMermaid = { createRenderer };
})();
