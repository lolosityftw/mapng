/* MapNG-AI Ground Texture Studio.
   Lists every land-cover class × every PBR map (diffuse, normal, roughness),
   shows thumbnails, lets you replace any one by drag-drop or file picker,
   and reset a class back to Poly Haven defaults. */

const grid = document.getElementById("texture-grid");
const summary = document.getElementById("textures-summary");
const fileInput = document.getElementById("upload-input");
const resetAllBtn = document.getElementById("reset-all");

let _state = { classes: [], pendingUpload: null };

const fmt = (n) => !n ? "—" : n < 1024 ? `${n} B`
                       : n < 1024 ** 2 ? `${(n / 1024).toFixed(1)} KB`
                       : `${(n / 1024 ** 2).toFixed(1)} MB`;

async function load() {
  summary.textContent = "loading…";
  const r = await fetch("/api/library/ground-textures");
  const data = await r.json();
  _state.classes = data.classes;
  render();
}

function pillForSource(source) {
  if (source === "polyhaven") return `<span class="pill">Poly Haven</span>`;
  if (source === "custom")    return `<span class="pill custom">custom</span>`;
  if (source === "missing")   return `<span class="pill missing">missing</span>`;
  return `<span class="pill procedural">${source}</span>`;
}

function render() {
  // Build grid: rows = classes, cols = (label) | diffuse | normal | roughness
  const head = `
    <div class="head">Class</div>
    <div class="head">Diffuse</div>
    <div class="head">Normal</div>
    <div class="head">Roughness</div>
  `;
  const rows = _state.classes.map((c) => {
    const cell = (kind) => {
      const m = c.maps[kind];
      const id = `cell-${c.class}-${kind}`;
      if (!m.url) {
        return `<div class="tex-cell" id="${id}" data-class="${c.class}" data-kind="${kind}">
          <div class="preview"><div class="placeholder">no ${kind}<br>(drop here to upload)</div></div>
          <div class="info"><span>${pillForSource(m.source)}</span></div>
          <div class="actions">
            <button data-act="upload">Upload PNG</button>
          </div>
        </div>`;
      }
      // Cache-bust per-load so an upload immediately reflects in the thumb
      const u = `${m.url}?v=${Date.now()}`;
      return `<div class="tex-cell" id="${id}" data-class="${c.class}" data-kind="${kind}">
        <div class="preview"><img src="${u}" alt="${c.class} ${kind}"/></div>
        <div class="info">
          <span>${pillForSource(m.source)}</span>
          <span class="dim">${m.width}×${m.height}</span>
          <span class="size">${fmt(m.size)}</span>
        </div>
        <div class="actions">
          <button data-act="upload">Replace</button>
          <a href="${u}" download="${c.class}_${kind}.${u.includes('.png') ? 'png' : 'jpg'}" target="_blank">
            <button>Download</button>
          </a>
        </div>
      </div>`;
    };
    return `
      <div class="class-cell">
        <h3>${c.class}</h3>
        <small class="meta">slug: ${c.slug || '—'}</small>
        <div class="actions" style="margin-top:auto">
          <button class="ghost" data-reset-class="${c.class}">Reset to Poly Haven</button>
        </div>
      </div>
      ${cell("diffuse")}
      ${cell("normal")}
      ${cell("roughness")}
    `;
  }).join("");
  grid.innerHTML = head + rows;
  wireCells();
  const built = _state.classes.reduce((acc, c) => acc + Object.values(c.maps).filter(m => m.url).length, 0);
  const total = _state.classes.length * 3;
  summary.textContent = `${built} / ${total} maps available`;
}

function wireCells() {
  grid.querySelectorAll(".tex-cell").forEach((cell) => {
    const cls = cell.dataset.class;
    const kind = cell.dataset.kind;
    cell.addEventListener("dragover", (ev) => {
      ev.preventDefault();
      cell.classList.add("dragover");
    });
    cell.addEventListener("dragleave", () => cell.classList.remove("dragover"));
    cell.addEventListener("drop", (ev) => {
      ev.preventDefault();
      cell.classList.remove("dragover");
      if (ev.dataTransfer.files?.[0]) uploadOne(cls, kind, ev.dataTransfer.files[0], cell);
    });
    cell.querySelector('button[data-act="upload"]')?.addEventListener("click", () => {
      _state.pendingUpload = { cls, kind, cell };
      fileInput.click();
    });
  });
  grid.querySelectorAll("[data-reset-class]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const cls = btn.dataset.resetClass;
      if (!confirm(`Reset all ${cls} textures? Removes any custom uploads — next 'Terrain PBR pack' rebuild will re-download from Poly Haven.`)) return;
      await fetch(`/api/library/ground-textures/${cls}`, { method: "DELETE" });
      load();
    });
  });
}

async function uploadOne(cls, kind, file, cell) {
  if (!file.type.startsWith("image/")) {
    alert("Please drop an image file");
    return;
  }
  cell.classList.add("uploading");
  try {
    const fd = new FormData();
    fd.append("file", file);
    const r = await fetch(`/api/library/ground-textures/${cls}/${kind}`, {
      method: "POST", body: fd,
    });
    if (!r.ok) throw new Error(await r.text());
    await load();
  } catch (e) {
    alert(`Upload failed: ${e}`);
  } finally {
    cell.classList.remove("uploading");
  }
}

fileInput.addEventListener("change", () => {
  const file = fileInput.files[0];
  fileInput.value = "";
  if (!file || !_state.pendingUpload) return;
  const { cls, kind, cell } = _state.pendingUpload;
  _state.pendingUpload = null;
  uploadOne(cls, kind, file, cell);
});

resetAllBtn.addEventListener("click", async () => {
  if (!confirm("Reset every class? Custom textures lost — re-run 'Terrain PBR pack' on /library afterwards to refetch defaults.")) return;
  for (const c of _state.classes) {
    await fetch(`/api/library/ground-textures/${c.class}`, { method: "DELETE" });
  }
  load();
});

load();
