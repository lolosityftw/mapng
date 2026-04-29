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
  const geo = new THREE.SphereGeometry(8000, 48, 24);
  const mat = new THREE.ShaderMaterial({
    side: THREE.BackSide,
    depthWrite: false,
    uniforms: {
      time:        { value: 0.0 },
      topColor:    { value: new THREE.Color(0x4a7eb8) },
      midColor:    { value: new THREE.Color(0x9bc6e5) },
      bottomColor: { value: new THREE.Color(0xe7eef0) },
      cloudColor:  { value: new THREE.Color(0xfdfdfd) },
      cloudShadow: { value: new THREE.Color(0xa3b5c8) },
    },
    vertexShader: `
      varying vec3 vWorld;
      varying vec3 vNorm;
      void main() {
        vec4 wp = modelMatrix * vec4(position, 1.0);
        vWorld = wp.xyz;
        vNorm = normalize(position);
        gl_Position = projectionMatrix * viewMatrix * wp;
      }`,
    fragmentShader: `
      uniform float time;
      uniform vec3 topColor;
      uniform vec3 midColor;
      uniform vec3 bottomColor;
      uniform vec3 cloudColor;
      uniform vec3 cloudShadow;
      varying vec3 vWorld;
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
        float h = clamp(vNorm.y, 0.0, 1.0);
        // Three-stop vertical gradient
        vec3 lower = mix(bottomColor, midColor, smoothstep(0.0, 0.3, h));
        vec3 base = mix(lower, topColor, smoothstep(0.3, 1.0, h));

        // Clouds projected onto the upper hemisphere; offset by time
        vec2 uv = vec2(atan(vNorm.x, vNorm.z) * 1.0, vNorm.y * 2.5);
        uv += vec2(time * 0.012, 0.0);
        float c = fbm(uv * 1.6);
        c = smoothstep(0.55, 0.85, c);
        // Hide clouds near horizon and below
        c *= smoothstep(0.10, 0.45, h);
        vec3 cloud = mix(cloudShadow, cloudColor, smoothstep(0.55, 0.95, fbm(uv * 3.0 + 13.0)));
        vec3 col = mix(base, cloud, c);
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

  const mat = new THREE.MeshStandardMaterial({
    color: 0x9bab78,
    roughness: 0.95,
    metalness: 0.0,
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
  }
}

function setTerrainTexture(url) {
  if (!state.terrainMesh) {
    console.warn("[preview] setTerrainTexture before terrain mesh");
    return;
  }
  console.info("[preview] setTerrainTexture", url);
  const loader = new THREE.TextureLoader();
  loader.crossOrigin = "anonymous";
  loader.load(url, (tex) => {
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.anisotropy = 8;
    tex.flipY = true;
    tex.needsUpdate = true;
    state.terrainMesh.material.map = tex;
    state.terrainMesh.material.color = new THREE.Color(0xffffff);
    state.terrainMesh.material.needsUpdate = true;
  }, undefined, (err) => console.error("[preview] texture load failed", err));
}

// ---------------------------------------------------------------------------
function _pitchedBoxGeometry() {
  const verts = new Float32Array([
    -0.5, 0.0, -0.5,   0.5, 0.0, -0.5,   0.5, 0.0,  0.5,  -0.5, 0.0,  0.5,
    -0.5, 0.7, -0.5,   0.5, 0.7, -0.5,   0.5, 0.7,  0.5,  -0.5, 0.7,  0.5,
    -0.5, 1.0,  0.0,   0.5, 1.0,  0.0,
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
  // One shared geometry per unique colour → InstancedMesh per group
  const byColor = new Map();
  for (const b of buildings) {
    if (!byColor.has(b.color)) byColor.set(b.color, []);
    byColor.get(b.color).push(b);
  }
  for (const [color, list] of byColor.entries()) {
    const mat = new THREE.MeshStandardMaterial({ color, roughness: 0.85 });
    const inst = new THREE.InstancedMesh(unitGeo, mat, list.length);
    inst.castShadow = inst.receiveShadow = true;
    const m = new THREE.Matrix4(); const q = new THREE.Quaternion();
    for (let i = 0; i < list.length; i++) {
      const b = list[i];
      const [sx, sy, sz] = b.scale;
      q.setFromAxisAngle(new THREE.Vector3(0, 1, 0), -b.yaw);
      m.compose(
        new THREE.Vector3(b.x, b.z, -b.y),
        q,
        new THREE.Vector3(sx, sz, sy),     // BeamNG zsize → preview Y; ysize → preview Z
      );
      inst.setMatrixAt(i, m);
    }
    inst.instanceMatrix.needsUpdate = true;
    group.add(inst);
  }
  state.scene.add(group);
  state.buildingsGroup = group;
}

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
    // Per-instance canopy colour jitter: split into a handful of green tints
    const greens = [0x2e7d32, 0x356b2c, 0x4a8a3a, 0x2a5e25, 0x568f3a];
    const trunkGeo = new THREE.CylinderGeometry(0.06, 0.08, 0.35, 6);
    trunkGeo.translate(0, 0.175, 0);
    const canopyGeo = new THREE.ConeGeometry(0.42, 0.7, 8);
    canopyGeo.translate(0, 0.35 + 0.7 / 2, 0);
    const trunkMat = new THREE.MeshStandardMaterial({ color: 0x5d4037, roughness: 0.95 });

    const trunks = new THREE.InstancedMesh(trunkGeo, trunkMat, trees.length);
    trunks.castShadow = true;

    // One InstancedMesh per canopy colour
    const buckets = new Map();
    for (let i = 0; i < trees.length; i++) {
      const idx = i % greens.length;
      if (!buckets.has(idx)) buckets.set(idx, []);
      buckets.get(idx).push(i);
    }

    const m = new THREE.Matrix4(); const q = new THREE.Quaternion();
    for (let i = 0; i < trees.length; i++) {
      const t = trees[i];
      const [sx, sy, sz] = t.scale;
      // Slight per-tree height jitter via the seeded yaw
      const wobble = 0.85 + ((i * 9301 + 49297) % 233) / 1100;   // ≈ 0.85..1.07
      q.setFromAxisAngle(new THREE.Vector3(0, 1, 0), -t.yaw);
      m.compose(
        new THREE.Vector3(t.x, t.z, -t.y),
        q,
        new THREE.Vector3(sx * wobble, sz * wobble, sy * wobble),
      );
      trunks.setMatrixAt(i, m);
    }
    trunks.instanceMatrix.needsUpdate = true;
    group.add(trunks);

    for (const [idx, indices] of buckets.entries()) {
      const canopyMat = new THREE.MeshStandardMaterial({
        color: greens[idx], roughness: 0.85, flatShading: true,
      });
      const inst = new THREE.InstancedMesh(canopyGeo, canopyMat, indices.length);
      inst.castShadow = inst.receiveShadow = true;
      for (let j = 0; j < indices.length; j++) {
        const i = indices[j];
        const t = trees[i];
        const [sx, sy, sz] = t.scale;
        const wobble = 0.85 + ((i * 9301 + 49297) % 233) / 1100;
        q.setFromAxisAngle(new THREE.Vector3(0, 1, 0), -t.yaw);
        m.compose(
          new THREE.Vector3(t.x, t.z, -t.y),
          q,
          new THREE.Vector3(sx * wobble, sz * wobble, sy * wobble),
        );
        inst.setMatrixAt(j, m);
      }
      inst.instanceMatrix.needsUpdate = true;
      group.add(inst);
    }
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

  state.scene.add(group);
  state.foliageGroup = group;
}

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
