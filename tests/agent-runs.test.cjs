"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { runId, conversationId: conversation, deferred, flush, response, runSummary, runPage, snapshot } = require("./run-fixtures.cjs");
const runsScript = fs.readFileSync(path.join(__dirname, "../nautobot_agent/static/nautobot_agent/agent-runs.js"), "utf8");
const other = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const now = 1804800000000;
const clone = value => JSON.parse(JSON.stringify(value));
const identity = fields => ({request_id: runId, scope: "new", detached: false, created_at: now, ...fields});
function fixture({selected = "new", active = null, latest = active, records = [], throwEvent = false, crypto} = {}) {
    const requests = [], timers = [], notifications = [];
    const storage = new Map([["nautobot-agent-pending-requests", JSON.stringify(records)]]);
    let count = 0, hidden = false;
    const context = vm.createContext({window: {}, AbortController, Date: class extends Date {static now() {return now;}},
        sessionStorage: {getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)},
        crypto: crypto ?? {randomUUID: () => [runId, other][count++]},
        fetch(url, options) {
            const { promise, resolve, reject } = deferred();
            // Deliberately ignores abort: stale completions must still be fenced.
            requests.push({url, options, resolve, reject});
            return promise;
        },
        setTimeout(callback, ms) {timers.push({callback, ms});},
    });
    vm.runInContext(runsScript, context);
    const runs = context.window.NautobotAgentRuns.create({sendUrl: "/runs", stateUrl: "/state/", csrf: "csrf",
        isHidden: () => hidden, notify(note) {
            notifications.push(clone(note));
            if (throwEvent && note.type === "event") throw new Error("notification processing failed");
        }});
    runs.initialize({conversation: selected, activeRun: active, latestRun: latest});
    return {runs, requests, timers, notifications,
        get state() {return runs.state;}, get stored() {return JSON.parse(storage.get("nautobot-agent-pending-requests") || "[]");},
        hide(value) {hidden = value;},
        async timer(expected) {assert.equal(timers[0]?.ms, expected); timers.shift().callback(); await flush();},
    };
}
const last = f => f.requests.at(-1);
const notices = f => f.notifications.filter(note => note.type === "notice");
async function reply(request, data = {}, status = 200, options) {request.resolve(response(data, status, options)); await flush();}
async function fail(request) {request.reject(new Error("offline")); await flush();}
async function submit(f, run = runSummary()) {
    const sent = f.runs.submit("question");
    await reply(last(f), run, 202);
    await sent;
}

test("HTTP submission generates a version 4 UUID without randomUUID", async () => {
    const f = fixture({crypto: {getRandomValues(bytes) {return bytes.fill(255);}}});
    const sent = f.runs.submit("HTTP prompt");
    const body = JSON.parse(last(f).options.body);
    assert.equal(body.client_request_id, "ffffffff-ffff-4fff-bfff-ffffffffffff");
    assert.equal(body.message, "HTTP prompt");
    assert.equal(f.stored[0].request_id, body.client_request_id);
    await reply(last(f), runSummary("queued"), 202);
    await sent;
    assert.equal(f.runs.canSubmit, false);
});

test("locally submitted queued prompts return on expiry or cancellation, but not after starting", async t => {
    for (const outcome of ["expired", "cancelled", "started", "resync", "completed"]) await t.test(outcome, async () => {
        const f = fixture();
        const sent = f.runs.submit("recover this prompt");
        await reply(last(f), runSummary("queued"), 202);
        await sent;
        if (outcome === "cancelled") {
            const cancelled = f.runs.cancel();
            await reply(last(f), runSummary("cancelled", {execution_started: false}));
            await cancelled;
            assert.equal(notices(f).at(-1).restoreDraft, "recover this prompt");
            await reply(f.requests[1], runPage("cancelled"));
        } else if (outcome === "expired") {
            await reply(last(f), runPage("dispatch_failed", {error_code: "queue_wait_expired", resync_required: true}));
        } else if (outcome === "resync") {
            await reply(last(f), runPage("queued", {resync_required: true}));
        } else if (outcome === "completed") {
            await reply(last(f), runPage("completed"));
        } else {
            await reply(last(f), runPage("running"));
            await f.timer(500);
            await reply(last(f), runPage("cancelled"));
        }
        const terminal = ["expired", "resync"].includes(outcome) ? "dispatch_failed"
            : outcome === "completed" ? "completed" : "cancelled";
        await reply(last(f), snapshot(runSummary(terminal)));
        const drafts = notices(f).filter(n => n.restoreDraft !== undefined);
        assert.deepEqual(drafts.map(n => n.restoreDraft),
            ["started", "completed"].includes(outcome) ? [] : ["recover this prompt"]);
        assert.equal(f.runs.canSubmit, true);
    });
});

