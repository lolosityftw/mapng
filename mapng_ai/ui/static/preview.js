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
  if (state.buildingsGroup) {
    state.scene.remove(state.buildingsGroup);
    state.buildingsGroup.traverse((o) => {
      if (o.isMesh) { o.geometry.dispose(); o.material.dispose(); }
    });
    state.buildingsGroup = null;
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
  const unitGeo = new THREE.BoxGeometry(1, 1, 1);
  // Cache one material per colour string to keep draw calls low
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
    m.position.set(b.x, b.z + sz / 2, -b.y);
    m.rotation.y = -b.yaw;
    m.castShadow = true;
    m.receiveShadow = true;
    group.add(m);
  }
  state.scene.add(group);
  state.buildingsGroup = group;
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

window.MapNGPreview = { reset, setHeightmap, setTerrainTexture, setBuildings };
