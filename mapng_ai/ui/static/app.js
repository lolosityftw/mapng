/* MapNG-AI frontend
   Map picker (Leaflet) + progress feed (SSE) + library batch panel.
   3D preview lives in preview.js as an ES module. */

// ---- Active quality readout (the value lives on /library) ------------------
const qualityReadout = document.getElementById("quality-readout");
const sceneSize = document.getElementById("scene-size");
window._mapngQuality = "10k";

async function refreshActiveQuality() {
  try {
    const r = await fetch("/api/library/active-quality");
    const { quality } = await r.json();
    window._mapngQuality = quality;
    if (qualityReadout) qualityReadout.textContent = `quality: ${quality}`;
  } catch (e) {
    if (qualityReadout) qualityReadout.textContent = "quality: ?";
  }
}
refreshActiveQuality();

// ---- Library status indicator (link to /library for management) -----------
const libStatus = document.getElementById("lib-status");
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

// Bbox presets — picks a 2 km area centred on each location and draws it
const PRESETS = {
  cookstown_rural:   { lat: 54.6479, lon: -6.7456, zoom: 13 },
  cookstown_town:    { lat: 54.6420, lon: -6.7470, zoom: 14 },
  belfast_west:      { lat: 54.5973, lon: -5.9301, zoom: 14 },
  giants_causeway:   { lat: 55.2407, lon: -6.5117, zoom: 14 },
  newcastle_mournes: { lat: 54.2169, lon: -5.8901, zoom: 13 },
};
function applyPreset(key) {
  const p = PRESETS[key];
  if (!p) return;
  // 2 km square: ~0.018 deg lat, longitude scaled by cos(lat)
  const halfLat = 1.0 / 111.0;   // ~1 km lat
  const halfLon = halfLat / Math.cos(p.lat * Math.PI / 180);
  const bounds = L.latLngBounds(
    [p.lat - halfLat, p.lon - halfLon],
    [p.lat + halfLat, p.lon + halfLon],
  );
  map.flyToBounds(bounds, { duration: 0.7 });
  const rect = L.rectangle(bounds, { color: "#d29922", weight: 2 });
  setBBox(rect);
}
document.getElementById("preset")?.addEventListener("change", (ev) => {
  if (ev.target.value) applyPreset(ev.target.value);
});

// ---- Fullscreen preview -----------------------------------------------------
const previewPane = document.getElementById("preview-pane");
const fsBtn = document.getElementById("preview-fullscreen");
function togglePreviewFullscreen() {
  if (!document.fullscreenElement) {
    previewPane?.requestFullscreen?.().catch((e) => console.warn("fs request failed", e));
  } else {
    document.exitFullscreen?.();
  }
}
fsBtn?.addEventListener("click", togglePreviewFullscreen);
window.addEventListener("keydown", (ev) => {
  if (ev.key === "f" && ev.target.tagName !== "INPUT" && ev.target.tagName !== "TEXTAREA") {
    togglePreviewFullscreen();
  }
});

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
  window.MapNGPreview?.reset?.();
  // Pick up any change made to the library's active quality before this run
  await refreshActiveQuality();
  window.MapNGPreview?.invalidateGlbCache?.();

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
    if (data.key === "splat" && Array.isArray(data.layers)) {
      // Real game-quality terrain shader: per-class PBR tiles, opacity blend,
      // world-space tiling. Wins over any baked terrain.png.
      window.MapNGPreview?.setTerrainLayers?.(data.layers);
    }
    if (data.key === "place") {
      window._mapngLastPlace = data;        // remember for quality switches
      if (data.buildings) window.MapNGPreview?.setBuildings?.(data.buildings);
      if (data.trees || data.hedges) {
        window.MapNGPreview?.setFoliage?.({ trees: data.trees || [], hedges: data.hedges || [] });
      }
      if (data.roads) window.MapNGPreview?.setRoads?.(data.roads);
      const totalUnique = data.buildings ? new Set(data.buildings.map(b => b.shape).filter(Boolean)).size : 0;
      const treeUnique = data.trees ? new Set(data.trees.map(t => t.shape).filter(Boolean)).size : 0;
      sceneSize.innerHTML = `<strong>${data.n_buildings || 0}</strong> buildings (${totalUnique} unique meshes), ` +
                            `<strong>${data.n_trees || 0}</strong> trees (${treeUnique} unique)`;
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