test("missed execution never restores a failed or cancelled prompt", async t => {
    for (const transport of ["poll", "cancel", "submission"]) {
        for (const status of ["failed", "cancelled"]) await t.test(`${transport}: ${status}`, async () => {
            const f = fixture();
            if (transport === "submission") {
                const sent = f.runs.submit("already executed");
                await reply(last(f), runSummary(status), 202);
                await sent;
            } else {
                await submit(f, runSummary("queued"));
                if (transport === "cancel") {
                    const cancelling = f.runs.cancel();
                    await reply(last(f), runSummary(status));
                    await cancelling;
                } else {
                    await reply(last(f), runPage(status, {
                        events: [{event: "error", data: {error: "Execution failed"}}],
                    }));
                }
            }
            assert.deepEqual(notices(f).filter(note => note.restoreDraft !== undefined), []);
            assert.equal(f.state.run.status, status);
        });
    }
});

test("HTML cancellation errors show the HTTP failure without restoring the draft", async () => {
    const f = fixture();
    await submit(f, runSummary("queued"));
    const cancelling = f.runs.cancel();
    await reply(last(f), {}, 403, {body: "<html>CSRF verification failed</html>"});
    await cancelling;
    assert.equal(notices(f).at(-1).text,
        " Cancellation request could not be confirmed. Cancellation failed (403).");
    assert.equal(notices(f).at(-1).restoreDraft, undefined);
    assert.equal(f.state.run.status, "queued");
    assert.equal(f.runs.canSubmit, false);
});

test("live and restored polling retry the durable refresh while retaining diagnostics", async t => {
    for (const restored of [false, true]) await t.test(restored ? "status-only" : "live", async () => {
        const f = fixture(restored ? {selected: conversation, active: runSummary()} : {});
        if (!restored) await submit(f);
        await reply(last(f), runPage("failed", {error_code: "execution_failed"}));
        assert.equal(f.state.run.error_code, "execution_failed");
        assert.equal(last(f).url, `/state/?conversation=${conversation}`);
        await reply(last(f), {}, 503);
        assert.equal(f.state.run.error_code, "execution_failed");
        assert.equal(f.state.run.terminal, true);
        assert.match(f.state.observationNotice, /retrying/);
        assert.equal(f.requests.length, restored ? 2 : 3);
        assert.equal(f.runs.canSubmit, false);
        await f.timer(restored ? 2000 : 1000);
        await reply(last(f), {}, 503);
        await f.timer(2000);
        await reply(last(f), snapshot(runSummary("failed", {error_code: "execution_failed"})));
        assert.equal(f.state.run.error_code, "execution_failed");
        assert.equal(f.notifications.filter(n => n.type === "snapshot").at(-1).rebuildHistory, true);
        assert.equal(f.runs.canSubmit, true);
    });
});

