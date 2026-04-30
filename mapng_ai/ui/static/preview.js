/* MapNG-AI 3D preview — game-quality scene.

   Sky:    fragment-shader gradient + animated procedural clouds
   Sun:    bright disc, halo, lens-flare-ish glow, real DirectionalLight
   Terrain: real Esri imagery wraps the displaced mesh (when available)
   Trees: instanced, per-instance scale + canopy colour jitter
   Buildings: pitched-roof unit DAE, per-type colour, light skyline jitter
   Atmosphere: distance fog tinted to horizon

   Public API used by app.js:
     reset()
     setHeightmap({url, sideMeters, minM, maxM})
     setTerrainTexture(url)
     setBuildings([{x,y,z,yaw,scale,color,type}])
     setFoliage({trees, hedges})
     setRoads([{nodes,width}])
*/

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { createTerrainMaterial, applyTerrainLayers } from "/static/terrain_shader.js";

const container = document.getElementById("preview");
console.info("[preview] booting");

const state = {
  scene: null, camera: null, renderer: null, controls: null,
  terrainMesh: null, terrainGroup: null,
  buildingsGroup: null, foliageGroup: null, roadsGroup: null,
  sun: null, sunMesh: null, sunHalo: null, sunLight: null,
  sky: null, clock: new THREE.Clock(),
  placeholder: null,
  sideMeters: 2000,
  heightRange: { min: 0, max: 100 },
};

if (!container) {
  console.error("[preview] no #preview element");
} else if (!window.WebGLRenderingContext) {
  container.innerHTML =
    '<div style="padding:20px;color:#f85149">WebGL not supported in this browser.</div>';
} else {
  try { boot(); }
  catch (err) {
    console.error("[preview] init failed:", err);
    container.innerHTML =
      `<div style="padding:20px;color:#f85149;font-family:ui-monospace">preview failed: ${String(err)}</div>`;
  }
}

// ---------------------------------------------------------------------------
function makeSky() {
  // Inner: gradient + scrolling clouds. Outer: glowing horizon ring.
  const geo = new THREE.SphereGeometry(9000, 48, 24);
  const mat = new THREE.ShaderMaterial({
    side: THREE.BackSide,
    depthWrite: false,
    uniforms: {
      time:        { value: 0.0 },
      sunDir:      { value: new THREE.Vector3(0.5, 0.7, 0.4).normalize() },
      sunColor:    { value: new THREE.Color(0xfff0c8) },
      topColor:    { value: new THREE.Color(0x3766a8) },
      midColor:    { value: new THREE.Color(0x9bc6e5) },
      bottomColor: { value: new THREE.Color(0xe7eef0) },
      cloudColor:  { value: new THREE.Color(0xfdfdfd) },
      cloudShadow: { value: new THREE.Color(0xa3b5c8) },
    },
    vertexShader: `
      varying vec3 vNorm;
      void main() {
        vNorm = normalize(position);
        gl_Position = projectionMatrix * viewMatrix * modelMatrix * vec4(position, 1.0);
      }`,
    fragmentShader: `
      uniform float time;
      uniform vec3 sunDir;
      uniform vec3 sunColor;
      uniform vec3 topColor;
      uniform vec3 midColor;
      uniform vec3 bottomColor;
      uniform vec3 cloudColor;
      uniform vec3 cloudShadow;
      varying vec3 vNorm;

      // Fast hash → noise
      float hash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
      float vnoise(vec2 p) {
        vec2 i = floor(p), f = fract(p);
        float a = hash(i);
        float b = hash(i + vec2(1.0, 0.0));
        float c = hash(i + vec2(0.0, 1.0));
        float d = hash(i + vec2(1.0, 1.0));
        vec2 u = f * f * (3.0 - 2.0 * f);
        return mix(a, b, u.x) + (c - a) * u.y * (1.0 - u.x) + (d - b) * u.x * u.y;
      }
      float fbm(vec2 p) {
        float v = 0.0, a = 0.5;
        for (int i = 0; i < 5; i++) { v += a * vnoise(p); p *= 2.0; a *= 0.5; }
        return v;
      }

      void main() {
        vec3 N = normalize(vNorm);
        float h = clamp(N.y, 0.0, 1.0);
        // Three-stop vertical gradient
        vec3 lower = mix(bottomColor, midColor, smoothstep(0.0, 0.3, h));
        vec3 base = mix(lower, topColor, smoothstep(0.3, 1.0, h));

        // Sun disc + glow + halo (warm tint near the sun direction)
        float sunDot = clamp(dot(N, normalize(sunDir)), -1.0, 1.0);
        float sunDisc = smoothstep(0.99965, 0.99996, sunDot);
        float sunHalo = pow(max(sunDot, 0.0), 64.0) * 0.50;
        float sunGlow = pow(max(sunDot, 0.0), 8.0)  * 0.18;
        vec3 atmos = mix(base, sunColor, sunGlow + sunHalo * 0.6);

        // Clouds projected onto upper hemisphere
        vec2 uv = vec2(atan(N.x, N.z), N.y * 2.5);
        uv += vec2(time * 0.010, 0.0);
        float c = fbm(uv * 1.5);
        c = smoothstep(0.55, 0.85, c);
        c *= smoothstep(0.05, 0.45, h);
        vec3 cloud = mix(cloudShadow, cloudColor, smoothstep(0.55, 0.95, fbm(uv * 3.0 + 13.0)));
        cloud *= mix(vec3(1.0), sunColor, max(sunDot, 0.0) * 0.4 + 0.1);

        vec3 col = mix(atmos, cloud, c);
        col += sunDisc * sunColor * 1.6;
        gl_FragColor = vec4(col, 1.0);
      }`,
  });
  return new THREE.Mesh(geo, mat);
}

