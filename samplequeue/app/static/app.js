/* SampleQueue frontend.

   Four views:
     #/                  processes
     #/p/<id>            status histogram for one process
     #/p/<id>/rules      the rule editor
     #/p/<id>/s/<sid>    the worksheet for one status

   No build step on purpose -- this has to be easy for the lab to run and patch. */

const state = {
  meta: null,
  actor: localStorage.getItem('sq-actor') || '',
};

/* ---------------------------------------------------------------- helpers */

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'html') el.innerHTML = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'value') el.value = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.appendChild(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { 'Content-Type': 'application/json', 'X-Actor': state.actor, ...(opts.headers || {}) },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* non-JSON error */ }
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

let toastTimer;
function toast(msg, isError) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast' + (isError ? ' error' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), isError ? 6000 : 2500);
}

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

function daysSince(iso) {
  if (!iso) return null;
  return Math.floor((Date.now() - new Date(iso).getTime()) / 86400000);
}

function fieldInfo(name) {
  return (state.meta.fields || []).find((f) => f.name === name) || { name, kind: 'text', values: [] };
}

const statusSlug = (id) => (id === null || id === undefined ? 'unassigned' : String(id));
const statusId = (slug) => (slug === 'unassigned' ? null : Number(slug));

/** Dismissable popover anchored under a button. */
function popover(anchor, title, body, width = 340) {
  document.querySelectorAll('.popover').forEach((p) => p.remove());
  const box = h('div', { class: 'popover', style: `width:${width}px` },
    h('div', { class: 'popover-head' },
      h('strong', {}, title),
      h('button', { class: 'x', onclick: () => box.remove() }, '×')),
    body);
  document.body.appendChild(box);
  const rect = anchor.getBoundingClientRect();
  box.style.top = `${rect.bottom + window.scrollY + 6}px`;
  box.style.left = `${Math.max(8, Math.min(rect.left + window.scrollX, window.innerWidth - width - 12))}px`;
  setTimeout(() => {
    const close = (e) => {
      if (!box.contains(e.target) && e.target !== anchor) { box.remove(); document.removeEventListener('mousedown', close); }
    };
    document.addEventListener('mousedown', close);
  }, 0);
  return box;
}

/* ------------------------------------------------------- condition builder */

function newGroup(op) { return { type: 'group', op: op || 'AND', children: [] }; }

function newCond() {
  const first = (state.meta.fields[0] || {}).name || '';
  return { type: 'cond', field: first, kind: fieldInfo(first).kind, op: 'eq', value: '' };
}

const ARITY_RESET = { 0: null, 1: '', 2: ['', ''], list: [] };
const opsFor = (kind) => state.meta.operators[kind] || state.meta.operators.text;
const arityOf = (kind, op) => (opsFor(kind)[op] ? opsFor(kind)[op][1] : 1);

/** The evaluator accepts a bare condition as a whole rule; the editor only
    draws groups. Wrap anything that is not already a group so a rule written
    through the API still opens in the builder. */
function asGroup(node) {
  if (!node || !node.type) return newGroup('AND');
  if (node.type === 'group') {
    node.children = node.children || [];
    return node;
  }
  return { type: 'group', op: 'AND', children: [node] };
}

function renderCondition(host, node, onChange, depth = 0) {
  host.innerHTML = '';
  host.appendChild(groupNode(asGroup(node), onChange, depth, null));
}

function groupNode(node, onChange, depth, remove) {
  node.children = node.children || [];
  const children = h('div', { class: 'group-children' });
  const wrap = h('div', { class: 'group' + (node.op === 'OR' ? ' or' : '') },
    h('div', { class: 'group-head' },
      h('button', {
        class: 'btn btn-sm op-toggle',
        title: 'Switch between matching every condition and any condition',
        onclick: () => { node.op = node.op === 'AND' ? 'OR' : 'AND'; onChange(); },
      }, node.op === 'OR' ? 'match ANY' : 'match ALL'),
      h('button', { class: 'btn btn-sm', onclick: () => { node.children.push(newCond()); onChange(); } }, '+ condition'),
      h('button', {
        class: 'btn btn-sm',
        onclick: () => { node.children.push(newGroup(node.op === 'AND' ? 'OR' : 'AND')); onChange(); },
      }, '+ group'),
      remove && h('button', { class: 'x', title: 'Remove this group', onclick: remove }, '×')),
    children);

  if (!node.children.length) {
    children.appendChild(h('div', { class: 'hint' },
      depth === 0 ? 'No conditions yet — this matches every sample.' : 'Empty group matches everything.'));
  }
  node.children.forEach((child, index) => {
    const drop = () => { node.children.splice(index, 1); onChange(); };
    children.appendChild(child.type === 'group'
      ? groupNode(child, onChange, depth + 1, drop)
      : condNode(child, onChange, drop));
  });
  return wrap;
}

function condNode(node, onChange, remove) {
  const info = fieldInfo(node.field);
  node.kind = node.kind || info.kind;

  const fieldSelect = h('select', {
    class: 'field-select',
    onchange: (e) => {
      node.field = e.target.value;
      node.kind = fieldInfo(node.field).kind;
      if (!opsFor(node.kind)[node.op]) node.op = Object.keys(opsFor(node.kind))[0];
      node.value = ARITY_RESET[arityOf(node.kind, node.op)];
      onChange();
    },
  }, state.meta.fields.map((f) => h('option', { value: f.name, selected: f.name === node.field },
    `${f.name}  ·  ${f.kind}`)));

  const kindSelect = h('select', {
    title: 'How to read this column. Override it if the guess is wrong.',
    onchange: (e) => {
      node.kind = e.target.value;
      node.op = Object.keys(opsFor(node.kind))[0];
      node.value = ARITY_RESET[arityOf(node.kind, node.op)];
      onChange();
    },
  }, ['text', 'number', 'date', 'bool'].map((k) => h('option', { value: k, selected: k === node.kind }, k)));

  const opSelect = h('select', {
    onchange: (e) => {
      node.op = e.target.value;
      node.value = ARITY_RESET[arityOf(node.kind, node.op)];
      onChange();
    },
  }, Object.entries(opsFor(node.kind)).map(([op, [label]]) =>
    h('option', { value: op, selected: op === node.op }, label)));

  return h('div', { class: 'cond' }, fieldSelect, kindSelect, opSelect,
    valueEditor(node, info, onChange),
    h('button', { class: 'x', title: 'Remove', onclick: remove }, '×'));
}

function valueEditor(node, info, onChange) {
  const arity = arityOf(node.kind, node.op);
  const wrap = h('div', { class: 'value-wrap' });
  if (arity === 0) return wrap;

  const suggestions = info.values || [];
  const hasList = suggestions.length > 0 && suggestions.length <= 250;
  // Editing a value changes no structure, so re-render nothing -- otherwise
  // the input is replaced on every keystroke and loses focus after one
  // character, which makes typing a value impossible.
  const valueChanged = () => onChange(false);

  if (arity === 'list') {
    if (!Array.isArray(node.value)) node.value = node.value ? [node.value] : [];
    const chips = h('div', { class: 'chips' }, node.value.map((v) =>
      h('span', { class: 'chip' }, v,
        h('button', {
          title: 'Remove',
          onclick: () => { node.value = node.value.filter((x) => x !== v); onChange(); },
        }, '×'))));
    const add = (raw) => {
      const v = String(raw || '').trim();
      if (v && !node.value.includes(v)) { node.value.push(v); onChange(); }
    };
    wrap.append(chips, hasList
      ? h('select', { onchange: (e) => { add(e.target.value); e.target.value = ''; } },
        [h('option', { value: '' }, 'add value…'), ...suggestions.map((v) => h('option', { value: v }, v))])
      : h('input', {
        type: 'text', placeholder: 'add value + Enter',
        onkeydown: (e) => { if (e.key === 'Enter') { e.preventDefault(); add(e.target.value); } },
      }));
    return wrap;
  }

  if (arity === 2) {
    if (!Array.isArray(node.value)) node.value = ['', ''];
    [0, 1].forEach((i) => wrap.appendChild(h('input', {
      type: 'number', value: node.value[i] ?? '', placeholder: i ? 'max' : 'min',
      oninput: (e) => { node.value[i] = e.target.value; valueChanged(); },
    })));
    return wrap;
  }

  if (node.kind === 'number' || ['within_days', 'older_than_days', 'next_days'].includes(node.op)) {
    wrap.appendChild(h('input', {
      type: 'number', value: node.value ?? '', placeholder: node.kind === 'date' ? 'days' : 'number',
      oninput: (e) => { node.value = e.target.value; valueChanged(); },
    }));
    return wrap;
  }
  if (node.kind === 'date') {
    wrap.appendChild(h('input', {
      type: 'date', value: node.value || '',
      oninput: (e) => { node.value = e.target.value; valueChanged(); },
    }));
    return wrap;
  }
  if (hasList && ['eq', 'neq'].includes(node.op)) {
    wrap.appendChild(h('select', { onchange: (e) => { node.value = e.target.value; valueChanged(); } },
      [h('option', { value: '' }, '— pick a value —'),
        ...suggestions.map((v) => h('option', { value: v, selected: v === node.value }, v))]));
    return wrap;
  }
  wrap.appendChild(h('input', {
    type: 'text', value: node.value ?? '', placeholder: 'value',
    oninput: (e) => { node.value = e.target.value; valueChanged(); },
  }));
  return wrap;
}

