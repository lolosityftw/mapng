/* MapNG-AI 3D preview pane.

   Renders a polished scene: gradient sky, visible sun, lit terrain mesh,
   instanced placeholder buildings. Reset / repopulated on each pipeline run.

   Public API (called from app.js as SSE events arrive):
       MapNGPreview.setHeightmap({url, sideMeters, minM, maxM})
       MapNGPreview.setTerrainTexture(url)
       MapNGPreview.setBuildings([{x, y, z, yaw, scale, color, type}])
       MapNGPreview.reset()
*/

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const container = document.getElementById("preview");
console.info("[preview] booting");

const state = {
  scene: null, camera: null, renderer: null, controls: null,
  terrainMesh: null, terrainGroup: null,
  buildingsGroup: null,
  sun: null, sunMesh: null, sunLight: null,
  placeholder: null,
  sideMeters: 2000,    // updated by setHeightmap
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
function boot() {
  const scene = new THREE.Scene();

  // Sky: a large back-faced sphere with a vertical gradient
  const skyGeo = new THREE.SphereGeometry(8000, 32, 16);
  const skyMat = new THREE.ShaderMaterial({
    side: THREE.BackSide,
    depthWrite: false,
    uniforms: {
      topColor:    { value: new THREE.Color(0x6ba6d6) },
      bottomColor: { value: new THREE.Color(0xdce8f0) },
      offset:      { value: 100 },
      exponent:    { value: 0.6 },
    },
    vertexShader: `
      varying vec3 vWorldPosition;
      void main() {
        vec4 worldPosition = modelMatrix * vec4(position, 1.0);
        vWorldPosition = worldPosition.xyz;
        gl_Position = projectionMatrix * viewMatrix * worldPosition;
      }`,
    fragmentShader: `
      uniform vec3 topColor;
      uniform vec3 bottomColor;
      uniform float offset;
      uniform float exponent;
      varying vec3 vWorldPosition;
      void main() {
        float h = normalize(vWorldPosition + vec3(0.0, offset, 0.0)).y;
        gl_FragColor = vec4(mix(bottomColor, topColor, max(pow(max(h, 0.0), exponent), 0.0)), 1.0);
      }`,
  });
  const sky = new THREE.Mesh(skyGeo, skyMat);
  scene.add(sky);

  // Subtle distance fog matching the horizon colour
  scene.fog = new THREE.Fog(0xdce8f0, 1500, 6000);

  // Renderer
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.domElement.style.display = "block";
  container.appendChild(renderer.domElement);

  // Camera + orbit controls
  const camera = new THREE.PerspectiveCamera(45, 1, 0.5, 20000);
  camera.position.set(2500, 1700, 2500);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.maxDistance = 8000;
  controls.minDistance = 50;
  controls.target.set(0, 0, 0);

  // Sun: visible billboard + directional light
  const sunGroup = new THREE.Group();
  // Position the sun high in the southern sky
  sunGroup.position.set(1500, 2200, 1100);

  const sunMesh = new THREE.Mesh(
    new THREE.SphereGeometry(120, 24, 16),
    new THREE.MeshBasicMaterial({ color: 0xfff2c4, fog: false }),
  );
  sunGroup.add(sunMesh);

  // Soft glow halo around the sun
  const halo = new THREE.Mesh(
    new THREE.SphereGeometry(220, 24, 16),
    new THREE.MeshBasicMaterial({
      color: 0xffe89c, transparent: true, opacity: 0.25, fog: false, depthWrite: false,
    }),
  );
  sunGroup.add(halo);
  scene.add(sunGroup);

  const sunLight = new THREE.DirectionalLight(0xfff1d6, 1.4);
  sunLight.position.copy(sunGroup.position);
  sunLight.castShadow = true;
  sunLight.shadow.mapSize.set(1024, 1024);
  sunLight.shadow.camera.near = 50;
  sunLight.shadow.camera.far = 6000;
  sunLight.shadow.camera.left = -1500;
  sunLight.shadow.camera.right = 1500;
  sunLight.shadow.camera.top = 1500;
  sunLight.shadow.camera.bottom = -1500;
  scene.add(sunLight);

  // Hemisphere fill so shadow sides aren't pitch black
  scene.add(new THREE.HemisphereLight(0xb6cdde, 0x4a4226, 0.55));

  // Empty-stage placeholder until the first heightmap arrives
  const placeholder = new THREE.Mesh(
    new THREE.IcosahedronGeometry(220, 0),
    new THREE.MeshStandardMaterial({ color: 0xd29922, flatShading: true, roughness: 0.6 }),
  );
  placeholder.position.y = 240;
  placeholder.castShadow = true;
  scene.add(placeholder);

  // Ground reference grid centred at origin (overwritten when terrain lands)
  const grid = new THREE.GridHelper(2000, 20, 0x6080a0, 0x3a4a5a);
  grid.position.y = 0.01;
  scene.add(grid);
  state.referenceGrid = grid;

  // ---- Resize ----
  function resize() {
    const w = container.clientWidth || 400;
    const h = container.clientHeight || 300;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h, false);
  }
  new ResizeObserver(resize).observe(container);
  resize();

  // ---- Loop ----
  (function loop() {
    requestAnimationFrame(loop);
    if (state.placeholder) {
      state.placeholder.rotation.y += 0.005;
      state.placeholder.rotation.x += 0.002;
    }
    controls.update();
    renderer.render(scene, camera);
  })();

  Object.assign(state, {
    scene, camera, renderer, controls,
    sun: sunGroup, sunMesh, sunLight,
    placeholder,
  });
  console.info("[preview] ready");
}