function boot() {
  const scene = new THREE.Scene();
  const sky = makeSky();
  scene.add(sky);

  scene.fog = new THREE.Fog(0xc7d6e0, 1500, 7000);

  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.domElement.style.display = "block";
  container.appendChild(renderer.domElement);

  const camera = new THREE.PerspectiveCamera(50, 1, 0.5, 20000);
  camera.position.set(2400, 1500, 2400);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.maxDistance = 8000;
  controls.minDistance = 30;
  controls.maxPolarAngle = Math.PI * 0.495;   // never below horizon
  controls.target.set(0, 0, 0);

  // Sun: bright disc + multi-layer halo + light + lens-flare-ish bloom replacement
  const sunGroup = new THREE.Group();
  sunGroup.position.set(1500, 2200, 1100);

  const sunDisc = new THREE.Mesh(
    new THREE.SphereGeometry(140, 32, 16),
    new THREE.MeshBasicMaterial({ color: 0xfff7d8, fog: false }),
  );
  sunGroup.add(sunDisc);

  const halo = new THREE.Mesh(
    new THREE.SphereGeometry(280, 32, 16),
    new THREE.MeshBasicMaterial({ color: 0xfff0a0, transparent: true, opacity: 0.32, fog: false, depthWrite: false }),
  );
  sunGroup.add(halo);

  const halo2 = new THREE.Mesh(
    new THREE.SphereGeometry(560, 32, 16),
    new THREE.MeshBasicMaterial({ color: 0xffe070, transparent: true, opacity: 0.10, fog: false, depthWrite: false }),
  );
  sunGroup.add(halo2);
  scene.add(sunGroup);

  const sunLight = new THREE.DirectionalLight(0xfff1d6, 1.5);
  sunLight.position.copy(sunGroup.position);
  sunLight.castShadow = true;
  sunLight.shadow.mapSize.set(2048, 2048);
  sunLight.shadow.camera.near = 50;
  sunLight.shadow.camera.far = 6000;
  sunLight.shadow.camera.left = -1500;
  sunLight.shadow.camera.right = 1500;
  sunLight.shadow.camera.top = 1500;
  sunLight.shadow.camera.bottom = -1500;
  sunLight.shadow.bias = -0.0003;
  scene.add(sunLight);
  scene.add(sunLight.target);

  scene.add(new THREE.HemisphereLight(0xb6cdde, 0x4a4a32, 0.55));

  // Empty-stage placeholder
  const placeholder = new THREE.Mesh(
    new THREE.IcosahedronGeometry(220, 0),
    new THREE.MeshStandardMaterial({ color: 0xd29922, flatShading: true, roughness: 0.6 }),
  );
  placeholder.position.y = 240;
  placeholder.castShadow = true;
  scene.add(placeholder);

  const grid = new THREE.GridHelper(2000, 20, 0x6080a0, 0x3a4a5a);
  grid.position.y = 0.01;
  scene.add(grid);
  state.referenceGrid = grid;

  function resize() {
    const w = container.clientWidth || 400;
    const h = container.clientHeight || 300;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h, false);
  }
  new ResizeObserver(resize).observe(container);
  resize();

  (function loop() {
    requestAnimationFrame(loop);
    const dt = state.clock.getDelta();
    if (sky.material.uniforms?.time) sky.material.uniforms.time.value += dt;
    if (state.terrainMesh?.material?.userData?.tickTime) {
      state.terrainMesh.material.userData.tickTime(dt);
    }
    if (state.placeholder) {
      state.placeholder.rotation.y += 0.005;
      state.placeholder.rotation.x += 0.002;
    }
    controls.update();
    renderer.render(scene, camera);
  })();

  Object.assign(state, {
    scene, camera, renderer, controls,
    sun: sunGroup, sunMesh: sunDisc, sunHalo: halo, sunLight,
    sky, placeholder,
  });
  console.info("[preview] ready");
}

