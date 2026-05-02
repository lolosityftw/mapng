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
    libStatus.textContent = `assets: ${built}/${s.catalogue_size} built`;
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

// Drawing — polygon (click to add points, double-click / first-point to
// finish) AND rectangle (still useful for quick squares). Polygon is the
// default tool now since "any shape" was the requested workflow.
const drawnItems = new L.FeatureGroup().addTo(map);
const drawControl = new L.Control.Draw({
  edit: { featureGroup: drawnItems, edit: true, remove: true },
  draw: {
    polyline: false, marker: false, circle: false, circlemarker: false,
    polygon: {
      allowIntersection: false,
      showArea: true,
      shapeOptions: { color: "#d29922", weight: 2, fillOpacity: 0.18 },
    },
    rectangle: { shapeOptions: { color: "#d29922", weight: 2, fillOpacity: 0.18 } },
  },
});
map.addControl(drawControl);

// `currentArea` holds the user's drawn area: bbox + (optional) polygon
// vertices in [lon, lat] order. The polygon is sent to the backend so
// features can be clipped to the actual selected shape.
let currentArea = null;
const bboxReadout = document.getElementById("bbox-readout");
const generateBtn = document.getElementById("generate");
const statusEl = document.getElementById("status");
const stagesEl = document.getElementById("stages");

function _layerToArea(layer) {
  // Returns { bbox, polygon } where polygon is null for rectangles
  // (we'll let the backend treat null as "use the bbox itself").
  let polygon = null;
  if (typeof layer.getLatLngs === "function" && !(layer instanceof L.Rectangle)) {
    // Polygon: getLatLngs() → [[ {lat,lng}, ... ]] for simple polygons
    const rings = layer.getLatLngs();
    const ring = Array.isArray(rings[0]) ? rings[0] : rings;
    polygon = ring.map((p) => [p.lng, p.lat]);
    // Ensure at least 3 distinct points
    if (polygon.length < 3) return null;
  }
  const b = layer.getBounds();
  return {
    bbox: {
      west:  b.getWest(), south: b.getSouth(),
      east:  b.getEast(), north: b.getNorth(),
    },
    polygon,
  };
}

// ---- Persistent area selection -----------------------------------------
// Saves the user's drawn polygon/rectangle to localStorage so it
// re-appears on the map after a server restart or browser reload.
const AREA_STORAGE_KEY = "mapng_last_area";
function _saveArea(area) {
  try { localStorage.setItem(AREA_STORAGE_KEY, JSON.stringify(area)); }
  catch { /* localStorage may be full or disabled */ }
}
function _loadSavedArea() {
  try {
    const s = localStorage.getItem(AREA_STORAGE_KEY);
    return s ? JSON.parse(s) : null;
  } catch { return null; }
}

function setArea(layer) {
  drawnItems.clearLayers();
  drawnItems.addLayer(layer);
  const area = _layerToArea(layer);
  if (!area) {
    currentArea = null;
    generateBtn.disabled = true;
    bboxReadout.textContent = "draw a polygon (click points, double-click to finish) or a rectangle";
    return;
  }
  currentArea = area;
  _saveArea(area);
  const b = area.bbox;
  const widthKm  = haversineKm(b.south, b.west, b.south, b.east);
  const heightKm = haversineKm(b.south, b.west, b.north, b.west);
  // BeamNG terrain is square, so the level side is whichever's larger,
  // clamped 0.5–8 km. Show the user what they'll actually get.
  const sizeChoice = document.getElementById("size-choice")?.value || "auto";
  let effectiveKm;
  if (sizeChoice === "auto") {
    effectiveKm = Math.min(8.0, Math.max(0.5, Math.max(widthKm, heightKm)));
  } else {
    effectiveKm = parseFloat(sizeChoice) / 1000;
  }
  const shapeWord = area.polygon ? `polygon (${area.polygon.length} pts)` : "rectangle";
  bboxReadout.textContent =
    `${shapeWord} ${widthKm.toFixed(2)} × ${heightKm.toFixed(2)} km bbox → ` +
    `level ${effectiveKm.toFixed(2)} km square · ` +
    `W ${b.west.toFixed(5)}, S ${b.south.toFixed(5)}, ` +
    `E ${b.east.toFixed(5)}, N ${b.north.toFixed(5)}`;
  generateBtn.disabled = false;
  // Async lookup of OSM feature counts so the user knows roughly what's
  // in the bbox before they hit Generate. Debounced so dragging a
  // rectangle doesn't spam Overpass.
  _scheduleAreaPreview(area);
}