// ---------------------------------------------------------------------------
// Public API
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
  if (state.referenceGrid) {
    state.referenceGrid.visible = true;
  }
  // Re-add the placeholder so the pane never looks empty
  if (!state.placeholder.parent) {
    state.scene.add(state.placeholder);
  }
}

async function setHeightmap({ url, sideMeters, minM, maxM, segments = 256 }) {
  if (!state.scene) return;
  console.info("[preview] setHeightmap", { url, sideMeters, minM, maxM });
  state.sideMeters = sideMeters;
  state.heightRange = { min: minM, max: maxM };

  const img = await loadImage(url);
  // Sample the heightmap PNG into a typed array
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
    const sample = pixels[(py * img.width + px) * 4] / 255;
    pos.setZ(i, minM + sample * range);
  }
  geo.rotateX(-Math.PI / 2);
  geo.computeVertexNormals();

  const mat = new THREE.MeshStandardMaterial({
    color: 0x9bab78,
    roughness: 0.95,
    metalness: 0.0,
    flatShading: false,
  });

  // Replace previous terrain
  if (state.terrainMesh) {
    state.scene.remove(state.terrainMesh);
    state.terrainMesh.geometry.dispose();
    state.terrainMesh.material.dispose();
  }
  if (state.placeholder?.parent) state.scene.remove(state.placeholder);
  if (state.referenceGrid) state.referenceGrid.visible = false;

  const mesh = new THREE.Mesh(geo, mat);
  mesh.receiveShadow = true;
  mesh.castShadow = false;
  state.scene.add(mesh);
  state.terrainMesh = mesh;

  // Frame the camera diagonally over the terrain
  if (state.controls && state.camera) {
    const dist = sideMeters * 1.4;
    const verticalCentre = (minM + maxM) / 2;
    state.camera.position.set(dist * 0.7, dist * 0.55, dist * 0.7);
    state.controls.target.set(0, verticalCentre, 0);
    state.controls.update();
  }

  // Push the sun so it casts shadows that cover the whole map
  if (state.sun && state.sunLight) {
    const elev = maxM + sideMeters * 1.1;
    state.sun.position.set(sideMeters * 0.75, elev, sideMeters * 0.55);
    state.sunLight.position.copy(state.sun.position);
    state.sunLight.target.position.set(0, (minM + maxM) / 2, 0);
    state.sunLight.target.updateMatrixWorld();
  }
}

function setTerrainTexture(url) {
  if (!state.terrainMesh) {
    console.warn("[preview] setTerrainTexture called before terrain mesh exists");
    return;
  }
  console.info("[preview] setTerrainTexture", url);
  const loader = new THREE.TextureLoader();
  loader.crossOrigin = "anonymous";
  loader.load(url, (tex) => {
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.anisotropy = 4;
    tex.flipY = true;          // PlaneGeometry UVs want flip-Y
    tex.needsUpdate = true;
    state.terrainMesh.material.map = tex;
    state.terrainMesh.material.color = new THREE.Color(0xffffff);
    state.terrainMesh.material.needsUpdate = true;
  }, undefined, (err) => console.error("[preview] texture load failed", err));
}

// ---- Pitched-roof unit geometry shared by every building instance ----
function _pitchedBoxGeometry() {
  const verts = new Float32Array([
    -0.5, 0.0, -0.5,   0.5, 0.0, -0.5,   0.5, 0.0,  0.5,  -0.5, 0.0,  0.5,
    -0.5, 0.7, -0.5,   0.5, 0.7, -0.5,   0.5, 0.7,  0.5,  -0.5, 0.7,  0.5,
    -0.5, 1.0,  0.0,   0.5, 1.0,  0.0,
  ]);
  const idx = new Uint16Array([
    0,2,1, 0,3,2,            // base
    0,1,5, 0,5,4,            // S wall
    1,2,6, 1,6,5,            // E wall
    2,3,7, 2,7,6,            // N wall
    3,0,4, 3,4,7,            // W wall
    4,8,7,                   // W gable
    5,6,9,                   // E gable
    4,5,9, 4,9,8,            // S roof
    7,9,6, 7,8,9,            // N roof
  ]);
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(verts, 3));
  geo.setIndex(new THREE.BufferAttribute(idx, 1));
  geo.computeVertexNormals();
  return geo;
}