/* ------------------------------------------------------------- chrome ---- */

function setCrumbs(parts) {
  const nav = document.getElementById('crumbs');
  nav.innerHTML = '';
  parts.forEach((p, i) => {
    if (i) nav.appendChild(h('span', {}, '/'));
    nav.appendChild(p.href ? h('a', { href: p.href }, p.label) : h('span', {}, p.label));
  });
}

function renderSnapshot() {
  const el = document.getElementById('snapshot');
  const run = state.meta.last_run;
  if (!run) { el.textContent = 'no data loaded yet'; return; }
  const when = new Date(run.finished_at || run.started_at);
  el.textContent = `${state.meta.sample_count.toLocaleString()} open samples · ${when.toLocaleString()}`;
  el.title = run.message || '';
}

async function refreshData() {
  const btn = document.getElementById('refresh-btn');
  const source = (state.meta.sources[0] || {}).name;
  if (!source) { toast('No source configured in sources.json', true); return; }
  btn.disabled = true;
  btn.textContent = 'Refreshing…';
  try {
    const out = await api(`/api/sources/${encodeURIComponent(source)}/ingest`, { method: 'POST' });
    state.meta = await api('/api/meta');
    renderSnapshot();
    const churn = out.processes.reduce((a, p) => a + p.entered + p.moved + p.left, 0);
    toast(`${out.ingest.open.toLocaleString()} open samples · ${churn} queue changes`);
    route();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Refresh data';
  }
}

const view = () => document.getElementById('view');

/* -------------------------------------------------------- processes ----- */

async function viewProcesses() {
  setCrumbs([{ label: 'Processes' }]);
  const list = await api('/api/processes');
  const root = view();
  root.className = '';
  root.innerHTML = '';
  root.append(
    h('h1', {}, 'Processes'),
    h('p', { class: 'sub' },
      'One process per lab workflow. Each owns its own statuses, its own rules, and its own saved priority order.'),
    h('div', { class: 'toolbar' },
      h('button', { class: 'btn btn-primary', onclick: createProcess }, 'New process')));

  if (!list.length) {
    root.appendChild(h('div', { class: 'empty' },
      h('h2', {}, 'Nothing here yet'),
      h('p', {}, 'Create a process, pick which samples belong to it, then define its statuses.')));
    return;
  }

  root.appendChild(h('div', { class: 'cards' }, list.map((p) =>
    h('div', { class: 'card' },
      h('h3', {}, p.name),
      h('div', { class: 'meta' }, p.description || 'No description'),
      h('div', { class: 'meta', style: 'margin-top:8px' },
        `${p.queued.toLocaleString()} samples · ${p.status_count} statuses`),
      h('div', { class: 'row', style: 'margin-top:10px' },
        h('a', { class: 'btn btn-sm btn-primary', href: `#/p/${p.id}` }, 'Open board'),
        h('a', { class: 'btn btn-sm', href: `#/p/${p.id}/rules` }, 'Edit rules'),
        h('button', {
          class: 'btn btn-sm', title: 'Copy this process and its rules',
          onclick: async () => {
            const name = prompt('Name for the copy', `${p.name} (copy)`);
            if (!name) return;
            const made = await api(`/api/processes/${p.id}/duplicate`, { method: 'POST', body: { name } });
            location.hash = `#/p/${made.id}/rules`;
          },
        }, 'Duplicate'),
        h('button', {
          class: 'btn btn-sm btn-danger',
          onclick: async () => {
            if (!confirm(`Delete "${p.name}" and its saved queues?`)) return;
            await api(`/api/processes/${p.id}`, { method: 'DELETE' });
            toast('Deleted');
            route();
          },
        }, 'Delete'))))));
}

async function createProcess() {
  const name = prompt('Process name (e.g. "PacBio Library Construction")');
  if (!name) return;
  try {
    const made = await api('/api/processes', { method: 'POST', body: { name, scope: newGroup('AND') } });
    location.hash = `#/p/${made.id}/rules`;
  } catch (e) { toast(e.message, true); }
}

/* -------------------------------------------------- board (histogram) --- */

async function viewOverview(pid) {
  const data = await api(`/api/processes/${pid}/overview`);
  setCrumbs([{ label: 'Processes', href: '#/' }, { label: data.process.name }]);

  const root = view();
  root.className = '';
  root.innerHTML = '';

  const peak = Math.max(1, ...data.bars.map((b) => b.count));

  root.append(
    h('h1', {}, data.process.name),
    h('p', { class: 'sub' }, data.process.description
      || 'Where every sample in this process is sitting right now.'),
    h('div', { class: 'toolbar' },
      h('button', {
        class: 'btn',
        onclick: async (e) => {
          e.target.disabled = true;
          const out = await api(`/api/processes/${pid}/reconcile`, { method: 'POST' });
          toast(`Re-sorted · ${out.entered} new, ${out.moved} moved, ${out.left} gone`);
          viewOverview(pid);
        },
      }, 'Re-apply rules'),
      h('a', { class: 'btn', href: `#/p/${pid}/rules` }, 'Edit rules'),
      h('a', { class: 'btn', href: `/api/processes/${pid}/export.csv`, download: '' }, 'Export all'),
      h('button', { class: 'btn btn-ghost', onclick: () => showLog(pid) }, 'Recent changes'),
      h('span', { class: 'spacer' }),
      h('span', { class: 'snapshot' }, `${data.total.toLocaleString()} samples in this process`)),
    h('div', { class: 'panel histogram' },
      data.bars.map((bar) => barRow(bar, peak, pid))),
    h('p', { class: 'hint' },
      'Click any status to open its worksheet: every sample in it, the metadata you choose, and the priority order you save.'));
}

function barRow(bar, peak, pid) {
  const width = Math.max(bar.count ? 2 : 0, Math.round((bar.count / peak) * 100));
  const stale = daysSince(bar.oldest_entered);
  const open = () => { location.hash = `#/p/${pid}/s/${statusSlug(bar.id)}`; };

  return h('div', {
    class: 'bar-row' + (bar.count ? '' : ' bar-empty'),
    role: 'button', tabindex: '0',
    onclick: open,
    onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } },
  },
    h('div', { class: 'bar-label' },
      h('span', { class: 'swatch', style: `background:${bar.color}` }),
      h('span', { class: 'bar-name' }, bar.name)),
    h('div', { class: 'bar-track' },
      h('div', {
        class: 'bar-fill' + (bar.over_limit ? ' over' : ''),
        style: `width:${width}%;background:${bar.color}`,
      }),
      bar.wip_limit ? h('div', {
        class: 'bar-limit', title: `WIP limit ${bar.wip_limit}`,
        style: `left:${Math.min(100, (bar.wip_limit / peak) * 100)}%`,
      }) : null),
    h('div', { class: 'bar-count' }, bar.count.toLocaleString()),
    h('div', { class: 'bar-tags' },
      bar.over_limit && h('span', { class: 'badge warn', title: `WIP limit is ${bar.wip_limit}` },
        `over by ${bar.count - bar.wip_limit}`),
      // Only meaningful as a subset; on a freshly built queue everything is
      // "new today" and the badge is just noise next to the count.
      bar.new_today && bar.new_today < bar.count
        ? h('span', { class: 'badge ok' }, `${bar.new_today} new`) : null,
      bar.pinned ? h('span', { class: 'badge' }, `${bar.pinned} pinned`) : null,
      stale !== null && stale > 7 ? h('span', { class: 'badge' }, `oldest ${stale}d`) : null,
      bar.batches ? h('span', { class: 'badge', title: `Batches of ${bar.batch_size}` },
        `${bar.batches} batch${bar.batches === 1 ? '' : 'es'}`) : null,
      bar.auto_sort ? h('span', { class: 'badge', title: `Auto-sorted by ${bar.sort_summary}` }, 'auto-sort') : null));
}

/* ----------------------------------------------------- rules editor ----- */

const PALETTE = ['#2f6f4f', '#1d4ed8', '#9a6b00', '#a4342c', '#6d28d9', '#0f766e', '#be185d', '#475569'];

