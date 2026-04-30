/* MapNG-AI library page.
   All entries listed up-front, filterable, sortable, with a 3D preview
   pane on the right that loads cached GLBs via Three.js GLTFLoader. */

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
const state = {
  entries: [],          // catalogue entries with built/size_bytes
  filter: { category: "", type: "", built: "", search: "" },
  sort: "natural",
  selectedSlug: null,
  selectedQuality: "10k",   // which baked variant the 3D viewer shows
  selectedStats: null,      // last loaded /stats payload for the selection
  liveJob: null,        // EventSource of an in-flight build job
};

// ---------------------------------------------------------------------------
// DOM
// ---------------------------------------------------------------------------
const els = {
  status: document.getElementById("lib-status"),
  fCategory: document.getElementById("f-category"),
  fType: document.getElementById("f-type"),
  fBuilt: document.getElementById("f-built"),
  fSort: document.getElementById("f-sort"),
  fSearch: document.getElementById("f-search"),
  activeQuality: document.getElementById("active-quality"),
  meshyPolycount: document.getElementById("meshy-polycount"),
  buildMissing: document.getElementById("build-missing"),
  buildAll: document.getElementById("build-all"),
  bakeActive: document.getElementById("bake-active"),
  bakeAll: document.getElementById("bake-all"),
  terrainPack: document.getElementById("terrain-pack"),
  body: document.getElementById("entries-body"),
  batchStatus: document.getElementById("batch-status"),
  detailEmpty: document.getElementById("detail-empty"),
  detail: document.getElementById("detail"),
  d: {
    slug: document.getElementById("d-slug"),
    category: document.getElementById("d-category"),
    type: document.getElementById("d-type"),
    footprint: document.getElementById("d-footprint"),
    levels: document.getElementById("d-levels"),
    prompt: document.getElementById("d-prompt"),
    size: document.getElementById("d-size"),
    status: document.getElementById("d-status"),
    generate: document.getElementById("d-generate"),
    regenerate: document.getElementById("d-regenerate"),
    bake: document.getElementById("d-bake"),
    delete: document.getElementById("d-delete"),
    promptOverride: document.getElementById("d-prompt-override"),
    promptSave: document.getElementById("d-prompt-save"),
    promptClear: document.getElementById("d-prompt-clear"),
    drop: document.getElementById("d-drop"),
    file: document.getElementById("d-file"),
  },
  preview3d: document.getElementById("preview3d"),
  qualityTabs: document.getElementById("quality-tabs"),
  previewMeta: document.getElementById("preview-meta"),
};

// Order matters — display this left-to-right
const PREVIEW_QUALITIES = ["1.5k", "5k", "10k", "50k", "100k", "original"];

// ---------------------------------------------------------------------------
// Catalogue load + render
// ---------------------------------------------------------------------------
async function loadCatalogue() {
  const r = await fetch("/api/library/catalogue");
  const { entries } = await r.json();
  state.entries = entries;
  populateTypeFilter();
  renderTable();
  updateStatus();
}

function populateTypeFilter() {
  const types = [...new Set(state.entries.map((e) => e.type))].sort();
  const current = els.fType.value;
  els.fType.innerHTML = `<option value="">all</option>` +
    types.map((t) => `<option value="${t}">${t}</option>`).join("");
  if (types.includes(current)) els.fType.value = current;
}

function filtered() {
  let out = state.entries.slice();
  const f = state.filter;
  if (f.category) out = out.filter((e) => e.category === f.category);
  if (f.type)     out = out.filter((e) => e.type === f.type);
  if (f.built === "built")    out = out.filter((e) => e.built);
  if (f.built === "missing")  out = out.filter((e) => !e.built);
  if (f.search) {
    const q = f.search.toLowerCase();
    out = out.filter((e) =>
      e.slug.toLowerCase().includes(q) || e.prompt.toLowerCase().includes(q));
  }
  switch (state.sort) {
    case "name":   out.sort((a, b) => a.slug.localeCompare(b.slug)); break;
    case "type":   out.sort((a, b) => a.type.localeCompare(b.type) || a.slug.localeCompare(b.slug)); break;
    case "size":   out.sort((a, b) => b.size_bytes - a.size_bytes); break;
    case "status": out.sort((a, b) => Number(b.built) - Number(a.built)); break;
    // 'natural' = original catalogue order
  }
  return out;
}