test("submission identity is persisted before POST and valid terminal pages drain sequentially", async () => {
    const f = fixture(); await submit(f);
    assert.deepEqual(JSON.parse(f.requests[0].options.body), {message: "question", conversation_id: null, client_request_id: runId});
    assert.equal(f.requests[0].options.headers["X-CSRFToken"], "csrf");
    assert.equal(f.requests[0].options.redirect, undefined);
    assert.deepEqual(f.stored, []);
    assert.equal(last(f).options.redirect, "manual");
    assert.equal(f.requests.length, 2);
    await reply(last(f), runPage("completed", {events: [{event: "token", data: {text: "first"}}], next_cursor: "1-0", has_more: true}));
    assert.equal(f.requests.length, 2);
    assert.equal(f.runs.canSubmit, false);
    await f.runs.submit("too soon");
    assert.equal(f.requests.length, 2);
    await f.timer(0);
    assert.match(last(f).url, /after=1-0$/);
    await reply(last(f), runPage("completed", {events: [
        {event: "token", data: {text: "last"}}, {event: "done", data: {}},
    ], next_cursor: "2-0"}));
    assert.equal(last(f).url, `/state/?conversation=${conversation}`);
    assert.equal(f.runs.canSubmit, false);
    await f.runs.submit("still refreshing");
    assert.equal(f.requests.length, 4);
    assert.deepEqual(f.notifications.filter(n => n.type === "event" && n.event === "token").map(n => n.data.text), ["first", "last"]);
    await reply(last(f), snapshot(runSummary("completed")));
    const completion = f.notifications.filter(n => n.type === "snapshot").at(-1);
    assert.equal(completion.rebuildHistory, false);
    assert.equal(f.state.run.terminal, true);
    assert.equal(f.runs.canSubmit, true);
    assert.deepEqual(f.stored, []);
    assert.equal(f.timers.length, 0);
});
test("single-page and status-only completion block sending until saved history arrives", async t => {
    for (const restored of [false, true]) await t.test(restored ? "status-only" : "single page", async () => {
        const f = fixture(restored ? {selected: conversation, active: runSummary()} : {});
        if (!restored) await submit(f);
        await reply(last(f), runPage("completed", {events: [{event: "done", data: {}}], has_more: false}));
        assert.equal(last(f).url, `/state/?conversation=${conversation}`);
        assert.equal(f.runs.canSubmit, false);
        const count = f.requests.length;
        await f.runs.submit("next question");
        assert.equal(f.requests.length, count);
        await reply(last(f), snapshot(runSummary("completed")));
        assert.equal(f.notifications.filter(n => n.type === "snapshot").at(-1).rebuildHistory, restored);
        assert.equal(f.runs.canSubmit, true);
    });
});
test("retryable terminal drain failures retain admission protection and recover", async t => {
    for (const stage of ["events", "snapshot"]) await t.test(stage, async () => {
        const f = fixture(); await submit(f);
        await reply(last(f), runPage("completed", {has_more: true, next_cursor: "1-0"}));
        await f.timer(0);
        if (stage === "snapshot") await reply(last(f), runPage("completed", {
            events: [{event: "done", data: {}}], next_cursor: "2-0",
        }));
        assert.equal(f.runs.canSubmit, false);
        await fail(last(f));
        assert.equal(f.runs.canSubmit, false);
        assert.match(f.state.observationNotice, /retrying/);
        await f.timer(1000);
        if (stage === "events") {
            assert.match(last(f).url, /after=1-0$/);
            await reply(last(f), runPage("completed", {
                events: [{event: "done", data: {}}], next_cursor: "2-0",
            }));
        }
        assert.equal(last(f).url, `/state/?conversation=${conversation}`);
        await reply(last(f), snapshot(runSummary("completed")));
        assert.equal(f.runs.canSubmit, true);
        assert.equal(f.timers.length, 0);
    });
});
test("permanent terminal refresh failures release admission and request a reload", async () => {
    const f = fixture(); await submit(f);
    await reply(last(f), runPage("completed"));
    await reply(last(f), {}, 403);
    assert.equal(f.runs.canSubmit, true);
    assert.match(notices(f).at(-1).text, /Saved conversation could not be refreshed.*Sign in/);
    assert.equal(f.timers.length, 0);
});
test("terminal drain resync rebuilds saved history before releasing submission", async () => {
    const f = fixture(); await submit(f);
    await reply(last(f), runPage("completed", {has_more: true, next_cursor: "1-0"}));
    await f.timer(0);
    await reply(last(f), runPage("completed", {resync_required: true}));
    assert.equal(last(f).url, `/state/?conversation=${conversation}`);
    assert.equal(f.runs.canSubmit, false);
    await reply(last(f), snapshot(runSummary("completed")));
    assert.equal(f.notifications.filter(n => n.type === "snapshot").at(-1).rebuildHistory, true);
    assert.equal(f.runs.canSubmit, true);
});
test("New chat clears terminal draining and fences old drain work", async t => {
    for (const stage of ["sleep", "events", "snapshot"]) await t.test(stage, async () => {
        const f = fixture(); await submit(f);
        await reply(last(f), runPage("completed", {has_more: true, next_cursor: "1-0"}));
        if (stage !== "sleep") await f.timer(0);
        if (stage === "snapshot") await reply(last(f), runPage("completed"));
        const old = last(f);
        assert.equal(f.runs.canSubmit, false);
        f.runs.newConversation();
        assert.equal(f.runs.canSubmit, true);
        const sent = f.runs.submit("new question");
        const admission = last(f), count = f.notifications.length, requests = f.requests.length;
        if (stage === "sleep") await f.timer(0);
        else await reply(old, stage === "snapshot" ? snapshot(runSummary("completed")) : runPage("completed"));
        assert.equal(f.notifications.length, count);
        assert.equal(f.requests.length, requests);
        await reply(admission, runSummary("running", {run_id: other}), 202); await sent;
        await reply(last(f), runPage("completed", {run_id: other}));
        await reply(last(f), snapshot(runSummary("completed", {run_id: other})));
        assert.equal(f.state.run.run_id, other);
        assert.equal(f.runs.canSubmit, true);
    });
});
test("display omissions retain streaming and rebuild complete history on completion", async () => {
    const f = fixture(); await submit(f);
    await reply(last(f), runPage("running", {
        events: [{event: "display_omitted", data: {}}, {event: "token", data: {text: "later progress"}}],
        next_cursor: "9-4",
    }));
    assert.equal(f.notifications.filter(n => n.type === "event").at(-1).data.text, "later progress");
    await f.timer(500);
    assert.match(last(f).url, /after=9-4$/);
    await reply(last(f), runPage("completed", {events: [{event: "done", data: {}}]}));
    await reply(last(f), snapshot(runSummary("completed")));
    assert.equal(f.notifications.filter(n => n.type === "snapshot").at(-1).rebuildHistory, true);
});

