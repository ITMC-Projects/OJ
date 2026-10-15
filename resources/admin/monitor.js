/* All remote values are rendered as text, including judge names and diagnostics. */
(() => {
    'use strict';
    const root = document.getElementById('monitor');
    const get = id => document.getElementById('monitor-' + id);
    const format = value => value === null || value === undefined ? '—' :
        typeof value === 'number' ? Number(value.toFixed(2)).toString() : String(value);
    let offset = 0, total = 0, selected = root.dataset.judge, after = 0, busy = false, timer;
    let caseSubmission = null, caseStarted = null;
    const cases = new Map();
    function node(tag, text) { const el = document.createElement(tag); if (text !== undefined) el.textContent = format(text); return el; }
    function row(body, values) { const tr = node('tr'); values.forEach(value => { const td = node('td'); if (value instanceof Node) td.append(value); else td.textContent = format(value); tr.append(td); }); body.append(tr); }
    async function request(url) {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 8000);
        try {
            const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store', signal: controller.signal});
            if (!response.ok) throw new Error('HTTP ' + response.status);
            return await response.json();
        } finally { clearTimeout(timeout); }
    }
    function judgeLink(name) {
        const a = node('a', name);
        a.href = root.dataset.root + 'judges/' + encodeURIComponent(name) + '/';
        a.addEventListener('click', event => {
            event.preventDefault(); selected = name; after = 0; cases.clear(); caseSubmission = null; caseStarted = null; refresh();
        });
        return a;
    }
    async function detail() {
        if (!selected) return;
        try {
            const url = root.dataset.root + 'api/judges/' + encodeURIComponent(selected) + '/?after=';
            let data = await request(url + after);
            const started = (data.judge.activity || {}).started || null;
            if (caseSubmission !== data.judge.submission || caseStarted !== started) {
                cases.clear();
                const previousAfter = after;
                after = 0; caseSubmission = data.judge.submission; caseStarted = started;
                if (previousAfter) data = await request(url + '0');
            }
            get('detail-status').textContent = selected + ' — ' + data.judge.state;
            const target = get('detail'); target.replaceChildren();
            const activity = data.judge.activity || {};
            target.append(node('p', 'Latest activity: ' + format(activity.event) +
                (activity.updated ? ' · ' + new Date(activity.updated * 1000).toLocaleString() : '')));
            target.append(node('p', 'Runtimes: ' + (data.judge.languages || []).join(', ')));
            const reasons = node('ul');
            (data.judge.diagnostics || []).forEach(item => reasons.append(node('li', '#' + item.submission + ': ' + item.reason)));
            target.append(node('h3', 'Scheduling diagnostics (first queue page)'), reasons);
            target.append(node('h3', 'Testcase results received so far'));
            if (data.testcases === null) target.append(node('p', 'Testcase data unavailable.'));
            (data.testcases || []).forEach(item => cases.set(item.case, item));
            const table = node('table'), head = node('thead'), body = node('tbody');
            const header = node('tr'); ['Case', 'Result', 'Time (s)', 'Memory (KiB)', 'Points', 'Total', 'Batch'].forEach(text => header.append(node('th', text)));
            head.append(header); table.append(head, body);
            Array.from(cases.values()).sort((a, b) => a.case - b.case).forEach(item => row(body, [item.case, item.status, item.time, item.memory, item.points, item.total, item.batch]));
            target.append(table);
            get('cases-more').hidden = !data.has_more;
        } catch (error) { get('detail-status').textContent = 'Judge details unavailable (' + error.message + '). Previously rendered data may be stale.'; }
    }
    async function refresh() {
        if (busy) return;
        busy = true; clearTimeout(timer); get('refresh').disabled = true;
        try {
            const data = await request(root.dataset.api + '?offset=' + offset);
            get('status').textContent = 'System: ' + data.health + ' · Updated ' + new Date().toLocaleTimeString();
            get('alerts').replaceChildren(); (data.alerts || []).forEach(text => get('alerts').append(node('li', text)));
            const unassigned = data.unassigned_in_progress;
            get('unassigned').textContent = unassigned && unassigned.length ?
                'Database processing/grading without a live assignment (up to 20): ' + unassigned.map(item => '#' + item.id).join(', ') +
                '. May be transitioning; investigate if persistent.' : '';
            const summary = get('summary'); summary.replaceChildren();
            const execution = data.execution || {};
            [['Bridge', data.bridge.available ? 'connected' : 'unavailable'], ['Database', data.database.available ? 'connected' : 'unavailable'],
                ['Queued', data.queue_total], ['Completions/min', execution.throughput_per_minute],
                ['Mean dispatch-to-finish (s)', execution.mean_dispatch_to_finish_seconds], ['Internal error rate', execution.internal_error_rate],
                ['Event window (s)', execution.window_seconds], ['Event buffer truncated', execution.truncated]].forEach(([label, value]) => {
                const group = node('div'); group.append(node('dt', label), node('dd', value)); summary.append(group);
            });
            get('judges').replaceChildren();
            data.judges.forEach(judge => row(get('judges'), [judgeLink(judge.name), judge.state +
                (judge.disabled || judge.is_disabled ? ' · disabled' : '') + (judge.is_blocked ? ' · blocked' : ''), judge.tier, judge.submission,
                format(judge.received_testcases) + ' / ' + format(judge.total_testcases),
                judge.last_ping ? Math.max(0, Date.now() / 1000 - judge.last_ping) : null, judge.ping_seconds, judge.load,
                Object.entries(judge.metrics || {}).map(([key, value]) => key.replaceAll('_', ' ') + ': ' + format(value)).join('; ') || '—']));
            get('queue').replaceChildren(); (data.queue || []).forEach(item => row(get('queue'), [item.id, item.problem, item.language, item.priority, item.target]));
            total = data.queue_total;
            get('queue-info').textContent = data.queue === null ? 'Bridge queue unavailable.' :
                'Offset ' + offset + ', showing ' + data.queue.length + ' of ' + total + '. Queue can change between pages.';
            get('prev').disabled = offset === 0; get('next').disabled = total === null || offset + 100 >= total;
            const history = data.history;
            get('history').textContent = history ? history.received + ' received; ' + history.internal_errors +
                ' internal errors; rate: ' + format(history.error_rate) + '. Sampled ' + history.sampled_at + '. States: ' +
                history.states.map(item => item.status + '/' + format(item.result) + ': ' + item.count).join(', ') : 'History unavailable.';
            await detail();
        } catch (error) { get('status').textContent = 'Refresh failed (' + error.message + '). Previously rendered data is stale.'; }
        finally { busy = false; get('refresh').disabled = false; timer = setTimeout(refresh, document.hidden ? 30000 : 5000); }
    }
    get('refresh').addEventListener('click', refresh);
    get('prev').addEventListener('click', () => { offset = Math.max(0, offset - 100); refresh(); });
    get('next').addEventListener('click', () => { offset += 100; refresh(); });
    get('cases-more').addEventListener('click', () => { after = Math.max(0, ...cases.keys()); refresh(); });
    refresh();
})();