async function viewRules(id) {
  const process = await api(`/api/processes/${id}`);
  setCrumbs([{ label: 'Processes', href: '#/' },
    { label: process.name, href: `#/p/${id}` }, { label: 'Rules' }]);

  const draft = {
    name: process.name,
    description: process.description,
    scope: asGroup(process.scope),
    statuses: process.statuses.map((s) => ({
      id: s.id, name: s.name, color: s.color,
      wip_limit: s.wip_limit, batch_size: s.batch_size,
      conditions: asGroup(s.conditions),
    })),
  };

  const root = view();
  root.className = '';
  root.innerHTML = '';

  const scopeHost = h('div', {});
  const statusHost = h('div', {});
  const scopeCount = h('span', { class: 'match-count' }, '');

  // Each editor instance gets a token. A preview that resolves after you
  // have navigated to a different process must not write its counts into
  // the page that replaced it.
  viewRules.generation = (viewRules.generation || 0) + 1;
  const myGeneration = viewRules.generation;

  const runPreview = debounce(async () => {
    try {
      const out = await api('/api/preview', { method: 'POST', body: draft });
      if (myGeneration !== viewRules.generation) return;
      scopeCount.innerHTML = `<b>${out.in_scope.toLocaleString()}</b> of ${out.total_samples.toLocaleString()} open samples in scope`;
      out.statuses.forEach((s, i) => {
        const target = statusHost.querySelector(`[data-count="${i}"]`);
        if (target) {
          target.innerHTML = `<b>${s.count.toLocaleString()}</b> samples`;
          target.title = s.examples.map((e) => `${e.order_id} / ${e.sample_id}`).join('\n');
        }
      });
      const leftover = statusHost.querySelector('[data-unmatched]');
      if (leftover) {
        leftover.innerHTML = out.unmatched.count
          ? `<b>${out.unmatched.count.toLocaleString()}</b> samples match no status and land in Unassigned`
          : 'Every in-scope sample matches a status.';
      }
    } catch (e) { toast(e.message, true); }
  }, 350);

  // `structural === false` means only a value was edited: re-check the
  // counts, but leave the DOM (and the focused input) alone.
  function redrawScope() {
    renderCondition(scopeHost, draft.scope, (structural = true) => {
      if (structural !== false) redrawScope();
      runPreview();
    });
  }
  function redrawStatuses() {
    renderStatusList(statusHost, draft, (structural = true) => {
      if (structural !== false) redrawStatuses();
      runPreview();
    });
  }

  root.append(
    h('h1', {}, process.name),
    h('p', { class: 'sub' }, 'Rules are checked top to bottom. The first status a sample matches is the one it lands in.'),

    h('div', { class: 'panel' },
      h('h2', {}, 'Details'),
      h('label', { class: 'field' }, h('span', {}, 'Name'),
        h('input', { type: 'text', value: draft.name, oninput: (e) => { draft.name = e.target.value; } })),
      h('label', { class: 'field' }, h('span', {}, 'Description'),
        h('input', { type: 'text', value: draft.description, oninput: (e) => { draft.description = e.target.value; } }))),

    h('div', { class: 'panel' },
      h('h2', {}, 'Which samples belong to this process?', scopeCount),
      h('p', { class: 'hint' }, 'Narrow the open-order feed down to the samples this process handles — usually by product, product group, or processing step.'),
      scopeHost),

    h('div', { class: 'panel' },
      h('h2', {}, 'Statuses'),
      h('p', { class: 'hint' }, 'Order matters. Put the most specific status first and a broad catch-all last. Columns and default sorting are set on each status’s own worksheet.'),
      statusHost,
      h('div', { class: 'row', style: 'margin-top:10px' },
        h('button', {
          class: 'btn',
          onclick: () => {
            draft.statuses.push({
              name: `Status ${draft.statuses.length + 1}`,
              color: PALETTE[draft.statuses.length % PALETTE.length],
              conditions: newGroup('AND'), wip_limit: null, batch_size: null,
            });
            redrawStatuses(); runPreview();
          },
        }, '+ Add status'))),

    h('div', { class: 'toolbar' },
      h('button', {
        class: 'btn btn-primary',
        onclick: async (e) => {
          e.target.disabled = true;
          try {
            const out = await api(`/api/processes/${id}`, { method: 'PUT', body: draft });
            toast(`Saved · ${out.reconcile.total} samples sorted (${out.reconcile.entered} new, ${out.reconcile.moved} moved)`);
            location.hash = `#/p/${id}`;
          } catch (err) { toast(err.message, true); e.target.disabled = false; }
        },
      }, 'Save rules & re-sort'),
      h('a', { class: 'btn', href: `#/p/${id}` }, 'Back to board')));

  redrawScope();
  redrawStatuses();
  runPreview();
}

function renderStatusList(host, draft, onChange) {
  host.innerHTML = '';
  draft.statuses.forEach((status, index) => {
    const body = h('div', { class: 'status-body' });
    const row = h('div', { class: 'status-row' },
      h('div', { class: 'status-head' },
        h('span', { class: 'order' }, index + 1),
        h('input', { type: 'color', value: status.color, oninput: (e) => { status.color = e.target.value; } }),
        h('input', { type: 'text', value: status.name, oninput: (e) => { status.name = e.target.value; } }),
        h('span', { class: 'match-count', 'data-count': index }, '…'),
        h('label', { class: 'match-count', title: 'Warn when this queue gets longer than N' }, 'WIP ',
          h('input', {
            type: 'number', style: 'width:66px', value: status.wip_limit ?? '', placeholder: '—',
            oninput: (e) => { status.wip_limit = e.target.value ? Number(e.target.value) : null; },
          })),
        h('label', { class: 'match-count', title: 'Split the queue into runs of N samples' }, 'Batch ',
          h('input', {
            type: 'number', style: 'width:66px', value: status.batch_size ?? '', placeholder: '—',
            oninput: (e) => { status.batch_size = e.target.value ? Number(e.target.value) : null; },
          })),
        h('button', {
          class: 'btn btn-sm', title: 'Move up', disabled: index === 0,
          onclick: () => { draft.statuses.splice(index - 1, 0, draft.statuses.splice(index, 1)[0]); onChange(); },
        }, '↑'),
        h('button', {
          class: 'btn btn-sm', title: 'Move down', disabled: index === draft.statuses.length - 1,
          onclick: () => { draft.statuses.splice(index + 1, 0, draft.statuses.splice(index, 1)[0]); onChange(); },
        }, '↓'),
        h('button', {
          class: 'x', title: 'Delete status',
          onclick: () => {
            if (!confirm(`Delete "${status.name}"? Its saved priority order is lost.`)) return;
            draft.statuses.splice(index, 1); onChange();
          },
        }, '×')),
      body);
    renderCondition(body, status.conditions, onChange, 0);
    host.appendChild(row);
  });
  host.appendChild(h('p', { class: 'hint', 'data-unmatched': '1', style: 'margin-top:10px' }, '…'));
}

/* --------------------------------------------------------- worksheet ---- */

const sheet = {
  pid: null, sid: null, data: null, rows: [], selected: new Set(),
  dirty: false, rendered: 0, pageSize: 200, search: '', dragKey: null,
  // Spreadsheet-style cell range, as {row, col} indices into the rendered
  // slice. `col` indexes sheet.data.status.columns.
  cellAnchor: null, cellFocus: null, cellDragging: false,
  // Last checkbox touched, so shift-click can fill the range between.
  lastChecked: null,
};

async function viewSheet(pid, slug) {
  const sid = statusId(slug);
  const data = await api(`/api/processes/${pid}/statuses/${statusSlug(sid)}/page`
    + (sheet.search ? `?search=${encodeURIComponent(sheet.search)}` : ''));

  Object.assign(sheet, {
    pid, sid, data, rows: data.rows, selected: new Set(),
    dirty: false, rendered: Math.min(data.rows.length, sheet.pageSize), dragKey: null,
  });

  setCrumbs([{ label: 'Processes', href: '#/' },
    { label: data.process.name, href: `#/p/${pid}` }, { label: data.status.name }]);

  const root = view();
  root.className = 'sheet-view';
  root.innerHTML = '';
  root.append(
    h('div', { class: 'sheet-head' },
      h('h1', {},
        h('span', { class: 'swatch big', style: `background:${data.status.color}` }),
        data.status.name),
      h('span', { class: 'snapshot' },
        `${data.total.toLocaleString()} samples`
        + (data.shown < data.total ? ` · showing ${data.shown.toLocaleString()}` : '')
        + (data.status.wip_limit ? ` · WIP limit ${data.status.wip_limit}` : '')),
      h('span', { class: 'spacer' }),
      statusJump(pid, data)),
    sheetToolbar(),
    h('div', { class: 'legend hidden', id: 'legend' }),
    h('div', { class: 'selection-bar hidden', id: 'selbar' }),
    h('div', { class: 'sheet-scroll', id: 'sheet-scroll' }),
    h('div', { class: 'row', id: 'sheet-more', style: 'margin-top:10px' }));

  renderTable();
}