function renderTable() {
  const list = filtered();
  els.body.innerHTML = list.map((e) => `
    <tr id="row-${e.slug}" data-slug="${e.slug}" class="${stateClass(e)}">
      <td class="c-icon"><span>${stateIcon(e)}</span></td>
      <td class="c-slug">
        ${e.slug}
        <span class="meta">${e.prompt}</span>
      </td>
      <td class="c-cat"><span class="pill">${e.category}</span> ${e.type}</td>
      <td class="c-size">${e.built ? fmtBytes(e.size_bytes) : ""}</td>
      <td class="c-actions">${rowActionButton(e)}</td>
    </tr>`).join("");
  // Highlight selected
  if (state.selectedSlug) {
    document.getElementById(`row-${state.selectedSlug}`)?.classList.add("selected");
  }
  // Wire row clicks
  els.body.querySelectorAll("tr").forEach((tr) => {
    tr.addEventListener("click", (ev) => {
      if (ev.target.closest("button")) return;
      selectEntry(tr.dataset.slug);
    });
  });
  els.body.querySelectorAll("button[data-action]").forEach((b) => {
    b.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const slug = b.closest("tr").dataset.slug;
      const action = b.dataset.action;
      if (action === "build") buildOne(slug, false);
      if (action === "rebuild") buildOne(slug, true);
    });
  });
}

function stateClass(e) {
  if (e._running) return "running";
  if (e._failed)  return "fail";
  if (e.built)    return "done";
  return "pending";
}
function stateIcon(e) {
  if (e._running) return "…";
  if (e._failed)  return "✗";
  if (e.built)    return "✓";
  return "○";
}
function rowActionButton(e) {
  if (e._running) return `<button disabled>building…</button>`;
  if (e.built)    return `<button class="ghost" data-action="rebuild">Rebuild</button>`;
  return `<button class="primary" data-action="build">Build</button>`;
}
function fmtBytes(n) {
  if (!n) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 ** 2).toFixed(1)} MB`;
}

function updateStatus() {
  const built = state.entries.filter((e) => e.built).length;
  els.status.textContent = `${built} / ${state.entries.length} built`;
  // Reflect total catalogue count on the 'Generate all' button
  const cnt = document.getElementById("build-all-count");
  if (cnt) cnt.textContent = `(${state.entries.length})`;
}

// ---------------------------------------------------------------------------
// Detail pane + 3D preview
// ---------------------------------------------------------------------------
function selectEntry(slug) {
  state.selectedSlug = slug;
  document.querySelectorAll("#entries-body tr").forEach((tr) => tr.classList.remove("selected"));
  document.getElementById(`row-${slug}`)?.classList.add("selected");
  const e = state.entries.find((x) => x.slug === slug);
  if (!e) return;
  els.detailEmpty.hidden = true;
  els.detail.hidden = false;
  els.d.slug.textContent = e.slug;
  els.d.category.textContent = e.category;
  els.d.type.textContent = e.type;
  els.d.footprint.textContent = `${e.footprint_m[0]} × ${e.footprint_m[1]} m`;
  els.d.levels.textContent = e.levels;
  els.d.prompt.textContent = e.prompt;
  els.d.size.textContent = e.built ? fmtBytes(e.size_bytes) : "(not generated)";
  els.d.status.textContent = e._running ? "building…"
                            : e._failed ? "failed"
                            : e.built ? "built ✓"
                            : "missing";
  els.d.generate.hidden = e.built || e._running;
  els.d.regenerate.hidden = !e.built;
  els.d.bake.hidden = !e.built;
  els.d.delete.hidden = !e.built;
  loadPreview(e);
  loadStats(e);
  loadPromptForEntry(slug);
  loadEmbeddedTextures(slug);
}

async function loadEmbeddedTextures(slug) {
  const block = document.getElementById("d-textures");
  const gridEl = document.getElementById("d-textures-grid");
  if (!block || !gridEl) return;
  block.hidden = true;
  gridEl.innerHTML = "";
  try {
    const r = await fetch(`/api/library/entries/${slug}/textures`);
    const data = await r.json();
    if (!data.exists || !data.textures?.length) return;
    block.hidden = false;
    const fmt = (n) => !n ? "—" : n < 1024 ** 2 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1024 ** 2).toFixed(1)} MB`;
    gridEl.innerHTML = data.textures.map((t) => `
      <div class="emb-cell">
        <div class="preview"><img src="${t.url}?v=${t.size}" alt="${t.name}" loading="lazy"/></div>
        <div class="info">
          <strong title="${t.name}">${t.name.length > 18 ? t.name.slice(0, 18) + '…' : t.name}</strong>
          <span class="dim">${t.width}×${t.height}</span>
          <span> · ${fmt(t.size)}</span>
        </div>
      </div>
    `).join("");
  } catch (e) {
    console.warn("textures fetch failed", e);
  }
}

