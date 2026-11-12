(() => {
    "use strict";

    // create({sendUrl, stateUrl, csrf, isHidden, notify}) exposes initialize({conversation, activeRun, latestRun}),
    // newConversation(), submit(trimmedPrompt), cancel(), and read-only canSubmit.
    // canSubmit checks run/pending admission and terminal draining; the caller owns prompt validation. Notifications are synchronous:
    // state(state, renderStatus, localAdmission), event(name, data), snapshot(snapshot, rebuildHistory),
    // and notice(text, restoreDraft). localAdmission transfers a first submission's transcript identity.
    // snapshot(snapshot, rebuildHistory) is explicit: true reconciles durable history; false retains streamed DOM.
    // state is detached/read-only; the module alone owns selection, run identity, requests, cursor and recovery.
    const terminal = (run) => !!run?.terminal;
    const recoverableDraft = (run) => run.status === "dispatch_failed"
        || (run.status === "cancelled" && run.execution_started === false);
    const copy = (run) => (run ? Object.freeze({ ...run }) : null);
    const errorOf = (message, retryable = false) =>
        Object.assign(new Error(message), { retryable });
    const storageKey = "nautobot-agent-pending-requests";
    const pendingRequestTtl = 5 * 60 * 1000;

    function requestId() {
        if (crypto.randomUUID) return crypto.randomUUID();
        const bytes = crypto.getRandomValues(new Uint8Array(16));
        bytes[6] = (bytes[6] & 0x0f) | 0x40;
        bytes[8] = (bytes[8] & 0x3f) | 0x80;
        const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
        return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
    }

    function list() {
        try {
            const value = JSON.parse(sessionStorage.getItem(storageKey) || "[]");
            return Array.isArray(value) ? value.filter((item) => item && typeof item.request_id === "string") : [];
        } catch {
            return [];
        }
    }

    function saveAll(items) {
        if (items.length) sessionStorage.setItem(storageKey, JSON.stringify(items));
        else sessionStorage.removeItem(storageKey);
    }

    function save(identity) {
        const items = list().filter((item) => item.request_id !== identity.request_id);
        items.push(identity);
        saveAll(items);
    }

    function remove(requestId) {
        saveAll(list().filter((item) => item.request_id !== requestId));
    }

    function detach() {
        saveAll(list().map((item) => ({ ...item, detached: true })));
    }

    function createPending() {
        let selected = null;

        return {
            get current() {
                return selected;
            },
            select(conversation) {
                selected = list().find((item) => !item.detached && item.scope === conversation) || null;
                selected = copy(selected);
            },
            begin(identity) {
                const record = copy(identity);
                save(record);
                selected = record;
            },
            resolveRequest(requestId) {
                remove(requestId);
                if (selected?.request_id === requestId) selected = null;
            },
            detach() {
                detach();
                selected = null;
            },
            list() {
                return list().map(copy);
            },
        };
    }

    function create({ sendUrl, stateUrl, csrf, isHidden, notify }) {
        const pending = createPending();
        let selected = "new",
            run = null,
            generation = 0,
            cursor = "0-0",
            statusOnly = false,
            draining = false;
        let notice = "",
            recovery = false;
        let queuedDraft = null;
        const requests = new Set();
        const state = () =>
            Object.freeze({
                conversation: selected,
                run: copy(run),
                observationNotice: notice,
                recoveryPending: recovery,
            });
        function publish(renderStatus = true, localAdmission = false) {
            notify({ type: "state", state: state(), renderStatus, localAdmission });
        }
        function noticeText(text, restoreDraft = null) {
            notify({ type: "notice", text, ...(restoreDraft === null ? {} : { restoreDraft }) });
        }
        function identityNotice(identity, text) {
            if (!identity.detached) noticeText(text);
        }
        const current = (token, id) => token === generation && (!id || run?.run_id === id);
        function merge(previous, next) {
            const value = { ...previous, ...next };
            if (terminal(previous) && !terminal(next))
                for (const key of ["status", "terminal", "cancellable"]) value[key] = previous[key];
            if (previous.cancel_requested && !terminal(next)) {
                value.cancel_requested = true;
                value.cancellable = false;
            }
            for (const key of ["external_outcome_unknown", "cleanup_incomplete"]) if (previous[key]) value[key] = true;
            return value;
        }
        function observe(summary) {
            const previous = run;
            run = merge(run, summary);
            if (queuedDraft?.run_id !== run.run_id) return;
            if (previous.status === "queued" && terminal(run) && recoverableDraft(run))
                noticeText("", queuedDraft.text);
            if (run.status !== "queued") queuedDraft = null;
        }
        function abortRequests() {
            for (const controller of requests) controller.abort();
            requests.clear();
        }
        async function http(url, options = {}) {
            const controller = new AbortController();
            requests.add(controller);
            try {
                return await fetch(url, { ...options, signal: controller.signal });
            } finally {
                requests.delete(controller);
            }
        }
        async function json(url, options = {}) {
            let response;
            try {
                response = await http(url, { ...options, redirect: "manual" });
            } catch (error) {
                if (error?.name === "AbortError") throw error;
                throw errorOf("Network request failed.", true);
            }
            if (response.type === "opaqueredirect")
                throw errorOf("The session or access may have changed. Sign in and reload to resume updates.");
            if (response.status === 429 || response.status >= 500)
                throw errorOf(`Polling service unavailable (${response.status}).`, true);
            if (!response.ok)
                throw errorOf(
                    response.status === 401 ||
                        response.status === 403 ||
                        (response.status >= 300 && response.status < 400)
                        ? "The session or access may have changed. Sign in and reload to resume updates."
                        : `The server rejected the request (${response.status}). Reload to resume updates.`,
                );
            let body;
            try {
                body = await response.text();
            } catch (error) {
                if (error?.name === "AbortError") throw error;
                throw errorOf("The response body could not be downloaded.", true);
            }
            try {
                return JSON.parse(body);
            } catch {
                throw errorOf("The server returned an invalid response. Reload to resume updates.");
            }
        }
        const stateUrlFor = (conversation) => `${stateUrl}?conversation=${encodeURIComponent(conversation)}`;
        async function durable(token, expected, preserveHistory = false) {
            const snapshot = await json(stateUrlFor(selected), { cache: "no-store" });
            if (!current(token, expected)) return null;
            const mayPreserve = preserveHistory
                && snapshot.conversation_id === selected
                && !snapshot.active_run
                && snapshot.latest_run?.run_id === expected
                && snapshot.latest_run.status === "completed"
                && snapshot.latest_run.terminal
                && !snapshot.latest_run.live_events_incomplete;
            if (snapshot.conversation_id) selected = snapshot.conversation_id;
            notice = "";
            recovery = false;
            draining = false;
            if (snapshot.active_run) {
                if (snapshot.active_run.run_id === run?.run_id) observe(snapshot.active_run);
                else run = { ...snapshot.active_run };
                statusOnly = true;
            } else {
                if (snapshot.latest_run?.run_id === run?.run_id && run) observe(snapshot.latest_run);
                else run = snapshot.latest_run ? { ...snapshot.latest_run } : (expected ? run : null);
            }
            publish();
            notify({ type: "snapshot", snapshot, rebuildHistory: !mayPreserve });
            return snapshot.active_run || null;
        }
        async function poll(token, restored = false) {
            let drain = false,
                failures = 0,
                emptyPages = 0,
                processedDone = false,
                observedError = false,
                uninterrupted = !restored;
            statusOnly = restored;
            const cadence = () => (isHidden() ? 5000 : statusOnly || run?.status === "queued" || emptyPages >= 4 ? 2000 : 500);
            while (token === generation && run) {
                const expected = run.run_id;
                let retry = null;
                try {
                    if (recovery) {
                        const active = await durable(token, expected);
                        if (!current(token)) return;
                        if (!active || terminal(run)) return;
                    } else if (terminal(run) && !drain) {
                        const active = await durable(token, expected);
                        if (!current(token)) return;
                        if (!active || terminal(active)) return;
                        continue;
                    } else if (!statusOnly) {
                        const page = await json(`${run.events_url}?after=${encodeURIComponent(cursor)}`, {
                            cache: "no-store",
                        });
                        if (!current(token, expected)) return;
                        const { events, next_cursor, has_more, resync_required, ...summary } = page;
                        emptyPages = events.length ? 0 : emptyPages + 1;
                        observe(summary);
                        if (resync_required) {
                            uninterrupted = false;
                            statusOnly = true;
                            recovery = true;
                            drain = false;
                            continue;
                        }
                        for (const item of events) {
                            if (item.event === "display_omitted") {
                                uninterrupted = false;
                                continue;
                            }
                            if (item.event === "error") {
                                run.execution_error = item.data.error || "The run reported an error.";
                                observedError = true;
                            }
                            notify({ type: "event", event: item.event, data: item.data });
                            if (item.event === "done") processedDone = true;
                        }
                        cursor = next_cursor;
                        drain = !!has_more;
                        draining ||= terminal(run) && drain;
                    } else {
                        uninterrupted = false;
                        const summary = await json(`${run.events_url}?status_only=1`, { cache: "no-store" });
                        if (!current(token, expected)) return;
                        observe(summary);
                    }
                    failures = 0;
                    notice = "";
                    if (terminal(run)) draining = true;
                    publish();
                    if (terminal(run)) {
                        if (drain && !statusOnly) {
                            await new Promise((resolve) => setTimeout(resolve, 0));
                            continue;
                        }
                        const preserve = uninterrupted && processedDone && !observedError && run.status === "completed";
                        if (await durable(token, run.run_id, preserve)) continue;
                        return;
                    }
                } catch (error) {
                    uninterrupted = false;
                    if (!current(token, expected)) return;
                    if (terminal(run) && !error?.retryable) {
                        draining = false;
                        publish();
                        noticeText(
                            ` Saved conversation could not be refreshed. Reload to retry. ${error.message}`,
                        );
                        return;
                    }
                    if (error?.name === "AbortError") return;
                    if (!terminal(run)) drain = false;
                    if (error?.retryable) {
                        draining ||= terminal(run);
                        failures += 1;
                        retry = Math.max(Math.min(1000 * 2 ** (failures - 1), 30000), cadence());
                        notice = `Live status is unavailable; retrying in ${Math.ceil(retry / 1000)} seconds.`;
                    } else {
                        notice = `Updates have stopped; the run may still be executing. ${error.message}`;
                        publish();
                        return;
                    }
                    publish();
                }
                await new Promise((resolve) => setTimeout(resolve, retry ?? (drain ? 0 : cadence())));
            }
        }
        function adopt(summary, restored = false) {
            generation += 1;
            const token = generation;
            run = { ...summary };
            cursor = "0-0";
            notice = "";
            recovery = false;
            draining = false;
            publish();
            poll(token, restored || terminal(summary));
        }
        function newConversation() {
            generation += 1;
            abortRequests();
            run = null;
            selected = "new";
            notice = "";
            recovery = false;
            draining = false;
            queuedDraft = null;
            pending.detach();
            publish();
        }
        async function uncertain(identity, token) {
            try {
                const result = await json(`${stateUrl}?client_request=${encodeURIComponent(identity.request_id)}`, {
                    cache: "no-store",
                });
                if (!current(token)) return;
                if (!result.run_id || !result.conversation_id) {
                    if (result.run_id === null
                        && Date.now() - identity.created_at >= pendingRequestTtl) {
                        pending.resolveRequest(identity.request_id);
                        publish(false);
                        identityNotice(identity, "Your previous request was not found. You can send a message again.");
                        return;
                    }
                    identityNotice(
                        identity,
                        "We couldn't confirm whether your previous message was sent. It won't be sent again automatically. Reload to check again, or select New chat to start a separate conversation.",
                    );
                    return;
                }
                pending.resolveRequest(identity.request_id);
                if (identity.detached || identity.scope !== selected) return;
                selected = result.conversation_id;
                run = { ...result };
                publish(false);
                let snapshot;
                try {
                    snapshot = await json(stateUrlFor(result.conversation_id), { cache: "no-store" });
                } catch (error) {
                    if (current(token)) {
                        adopt(run, true);
                        noticeText(`The submission outcome is resolved; refreshing its run state failed. ${error.message}`);
                    }
                    return;
                }
                if (!snapshot || !current(token)) return;
                const summary = snapshot.active_run || snapshot.latest_run || result;
                const restored = summary.run_id === run.run_id ? merge(run, summary) : summary;
                if (snapshot.conversation_id) selected = snapshot.conversation_id;
                if (terminal(restored)) {
                    run = { ...restored };
                    publish();
                    notify({ type: "snapshot", snapshot, rebuildHistory: true });
                    return;
                }
                adopt(restored, true);
                notify({ type: "snapshot", snapshot, rebuildHistory: true });
            } catch (error) {
                if (current(token))
                    identityNotice(
                        identity,
                        `We couldn't check whether your previous message was sent. It won't be sent again automatically. Reload to check again, or select New chat to start a separate conversation. ${error.message}`,
                    );
            }
        }
        async function lookupOthers(token) {
            for (const identity of pending.list()) {
                if (!current(token)) return;
                const belongs = !identity.detached && identity.scope === selected;
                if (belongs) continue;
                await uncertain({ ...identity, detached: true }, token);
            }
        }
        function restorePending(token) {
            pending.select(selected);
            publish(false);
            if (pending.current) {
                uncertain(pending.current, token);
                return true;
            }
            return false;
        }
        const canSubmit = () => (!run || terminal(run)) && !pending.current && !draining;
        async function submit(text) {
            if (!canSubmit()) return;
            const token = ++generation,
                id = requestId();
            queuedDraft = null;
            const identity = { request_id: id, scope: selected, detached: false, created_at: Date.now() };
            pending.begin(identity);
            publish(false);
            let resolved = false;
            try {
                const response = await http(sendUrl, {
                    method: "POST",
                    headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
                    body: JSON.stringify({
                        message: text,
                        conversation_id: selected === "new" ? null : selected,
                        client_request_id: id,
                    }),
                });
                const data = await response.json().catch(() => ({}));
                if (!current(token)) return;
                if (([400, 401, 403, 404].includes(response.status) || response.redirected
                    || (response.status === 409 && data.code === "user_run_limit")) && !data.run_id) {
                    resolved = true;
                    pending.resolveRequest(id);
                    publish(false);
                    noticeText(response.redirected
                        ? "The session or access may have changed. Sign in and reload before sending again."
                        : data.error || `Request rejected (${response.status}).`, text);
                    return;
                }
                if (!response.ok && response.status !== 503 && response.status !== 409)
                    throw new Error(data.error || `Submission failed (${response.status} ${response.statusText}).`);
                if (data.run_id) {
                    pending.resolveRequest(id);
                    const canonicalizedLocalAdmission = selected === "new";
                    selected = data.conversation_id;
                    publish(false, canonicalizedLocalAdmission);
                    resolved = true;
                    if (data.status === "queued") queuedDraft = { run_id: data.run_id, text };
                    adopt(data);
                    if (terminal(data) && recoverableDraft(data)) noticeText("", text);
                    return;
                }
                if (response.status === 409 && data.blocking_run) {
                    resolved = true;
                    pending.resolveRequest(id);
                    selected = data.blocking_run.conversation_id;
                    publish(false);
                    let snapshot = null;
                    try {
                        snapshot = await json(stateUrlFor(selected), { cache: "no-store" });
                    } catch {
                        // The blocker remains authoritative when its durable snapshot is unavailable.
                    }
                    if (!current(token)) return;
                    adopt(data.blocking_run, true);
                    if (snapshot) notify({ type: "snapshot", snapshot, rebuildHistory: true });
                    noticeText("", text);
                    return;
                }
                throw new Error(data.error || "Submission outcome is unresolved; reload to check its identity.");
            } catch (error) {
                if (!current(token)) return;
                if (resolved) {
                    noticeText(`The submission outcome is resolved; refreshing its run state failed. ${error.message}`);
                    return;
                }
                noticeText(
                    `Submission outcome is uncertain. Request ${id} is retained; it will not be resubmitted automatically. ${error.message}`,
                );
            }
        }
        async function cancel() {
            if (!run?.cancel_url || run.cancel_requested) return;
            const expected = run.run_id,
                token = generation;
            try {
                const response = await http(run.cancel_url, { method: "POST", headers: { "X-CSRFToken": csrf } });
                const result = await response.json().catch(() => ({}));
                if (!current(token, expected)) return;
                if (!response.ok) throw new Error(result.error || `Cancellation failed (${response.status}).`);
                observe(result);
                if (terminal(run)) {
                    notice = "";
                }
                publish();
            } catch (error) {
                if (current(token, expected))
                    noticeText(` Cancellation request could not be confirmed. ${error.message}`);
            }
        }
        function initialize({ conversation, activeRun, latestRun }) {
            selected = conversation || "new";
            const summary = activeRun || latestRun;
            run = summary ? { ...summary } : null;
            const token = generation;
            if (!restorePending(token)) {
                if (run && !terminal(run)) {
                    publish();
                    poll(token, true);
                } else {
                    publish();
                }
                lookupOthers(token);
            }
        }
        return Object.freeze({
            initialize,
            newConversation,
            submit,
            cancel,
            get canSubmit() {
                return canSubmit();
            },
            get state() {
                return state();
            },
        });
    }
    window.NautobotAgentRuns = Object.freeze({ create });
})();
