const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { runId, conversationId, deferred, flush, response, runSummary, runPage, snapshot } = require("./run-fixtures.cjs");
const script = fs.readFileSync(path.join(__dirname, "../nautobot_agent/static/nautobot_agent/agent.js"), "utf8");
const clipboardScript = fs.readFileSync(path.join(__dirname, "../nautobot_agent/static/nautobot_agent/clipboard.js"), "utf8");
const runsScript = fs.readFileSync(path.join(__dirname, "../nautobot_agent/static/nautobot_agent/agent-runs.js"), "utf8");

class Element {
    constructor() {
        this.children = [];
        this.dataset = {};
        this.style = {};
        this.attributes = new Map();
        this.listeners = new Map();
        this.className = "";
        this.textContent = "";
        this._innerHTML = "";
        this.value = "";
        this.hidden = false;
        this.disabled = false;
        this.scrollHeight = 0;
        this.scrollTop = 0;
        this.clientHeight = 100;
        this.isConnected = true;
        this.classList = {
            add: (...names) => {
                const classes = this.className.split(/\s+/).filter(Boolean);
                this.className = [...new Set([...classes, ...names])].join(" ");
            },
            remove: (...names) => {
                this.className = this.className.split(/\s+/)
                    .filter((name) => !names.includes(name)).join(" ");
            },
            contains: (name) => this.className.split(/\s+/).includes(name),
            toggle: (name, force) => {
                const on = force ?? !this.classList.contains(name);
                this.classList[on ? "add" : "remove"](name);
                return on;
            },
        };
    }
    set innerHTML(value) {
        this._innerHTML = value;
        if (value.includes("agent-message-body")) {
            const body = new Element();
            body.className = "agent-message-body";
            const parts = new Element();
            parts.className = "agent-parts";
            const actions = new Element();
            actions.className = "agent-message-actions";
            body.append(parts, actions);
            this.append(body);
        } else if (value.includes("<i")) this.append(new Element());
    }
    get innerHTML() { return this._innerHTML; }
    get firstChild() { return this.children[0] ?? null; }
    get lastElementChild() { return this.children.at(-1) ?? null; }
    append(...nodes) {
        for (const node of nodes) {
            node.remove();
            node.parentElement = this;
            node.isConnected = this.isConnected;
            this.children.push(node);
        }
    }
    replaceChildren(...nodes) {
        for (const child of [...this.children]) child.remove();
        this.append(...nodes);
    }
    prepend(node) {
        node.remove();
        node.parentElement = this;
        this.children.unshift(node);
    }
    insertBefore(node, reference) {
        const index = this.children.indexOf(reference);
        if (index < 0) return this.append(node);
        node.remove();
        node.parentElement = this;
        this.children.splice(index, 0, node);
    }
    before(node) {
        if (this.parentElement) this.parentElement.insertBefore(node, this);
    }
    after(...nodes) {
        if (!this.parentElement) return;
        let index = this.parentElement.children.indexOf(this) + 1;
        for (const node of nodes) {
            node.remove();
            node.parentElement = this.parentElement;
            this.parentElement.children.splice(index++, 0, node);
        }
    }
    remove() {
        if (this.parentElement) {
            this.parentElement.children.splice(this.parentElement.children.indexOf(this), 1);
        }
        this.parentElement = null;
        this.isConnected = false;
    }
    setAttribute(name, value) { this.attributes.set(name, String(value)); }
    getAttribute(name) { return this.attributes.get(name) ?? null; }
    removeAttribute(name) { this.attributes.delete(name); }
    addEventListener(name, cb) { this.listeners.set(name, cb); }
    focus() {}
    querySelector(selector) {
        if (selector === "i") {
            return this.children.find((node) => node.tagName === "I") ?? new Element();
        }
        if (selector.startsWith(".")) {
            return this.find((node) => node.classList.contains(selector.slice(1)));
        }
        return null;
    }
    querySelectorAll(selector) {
        if (selector === "pre > code") return [];
        return this.findAll((node) => selector.startsWith(".") && node.classList.contains(selector.slice(1)));
    }
    find(predicate) {
        for (const child of this.children) {
            if (predicate(child)) return child;
            const found = child.find(predicate);
            if (found) return found;
        }
        return null;
    }
    findAll(predicate) {
        return this.children.flatMap((child) => [
            ...(predicate(child) ? [child] : []),
            ...child.findAll(predicate),
        ]);
    }
}