async function loadPromptForEntry(slug) {
  if (!els.d.promptOverride) return;
  els.d.promptOverride.value = "";
  els.d.promptOverride.placeholder = "loading…";
  try {
    const r = await fetch(`/api/library/entries/${slug}/prompt`);
    const data = await r.json();
    els.d.promptOverride.placeholder = data.default || "";
    els.d.promptOverride.value = data.override || "";
  } catch (e) { els.d.promptOverride.placeholder = "(failed to load prompt)"; }
}

async function loadStats(e) {
  const block = document.getElementById("d-stats");
  const body = document.getElementById("d-stats-body");
  const trisEl = document.getElementById("d-tris");
  if (!e.built) {
    block.hidden = true;
    state.selectedStats = null;
    renderQualityTabs(null);
    updatePreviewMeta();
    return;
  }
  block.hidden = false;
  body.innerHTML = `<tr><td colspan="4">loading…</td></tr>`;
  try {
    const r = await fetch(`/api/library/entries/${e.slug}/stats`);
    const data = await r.json();
    if (!data.exists) {
      block.hidden = true;
      state.selectedStats = null;
      renderQualityTabs(null);
      updatePreviewMeta();
      return;
    }
    state.selectedStats = data;
    renderQualityTabs(data);
    updatePreviewMeta();
    const order = ["original", "100k", "50k", "10k", "5k", "1.5k"];
    const fmt = (n) => n < 1024 ? `${n} B`
                       : n < 1024 ** 2 ? `${(n / 1024).toFixed(1)} KB`
                       : `${(n / 1024 ** 2).toFixed(1)} MB`;
    body.innerHTML = order.map((q) => {
      const s = data.qualities[q];
      const built = s?.exists ?? false;
      const dim = s?.image_max_dim || s?.max_dim_target || 0;
      const file = built && s.file_size ? fmt(s.file_size) : "(not built)";
      const gpu = s?.gpu_texture_bytes ? fmt(s.gpu_texture_bytes) : "—";
      return `<tr class="${built ? '' : 'missing'}">
                <td>${q}</td>
                <td>${dim ? dim + ' px' : '—'}</td>
                <td>${file}</td>
                <td>${gpu}</td>
              </tr>`;
    }).join("");
    trisEl.textContent = data.qualities.original?.triangle_count
      ? data.qualities.original.triangle_count.toLocaleString()
      : "?";
  } catch (err) {
    body.innerHTML = `<tr><td colspan="4">stats failed: ${err}</td></tr>`;
  }
}