function statusJump(pid, data) {
  return h('select', {
    class: 'status-jump', title: 'Jump to another status',
    onchange: (e) => { location.hash = `#/p/${pid}/s/${e.target.value}`; },
  }, data.siblings.map((s) => h('option', {
    value: statusSlug(s.id), selected: s.id === data.status.id,
  }, s.name)));
}

function sheetToolbar() {
  const search = h('input', {
    type: 'text', class: 'search', placeholder: 'Search this status…', value: sheet.search,
    oninput: debounce((e) => { sheet.search = e.target.value; viewSheet(sheet.pid, statusSlug(sheet.sid)); }, 350),
  });
  return h('div', { class: 'toolbar' },
    search,
    h('button', { class: 'btn', onclick: (e) => columnsPopover(e.target) },
      `Columns (${sheet.data.status.columns.length})`),
    h('button', { class: 'btn', onclick: (e) => sortPopover(e.target) },
      sheet.data.status.sort_spec.length
        ? `Sort: ${summariseSort(sheet.data.status.sort_spec)}`
        : 'Default sort'),
    h('button', { class: 'btn', onclick: () => batchModal() },
      sheet.data.status.batch_size
        ? `Batches of ${sheet.data.status.batch_size}`
          + (((sheet.data.status.batch_composition || {}).slots || []).length ? ' ·  composed' : '')
        : 'Batches'),
    h('button', { class: 'btn', onclick: (e) => colorPopover(e.target) },
      sheet.data.status.color_by ? `Colour: ${sheet.data.status.color_by}` : 'Colour rows'),
    h('button', {
      class: 'btn btn-primary hidden', id: 'save-order',
      onclick: () => saveSheetOrder(),
    }, 'Save priority order'),
    h('span', { class: 'copy-hint hidden', id: 'copy-hint' }),
    h('span', { class: 'spacer' }),
    h('a', {
      class: 'btn btn-sm',
      href: `/api/processes/${sheet.pid}/export.csv?status_id=${statusSlug(sheet.sid)}`,
      download: '',
    }, 'Export CSV'),
    h('a', { class: 'btn btn-sm', href: `#/p/${sheet.pid}` }, 'Back to board'));
}

const summariseSort = (spec) => spec.map((k) => `${k.field} ${k.dir === 'desc' ? '↓' : '↑'}`).join(', ');

/* --- columns --- */

function columnsPopover(anchor) {
  const chosen = [...sheet.data.status.columns];
  const body = h('div', {});
  const shown = h('div', { class: 'chooser-list' });
  const available = h('div', { class: 'chooser-list' });
  const filter = h('input', { type: 'text', placeholder: 'Find a column…', class: 'search', style: 'width:100%' });

  const redraw = () => {
    shown.innerHTML = '';
    chosen.forEach((name, index) => shown.appendChild(h('div', { class: 'chooser-row' },
      h('span', { class: 'grow' }, name),
      h('button', {
        class: 'btn btn-sm', title: 'Move left', disabled: index === 0,
        onclick: () => { chosen.splice(index - 1, 0, chosen.splice(index, 1)[0]); redraw(); },
      }, '↑'),
      h('button', {
        class: 'btn btn-sm', title: 'Move right', disabled: index === chosen.length - 1,
        onclick: () => { chosen.splice(index + 1, 0, chosen.splice(index, 1)[0]); redraw(); },
      }, '↓'),
      h('button', { class: 'x', title: 'Hide', onclick: () => { chosen.splice(index, 1); redraw(); } }, '×'))));
    if (!chosen.length) shown.appendChild(h('div', { class: 'hint' }, 'No columns shown.'));

    const needle = filter.value.trim().toLowerCase();
    available.innerHTML = '';
    sheet.data.available_columns
      .filter((c) => !chosen.includes(c.name) && (!needle || c.name.toLowerCase().includes(needle)))
      .slice(0, 60)
      .forEach((c) => available.appendChild(h('div', { class: 'chooser-row' },
        h('span', { class: 'grow' }, c.name, h('span', { class: 'kind' }, c.kind)),
        h('button', { class: 'btn btn-sm', onclick: () => { chosen.push(c.name); redraw(); } }, '+'))));
  };
  filter.addEventListener('input', redraw);

  body.append(
    h('p', { class: 'hint' }, 'Shown, in order:'), shown,
    h('p', { class: 'hint', style: 'margin-top:10px' }, 'Add a column:'), filter, available,
    h('div', { class: 'row', style: 'margin-top:10px' },
      h('button', {
        class: 'btn btn-primary btn-sm',
        onclick: async () => {
          await api(`/api/processes/${sheet.pid}/statuses/${statusSlug(sheet.sid)}/view`,
            { method: 'PUT', body: { columns: chosen } });
          toast('Columns saved');
          viewSheet(sheet.pid, statusSlug(sheet.sid));
        },
      }, 'Save for this status'),
      h('span', { class: 'hint' }, 'Everyone sees this layout.')));
  redraw();
  popover(anchor, 'Columns', body, 380);
}

/* --- default sort --- */

function sortPopover(anchor) {
  const spec = sheet.data.status.sort_spec.map((k) => ({ ...k }));
  let auto = sheet.data.status.auto_sort;
  const body = h('div', {});
  const list = h('div', {});

  const redraw = () => {
    list.innerHTML = '';
    spec.forEach((key, index) => list.appendChild(h('div', { class: 'cond' },
      h('span', { class: 'order' }, index + 1),
      h('select', {
        class: 'grow',
        onchange: (e) => { key.field = e.target.value; key.kind = fieldInfo(key.field).kind; },
      }, sheet.data.available_columns.map((c) => h('option', {
        value: c.name, selected: c.name === key.field,
      }, `${c.name} · ${c.kind}`))),
      h('select', { onchange: (e) => { key.dir = e.target.value; } },
        [h('option', { value: 'asc', selected: key.dir !== 'desc' }, 'A → Z / low → high'),
          h('option', { value: 'desc', selected: key.dir === 'desc' }, 'Z → A / high → low')]),
      h('button', {
        class: 'btn btn-sm', title: 'Move up', disabled: index === 0,
        onclick: () => { spec.splice(index - 1, 0, spec.splice(index, 1)[0]); redraw(); },
      }, '↑'),
      h('button', { class: 'x', onclick: () => { spec.splice(index, 1); redraw(); } }, '×'))));
    if (!spec.length) list.appendChild(h('div', { class: 'hint' }, 'No default sort — new samples join the bottom of the queue.'));
  };

  const save = async (then) => {
    await api(`/api/processes/${sheet.pid}/statuses/${statusSlug(sheet.sid)}/view`,
      { method: 'PUT', body: { sort_spec: spec.filter((k) => k.field), auto_sort: auto } });
    if (then) await then();
    toast('Default sort saved');
    viewSheet(sheet.pid, statusSlug(sheet.sid));
  };

  body.append(
    h('p', { class: 'hint' },
      'Blanks always sort last. Pinned samples always stay on top.'),
    list,
    h('div', { class: 'row', style: 'margin-top:8px' },
      h('button', {
        class: 'btn btn-sm',
        onclick: () => {
          const first = sheet.data.available_columns[0];
          spec.push({ field: first ? first.name : '', dir: 'asc', kind: first ? first.kind : 'text' });
          redraw();
        },
      }, '+ Add sort key')),
    h('label', { class: 'row', style: 'margin-top:12px;gap:6px' },
      h('input', {
        type: 'checkbox', checked: auto || null,
        onchange: (e) => { auto = e.target.checked; },
      }),
      h('span', {}, 'Re-apply on every data refresh')),
    h('p', { class: 'hint' },
      'Leave this off to keep hand-set priority. Turn it on for queues that are genuinely always "oldest first".'),
    h('div', { class: 'row', style: 'margin-top:12px' },
      h('button', { class: 'btn btn-primary btn-sm', onclick: () => save() }, 'Save'),
      h('button', {
        class: 'btn btn-sm',
        title: 'Renumber the whole queue by these keys right now',
        onclick: () => save(async () => {
          const out = await api(`/api/processes/${sheet.pid}/statuses/${statusSlug(sheet.sid)}/resort`,
            { method: 'POST' });
          toast(out.sorted ? `Re-sorted ${out.sorted} samples` : out.reason);
        }),
      }, 'Save & apply now')));
  redraw();
  popover(anchor, 'Default sort', body, 460);
}

/* --- colouring rows by a categorical field --- */

// Pairs of (bar, tint). The bar reads as a spine on the left of the row;
// the tint is light enough that selected and pinned states still show.
const ROW_COLORS = [
  ['#2f6f4f', '#e9f2ed'], ['#1d4ed8', '#e8eefc'], ['#b45309', '#fbf0e3'],
  ['#a4342c', '#fae9e7'], ['#6d28d9', '#f0eafc'], ['#0f766e', '#e5f2f1'],
  ['#be185d', '#fbe8f0'], ['#4d7c0f', '#eef4e3'], ['#0369a1', '#e4f0f8'],
  ['#9a3412', '#f9ebe4'], ['#7c3aed', '#f1ebfd'], ['#065f46', '#e4efec'],
  ['#9d174d', '#fae7ee'], ['#1e40af', '#e7ecfa'],
];
const NO_VALUE_COLOR = ['#cbd5e1', '#f4f5f7'];