test("completion rendering intent fails closed for missing done, errors, and interruption", async t => {
    await t.test("terminal event page without done drains and rebuilds", async () => {
        const f = fixture(); await submit(f);
        await reply(last(f), runPage("completed", {events: [{event: "token", data: {text: "partial"}}], has_more: true, next_cursor: "1-0"}));
        await f.timer(0);
        await reply(last(f), runPage("completed", {next_cursor: "1-0"}));
        await reply(last(f), snapshot(runSummary("completed")));
        assert.equal(f.notifications.filter(n => n.type === "snapshot").at(-1).rebuildHistory, true);
    });
    await t.test("error followed by done still rebuilds", async () => {
        const f = fixture(); await submit(f);
        await reply(last(f), runPage("completed", {events: [
            {event: "error", data: {error: "partial failure"}}, {event: "done", data: {}},
        ]}));
        await reply(last(f), snapshot(runSummary("completed")));
        assert.equal(f.notifications.filter(n => n.type === "snapshot").at(-1).rebuildHistory, true);
    });
    await t.test("interrupted polling completion rebuilds", async () => {
        const f = fixture({selected: conversation, active: runSummary()});
        await reply(last(f), runPage("interrupted"));
        await reply(last(f), snapshot(runSummary("interrupted")));
        assert.equal(f.notifications.filter(n => n.type === "snapshot").at(-1).rebuildHistory, true);
    });
});
test("completion preservation rejects non-success outcomes and contradictory durable evidence", async t => {
    for (const status of ["refused", "failed", "cancelled", "dispatch_failed", "interrupted"]) {
        await t.test(status, async () => {
            const f = fixture(); await submit(f);
            await reply(last(f), runPage(status, {events: [{event: "done", data: {}}]}));
            await reply(last(f), snapshot(runSummary(status)));
            assert.equal(f.notifications.filter(n => n.type === "snapshot").at(-1).rebuildHistory, true);
        });
    }
    for (const [name, fields] of [
        ["different latest", {latest_run: runSummary("completed", {run_id: other})}],
        ["durable incomplete", {latest_run: runSummary("completed", {live_events_incomplete: true})}],
        ["durable not terminal", {latest_run: runSummary("completed", {terminal: false})}],
        ["different conversation", {conversation_id: other}],
        ["active successor", {active_run: runSummary("running", {run_id: other})}],
    ]) await t.test(name, async () => {
        const f = fixture(); await submit(f);
        await reply(last(f), runPage("completed", {events: [{event: "done", data: {}}]}));
        await reply(last(f), snapshot(runSummary("completed"), fields));
        assert.equal(f.notifications.filter(n => n.type === "snapshot").at(-1).rebuildHistory, true);
    });
    for (const mode of ["restored", "resync"]) await t.test(mode, async () => {
        const f = fixture(mode === "restored" ? {selected: conversation, active: runSummary()} : {});
        if (mode !== "restored") await submit(f);
        await reply(last(f), runPage("completed", {
            events: [{event: "done", data: {}}],
            resync_required: mode === "resync", live_events_incomplete: mode === "resync",
        }));
        await reply(last(f), snapshot(runSummary("completed")));
        assert.equal(f.notifications.filter(n => n.type === "snapshot").at(-1).rebuildHistory, true);
    });
});


test("confirmed identity lookup transfers duplicate protection to the run when history refresh fails", async () => {
    const f = fixture(); const sent = f.runs.submit("private prompt");
    assert.equal(JSON.stringify(f.stored).includes("private prompt"), false);
    await f.runs.submit("duplicate"); assert.equal(f.requests.length, 1);
    await fail(last(f)); await sent;
    assert.match(notices(f).at(-1).text, /uncertain/);
    const reloaded = fixture({records: f.stored});
    await reply(last(reloaded), runSummary());
    assert.equal(reloaded.runs.canSubmit, false);
    assert.deepEqual(reloaded.stored, []);
    await reply(last(reloaded), {}, 503);
    assert.equal(reloaded.state.run.run_id, runId);
    assert.equal(reloaded.runs.canSubmit, false);
    await reloaded.runs.submit("blocked");
    assert.equal(reloaded.requests.filter(request => request.options.method === "POST").length, 0);
    assert.deepEqual(reloaded.stored, []);
    assert.match(notices(reloaded).at(-1).text, /outcome is resolved/);
    const restored = fixture({selected: conversation, active: reloaded.state.run, records: reloaded.stored});
    assert.match(last(restored).url, /status_only=1$/);
    assert.equal(restored.runs.canSubmit, false);
});

test("only the newer uncertain submission remains after an earlier confirmed acceptance", async () => {
    const f = fixture(); await submit(f);
    await reply(last(f), runPage("completed")); await reply(last(f), snapshot(runSummary("completed")));
    const sent = f.runs.submit("new request"); await reply(last(f), {error: "Storage unavailable."}, 503); await sent;
    assert.deepEqual(f.stored.map(record => record.request_id), [other]);
    assert.equal(f.runs.canSubmit, false);
    await f.runs.submit("duplicate"); assert.equal(f.requests.length, 4);
});

test("cancellation during identity recovery survives late history and failed refreshes", async t => {
    for (const failed of [false, true]) await t.test(failed ? "failed refresh" : "late snapshot", async () => {
        const f = fixture({records: [identity()]});
        await reply(last(f), runSummary());
        const history = last(f), cancel = f.runs.cancel();
        await reply(last(f), runSummary("running", {cancel_requested: true}));
        await cancel;
        if (failed) await reply(history, {}, 503);
        else await reply(history, snapshot(runSummary()));
        assert.equal(f.state.run.cancel_requested, true);
        assert.equal(f.state.run.cancellable, false);
        assert.equal(f.runs.canSubmit, false);
        assert.deepEqual(f.stored, []);
    });
});

test("confirmed acceptance cannot be readopted by a fresh New chat page", async () => {
    const f = fixture();
    const sent = f.runs.submit("question");
    assert.deepEqual(f.stored, [identity()]);
    await reply(last(f), runSummary(), 202);
    await sent;
    const fresh = fixture({records: f.stored});
    assert.equal(fresh.state.conversation, "new");
    assert.equal(fresh.state.run, null);
    assert.equal(fresh.runs.canSubmit, true);
    assert.equal(fresh.requests.length, 0);
});

