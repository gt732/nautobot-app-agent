(() => {
    "use strict";

    const root = () => document.getElementById("agent-trace");
    let lastFocus = null;

    function resetRequestState(current) {
        current?.setAttribute("aria-busy", "false");
        current?.querySelector("#trace-detail")?.setAttribute("aria-busy", "false");
    }

    function message(text, error = false) {
        const live = root()?.querySelector(".trace-request-state");
        if (!live) return;
        live.hidden = !text;
        live.classList.toggle("alert", Boolean(text));
        live.classList.toggle("alert-danger", Boolean(text && error));
        live.classList.toggle("alert-info", Boolean(text && !error));
        live.textContent = text;
    }

    function enhanceTabs(scope = root()) {
        if (scope && window.bootstrap?.Tab) scope.classList.add("js-tabs");
    }

    function updateAncestors(runId) {
        const current = root();
        if (!current) return;
        current.querySelectorAll(".trace-tree-item").forEach((item) => {
            const selected = item.dataset.runId === runId;
            item.classList.toggle("is-selected", selected);
            const link = item.querySelector(":scope > .trace-node .trace-node-link");
            if (link) {
                if (selected) link.setAttribute("aria-current", "true");
                else link.removeAttribute("aria-current");
            }
        });
        const selected = [...current.querySelectorAll(".trace-tree-item")].find((item) => item.dataset.runId === runId);
        for (let item = selected?.parentElement; item && current.contains(item); item = item.parentElement) {
            if (item.matches(".trace-disclosure")) item.open = true;
        }
    }

    function copyText(text, button, status) {
        window.NautobotAgentClipboard.copyText(text).then((copied) => {
            if (!copied) throw new Error("Clipboard unavailable");
            status.textContent = "Copied.";
            status.setAttribute("role", "status");
        }).catch(() => {
            status.textContent = "Copy failed. Select the displayed text and copy it manually.";
            status.setAttribute("role", "status");
        });
        button?.focus({ preventScroll: true });
    }

    function copyPayload(button) {
        const section = button.closest(".trace-payload");
        const status = section?.querySelector(".trace-copy-status");
        if (!section || !status) return;
        const target = section.querySelector(".trace-payload-json");
        copyText(target?.textContent ?? "", button, status);
    }

    document.addEventListener("click", (event) => {
        const copy = event.target.closest?.("[data-copy-payload]");
        if (copy) {
            copyPayload(copy);
            return;
        }
        const link = event.target.closest?.("[data-trace-selection]");
        if (link && root()?.contains(link)) {
            lastFocus = link.dataset.traceSelection;
            message("");
        }
    });


    document.addEventListener("htmx:beforeRequest", (event) => {
        if (!root()?.contains(event.detail.elt)) return;
        root().setAttribute("aria-busy", "true");
        const detail = root().querySelector("#trace-detail");
        if (detail) detail.setAttribute("aria-busy", "true");
        message("Loading trace details…");
    });


    document.addEventListener("htmx:afterSwap", (event) => {
        const current = root();
        if (!current?.contains(event.detail.elt)) return;
        resetRequestState(current);
        enhanceTabs(current);
        message("");

        const detail = current.querySelector("#trace-detail");
        const selectedRun = detail?.dataset.selectedRun;
        if (detail && lastFocus !== null) {
            updateAncestors(selectedRun || "");
            const focusTarget = selectedRun
                ? current.querySelector(`.trace-tree-item[data-run-id="${CSS.escape(selectedRun)}"] .trace-node-link`)
                : current.querySelector('.trace-tree-item[data-run-id=""] .trace-node-link');
            (focusTarget || detail.querySelector("h2"))?.focus?.({ preventScroll: true });
            lastFocus = null;
        }

        const canonical = new URL(window.location.href);
        canonical.search = selectedRun ? `?span=${encodeURIComponent(selectedRun)}` : "";
        history.replaceState(history.state, "", canonical);
        const refresh = current.querySelector("[data-trace-refresh]");
        if (refresh) refresh.href = canonical.href;
    });

    document.addEventListener("htmx:responseError", (event) => {
        const current = root();
        if (!current?.contains(event.detail.elt)) return;
        resetRequestState(current);
        message("The trace could not be refreshed. Your current details remain visible. Retry or use Back to Traces.", true);
    });

    document.addEventListener("htmx:sendError", (event) => {
        const current = root();
        if (!current?.contains(event.detail.elt)) return;
        resetRequestState(current);
        message("The trace request failed. Check the connection and retry.", true);
    });
    document.addEventListener("htmx:timeout", (event) => {
        const current = root();
        if (!current?.contains(event.detail.elt)) return;
        resetRequestState(current);
        message("The trace request timed out. Retry or use Back to Traces.", true);
    });

    // Selection uses replaceState, but returning from a different document must
    // never leave details associated with a different address.
    window.addEventListener("popstate", () => {
        if (root()) window.location.reload();
    });



    enhanceTabs();
})();
