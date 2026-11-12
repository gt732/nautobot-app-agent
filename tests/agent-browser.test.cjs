const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { pathToFileURL } = require("node:url");
const { snapshot } = require("./run-fixtures.cjs");

test("chat sanitizes untrusted Markdown and reports failed message and code copies", () => {
    const browser = process.env.CHROME_BIN || [
        "C:/Program Files/Google/Chrome/Application/chrome.exe",
        "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ].find((candidate) => fs.existsSync(candidate));
    assert.ok(browser, "Install Chrome/Chromium or set CHROME_BIN to its executable.");
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "agent-browser-"));
    const asset = (name) => pathToFileURL(path.resolve(
        __dirname, "../nautobot_agent/static/nautobot_agent", name,
    )).href;
    const state = snapshot(null, {
        history: [{ role: "assistant", content: [
            '<img src="https://example.invalid/leak"><picture><source></picture>',
            '<video></video><audio></audio><style>body { display: none }</style>',
            '<form><input><button>Untrusted</button><textarea></textarea><select></select></form>',
            '<dialog open>Untrusted</dialog>',
            '<p id="agent-form" name="agent-send" style="color:red" background="bad" role="button" hidden popover open data-action="bad" class="position-fixed" onclick="alert(1)">Safe text</p>',
            '<a href="https://example.com/">Safe link</a>',
            "",
            "**Safe emphasis**",
            "",
            "```python",
            "print(42)",
            "```",
        ].join("\n") }],
    });
    const ids = [
        "scroller", "empty", "new-chat", "model-name", "stop", "run-status",
        "run-status-spinner", "run-status-text", "run-status-details", "run-status-error-code",
        "run-status-execution-error", "todos-panel", "todos-list", "context", "context-status",
        "context-display", "context-meter", "context-fill", "context-details",
    ];
    const html = `<!doctype html><meta charset="utf-8">
        <meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src file: 'unsafe-inline'">
        <div id="agent-chat" data-llm-configured="true" data-departure-cookie-name="departure"
             data-departure-user-id="test" data-departure-cookie-path="/" data-server-now-ms="${Date.now()}">
            ${ids.map((id) => `<div id="agent-${id}"></div>`).join("")}
            <div id="agent-log"></div>
            <form id="agent-form"><input name="csrfmiddlewaretoken" value="test">
                <textarea id="agent-input"></textarea><button id="agent-send"><i></i></button>
            </form>
        </div>
        <script id="agent-state" type="application/json">${JSON.stringify(state).replaceAll("<", "\\u003c")}</script>
        <script>
            window.NautobotAgentMermaid = { createRenderer: () => ({ enhance() {} }) };
            window.hljs = { getLanguage: () => true, highlightElement() {} };
            window.addEventListener("error", (event) => {
                document.body.dataset.error = event.message;
            });
        </script>
        ${["vendor/marked.min.js", "vendor/purify.min.js", "clipboard.js", "agent-runs.js", "agent.js"]
            .map((name) => `<script src="${asset(name)}"></script>`).join("")}
        <script>
            (async () => {
                const markdown = document.querySelector(".agent-markdown");
                const result = {
                    error: document.body.dataset.error || null,
                    // The app adds its own code-copy button after sanitizing.
                    forbiddenTags: markdown.querySelectorAll("img,picture,source,video,audio,style,form,input,button:not(.agent-copy-code),textarea,select,dialog").length,
                    forbiddenAttributes: markdown.querySelectorAll("[id],[name],[style],[background],[role],[hidden],[popover],[open],[data-action],[onclick],.position-fixed").length,
                    emphasis: markdown.querySelector("strong").textContent,
                    code: markdown.querySelector("code").textContent,
                    codeClass: markdown.querySelector("code").className,
                    linkTarget: markdown.querySelector("a").target,
                    linkRel: markdown.querySelector("a").rel,
                    copies: [],
                };
                window.NautobotAgentClipboard.copyText = async () => false;
                for (const selector of [".agent-copy-message", ".agent-copy-code"]) {
                    const button = document.querySelector(selector);
                    button.click();
                    await Promise.resolve();
                    result.copies.push({
                        icon: button.querySelector("i").className,
                        title: button.title,
                        label: button.getAttribute("aria-label"),
                    });
                }
                const output = document.createElement("pre");
                output.id = "result";
                output.textContent = JSON.stringify(result);
                document.body.replaceChildren(output);
            })();
        </script>`;
    try {
        const file = path.join(directory, "chat.html");
        fs.writeFileSync(file, html);
        const browserResult = spawnSync(browser, [
            "--headless", "--no-sandbox", "--disable-gpu", "--disable-background-networking",
            `--user-data-dir=${path.join(directory, "profile")}`, "--dump-dom", pathToFileURL(file).href,
        ], { encoding: "utf8", timeout: 30000 });
        assert.ifError(browserResult.error);
        assert.equal(browserResult.status, 0, browserResult.stderr);
        const output = browserResult.stdout.match(/<pre id="result">([^<]+)<\/pre>/);
        assert.ok(output, browserResult.stdout + browserResult.stderr);
        const result = JSON.parse(output[1]);
        assert.equal(result.error, null);
        assert.equal(result.forbiddenTags, 0);
        assert.equal(result.forbiddenAttributes, 0);
        assert.equal(result.emphasis, "Safe emphasis");
        assert.equal(result.code, "print(42)\n");
        assert.equal(result.codeClass, "language-python");
        assert.equal(result.linkTarget, "_blank");
        assert.equal(result.linkRel, "noopener noreferrer");
        for (const copy of result.copies) {
            assert.equal(copy.icon, "mdi mdi-alert-circle-outline");
            assert.equal(copy.title, "Copy failed. Select the text and copy it manually.");
            assert.equal(copy.label, copy.title);
        }
    } finally {
        fs.rmSync(directory, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
    }
});