/** Fields worth colouring by: a handful of repeating values.
    Mass or concentration would give every row its own colour, which is
    just noise, so anything with too many distinct values is excluded. */
function colorableFields() {
  return (state.meta.fields || []).filter((f) =>
    f.kind !== 'date' && f.distinct_count >= 2 && f.distinct_count <= 24);
}

function colorScale(fieldName) {
  const info = (state.meta.fields || []).find((f) => f.name === fieldName);
  const values = (info && info.values) || [];
  const map = new Map();
  values.forEach((v, i) => map.set(String(v), ROW_COLORS[i % ROW_COLORS.length]));
  return (value) => {
    const key = String(value ?? '');
    if (!key) return NO_VALUE_COLOR;
    if (!map.has(key)) {
      // Value absent from the catalogue sample; fall back to a stable hash.
      let hash = 0;
      for (let i = 0; i < key.length; i += 1) hash = (hash * 31 + key.charCodeAt(i)) | 0;
      map.set(key, ROW_COLORS[Math.abs(hash) % ROW_COLORS.length]);
    }
    return map.get(key);
  };
}

function colorPopover(anchor) {
  const current = sheet.data.status.color_by || '';
  const fields = colorableFields();
  const save = async (value) => {
    await api(`/api/processes/${sheet.pid}/statuses/${statusSlug(sheet.sid)}/view`,
      { method: 'PUT', body: { color_by: value } });
    toast(value ? `Colouring by ${value}` : 'Colouring off');
    viewSheet(sheet.pid, statusSlug(sheet.sid));
  };
  const list = h('div', { class: 'chooser-list' },
    h('div', { class: 'chooser-row' },
      h('span', { class: 'grow' }, 'No colouring'),
      h('button', { class: 'btn btn-sm', onclick: () => save('') }, current ? 'Use' : '✓')),
    fields.map((f) => h('div', { class: 'chooser-row' },
      h('span', { class: 'grow' }, f.name,
        h('span', { class: 'kind' }, `${f.distinct_count} values`)),
      h('button', {
        class: 'btn btn-sm' + (f.name === current ? ' btn-primary' : ''),
        onclick: () => save(f.name),
      }, f.name === current ? '✓' : 'Use'))));
  popover(anchor, 'Colour rows by', h('div', {},
    h('p', { class: 'hint' },
      'Tints each row by the value of one field, so different kinds of sample '
      + 'are easy to tell apart at a glance. Only fields with a small set of '
      + 'repeating values are offered.'),
    list), 380);
}

function renderLegend() {
  const host = document.getElementById('legend');
  if (!host) return;
  const field = sheet.data.status.color_by;
  host.innerHTML = '';
  if (!field) { host.classList.add('hidden'); return; }
  host.classList.remove('hidden');
  const scale = colorScale(field);
  const counts = new Map();
  sheet.rows.forEach((r) => {
    const key = r.color_value || '—';
    counts.set(key, (counts.get(key) || 0) + 1);
  });
  const sorted = [...counts.entries()].sort((a, b) => b[1] - a[1]);
  // Element.append does not flatten arrays the way the h() helper does.
  const chips = sorted.map(([value, n]) => {
    const [bar, tint] = scale(value === '—' ? '' : value);
    return h('span', {
      class: 'legend-chip',
      style: `background:${tint};border-color:${bar}`,
    }, h('span', { class: 'legend-dot', style: `background:${bar}` }),
      value, h('span', { class: 'legend-count' }, n.toLocaleString()));
  });
  host.append(h('span', { class: 'hint' }, `${field}:`), ...chips);
}

/* --- batch size --- */

function batchModal() {
  const status = sheet.data.status;
  let size = status.batch_size;
  const slots = JSON.parse(JSON.stringify((status.batch_composition || {}).slots || []));

  const sizeInput = h('input', {
    type: 'number', min: '1', style: 'width:110px',
    value: size ?? '', placeholder: 'none',
    oninput: (e) => { size = e.target.value ? Number(e.target.value) : null; },
  });

  const slotHost = h('div', {});
  const summary = h('p', { class: 'hint' }, '');

  const refreshSummary = () => {
    if (!size) { summary.textContent = 'Set a batch size to use a composition.'; return; }
    const fixed = slots.filter((s2) => s2.mode === 'count')
      .reduce((a, s2) => a + (Number(s2.count) || 0), 0);
    const share = Math.max(0, size - fixed);
    const pct = slots.filter((s2) => s2.mode === 'percent')
      .reduce((a, s2) => a + (Number(s2.percent) || 0), 0);
    const parts = [`${fixed} of ${size} reserved by fixed counts`, `${share} left to share`];
    if (pct) parts.push(`percentages total ${pct}%${pct === 100 ? '' : ' — not 100%'}`);
    summary.textContent = parts.join(' · ');
  };

  const redraw = () => {
    slotHost.innerHTML = '';
    slots.forEach((slot, index) => {
      const ruleHost = h('div', {});
      const amount = slot.mode === 'count'
        ? h('input', {
          type: 'number', min: '0', style: 'width:80px', value: slot.count ?? '',
          oninput: (e) => { slot.count = Number(e.target.value) || 0; refreshSummary(); },
        })
        : slot.mode === 'percent'
          ? h('input', {
            type: 'number', min: '0', max: '100', style: 'width:80px', value: slot.percent ?? '',
            oninput: (e) => { slot.percent = Number(e.target.value) || 0; refreshSummary(); },
          })
          : h('span', { class: 'hint' }, 'takes whatever is left that matches');

      const card = h('div', { class: 'slot' },
        h('div', { class: 'slot-head' },
          h('span', { class: 'order' }, index + 1),
          h('input', {
            type: 'text', placeholder: 'Slot name, e.g. Clinical',
            value: slot.label || '',
            oninput: (e) => { slot.label = e.target.value; },
          }),
          h('select', {
            onchange: (e) => { slot.mode = e.target.value; redraw(); refreshSummary(); },
          }, [['count', 'exactly N'], ['percent', 'N% of what is left'], ['fill', 'the rest']]
            .map(([v, label]) => h('option', { value: v, selected: slot.mode === v }, label))),
          amount,
          h('button', {
            class: 'btn btn-sm', title: 'Move up', disabled: index === 0,
            onclick: () => { slots.splice(index - 1, 0, slots.splice(index, 1)[0]); redraw(); },
          }, '↑'),
          h('button', {
            class: 'x', title: 'Remove slot',
            onclick: () => { slots.splice(index, 1); redraw(); refreshSummary(); },
          }, '×')),
        h('div', { class: 'slot-rule' },
          h('span', { class: 'hint' }, 'Samples that match:'), ruleHost));
      slotHost.appendChild(card);
      slot.rule = asGroup(slot.rule);
      renderCondition(ruleHost, slot.rule, (structural = true) => {
        if (structural !== false) redraw();
      });
    });
    if (!slots.length) {
      slotHost.appendChild(h('p', { class: 'hint' },
        'No composition. Batches are just the next N samples in priority order.'));
    }
  };

  const panel = h('div', { class: 'modal wide' },
    h('div', { class: 'row' },
      h('h2', { style: 'flex:1' }, 'Batches — ', sheet.data.status.name),
      h('button', { class: 'btn btn-sm', onclick: () => panel.remove() }, 'Close')),

    h('p', { class: 'hint' },
      'A batch is how many samples the team runs together. Batches are counted '
      + 'down from the top of the priority order, so batch 1 is always the next run.'),
    h('div', { class: 'row', style: 'margin:8px 0 4px' },
      h('strong', {}, 'Batch size'), sizeInput, h('span', { class: 'hint' }, 'samples'),
      [8, 24, 48, 96, 384].map((n) => h('button', {
        class: 'btn btn-sm',
        onclick: () => { sizeInput.value = n; size = n; refreshSummary(); },
      }, n))),

    h('h2', { style: 'margin-top:18px' }, 'Ideal composition'),
    h('p', { class: 'hint' },
      'Optional. Slots claim samples in the order listed, so put the ones that '
      + 'must be satisfied first. Priority order decides which matching sample '
      + 'gets claimed. If a slot cannot be filled the shortfall is shown on the '
      + 'batch rather than hidden.'),
    summary,
    slotHost,
    h('div', { class: 'row', style: 'margin-top:10px' },
      h('button', {
        class: 'btn btn-sm',
        onclick: () => {
          slots.push({ label: `Slot ${slots.length + 1}`, mode: 'count', count: 8,
            rule: newGroup('AND') });
          redraw(); refreshSummary();
        },
      }, '+ Add slot'),
      h('button', {
        class: 'btn btn-sm',
        title: 'A worked example you can edit',
        onclick: () => {
          slots.length = 0;
          slots.push(
            { label: 'Clinical', mode: 'count', count: 24,
              rule: { type: 'group', op: 'AND', children: [
                { type: 'cond', field: 'Is Clinical', kind: 'bool', op: 'is_true', value: null }] } },
            { label: 'Everything else', mode: 'fill', rule: newGroup('AND') });
          redraw(); refreshSummary();
        },
      }, 'Use an example')),

    h('div', { class: 'row', style: 'margin-top:18px' },
      h('button', {
        class: 'btn btn-primary',
        onclick: async () => {
          await api(`/api/processes/${sheet.pid}/statuses/${statusSlug(sheet.sid)}/view`, {
            method: 'PUT',
            body: { batch_size: size, batch_composition: { slots } },
          });
          panel.remove();
          toast(size ? `Batches of ${size}` : 'Batching off');
          viewSheet(sheet.pid, statusSlug(sheet.sid));
        },
      }, 'Save'),
      status.batch_size && h('button', {
        class: 'btn',
        onclick: async () => {
          await api(`/api/processes/${sheet.pid}/statuses/${statusSlug(sheet.sid)}/view`, {
            method: 'PUT', body: { batch_size: null, batch_composition: {} },
          });
          panel.remove();
          toast('Batching off');
          viewSheet(sheet.pid, statusSlug(sheet.sid));
        },
      }, 'Turn batching off')));

  document.body.appendChild(panel);
  redraw();
  refreshSummary();
}

