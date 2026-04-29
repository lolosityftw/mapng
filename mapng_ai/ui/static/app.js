/* MapNG-AI frontend
   Map picker (Leaflet) + progress feed (SSE) + library batch panel.
   3D preview lives in preview.js as an ES module. */

// ---- Library panel ----------------------------------------------------------
const libStatus = document.getElementById("lib-status");
const libBuildBtn = document.getElementById("lib-build");
const libPanel = document.getElementById("lib-panel");
const libCloseBtn = document.getElementById("lib-close");
const libList = document.getElementById("lib-list");
const libSummary = document.getElementById("lib-summary");

async function refreshLibStatus() {
  try {
    const r = await fetch("/api/library/status");
    if (!r.ok) throw new Error(r.statusText);
    const s = await r.json();
    const built = (s.totals.building || 0) + (s.totals.tree || 0) + (s.totals.vehicle || 0);
    libStatus.textContent = `library: ${built}/${s.catalogue_size} built`;
  } catch (e) {
    libStatus.textContent = "library: ?";
  }
}
refreshLibStatus();

libBuildBtn?.addEventListener("click", () => {
  libPanel.hidden = false;
  libList.innerHTML = "";
  libSummary.textContent =
    "Generating region pack via Meshy. Each entry takes 30–90 s. Cached results skip; safe to re-run.";
  fetch("/api/library/build", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({}),
  }).then((r) => r.json()).then(({ job_id }) => {
    const es = new EventSource(`/api/library/jobs/${job_id}/events`);
    es.addEventListener("batch:start", (ev) => {
      const d = JSON.parse(ev.data);
      libSummary.textContent = `0 / ${d.total} entries (categories: ${d.categories.join(", ")})`;
    });
    es.addEventListener("entry:start", (ev) => addLibLine(JSON.parse(ev.data), "running"));
    es.addEventListener("entry:done", (ev) => updateLibLine(JSON.parse(ev.data), "done"));
    es.addEventListener("entry:skip", (ev) => addLibLine(JSON.parse(ev.data), "skip"));
    es.addEventListener("entry:fail", (ev) => updateLibLine(JSON.parse(ev.data), "fail"));
    es.addEventListener("batch:done", (ev) => {
      const d = JSON.parse(ev.data);
      libSummary.textContent = `done — ${d.completed} processed, ${d.skipped} skipped, ${d.failed} failed`;
      refreshLibStatus();
      es.close();
    });
    es.addEventListener("batch:error", (ev) => {
      const d = JSON.parse(ev.data);
      libSummary.textContent = `error: ${d.message}`;
      es.close();
    });
  });
});

libCloseBtn?.addEventListener("click", () => { libPanel.hidden = true; });

function addLibLine(d, state) {
  let li = document.getElementById(`lib-${d.slug}`);
  if (!li) {
    li = document.createElement("li");
    li.id = `lib-${d.slug}`;
    li.innerHTML = `
      <span class="icon">${state === "skip" ? "↻" : "…"}</span>
      <span class="label">${d.slug}<br><span class="meta">${d.category || ""}/${d.type || ""}</span></span>
      <span class="size"></span>`;
    libList.appendChild(li);
  }
  li.classList.add(state);
}

function updateLibLine(d, state) {
  let li = document.getElementById(`lib-${d.slug}`);
  if (!li) {
    addLibLine(d, state);
    li = document.getElementById(`lib-${d.slug}`);
  }
  li.classList.remove("running", "done", "fail", "skip");
  li.classList.add(state);
  const icon = li.querySelector(".icon");
  if (icon) icon.textContent = state === "done" ? "✓" : state === "fail" ? "✗" : state === "skip" ? "↻" : "…";
  if (state === "done" && d.size_bytes) {
    li.querySelector(".size").textContent = `${(d.size_bytes / 1e6).toFixed(1)} MB`;
  }
  if (state === "done" && d.completed && d.total) {
    libSummary.textContent = `${d.completed} / ${d.total} entries`;
  }
}

