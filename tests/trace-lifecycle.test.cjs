const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "../nautobot_agent/static/nautobot_agent/trace.js"), "utf8");
const clipboardScript = fs.readFileSync(path.join(__dirname, "../nautobot_agent/static/nautobot_agent/clipboard.js"), "utf8");
const completions = ["htmx:afterSwap", "htmx:responseError", "htmx:sendError", "htmx:timeout"];

function fixture({ withDetail = true } = {}) {
    const listeners = new Map();
    const element = () => ({
        attributes: {},
        setAttribute(name, value) { this.attributes[name] = value; },
    });
    const detail = { ...element(), dataset: {} };
    const refresh = { href: "https://example.test/trace?span=old" };
    const classes = new Set();
    const notice = {
        hidden: true,
        textContent: "",
        classList: { toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name) },
    };
    const root = {
        ...element(),
        contains: (target) => target === root || target === detail,
        querySelector: (selector) => selector === "#trace-detail" ? (withDetail ? detail : null)
            : selector === ".trace-request-state" ? notice
                : selector === "[data-trace-refresh]" ? refresh : null,
    };
    const navigation = [];
    const context = vm.createContext({
        document: {
            getElementById: () => root,
            addEventListener: (name, callback) => listeners.set(name, callback),
        },
        window: { addEventListener() {}, location: { href: "https://example.test/trace?span=old" } },
        history: { state: null, replaceState: (...args) => navigation.push(args) },
        URL,
    });
    vm.runInContext(clipboardScript, context);
    vm.runInContext(script, context);
    const emit = (name, detail) => listeners.get(name)({ detail });
    return {
        root, detail, refresh, notice, classes, navigation,
        start: (xhr) => emit("htmx:beforeRequest", { elt: root, xhr }),
        complete: (name, xhr, elt = root) => emit(name, { xhr, elt }),
    };
}

test("completions and timeouts release indicators safely", () => {
    for (const name of completions) {
        const page = fixture();
        const xhr = {};
        page.start(xhr);
        assert.equal(page.root.attributes["aria-busy"], "true");
        assert.equal(page.detail.attributes["aria-busy"], "true");
        page.complete(name, xhr);
        assert.equal(page.root.attributes["aria-busy"], "false", name);
        assert.equal(page.detail.attributes["aria-busy"], "false", name);
        const failed = name !== "htmx:afterSwap";
        assert.equal(page.notice.hidden, !failed, name);
        assert.equal(page.classes.has("alert-danger"), failed, name);
        if (failed) assert.deepEqual(page.navigation, [], name);
    }
    const page = fixture({ withDetail: false });
    const xhr = {};
    page.start(xhr);
    page.complete("htmx:timeout", xhr);
    assert.equal(page.root.attributes["aria-busy"], "false");
});

test("successful span selection updates the encoded address and Refresh link", () => {
    const page = fixture();
    const xhr = {};
    page.start(xhr);
    page.detail.dataset.selectedRun = "span/with +?&=#";
    page.complete("htmx:afterSwap", xhr);

    const expected = "https://example.test/trace?span=span%2Fwith%20%2B%3F%26%3D%23";
    assert.equal(page.navigation.length, 1);
    assert.equal(page.navigation[0][2].href, expected);
    assert.equal(page.refresh.href, expected);
});

test("unrelated completions cannot clear trace loading state", () => {
    for (const name of completions) {
        const page = fixture();
        const xhr = {};
        page.start(xhr);
        const loadingNotice = page.notice.textContent;
        page.complete(name, {}, {});
        assert.equal(page.root.attributes["aria-busy"], "true", name);
        assert.equal(page.detail.attributes["aria-busy"], "true", name);
        assert.equal(page.notice.textContent, loadingNotice, name);
        assert.equal(page.notice.hidden, false, name);
        assert.deepEqual(page.navigation, [], name);
        page.complete("htmx:timeout", xhr);
        assert.equal(page.root.attributes["aria-busy"], "false", name);
        assert.equal(page.detail.attributes["aria-busy"], "false", name);
    }
});

test("trace copy falls back without the Clipboard API or after a rejected write", async () => {
    for (const mode of ["absent", "rejected", "success", "fallback-failed"]) {
        const listeners = new Map();
        const writes = [];
        let fallbackCalls = 0;
        let removed = false;
        let restoredFocus = false;
        let textarea;
        const status = { textContent: "", setAttribute() {} };
        const section = {
            querySelector: (selector) => selector === ".trace-copy-status" ? status
                : selector === ".trace-payload-json" ? { textContent: "visible bounded preview" } : null,
        };
        const button = { closest: () => section, focus() { restoredFocus = true; } };
        const navigator = mode === "absent" || mode === "fallback-failed" ? {} : {
            clipboard: {
                async writeText(text) {
                    if (mode === "rejected") throw new Error("Permission denied");
                    writes.push(text);
                },
            },
        };
        const context = vm.createContext({
            navigator,
            document: {
                activeElement: button,
                getElementById: () => null,
                addEventListener: (name, callback) => listeners.set(name, callback),
                createElement: () => ({ style: {}, select() {}, remove() { removed = true; } }),
                body: { append(element) { textarea = element; } },
                execCommand(command) {
                    assert.equal(command, "copy");
                    assert.equal(textarea.value, "visible bounded preview");
                    fallbackCalls += 1;
                    return mode !== "fallback-failed";
                },
            },
            window: { addEventListener() {} },
        });
        vm.runInContext(clipboardScript, context);
        vm.runInContext(script, context);
        listeners.get("click")({ target: { closest: () => button } });
        await new Promise((resolve) => setImmediate(resolve));
        assert.equal(status.textContent, mode === "fallback-failed"
            ? "Copy failed. Select the displayed text and copy it manually." : "Copied.", mode);
        assert.equal(fallbackCalls, mode === "success" ? 0 : 1, mode);
        assert.deepEqual(writes, mode === "success" ? ["visible bounded preview"] : [], mode);
        assert.equal(removed, mode !== "success", mode);
        assert.equal(restoredFocus, true, mode);
    }
});

test("delegated copy uses the replacement preview without fetching complete JSON", async () => {
    const page = fixture();
    const copied = [];
    let preview = "initial preview";
    let fetches = 0;
    const status = { textContent: "", setAttribute() {} };
    const section = {
        querySelector(selector) {
            if (selector === ".trace-copy-status") return status;
            if (selector === ".trace-payload-json") return { textContent: preview };
            return null;
        },
    };
    const button = {
        closest: (selector) => selector === "[data-copy-payload]" ? button
            : selector === ".trace-payload" ? section : null,
        focus() {},
    };
    const context = vm.createContext({
        navigator: { clipboard: { async writeText(text) { copied.push(text); } } },
        document: {
            activeElement: button,
            getElementById: () => page.root,
            addEventListener: (name, callback) => {
                if (name === "click") page.click = callback;
            },
        },
        window: { addEventListener() {} },
        fetch() { fetches += 1; },
    });
    vm.runInContext(clipboardScript, context);
    vm.runInContext(script, context);
    preview = "replacement bounded preview";
    page.click({ target: { closest: (selector) => selector === "[data-copy-payload]" ? button : null } });
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(copied, ["replacement bounded preview"]);
    page.click({ target: { closest: () => null } }); // Full JSON anchors remain ordinary browser links.
    assert.equal(fetches, 0);
});