// ---------------------------------------------------------------------------
function reset() {
  if (!state.scene) return;
  if (state.terrainMesh) {
    state.scene.remove(state.terrainMesh);
    state.terrainMesh.geometry.dispose();
    state.terrainMesh.material.dispose();
    state.terrainMesh = null;
  }
  for (const key of ["buildingsGroup", "foliageGroup", "roadsGroup"]) {
    if (state[key]) {
      state.scene.remove(state[key]);
      state[key].traverse((o) => {
        if (o.isMesh || o.isLine) { o.geometry?.dispose?.(); o.material?.dispose?.(); }
      });
      state[key] = null;
    }
  }
  if (state.referenceGrid) state.referenceGrid.visible = true;
  if (!state.placeholder.parent) state.scene.add(state.placeholder);
}

async function setHeightmap({ url, sideMeters, minM, maxM, segments = 256 }) {
  if (!state.scene) return;
  console.info("[preview] setHeightmap", { url, sideMeters, minM, maxM });
  state.sideMeters = sideMeters;
  state.heightRange = { min: minM, max: maxM };

  const img = await loadImage(url);
  const cv = document.createElement("canvas");
  cv.width = img.width; cv.height = img.height;
  const cx = cv.getContext("2d");
  cx.drawImage(img, 0, 0);
  const pixels = cx.getImageData(0, 0, img.width, img.height).data;

  const range = (maxM - minM) || 1;
  const geo = new THREE.PlaneGeometry(sideMeters, sideMeters, segments, segments);
  const pos = geo.attributes.position;
  for (let i = 0; i < pos.count; i++) {
    const u = (pos.getX(i) + sideMeters / 2) / sideMeters;
    const v = 1 - (pos.getY(i) + sideMeters / 2) / sideMeters;
    const px = Math.min(img.width - 1, Math.max(0, Math.floor(u * img.width)));
    const py = Math.min(img.height - 1, Math.max(0, Math.floor(v * img.height)));
    pos.setZ(i, minM + (pixels[(py * img.width + px) * 4] / 255) * range);
  }
  geo.rotateX(-Math.PI / 2);
  geo.computeVertexNormals();

  // Fresh shader material every heightmap. The splat stage will populate
  // textures into it via applyTerrainLayers().
  const mat = createTerrainMaterial({
    tileSize: 4.0,
    fogColor: 0xc7d6e0,
    fogNear: Math.max(800, sideMeters * 0.6),
    fogFar:  Math.max(4000, sideMeters * 3.0),
    terrainHalf: sideMeters / 2,
  });

  if (state.terrainMesh) {
    state.scene.remove(state.terrainMesh);
    state.terrainMesh.geometry.dispose();
    state.terrainMesh.material.dispose();
  }
  if (state.placeholder?.parent) state.scene.remove(state.placeholder);
  if (state.referenceGrid) state.referenceGrid.visible = false;

  const mesh = new THREE.Mesh(geo, mat);
  mesh.receiveShadow = true;
  state.scene.add(mesh);
  state.terrainMesh = mesh;

  if (state.controls && state.camera) {
    const dist = sideMeters * 1.3;
    const verticalCentre = (minM + maxM) / 2;
    state.camera.position.set(dist * 0.7, dist * 0.55, dist * 0.7);
    state.controls.target.set(0, verticalCentre, 0);
    state.controls.update();
  }

  if (state.sun && state.sunLight) {
    const elev = maxM + sideMeters * 1.0;
    state.sun.position.set(sideMeters * 0.75, elev, sideMeters * 0.55);
    state.sunLight.position.copy(state.sun.position);
    state.sunLight.target.position.set(0, (minM + maxM) / 2, 0);
    state.sunLight.target.updateMatrixWorld();
    // Push sun direction into sky shader so the disc + halo follow the light
    if (state.sky?.material?.uniforms?.sunDir) {
      state.sky.material.uniforms.sunDir.value
        .copy(state.sun.position).normalize();
    }
  }
}

async function setTerrainLayers(layers) {
  if (!state.terrainMesh) {
    console.warn("[preview] setTerrainLayers before terrain mesh");
    return;
  }
  console.info("[preview] setTerrainLayers:", layers.map(l => l.key).join(","));
  // Sort by coverage descending — top 4 wins
  const sorted = [...layers].sort((a, b) => b.coverage_pct - a.coverage_pct);
  try {
    const used = await applyTerrainLayers(state.terrainMesh.material, sorted, {
      sunDir: state.sunLight ? state.sunLight.position.clone().normalize() : undefined,
    });
    console.info("[preview] terrain shader: %d layers active", used);
  } catch (err) {
    console.error("[preview] terrain shader load failed:", err);
  }
}