test("identity reload renders durable history before restored status-only observation", async () => {
    const f = fixture({records: [identity()]});
    assert.match(last(f).url, /client_request=/);
    await reply(last(f), runSummary());
    assert.equal(last(f).url, `/state/?conversation=${conversation}`);
    await reply(last(f), snapshot(runSummary(), {history: [{role: "assistant", content: "restored answer"}]}));
    assert.equal(f.state.conversation, conversation);
    assert.match(last(f).url, /status_only=1$/);
    assert.equal(f.notifications.find(n => n.type === "snapshot").snapshot.history[0].content, "restored answer");
    assert.deepEqual(f.stored, []);
    assert.equal(f.runs.canSubmit, false);
});

test("identity reload adopts a newer active run and resolves the old identity", async () => {
    const f = fixture({selected: conversation, records: [identity({scope: conversation})]});
    await reply(last(f), runSummary("completed"));
    await reply(last(f), snapshot(runSummary("running", {run_id: other})));
    assert.equal(f.state.run.run_id, other);
    assert.match(last(f).url, new RegExp(`${other}/events/\\?status_only=1$`));
    assert.deepEqual(f.stored, []);
});

test("identity misses and failures remain visible, including follow-ups with prior diagnostics", async t => {
    for (const prior of [false, true]) for (const failed of [false, true]) {
        await t.test(`${prior ? "follow-up" : "first"} ${failed ? "failure" : "miss"}`, async () => {
            const record = identity({scope: prior ? conversation : "new"});
            const f = fixture({selected: prior ? conversation : "new", latest: prior ? runSummary("failed", {execution_error: "prior diagnostic"}) : null, records: [record]});
            await reply(last(f), failed ? {} : {run_id: null}, failed ? 503 : 200);
            assert.equal(notices(f).length, 1);
            assert.equal(f.runs.canSubmit, false);
            assert.deepEqual(f.stored, [record]);
            if (prior) assert.equal(f.state.run.execution_error, "prior diagnostic");
            assert.equal(f.requests.length, 1);
        });
    }
});

test("detached identities preserve selection and diagnostics", async t => {
    await t.test("New chat preserves unresolved identity; old lookup cannot adopt", async () => {
        const record = identity({request_id: other, scope: conversation});
        const f = fixture({selected: conversation, records: [record]});
        const old = last(f);
        f.runs.newConversation();
        assert.equal(old.options.signal.aborted, true);
        await reply(old, runSummary());
        assert.equal(f.state.conversation, "new");
        assert.deepEqual(f.stored, [{...record, detached: true}]);
        const sent = f.runs.submit("another conversation");
        assert.equal(JSON.parse(last(f).options.body).conversation_id, null);
        assert.equal(f.stored[1].scope, "new");
        await fail(last(f)); await sent;
    });
    for (const selected of ["new", conversation]) await t.test(`detached resolution from ${selected}`, async () => {
        const f = fixture({selected, latest: selected === conversation ? runSummary("failed") : null, records: [identity({detached: true})]});
        await reply(last(f), runSummary("running", {run_id: other, conversation_id: other}));
        assert.equal(f.state.conversation, selected);
        assert.deepEqual(f.stored, []);
        assert.equal(notices(f).length, 0);
    });
});

test("terminal flags, not status strings, govern admission and bootstrap observation", async () => {
    const f = fixture({selected: conversation, active: runSummary("running")});
    await f.runs.submit("blocked");
    assert.equal(f.requests.some(request => request.url === "/runs"), false);
    const g = fixture({selected: conversation, latest: runSummary("running", {terminal: true})});
    const sent = g.runs.submit("allowed"); assert.equal(last(g).url, "/runs");
    await fail(last(g)); await sent;
    const observed = fixture({selected: conversation, active: runSummary()});
    await reply(last(observed), runSummary("interrupted"));
    await reply(last(observed), snapshot(runSummary("interrupted")));
    assert.equal(observed.requests.length, 2);
    assert.equal(observed.state.run.terminal, true);
    assert.equal(observed.timers.length, 0);
});

test("permanent observation failures stop without releasing the confirmed run", async t => {
    for (const [name, status, options, message] of [
        ["400", 400, {}, /400/], ["401", 401, {}, /Sign in/], ["403", 403, {}, /Sign in/], ["404", 404, {}, /404/],
        ["HTML", 200, {body: "<html>login</html>"}, /invalid response/],
        ["opaque redirect", 0, {type: "opaqueredirect"}, /Sign in/],
        ["multiple choices", 300, {}, /Sign in/], ["not modified", 304, {}, /Sign in/],
    ]) await t.test(name, async () => {
        const f = fixture(); await submit(f); await reply(last(f), {}, status, options);
        assert.match(f.state.observationNotice, message);
        assert.match(f.state.observationNotice, /may still be executing/);
        assert.equal(f.state.run.terminal, false);
        assert.equal(f.timers.length, 0);
        assert.equal(f.requests.length, 2);
        assert.deepEqual(f.stored, []);
    });
});