// ---- Live OSM-feature preview for the drawn area ------------------------
let _previewTimer = null;
const _previewReadout = (() => {
  // Inject a small readout next to the bbox readout if it doesn't exist.
  let el = document.getElementById("area-preview-readout");
  if (el) return el;
  el = document.createElement("div");
  el.id = "area-preview-readout";
  el.className = "area-preview-readout";
  el.style.cssText = "font-size: 12px; color: #7d8590; padding: 4px 0;";
  el.textContent = "";
  bboxReadout?.parentNode?.insertBefore(el, bboxReadout.nextSibling);
  return el;
})();
function _scheduleAreaPreview(area) {
  if (_previewTimer) clearTimeout(_previewTimer);
  _previewReadout.textContent = "(counting OSM features…)";
  _previewTimer = setTimeout(async () => {
    try {
      const res = await fetch("/api/preview-area", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(area.bbox),
      });
      if (!res.ok) {
        _previewReadout.textContent = "(preview unavailable)";
        return;
      }
      const c = await res.json();
      const parts = [];
      if (c.n_buildings) parts.push(`${c.n_buildings} buildings`);
      if (c.n_roads)     parts.push(`${c.n_roads} roads`);
      if (c.n_landuse)   parts.push(`${c.n_landuse} landuse polys`);
      if (c.n_water)     parts.push(`${c.n_water} water`);
      if (c.n_barriers)  parts.push(`${c.n_barriers} barriers`);
      _previewReadout.textContent = parts.length
        ? "OSM in bbox: " + parts.join(" · ")
        : "OSM in bbox: empty (likely an unmapped area)";
    } catch {
      _previewReadout.textContent = "(preview unavailable)";
    }
  }, 600);
}
// Backwards-compat alias used by applyPreset() below.
const setBBox = setArea;
// Re-render readout when size choice changes
document.getElementById("size-choice")?.addEventListener("change", () => {
  const layers = drawnItems.getLayers();
  if (layers.length) setArea(layers[0]);
});

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

// ---- Persisted size + zoom choices --------------------------------------
const _CHOICE_KEYS = { size: "mapng_size_choice", zoom: "mapng_zoom_choice" };
for (const [field, key] of Object.entries(_CHOICE_KEYS)) {
  const sel = document.getElementById(`${field}-choice`);
  if (!sel) continue;
  try {
    const saved = localStorage.getItem(key);
    if (saved !== null && [...sel.options].some((o) => o.value === saved)) {
      sel.value = saved;
    }
  } catch { /* ignored */ }
  sel.addEventListener("change", () => {
    try { localStorage.setItem(key, sel.value); } catch { /* ignored */ }
  });
}

// ---- Restore previously-drawn area on page load -------------------------
// Runs after the map + draw controls are wired. Re-creates the layer and
// flies to it so the user keeps their selection across sessions.
(function _restoreSavedArea() {
  const saved = _loadSavedArea();
  if (!saved || !saved.bbox) return;
  let layer;
  if (saved.polygon && Array.isArray(saved.polygon) && saved.polygon.length >= 3) {
    // Polygon — saved as [[lon, lat], ...]; Leaflet wants [[lat, lon], ...]
    const latLngs = saved.polygon.map(([lon, lat]) => [lat, lon]);
    layer = L.polygon(latLngs, { color: "#d29922", weight: 2, fillOpacity: 0.18 });
  } else {
    const b = saved.bbox;
    layer = L.rectangle(
      [[b.south, b.west], [b.north, b.east]],
      { color: "#d29922", weight: 2, fillOpacity: 0.18 },
    );
  }
  setArea(layer);
  try { map.flyToBounds(layer.getBounds(), { duration: 0.5 }); } catch { /* ignored */ }
})();

// ---- Grass tint sliders -----------------------------------------------------
const grassHueIn = document.getElementById("grass-hue");
const grassSatIn = document.getElementById("grass-sat");
const grassBriIn = document.getElementById("grass-bri");
const grassHueV = document.getElementById("grass-hue-v");
const grassSatV = document.getElementById("grass-sat-v");
const grassBriV = document.getElementById("grass-bri-v");
const grassReset = document.getElementById("grass-reset");

// Defaults locked to user-tuned values: hue=60 (mildly blue-leaning
// green for an overcast NI sky), sat=1.15, bri=0.65. Saved settings in
// localStorage still win — hit Reset to fall back to these.
const GRASS_DEFAULTS = { hue: 60, sat: 1.15, bri: 0.65 };
function _loadGrass() {
  try {
    const s = JSON.parse(localStorage.getItem("mapng_grass") || "{}");
    return { ...GRASS_DEFAULTS, ...s };
  } catch { return { ...GRASS_DEFAULTS }; }
}
function _saveGrass(g) {
  try { localStorage.setItem("mapng_grass", JSON.stringify(g)); } catch {}
}
// Hue rotation maps to an RGB tint multiplier centred on green. Hue=0
// matches the vivid-Irish-green base in terrain_shader.js. Negative hue
// pushes yellow-green, positive hue pushes blue-green.
function _hueToTint(hueDeg) {
  const base = [0.78, 1.22, 0.62];
  const r = base[0] + hueDeg * -0.004;
  const g = base[1] + hueDeg * 0.002;
  const b = base[2] + hueDeg * 0.005;
  return [Math.max(0, r), Math.max(0, g), Math.max(0, b)];
}
function applyGrass(g) {
  const m = window._terrainMaterial;
  if (!m) return;
  const [r, gn, b] = _hueToTint(g.hue);
  m.uniforms.grassTint.value.setRGB(r, gn, b);
  m.uniforms.grassSaturation.value = g.sat;
  m.uniforms.grassBrightness.value = g.bri;
  if (grassHueV) grassHueV.textContent = `${g.hue}°`;
  if (grassSatV) grassSatV.textContent = g.sat.toFixed(2);
  if (grassBriV) grassBriV.textContent = g.bri.toFixed(2);
}
function _readSliders() {
  const g = _loadGrass();
  if (grassHueIn) grassHueIn.value = g.hue;
  if (grassSatIn) grassSatIn.value = g.sat;
  if (grassBriIn) grassBriIn.value = g.bri;
  applyGrass(g);
}
[grassHueIn, grassSatIn, grassBriIn].forEach((el) => {
  el?.addEventListener("input", () => {
    const g = {
      hue: parseFloat(grassHueIn.value),
      sat: parseFloat(grassSatIn.value),
      bri: parseFloat(grassBriIn.value),
    };
    _saveGrass(g);
    applyGrass(g);
  });
});
grassReset?.addEventListener("click", () => {
  _saveGrass(GRASS_DEFAULTS);
  _readSliders();
});