// ---------------------------------------------------------------------------
// Pitched (steep ridge) and flat (industrial) roof variants share the same
// vertex/index layout so InstancedMesh can swap them transparently.
function _buildingGeometry({ flat = false } = {}) {
  const boxH = flat ? 1.0 : 0.7;
  const ridgeZ = flat ? 1.0 : 1.0;
  const verts = new Float32Array([
    -0.5, 0.0, -0.5,   0.5, 0.0, -0.5,   0.5, 0.0,  0.5,  -0.5, 0.0,  0.5,
    -0.5, boxH, -0.5,  0.5, boxH, -0.5,  0.5, boxH, 0.5,  -0.5, boxH, 0.5,
    -0.5, ridgeZ, 0.0, 0.5, ridgeZ, 0.0,
  ]);
  const idx = new Uint16Array([
    0,2,1, 0,3,2,
    0,1,5, 0,5,4,
    1,2,6, 1,6,5,
    2,3,7, 2,7,6,
    3,0,4, 3,4,7,
    4,8,7,
    5,6,9,
    4,5,9, 4,9,8,
    7,9,6, 7,8,9,
  ]);
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(verts, 3));
  geo.setIndex(new THREE.BufferAttribute(idx, 1));
  geo.computeVertexNormals();
  return geo;
}

const _FLAT_ROOF_TYPES = new Set([
  "industrial", "warehouse", "garage", "shed", "barn",
  "commercial", "retail", "shop", "office",
]);
function _isFlatRoof(t) { return _FLAT_ROOF_TYPES.has(t); }

// ---------------------------------------------------------------------------
// GLB cache — loaded once per shape path, cloned per instance
// ---------------------------------------------------------------------------
const _gltfLoader = new GLTFLoader();
const _glbCache = new Map();      // shape relpath → { scene, bbox } | Promise

function _showLoadingBadge(text) {
  let el = document.getElementById("preview-loading");
  if (!el) {
    el = document.createElement("div");
    el.id = "preview-loading";
    el.style.cssText = `
      position:absolute; top:10px; left:50%; transform:translateX(-50%);
      background:#14171c; border:1px solid #2a2f37; color:#adbac7;
      padding:6px 14px; border-radius:999px; font-size:11px;
      font-family:ui-monospace,Consolas,monospace; pointer-events:none; z-index:5;`;
    container.appendChild(el);
  }
  el.textContent = text;
  el.style.display = "block";
}
function _hideLoadingBadge() {
  const el = document.getElementById("preview-loading");
  if (el) el.style.display = "none";
}

async function _loadGlb(shapeRelpath) {
  const quality = window._mapngQuality || "10k";
  const cacheKey = `${shapeRelpath}|${quality}`;
  let cached = _glbCache.get(cacheKey);
  if (cached) return cached;
  const url = `/api/asset?path=${encodeURIComponent(shapeRelpath)}&quality=${quality}`;
  const promise = new Promise((resolve, reject) => {
    _gltfLoader.load(
      url,
      (gltf) => {
        const scene = gltf.scene;
        scene.traverse((o) => {
          if (o.isMesh) {
            o.castShadow = true;
            o.receiveShadow = true;
            // Force normalised UVs and standard material if missing
            if (!o.material) o.material = new THREE.MeshStandardMaterial({ color: 0x999999 });
          }
        });
        const bbox = new THREE.Box3().setFromObject(scene);
        const size = new THREE.Vector3(); bbox.getSize(size);
        if (size.x < 1e-3) size.x = 1;
        if (size.y < 1e-3) size.y = 1;
        if (size.z < 1e-3) size.z = 1;
        resolve({ scene, bboxSize: size, bboxMin: bbox.min.clone() });
      },
      undefined,
      (err) => reject(err),
    );
  });
  _glbCache.set(cacheKey, promise);
  try {
    const result = await promise;
    _glbCache.set(cacheKey, result);
    return result;
  } catch (e) {
    _glbCache.delete(cacheKey);
    throw e;
  }
}

function invalidateGlbCache() {
  // Drop scene refs so old quality assets can be GC'd
  for (const v of _glbCache.values()) {
    if (v && typeof v === "object" && v.scene) {
      v.scene.traverse?.((o) => {
        if (o.isMesh) {
          o.geometry?.dispose?.();
          (Array.isArray(o.material) ? o.material : [o.material]).forEach((m) => {
            if (!m) return;
            for (const k of ["map", "normalMap", "roughnessMap", "metalnessMap", "emissiveMap"]) {
              m[k]?.dispose?.();
            }
            m.dispose?.();
          });
        }
      });
    }
  }
  _glbCache.clear();
}