function fixture({
    conversation = "new", history = [], active = null, latest = active, pending = [], urlConversation = conversation,
    modelName = "", todos = [], contextUsage = { used: null, limit: null, percent: null },
    initialScroll = {}, llmConfigured = true, defaultModel = "", protocol = "http:", cookiePath = "/plugins/nautobot-agent/",
    browserNow = () => 1_700_000_000_123,
} = {}) {
    const make = () => new Element();
    const root = make();
    root.dataset = {
        departureCookieName: "nautobot_agent_chat_departure",
        departureUserId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        departureCookiePath: cookiePath,
        serverNowMs: "1700000000123",
        sendUrl: "/runs",
        stateUrl: "/state/",
        mermaidUrl: "/mermaid",
        llmConfigured: String(llmConfigured),
        defaultModel,
    };
    const log = make();
    const input = make();
    const form = make();
    const sendButton = make();
    const newButton = make();
    input.scrollHeight = 20;
    sendButton.querySelector = () => new Element();
    const elements = Object.fromEntries([
        "#agent-scroller", "#agent-empty", "#agent-todos-panel", "#agent-todos-list",
        "#agent-context", "#agent-context-status", "#agent-context-display",
        "#agent-context-meter", "#agent-context-fill", "#agent-context-details",
        "#agent-run-status", "#agent-run-status-details",
        "#agent-run-status-error-code", "#agent-run-status-execution-error",
        "#agent-run-status-spinner", "#agent-model-name",
    ].map((key) => [key, make()]));
    Object.assign(elements["#agent-scroller"], initialScroll);
    elements["#agent-model-name"].hidden = true;
    elements["#agent-run-status-spinner"].hidden = true;
    elements["#agent-stop"] = make();
    elements["#agent-run-status-text"] = elements["#agent-run-status"];
    elements["#agent-run-status-details"].hidden = true;
    Object.assign(elements, { "#agent-log": log, "#agent-input": input, "#agent-form": form, "#agent-send": sendButton, "#agent-new-chat": newButton });
    root.querySelector = (selector) => selector === "[name=csrfmiddlewaretoken]"
        ? { value: "csrf" }
        : elements[selector];
    const payloads = new Map([
        ["#agent-state", {
            conversation_id: conversation, history, todos, context_usage: contextUsage,
            model_name: modelName, active_run: active, latest_run: latest,
        }],
    ]);
    const cookieWrites = [];
    let cookie = "";
    const document = {
        readyState: "complete",
        hidden: false,
        documentElement: { dataset: { bsTheme: "light" } },
        get cookie() { return cookie; },
        set cookie(value) { cookie = value; cookieWrites.push(value); },
        querySelector: (selector) => selector === "#agent-chat"
            ? root
            : { textContent: JSON.stringify(payloads.get(selector)) },
        createElement: () => make(),
    };
    const requests = [], charts = [];
    let requestIdentityCount = 0;
    const fetch = (url, options = {}) => {
        const gate = deferred();
        requests.push({ url, options, gate });
        return gate.promise;
    };
    const sessionStorage = {
        data: new Map([["nautobot-agent-pending-requests", JSON.stringify(pending)]]),
        setItem(key, value) { this.data.set(key, value); },
        getItem(key) { return this.data.get(key) ?? null; },
        removeItem(key) { this.data.delete(key); },
    };
    let currentUrl = urlConversation ? `?conversation=${encodeURIComponent(urlConversation)}` : "";
    const windowListeners = new Map();
    let reloads = 0;
    const chartApi = {
        init: (container) => {
            const chart = {
                container,
                disposed: false,
                options: [],
                setOption(value) { this.options.push(value); },
                dispose() { this.disposed = true; },
                resize() {},
            };
            charts.push(chart);
            return chart;
        },
    };
    const window = {
        location: { protocol, get search() { return currentUrl; }, reload() { reloads += 1; } },
        addEventListener: (name, callback) => windowListeners.set(name, callback),
        NautobotAgentMermaid: { createRenderer: () => ({ enhance() {} }) },
        echarts: chartApi,
        nb: { echarts: { getEchartsOptionsThemeOverrides: () => ({}) } },
    };
    const crypto = {
        randomUUID: () => [
            runId,
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        ][requestIdentityCount++] || "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    };
    const browserHistory = {
        replaceState(_state, _title, url) { currentUrl = url; },
    };
    const animationFrames = new Map();
    const sanitizerHooks = new Map();
    let nextAnimationFrameId = 1;
    const context = vm.createContext({
        document,
        window,
        fetch,
        AbortController,
        DOMException,
        crypto,
        history: browserHistory,
        Date: { now: browserNow },
        URLSearchParams,
        sessionStorage,
        requestAnimationFrame: (callback) => {
            const id = nextAnimationFrameId++;
            animationFrames.set(id, callback);
            return id;
        },
        cancelAnimationFrame: (id) => animationFrames.delete(id),
        marked: { setOptions() {}, parse: (source) => source },
        DOMPurify: {
            addHook(name, callback) { sanitizerHooks.set(name, callback); },
            sanitize: (source) => source,
        },
        hljs: { getLanguage: () => true, highlightElement() {} },
        setTimeout: (callback) => { queueMicrotask(callback); return 1; },
        clearTimeout() {},
        queueMicrotask,
        console,
    });
    vm.runInContext(clipboardScript, context);
    vm.runInContext(runsScript, context);
    vm.runInContext(script, context);
    return {
        document,
        windowListeners,
        root,
        log,
        input,
        form,
        sendButton,
        newButton,
        requests,
        cookieWrites,
        charts,
        sessionStorage,
        sanitizerHooks,
        status: elements["#agent-run-status"],
        stopButton: elements["#agent-stop"],
        get url() { return currentUrl; },
        get reloads() { return reloads; },
        flushAnimationFrames() {
            const queued = [...animationFrames.entries()];
            for (const [id] of queued) animationFrames.delete(id);
            for (const [, callback] of queued) callback();
        },
        submit() { form.listeners.get("submit")({ preventDefault() {} }); },
        newChat() { newButton.listeners.get("click")(); },
        pagehide(persisted = false) { windowListeners.get("pagehide")({ persisted }); },
        pageshow() { windowListeners.get("pageshow")({ persisted: true }); },
        popstate(url) {
            currentUrl = url;
            windowListeners.get("popstate")();
        },
    };
}

test("markdown classes cannot impersonate UI but retain code-language markers", () => {
    const page = fixture();
    const clean = page.sanitizerHooks.get("afterSanitizeAttributes");
    for (const [tag, classes, expected] of [
        ["DIV", "alert position-fixed", null],
        ["A", "btn btn-danger", null],
        ["PRE", "language-python position-fixed", null],
        ["CODE", "alert position-fixed", null],
        ["CODE", "position-fixed language-python alert", "language-python"],
        ["CODE", "language-mermaid", "language-mermaid"],
        ["CODE", "language-c++", "language-c++"],
        ["CODE", "language-python=alert", null],
        ["CODE", "", null],
    ]) {
        const node = new Element();
        node.tagName = tag;
        node.setAttribute("class", classes);
        clean(node);
        assert.equal(node.getAttribute("class"), expected, `${tag}: ${classes}`);
        if (tag === "A") {
            assert.equal(node.getAttribute("target"), "_blank");
            assert.equal(node.getAttribute("rel"), "noopener noreferrer");
        }
    }
});

test("default model is visible before the first message without exposing context usage", () => {
    const page = fixture({ defaultModel: "deepseek:deepseek-flash" });
    const badge = page.root.querySelector("#agent-model-name");
    assert.equal(badge.textContent, "deepseek:deepseek-flash");
    assert.equal(badge.hidden, false);
    assert.equal(page.root.querySelector("#agent-context").hidden, true);
    assert.equal(page.requests.length, 0);
});

test("captured conversation model takes precedence over the configured default", () => {
    const page = fixture({
        defaultModel: "deepseek:deepseek-flash",
        modelName: "openai:gpt-5.4",
    });
    assert.equal(page.root.querySelector("#agent-model-name").textContent, "openai:gpt-5.4");
});
async function submit(page) {
    page.input.value = "question";
    page.submit();
    await flush();
    page.requests[0].gate.resolve(response(runSummary(), 202));
    await flush();
}
function events(page) { return page.requests.at(-1); }

test("fresh chat stays clear of unrelated submission recovery after reload", async (t) => {
    for (const record of [
        { request_id: runId, scope: "new", detached: true, created_at: Date.now() },
        { request_id: runId, scope: conversationId, detached: false, created_at: Date.now() },
    ]) {
        for (const outcome of ["missing", "unavailable", "found"]) {
            await t.test(`${record.scope} ${outcome}`, async () => {
                const page = fixture({ pending: [record] });
                page.requests[0].gate.resolve(response(
                    outcome === "found" ? runSummary() : { run_id: null },
                    outcome === "unavailable" ? 503 : 200,
                ));
                await flush();
                assert.equal(page.url, "?conversation=new");
                assert.equal(page.status.hidden, true);
                assert.equal(page.log.children.length, 0);
                const stored = JSON.parse(page.sessionStorage.getItem("nautobot-agent-pending-requests") || "[]");
                assert.deepEqual(stored, outcome === "found" ? [] : [record]);
                page.input.value = "a separate question";
                page.input.listeners.get("input")();
                assert.equal(page.sendButton.disabled, false);
                page.submit();
                assert.equal(page.requests[1].options.method, "POST");
                assert.equal(JSON.parse(page.requests[1].options.body).conversation_id, null);
            });
        }
    }
});

test("unconfigured chat blocks submissions and keeps the welcome hidden through New chat", () => {
    const page = fixture({
        llmConfigured: false,
        conversation: conversationId,
        history: [{ role: "assistant", content: "saved answer" }],
        latest: runSummary("failed", { error_code: "llm_not_configured" }),
    });
    assert.ok(page.log.find((node) => node.dataset.raw === "saved answer"));
    assert.equal(page.status.hidden, true);
    assert.equal(page.input.disabled, true);
    assert.equal(page.root.querySelector("#agent-empty").hidden, true);
    page.input.value = "cannot send yet";
    page.input.listeners.get("input")();
    page.submit();
    assert.equal(page.sendButton.disabled, true);
    assert.equal(page.requests.length, 0);
    page.newChat();
    assert.equal(page.root.querySelector("#agent-empty").hidden, true);
    assert.equal(page.input.disabled, true);
    assert.equal(page.sendButton.disabled, true);
});

test("submit shows activity in run status after admission", async () => {
    const page = fixture();
    page.input.value = "  question  ";
    page.submit();
    assert.equal(page.input.value, "");
    assert.equal(page.log.find((node) => node.textContent === "question") !== null, true);
    assert.equal(page.root.querySelector("#agent-run-status-spinner").hidden, true);
    assert.equal(page.sendButton.disabled, true);
    page.requests[0].gate.resolve(response(runSummary(), 202));
    await flush();
    assert.equal(page.url, `?conversation=${conversationId}`);
    assert.equal(page.root.querySelector("#agent-run-status-spinner").hidden, false);
    assert.equal(page.stopButton.hidden, false);
    assert.match(page.status.textContent, /Running/);
});
test("queued runs show activity until they reach a terminal status", async () => {
    const page = fixture();
    page.input.value = "question";
    page.submit();
    page.requests[0].gate.resolve(response(runSummary("queued"), 202));
    await flush();
    assert.match(page.status.textContent, /Queued/);
    assert.equal(page.root.querySelector("#agent-run-status-spinner").hidden, false);
    events(page).gate.resolve(response(runPage("completed")));
    await flush();
    assert.equal(page.status.textContent, "Completed.");
    assert.equal(page.root.querySelector("#agent-run-status-spinner").hidden, true);
});

test("restored unavailable integration labels render literally and a healthy run clears the notice", async () => {
    const serverName = "<img src=x onerror=alert(1)>";
    const page = fixture({
        conversation: conversationId,
        latest: runSummary("completed", {
            mcp_unavailable_servers: [{ id: "server-id", name: serverName }],
        }),
    });
    assert.match(page.status.textContent, /Completed\./);
    assert.match(page.status.textContent, /Built-in and other available tools remain usable/);
    assert.ok(page.status.textContent.includes(serverName));
    assert.equal(page.status.innerHTML.includes("<img"), false);
    page.input.value = "next request";
    page.submit();
    page.requests[0].gate.resolve(response(runSummary("running", { mcp_unavailable_servers: [] }), 202));
    await flush();
    assert.match(page.status.textContent, /Running/);
    assert.equal(page.status.textContent.includes(serverName), false);
    events(page).gate.resolve(response(runPage("completed", { mcp_unavailable_servers: [] })));
    await flush();
    assert.equal(page.status.textContent, "Completed.");
});

test("spinner follows the run status while tools execute", async () => {
    const page = fixture();
    await submit(page);
    assert.equal(page.root.querySelector("#agent-run-status-spinner").hidden, false);
    events(page).gate.resolve(response(runPage("running", {
        events: [{ id: "1-0", event: "token", data: { text: "" } }],
        next_cursor: "1-0",
    })));
    await flush();
    assert.equal(page.root.querySelector("#agent-run-status-spinner").hidden, false);
    events(page).gate.resolve(response(runPage("running", {
        events: [{ id: "1-1", event: "tool_call", data: { id: "tool", name: "query_records", args: {}, status: "running" } }],
        next_cursor: "1-1",
    })));
    await flush();
    assert.ok(page.log.find(node => node.classList.contains("agent-tool")));
    assert.equal(page.root.querySelector("#agent-run-status-spinner").hidden, false);
    assert.equal(page.sendButton.disabled, true);
    assert.equal(page.stopButton.hidden, false);
});
test("uncertain submission preserves the optimistic message", async () => {
    const page = fixture({ active: runSummary("completed"), latest: runSummary("completed") });
    page.input.value = "question";
    page.submit();
    page.requests[0].gate.resolve(response({ error: "Unavailable." }, 503));
    await flush();
    assert.equal(page.log.children.length, 1);
    assert.equal(page.log.children[0].classList.contains("agent-message-user"), true);
});


test("Stop posts cancellation without aborting execution polling and ignores a late running response", async () => {
    const page = fixture();
    await submit(page);
    assert.equal(page.root.querySelector("#agent-stop").hidden, false);
    const runningPoll = page.requests[1];
    page.root.querySelector("#agent-stop").listeners.get("click")();
    await flush();
    const cancellation = page.requests[2];
    assert.equal(cancellation.url, `/runs/${runId}/cancel/`);
    assert.equal(cancellation.options.method, "POST");
    assert.equal(cancellation.options.headers["X-CSRFToken"], "csrf");
    cancellation.gate.resolve(response(runSummary("cancelled"), 200));
    await flush();
    assert.match(page.root.querySelector("#agent-run-status").textContent, /safely cancelled/i);
    runningPoll.gate.resolve(response(runPage("running")));
    await flush(); await flush();
    assert.match(page.root.querySelector("#agent-run-status").textContent, /safely cancelled/i);
});

test("failed cancellation notice survives same-run status refresh and clears on New chat", async () => {
    const page = fixture();
    await submit(page);
    const runningPoll = page.requests[1];
    page.stopButton.listeners.get("click")();
    await flush();
    page.requests[2].gate.resolve(response({}, 500));
    await flush();

    const failureNotice = "Cancellation request could not be confirmed";
    assert.match(page.status.textContent, new RegExp(failureNotice));
    runningPoll.gate.resolve(response(runPage("running")));
    await flush(); await flush();
    assert.match(page.status.textContent, new RegExp(failureNotice));
    assert.match(page.status.textContent, /Running/);

    page.newChat();
    assert.equal(page.status.hidden, true);
    assert.equal(page.status.textContent, "");
});

test("accepted cancellation hides Stop and displays its pending outcome", async () => {
    const page = fixture();
    await submit(page);
    page.stopButton.listeners.get("click")();
    await flush();
    page.requests[2].gate.resolve(response(runSummary("running", { cancel_requested: true })));
    await flush();
    assert.equal(page.stopButton.hidden, true);
    assert.match(page.status.textContent, /cancellation was requested/i);
});

test("definitive submission rejections restore drafts and release pending admission", async (t) => {
    const status = 400;
    await t.test(`${status} removes only the rejected node, restores the prompt and permits retry`, async () => {
        const page = fixture({ history: [{ role: "user", content: "question" }, { role: "assistant", content: "accepted" }] });
        const retained = [...page.log.children];
        page.input.value = "question";
        page.submit();
        page.requests[0].gate.resolve(response({ error: "Rejected." }, status));
        await flush();
        assert.equal(page.input.value, "question");
        assert.deepEqual(page.log.children, retained);
        assert.equal(page.sessionStorage.getItem("nautobot-agent-pending-requests"), null);
        assert.equal(page.sendButton.disabled, false);
        page.submit();
        assert.equal(page.requests.length, 2);
        assert.equal(JSON.parse(page.requests[1].options.body).message, "question");
        assert.equal(page.log.children.length, 3);
        assert.equal(page.log.children[0], retained[0]);
        assert.equal(page.log.children[1], retained[1]);
        page.requests[1].gate.resolve(response(runSummary(), 202));
        await flush();
        assert.equal(page.log.children.filter((node) => node.classList.contains("agent-message-user")).length, 2);
    });
    await t.test(`${status} restores the empty view when the rejected bubble was the only entry`, async () => {
        const page = fixture();
        page.input.value = "question";
        page.submit();
        assert.equal(page.root.querySelector("#agent-empty").hidden, true);
        page.requests[0].gate.resolve(response({ error: "Rejected." }, status));
        await flush();
        assert.equal(page.log.children.length, 0);
        assert.equal(page.root.querySelector("#agent-empty").hidden, false);
    });
    for (const draft of ["updated draft", "   "]) {
        await t.test(`preserves draft ${JSON.stringify(draft)} edited while POST is pending`, async () => {
            const page = fixture();
            page.input.value = "question";
            page.submit();
            page.input.value = draft;
            page.input.listeners.get("input")();
            page.requests[0].gate.resolve(response({ error: "Rejected." }, 400));
            await flush();
            assert.equal(page.input.value, draft);
            assert.equal(page.sendButton.disabled, !draft.trim());
        });
    }
});

test("dispatch failures and login redirects preserve the composer through reconciliation", async t => {
    for (const mode of ["dispatch", "login"]) for (const edited of [false, true]) {
        await t.test(`${mode}, edited=${edited}`, async () => {
            const page = fixture();
            page.input.value = "unsent question";
            page.submit();
            if (edited) {
                page.input.value = "next question";
                page.input.listeners.get("input")();
            }
            page.requests[0].gate.resolve(mode === "dispatch"
                ? response(runSummary("dispatch_failed"), 503)
                : response({}, 200, {redirected: true, body: "<html>login</html>"}));
            await flush();
            if (mode === "dispatch") {
                page.requests[1].gate.resolve(response(snapshot(runSummary("dispatch_failed"))));
                await flush();
            } else assert.match(page.status.textContent, /Sign in/);
            assert.equal(page.input.value, edited ? "next question" : "unsent question");
            assert.equal(page.sendButton.disabled, false);
            assert.equal(page.log.children.length, 0);
            assert.equal(page.sessionStorage.getItem("nautobot-agent-pending-requests"), null);
        });
    }
});

test("busy conversation restores the draft and permits a later send after completion", async () => {
    const blocker = runSummary("running");
    const page = fixture({ conversation: conversationId });
    page.input.value = "question";
    page.submit();
    page.requests[0].gate.resolve(response({ error: "Conversation already has an active run.", blocking_run: blocker }, 409));
    await flush();
    assert.equal(page.url, `?conversation=${conversationId}`);
    assert.equal(page.input.value, "");
    assert.equal(page.sendButton.disabled, true);
    assert.equal(page.requests[1].url, `/state/?conversation=${conversationId}`);
    page.requests[1].gate.resolve(response(snapshot(blocker, {
        history: [{ role: "assistant", content: "blocker history" }],
    })));
    await flush();
    assert.equal(page.log.find(node => node.dataset.raw === "blocker history") !== null, true);
    assert.equal(page.input.value, "question");
    assert.equal(page.sendButton.disabled, true);
    assert.equal(page.status.textContent.includes("uncertain"), false);
    assert.equal(page.requests[2].url, `/runs/${runId}/events/?status_only=1`);
    page.requests[2].gate.resolve(response(runPage("completed")));
    await flush();
    assert.equal(page.requests[3].url, `/state/?conversation=${conversationId}`);
    page.requests[3].gate.resolve(response(snapshot(runSummary("completed"), {
        history: [{ role: "assistant", content: "blocker completed" }],
    })));
    await flush();
    assert.equal(page.input.value, "question");
    assert.equal(page.sendButton.disabled, false);
    assert.equal(page.requests.filter(request => request.url === "/runs").length, 1);
    page.submit();
    assert.equal(page.requests[4].url, "/runs");
    assert.equal(JSON.parse(page.requests[4].options.body).conversation_id, conversationId);
});

test("busy conversation tolerates an unavailable snapshot while observing its blocker", async () => {
    const page = fixture({ conversation: conversationId, modelName: "Model B", history: [{ role: "user", content: "question" }] });
    const priorMessage = page.log.children[0];
    page.input.value = "question";
    page.submit();
    const optimisticMessage = page.log.children[1];
    page.requests[0].gate.resolve(response({ error: "Conversation already has an active run.", blocking_run: runSummary() }, 409));
    await flush();
    page.requests[1].gate.resolve(response({}, 503));
    await flush();
    assert.equal(page.input.value, "question");
    assert.deepEqual(page.log.children, [priorMessage]);
    assert.equal(optimisticMessage.isConnected, false);
    assert.equal(page.sendButton.disabled, true);
    assert.equal(page.root.querySelector("#agent-model-name").textContent, "Model B");
    assert.equal(page.root.querySelector("#agent-model-name").hidden, false);
    assert.equal(page.requests[2].url, `/runs/${runId}/events/?status_only=1`);
});

test("departure cookies follow current selection and browser page lifecycle without requests", async () => {
    const page = fixture();
    const cookieName = "nautobot_agent_chat_departure";
    const cookiePath = "/plugins/nautobot-agent/";
    const initialRequestCount = page.requests.length;
    assert.equal(page.cookieWrites[0], `${cookieName}=; Path=${cookiePath}; SameSite=Lax; Max-Age=0`);

    await submit(page);
    assert.equal(page.url, `?conversation=${conversationId}`);
    const requestCount = page.requests.length;
    page.pagehide(false);
    assert.equal(
        page.cookieWrites.at(-1),
        `${cookieName}=aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa:${conversationId}:1700000000123; Path=${cookiePath}; SameSite=Lax; Max-Age=31536000`,
    );
    page.pagehide(true);
    assert.equal(page.cookieWrites.at(-1), page.cookieWrites.at(-2));
    page.pageshow();
    assert.equal(page.cookieWrites.at(-1), `${cookieName}=; Path=${cookiePath}; SameSite=Lax; Max-Age=0`);
    assert.equal(page.requests.length, requestCount);
    assert.ok(page.requests.every(({ options }) => !options.signal?.aborted));

    page.newChat();
    page.pagehide();
    assert.match(page.cookieWrites.at(-1), new RegExp(`^${cookieName}=aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa:new:1700000000123;`));
    assert.equal(page.requests.length, requestCount);
    assert.equal(initialRequestCount, 0);
});

test("departure cookies use HTTPS Secure and configured path", () => {
    const path = "/custom/agent/";
    const page = fixture({ conversation: conversationId, protocol: "https:", cookiePath: path });
    assert.equal(page.cookieWrites[0], `nautobot_agent_chat_departure=; Path=${path}; SameSite=Lax; Max-Age=0; Secure`);
    page.pagehide();
    assert.equal(
        page.cookieWrites.at(-1),
        `nautobot_agent_chat_departure=aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa:${conversationId}:1700000000123; Path=${path}; SameSite=Lax; Max-Age=31536000; Secure`,
    );
});

test("departure timestamps use server time plus elapsed time despite browser clock skew", () => {
    for (const skew of [-900_000, 900_000]) {
        let now = 1_700_000_000_123 + skew;
        const page = fixture({ conversation: conversationId, browserNow: () => now });
        now += 5_000;
        page.pagehide();
        assert.match(page.cookieWrites.at(-1), new RegExp(`:${conversationId}:1700000005123;`));
        now += 660_000;
        page.pageshow();
        page.pagehide();
        assert.match(page.cookieWrites.at(-1), new RegExp(`:${conversationId}:1700000665123;`));
    }
});

test("an open background page records no departure", async () => {
    const page = fixture({ conversation: conversationId });
    page.document.hidden = true;
    await flush();
    assert.equal(page.windowListeners.has("visibilitychange"), false);
    assert.equal(page.cookieWrites.length, 1);
    assert.equal(page.requests.length, 0);
});

test("Back or Forward reloads instead of fetching an in-place conversation", () => {
    const page = fixture({ conversation: conversationId });
    page.popstate("?conversation=bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb");
    assert.equal(page.reloads, 1);
    assert.equal(page.requests.length, 0);
});

test("restored configuration failure shows its remedy instead of partial-progress guidance", () => {
    const remedy = "Ask an administrator to configure the Agent model.";
    const page = fixture({
        conversation: conversationId,
        latest: runSummary("failed", { error_code: "llm_not_configured", error_message: remedy }),
    });
    assert.equal(page.status.hidden, false);
    assert.ok(page.status.textContent.startsWith(remedy));
    assert.doesNotMatch(page.status.textContent, /partial progress/);
    page.input.value = "try after setup";
    page.input.listeners.get("input")();
    assert.equal(page.sendButton.disabled, false);
});

test("categorized model failures keep guidance primary and technical metadata secondary", async () => {
    const remedy = "Check the Agent model configuration and try again.";
    const categorized = runSummary("failed", {
        error_code: "model_authentication_failed",
        error_message: remedy,
        execution_error: remedy,
    });
    const page = fixture({ conversation: conversationId, latest: categorized });
    assert.equal(page.status.textContent, remedy);
    assert.equal(page.root.querySelector("#agent-run-status-details").hidden, false);
    assert.doesNotMatch(page.status.textContent, /partial progress|Run ID|Diagnostic:/);

    const live = fixture({ conversation: conversationId });
    await submit(live);
    events(live).gate.resolve(response(runPage("failed", {
        ...categorized,
        events: [{ id: "1-0", event: "error", data: { error: remedy } }],
        next_cursor: "1-0",
    })));
    await flush();
    assert.equal(live.status.textContent, remedy);
    assert.doesNotMatch(live.status.textContent, /partial progress|Run ID|Diagnostic:/);
});

test("unknown saved failures retain the generic fallback and inline diagnostics", () => {
    const page = fixture({
        conversation: conversationId,
        latest: runSummary("failed", { error_code: "legacy_failure", error_message: null }),
    });
    assert.doesNotMatch(page.status.textContent, /Run ID:/);
    assert.match(page.status.textContent, /legacy_failure/);
    assert.equal(page.root.querySelector("#agent-run-status-details").hidden, true);
});

test("timed-out runs show failure guidance while user stops show cancellation without an error code", () => {
    const remedy = "The run reached its time limit and was stopped before finishing. Earlier checkpoints may contain partial progress.";
    const timedOut = fixture({
        conversation: conversationId,
        latest: runSummary("failed", { error_code: "soft_deadline", error_message: remedy }),
    });
    assert.ok(timedOut.status.textContent.startsWith(remedy));
    assert.doesNotMatch(timedOut.status.textContent, /cancelled/i);

    const stopped = fixture({
        conversation: conversationId,
        latest: runSummary("cancelled", { error_code: null, error_message: null }),
    });
    assert.match(stopped.status.textContent, /safely cancelled/i);
    assert.doesNotMatch(stopped.status.textContent, /Error:/);
});

test("diagnostic warnings render on reopened and live terminal runs without blocking later sends", async () => {
    const page = fixture({
        conversation: conversationId,
        latest: runSummary("completed", { external_outcome_unknown: true, cleanup_incomplete: true }),
    });
    assert.match(page.status.textContent, /Completed\./);
    assert.match(page.status.textContent, /may have taken effect without a confirmed result/);
    assert.match(page.status.textContent, /cleanup could not be confirmed; local cleanup does not roll back/);
    assert.doesNotMatch(page.status.textContent, /rolled back|no effect/i);
    page.input.value = "next request";
    page.submit();
    page.requests[0].gate.resolve(response(runSummary("running"), 202));
    await flush();
    assert.doesNotMatch(page.status.textContent, /Warning:/);
    events(page).gate.resolve(response(runPage("failed", { external_outcome_unknown: true })));
    await flush();
    assert.match(page.status.textContent, /may have taken effect without a confirmed result/);
    assert.doesNotMatch(page.status.textContent, /cleanup could not be confirmed/);
});

test("bootstrap canonicalizes selection and restores status-only runs", async (t) => {
    await t.test("retained bootstrap renders model, context, todos and history without a state fetch", async () => {
        const page = fixture({
            conversation: conversationId,
            history: [{ role: "assistant", content: "retained answer" }],
            modelName: "<Captured model>",
            todos: [{ content: "retained task", status: "pending" }],
            contextUsage: { used: 25, limit: 100, percent: 25 },
            latest: runSummary("completed"),
            initialScroll: { scrollHeight: 2400, clientHeight: 100, scrollTop: 0 },
        });
        assert.equal(page.log.find(node => node.dataset.raw === "retained answer") !== null, true);
        const badge = page.root.querySelector("#agent-model-name");
        assert.equal(badge.textContent, "<Captured model>");
        assert.equal(badge.hidden, false);
        assert.equal(page.root.querySelector("#agent-context-status").textContent, "Context 25%");
        assert.equal(page.root.querySelector("#agent-todos-list").children[0].children[1].textContent, "retained task");
        assert.match(page.status.textContent, /Completed/);
        assert.equal(page.requests.length, 0);
        assert.equal(page.root.querySelector("#agent-scroller").scrollTop, 2400);
        page.input.value = "next question";
        page.input.listeners.get("input")();
        assert.equal(page.sendButton.disabled, false);
    });
    await t.test("bootstrap canonicalizes the convenience-selected conversation to an explicit tab URL", async () => {
        const page = fixture({ conversation: conversationId, urlConversation: null });
        assert.equal(page.url, `?conversation=${conversationId}`);
    });
    await t.test("restored active runs are status-only and durable refusal replaces provisional output", async () => {
        const restored = runSummary();
        const page = fixture({ conversation: conversationId, active: restored });
        assert.equal(page.requests[0].url, `/runs/${runId}/events/?status_only=1`);
        page.requests[0].gate.resolve(response(runPage("refused")));
        await flush();
        assert.equal(page.requests[1].url, `/state/?conversation=${conversationId}`);
        page.requests[1].gate.resolve(response(snapshot(runSummary("refused"), {
            history: [{ role: "assistant", content: "refused durably" }],
        })));
        await flush();
        assert.equal(page.log.find((node) => node.dataset.raw === "refused durably") !== null, true);
        assert.match(page.root.querySelector("#agent-run-status").textContent, /refused/i);
        await flush();
        assert.equal(page.requests.length, 2, "one status poll is followed by exactly one durable rebuild");
    });
    await t.test("interrupted bootstrap releases the conversation and always states uncertain effects", async () => {
        const page = fixture({ conversation: conversationId, latest: runSummary("interrupted", { error_code: "stale_heartbeat" }) });
        await flush();
        assert.equal(page.requests.length, 0);
        const status = page.root.querySelector("#agent-run-status").textContent;
        assert.match(status, /interrupted/i);
        assert.match(status, /external effects may remain uncertain/i);
        assert.doesNotMatch(status, /rolled back|administrator/i);
        page.input.value = "after interruption";
        page.input.listeners.get("input")();
        assert.equal(page.sendButton.disabled, false);
    });
});

test("durable rebuild disposes chart resources and preserves opaque tool IDs", async () => {
    const displayId = '["assistant","tool","provider-owned-id"]';
    const page = fixture(); await submit(page);
    events(page).gate.resolve(response(runPage("running", {
        events: [{ id: "1-0", event: "tool_call", data: { id: displayId, name: "render_chart", args: {}, status: "completed", chart: { title: { text: "chart" }, series: [] } } }],
        next_cursor: "1-0",
    })));
    await flush();
    assert.equal(page.charts.length, 1);
    const nextPoll = page.requests.at(-1);
    assert.match(nextPoll.url, /after=1-0/);
    nextPoll.gate.resolve(response(runPage("completed", { next_cursor: "1-0" })));
    await flush();
    page.requests.at(-1).gate.resolve(response(snapshot(runSummary("completed"))));
    await flush();
    assert.equal(page.charts[0].disposed, true);
    assert.equal(page.log.find((node) => node.textContent === "provider-owned-id") !== null, false);
});

test("fully observed successful completion preserves historical DOM and chart resources", async () => {
    const displayId = '["assistant","tool","historical"]';
    const page = fixture({ conversation: conversationId, history: [
        { role: "user", content: "prior question" },
        { role: "assistant", content: "prior answer" },
        { role: "tool", id: displayId, name: "render_chart", args: {}, status: "completed",
            chart: { title: { text: "prior chart" }, series: [] } },
    ], modelName: "Model A" });
    const oldNodes = [...page.log.children];
    const chart = page.charts[0];
    const detail = page.log.find(node => node.classList.contains("agent-tool"));
    detail.open = false;
    const scroller = page.root.querySelector("#agent-scroller");
    scroller.scrollHeight = 2000;
    scroller.clientHeight = 100;
    scroller.scrollTop = 0;
    await submit(page);
    events(page).gate.resolve(response(runPage("completed", { events: [
        { id: "1-0", event: "token", data: { text: "answer" } },
        { id: "1-1", event: "tool_call", data: { id: '["assistant","tool","current"]', name: "lookup", args: {}, status: "completed" } },
        { id: "1-2", event: "todos", data: { todos: [{ content: "task", status: "completed" }] } },
        { id: "1-3", event: "done", data: {} },
    ], next_cursor: "1-3" })));
    await flush();
    scroller.scrollTop = 0;
    page.requests.at(-1).gate.resolve(response(snapshot(runSummary("completed"), {
        history: [
            { role: "user", content: "prior question" }, { role: "assistant", content: "prior answer" },
            { role: "user", content: "question" }, { role: "assistant", content: "answer" },
        ],
        todos: [{ content: "durable task", status: "completed" }],
        context_usage: { used: 12, limit: 100, percent: 12 },
        model_name: "Model B",
    })));
    await flush();
    assert.equal(page.charts.includes(chart), true);
    assert.equal(chart.disposed, false);
    assert.deepEqual(page.log.children.slice(0, oldNodes.length), oldNodes);
    assert.equal(detail.open, false);
    assert.equal(scroller.scrollTop, 0);
    assert.equal(page.root.querySelector("#agent-model-name").textContent, "Model B");
    assert.equal(page.root.querySelector("#agent-context-status").textContent, "Context 12%");
    assert.equal(page.root.querySelector("#agent-todos-list").children[0].children[1].textContent, "durable task");
});

test("successful completion restores turns made in another tab", async () => {
    const page = fixture({ conversation: conversationId });
    await submit(page);
    events(page).gate.resolve(response(runPage("completed", { events: [
        { event: "token", data: { text: "answer two" } },
        { event: "done", data: {} },
    ] })));
    await flush();
    page.requests.at(-1).gate.resolve(response(snapshot(runSummary("completed"), {
        history: [
            { role: "user", content: "question from tab A" },
            { role: "assistant", content: "answer from tab A" },
            { role: "user", content: "question" },
            { role: "assistant", content: "answer two" },
        ],
    })));
    await flush();
    assert.equal(page.log.querySelectorAll(".agent-message-user").length, 2);
    assert.ok(page.log.find(node => node.dataset.raw === "answer from tab A"));
    assert.ok(page.log.find(node => node.dataset.raw === "answer two"));
});

test("snapshot scroll decision survives nested tool and todo rebuilds", async t => {
    for (const nearBottom of [false, true]) await t.test(nearBottom ? "near bottom" : "reading earlier output", async () => {
        const page = fixture({ conversation: conversationId, history: [{ role: "assistant", content: "old" }] });
        const scroller = page.root.querySelector("#agent-scroller");
        Object.defineProperty(scroller, "scrollHeight", { get: () => page.log.children.length ? 1200 : 0 });
        scroller.clientHeight = 100;
        scroller.scrollTop = nearBottom ? 1100 : 0;
        await submit(page);
        events(page).gate.resolve(response(runPage("completed", { events: [
            { id: "1-0", event: "token", data: { text: "partial" } },
        ], next_cursor: "1-0" })));
        await flush();
        scroller.scrollTop = nearBottom ? 1100 : 0;
        page.requests.at(-1).gate.resolve(response(snapshot(runSummary("completed"), {
            history: [
                { role: "assistant", content: "rebuilt" },
                { role: "tool", id: "opaque-tool", name: "lookup", args: {}, status: "completed" },
            ],
            todos: [{ content: "rebuilt task", status: "pending" }],
        })));
        await flush();
        assert.equal(scroller.scrollTop, nearBottom ? 1200 : 0);
        assert.equal(page.log.find(node => node.classList.contains("agent-tool")) !== null, true);
        assert.equal(page.root.querySelector("#agent-todos-list").children.length, 1);
    });
});


test("first-turn canonicalization preserves scroll for preservation and missing-done rebuild", async t => {
    for (const includeDone of [false, true]) await t.test(includeDone ? "preservation" : "rebuild", async () => {
        const page = fixture();
        const scroller = page.root.querySelector("#agent-scroller");
        scroller.scrollHeight = 2400;
        scroller.clientHeight = 100;
        await submit(page);
        scroller.scrollTop = 0;
        events(page).gate.resolve(response(runPage("completed", { events: [
            { id: "1-0", event: "token", data: { text: "long first response" } },
            ...(includeDone ? [{ id: "1-1", event: "done", data: {} }] : []),
        ], next_cursor: includeDone ? "1-1" : "1-0" })));
        await flush();
        scroller.scrollTop = 0;
        page.requests.at(-1).gate.resolve(response(snapshot(runSummary("completed"), {
            history: [{ role: "user", content: "question" }, { role: "assistant", content: "long first response" }],
        })));
        await flush();
        assert.equal(scroller.scrollTop, 0);
    });
});
test("terminal, error, and observation-stop states remain covered before response text", async t => {
    await t.test("terminal without response tokens", async () => {
        const page = fixture(); await submit(page);
        events(page).gate.resolve(response(runPage("completed")));
        await flush();
    });
    await t.test("error event", async () => {
        const page = fixture(); await submit(page);
        events(page).gate.resolve(response(runPage("running", {
            events: [{ id: "1-0", event: "error", data: { error: "failed" } }],
        })));
        await flush();
    });
    await t.test("permanent observation stop", async () => {
        const page = fixture(); await submit(page);
        events(page).gate.resolve(response({ error: "Sign in" }, 403));
        await flush();
    });
    await t.test("new run supersedes a previous terminal run", async () => {
        const page = fixture({ conversation: conversationId, active: null,
            latest: runSummary("completed", { run_id: "old-terminal-run" }) });
        page.input.value = "new question";
        page.submit();
        page.requests[0].gate.resolve(response(runSummary("running"), 202));
        await flush();
    });
});

test("retryable pre-token poll failures keep the active run status visible", async () => {
    const page = fixture();
    await submit(page);
    events(page).gate.resolve(response({}, 503));
    await flush();
    assert.match(page.status.textContent, /retrying/);
    events(page).gate.resolve(response(runPage("running", {
        events: [{ event: "token", data: { text: "recovered answer" } }],
        next_cursor: "1-0",
    })));
    await flush();
    assert.equal(page.log.find(node => node.dataset.raw === "recovered answer") !== null, true);
});
test("streaming messages finalize at boundaries and cancel stale scheduled renders", async () => {
    const page = fixture();
    await submit(page);
    events(page).gate.resolve(response(runPage("running", {
        events: [
            { id: "1-0", event: "message_start", data: {} },
            { id: "1-1", event: "message_start", data: {} },
            { id: "1-2", event: "token", data: { text: "First " } },
            { id: "1-3", event: "token", data: { text: "answer" } },
        ],
        next_cursor: "1-3", has_more: true,
    })));
    await flush();
    let log = page.root.querySelector("#agent-log");
    let messages = log.children.filter(node => node.classList.contains("agent-message-assistant"));
    assert.equal(messages.length, 1, "starts before any text do not create empty bubbles");
    const firstText = messages[0].querySelector(".agent-parts").children[0];
    assert.equal(firstText.dataset.raw, "First answer");
    assert.equal(firstText.innerHTML, "");
    page.flushAnimationFrames();
    assert.equal(firstText.innerHTML, "First answer", "an explicit frame flush renders accumulated tokens");
    events(page).gate.resolve(response(runPage("completed", {
        events: [
            { id: "1-4", event: "token", data: { text: " boundary" } },
            { id: "1-5", event: "message_start", data: {} },
            { id: "1-6", event: "message_start", data: {} },
            { id: "1-7", event: "token", data: { text: "Second answer" } },
            { id: "1-8", event: "done", data: {} },
        ],
        next_cursor: "1-8",
    })));
    await flush();
    log = page.root.querySelector("#agent-log");
    messages = log.children.filter(node => node.classList.contains("agent-message-assistant"));
    assert.equal(messages.length, 2);
    assert.equal(firstText.dataset.raw, "First answer boundary");
    assert.equal(firstText.innerHTML, "First answer boundary", "message_start finalizes text before its pending frame");
    assert.equal(messages[1].querySelector(".agent-parts").children[0].dataset.raw, "Second answer");
    assert.equal(messages[1].querySelector(".agent-parts").children[0].innerHTML, "Second answer");
    for (const message of messages) {
        assert.equal(message.classList.contains("agent-pending"), false);
        assert.equal(message.querySelector(".agent-parts").children[0].classList.contains("agent-streaming"), false);
    }
    const beforeFlush = messages.map(message => message.querySelector(".agent-parts").children[0].innerHTML);
    page.flushAnimationFrames();
    assert.deepEqual(messages.map(message => message.querySelector(".agent-parts").children[0].innerHTML), beforeFlush);
});

test("tool updates preserve transcript order and reuse the visible tool node", async () => {
    const page = fixture();
    await submit(page);
    const displayId = '["assistant","tool","lookup-1"]';
    events(page).gate.resolve(response(runPage("running", {
        events: [
            { id: "1-0", event: "token", data: { text: "Before tool" } },
            { id: "1-1", event: "tool_call", data: { id: displayId, name: "lookup", args: { query: "x" }, status: "running" } },
        ],
        next_cursor: "1-1", has_more: true,
    })));
    await flush();
    const log = page.root.querySelector("#agent-log");
    const [before, tool] = log.children.slice(1);
    assert.equal(before.classList.contains("agent-message-assistant"), true);
    assert.equal(tool.classList.contains("agent-tool"), true);
    page.flushAnimationFrames();
    events(page).gate.resolve(response(runPage("completed", {
        events: [
            { id: "1-2", event: "tool_call", data: { id: displayId, name: "lookup", args: { query: "x" }, status: "completed" } },
            { id: "1-3", event: "token", data: { text: "After tool" } },
            { id: "1-4", event: "done", data: {} },
        ],
        next_cursor: "1-4",
    })));
    await flush();
    assert.deepEqual(log.children.slice(1).map(node => node.classList.contains("agent-message-assistant")
        ? "assistant" : node.classList.contains("agent-tool") ? "tool" : "other"), ["assistant", "tool", "assistant"]);
    const after = log.children[3];
    assert.equal(before.querySelector(".agent-parts").children[0].dataset.raw, "Before tool");
    assert.equal(before.querySelector(".agent-parts").children[0].innerHTML, "Before tool");
    assert.equal(after.querySelector(".agent-parts").children[0].dataset.raw, "After tool");
    assert.equal(after.querySelector(".agent-parts").children[0].innerHTML, "After tool");
    assert.equal(log.querySelectorAll(".agent-tool").length, 1);
    assert.equal(log.children[2], tool, "same display ID updates the existing tool node");
    assert.equal(tool.dataset.status, "completed");
    assert.equal(tool.querySelector(".agent-tool-status").textContent, "Completed");
});

test("todo events render supported statuses, replace rows, clear, and match durable snapshots", async () => {
    const page = fixture();
    await submit(page);
    const panel = page.root.querySelector("#agent-todos-panel");
    const list = page.root.querySelector("#agent-todos-list");
    const deliver = async (runStatus, eventList, cursor, hasMore = false) => {
        events(page).gate.resolve(response(runPage(runStatus, {
            events: eventList, next_cursor: cursor, has_more: hasMore,
        })));
        await flush();
    };
    await deliver("running", [{ id: "1-0", event: "todos", data: { todos: [
        { content: "Queued work", status: "pending" },
        { content: "Active work", status: "in_progress" },
        { content: "Finished work", status: "completed" },
    ] } }], "1-0", true);
    const assertRows = (rows, expected) => {
        assert.deepEqual(rows.map(row => [
            row.querySelector(".agent-todo-content").textContent,
            row.querySelector(".agent-todo-status").textContent,
        ]), expected.map(([, content, label]) => [content, label]));
        rows.forEach((row, index) => assert.equal(row.classList.contains(expected[index][0]), true));
    };
    assert.equal(panel.hidden, false);
    assertRows(list.children, [
        ["agent-todo-pending", "Queued work", "Pending"],
        ["agent-todo-in-progress", "Active work", "In progress"],
        ["agent-todo-completed", "Finished work", "Completed"],
    ]);
    const replaced = [...list.children];
    await deliver("running", [{ id: "1-1", event: "todos", data: { todos: [
        { content: "Replacement", status: "in_progress" },
    ] } }], "1-1", true);
    assert.equal(list.children.length, 1);
    assert.equal(list.children[0].children[1].textContent, "Replacement");
    assert.equal(replaced.every(row => !row.isConnected), true);
    await deliver("completed", [
        { id: "1-2", event: "todos", data: { todos: [] } },
        { id: "1-3", event: "done", data: {} },
    ], "1-3");
    assert.equal(list.children.length, 0);
    assert.equal(panel.hidden, true);

    const durable = fixture({
        conversation: conversationId,
        history: [{ role: "assistant", content: "answer" }],
        todos: [{ content: "Durable task", status: "completed" }],
        latest: runSummary("completed"),
    });
    const durablePanel = durable.root.querySelector("#agent-todos-panel");
    const durableList = durable.root.querySelector("#agent-todos-list");
    assert.equal(durablePanel.hidden, false);
    assertRows(durableList.children, [
        ["agent-todo-completed", "Durable task", "Completed"],
    ]);
});

test("context event boundaries, clamping, unknown limits, and clearing are observable", async () => {
    const page = fixture();
    await submit(page);
    const display = page.root.querySelector("#agent-context-display");
    const meter = page.root.querySelector("#agent-context-meter");
    const indicator = page.root.querySelector("#agent-context");
    const fill = page.root.querySelector("#agent-context-fill");
    const status = page.root.querySelector("#agent-context-status");
    const details = page.root.querySelector("#agent-context-details");
    const scenarios = [
        { id: "1-0", percent: 69.9, level: "normal", aria: "69.9", width: "69.9%" },
        { id: "1-1", percent: 70, level: "warning", aria: "70", width: "70%" },
        { id: "1-2", percent: 84.9, level: "warning", aria: "84.9", width: "84.9%" },
        { id: "1-3", percent: 85, level: "danger", aria: "85", width: "85%" },
        { id: "1-4", percent: 140, level: "danger", aria: "100", width: "100%" },
    ];
    for (const [index, scenario] of scenarios.entries()) {
        events(page).gate.resolve(response(runPage("running", {
            events: [{ id: scenario.id, event: "context", data: { used: 140, limit: 100, percent: scenario.percent } }],
            next_cursor: scenario.id, has_more: index < scenarios.length - 1,
        })));
        await flush();
        assert.equal(indicator.hidden, false);
        assert.equal(meter.hidden, false);
        assert.equal(display.dataset.level, scenario.level);
        assert.equal(display.attributes.get("role"), "meter");
        assert.equal(display.attributes.get("aria-valuemin"), "0");
        assert.equal(display.attributes.get("aria-valuemax"), "100");
        assert.equal(display.attributes.get("aria-valuenow"), scenario.aria);
        assert.equal(fill.style.width, scenario.width);
        assert.equal(status.textContent, `Context ${scenario.percent.toFixed(1).replace(".0", "")}%`);
        assert.match(details.textContent, /140 \/ 100 tokens/);
    }
    events(page).gate.resolve(response(runPage("running", {
        events: [{ id: "1-5", event: "context", data: { used: 140, limit: null, percent: null } }],
        next_cursor: "1-5", has_more: true,
    })));
    await flush();
    assert.equal(indicator.hidden, false);
    assert.equal(meter.hidden, true);
    assert.equal(status.textContent, "Context 140 tokens · limit unknown");
    assert.equal(display.attributes.has("role"), false);
    assert.equal(display.attributes.has("aria-valuenow"), false);
    events(page).gate.resolve(response(runPage("completed", {
        events: [
            { id: "1-6", event: "context", data: { used: null, limit: null, percent: null } },
            { id: "1-7", event: "done", data: {} },
        ],
        next_cursor: "1-7",
    })));
    await flush();
    assert.equal(indicator.hidden, true);
    assert.equal(meter.hidden, true);
    assert.equal(status.textContent, "");
    assert.equal(details.textContent, "");
    assert.equal(display.dataset.level, "");
    for (const name of ["role", "aria-valuemin", "aria-valuemax", "aria-valuenow", "aria-valuetext"])
        assert.equal(display.attributes.has(name), false, `${name} is cleared`);
});
