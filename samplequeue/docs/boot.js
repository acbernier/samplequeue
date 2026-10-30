/* Runs the SampleQueue Python backend inside the page (Pyodide) and answers
   the app's /api/... requests locally. No server involved.

   The whole SQLite database is one file. After every change it is copied
   into this browser's IndexedDB, and restored on the next visit. */

const PYODIDE_URL = 'https://cdn.jsdelivr.net/pyodide/v0.26.4/full/';
const PY_FILES = [
  'sqshim.py', 'sources.json', 'seed_demo.py',
  'app/__init__.py', 'app/auth.py', 'app/db.py', 'app/main.py', 'app/queues.py',
  'app/rules.py', 'app/sources.py', 'app/synth.py',
];
const DB_PATH = '/tmp/sq/samplequeue.db';
const IDB_NAME = 'samplequeue-demo';

/* ------------------------------------------------------------ IndexedDB -- */

function idb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(IDB_NAME, 1);
    req.onupgradeneeded = () => req.result.createObjectStore('kv');
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
async function idbGet(key) {
  try {
    const db = await idb();
    return await new Promise((resolve) => {
      const r = db.transaction('kv').objectStore('kv').get(key);
      r.onsuccess = () => resolve(r.result);
      r.onerror = () => resolve(undefined);
    });
  } catch (e) { return undefined; }
}
async function idbPut(key, value) {
  try {
    const db = await idb();
    await new Promise((resolve) => {
      const tx = db.transaction('kv', 'readwrite');
      value === null ? tx.objectStore('kv').delete(key) : tx.objectStore('kv').put(value, key);
      tx.oncomplete = resolve; tx.onerror = resolve;
    });
  } catch (e) { /* private mode: the demo still works, it just won't persist */ }
}

/* -------------------------------------------------------------- backend -- */

const say = (msg) => { document.getElementById('sq-loading-msg').textContent = msg; };
let pyodide;
let handle;
let saveTimer;

function scheduleSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    await idbPut('db', pyodide.FS.readFile(DB_PATH));
  }, 400);
}

async function startBackend() {
  say('Loading Python…');
  const { loadPyodide } = await import(PYODIDE_URL + 'pyodide.mjs');
  pyodide = await loadPyodide({ indexURL: PYODIDE_URL });
  await pyodide.loadPackage('sqlite3'); // part of Python, but shipped as an add-on here

  say('Loading SampleQueue…');
  pyodide.FS.mkdirTree('/home/pyodide/py/app');
  pyodide.FS.mkdirTree('/tmp/sq');
  await Promise.all(PY_FILES.map(async (f) => {
    const res = await fetch('py/' + f);
    if (!res.ok) throw new Error('could not load ' + f);
    pyodide.FS.writeFile('/home/pyodide/py/' + f, await res.text());
  }));

  const saved = await idbGet('db');
  if (saved) pyodide.FS.writeFile(DB_PATH, saved);

  pyodide.runPython(`
import os, sys
sys.path.insert(0, "/home/pyodide/py")
os.chdir("/home/pyodide/py")
os.environ["SAMPLEQUEUE_DB"] = "${DB_PATH}"
os.environ["SAMPLEQUEUE_JOURNAL"] = "DELETE"
import sqshim
sqshim.start()
from app import db
db.init()
`);

  if (!saved) {
    say('Creating demo data (first visit only)…');
    await new Promise((r) => setTimeout(r, 50)); // let the message paint
    pyodide.runPython(`
from app import sources
import seed_demo
sources.ingest("Synthetic feed")
seed_demo.seed()
`);
    scheduleSave();
  }
  handle = pyodide.runPython('sqshim.handle');
}

/* ---------------------------------------------------------------- fetch -- */

const realFetch = window.fetch.bind(window);
let ready;

function call(method, url, body, actor) {
  const out = JSON.parse(handle(method, url, body || '', actor || ''));
  if (method !== 'GET') scheduleSave();
  return out;
}

window.fetch = async (input, opts = {}) => {
  // Callers pass a string, a URL object or a Request; only act on our own /api paths.
  const url = typeof input === 'string' ? input : String(input.url || input.href || '');
  if (!url.startsWith('/api/')) return realFetch(input, opts);
  await ready;
  const method = (opts.method || 'GET').toUpperCase();
  const actor = (opts.headers && opts.headers['X-Actor']) || '';
  await new Promise((r) => setTimeout(r, 0)); // keep the UI responsive
  const out = call(method, url, opts.body, actor);
  return new Response(out.body, {
    status: out.status,
    headers: { 'Content-Type': out.type, ...out.headers },
  });
};

/* The app links straight to /api/.../export.csv; serve those locally too. */
document.addEventListener('click', async (e) => {
  const a = e.target.closest && e.target.closest('a[href^="/api/"]');
  if (!a) return;
  e.preventDefault();
  const out = call('GET', a.getAttribute('href'), '', '');
  const disposition = (out.headers['Content-Disposition'] || '').match(/filename="([^"]+)"/);
  const link = document.createElement('a');
  link.href = URL.createObjectURL(new Blob([out.body], { type: out.type }));
  link.download = disposition ? disposition[1] : 'export.csv';
  link.click();
  URL.revokeObjectURL(link.href);
});

document.getElementById('sq-reset').addEventListener('click', async () => {
  if (!confirm('Reset the demo? Your saved edits in this browser will be erased.')) return;
  await idbPut('db', null);
  localStorage.removeItem('sq-actor');
  location.reload();
});

/* ----------------------------------------------------------------- start -- */

ready = startBackend();
ready.then(() => {
  const s = document.createElement('script');
  s.src = 'app.js';
  document.body.appendChild(s);
  document.getElementById('sq-loading').remove();
}).catch((err) => {
  say('Could not start: ' + err.message);
  console.error(err);
});
