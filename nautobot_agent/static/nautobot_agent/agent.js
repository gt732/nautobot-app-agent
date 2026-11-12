(() => {
    "use strict";

    const root = document.querySelector("#agent-chat");
    const sendUrl = root.dataset.sendUrl;
    const stateUrl = root.dataset.stateUrl;
    const llmConfigured = root.dataset.llmConfigured === "true";
    const departureCookieName = root.dataset.departureCookieName;
    const departureUserId = root.dataset.departureUserId;
    const departureCookiePath = root.dataset.departureCookiePath;
    const serverNowMs = Number(root.dataset.serverNowMs);
    const clientNowAtLoad = Date.now();
    const csrf = root.querySelector("[name=csrfmiddlewaretoken]").value;
    const scroller = root.querySelector("#agent-scroller");
    const log = root.querySelector("#agent-log");
    const empty = root.querySelector("#agent-empty");
    const form = root.querySelector("#agent-form");
    const input = root.querySelector("#agent-input");
    const sendButton = root.querySelector("#agent-send");
    const newChatButton = root.querySelector("#agent-new-chat");
    const modelBadge = root.querySelector("#agent-model-name");
    const stopButton = root.querySelector("#agent-stop");
    const runStatusPanel = root.querySelector("#agent-run-status");
    const runStatusSpinner = root.querySelector("#agent-run-status-spinner");
    const runStatusText = root.querySelector("#agent-run-status-text");
    const runStatusDetails = root.querySelector("#agent-run-status-details");
    const runStatusErrorCode = root.querySelector("#agent-run-status-error-code");
    const runStatusExecutionError = root.querySelector("#agent-run-status-execution-error");
    const todosPanel = root.querySelector("#agent-todos-panel");
    const todosList = root.querySelector("#agent-todos-list");
    const state = JSON.parse(document.querySelector("#agent-state").textContent);
    let displayedConversation = state.conversation_id;
    let renderedConversation = null;
    let snapshotScrollStick = null;
    let runs;
    let pendingTextFrame = 0;
    let pendingTextElement = null;
    let nextMessage = true;
    let optimisticUserMessage = null;
    const contextIndicator = root.querySelector("#agent-context");
    const contextStatus = root.querySelector("#agent-context-status");
    const contextDisplay = root.querySelector("#agent-context-display");
    const contextMeter = root.querySelector("#agent-context-meter");
    const contextFill = root.querySelector("#agent-context-fill");
    const contextDetails = root.querySelector("#agent-context-details");
    let runStatusState = null;
    let runStatusNotice = "";

    function writeDepartureCookie(value, maxAge) {
        document.cookie = `${departureCookieName}=${value}; Path=${departureCookiePath}; SameSite=Lax; Max-Age=${maxAge}${window.location.protocol === "https:" ? "; Secure" : ""}`;
    }
    function clearDepartureCookie() {
        writeDepartureCookie("", 0);
    }
    function recordDeparture() {
        const departureMs = serverNowMs + (Date.now() - clientNowAtLoad);
        writeDepartureCookie(`${departureUserId}:${displayedConversation}:${departureMs}`, 31536000);
    }
    clearDepartureCookie();
    window.addEventListener("pagehide", recordDeparture);
    window.addEventListener("pageshow", clearDepartureCookie);

    function renderRunStatus(state, notice) {
        if (state !== undefined) {
            const previousRunId = runStatusState?.run?.run_id;
            const nextRunId = state?.run?.run_id;
            if (displayedConversation !== state?.conversation || previousRunId !== nextRunId) runStatusNotice = "";
            runStatusState = state;
        }
        if (notice) runStatusNotice = notice.text;

        const summary = runStatusState?.run;
        runStatusSpinner.hidden = summary?.status !== "queued" && summary?.status !== "running";
        if (!llmConfigured && summary?.error_code === "llm_not_configured") {
            runStatusPanel.hidden = true;
            return;
        }
        let text = "";
        let knownError = false;
        if (summary) {
            const labels = {
                queued: "Queued; waiting for a worker.",
                running: "Running.",
                completed: "Completed.",
                refused: "Request refused.",
                failed: "Run failed. Earlier checkpoints may contain partial progress.",
                dispatch_failed: "Run could not be dispatched.",
                interrupted: "Run was interrupted. Execution and external effects may remain uncertain; interruption does not roll back work, so verify external systems before retrying.",
                cancelled: "Run was safely cancelled.",
            };
            knownError = Boolean(summary.error_message);
            text = summary.error_message || labels[summary.status] || `Run status: ${summary.status}.`;
            if (summary.mcp_unavailable_servers?.length) {
                const names = summary.mcp_unavailable_servers.map((server) => server.name).join(", ");
                text += ` Unavailable integrations: ${names}. Built-in and other available tools remain usable.`;
            }
            if (summary.queued_warning) text += " It has remained queued longer than expected; execution has not started.";
            if (summary.external_outcome_unknown) text += " Warning: an external tool operation may have taken effect without a confirmed result; verify external systems before retrying.";
            if (summary.cleanup_incomplete) text += " Warning: integration cleanup could not be confirmed; local cleanup does not roll back external work.";
            if (summary.cancel_requested && !summary.terminal) text += " Cancellation was requested; completion is not yet confirmed.";
            if (summary.live_events_incomplete && !runStatusState.recoveryPending) text += " Live updates were incomplete; the durable conversation was refreshed.";
            if (summary.error_code && !knownError) text += ` Error: ${summary.error_code}.`;
            if (summary.execution_error && !knownError) text += ` ${summary.execution_error}`;
            if (runStatusState.observationNotice) text += ` ${runStatusState.observationNotice}`;
        }
        runStatusText.textContent = `${text}${runStatusNotice ? `${text ? " " : ""}${runStatusNotice}` : ""}`;
        runStatusPanel.classList.toggle("agent-run-status-error", knownError);
        runStatusErrorCode.textContent = summary?.error_code ? `Error: ${summary.error_code}` : "";
        runStatusExecutionError.textContent = knownError && summary.execution_error !== summary.error_message ? summary.execution_error || "" : "";
        runStatusDetails.hidden = !(knownError && (summary.error_code || runStatusExecutionError.textContent));
        runStatusPanel.hidden = !runStatusText.textContent;
    }

    function projectSelection(conversation, localAdmission = false) {
        const selected = conversation || "new";
        if (selected !== displayedConversation) {
            runStatusNotice = "";
            renderModelMetadata({});
        }
        if (localAdmission && displayedConversation === "new" && selected !== "new")
            renderedConversation = selected;
        displayedConversation = selected;
        if (new URLSearchParams(window.location.search).get("conversation") !== selected)
            history.replaceState(null, "", `?conversation=${encodeURIComponent(selected)}`);
    }
    function renderModelMetadata(snapshot) {
        const modelName = snapshot.model_name || root.dataset.defaultModel || "";
        modelBadge.textContent = modelName;
        modelBadge.hidden = !modelName;
    }
    // Context and message rendering.
    function renderContext(snapshot) {
        const { used, limit, percent } = snapshot;
        contextIndicator.hidden = used === null;
        contextMeter.hidden = used === null || limit === null;
        contextDisplay.dataset.level = "";
        contextDisplay.removeAttribute("role");
        contextDisplay.removeAttribute("aria-valuemin");
        contextDisplay.removeAttribute("aria-valuemax");
        contextDisplay.removeAttribute("aria-valuenow");
        contextDisplay.removeAttribute("aria-valuetext");
        if (used === null) {
            contextStatus.textContent = "";
            contextDetails.textContent = "";
            return;
        }
        const number = new Intl.NumberFormat().format;
        if (limit === null) {
            contextStatus.textContent = `Context ${number(used)} tokens · limit unknown`;
            contextDetails.textContent = `${number(used)} tokens used. Context limit unknown.`;
            return;
        }
        const level = percent >= 85 ? "danger" : percent >= 70 ? "warning" : "normal";
        contextDisplay.dataset.level = level;
        contextDisplay.setAttribute("role", "meter");
        contextDisplay.setAttribute("aria-valuemin", "0");
        contextDisplay.setAttribute("aria-valuemax", "100");
        contextDisplay.setAttribute("aria-valuenow", String(Math.min(100, Math.max(0, percent))));
        const shown = Number.isInteger(percent) ? String(percent) : percent.toFixed(1);
        contextStatus.textContent = `Context ${shown}%`;
        contextFill.style.width = `${Math.min(100, Math.max(0, percent))}%`;
        contextDetails.textContent = `${number(used)} / ${number(limit)} tokens (${shown}% used). Compaction typically near 85%.`;
        contextDisplay.setAttribute("aria-valuetext", `${shown}% (${number(used)} of ${number(limit)} tokens)`);
    }

    marked.setOptions({ gfm: true, breaks: true });
    DOMPurify.addHook("afterSanitizeAttributes", (node) => {
        const language = node.tagName === "CODE"
            ? node.getAttribute("class")?.match(/(?:^|\s)(language-[\w#+.-]+)(?=\s|$)/)?.[1]
            : null;
        if (language) node.setAttribute("class", language);
        else node.removeAttribute("class");
        if (node.tagName === "A") {
            node.setAttribute("target", "_blank");
            node.setAttribute("rel", "noopener noreferrer");
        }
    });

    function wrapCodeBlock(pre, label) {
        const block = document.createElement("div");
        block.className = "agent-code-block";
        block.innerHTML = '<div class="agent-code-head"><span></span><button type="button" class="btn btn-sm agent-icon-button agent-copy-code"><i class="mdi mdi-content-copy" aria-hidden="true"></i> Copy</button></div>';
        block.querySelector("span").textContent = label;
        pre.replaceWith(block);
        block.append(pre);
        return block;
    }

    function renderMarkdown(element, text, renderDiagrams = true) {
        element.dataset.raw = text;
        element.innerHTML = DOMPurify.sanitize(marked.parse(text), {
            USE_PROFILES: { html: true },
            ALLOW_DATA_ATTR: false,
            FORBID_TAGS: ["img", "picture", "source", "video", "audio", "style", "form", "input", "button", "textarea", "select", "dialog"],
            FORBID_ATTR: ["style", "background", "id", "name", "role", "hidden", "popover", "open"],
        });
        element.querySelectorAll("pre > code").forEach((code) => {
            const language = code.className.match(/(?:^|\s)language-(\S+)/)?.[1];
            const source = code.textContent;
            if (!language || hljs.getLanguage(language)) {
                hljs.highlightElement(code);
            }

            const block = wrapCodeBlock(code.parentElement, language || "text");
            if (renderDiagrams && language === "mermaid") mermaidRenderer.enhance(block, source);
        });
    }

    const nearBottom = () => snapshotScrollStick ?? scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
    const scrollToBottom = () => { scroller.scrollTop = scroller.scrollHeight; };
    const mermaidRenderer = window.NautobotAgentMermaid.createRenderer(
        log, root.dataset.mermaidUrl, nearBottom, scrollToBottom,
    );

    function addUserMessage(text) {
        empty.hidden = true;
        const message = document.createElement("div");
        message.className = "agent-message agent-message-user";
        const bubble = document.createElement("div");
        bubble.className = "agent-bubble";
        bubble.textContent = text;
        message.append(bubble);
        log.append(message);
        return message;
    }

    function addAssistantMessage() {
        empty.hidden = true;
        const message = document.createElement("div");
        message.className = "agent-message agent-message-assistant";
        message.innerHTML = '<div class="agent-avatar"><i class="mdi mdi-robot-happy-outline" aria-hidden="true"></i></div><div class="agent-message-body"><div class="agent-parts"></div><div class="agent-message-actions"><button type="button" class="btn btn-sm agent-icon-button agent-copy-message" title="Copy response" aria-label="Copy response"><i class="mdi mdi-content-copy" aria-hidden="true"></i></button></div></div>';
        log.append(message);
        return message;
    }

    function addTextPart(parts, text = "") {
        const element = document.createElement("div");
        element.className = "agent-markdown";
        parts.append(element);
        if (text) renderMarkdown(element, text);
        return element;
    }

    const toolStatuses = {
        running: "Running",
        completed: "Completed",
        failed: "Failed",
        unknown: "Outcome unknown",
    };
    // The payload ID is an opaque display ID, not the provider's native call ID.
    const toolEntriesByDisplayId = new Map();

    function toolArguments(args) {
        if (typeof args === "string") return args;
        if (args === null) return "No arguments captured.";
        return JSON.stringify(args, null, 2);
    }

    function setToolStatus(entry, status, chart) {
        const stick = nearBottom();
        entry.element.dataset.status = status;
        entry.label.textContent = toolStatuses[status] ?? toolStatuses.unknown;
        if (stick) scrollToBottom();
        if (status === "completed" && entry.name === "render_chart" && chart) scheduleToolChart(entry, chart);
    }

    let chartId = 0;

    function scheduleToolChart(entry, config) {
        if (entry.chartScheduled) return;
        entry.chartScheduled = true;
        const stick = nearBottom();
        const render = () => {
            if (toolEntriesByDisplayId.get(entry.displayId) !== entry || !entry.element.isConnected) return;
            if (!window.echarts || !window.nb?.echarts) return;
            entry.element.open = true;
            const id = `agent-chart-${++chartId}`;
            const container = document.createElement("div");
            container.id = id;
            container.className = "agent-chart";
            container.setAttribute("role", "img");
            container.setAttribute("aria-label", config.title.text);
            const configScript = document.createElement("script");
            configScript.type = "application/json";
            configScript.id = `echarts-config-${id}`;
            configScript.textContent = JSON.stringify(config);
            const argsDetails = document.createElement("details");
            argsDetails.className = "agent-chart-args";
            const argsSummary = document.createElement("summary");
            argsSummary.textContent = "Chart arguments";
            argsDetails.append(argsSummary, entry.argsElement);
            entry.element.insertBefore(container, entry.summary.nextSibling);
            container.after(configScript, argsDetails);

            const chart = window.echarts.init(container, null, { renderer: "canvas" });
            entry.chartInstance = chart;
            const theme = document.documentElement.dataset.bsTheme || "light";
            chart.setOption({
                ...config,
                ...window.nb.echarts.getEchartsOptionsThemeOverrides(config, theme),
            });
            if (typeof ResizeObserver === "function") {
                entry.resizeObserver = new ResizeObserver(() => chart.resize());
                entry.resizeObserver.observe(container);
            }
            if (stick) scrollToBottom();
        };
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", () => setTimeout(render, 0), { once: true });
        } else {
            render();
        }
    }

    function upsertTool({ id: displayId, name, args, status, chart }) {
        const existing = toolEntriesByDisplayId.get(displayId);
        if (existing) {
            setToolStatus(existing, status, chart);
            return existing;
        }
        const stick = nearBottom();
        empty.hidden = true;
        const element = document.createElement("details");
        element.className = "agent-tool";
        const summary = document.createElement("summary");
        const toolName = document.createElement("span");
        toolName.className = "agent-tool-name";
        toolName.textContent = name;
        const label = document.createElement("span");
        label.className = "agent-tool-status";
        summary.append(toolName, label);
        const evalCode = name === "eval_python" && args !== null && typeof args === "object" && !Array.isArray(args) && typeof args.code === "string" ? args.code : null;
        let argsElement = null;
        if (evalCode !== null) {
            element.className = "agent-tool agent-tool-eval";
            element.open = true;
            const pre = document.createElement("pre");
            const code = document.createElement("code");
            code.className = "language-python";
            code.textContent = evalCode;
            // Preserve CRLF before highlighting; a JS property cannot be supplied by sanitized Markdown attributes.
            code.agentSource = evalCode;
            hljs.highlightElement(code);
            pre.append(code);
            element.append(summary, wrapCodeBlock(pre, "Python"));
        } else {
            argsElement = document.createElement("pre");
            argsElement.className = "agent-tool-args";
            argsElement.textContent = toolArguments(args);
            element.append(summary, argsElement);
        }
        const entry = { displayId, name, argsElement, element, summary, label };
        toolEntriesByDisplayId.set(displayId, entry);
        log.append(element);
        setToolStatus(entry, status, chart);
        if (stick) scrollToBottom();
        return entry;
    }

    function renderHistory(history) {
        for (const entry of history) {
            if (entry.role === "user") {
                addUserMessage(entry.content);
            } else if (entry.role === "assistant") {
                const parts = addAssistantMessage().querySelector(".agent-parts");
                addTextPart(parts, entry.content);
            } else if (entry.role === "tool") {
                upsertTool(entry);
            }
        }
    }


    const todoStatuses = {
        pending: ["Pending", "agent-todo-pending", "mdi-checkbox-blank-circle-outline"],
        in_progress: ["In progress", "agent-todo-in-progress", "mdi-progress-clock"],
        completed: ["Completed", "agent-todo-completed", "mdi-check-circle"],
    };

    function renderTodos(todos) {
        const stick = nearBottom();
        const rows = todos.map(({ content, status }) => {
            const [label, className, icon] = todoStatuses[status];
            const row = document.createElement("li");
            row.className = `agent-todo ${className}`;
            const mark = document.createElement("i");
            mark.className = `mdi ${icon}`;
            mark.setAttribute("aria-hidden", "true");
            const text = document.createElement("span");
            text.className = "agent-todo-content";
            text.textContent = content;
            const state = document.createElement("span");
            state.className = "agent-todo-status";
            state.textContent = label;
            row.append(mark, text, state);
            return row;
        });
        todosList.replaceChildren(...rows);
        todosPanel.hidden = rows.length === 0;
        if (stick) scrollToBottom();
    }

    function updateSendState() {
        const summary = runs.state.run;
        input.disabled = !llmConfigured;
        sendButton.disabled = !llmConfigured || !runs.canSubmit || !input.value.trim();
        stopButton.hidden = !summary?.cancellable;
    }

    runs = window.NautobotAgentRuns.create({
        sendUrl, stateUrl, csrf, isHidden: () => document.hidden,
        notify(notification) {
            if (notification.type === "state") {
                const state = notification.state;
                projectSelection(state.conversation, notification.localAdmission);
                updateSendState();
                if (notification.renderStatus) renderRunStatus(state);
            } else if (notification.type === "event") applyEvent(notification.event, notification.data);
            else if (notification.type === "snapshot") {
                applySnapshot(notification.snapshot, notification.rebuildHistory);
            } else if (notification.type === "notice") {
                if (notification.text) {
                    renderRunStatus(undefined, notification);
                }
                if (notification.restoreDraft !== undefined) {
                    if (optimisticUserMessage?.isConnected) optimisticUserMessage.remove();
                    optimisticUserMessage = null;
                    empty.hidden = !llmConfigured || log.children.length > 0;
                    if (input.value === "") {
                        input.value = notification.restoreDraft;
                        autoGrow();
                    }
                }
            }
        },
    });
    function applyEvent(event, data) {
        const finishText = () => {
            const message = log.lastElementChild;
            const text = message?.querySelector(".agent-parts")?.lastElementChild;
            if (text?.classList.contains("agent-streaming")) {
                if (pendingTextFrame) cancelAnimationFrame(pendingTextFrame);
                pendingTextFrame = 0;
                pendingTextElement = null;
                renderMarkdown(text, text.dataset.raw || "", true);
                text.classList.remove("agent-streaming");
                message.classList.remove("agent-pending");
            }
        };
        if (event === "token") {
            let message = log.lastElementChild;
            if (nextMessage || !message?.classList.contains("agent-message-assistant")) message = addAssistantMessage();
            nextMessage = false;
            message.classList.add("agent-pending");
            const parts = message.querySelector(".agent-parts");
            let element = parts.lastElementChild;
            if (!element?.classList.contains("agent-streaming")) {
                element = addTextPart(parts);
                element.classList.add("agent-streaming");
            }
            element.dataset.raw = (element.dataset.raw || "") + data.text;
            pendingTextElement = element;
            pendingTextFrame ||= requestAnimationFrame(() => {
                pendingTextFrame = 0;
                const target = pendingTextElement;
                pendingTextElement = null;
                if (target?.isConnected) {
                    const stick = nearBottom();
                    renderMarkdown(target, target.dataset.raw || "", false);
                    if (stick) scrollToBottom();
                }
            });
        } else if (event === "message_start") {
            finishText();
            nextMessage = true;
            const message = log.lastElementChild;
            if (message?.classList.contains("agent-message-assistant")) message.classList.remove("agent-pending");
        } else if (event === "tool_call") {
            finishText();
            nextMessage = true;
            upsertTool(data);
        } else if (event === "todos") {
            renderTodos(data.todos);
        } else if (event === "context") {
            renderContext(data);
        } else if (event === "done") {
            finishText();
        }
    }

    function disposeRendering() {
        optimisticUserMessage = null;
        renderedConversation = null;
        if (pendingTextFrame) cancelAnimationFrame(pendingTextFrame);
        pendingTextFrame = 0;
        pendingTextElement = null;
        nextMessage = true;
        for (const entry of toolEntriesByDisplayId.values()) {
            entry.resizeObserver?.disconnect();
            entry.chartInstance?.dispose();
        }
        toolEntriesByDisplayId.clear();
        log.replaceChildren();
        empty.hidden = !llmConfigured;
        renderTodos([]);
    }

    function applySnapshot(snapshot, rebuildHistory) {
        const stick = nearBottom();
        const previousTop = scroller.scrollTop;
        const conversation = snapshot.conversation_id;
        const freshConversation = conversation !== renderedConversation;
        snapshotScrollStick = freshConversation || stick;
        try {
            const visibleUserTurns = log.querySelectorAll(".agent-message-user").length;
            const savedUserTurns = snapshot.history.filter((item) => item.role === "user").length;
            if (rebuildHistory || visibleUserTurns !== savedUserTurns) {
                disposeRendering();
                renderHistory(snapshot.history);
            }
            renderModelMetadata(snapshot);
            renderTodos(snapshot.todos);
            renderContext(snapshot.context_usage);
            empty.hidden = !llmConfigured || log.children.length > 0;
            optimisticUserMessage = null;
            renderedConversation = conversation;
            if (snapshotScrollStick) scrollToBottom();
            else {
                const maximum = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
                scroller.scrollTop = Math.max(0, Math.min(previousTop, maximum));
            }
        } finally {
            snapshotScrollStick = null;
        }
    }

    form.addEventListener("submit", (event) => {
        event.preventDefault();
        const text = input.value.trim();
        if (!text) return;
        if (!llmConfigured || !runs.canSubmit) return;
        const stick = nearBottom();
        input.value = "";
        autoGrow();
        optimisticUserMessage = addUserMessage(text);
        if (stick) scrollToBottom();
        runs.submit(text);
    });

    stopButton.addEventListener("click", () => runs.cancel());

    input.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
            event.preventDefault();
            form.requestSubmit();
        }
    });

    function autoGrow() {
        input.style.height = "auto";
        input.style.height = `${input.scrollHeight}px`;
        updateSendState();
    }
    input.addEventListener("input", autoGrow);

    async function copyWithFeedback(button, text) {
        const icon = button.querySelector("i");
        icon.className = "mdi mdi-content-copy";
        const copied = await window.NautobotAgentClipboard.copyText(text, { input });
        button.title = copied
            ? (button.classList.contains("agent-copy-code") ? "Copy code" : "Copy response")
            : "Copy failed. Select the text and copy it manually.";
        button.setAttribute("aria-label", button.title);
        if (!copied) {
            icon.className = "mdi mdi-alert-circle-outline";
            return;
        }
        icon.className = "mdi mdi-check";
        setTimeout(() => { icon.className = "mdi mdi-content-copy"; }, 1500);
    }

    log.addEventListener("click", (event) => {
        const codeButton = event.target.closest(".agent-copy-code");
        if (codeButton) {
            const code = codeButton.closest(".agent-code-block").querySelector("code");
            copyWithFeedback(codeButton, code.agentSource ?? code.textContent);
            return;
        }
        const messageButton = event.target.closest(".agent-copy-message");
        if (messageButton) {
            const blocks = messageButton.closest(".agent-message").querySelectorAll(".agent-markdown");
            copyWithFeedback(messageButton, [...blocks].map((element) => element.dataset.raw ?? "").join("\n\n"));
        }
    });

    newChatButton.addEventListener("click", () => {
        runs.newConversation();
        disposeRendering();
        renderedConversation = "new";
        renderContext({ used: null, limit: null, percent: null });
        renderRunStatus(null);
        input.focus();
    });
    window.addEventListener("popstate", () => window.location.reload());

    projectSelection(state.conversation_id);
    applySnapshot(state, true);
    runs.initialize({ conversation: displayedConversation, activeRun: state.active_run, latestRun: state.latest_run });
})();