// ---- Three.js scene for the detail pane ----
const preview = (() => {
  const container = els.preview3d;
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0d1117);
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  container.appendChild(renderer.domElement);

  const camera = new THREE.PerspectiveCamera(45, 1, 0.05, 500);
  camera.position.set(8, 6, 8);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.target.set(0, 1, 0);

  scene.add(new THREE.HemisphereLight(0xc8d8e8, 0x4a4226, 0.7));
  const sun = new THREE.DirectionalLight(0xfff1d6, 1.2);
  sun.position.set(5, 10, 6);
  scene.add(sun);

  const grid = new THREE.GridHelper(20, 20, 0x4a5560, 0x2a2f37);
  grid.position.y = 0;
  scene.add(grid);

  let model = null;
  function clear() {
    if (model) {
      scene.remove(model);
      model.traverse((o) => {
        if (o.isMesh) {
          o.geometry?.dispose?.();
          (Array.isArray(o.material) ? o.material : [o.material]).forEach((m) => m?.dispose?.());
        }
      });
      model = null;
    }
  }

  function setModel(obj) {
    clear();
    obj.traverse((o) => { if (o.isMesh) { o.castShadow = true; o.receiveShadow = true; } });
    // Centre + scale so the longest dim ~ 6 units
    const bbox = new THREE.Box3().setFromObject(obj);
    const size = new THREE.Vector3(); bbox.getSize(size);
    const max = Math.max(size.x, size.y, size.z) || 1;
    const scale = 6 / max;
    obj.scale.setScalar(scale);
    bbox.setFromObject(obj);
    const centre = new THREE.Vector3(); bbox.getCenter(centre);
    obj.position.sub(centre);
    obj.position.y -= bbox.min.y - centre.y;   // sit on grid
    scene.add(obj);
    model = obj;
    controls.target.set(0, size.y * scale * 0.5, 0);
    camera.position.set(max * scale * 1.6, max * scale * 1.1, max * scale * 1.6);
    controls.update();
  }

  function showMessage(text, isError = false) {
    clear();
    container.querySelectorAll(".overlay").forEach((n) => n.remove());
    const div = document.createElement("div");
    div.className = "overlay";
    div.style.cssText = `position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
      color:${isError ? "#f5b3b6" : "#6e7681"};font-size:13px;text-align:center;padding:20px;`;
    div.textContent = text;
    container.appendChild(div);
  }

  function clearMessage() {
    container.querySelectorAll(".overlay").forEach((n) => n.remove());
  }

  function resize() {
    const w = container.clientWidth || 400;
    const h = container.clientHeight || 380;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h, false);
  }
  new ResizeObserver(resize).observe(container);
  resize();

  (function loop() {
    requestAnimationFrame(loop);
    controls.update();
    renderer.render(scene, camera);
  })();

  return { setModel, clear, showMessage, clearMessage };
})();

const gltfLoader = new GLTFLoader();

