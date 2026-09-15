"use strict";

// State is deliberately tiny: the current directory listing and the jobs we
// know about, both keyed so SSE messages can patch them in place.
const state = {
  path: "",
  entries: [],
  jobs: new Map(),
  selected: new Set(),
  player: { path: null, el: null, duration: 0 },
};

// Extensions that get an <audio> element instead of a <video> one.
const AUDIO_EXTENSIONS = [".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav"];

// Seeking to a match lands slightly before it, so you hear the run-up.
const SEEK_LEAD_IN = 1.5;

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
  const link = document.createElement("a");
  link.className = "fname";
  link.textContent = entry.name;          // textContent: filenames are untrusted
  link.title = `Play ${entry.name}`;
  link.onclick = () => openPlayer(entry.path, entry.name);
  tdName.append(link);

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
  path.textContent = job.path || job.url || "";
  path.title = job.url || job.path;

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

  if (job.kind === "download" && job.state === "done" && job.path) {
    const play = document.createElement("button");
    play.className = "link";
    play.textContent = "play";
    play.onclick = () => openPlayer(job.path, job.path.split("/").pop());
    head.append(play);
  }
  li.append(head);

  if (job.state === "running") {
    const bar = document.createElement("div");
    bar.className = "bar";
    const fill = document.createElement("i");
    fill.style.width = `${percent(job)}%`;
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
  } else if (job.kind !== "download" && job.state === "done" && !job.skipped_reason) {
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

// A fraction from the server can overshoot when it was derived from an
// estimated size, so never render more than 100%.
function percent(job) {
  return Math.round(Math.min(Math.max(job.progress || 0, 0), 1) * 100);
}

function describe(job) {
  if (job.kind === "download") return describeDownload(job);
  if (job.state === "running") {
    if (job.cancel_requested) return "cancelling…";
    const pct = percent(job);
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

function describeDownload(job) {
  if (job.state === "running") {
    if (job.cancel_requested) return "cancelling…";
    if (job.stage === "merging") return "merging…";
    return `downloading ${percent(job)}%`;
  }
  if (job.state === "done") {
    const took = `downloaded in ${Math.round(job.elapsed)}s`;
    return cleanPending(job) ? `${took} — cleaning next` : took;
  }
  return job.state;
}

// True while the clean this download queued has yet to run. The follow-up job
// carries its own row, so once it has finished the download row stops
// promising something that already happened.
function cleanPending(job) {
  if (!job.clean) return false;
  const follow = [...state.jobs.values()].find((j) => j.source === job.id);
  return !follow || follow.state === "queued" || follow.state === "running";
}

// --- player ----------------------------------------------------------------
//
// Scrubbing is the browser's own: /api/media answers Range requests, so the
// <video> element seeks without pulling the whole file first. What this adds
// on top is where the matches are -- markers on a strip under the controls,
// and a clickable list beside it.

function openPlayer(path, name) {
  const isAudio = AUDIO_EXTENSIONS.some((ext) => path.toLowerCase().endsWith(ext));
  const el = document.createElement(isAudio ? "audio" : "video");
  el.controls = true;
  el.preload = "metadata";
  el.playsInline = true;
  el.src = `/api/media?path=${encodeURIComponent(path)}`;
  el.addEventListener("loadedmetadata", () => {
    state.player.duration = el.duration;
    renderPlayerMatches();
  });
  el.addEventListener("error", () => {
    // Codecs, mostly: mkv and avi often will not decode natively even though
    // the file is served perfectly well.
    playerNote(`This browser cannot play ${name} natively.`);
  });

  state.player = { path, el, duration: 0 };
  $("player-wrap").replaceChildren(el);
  $("player-name").textContent = name;     // textContent: filenames are untrusted
  $("player-name").title = path;
  $("player-panel").hidden = false;
  renderPlayerMatches();
  $("player-panel").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function closePlayer() {
  const { el } = state.player;
  if (el) {
    el.pause();
    el.removeAttribute("src");
    el.load();                             // drops the connection, not just the src
  }
  state.player = { path: null, el: null, duration: 0 };
  $("player-wrap").replaceChildren();
  $("player-panel").hidden = true;
}

function seek(seconds) {
  const { el } = state.player;
  if (!el) return;
  el.currentTime = Math.max(0, seconds - SEEK_LEAD_IN);
  el.play().catch(() => { /* autoplay blocked; the seek still landed */ });
}

function playerNote(text) {
  const note = $("player-note");
  note.textContent = text || "";
  note.hidden = !text;
}

function scansOf(path) {
  // Newest first: the most recent scan is the one that still describes the file.
  return [...state.jobs.values()]
    .filter((j) => j.kind !== "download" && j.path === path)
    .sort((a, b) => Number(b.id) - Number(a.id));
}

function matchesFor(path) {
  const found = scansOf(path).find((j) => j.matches.length);
  return found ? found.matches : [];
}

function emptyNote(path) {
  // "Nothing to mark" and "nothing found" are different answers, and the
  // difference is the whole point of having scanned.
  const scanned = scansOf(path).some((j) => j.state === "done" && !j.skipped_reason);
  return scanned
    ? "Scanned: no matches found in this file."
    : "Queue a dry run for this file to mark its matches on the timeline.";
}

function renderPlayerMatches() {
  const { path, duration } = state.player;
  const list = $("player-matches");
  const markers = $("markers");
  list.replaceChildren();
  markers.replaceChildren();
  if (!path) return;

  const matches = matchesFor(path);
  $("timeline").hidden = !(matches.length && duration);
  if (!matches.length) {
    playerNote(emptyNote(path));
    return;
  }
  playerNote("");

  for (const m of matches) {
    if (duration) {
      const mark = document.createElement("i");
      mark.style.left = `${(m.start / duration) * 100}%`;
      mark.style.width = `${Math.max(0.4, ((m.end - m.start) / duration) * 100)}%`;
      mark.title = `${timestamp(m.start)}  ${mask(m.text)}`;
      mark.onclick = () => seek(m.start);
      markers.append(mark);
    }
    const item = document.createElement("li");
    const time = document.createElement("time");
    time.textContent = timestamp(m.start);
    const text = document.createElement("span");
    text.textContent = mask(m.text);
    item.append(time, text);
    item.onclick = () => seek(m.start);
    list.append(item);
  }
}

// --- downloads -------------------------------------------------------------

async function startDownload(event) {
  event.preventDefault();
  const input = $("url");
  const url = input.value.trim();
  if (!url) return;

  const button = $("fetch-go");
  button.disabled = true;
  try {
    // Downloads land in the directory being browsed, so where a file goes is
    // wherever you were looking when you pasted the link.
    await api("/api/downloads", {
      method: "POST",
      body: JSON.stringify({
        url, dest: state.path, clean: $("download-clean").checked,
      }),
    });
    input.value = "";
    fetchError("");
  } catch (err) {
    fetchError(err.message);
  } finally {
    button.disabled = false;
  }
}

// --- cookies ---------------------------------------------------------------
//
// The file never leaves the browser as a path: it is read here and its text is
// sent as the request body, so the server stores it at one fixed place in the
// config directory and no caller-supplied path is ever opened.

async function loadCookies() {
  try {
    renderCookies(await api("/api/cookies"));
  } catch (err) {
    $("cookies-state").textContent = "Cookies: unavailable";
    console.error(err);
  }
}

function renderCookies(status) {
  const label = $("cookies-state");
  if (!status.present) {
    label.textContent = "No cookies file";
  } else {
    const plural = status.count === 1 ? "" : "s";
    label.textContent = status.uploaded
      ? `${status.count} cookie${plural} uploaded`
      : `${status.count} cookie${plural} from config.toml`;
    label.title = status.path;
  }
  $("cookies-label").textContent = status.present ? "Replace" : "Upload cookies.txt";
  // A file named by config.toml is not ours to delete.
  $("cookies-remove").hidden = !status.uploaded;
}

async function uploadCookies(event) {
  const file = event.target.files[0];
  if (!file) return;
  try {
    renderCookies(await api("/api/cookies", {
      method: "PUT",
      headers: { "Content-Type": "text/plain" },
      body: await file.text(),
    }));
    fetchError("");
  } catch (err) {
    fetchError(`Cookies: ${err.message}`);
  } finally {
    // Clear it, or picking the same file again fires no change event.
    event.target.value = "";
  }
}

async function removeCookies() {
  try {
    renderCookies(await api("/api/cookies", { method: "DELETE" }));
    fetchError("");
  } catch (err) {
    fetchError(`Cookies: ${err.message}`);
  }
}

function fetchError(text) {
  const box = $("fetch-error");
  box.textContent = text || "";
  box.hidden = !text;
}

async function downloadFinished(job) {
  if (job.dest === state.path) await browse(state.path);
  // A clean is queued behind this download and is about to rewrite the file,
  // so the player waits for that job instead: what opens is the cleaned file,
  // with its matches already on the timeline.
  if (job.path && !job.clean) openPlayer(job.path, job.path.split("/").pop());
}

async function cleanFinished(job) {
  // The file was rewritten in place, so its size in the listing is stale.
  const slash = job.path.lastIndexOf("/");
  const dir = slash === -1 ? "" : job.path.slice(0, slash);
  if (dir === state.path) await browse(state.path);
  openPlayer(job.path, job.path.split("/").pop());
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
      const previous = state.jobs.get(msg.job.id);
      const justFinished = msg.job.state === "done" && previous?.state !== "done";
      upsertJob(msg.job);

      if (msg.job.kind === "download") {
        // The file only exists, and only has a name, once the job is done.
        if (justFinished) downloadFinished(msg.job).catch(console.error);
      } else {
        // A finished clean changes the file's status in the listing.
        if (msg.job.state === "done" && !msg.job.dry_run) {
          applyScanResult(msg.job.path, true);
          // Downloading was one request, not two: the player it was heading
          // for opens here, now that the file is the cleaned one.
          if (justFinished && msg.job.source) cleanFinished(msg.job).catch(console.error);
        }
        // New matches belong on the timeline of the file being watched.
        if (msg.job.path === state.player.path) renderPlayerMatches();
      }
    } else if (msg.type === "scan") {
      applyScanResult(msg.path, msg.cleaned);
    } else if (msg.type === "sync") {
      state.jobs = new Map(msg.jobs.map((j) => [j.id, j]));
      renderJobs();
      renderPlayerMatches();
    }
  };
}

// --- boot ------------------------------------------------------------------

async function main() {
  $("queue-selected").onclick = queueSelected;
  $("fetch").onsubmit = startDownload;
  $("cookies-file").onchange = uploadCookies;
  $("cookies-remove").onclick = removeCookies;
  $("player-close").onclick = closePlayer;
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
  loadCookies();
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
