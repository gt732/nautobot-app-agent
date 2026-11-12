const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { deferred, flush: tick } = require("./run-fixtures.cjs");

const script = fs.readFileSync(path.join(__dirname, "../nautobot_agent/static/nautobot_agent/mermaid.js"), "utf8");

async function fixture({ source = "graph TD; A-->B", nodes = [] } = {}) {
    const fonts = deferred();
    const rendering = deferred();
    const calls = [];
    const configurations = [];
    const sanitizerOptions = [];
    const published = [];
    let observer;
    // A real dynamic import keeps the production loader intact; only the renderer is controlled.
    const url = `data:text/javascript,export default {}#${Math.random()}`;
    const mermaid = (await import(url)).default;
    Object.assign(mermaid, {
        mermaidAPI: { getConfig: () => ({ secure: [] }) },
        initialize(config) { configurations.push(config); this.theme = config.theme; },
        async render() {
            calls.push(this.theme);
            if (calls.length === 1) await rendering.promise;
            return { svg: this.theme };
        },
    });
    function element() {
        return {
            isConnected: true, hidden: false, style: {}, children: [],
            append(...nodes) { this.children.push(...nodes); },
            replaceWith(node) { this.replacement = node; },
            replaceChildren(fragment) { published.push(fragment.theme); this.svg = fragment.svg; },
            querySelector() { return this.svg; },
            remove() {},
        };
    }
    const document = {
        documentElement: { dataset: {} }, body: element(), fonts: { ready: fonts.promise },
        createElement: element,
    };
    const window = {};
    vm.runInNewContext(script, {
        window, document, getComputedStyle: () => ({ fontFamily: "sans-serif" }),
        MutationObserver: class {
            constructor(callback) { observer = callback; }
            observe() {}
        },
        DOMPurify: () => ({ sanitize: (theme, options) => {
            sanitizerOptions.push(options);
            const svg = { hasAttribute: () => true };
            return { theme, svg, querySelector: () => svg, querySelectorAll: () => nodes };
        } }),
    }, { importModuleDynamically: vm.constants.USE_MAIN_CONTEXT_DEFAULT_LOADER });
    const block = element();
    const transcript = { querySelectorAll: () => [block.replacement] };
    window.NautobotAgentMermaid.createRenderer(transcript, url, () => false, () => {}).enhance(block, source);
    await tick();
    return {
        fonts, rendering, calls, published, configurations, sanitizerOptions,
        destination: block.replacement.children[0], failure: block.replacement.children[1],
        sourceDetails: block.replacement.children[2],
        changeTheme(notify = false) {
            document.documentElement.dataset.bsTheme = "dark";
            if (notify) observer();
        },
    };
}

test("diagram rendering retains strict security and the SVG sanitizer boundary", async () => {
    const page = await fixture();
    page.fonts.resolve();
    page.rendering.resolve();
    for (let i = 0; i < 20 && !page.published.length; i++) await tick();
    assert.deepEqual(page.published, ["default"]);
    const config = page.configurations[0];
    assert.equal(config.securityLevel, "strict");
    assert.equal(config.htmlLabels, false);
    assert.equal(config.flowchart.htmlLabels, false);
    for (const key of [
        "secure", "securityLevel", "startOnLoad", "maxTextSize", "suppressErrorRendering", "maxEdges",
        "callback", "dompurifyConfig", "theme", "themeVariables", "themeCSS", "fontFamily", "htmlLabels", "flowchart",
    ]) assert.ok(config.secure.includes(key), `Diagram directives must not override ${key}`);
    const options = page.sanitizerOptions[0];
    assert.equal(options.USE_PROFILES.svg, true);
    for (const tag of ["foreignObject", "script", "iframe", "image", "feImage", "a"])
        assert.ok(options.FORBID_TAGS.includes(tag), `SVG must forbid ${tag}`);
});

for (const [label, value, safe] of [
    ["local URL", "url(#a)", true],
    ["quoted local URL", "url('#a')", true],
    ["ordinary styling", "rgb(10, 20, 30)", true],
    ["external URL", "url(http://outside.invalid)", false],
    ["uppercase quoted URL", "URL( 'x' )", false],
    ["import", '@import "https://outside.invalid";', false],
    ["escaped URL", String.raw`\75rl(https://outside.invalid)`, false],
    ["image set", "-webkit-image-set('https://outside.invalid' 1x)", false],
    ["unclosed function", "url(#a", false],
    ["nested external URL", "anything(url(https://outside.invalid))", false],
]) {
    for (const location of ["style", "attribute"]) {
        test(`diagram ${location}: ${label} ${safe ? "renders" : "shows failure"}`, async () => {
            const node = location === "style"
                ? { localName: "style", textContent: value, attributes: [] }
                : { localName: "path", attributes: [{ name: "fill", value }] };
            const page = await fixture({ nodes: [node] });
            page.rendering.resolve();
            page.fonts.resolve();
            for (let i = 0; i < 20 && !page.published.length && page.failure.hidden; i++) await tick();
            assert.equal(page.destination.hidden, !safe);
            assert.equal(page.failure.hidden, safe);
            assert.deepEqual(page.published, safe ? ["default"] : []);
            if (!safe) assert.equal(page.sourceDetails.open, true);
        });
    }
}

for (const name of ["href", "xlink:href"]) {
    for (const value of ["#local", "https://outside.invalid/image", "//outside.invalid/image", "data:image/png,test"]) {
        test(`diagram ${name} accepts only local references: ${value}`, async () => {
            const safe = value === "#local";
            const page = await fixture({ nodes: [{ localName: "use", attributes: [{ name, value }] }] });
            page.rendering.resolve();
            page.fonts.resolve();
            for (let i = 0; i < 20 && !page.published.length && page.failure.hidden; i++) await tick();
            assert.equal(page.destination.hidden, !safe);
            assert.equal(page.failure.hidden, safe);
            assert.deepEqual(page.published, safe ? ["default"] : []);
        });
    }
}

for (const source of [
    "graph TD; A[img]",
    "graph TD; A[url(https://outside.invalid)]",
    "graph TD; A[image(https://outside.invalid)]",
    "graph TD; A[@import 'https://outside.invalid']",
    String.raw`graph TD; A[\75rl(x)]`,
    "graph TD; A[/* comment */]",
]) {
    test(`unsafe diagram source never reaches Mermaid: ${source}`, async () => {
        const page = await fixture({ source });
        assert.deepEqual(page.calls, []);
        assert.deepEqual(page.published, []);
        assert.equal(page.destination.hidden, true);
        assert.equal(page.failure.hidden, false);
        assert.equal(page.sourceDetails.open, true);
    });
}

for (const checkpoint of ["fonts", "render", "render failure", "new generation"]) {
    test(`theme change during ${checkpoint} publishes only the replacement diagram`, async () => {
        const page = await fixture();
        if (checkpoint !== "fonts") {
            page.fonts.resolve();
            for (let i = 0; i < 20 && page.calls.length === 0; i++) await tick();
            assert.deepEqual(page.calls, ["default"]);
        }
        page.changeTheme(checkpoint === "new generation");
        assert.deepEqual(page.published, []);
        if (checkpoint === "render failure") page.rendering.reject(new Error("Old render failed"));
        else page.rendering.resolve();
        page.fonts.resolve();
        for (let i = 0; i < 20 && page.published.length === 0; i++) await tick();
        assert.deepEqual(page.published, ["dark"]);
        assert.deepEqual(page.calls, checkpoint === "fonts" ? ["dark"] : ["default", "dark"]);
        assert.equal(page.destination.hidden, false);
        assert.equal(page.failure.hidden, true);
    });
}