/* --- table --- */

function renderTable() {
  const host = document.getElementById('sheet-scroll');
  const columns = sheet.data.status.columns;
  const batchSize = sheet.data.status.batch_size || 0;
  const slice = sheet.rows.slice(0, sheet.rendered);

  // "new" only reads as useful when it marks a subset. On a queue that was
  // just built every row arrived today, and the marker is pure noise.
  const freshCount = sheet.rows.filter((r) => daysSince(r.entered_status_at) === 0).length;
  const markFresh = freshCount > 0 && freshCount < sheet.rows.length;

  const allShownSelected = slice.length > 0 && slice.every((r) => sheet.selected.has(r.sample_key));
  const head = h('tr', {},
    h('th', { class: 'col-check' },
      h('input', {
        type: 'checkbox', checked: allShownSelected || null,
        title: 'Select everything shown',
        onchange: (e) => {
          slice.forEach((r) => (e.target.checked
            ? sheet.selected.add(r.sample_key) : sheet.selected.delete(r.sample_key)));
          sheet.lastChecked = null;
          renderTable();
        },
      })),
    h('th', { class: 'col-grip' }, ''),
    h('th', { class: 'col-rank', title: 'Saved priority order' }, '#'),
    columns.map((c, i) => h('th', {
      title: `${c} — click to select the whole column`,
      onclick: () => selectColumn(i),
    }, c)),
    h('th', {}, 'Note'),
    h('th', { class: 'col-actions' }, ''));

  const scale = sheet.data.status.color_by ? colorScale(sheet.data.status.color_by) : null;
  const plan = sheet.data.batch_plan || [];
  const body = h('tbody', {});
  let lastBatch = null;
  slice.forEach((row, index) => {
    // Batches come from the server, which composes them; grouping here by
    // row.batch keeps the display honest when slots pull samples forward.
    if (batchSize && row.batch && row.batch !== lastBatch) {
      lastBatch = row.batch;
      body.appendChild(batchHeaderRow(row.batch, plan, columns.length + 5));
    }
    body.appendChild(rowEl(row, index, columns, markFresh, scale));
  });

  host.innerHTML = '';
  host.appendChild(h('table', { class: 'sheet' }, h('thead', {}, head), body));

  const more = document.getElementById('sheet-more');
  more.innerHTML = '';
  if (sheet.rendered < sheet.rows.length) {
    more.append(
      h('button', {
        class: 'btn',
        onclick: () => {
          sheet.rendered = Math.min(sheet.rows.length, sheet.rendered + sheet.pageSize);
          renderTable();
        },
      }, `Show ${Math.min(sheet.pageSize, sheet.rows.length - sheet.rendered)} more`),
      h('span', { class: 'hint' },
        `${sheet.rendered.toLocaleString()} of ${sheet.rows.length.toLocaleString()} rendered`));
  } else if (sheet.data.truncated) {
    more.appendChild(h('span', { class: 'hint' },
      `Showing the first ${sheet.rows.length.toLocaleString()} by priority. Narrow with search to see the rest.`));
  }
  renderSelectionBar();
  renderLegend();
  paintCellRange();
  document.getElementById('save-order').classList.toggle('hidden', !sheet.dirty);
}

function batchHeaderRow(number, plan, span) {
  const info = plan.find((b) => b.number === number);
  const short = info ? info.slots.filter((s) => s.short > 0) : [];
  const partial = info && info.size < info.capacity;
  return h('tr', { class: 'batch-row' },
    h('td', { colspan: span },
      h('span', { class: 'batch-name' }, `Batch ${number}`),
      info && h('span', { class: 'batch-range' }, `${info.size} of ${info.capacity}`),
      info && info.slots.filter((s) => s.got > 0 || s.want).map((s) =>
        h('span', {
          class: 'slot-chip' + (s.short ? ' short' : ''),
          title: s.short ? `${s.short} short of ${s.want}` : '',
        }, s.label || '(unnamed)',
          h('b', {}, s.want === null ? s.got : `${s.got}/${s.want}`))),
      info && info.filler > 0 && h('span', {
        class: 'slot-chip filler',
        title: 'Topped up with samples matching no slot, to avoid leaving them unqueued',
      }, 'filler', h('b', {}, info.filler)),
      partial && h('span', { class: 'badge warn' }, 'partial batch'),
      short.length > 0 && h('span', { class: 'badge warn' },
        `${short.reduce((a, s) => a + s.short, 0)} short of spec`),
      h('button', {
        class: 'btn btn-sm', title: 'Tick every sample in this batch',
        onclick: () => selectBatchNumber(number),
      }, 'Select batch')));
}

function rowEl(row, index, columns, markFresh, scale) {
  const selected = sheet.selected.has(row.sample_key);
  const age = daysSince(row.entered_status_at);

  const tint = scale ? scale(row.color_value) : null;
  const tr = h('tr', {
    class: (selected ? 'selected ' : '') + (row.pinned ? 'pinned ' : '')
      + (markFresh && age === 0 ? 'fresh-row ' : '') + (tint ? 'tinted' : ''),
    style: tint ? `--row-bar:${tint[0]};--row-tint:${tint[1]}` : null,
    title: tint && row.color_value ? `${sheet.data.status.color_by}: ${row.color_value}` : null,
    'data-key': row.sample_key,
    'data-row': index,
    ondragstart: (e) => {
      // Only the grip starts a drag; anywhere else must stay selectable
      // text, otherwise copying a column of IDs is impossible.
      if (!sheet.dragArmed) { e.preventDefault(); return; }
      sheet.dragKey = row.sample_key;
      tr.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
    },
    ondragend: () => { tr.classList.remove('dragging'); sheet.dragArmed = false; },
    ondragover: (e) => { e.preventDefault(); tr.classList.add('drop-target'); },
    ondragleave: () => tr.classList.remove('drop-target'),
    ondrop: (e) => { e.preventDefault(); tr.classList.remove('drop-target'); dropRow(index); },
  },
    h('td', { class: 'col-check' },
      h('input', {
        type: 'checkbox', checked: selected || null,
        onclick: (e) => {
          e.stopPropagation();
          toggleChecked(index, e.target.checked, e.shiftKey);
        },
      })),
    h('td', {
      class: 'col-grip', title: 'Drag to reorder',
      onmousedown: () => { sheet.dragArmed = true; },
      onmouseup: () => { sheet.dragArmed = false; },
    }, '⠿'),
    h('td', { class: 'col-rank' }, row.rank,
      row.pinned ? h('span', { class: 'pin-dot', title: 'Pinned to the top' }, '●') : null),
    columns.map((c, col) => h('td', {
      class: 'cell',
      'data-row': index, 'data-col': col,
      title: String(row.cells[c] ?? ''),
      onmousedown: (e) => startCellSelect(e, index, col),
      onmouseenter: (e) => extendCellSelect(e, index, col),
    }, formatCell(row.cells[c]))),
    h('td', { class: 'col-note' }, row.note || ''),
    h('td', { class: 'col-actions' },
      h('button', {
        class: 'btn btn-sm', title: 'Move to the top of this queue',
        onclick: () => moveRowTo(index, 0),
      }, '⤒'),
      h('button', {
        class: 'btn btn-sm', title: row.pinned ? 'Unpin' : 'Pin to the top',
        onclick: () => togglePin(row),
      }, row.pinned ? '📌' : '📍'),
      h('button', {
        class: 'btn btn-sm', title: 'Add or edit a note',
        onclick: () => editNote(row),
      }, '✎')));
  return tr;
}