test("network, body, 429 and 5xx failures back off sequentially with an exact cursor and cap", async () => {
    const f = fixture(); await submit(f);
    await reply(last(f), runPage("running", {events: [{event: "error", data: {error: "keep diagnostic"}}], next_cursor: "9-4"}));
    await f.timer(500);
    const delays = [1000, 2000, 4000, 8000, 16000, 30000, 30000];
    for (const [i, delay] of delays.entries()) {
        assert.match(last(f).url, /after=9-4$/);
        if (i === 1) await reply(last(f), {}, 429);
        else if (i === 2) await reply(last(f), {}, 503, {body: "<html>unavailable</html>"});
        else if (i === 3) await reply(last(f), {}, 200, {bodyError: new Error("body lost")});
        else await fail(last(f));
        assert.equal(f.state.run.execution_error, "keep diagnostic");
        assert.match(f.state.observationNotice, /retrying/);
        assert.equal(f.timers.length, 1);
        const count = f.requests.length;
        await f.timer(delay); assert.equal(f.requests.length, count + 1);
    }
    await reply(last(f), runPage("running")); await f.timer(500);
    await fail(last(f)); assert.equal(f.timers[0].ms, 1000);
    assert.equal(f.state.run.execution_error, "keep diagnostic");
});

test("hidden and restored status-only modes never retry below normal cadence", async () => {
    const f = fixture(); await submit(f); f.hide(true); await fail(last(f)); assert.equal(f.timers[0].ms, 5000);
    const restored = fixture({selected: conversation, active: runSummary()});
    assert.match(last(restored).url, /status_only=1$/);
    await reply(last(restored), {}, 503); assert.equal(restored.timers[0].ms, 2000);
});

test("resync retries saved state, preserves recovery notice and cancellation, then resumes status-only", async () => {
    const f = fixture(); await submit(f);
    await reply(last(f), runPage("running", {resync_required: true, live_events_incomplete: true}));
    for (const delay of [2000, 2000, 4000]) {
        assert.equal(last(f).url, `/state/?conversation=${conversation}`);
        await fail(last(f));
        assert.equal(f.state.recoveryPending, true);
        assert.match(f.state.observationNotice, /retrying/);
        assert.equal(f.requests.some(r => /status_only=1/.test(r.url)), false);
        await f.timer(delay);
    }
    const saved = last(f), cancel = f.runs.cancel();
    await reply(last(f), runSummary("running", {cancel_requested: true})); await cancel;
    assert.equal(f.state.recoveryPending, true);
    await reply(saved, snapshot(runSummary("running", {live_events_incomplete: true})));
    assert.equal(f.state.recoveryPending, false);
    assert.equal(f.state.observationNotice, "");
    assert.equal(f.state.run.cancel_requested, true);
    await f.timer(2000); assert.match(last(f).url, /status_only=1$/);
    await fail(last(f)); assert.equal(f.timers[0].ms, 2000);
});

test("status-only, recovery, malformed payload and notification processing errors fail closed", async t => {
    for (const mode of ["status-only", "recovery", "malformed", "notification"]) await t.test(mode, async () => {
        const f = fixture({selected: conversation, active: mode === "status-only" ? runSummary() : null, throwEvent: mode === "notification"});
        if (mode !== "status-only") await submit(f);
        if (mode === "recovery") await reply(last(f), runPage("running", {resync_required: true}));
        if (mode === "malformed") await reply(last(f), null);
        else if (mode === "notification") await reply(last(f), runPage("running", {events: [{event: "token", data: {text: "x"}}]}));
        else await reply(last(f), {}, 403);
        assert.match(f.state.observationNotice, /Updates have stopped/);
        assert.doesNotMatch(f.state.observationNotice, /retrying/);
        assert.equal(f.state.run.terminal, false);
        assert.equal(f.timers.length, 0);
    });
});

test("cancellation stays sticky through late live and same-run saved-state responses", async () => {
    const f = fixture(); await submit(f); const poll = last(f);
    const cancel = f.runs.cancel();
    assert.equal(poll.options.signal.aborted, false);
    assert.equal(last(f).options.method, "POST");
    assert.equal(last(f).options.headers["X-CSRFToken"], "csrf");
    assert.equal(last(f).options.redirect, undefined);
    await reply(last(f), runSummary("running", {cancel_requested: true})); await cancel;
    await reply(poll, runPage("running"));
    assert.equal(f.state.run.cancel_requested, true);
    assert.equal(f.state.run.cancellable, false);
    await f.timer(500);
    await reply(last(f), runPage("running", {resync_required: true}));
    await reply(last(f), snapshot(runSummary()));
    assert.equal(f.state.run.cancel_requested, true);
    assert.equal(f.state.run.cancellable, false);
});