// Placeholder geometry used when a GLB is missing or fails to load
function _placeholderInstance({ color, flat, sx, sy, sz, yaw, x, y, z }) {
  const geo = _buildingGeometry({ flat });
  const mat = new THREE.MeshStandardMaterial({ color, roughness: 0.85 });
  const m = new THREE.Mesh(geo, mat);
  m.scale.set(sx, sz, sy);
  m.position.set(x, z, -y);
  m.rotation.y = -yaw;
  m.castShadow = true; m.receiveShadow = true;
  return m;
}

// Buildings within this radius from camera target render as full GLBs;
// beyond that, instanced impostor boxes (1 draw call per colour bucket).
const BUILDING_GLB_RADIUS = 500;     // metres
const MAX_GLB_BUILDINGS = 200;

async function setBuildings(buildings) {
  if (!state.scene) return;
  console.info("[preview] setBuildings:", buildings.length);
  if (state.buildingsGroup) {
    state.scene.remove(state.buildingsGroup);
    state.buildingsGroup.traverse((o) => {
      if (o.isMesh) { o.geometry?.dispose?.(); o.material?.dispose?.(); }
    });
  }
  const group = new THREE.Group();
  state.scene.add(group);
  state.buildingsGroup = group;

  // Distance-sort and split into close (full GLB) and distant (impostor)
  const cameraTarget = state.controls?.target || new THREE.Vector3();
  const dist2 = (b) => {
    const dx = b.x - cameraTarget.x;
    const dy = -b.y - cameraTarget.z;
    return dx * dx + dy * dy;
  };
  const sorted = buildings.slice().sort((a, b) => dist2(a) - dist2(b));
  const closeBuildings = sorted
    .filter((b) => Math.sqrt(dist2(b)) < BUILDING_GLB_RADIUS)
    .slice(0, MAX_GLB_BUILDINGS);
  const farBuildings = sorted.filter((b) => !closeBuildings.includes(b));

  // ---- Distant impostors: one InstancedMesh per (color, roof-style) bucket ----
  if (farBuildings.length) {
    const pitchedGeo = _buildingGeometry({ flat: false });
    const flatGeo = _buildingGeometry({ flat: true });
    const buckets = new Map();
    for (const b of farBuildings) {
      const flat = _isFlatRoof(b.type);
      const key = `${b.color}|${flat ? "f" : "p"}`;
      if (!buckets.has(key)) buckets.set(key, { color: b.color, flat, list: [] });
      buckets.get(key).list.push(b);
    }
    const m = new THREE.Matrix4(); const q = new THREE.Quaternion();
    for (const { color, flat, list } of buckets.values()) {
      const mat = new THREE.MeshStandardMaterial({ color, roughness: 0.85 });
      const inst = new THREE.InstancedMesh(flat ? flatGeo : pitchedGeo, mat, list.length);
      inst.castShadow = inst.receiveShadow = true;
      for (let i = 0; i < list.length; i++) {
        const b = list[i];
        const [sx, sy, sz] = b.scale;
        q.setFromAxisAngle(new THREE.Vector3(0, 1, 0), -b.yaw);
        m.compose(
          new THREE.Vector3(b.x, b.z, -b.y),
          q,
          new THREE.Vector3(sx, sz, sy),
        );
        inst.setMatrixAt(i, m);
      }
      inst.instanceMatrix.needsUpdate = true;
      group.add(inst);
    }
  }

  // ---- Close buildings → full GLB clones (existing path) ----
  buildings = closeBuildings;

  // Bucket by shape path (so each unique GLB loads once)
  const byShape = new Map();
  for (const b of buildings) {
    const key = b.shape || "_placeholder";
    if (!byShape.has(key)) byShape.set(key, []);
    byShape.get(key).push(b);
  }
  const libShapes = [...byShape.keys()].filter(
    (s) => s.startsWith("art/shapes/buildings_lib/") || s.startsWith("art/shapes/buildings_ai/"));
  if (libShapes.length) _showLoadingBadge(`loading ${libShapes.length} building meshes…`);

  const isLibrary = (p) => p && (p.startsWith("art/shapes/buildings_lib/") || p.startsWith("art/shapes/buildings_ai/"));
  const placeholderForBucket = (instances) => {
    for (const b of instances) {
      const [sx, sy, sz] = b.scale;
      group.add(_placeholderInstance({
        color: b.color, flat: _isFlatRoof(b.type),
        sx, sy, sz, yaw: b.yaw, x: b.x, y: b.y, z: b.z,
      }));
    }
  };

  // Kick all GLB loads in parallel
  let loaded = 0;
  await Promise.all([...byShape.entries()].map(async ([shape, instances]) => {
    if (!isLibrary(shape)) {
      placeholderForBucket(instances);
      return;
    }
    let glb;
    try {
      glb = await _loadGlb(shape);
    } catch (err) {
      console.warn("[preview] GLB load failed, falling back:", shape, err);
      placeholderForBucket(instances);
      return;
    }

    // The GLB is its own size; we want each instance to be `scale` metres in
    // BeamNG world units. Compute the per-axis factor.
    const { bboxSize, bboxMin, scene } = glb;
    for (const b of instances) {
      const [sx, sy, sz] = b.scale;     // metres (BeamNG x/y/z)
      const obj = scene.clone(true);
      // BeamNG world: x east, y north, z up. Three.js: y up. Swap y↔z for axes,
      // then divide each by the GLB's natural extent on that axis.
      const fx = sx / bboxSize.x;
      const fy = sz / bboxSize.y;     // GLB Y is up too (typical) → world Z
      const fz = sy / bboxSize.z;
      obj.scale.set(fx, fy, fz);

      // Place: BeamNG's TSStatic convention is "position = base anchor"; we
      // mirror that — lift by the GLB's vertical offset times the new factor.
      obj.position.set(b.x, b.z - bboxMin.y * fy, -b.y);
      obj.rotation.y = -b.yaw;
      group.add(obj);
    }
    loaded++;
    if (libShapes.length) _showLoadingBadge(`buildings: ${loaded}/${libShapes.length} meshes`);
  }));
  if (libShapes.length) _hideLoadingBadge();
}