function fmtBytesQ(n) {
  if (!n) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 ** 2).toFixed(1)} MB`;
}

function renderQualityTabs(stats) {
  if (!els.qualityTabs) return;
  els.qualityTabs.innerHTML = "";
  for (const q of PREVIEW_QUALITIES) {
    const meta = stats?.qualities?.[q];
    const baked = !!meta?.exists;
    const btn = document.createElement("button");
    btn.dataset.quality = q;
    btn.textContent = q;
    if (q === state.selectedQuality) btn.classList.add("active");
    btn.classList.add(baked ? "baked" : "missing");
    btn.title = baked
      ? `baked · ${meta.triangle_count?.toLocaleString() ?? "?"} tris · ${fmtBytesQ(meta.file_size)}`
      : `not yet baked — click to bake on demand`;
    btn.addEventListener("click", () => {
      state.selectedQuality = q;
      renderQualityTabs(state.selectedStats);
      const e = state.entries.find((x) => x.slug === state.selectedSlug);
      if (e) loadPreview(e, q);
      updatePreviewMeta();
    });
    els.qualityTabs.appendChild(btn);
  }
}

function updatePreviewMeta() {
  if (!els.previewMeta) return;
  const m = state.selectedStats?.qualities?.[state.selectedQuality];
  if (!m || !m.exists) {
    els.previewMeta.innerHTML = `<span class="pill">${state.selectedQuality}</span> <span>(baking on demand…)</span>`;
    return;
  }
  els.previewMeta.innerHTML =
    `<span class="pill">${state.selectedQuality}</span>` +
    `<span><strong>${m.triangle_count?.toLocaleString() ?? "?"}</strong> tris</span>` +
    `<span><strong>${fmtBytesQ(m.file_size)}</strong> file</span>` +
    `<span>${m.image_max_dim || "?"} px tex</span>`;
}

async function loadPreview(e, quality) {
  if (!e.built) {
    preview.showMessage("Mesh not generated yet — click Build to create it via Meshy.");
    return;
  }
  const q = quality || state.selectedQuality || "10k";
  preview.showMessage(`loading ${q}…`);
  try {
    await new Promise((resolve, reject) => {
      gltfLoader.load(
        `/api/library/entries/${e.slug}/glb?quality=${encodeURIComponent(q)}&v=${e.size_bytes}`,
        (gltf) => { preview.clearMessage(); preview.setModel(gltf.scene); resolve(); },
        undefined,
        (err) => reject(err),
      );
    });
  } catch (err) {
    preview.showMessage(`failed to load mesh: ${err}`, true);
  }
}

// ---------------------------------------------------------------------------
// Build actions (single + bulk)
// ---------------------------------------------------------------------------
function setRunning(slug, running) {
  const e = state.entries.find((x) => x.slug === slug);
  if (!e) return;
  e._running = running;
  if (running) e._failed = false;
  renderTable();
  if (state.selectedSlug === slug) selectEntry(slug);
}

function markBuilt(slug, sizeBytes) {
  const e = state.entries.find((x) => x.slug === slug);
  if (!e) return;
  e.built = true;
  e._running = false;
  e._failed = false;
  if (sizeBytes) e.size_bytes = sizeBytes;
  renderTable();
  updateStatus();
  if (state.selectedSlug === slug) selectEntry(slug);
}

function markFailed(slug) {
  const e = state.entries.find((x) => x.slug === slug);
  if (!e) return;
  e._running = false;
  e._failed = true;
  renderTable();
  if (state.selectedSlug === slug) selectEntry(slug);
}

async function buildOne(slug, force) {
  setRunning(slug, true);
  try {
    const r = await fetch("/api/library/build/single", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ slug, force }),
    });
    const { job_id } = await r.json();
    streamJob(job_id, true);
  } catch (err) {
    setRunning(slug, false);
    markFailed(slug);
  }
}

async function buildBatch(force = false) {
  // Snapshot which slugs are about to run so we can mark them
  const targets = state.entries.filter((e) => force || !e.built);
  for (const t of targets) setRunning(t.slug, true);

  const r = await fetch("/api/library/build", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({}),
  });
  const { job_id } = await r.json();
  streamJob(job_id, false);
}

function streamJob(jobId, isSingle) {
  if (state.liveJob) state.liveJob.close();
  const es = new EventSource(`/api/library/jobs/${jobId}/events`);
  state.liveJob = es;

  els.batchStatus.hidden = false;
  els.batchStatus.classList.remove("error");
  els.batchStatus.textContent = "starting…";

  es.addEventListener("batch:start", (ev) => {
    const d = JSON.parse(ev.data);
    els.batchStatus.textContent =
      `building 0 / ${d.total} · concurrency ${d.concurrency} · ${d.rps} req/s · texture ${d.texture ? "on" : "off"}`;
  });
  es.addEventListener("entry:start", (ev) => {
    const d = JSON.parse(ev.data);
    setRunning(d.slug, true);
  });
  es.addEventListener("entry:done", (ev) => {
    const d = JSON.parse(ev.data);
    markBuilt(d.slug, d.size_bytes);
    if (d.completed && d.total) {
      els.batchStatus.textContent = `building ${d.completed} / ${d.total}`;
    }
  });
  es.addEventListener("entry:skip", (ev) => {
    const d = JSON.parse(ev.data);
    setRunning(d.slug, false);
  });
  es.addEventListener("entry:fail", (ev) => {
    const d = JSON.parse(ev.data);
    markFailed(d.slug);
  });
  es.addEventListener("batch:done", (ev) => {
    const d = JSON.parse(ev.data);
    els.batchStatus.textContent =
      `done — ${d.completed} processed, ${d.skipped} skipped, ${d.failed} failed`;
    state.liveJob = null;
    es.close();
    setTimeout(() => { els.batchStatus.hidden = true; }, 4000);
  });
  es.addEventListener("batch:error", (ev) => {
    const d = JSON.parse(ev.data);
    els.batchStatus.classList.add("error");
    els.batchStatus.textContent = `error: ${d.message}`;
    state.liveJob = null;
    es.close();
  });
}

// ---------------------------------------------------------------------------
// Wire filter / action UI
// ---------------------------------------------------------------------------
els.fCategory.addEventListener("change", () => { state.filter.category = els.fCategory.value; renderTable(); });
els.fType.addEventListener("change", () => { state.filter.type = els.fType.value; renderTable(); });
els.fBuilt.addEventListener("change", () => { state.filter.built = els.fBuilt.value; renderTable(); });
els.fSort.addEventListener("change", () => { state.sort = els.fSort.value; renderTable(); });
els.fSearch.addEventListener("input", () => { state.filter.search = els.fSearch.value.trim(); renderTable(); });

els.buildMissing.addEventListener("click", () => buildBatch(false));
els.buildAll.addEventListener("click", () => {
  const n = state.entries.length;
  const cap = parseInt(els.meshyPolycount?.value || "0", 10) || 0;
  const capStr = cap ? `at ${cap.toLocaleString()} tris` : "at catalogue defaults";
  if (confirm(`Generate all ${n} entries ${capStr}? Regenerates EVERY entry via Meshy and burns credits.`)) {
    buildBatch(true);
  }
});

// ---- Terrain PBR pack download (Poly Haven CC0) -----------------------------
async function downloadTerrainPack() {
  els.terrainPack.disabled = true;
  els.batchStatus.hidden = false;
  els.batchStatus.classList.remove("error");
  els.batchStatus.textContent = "starting Poly Haven downloads…";

  try {
    const r = await fetch("/api/library/terrain-pack/download", { method: "POST" });
    if (!r.ok) throw new Error(`download: ${r.status}`);
    const { job_id } = await r.json();
    if (state.liveJob) state.liveJob.close();
    const es = new EventSource(`/api/library/jobs/${job_id}/events`);
    state.liveJob = es;

    es.addEventListener("pack:start", (ev) => {
      const d = JSON.parse(ev.data);
      els.batchStatus.textContent = `terrain pack: 0 / ${d.total} classes`;
    });
    let completed = 0, total = 0;
    es.addEventListener("class:done", (ev) => {
      const d = JSON.parse(ev.data); completed++;
      els.batchStatus.textContent =
        `terrain pack: ${completed} / ${total || "?"} · last: ${d.class} (${d.downloaded}/${d.attempted} maps)`;
    });
    es.addEventListener("class:skip", (ev) => {
      const d = JSON.parse(ev.data); completed++;
      els.batchStatus.textContent = `terrain pack: ${completed} cached · ${d.class} skipped`;
    });
    es.addEventListener("class:fail", (ev) => {
      const d = JSON.parse(ev.data);
      els.batchStatus.textContent =
        `terrain pack: ${d.class} failed (${d.reason}) — falling back to procedural`;
    });
    es.addEventListener("pack:start", (ev) => { total = JSON.parse(ev.data).total; });
    es.addEventListener("pack:done", (ev) => {
      const d = JSON.parse(ev.data);
      els.batchStatus.textContent =
        `terrain pack: done — ${d.completed} processed, ${d.skipped} cached, ${d.failed} failed`;
      state.liveJob = null;
      es.close();
      els.terrainPack.disabled = false;
      setTimeout(() => { els.batchStatus.hidden = true; }, 5000);
    });
    es.addEventListener("pack:error", (ev) => {
      const d = JSON.parse(ev.data);
      els.batchStatus.classList.add("error");
      els.batchStatus.textContent = `error: ${d.message}`;
      els.terrainPack.disabled = false;
      es.close();
    });
  } catch (e) {
    els.batchStatus.classList.add("error");
    els.batchStatus.textContent = `failed: ${e}`;
    els.terrainPack.disabled = false;
  }
}

els.terrainPack.addEventListener("click", downloadTerrainPack);

// ---- Pre-bake quality variants ---------------------------------------------
async function bakeQualities(qualities, btnEl) {
  // qualities = null  → backend uses active quality only
  // qualities = ["1.5k","5k",...]  → explicit set
  const label = qualities === null ? "active" : qualities.join(", ");
  if (!confirm(`Pre-bake every built entry at: ${label}? CPU-bound, ~10s per variant per entry.`)) return;
  if (btnEl) btnEl.disabled = true;
  els.batchStatus.hidden = false;
  els.batchStatus.classList.remove("error");
  els.batchStatus.textContent = "starting pre-bake…";
  try {
    const r = await fetch("/api/library/optimise/all", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ qualities }),
    });
    if (!r.ok) throw new Error(await r.text());
    const { job_id, qualities: actual } = await r.json();
    if (state.liveJob) state.liveJob.close();
    const es = new EventSource(`/api/library/jobs/${job_id}/events`);
    state.liveJob = es;
    let done = 0, total = 0, failed = 0;
    es.addEventListener("bake:start", (ev) => {
      const d = JSON.parse(ev.data);
      total = d.total;
      els.batchStatus.textContent = `pre-baking 0 / ${total} (${d.qualities.join(', ')})…`;
    });
    es.addEventListener("variant:done", (ev) => {
      const d = JSON.parse(ev.data); done++;
      els.batchStatus.textContent = `pre-baking ${done} / ${total} · last: ${d.slug} @ ${d.quality}`;
    });
    es.addEventListener("variant:fail", (ev) => {
      const d = JSON.parse(ev.data); failed++;
      console.warn("variant fail", d);
    });
    es.addEventListener("bake:done", () => {
      els.batchStatus.textContent = `pre-bake done — ${done} ok, ${failed} failed`;
      if (btnEl) btnEl.disabled = false;
      els.bakeActive.disabled = false;
      els.bakeAll.disabled = false;
      es.close();
      state.liveJob = null;
      setTimeout(() => { els.batchStatus.hidden = true; }, 5000);
      if (state.selectedSlug) loadStats(state.entries.find(e => e.slug === state.selectedSlug));
    });
    es.addEventListener("bake:error", (ev) => {
      const d = JSON.parse(ev.data);
      els.batchStatus.classList.add("error");
      els.batchStatus.textContent = `bake error: ${d.message}`;
      if (btnEl) btnEl.disabled = false;
      els.bakeActive.disabled = false;
      els.bakeAll.disabled = false;
      es.close();
    });
  } catch (e) {
    els.batchStatus.classList.add("error");
    els.batchStatus.textContent = `bake failed: ${e}`;
    if (btnEl) btnEl.disabled = false;
  }
}

function updateBakeActiveLabel() {
  if (els.bakeActive && els.activeQuality) {
    els.bakeActive.textContent = `Pre-bake ${els.activeQuality.value}`;
  }
}

els.bakeActive.addEventListener("click", () => bakeQualities(null, els.bakeActive));
els.bakeAll.addEventListener("click", () => bakeQualities(
  ["100k", "50k", "10k", "5k", "1.5k"], els.bakeAll
));

els.d.generate.addEventListener("click", () => buildOne(state.selectedSlug, false));
els.d.regenerate.addEventListener("click", () => {
  if (confirm(`Regenerate ${state.selectedSlug}? Existing cached mesh will be replaced.`)) {
    buildOne(state.selectedSlug, true);
  }
});
// ---- Prompt override + GLB upload (per-entry, detail pane) -----------------
els.d.promptSave?.addEventListener("click", async () => {
  const slug = state.selectedSlug;
  if (!slug) return;
  await fetch(`/api/library/entries/${slug}/prompt`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ prompt: els.d.promptOverride.value }),
  });
});
els.d.promptClear?.addEventListener("click", async () => {
  const slug = state.selectedSlug;
  if (!slug) return;
  await fetch(`/api/library/entries/${slug}/prompt`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ prompt: null }),
  });
  loadPromptForEntry(slug);
});

async function uploadGlb(file) {
  const slug = state.selectedSlug;
  if (!slug || !file) return;
  if (!file.name.toLowerCase().endsWith(".glb")) {
    alert("Please drop a .glb file");
    return;
  }
  els.d.drop.classList.add("uploading");
  els.d.drop.textContent = `uploading ${file.name}…`;
  try {
    const fd = new FormData();
    fd.append("file", file);
    const r = await fetch(`/api/library/entries/${slug}/upload`, { method: "POST", body: fd });
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    els.d.drop.classList.remove("uploading");
    els.d.drop.textContent = `replaced (${(data.saved_bytes / 1e6).toFixed(1)} MB) — reloading…`;
    // Mark built locally + refresh stats/preview
    const e = state.entries.find((x) => x.slug === slug);
    if (e) { e.built = true; e.size_bytes = data.saved_bytes; }
    renderTable();
    selectEntry(slug);
    setTimeout(() => { els.d.drop.textContent = "drop a .glb here or click to choose"; }, 2500);
  } catch (err) {
    els.d.drop.classList.remove("uploading");
    els.d.drop.textContent = `upload failed: ${err}`;
  }
}

els.d.drop?.addEventListener("click", () => els.d.file.click());
els.d.file?.addEventListener("change", () => {
  if (els.d.file.files[0]) uploadGlb(els.d.file.files[0]);
  els.d.file.value = "";
});
els.d.drop?.addEventListener("dragover", (ev) => {
  ev.preventDefault();
  els.d.drop.classList.add("dragover");
});
els.d.drop?.addEventListener("dragleave", () => {
  els.d.drop.classList.remove("dragover");
});
els.d.drop?.addEventListener("drop", (ev) => {
  ev.preventDefault();
  els.d.drop.classList.remove("dragover");
  if (ev.dataTransfer.files?.[0]) uploadGlb(ev.dataTransfer.files[0]);
});

els.d.bake.addEventListener("click", async () => {
  const slug = state.selectedSlug;
  if (!slug) return;
  els.d.bake.disabled = true;
  els.d.bake.textContent = "baking…";
  try {
    const r = await fetch(`/api/library/optimise/${slug}`, { method: "POST" });
    const { job_id } = await r.json();
    const es = new EventSource(`/api/library/jobs/${job_id}/events`);
    let done = 0, total = 0;
    es.addEventListener("bake:start", (ev) => { total = JSON.parse(ev.data).total; });
    es.addEventListener("variant:done", () => {
      done++;
      els.d.bake.textContent = `baking ${done}/${total}…`;
    });
    es.addEventListener("bake:done", () => {
      els.d.bake.textContent = "Pre-bake variants";
      els.d.bake.disabled = false;
      es.close();
      loadStats(state.entries.find((e) => e.slug === slug));   // refresh stats table
    });
  } catch (e) {
    els.d.bake.textContent = "Pre-bake variants";
    els.d.bake.disabled = false;
  }
});

els.d.delete.addEventListener("click", async () => {
  if (!confirm(`Delete cached mesh for ${state.selectedSlug}?`)) return;
  await fetch(`/api/library/entries/${state.selectedSlug}`, { method: "DELETE" });
  // Mark missing locally
  const e = state.entries.find((x) => x.slug === state.selectedSlug);
  if (e) { e.built = false; e.size_bytes = 0; }
  renderTable();
  updateStatus();
  selectEntry(state.selectedSlug);
});

// ---- Active quality (the value the main pipeline reads) --------------------
async function refreshActiveQuality() {
  try {
    const r = await fetch("/api/library/active-quality");
    const { quality } = await r.json();
    if (els.activeQuality) els.activeQuality.value = quality;
    updateBakeActiveLabel();
  } catch (e) {
    console.warn("active quality fetch failed", e);
  }
  try {
    const r = await fetch("/api/library/meshy-polycount");
    const { polycount } = await r.json();
    if (els.meshyPolycount) {
      // Snap to closest known tier for the dropdown
      const tiers = [1500, 2500, 5000, 7500, 10000, 15000, 20000];
      const closest = tiers.reduce((a, b) =>
        Math.abs(b - polycount) < Math.abs(a - polycount) ? b : a);
      els.meshyPolycount.value = closest;
    }
  } catch (e) {
    console.warn("polycount fetch failed", e);
  }
}

els.meshyPolycount?.addEventListener("change", async () => {
  const n = parseInt(els.meshyPolycount.value || "0", 10);
  try {
    await fetch("/api/library/meshy-polycount", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ polycount: n }),
    });
  } catch (e) { console.warn("set polycount failed", e); }
});
els.activeQuality?.addEventListener("change", async () => {
  const v = els.activeQuality.value;
  updateBakeActiveLabel();
  try {
    await fetch("/api/library/active-quality", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ quality: v }),
    });
  } catch (e) {
    console.warn("set active quality failed", e);
  }
});

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
loadCatalogue();
refreshActiveQuality();
