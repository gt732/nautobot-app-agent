"use strict";

const runId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const conversationId = "12345678-1234-1234-1234-123456789abc";

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

function response(data = {}, status = 200, { type = "basic", body = JSON.stringify(data), bodyError = null, redirected = false } = {}) {
    const text = async () => {
        if (bodyError) throw bodyError;
        return body;
    };
    return {
        ok: status >= 200 && status < 300,
        status,
        statusText: "OK",
        type,
        redirected,
        text,
        json: async () => JSON.parse(await text()),
    };
}

function runSummary(status = "running", overrides = {}) {
    const terminal = overrides.terminal ?? ["completed", "refused", "failed", "cancelled", "dispatch_failed", "interrupted"].includes(status);
    return {
        run_id: runId,
        conversation_id: conversationId,
        status,
        execution_started: !["queued", "dispatch_failed"].includes(status),
        terminal,
        cancellable: !terminal && !overrides.cancel_requested,
        events_url: `/runs/${overrides.run_id || runId}/events/`,
        cancel_url: `/runs/${overrides.run_id || runId}/cancel/`,
        cancel_requested: false,
        live_events_incomplete: false,
        external_outcome_unknown: false,
        cleanup_incomplete: false,
        error_code: null,
        queued_warning: false,
        ...overrides,
    };
}

function runPage(status = "running", fields = {}) {
    return { ...runSummary(status, fields), events: [], next_cursor: "0-0", has_more: false, resync_required: false, ...fields };
}

function snapshot(run, fields = {}) {
    return {
        conversation_id: run?.conversation_id || conversationId,
        history: [],
        todos: [],
        context_usage: { used: null, limit: null, percent: null },
        model_name: null,
        active_run: run && !run.terminal ? run : null,
        latest_run: run,
        ...fields,
    };
}

module.exports = { runId, conversationId, deferred, flush, response, runSummary, runPage, snapshot };