// ---------------------------------------------------------------------------
// Camera-distance threshold where tree GLB → billboard fallback kicks in.
// Buildings stay full-mesh; trees are the high-count scene-killer.
const TREE_GLB_RADIUS = 600;     // metres around the camera target
const MAX_GLB_TREES = 250;       // cap GLB instances regardless of bucket

// Procedural tree silhouette texture — used on the billboard quads so
// distant trees look like trees, not green rectangles.
let _treeBillboardTex = null;
function _treeBillboardTexture() {
  if (_treeBillboardTex) return _treeBillboardTex;
  const w = 128, h = 192;
  const cv = document.createElement("canvas"); cv.width = w; cv.height = h;
  const cx = cv.getContext("2d");
  cx.clearRect(0, 0, w, h);
  // Trunk
  cx.fillStyle = "#5d4037";
  cx.fillRect(w / 2 - 5, h - 50, 10, 50);
  // Canopy (overlapping circles for organic look)
  const canopyColor = "#2e7d32";
  cx.fillStyle = canopyColor;
  const blobs = [
    [w * 0.5, h * 0.30, 38],
    [w * 0.32, h * 0.45, 30],
    [w * 0.68, h * 0.45, 30],
    [w * 0.40, h * 0.62, 26],
    [w * 0.62, h * 0.62, 26],
    [w * 0.50, h * 0.55, 32],
    [w * 0.50, h * 0.74, 24],
  ];
  for (const [bx, by, br] of blobs) {
    cx.beginPath(); cx.arc(bx, by, br, 0, Math.PI * 2); cx.fill();
  }
  // Subtle highlight
  cx.fillStyle = "#4a8a3a";
  for (const [bx, by, br] of blobs) {
    cx.beginPath(); cx.arc(bx - br * 0.25, by - br * 0.25, br * 0.55, 0, Math.PI * 2); cx.fill();
  }
  // Subtle shadow
  cx.fillStyle = "#1f5722";
  for (const [bx, by, br] of blobs) {
    cx.beginPath(); cx.arc(bx + br * 0.20, by + br * 0.20, br * 0.40, 0, Math.PI * 2); cx.fill();
  }
  const tex = new THREE.CanvasTexture(cv);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.needsUpdate = true;
  _treeBillboardTex = tex;
  return tex;
}