// ---- Atmosphere panel: wet roads + cloud shadow + HDRI download ----------
const wetIn  = document.getElementById("atmos-wetness");
const wetV   = document.getElementById("atmos-wetness-v");
const cloudIn = document.getElementById("atmos-cloud");
const cloudV  = document.getElementById("atmos-cloud-v");
const hdriBtn = document.getElementById("hdri-download");
const hdriStatus = document.getElementById("hdri-status");

function _applyAtmos({ wetness, cloud }) {
  const m = window._terrainMaterial;
  if (!m) return;
  if (typeof wetness === "number" && m.uniforms.wetness)
    m.uniforms.wetness.value = wetness;
  if (typeof cloud === "number" && m.uniforms.cloudShadowStrength)
    m.uniforms.cloudShadowStrength.value = cloud;
  if (wetV)   wetV.textContent   = (wetness ?? 0).toFixed(2);
  if (cloudV) cloudV.textContent = (cloud ?? 0).toFixed(2);
}
wetIn?.addEventListener("input", () => _applyAtmos({
  wetness: parseFloat(wetIn.value), cloud: parseFloat(cloudIn?.value ?? "0.32"),
}));
cloudIn?.addEventListener("input", () => _applyAtmos({
  wetness: parseFloat(wetIn?.value ?? "0"), cloud: parseFloat(cloudIn.value),
}));
window._mapngApplyAtmos = () => _applyAtmos({
  wetness: parseFloat(wetIn?.value ?? "0"),
  cloud:   parseFloat(cloudIn?.value ?? "0.32"),
});

async function _refreshHdriStatus() {
  if (!hdriStatus) return;
  try {
    const r = await fetch("/api/sky/status");
    const s = await r.json();
    const have = Object.keys(s.cached || {}).length > 0;
    hdriStatus.textContent = have ? `cached (${Math.round((Object.values(s.cached)[0] || 0) / 1024)} KB)` : "not downloaded";
    hdriStatus.classList.toggle("on", have);
    if (hdriBtn) hdriBtn.disabled = false;
  } catch (e) {
    hdriStatus.textContent = "(status unavailable)";
  }
}
hdriBtn?.addEventListener("click", async () => {
  if (!hdriStatus) return;
  hdriBtn.disabled = true;
  hdriStatus.textContent = "downloading…";
  try {
    const r = await fetch("/api/sky/download", { method: "POST" });
    if (!r.ok) throw new Error(await r.text());
    hdriStatus.textContent = "downloaded — reload the page to apply";
  } catch (e) {
    hdriStatus.textContent = "download failed: " + (e.message || e);
    hdriBtn.disabled = false;
  }
});
_refreshHdriStatus();
// Reload sliders + reapply whenever a new pipeline run replaces the terrain
// (the material changes per setHeightmap, so the tint must be reapplied).
const _origSetHeightmap = () => null;     // placeholder — wire below
window._mapngApplyGrass = _readSliders;

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

map.on(L.Draw.Event.CREATED, (e) => setArea(e.layer));
map.on("draw:edited", (e) => {
  // After in-place edit (vertex drag), re-derive the bbox/polygon
  e.layers.eachLayer((layer) => setArea(layer));
});
map.on("draw:deleted", () => {
  currentArea = null;
  try { localStorage.removeItem(AREA_STORAGE_KEY); } catch { /* ignored */ }
  bboxReadout.textContent = "draw a polygon (click points, double-click to finish) or a rectangle";
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
  if (!currentArea) return;
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

  // Body carries the bbox + optional polygon vertices + size/zoom
  // overrides. `polygon: null` means "use the bbox"; otherwise the
  // backend clips placements to the polygon.
  const sizeChoice = document.getElementById("size-choice")?.value || "auto";
  const zoomChoice = document.getElementById("zoom-choice")?.value || "auto";
  const body = {
    ...currentArea.bbox,
    polygon: currentArea.polygon,
    size_m: sizeChoice === "auto" ? null : parseFloat(sizeChoice),
    imagery_zoom: zoomChoice === "auto" ? null : parseInt(zoomChoice, 10),
  };
  const res = await fetch("/api/generate", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
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
