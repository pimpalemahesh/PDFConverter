/* app.js — UI controller (main thread). Delegates all heavy work to worker.js. */

const $ = (id) => document.getElementById(id);
const drop = $("drop"),
  fileInput = $("file"),
  fname = $("fname"),
  go = $("go");
const titleIn = $("title"),
  authorIn = $("author");
const statusCard = $("statusCard"),
  statusText = $("statusText"),
  spinner = $("spinner");
const logEl = $("log"),
  download = $("download"),
  engineState = $("engineState");

let selectedFile = null;
let engineReady = false;
let lastUrl = null;

// --- boot the worker immediately so Pyodide + the wheel warm up in the background ---
const worker = new Worker("assets/js/worker.js");

worker.onmessage = (e) => {
  const m = e.data;
  switch (m.type) {
    case "status":
      engineState.textContent = m.message;
      if (converting) setStatus(m.message);
      break;
    case "ready":
      engineReady = true;
      engineState.textContent = `Engine ready · Pyodide ${m.pyodide} · ${m.pymupdf}`;
      refreshButton();
      break;
    case "progress":
      addLog(m.message);
      setStatus(m.message);
      break;
    case "done":
      onDone(m);
      break;
    case "error":
      onError(m.message);
      break;
  }
};

let converting = false;

function refreshButton() {
  go.disabled = !(selectedFile && engineReady && !converting);
  go.textContent = !engineReady
    ? "Preparing engine…"
    : converting
      ? "Converting…"
      : "Convert to EPUB";
}

function pickFile(file) {
  if (!file) return;
  if (
    !file.name.toLowerCase().endsWith(".pdf") &&
    file.type !== "application/pdf"
  ) {
    onError("That doesn't look like a PDF.");
    return;
  }
  selectedFile = file;
  fname.textContent = file.name;
  refreshButton();
}

fileInput.addEventListener("change", () => pickFile(fileInput.files[0]));
["dragover", "dragenter"].forEach((ev) =>
  drop.addEventListener(ev, (e) => {
    e.preventDefault();
    drop.classList.add("hover");
  }),
);
["dragleave", "dragend", "drop"].forEach((ev) =>
  drop.addEventListener(ev, (e) => {
    e.preventDefault();
    drop.classList.remove("hover");
  }),
);
drop.addEventListener("drop", (e) => {
  if (e.dataTransfer.files[0]) pickFile(e.dataTransfer.files[0]);
});

go.addEventListener("click", async () => {
  if (!selectedFile || !engineReady || converting) return;
  converting = true;
  refreshButton();
  resetStatus();
  setStatus("Reading file…");
  const bytes = await selectedFile.arrayBuffer();
  const stem =
    selectedFile.name
      .replace(/\.pdf$/i, "")
      .replace(/[^\w .()-]+/g, "")
      .trim() || "book";
  worker.postMessage(
    {
      type: "convert",
      bytes,
      title: titleIn.value.trim(),
      author: authorIn.value.trim(),
      filename: `${stem}.epub`,
    },
    [bytes], // transfer — no copy
  );
});

// --- status helpers ---
function resetStatus() {
  statusCard.hidden = false;
  statusCard.classList.remove("state-ok", "state-err");
  spinner.className = "spinner";
  logEl.innerHTML = "";
  download.hidden = true;
  if (lastUrl) {
    URL.revokeObjectURL(lastUrl);
    lastUrl = null;
  }
}
function setStatus(t) {
  statusText.textContent = t;
}
function addLog(t, cls = "") {
  const d = document.createElement("div");
  d.className = "line " + cls;
  d.textContent = t;
  logEl.appendChild(d);
  logEl.scrollTop = logEl.scrollHeight;
}

function onDone(m) {
  converting = false;
  refreshButton();
  spinner.className = "spinner done";
  statusCard.classList.add("state-ok");
  const kb = Math.round(m.stats.bytes / 1024);
  setStatus(`Done — “${m.stats.title}”`);
  addLog(
    `✓ ${m.stats.chapters} sections · ${m.stats.figures} figures · ${kb} KB`,
    "ok",
  );
  const blob = new Blob([m.epub], { type: "application/epub+zip" });
  lastUrl = URL.createObjectURL(blob);
  download.href = lastUrl;
  download.setAttribute("download", m.filename);
  download.textContent = `Download ${m.filename}`;
  download.hidden = false;
}

function onError(message) {
  converting = false;
  refreshButton();
  statusCard.hidden = false;
  spinner.className = "spinner err";
  statusCard.classList.add("state-err");
  setStatus("Conversion failed");
  addLog(message, "err");
  engineState.textContent = "Engine error — see message above.";
}

refreshButton();