async function setFoliage({ trees, hedges }) {
  if (!state.scene) return;
  console.info("[preview] setFoliage trees=%d hedges=%d", trees?.length ?? 0, hedges?.length ?? 0);
  if (state.foliageGroup) {
    state.scene.remove(state.foliageGroup);
    state.foliageGroup.traverse((o) => {
      if (o.isMesh) { o.geometry?.dispose?.(); o.material?.dispose?.(); }
    });
  }
  const group = new THREE.Group();
  state.scene.add(group);
  state.foliageGroup = group;

  // Pick which trees become full GLBs (close to terrain centre) vs billboards
  const cameraTarget = state.controls?.target || new THREE.Vector3();
  const dist2 = (t) => {
    const dx = t.x - cameraTarget.x;
    const dy = -t.y - cameraTarget.z;     // BeamNG y → preview -z
    return dx * dx + dy * dy;
  };
  const sortedTrees = (trees || []).slice().sort((a, b) => dist2(a) - dist2(b));
  const glbTrees = sortedTrees
    .filter((t) => Math.sqrt(dist2(t)) < TREE_GLB_RADIUS)
    .slice(0, MAX_GLB_TREES);
  const billboardTrees = sortedTrees.filter((t) => !glbTrees.includes(t));

  // ---- Distant trees: instanced billboards (one quad each, lit cheaply) ----
  if (billboardTrees.length) {
    const quad = new THREE.PlaneGeometry(1, 1);
    const billboardMat = new THREE.MeshLambertMaterial({
      map: _treeBillboardTexture(),
      transparent: true, alphaTest: 0.5,
      depthWrite: true, side: THREE.DoubleSide,
    });
    const inst = new THREE.InstancedMesh(quad, billboardMat, billboardTrees.length);
    inst.castShadow = false;
    const m = new THREE.Matrix4(); const q = new THREE.Quaternion();
    for (let i = 0; i < billboardTrees.length; i++) {
      const t = billboardTrees[i];
      const [, , sz] = t.scale;
      // Add a tiny per-instance yaw jitter so neighbouring trees don't all
      // face the same way.
      const yawJit = -t.yaw + ((i * 137) % 360) * Math.PI / 180;
      q.setFromAxisAngle(new THREE.Vector3(0, 1, 0), yawJit);
      m.compose(
        new THREE.Vector3(t.x, t.z + sz / 2, -t.y),
        q,
        new THREE.Vector3(sz * 0.85, sz, 1),
      );
      inst.setMatrixAt(i, m);
    }
    inst.instanceMatrix.needsUpdate = true;
    group.add(inst);
  }

  // Replace the input list so the GLB path below only handles close trees
  trees = glbTrees;

  if (trees?.length) {
    // Bucket trees by shape path so each tree GLB loads once
    const byShape = new Map();
    for (const t of trees) {
      const key = t.shape || "_placeholder";
      if (!byShape.has(key)) byShape.set(key, []);
      byShape.get(key).push(t);
    }
    const isLibrary = (p) => p && p.startsWith("art/shapes/trees_lib/");

    const placeholderTree = (t) => {
      // Cone+cylinder fallback (the original built-in tree.dae shape)
      const trunkGeo = new THREE.CylinderGeometry(0.06, 0.08, 0.35, 6);
      trunkGeo.translate(0, 0.175, 0);
      const canopyGeo = new THREE.ConeGeometry(0.42, 0.7, 8);
      canopyGeo.translate(0, 0.35 + 0.7 / 2, 0);
      const trunk = new THREE.Mesh(
        trunkGeo, new THREE.MeshStandardMaterial({ color: 0x5d4037, roughness: 0.95 }));
      const canopy = new THREE.Mesh(
        canopyGeo, new THREE.MeshStandardMaterial({ color: 0x2e7d32, roughness: 0.85, flatShading: true }));
      const wrap = new THREE.Group();
      wrap.add(trunk); wrap.add(canopy);
      const [sx, sy, sz] = t.scale;
      wrap.scale.set(sx, sz, sy);
      wrap.position.set(t.x, t.z, -t.y);
      wrap.rotation.y = -t.yaw;
      wrap.traverse((o) => { if (o.isMesh) { o.castShadow = true; o.receiveShadow = true; } });
      return wrap;
    };

    await Promise.all([...byShape.entries()].map(async ([shape, instances]) => {
      if (!isLibrary(shape)) {
        for (const t of instances) group.add(placeholderTree(t));
        return;
      }
      let glb;
      try {
        glb = await _loadGlb(shape);
      } catch (err) {
        console.warn("[preview] tree GLB load failed:", shape, err);
        for (const t of instances) group.add(placeholderTree(t));
        return;
      }
      const { bboxSize, bboxMin, scene } = glb;
      for (const t of instances) {
        const [sx, sy, sz] = t.scale;
        const obj = scene.clone(true);
        const fx = sx / bboxSize.x;
        const fy = sz / bboxSize.y;
        const fz = sy / bboxSize.z;
        obj.scale.set(fx, fy, fz);
        obj.position.set(t.x, t.z - bboxMin.y * fy, -t.y);
        obj.rotation.y = -t.yaw;
        group.add(obj);
      }
    }));
  }

  if (hedges?.length) {
    const hedgeGeo = new THREE.BoxGeometry(1, 1, 1);
    const hedgeMat = new THREE.MeshStandardMaterial({ color: 0x3f5a28, roughness: 0.95, flatShading: true });
    const inst = new THREE.InstancedMesh(hedgeGeo, hedgeMat, hedges.length);
    inst.castShadow = inst.receiveShadow = true;
    const m = new THREE.Matrix4(); const q = new THREE.Quaternion();
    for (let i = 0; i < hedges.length; i++) {
      const h = hedges[i];
      q.setFromAxisAngle(new THREE.Vector3(0, 1, 0), -h.yaw);
      m.compose(
        new THREE.Vector3(h.x, h.z + h.height / 2, -h.y),
        q,
        new THREE.Vector3(h.length, h.height, h.width),
      );
      inst.setMatrixAt(i, m);
    }
    inst.instanceMatrix.needsUpdate = true;
    group.add(inst);
  }
}