/* --- checkbox selection, with shift-click ranges --- */

function toggleChecked(index, checked, shiftKey) {
  const apply = (i) => {
    const key = sheet.rows[i].sample_key;
    if (checked) sheet.selected.add(key); else sheet.selected.delete(key);
  };
  if (shiftKey && sheet.lastChecked !== null && sheet.lastChecked !== index) {
    const [lo, hi] = [sheet.lastChecked, index].sort((a, b) => a - b);
    for (let i = lo; i <= hi; i += 1) apply(i);
  } else {
    apply(index);
  }
  sheet.lastChecked = index;
  renderTable();
}

function selectBatchNumber(number) {
  let last = null;
  sheet.rows.forEach((r, i) => {
    if (r.batch === number) { sheet.selected.add(r.sample_key); last = i; }
  });
  sheet.lastChecked = last;
  renderTable();
}

/* --- cell range selection and copy --- */

function startCellSelect(event, row, col) {
  if (event.button !== 0) return;
  if (event.shiftKey && sheet.cellAnchor) {
    sheet.cellFocus = { row, col };
  } else {
    sheet.cellAnchor = { row, col };
    sheet.cellFocus = { row, col };
  }
  sheet.cellDragging = true;
  // Let the browser's own text selection go; ours is a cell overlay.
  event.preventDefault();
  paintCellRange();
}

function extendCellSelect(event, row, col) {
  if (!sheet.cellDragging) return;
  sheet.cellFocus = { row, col };
  paintCellRange();
}

function cellRange() {
  if (!sheet.cellAnchor || !sheet.cellFocus) return null;
  const r = [sheet.cellAnchor.row, sheet.cellFocus.row].sort((a, b) => a - b);
  const c = [sheet.cellAnchor.col, sheet.cellFocus.col].sort((a, b) => a - b);
  return { r0: r[0], r1: r[1], c0: c[0], c1: c[1] };
}

function paintCellRange() {
  const range = cellRange();
  document.querySelectorAll('td.cell.picked').forEach((td) => td.classList.remove('picked'));
  const note = document.getElementById('copy-hint');
  if (!range) { if (note) note.classList.add('hidden'); return; }
  let count = 0;
  document.querySelectorAll('td.cell').forEach((td) => {
    const row = Number(td.dataset.row);
    const col = Number(td.dataset.col);
    if (row >= range.r0 && row <= range.r1 && col >= range.c0 && col <= range.c1) {
      td.classList.add('picked');
      count += 1;
    }
  });
  if (note) {
    note.classList.remove('hidden');
    note.textContent = `${count.toLocaleString()} cell${count === 1 ? '' : 's'} selected — ${copyKeyLabel()} to copy`;
  }
}

function selectColumn(col) {
  sheet.cellAnchor = { row: 0, col };
  sheet.cellFocus = { row: Math.max(0, sheet.rendered - 1), col };
  paintCellRange();
}

const copyKeyLabel = () => (navigator.platform.toLowerCase().includes('mac') ? '⌘C' : 'Ctrl+C');

/** Tab-separated, so it pastes straight into a spreadsheet. */
function cellRangeText() {
  const range = cellRange();
  if (!range) return '';
  const columns = sheet.data.status.columns;
  const lines = [];
  for (let r = range.r0; r <= range.r1 && r < sheet.rows.length; r += 1) {
    const cells = [];
    for (let c = range.c0; c <= range.c1; c += 1) {
      cells.push(String(sheet.rows[r].cells[columns[c]] ?? ''));
    }
    lines.push(cells.join('\t'));
  }
  return lines.join('\n');
}

async function copyCellRange() {
  const text = cellRangeText();
  if (!text) return false;
  const rows = text.split('\n').length;
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    // Clipboard API needs a secure context; fall back to a hidden textarea.
    const scratch = document.createElement('textarea');
    scratch.value = text;
    document.body.appendChild(scratch);
    scratch.select();
    document.execCommand('copy');
    scratch.remove();
  }
  toast(`Copied ${rows.toLocaleString()} row${rows === 1 ? '' : 's'}`);
  return true;
}

function formatCell(value) {
  if (value === null || value === undefined || value === '') return '';
  const text = String(value);
  // Trim the time off ISO-ish timestamps; the date is what people scan for.
  const match = text.match(/^(\d{4}-\d{2}-\d{2})[ T]\d{2}:\d{2}/);
  return match ? match[1] : text;
}

/* --- reordering --- */

function dropRow(targetIndex) {
  const key = sheet.dragKey;
  sheet.dragKey = null;
  if (!key) return;
  const from = sheet.rows.findIndex((r) => r.sample_key === key);
  if (from < 0 || from === targetIndex) return;
  moveRowTo(from, targetIndex);
}

function moveRowTo(from, to) {
  const [moved] = sheet.rows.splice(from, 1);
  sheet.rows.splice(to > from ? to - 1 : to, 0, moved);
  stablePinFirst(sheet.rows);
  sheet.rows.forEach((r, i) => { r.rank = i + 1; });
  sheet.dirty = true;
  sheet.rendered = Math.max(sheet.rendered, Math.min(sheet.rows.length, to + 1));
  renderTable();
}

/** Pinned rows float to the top, order within each half preserved.
    The server serves them that way, so the table has to agree or a drag
    past a pinned row would silently revert on reload. */
function stablePinFirst(list) {
  const pinned = list.filter((r) => r.pinned);
  const rest = list.filter((r) => !r.pinned);
  list.length = 0;
  list.push(...pinned, ...rest);
}