const COOKSTOWN = [54.6479, -6.7456]; // default centre

const map = L.map("map", { zoomControl: true }).setView(COOKSTOWN, 13);

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution: "© OpenStreetMap contributors",
  maxZoom: 19,
}).addTo(map);

// Drawing
const drawnItems = new L.FeatureGroup().addTo(map);
const drawControl = new L.Control.Draw({
  edit: { featureGroup: drawnItems, edit: false, remove: true },
  draw: {
    polygon: false, polyline: false, marker: false,
    circle: false, circlemarker: false,
    rectangle: { shapeOptions: { color: "#d29922", weight: 2 } },
  },
});
map.addControl(drawControl);

let currentBBox = null;
const bboxReadout = document.getElementById("bbox-readout");
const generateBtn = document.getElementById("generate");
const statusEl = document.getElementById("status");
const stagesEl = document.getElementById("stages");

function setBBox(rect) {
  drawnItems.clearLayers();
  drawnItems.addLayer(rect);
  const b = rect.getBounds();
  currentBBox = {
    west:  b.getWest(),
    south: b.getSouth(),
    east:  b.getEast(),
    north: b.getNorth(),
  };
  const widthKm  = haversineKm(b.getSouth(), b.getWest(), b.getSouth(), b.getEast());
  const heightKm = haversineKm(b.getSouth(), b.getWest(), b.getNorth(), b.getWest());
  bboxReadout.textContent =
    `${widthKm.toFixed(2)} × ${heightKm.toFixed(2)} km · ` +
    `W ${currentBBox.west.toFixed(5)}, S ${currentBBox.south.toFixed(5)}, ` +
    `E ${currentBBox.east.toFixed(5)}, N ${currentBBox.north.toFixed(5)}`;
  generateBtn.disabled = false;
}

map.on(L.Draw.Event.CREATED, (e) => setBBox(e.layer));
map.on("draw:deleted", () => {
  currentBBox = null;
  bboxReadout.textContent = "draw a rectangle on the map";
  generateBtn.disabled = true;
});