function setBuildings(buildings) {
  if (!state.scene) return;
  console.info("[preview] setBuildings:", buildings.length);
  if (state.buildingsGroup) {
    state.scene.remove(state.buildingsGroup);
    state.buildingsGroup.traverse((o) => {
      if (o.isMesh) { o.geometry.dispose(); o.material.dispose(); }
    });
  }
  const group = new THREE.Group();
  const unitGeo = _pitchedBoxGeometry();
  const matCache = new Map();
  for (const b of buildings) {
    const [sx, sy, sz] = b.scale;
    let mat = matCache.get(b.color);
    if (!mat) {
      mat = new THREE.MeshStandardMaterial({ color: b.color, roughness: 0.85, metalness: 0.0 });
      matCache.set(b.color, mat);
    }
    const m = new THREE.Mesh(unitGeo, mat);
    m.scale.set(sx, sy, sz);
    // BeamNG world: x east, y north, z up. Three.js: y up → swap y↔z
    m.position.set(b.x, b.z, -b.y);
    m.rotation.y = -b.yaw;
    m.castShadow = true;
    m.receiveShadow = true;
    group.add(m);
  }
  state.scene.add(group);
  state.buildingsGroup = group;
}

// ---------------------------------------------------------------------------
// Foliage: trees as instanced cone+cylinder; hedges as scaled boxes
// ---------------------------------------------------------------------------
function setFoliage({ trees, hedges }) {
  if (!state.scene) return;
  console.info("[preview] setFoliage trees=%d hedges=%d", trees?.length ?? 0, hedges?.length ?? 0);
  if (state.foliageGroup) {
    state.scene.remove(state.foliageGroup);
    state.foliageGroup.traverse((o) => {
      if (o.isMesh) { o.geometry.dispose(); o.material.dispose(); }
    });
  }
  const group = new THREE.Group();

  if (trees?.length) {
    const trunkGeo = new THREE.CylinderGeometry(0.04, 0.05, 0.35, 6);
    trunkGeo.translate(0, 0.175, 0);
    const canopyGeo = new THREE.ConeGeometry(0.35, 0.65, 8);
    canopyGeo.translate(0, 0.35 + 0.65 / 2, 0);
    const trunkMat = new THREE.MeshStandardMaterial({ color: 0x5d4037, roughness: 0.95 });
    const canopyMat = new THREE.MeshStandardMaterial({ color: 0x2e7d32, roughness: 0.85, flatShading: true });
    const trunks = new THREE.InstancedMesh(trunkGeo, trunkMat, trees.length);
    const canopies = new THREE.InstancedMesh(canopyGeo, canopyMat, trees.length);
    trunks.castShadow = canopies.castShadow = true;
    trunks.receiveShadow = canopies.receiveShadow = true;
    const m = new THREE.Matrix4();
    const q = new THREE.Quaternion();
    for (let i = 0; i < trees.length; i++) {
      const t = trees[i];
      const [sx, sy, sz] = t.scale;
      // Three.js Y-up: tree height runs along Y. BeamNG Z is height.
      q.setFromAxisAngle(new THREE.Vector3(0, 1, 0), -t.yaw);
      m.compose(
        new THREE.Vector3(t.x, t.z, -t.y),
        q,
        new THREE.Vector3(sx, sz, sy),    // swap Y↔Z scale to match
      );
      trunks.setMatrixAt(i, m);
      canopies.setMatrixAt(i, m);
    }
    trunks.instanceMatrix.needsUpdate = true;
    canopies.instanceMatrix.needsUpdate = true;
    group.add(trunks); group.add(canopies);
  }

  if (hedges?.length) {
    const hedgeGeo = new THREE.BoxGeometry(1, 1, 1);
    const hedgeMat = new THREE.MeshStandardMaterial({ color: 0x3f5a28, roughness: 0.95, flatShading: true });
    const inst = new THREE.InstancedMesh(hedgeGeo, hedgeMat, hedges.length);
    inst.castShadow = inst.receiveShadow = true;
    const m = new THREE.Matrix4();
    const q = new THREE.Quaternion();
    for (let i = 0; i < hedges.length; i++) {
      const h = hedges[i];
      q.setFromAxisAngle(new THREE.Vector3(0, 1, 0), -h.yaw);
      // Hedge slab is 1×1×1 with base at Z=0 in BeamNG → in Three.js +Y is up,
      // so position centre is at z + height/2
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

  state.scene.add(group);
  state.foliageGroup = group;
}

// ---------------------------------------------------------------------------
// Roads: simple line meshes along OSM centrelines, lifted off the terrain
// ---------------------------------------------------------------------------
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
  const mat = new THREE.LineBasicMaterial({ color: 0x1c1c1c });
  for (const r of roads) {
    const pts = [];
    for (const [x, y, z] of r.nodes) pts.push(new THREE.Vector3(x, z + 0.2, -y));
    if (pts.length < 2) continue;
    const geo = new THREE.BufferGeometry().setFromPoints(pts);
    group.add(new THREE.Line(geo, mat));
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

window.MapNGPreview = { reset, setHeightmap, setTerrainTexture, setBuildings, setFoliage, setRoads };