async function saveSheetOrder() {
  const btn = document.getElementById('save-order');
  btn.disabled = true;
  try {
    await api(`/api/processes/${sheet.pid}/order`, {
      method: 'POST',
      body: { status_id: statusSlug(sheet.sid), sample_keys: sheet.rows.map((r) => r.sample_key) },
    });
    sheet.dirty = false;
    toast('Priority saved');
    // Batches are composed from the priority order, so they have to be
    // recomputed server-side rather than guessed at here.
    if (sheet.data.status.batch_size) await viewSheet(sheet.pid, statusSlug(sheet.sid));
    else renderTable();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
}

/* --- per-row actions --- */

async function togglePin(row) {
  try {
    await api(`/api/processes/${sheet.pid}/items/${encodeURIComponent(row.sample_key)}`,
      { method: 'POST', body: { pinned: !row.pinned } });
    row.pinned = !row.pinned;
    stablePinFirst(sheet.rows);
    sheet.rows.forEach((r, i) => { r.rank = i + 1; });
    sheet.dirty = true;
    renderTable();
  } catch (e) { toast(e.message, true); }
}

async function editNote(row) {
  const note = prompt(`Note for ${row.sample_id || row.sample_key}`, row.note || '');
  if (note === null) return;
  try {
    await api(`/api/processes/${sheet.pid}/items/${encodeURIComponent(row.sample_key)}`,
      { method: 'POST', body: { note } });
    row.note = note;
    renderTable();
  } catch (e) { toast(e.message, true); }
}

/* --- selection --- */

function renderSelectionBar() {
  const bar = document.getElementById('selbar');
  const count = sheet.selected.size;
  bar.classList.toggle('hidden', count === 0);
  if (!count) return;
  bar.innerHTML = '';
  bar.append(
    h('strong', {}, `${count.toLocaleString()} selected`),
    h('button', { class: 'btn btn-sm', onclick: () => moveSelectionToTop() }, 'Move to top'),
    h('button', { class: 'btn btn-sm', onclick: () => bulkPin(true) }, 'Pin'),
    h('button', { class: 'btn btn-sm', onclick: () => bulkPin(false) }, 'Unpin'),
    h('span', { class: 'hint' }, 'Send to:'),
    h('select', {
      onchange: (e) => { if (e.target.value) bulkMove(e.target.value); e.target.value = ''; },
    }, [h('option', { value: '' }, 'choose a status…'),
      ...sheet.data.siblings
        .filter((s) => s.id !== sheet.sid)
        .map((s) => h('option', { value: statusSlug(s.id) }, s.name))]),
    h('span', { class: 'spacer' }),
    h('button', {
      class: 'btn btn-sm btn-ghost',
      onclick: () => { sheet.selected.clear(); renderTable(); },
    }, 'Clear selection'));
}

function moveSelectionToTop() {
  const chosen = sheet.rows.filter((r) => sheet.selected.has(r.sample_key));
  const rest = sheet.rows.filter((r) => !sheet.selected.has(r.sample_key));
  sheet.rows = chosen.concat(rest);
  stablePinFirst(sheet.rows);
  sheet.rows.forEach((r, i) => { r.rank = i + 1; });
  sheet.dirty = true;
  sheet.rendered = Math.max(sheet.rendered, Math.min(sheet.rows.length, chosen.length));
  renderTable();
}

async function bulkPin(pinned) {
  const keys = [...sheet.selected];
  try {
    await api(`/api/processes/${sheet.pid}/bulk`,
      { method: 'POST', body: { action: 'pin', sample_keys: keys, pinned } });
    sheet.rows.forEach((r) => { if (sheet.selected.has(r.sample_key)) r.pinned = pinned; });
    stablePinFirst(sheet.rows);
    sheet.rows.forEach((r, i) => { r.rank = i + 1; });
    sheet.dirty = true;
    toast(`${keys.length} ${pinned ? 'pinned' : 'unpinned'}`);
    renderTable();
  } catch (e) { toast(e.message, true); }
}

async function bulkMove(targetSlug) {
  const keys = [...sheet.selected];
  const target = sheet.data.siblings.find((s) => statusSlug(s.id) === targetSlug);
  const ok = confirm(
    `Move ${keys.length} sample${keys.length > 1 ? 's' : ''} to "${target.name}"?\n\n`
    + 'This is a manual override. The next data refresh will put them back where the '
    + 'rules say they belong, unless you change the rules.');
  if (!ok) { renderTable(); return; }
  try {
    const out = await api(`/api/processes/${sheet.pid}/bulk`,
      { method: 'POST', body: { action: 'move', sample_keys: keys, status_id: targetSlug } });
    toast(`${out.moved} moved to ${out.to}`);
    sheet.selected.clear();
    viewSheet(sheet.pid, statusSlug(sheet.sid));
  } catch (e) { toast(e.message, true); }
}

/* ------------------------------------------------- global sample search -- */

/** "Where is SM-XXXXX?" -- every queue the sample sits in, across processes. */
async function lookupSample(term) {
  const panel = document.getElementById('lookup-panel');
  const value = (term || '').trim();
  if (!value) { panel.classList.add('hidden'); return; }

  panel.classList.remove('hidden');
  panel.innerHTML = '';
  panel.appendChild(h('div', { class: 'hint' }, 'Searching…'));

  let out;
  try {
    out = await api(`/api/samples/lookup?q=${encodeURIComponent(value)}`);
  } catch (e) {
    panel.innerHTML = '';
    panel.appendChild(h('div', { class: 'hint' }, e.message));
    return;
  }

  panel.innerHTML = '';
  panel.append(
    h('div', { class: 'lookup-head' },
      h('strong', {}, out.samples.length
        ? `${out.samples.length} match${out.samples.length === 1 ? '' : 'es'} for “${out.term}”`
        : `Nothing matches “${out.term}”`),
      h('button', {
        class: 'x',
        onclick: () => { panel.classList.add('hidden'); document.getElementById('lookup').value = ''; },
      }, '×')));

  if (!out.samples.length) {
    panel.appendChild(h('div', { class: 'hint' },
      'Try a sample id (SM-…), an order id (PDO-…), or part of either.'));
    return;
  }

  out.samples.forEach((sample) => {
    const rows = sample.placements.length
      ? sample.placements.map((p) => h('a', {
        class: 'placement',
        href: `#/p/${p.process_id}/s/${statusSlug(p.status_id)}`,
        onclick: () => { panel.classList.add('hidden'); },
      },
        h('span', { class: 'swatch', style: `background:${p.color}` }),
        h('span', { class: 'placement-process' }, p.process),
        h('span', { class: 'placement-status' }, p.status),
        h('span', { class: 'placement-rank' }, `#${p.rank}`),
        p.pinned && h('span', { class: 'badge' }, 'pinned'),
        p.note && h('span', { class: 'placement-note' }, p.note)))
      : [h('div', { class: 'hint' },
        'In no process. Nothing claims it — check the scope rules.')];

    panel.appendChild(h('div', { class: 'lookup-sample' },
      h('div', { class: 'lookup-id' },
        h('strong', {}, sample.sample_id || sample.sample_key),
        h('span', { class: 'hint' }, sample.order_id),
        h('span', { class: 'hint' }, sample.product_name)),
      h('div', { class: 'lookup-facts' },
        Object.entries(sample.highlights).map(([k, v]) =>
          h('span', { class: 'fact' }, h('span', { class: 'fact-key' }, k), String(v)))),
      h('div', { class: 'placements' }, rows)));
  });
}

/* ------------------------------------------------------------ log ------ */

async function showLog(processId) {
  const events = await api(`/api/processes/${processId}/events?limit=150`);
  const panel = h('div', { class: 'modal' },
    h('div', { class: 'row' },
      h('h2', { style: 'flex:1' }, 'Recent changes'),
      h('button', { class: 'btn btn-sm', onclick: () => panel.remove() }, 'Close')),
    h('table', { class: 'log' },
      h('tr', {}, ['When', 'What', 'Sample', 'From', 'To', 'By'].map((t) => h('th', {}, t))),
      events.map((e) => h('tr', {},
        h('td', {}, new Date(e.at).toLocaleString()),
        h('td', {}, e.kind),
        h('td', {}, e.sample_key),
        h('td', {}, e.from_status || ''),
        h('td', {}, e.to_status || ''),
        h('td', {}, e.actor)))));
  document.body.appendChild(panel);
}

/* -------------------------------------------------------------- routing -- */

async function route() {
  const hash = location.hash || '#/';
  const sheetMatch = hash.match(/^#\/p\/(\d+)\/s\/([\w-]+)/);
  const rulesMatch = hash.match(/^#\/p\/(\d+)\/rules/);
  const procMatch = hash.match(/^#\/p\/(\d+)/);
  try {
    if (sheetMatch) {
      if (Number(sheetMatch[1]) !== sheet.pid || sheetMatch[2] !== statusSlug(sheet.sid)) sheet.search = '';
      await viewSheet(Number(sheetMatch[1]), sheetMatch[2]);
    } else if (rulesMatch) await viewRules(Number(rulesMatch[1]));
    else if (procMatch) await viewOverview(Number(procMatch[1]));
    else await viewProcesses();
  } catch (e) {
    view().innerHTML = '';
    view().appendChild(h('div', { class: 'empty' },
      h('h2', {}, 'Could not load that'), h('p', {}, e.message)));
  }
}

/** Deployed behind IAP the identity is fixed and verified, so show it
    instead of a box anyone can type into. */
function renderIdentity() {
  const identity = state.meta.identity || { mode: 'open', editable: true };
  const label = document.querySelector('.who');
  if (identity.editable) return;
  label.innerHTML = '';
  label.append(
    h('span', { class: 'signed-in', title: `Signed in via ${identity.mode}` },
      identity.email || 'signed in'));

  // Trusted-header mode is the halfway house after deploying. Surface the
  // exact audience string so finishing the setup is copy-and-paste.
  if (identity.mode === 'iap-header' && identity.suggested_audience) {
    document.body.insertBefore(
      h('div', { class: 'setup-banner' },
        h('strong', {}, 'Signatures are not being verified. '),
        'Finish the setup by redeploying with ',
        h('code', {}, `IAP_AUDIENCE=${identity.suggested_audience}`)),
      document.body.firstChild);
  }
}

async function boot() {
  const actorInput = document.getElementById('actor');
  actorInput.value = state.actor;
  actorInput.addEventListener('input', (e) => {
    state.actor = e.target.value;
    localStorage.setItem('sq-actor', state.actor);
  });
  document.getElementById('refresh-btn').addEventListener('click', refreshData);
  window.addEventListener('hashchange', route);

  const lookup = document.getElementById('lookup');
  const runLookup = debounce(() => lookupSample(lookup.value), 300);
  lookup.addEventListener('input', runLookup);
  lookup.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') lookupSample(lookup.value);
    if (e.key === 'Escape') { lookup.value = ''; lookupSample(''); lookup.blur(); }
  });

  document.addEventListener('mouseup', () => { sheet.cellDragging = false; });

  document.addEventListener('keydown', async (e) => {
    const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName);
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'c' && !typing) {
      // Only intercept when our own cell range is live; otherwise leave the
      // browser's ordinary text copy alone.
      if (cellRange()) { e.preventDefault(); await copyCellRange(); }
    }
    if (e.key === 'Escape' && !typing) {
      sheet.cellAnchor = null;
      sheet.cellFocus = null;
      paintCellRange();
    }
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      document.getElementById('lookup').focus();
    }
  });

  state.meta = await api('/api/meta');
  renderIdentity();
  renderSnapshot();
  if (!state.meta.fields.length) {
    view().innerHTML = '';
    view().appendChild(h('div', { class: 'empty' },
      h('h2', {}, 'No data loaded'),
      h('p', {}, 'Click "Refresh data" to pull open orders from the configured source.')));
    return;
  }
  route();
}

boot();