// ---------------------------------------------------------------------------
// Build a ribbon mesh along a polyline at the given width — a flat strip
// of 2-vertex-wide quads, perpendicular to the local segment, laid 0.3 m
// above terrain so it z-fights nicely as a decal-style overlay.
function _buildRoadRibbon(nodes, width) {
  const verts = [];
  const uvs = [];
  const idx = [];
  const halfW = width / 2;
  let runLength = 0;     // for V coordinate, repeats the texture along length

  // Convert (BeamNG x, y, z) → (Three x, z, -y), lifted +0.25 m
  const toThree = ([x, y, z]) => new THREE.Vector3(x, z + 0.25, -y);
  const pts3 = nodes.map(toThree);
  if (pts3.length < 2) return null;

  for (let i = 0; i < pts3.length; i++) {
    // Tangent: average of neighbouring segments (for smoother joints)
    const prev = pts3[Math.max(0, i - 1)];
    const next = pts3[Math.min(pts3.length - 1, i + 1)];
    const t = next.clone().sub(prev);
    t.y = 0;
    if (t.lengthSq() < 1e-6) t.set(1, 0, 0);
    t.normalize();
    // Perpendicular in the horizontal plane
    const n = new THREE.Vector3(-t.z, 0, t.x);
    const left = pts3[i].clone().addScaledVector(n, halfW);
    const right = pts3[i].clone().addScaledVector(n, -halfW);
    if (i > 0) {
      runLength += pts3[i].distanceTo(pts3[i - 1]);
    }
    verts.push(left.x, left.y, left.z);
    verts.push(right.x, right.y, right.z);
    // Texture U across, V along length (one repeat per ~12 metres)
    uvs.push(0, runLength / 12);
    uvs.push(1, runLength / 12);
  }

  for (let i = 0; i < pts3.length - 1; i++) {
    const a = i * 2, b = a + 1, c = a + 2, d = a + 3;
    idx.push(a, c, b);
    idx.push(b, c, d);
  }

  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.Float32BufferAttribute(verts, 3));
  geo.setAttribute("uv", new THREE.Float32BufferAttribute(uvs, 2));
  geo.setIndex(idx);
  geo.computeVertexNormals();
  return geo;
}

let _roadDecalTex = null;
function _roadDecalTexture() {
  if (_roadDecalTex) return _roadDecalTex;
  const tex = new THREE.TextureLoader().load("/api/road-decal");
  tex.wrapS = THREE.ClampToEdgeWrapping;
  tex.wrapT = THREE.RepeatWrapping;
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = 8;
  _roadDecalTex = tex;
  return tex;
}

function setRoads(roads) {
  if (!state.scene) return;
  console.info("[preview] setRoads:", roads.length);
  if (state.roadsGroup) {
    state.scene.remove(state.roadsGroup);
    state.roadsGroup.traverse((o) => {
      if (o.isMesh || o.isLine) { o.geometry.dispose(); o.material.dispose(); }
    });
  }
  const group = new THREE.Group();
  const sharedTex = _roadDecalTexture();
  // Per-road clone of the texture so each can have its own repeat without
  // disturbing others (cheap — backing image is shared via texture caching)
  for (const r of roads) {
    const geo = _buildRoadRibbon(r.nodes, r.width);
    if (!geo) continue;
    const mat = new THREE.MeshStandardMaterial({
      map: sharedTex, roughness: 0.92, metalness: 0.0,
      polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -1,
    });
    const mesh = new THREE.Mesh(geo, mat);
    mesh.receiveShadow = true;
    group.add(mesh);
  }
  state.scene.add(group);
  state.roadsGroup = group;
}

function loadImage(url) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.crossOrigin = "anonymous";
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error(`Image load failed: ${url}`));
    img.src = url;
  });
}

window.MapNGPreview = {
  reset, setHeightmap, setTerrainLayers, setBuildings, setFoliage, setRoads,
  invalidateGlbCache,
};