function haversineKm(lat1, lon1, lat2, lon2) {
  const toRad = (d) => (d * Math.PI) / 180;
  const R = 6371;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a = Math.sin(dLat / 2) ** 2 +
            Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

// ---- Stage rendering ----
function renderStages(stageKeys) {
  const labels = {
    region:    "Resolve region",
    fetch:     "Fetch DEM / imagery / OSM",
    heightmap: "Build heightmap",
    segment:   "Land-cover segmentation",
    splat:     "Material splatting",
    place:     "Object placement",
    export:    "BeamNG export",
  };
  stagesEl.innerHTML = "";
  stageKeys.forEach((key, i) => {
    const li = document.createElement("li");
    li.id = `stage-${key}`;
    li.innerHTML = `
      <span class="icon">${(i + 1).toString().padStart(2, "0")}</span>
      <span class="label">${labels[key] || key}</span>
      <span class="state">·</span>
      <div class="bar"><div></div></div>`;
    stagesEl.appendChild(li);
  });
}

function setStageState(key, state) {
  const li = document.getElementById(`stage-${key}`);
  if (!li) return;
  li.classList.remove("running", "done", "error");
  li.classList.add(state);
  const stateEl = li.querySelector(".state");
  if (stateEl) stateEl.textContent =
    state === "running" ? "…" :
    state === "done"    ? "✓" :
    state === "error"   ? "✗" : "·";
}

function setStageProgress(key, fraction) {
  const li = document.getElementById(`stage-${key}`);
  if (!li) return;
  const bar = li.querySelector(".bar > div");
  if (bar) bar.style.width = `${(fraction * 100).toFixed(0)}%`;
}

function addDownload(url, label, meta) {
  const wrap = document.getElementById("downloads");
  const list = document.getElementById("download-list");
  wrap.hidden = false;
  const li = document.createElement("li");
  const a = document.createElement("a");
  a.href = url; a.textContent = label; a.download = label;
  li.appendChild(a);
  if (meta) {
    const span = document.createElement("span");
    span.className = "meta"; span.textContent = meta;
    li.appendChild(span);
  }
  list.appendChild(li);
}

function formatBytes(n) {
  if (!n) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

// ---- Generate flow ----
generateBtn.addEventListener("click", async () => {
  if (!currentBBox) return;
  generateBtn.disabled = true;
  statusEl.className = "status running";
  statusEl.textContent = "starting…";
  stagesEl.innerHTML = "";
  document.getElementById("downloads").hidden = true;
  document.getElementById("download-list").innerHTML = "";
  window._satelliteUsed = false;
  window.MapNGPreview?.reset?.();

  const res = await fetch("/api/generate", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(currentBBox),
  });
  if (!res.ok) {
    const err = await res.text();
    statusEl.className = "status error";
    statusEl.textContent = `error: ${err}`;
    generateBtn.disabled = false;
    return;
  }
  const { job_id } = await res.json();
  statusEl.textContent = `running (${job_id})`;

  const es = new EventSource(`/api/jobs/${job_id}/events`);

  es.addEventListener("pipeline:start", (ev) => {
    const data = JSON.parse(ev.data);
    renderStages(data.stages);
  });
  es.addEventListener("stage:start", (ev) => {
    const { key } = JSON.parse(ev.data);
    setStageState(key, "running");
  });
  es.addEventListener("stage:progress", (ev) => {
    const { key, fraction } = JSON.parse(ev.data);
    setStageProgress(key, fraction);
  });
  es.addEventListener("stage:info", (ev) => {
    const data = JSON.parse(ev.data);
    if (data.key === "heightmap" && data.preview_url) {
      window.MapNGPreview?.setHeightmap?.({
        url: data.preview_url,
        sideMeters: data.side_m,
        minM: data.min_m,
        maxM: data.max_m,
      }).catch((e) => console.error("[preview]", e));
    }
    if (data.key === "segment" && data.satellite_url) {
      // Real Esri imagery is far better than the splat blend — use it
      window.MapNGPreview?.setTerrainTexture?.(data.satellite_url, data.satellite_normal_url);
      window._satelliteUsed = true;
    }
    if (data.key === "splat" && data.combined_url && !window._satelliteUsed) {
      window.MapNGPreview?.setTerrainTexture?.(data.combined_url);
    }
    if (data.key === "place") {
      if (data.buildings) window.MapNGPreview?.setBuildings?.(data.buildings);
      if (data.trees || data.hedges) {
        window.MapNGPreview?.setFoliage?.({ trees: data.trees || [], hedges: data.hedges || [] });
      }
      if (data.roads) window.MapNGPreview?.setRoads?.(data.roads);
    }
    if (data.key === "export" && data.zip_url) {
      addDownload(data.zip_url, `${data.level_name}.zip`, formatBytes(data.zip_bytes));
    }
  });
  es.addEventListener("stage:done", (ev) => {
    const { key } = JSON.parse(ev.data);
    setStageState(key, "done");
    setStageProgress(key, 1);
  });
  es.addEventListener("stage:error", (ev) => {
    const { key, message } = JSON.parse(ev.data);
    setStageState(key, "error");
    statusEl.className = "status error";
    statusEl.textContent = `error in ${key}: ${message}`;
  });
  es.addEventListener("pipeline:done", () => {
    statusEl.className = "status done";
    statusEl.textContent = "done";
    generateBtn.disabled = false;
    es.close();
  });
  es.addEventListener("pipeline:error", (ev) => {
    const { message } = JSON.parse(ev.data);
    statusEl.className = "status error";
    statusEl.textContent = `error: ${message}`;
    generateBtn.disabled = false;
    es.close();
  });
  es.onerror = () => { /* SSE auto-closes when server ends stream; ignore */ };
});