test("only confirmed completion clears stopped-observation warning", async t => {
    for (const complete of [false, true]) await t.test(complete ? "confirmed" : "accepted incomplete", async () => {
        const f = fixture(); await submit(f, runSummary("queued")); await reply(last(f), {}, 404);
        const cancel = f.runs.cancel(); await reply(last(f), runSummary(complete ? "cancelled" : "running", {cancel_requested: true})); await cancel;
        if (complete) {assert.equal(f.state.observationNotice, ""); assert.deepEqual(f.stored, []);}
        else {assert.match(f.state.observationNotice, /Updates have stopped/); assert.deepEqual(f.stored, []);}
    });
});

test("late incomplete summary cannot reopen a confirmed cancellation", async () => {
    const f = fixture(); await submit(f); const poll = last(f);
    const cancel = f.runs.cancel(); await reply(last(f), runSummary("cancelled")); await cancel;
    await reply(poll, runPage("running"));
    assert.equal(f.state.run.status, "cancelled");
    assert.equal(f.state.run.terminal, true);
    assert.equal(last(f).url, `/state/?conversation=${conversation}`);
});

test("successor and missing latest force a durable rebuild", async t => {
    await t.test("successor status-only", async () => {
        const f = fixture(); await submit(f); await reply(last(f), runPage("completed"));
        await reply(last(f), snapshot(runSummary("running", {run_id: other})));
        assert.equal(f.state.run.run_id, other);
        assert.match(last(f).url, new RegExp(`${other}/events/\\?status_only=1$`));
        assert.equal(f.notifications.filter(n => n.type === "snapshot").at(-1).rebuildHistory, true);
        assert.deepEqual(f.stored, []);
    });
    await t.test("missing latest run rebuilds", async () => {
        const f = fixture(); await submit(f);
        await reply(last(f), runPage("completed", {events: [{event: "done", data: {}}]}));
        await reply(last(f), snapshot(runSummary("completed"), {latest_run: null}));
        assert.equal(f.notifications.filter(n => n.type === "snapshot").at(-1).rebuildHistory, true);
    });
});
test("obsolete failures, cancellations, responses and sleeps cannot publish or start requests", async t => {
    for (const kind of ["failure", "cancel", "sleep", "response", "submission", "blocker snapshot"]) await t.test(kind, async () => {
        const f = fixture();
        const sent = kind === "submission" ? f.runs.submit("question") : null;
        if (!sent) await submit(f);
        let old = last(f);
        if (kind === "cancel") {f.runs.cancel(); old = last(f);}
        if (kind === "sleep") await reply(old, runPage("running"));
        if (kind === "blocker snapshot") {
            await reply(old, runPage("completed")); await reply(last(f), snapshot(runSummary("completed")));
            f.runs.submit("blocked");
            await reply(last(f), {blocking_run: runSummary()}, 409);
            old = last(f);
        }
        f.runs.newConversation();
        const notificationCount = f.notifications.length, requestCount = f.requests.length;
        if (kind === "sleep") await f.timer(500);
        else if (kind === "failure") await fail(old);
        else if (kind === "response") await reply(old, runPage("running", {events: [{event: "token", data: {text: "stale"}}]}));
        else if (kind === "submission") {await reply(old, runSummary(), 202); await sent;}
        else if (kind === "blocker snapshot") await reply(old, snapshot(runSummary()));
        else await reply(old, runSummary("cancelled"));
        assert.equal(f.notifications.length, notificationCount);
        assert.equal(f.requests.length, requestCount);
        assert.equal(f.state.conversation, "new");
        assert.equal(f.state.run, null);
    });
});

test("notices follow admission state; successful same-summary polls repaint transient cancel failures", async t => {
    for (const status of [400, 401, 403, 404]) await t.test(`rejection ${status}`, async () => {
        const f = fixture(); const sent = f.runs.submit("question");
        await reply(last(f), {}, status, {body: "<html>denied</html>"}); await sent;
        assert.deepEqual(f.stored, []);
        assert.equal(f.runs.canSubmit, true);
        assert.equal(notices(f).at(-1).restoreDraft, "question");
        assert.equal(f.notifications.at(-1).type, "notice");
    });
    const g = fixture({selected: conversation, active: runSummary()}); const poll = last(g);
    const cancel = g.runs.cancel(); await fail(last(g)); await cancel;
    assert.match(notices(g).at(-1).text, /Cancellation request could not be confirmed/);
    assert.equal(g.state.observationNotice, "");
    assert.equal(g.notifications.at(-1).type, "notice");
    const index = g.notifications.length;
    await reply(poll, runSummary());
    assert.equal(g.notifications.slice(index).some(n => n.type === "state" && n.renderStatus), true);
});

test("blocker selection precedes snapshot wait; adoption and draft restoration finish synchronously", async () => {
    const f = fixture(); const sent = f.runs.submit("draft");
    await reply(last(f), {blocking_run: runSummary("running", {conversation_id: other})}, 409);
    assert.equal(f.state.conversation, other);
    assert.deepEqual(f.stored, []);
    assert.equal(f.runs.canSubmit, true);
    assert.equal(f.notifications.at(-1).renderStatus, false);
    await reply(last(f), snapshot(runSummary("running", {conversation_id: other}))); await sent;
    assert.equal(f.runs.canSubmit, false);
    const tail = f.notifications.slice(-3);
    assert.deepEqual(tail.map(n => n.type), ["state", "snapshot", "notice"]);
    assert.equal(tail[0].renderStatus, true);
    assert.equal(tail[2].restoreDraft, "draft");
    assert.equal(tail[2].text, "");
    assert.match(last(f).url, /status_only=1$/);
});

