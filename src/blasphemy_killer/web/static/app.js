"use strict";

// State is deliberately tiny: the current directory listing and the jobs we
// know about, both keyed so SSE messages can patch them in place.
const state = {
  path: "",
  entries: [],
  jobs: new Map(),
  selected: new Set(),
};

const $ = (id) => document.getElementById(id);

// --- helpers ---------------------------------------------------------------

function humanSize(bytes) {
  if (!bytes) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, n = bytes;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n < 10 && i > 0 ? n.toFixed(1) : Math.round(n)} ${units[i]}`;
}

function timestamp(seconds) {
  const ms = Math.round(seconds * 1000);
  const h = Math.floor(ms / 3600000);
  const m = Math.floor((ms % 3600000) / 60000);
  const s = Math.floor((ms % 60000) / 1000);
  const pad = (v) => String(v).padStart(2, "0");
  return `${pad(h)}:${pad(m)}:${pad(s)}`;
}

// Mirrors cli._mask: keep each word's first character, star out the rest, so
// the UI never spells out what it is censoring.
function mask(text) {
  return text.replace(/(?<=[\w'])[\w']/g, "*");
}

async function api(path, options) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* not json */ }
    throw new Error(detail);
  }
  return res.json();
}

// --- library browser -------------------------------------------------------

async function browse(path) {
  const data = await api(`/api/browse?path=${encodeURIComponent(path || "")}`);
  state.path = data.path;
  state.parent = data.parent;
  state.entries = data.entries;
  state.selected.clear();
  renderCrumbs();
  renderEntries();
  updateQueueButton();
}

function renderCrumbs() {
  const crumbs = $("crumbs");
  crumbs.replaceChildren();
  const parts = state.path ? state.path.split("/") : [];

  const root = document.createElement("a");
  root.textContent = "media";
  root.onclick = () => browse("");
  crumbs.append(root);

  parts.forEach((part, i) => {
    const sep = document.createElement("span");
    sep.className = "sep";
    sep.textContent = "/";
    const link = document.createElement("a");
    link.textContent = part;
    const target = parts.slice(0, i + 1).join("/");
    link.onclick = () => browse(target);
    crumbs.append(sep, link);
  });
}

function renderEntries() {
  const tbody = $("entries");
  tbody.replaceChildren();
  $("empty").hidden = state.entries.length > 0;

  if (state.parent !== null && state.parent !== undefined) {
    tbody.append(dirRow("..", state.parent));
  }

  for (const entry of state.entries) {
    tbody.append(entry.is_dir ? dirRow(entry.name, entry.path) : fileRow(entry));
  }
  $("select-all").checked = false;
}

function dirRow(label, target) {
  const tr = document.createElement("tr");
  tr.innerHTML = `<td></td><td class="name"></td><td></td><td class="col-size"></td>`;
  const link = document.createElement("a");
  link.textContent = `${label}/`;
  link.onclick = () => browse(target);
  tr.children[1].append(link);
  return tr;
}

function fileRow(entry) {
  const tr = document.createElement("tr");
  tr.dataset.path = entry.path;

  const check = document.createElement("input");
  check.type = "checkbox";
  check.checked = state.selected.has(entry.path);
  check.onchange = () => {
    check.checked ? state.selected.add(entry.path) : state.selected.delete(entry.path);
    updateQueueButton();
  };

  const tdCheck = document.createElement("td");
  tdCheck.append(check);

  const tdName = document.createElement("td");
  tdName.className = "name";
  const span = document.createElement("span");
  span.className = "fname";
  span.textContent = entry.name;          // textContent: filenames are untrusted
  span.title = entry.name;
  tdName.append(span);

  const tdStatus = document.createElement("td");
  tdStatus.className = "col-status";
  tdStatus.append(statusBadge(entry.cleaned));

  const tdSize = document.createElement("td");
  tdSize.className = "col-size";
  tdSize.textContent = humanSize(entry.size);

  tr.append(tdCheck, tdName, tdStatus, tdSize);
  return tr;
}

function statusBadge(cleaned) {
  const badge = document.createElement("span");
  if (cleaned === null || cleaned === undefined) {
    badge.className = "badge pending";
    badge.textContent = "checking…";
  } else if (cleaned) {
    badge.className = "badge clean";
    badge.textContent = "cleaned";
  } else {
    badge.className = "badge";
    badge.textContent = "not scanned";
  }
  return badge;
}

function applyScanResult(path, cleaned) {
  const entry = state.entries.find((e) => e.path === path);
  if (entry) entry.cleaned = cleaned;
  const row = $("entries").querySelector(`tr[data-path="${CSS.escape(path)}"]`);
  if (row) row.children[2].replaceChildren(statusBadge(cleaned));
}

function updateQueueButton() {
  const n = state.selected.size;
  const button = $("queue-selected");
  button.disabled = n === 0;
  button.textContent = n ? `Queue ${n} file${n === 1 ? "" : "s"}` : "Queue selected";
}

// --- queue -----------------------------------------------------------------

async function queueSelected() {
  const paths = [...state.selected];
  const dryRun = $("dry-run").checked;

  if (!dryRun && !(await confirmDestructive(paths))) return;

  for (const path of paths) {
    try {
      await api("/api/jobs", {
        method: "POST",
        body: JSON.stringify({ path, dry_run: dryRun, force: $("force").checked }),
      });
    } catch (err) {
      console.error("queue failed", path, err);
    }
  }
  state.selected.clear();
  renderEntries();
  updateQueueButton();
}

function confirmDestructive(paths) {
  const dialog = $("confirm");
  $("confirm-files").textContent = paths.join("\n");
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "go"), { once: true });
  });
}

function upsertJob(job) {
  state.jobs.set(job.id, job);
  renderJobs();
}

function renderJobs() {
  const list = $("jobs");
  list.replaceChildren();
  const jobs = [...state.jobs.values()].sort((a, b) => Number(b.id) - Number(a.id));
  $("no-jobs").hidden = jobs.length > 0;
  for (const job of jobs) list.append(jobRow(job));
}

function jobRow(job) {
  const li = document.createElement("li");

  const head = document.createElement("div");
  head.className = "job-head";

  const path = document.createElement("span");
  path.className = "job-path";
  path.textContent = job.path;
  path.title = job.path;

  const stateLabel = document.createElement("span");
  stateLabel.className = `job-state ${job.state}`;
  stateLabel.textContent = describe(job);

  head.append(path, stateLabel);

  if (job.state === "queued" || job.state === "running") {
    const cancel = document.createElement("button");
    cancel.className = "link";
    cancel.textContent = "cancel";
    cancel.disabled = job.cancel_requested;
    cancel.onclick = () => api(`/api/jobs/${job.id}`, { method: "DELETE" }).catch(console.error);
    head.append(cancel);
  }
  li.append(head);

  if (job.state === "running") {
    const bar = document.createElement("div");
    bar.className = "bar";
    const fill = document.createElement("i");
    fill.style.width = `${Math.round((job.progress || 0) * 100)}%`;
    bar.append(fill);
    li.append(bar);
  }

  if (job.matches.length) {
    const ul = document.createElement("ul");
    ul.className = "matches";
    for (const m of job.matches) {
      const item = document.createElement("li");
      const time = document.createElement("time");
      time.textContent = timestamp(m.start);
      const text = document.createElement("span");
      text.textContent = mask(m.text);
      item.append(time, text);
      ul.append(item);
    }
    li.append(ul);
  } else if (job.state === "done" && !job.skipped_reason) {
    li.append(note("no matches found"));
  }

  if (job.unsigned_marker) {
    li.append(note("done-marker not signed by this machine — reprocessed"));
  }
  if (job.error) {
    const err = document.createElement("p");
    err.className = "job-error";
    err.textContent = job.error;
    li.append(err);
  }
  return li;
}

function note(text) {
  const p = document.createElement("p");
  p.className = "job-note";
  p.textContent = text;
  return p;
}

function describe(job) {
  if (job.state === "running") {
    if (job.cancel_requested) return "cancelling…";
    const pct = Math.round((job.progress || 0) * 100);
    return job.stage === "transcribing" ? `transcribing ${pct}%` : (job.stage || "running");
  }
  if (job.state === "done") {
    if (job.skipped_reason === "already-processed") return "skipped — already cleaned";
    if (job.skipped_reason === "no-audio") return "skipped — no audio";
    if (job.dry_run) return `dry run — ${job.matches.length} match(es)`;
    return `muted ${job.muted} interval(s) in ${Math.round(job.elapsed)}s`;
  }
  return job.state;
}

// --- event stream ----------------------------------------------------------

function connect() {
  const source = new EventSource("/api/events");
  const conn = $("conn");

  source.onopen = () => { conn.className = "conn live"; conn.textContent = "live"; };
  source.onerror = () => { conn.className = "conn down"; conn.textContent = "reconnecting…"; };
  source.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "job") {
      upsertJob(msg.job);
      // A finished clean changes the file's status in the listing.
      if (msg.job.state === "done" && !msg.job.dry_run) {
        applyScanResult(msg.job.path, true);
      }
    } else if (msg.type === "scan") {
      applyScanResult(msg.path, msg.cleaned);
    } else if (msg.type === "sync") {
      state.jobs = new Map(msg.jobs.map((j) => [j.id, j]));
      renderJobs();
    }
  };
}

// --- boot ------------------------------------------------------------------

async function main() {
  $("queue-selected").onclick = queueSelected;
  $("select-all").onchange = (e) => {
    state.selected.clear();
    if (e.target.checked) {
      for (const entry of state.entries) if (!entry.is_dir) state.selected.add(entry.path);
    }
    renderEntries();
    $("select-all").checked = e.target.checked;
    updateQueueButton();
  };

  try {
    const cfg = await api("/api/config");
    $("meta").textContent = `v${cfg.version} · model ${cfg.model} · ${cfg.phrases} phrases · ${cfg.root}`;
  } catch (err) {
    console.error(err);
  }

  connect();
  await browse("");
}

main().catch((err) => {
  console.error(err);
  document.body.prepend(
    Object.assign(document.createElement("p"), {
      className: "job-error",
      textContent: `Failed to start: ${err.message}`,
    }),
  );
});