test("user run limit restores the draft without switching conversations or retaining uncertain identity", async () => {
    for (const selected of ["new", conversation]) {
        const f = fixture({selected}); const sent = f.runs.submit("keep this draft");
        const message = "You already have two active runs. Wait for one to finish or cancel it before sending.";
        await reply(last(f), {code: "user_run_limit", error: message}, 409); await sent;
        assert.equal(f.state.conversation, selected);
        assert.deepEqual(f.stored, []);
        assert.equal(f.runs.canSubmit, true);
        assert.equal(notices(f).at(-1).restoreDraft, "keep this draft");
        assert.equal(notices(f).at(-1).text, message);
        assert.equal(f.requests.length, 1);
        const retry = f.runs.submit("try later");
        assert.equal(f.requests.length, 2);
        assert.notEqual(JSON.parse(last(f).options.body).client_request_id, runId);
        await reply(last(f), {code: "user_run_limit", error: message}, 409); await retry;
    }
});

test("409 without blocker data retains uncertain identity and blocks retry", async () => {
    const f = fixture(); const sent = f.runs.submit("question");
    await reply(last(f), {error: "Blocked."}, 409); await sent;
    assert.deepEqual(f.stored, [identity()]);
    assert.equal(f.runs.canSubmit, false);
    await f.runs.submit("retry");
    assert.equal(f.requests.length, 1);
});

test("terminal dispatch failure restores the submitted draft before durable history arrives", async () => {
    const f = fixture();
    const sent = f.runs.submit("keep this prompt");
    await reply(last(f), runSummary("dispatch_failed"), 503);
    await sent;
    assert.equal(notices(f).at(-1)?.restoreDraft, "keep this prompt");
    assert.equal(f.runs.canSubmit, true);
    assert.deepEqual(f.stored, []);
    await reply(last(f), snapshot(runSummary("dispatch_failed")));
    assert.equal(notices(f).at(-1).restoreDraft, "keep this prompt");
});

test("idle event polling slows after four empty pages and resets on any event", async () => {
    const f = fixture(); await submit(f);
    for (const delay of [500, 500, 500, 2000, 2000]) {
        await reply(last(f), runPage());
        await f.timer(delay);
    }
    await reply(last(f), runPage("running", {events: [{event: "token", data: {text: "answer"}}], next_cursor: "1-0"}));
    await f.timer(500);
    assert.match(last(f).url, /after=1-0$/);
    await reply(last(f), runPage());
    await f.timer(500);
    f.hide(true);
    await reply(last(f), runPage());
    await f.timer(5000);
});

test("login redirects restore the draft and release unresolved submission protection", async () => {
    const f = fixture();
    const sent = f.runs.submit("keep this prompt");
    await reply(last(f), {}, 200, {body: "<html>login</html>", redirected: true});
    await sent;
    assert.equal(f.runs.canSubmit, true);
    assert.deepEqual(f.stored, []);
    assert.equal(notices(f).at(-1).restoreDraft, "keep this prompt");
    assert.match(notices(f).at(-1).text, /Sign in/);
});

test("only old identities with a confirmed missing run expire", async t => {
    for (const [name, record, data, status, expires] of [
        ["expired", identity({detached: true, created_at: now - 300000}), {run_id: null}, 200, true],
        ["recent", identity({detached: true, created_at: now - 299999}), {run_id: null}, 200, false],
        ["selected", identity({created_at: now - 300000}), {run_id: null}, 200, true],
        ["selected conversation", identity({scope: conversation, created_at: now - 300000}), {run_id: null}, 200, true],
        ["recent selected", identity({created_at: now - 299999}), {run_id: null}, 200, false],
        ["failed lookup", identity({detached: true, created_at: now - 300000}), {}, 503, false],
        ["malformed lookup", identity({detached: true, created_at: now - 300000}), {}, 200, false],
    ]) await t.test(name, async () => {
        const f = fixture({selected: record.scope, records: [record]});
        await reply(last(f), data, status);
        assert.deepEqual(f.stored, expires ? [] : [record]);
        const reloaded = fixture({records: f.stored});
        assert.equal(reloaded.requests.length, expires ? 0 : 1);
        if (!record.detached) assert.equal(f.runs.canSubmit, expires);
    });
});

test("state snapshots and durable notifications cannot expose mutable lifecycle ownership", async () => {
    const initial = runSummary(); const f = fixture({selected: conversation, active: initial});
    initial.status = "failed";
    const read = f.state;
    assert.throws(() => {read.run.status = "failed";}, TypeError);
    assert.equal(f.state.run.status, "running");
    assert.equal(f.notifications.find(n => n.type === "state").state.run.status, "running");
    await reply(last(f), runSummary("completed"));
    const saved = snapshot(runSummary("completed")); await reply(last(f), saved);
    saved.latest_run.status = "failed";
    assert.equal(f.state.run.status, "completed");
    assert.equal(read.run.status, "running");
});
