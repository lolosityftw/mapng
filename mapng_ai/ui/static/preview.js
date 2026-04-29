/* MapNG-AI 3D preview pane.
   Phase 0: empty stage with orbit controls.
   Phase 1: setHeightmap(url, sideMeters, minM, maxM) replaces placeholder
            with a displaced PlaneGeometry mesh. */

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const container = document.getElementById("preview");
console.info("[preview] booting");

const state = { scene: null, camera: null, renderer: null, terrainMesh: null, placeholder: null };

if (!container) {
  console.error("[preview] no #preview element");
} else if (!window.WebGLRenderingContext) {
  container.innerHTML =
    '<div style="padding:20px;color:#f85149">WebGL not supported in this browser.</div>';
} else {
  try { boot(); } catch (err) {
    console.error("[preview] init failed:", err);
    container.innerHTML =
      `<div style="padding:20px;color:#f85149;font-family:ui-monospace">preview failed: ${String(err)}</div>`;
  }
}

function boot() {
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x12161c);

  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(window.devicePixelRatio);
  renderer.domElement.style.display = "block";
  container.appendChild(renderer.domElement);

  const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 20000);
  camera.position.set(2200, 1500, 2400);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.target.set(0, 0, 0);

  scene.add(new THREE.AmbientLight(0xffffff, 0.55));
  const sun = new THREE.DirectionalLight(0xffffff, 1.2);
  sun.position.set(1500, 2500, 1000);
  scene.add(sun);

  const grid = new THREE.GridHelper(2000, 20, 0x4a5560, 0x2a2f37);
  scene.add(grid);
  const axes = new THREE.AxesHelper(200);
  scene.add(axes);

  // Empty-stage placeholder (Phase 0 visual)
  const placeholder = new THREE.Mesh(
    new THREE.IcosahedronGeometry(140, 0),
    new THREE.MeshStandardMaterial({ color: 0xd29922, flatShading: true, roughness: 0.55 }),
  );
  placeholder.position.y = 180;
  scene.add(placeholder);

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
    if (state.placeholder) {
      state.placeholder.rotation.y += 0.005;
      state.placeholder.rotation.x += 0.002;
    }
    controls.update();
    renderer.render(scene, camera);
  })();

  state.scene = scene;
  state.camera = camera;
  state.renderer = renderer;
  state.placeholder = placeholder;
  state.controls = controls;

  console.info("[preview] ready");
}

// ---------------------------------------------------------------------------
// Public API used by the rest of the UI
// ---------------------------------------------------------------------------
async function setHeightmap({ url, sideMeters, minM, maxM, segments = 256 }) {
  if (!state.scene) return;
  const img = await loadImage(url);

  // Sample the 8-bit preview PNG into a typed array (one row of texels per row)
  const cv = document.createElement("canvas");
  cv.width = img.width; cv.height = img.height;
  const cx = cv.getContext("2d");
  cx.drawImage(img, 0, 0);
  const pixels = cx.getImageData(0, 0, img.width, img.height).data;

  // Build a PlaneGeometry of (segments+1)² vertices, displace by sampled height
  const geo = new THREE.PlaneGeometry(sideMeters, sideMeters, segments, segments);
  const range = (maxM - minM) || 1;
  const pos = geo.attributes.position;
  for (let i = 0; i < pos.count; i++) {
    const u = (pos.getX(i) + sideMeters / 2) / sideMeters;
    const v = 1 - (pos.getY(i) + sideMeters / 2) / sideMeters; // texture Y is flipped vs. plane Y
    const px = Math.min(img.width - 1, Math.max(0, Math.floor(u * img.width)));
    const py = Math.min(img.height - 1, Math.max(0, Math.floor(v * img.height)));
    const sample = pixels[(py * img.width + px) * 4] / 255;
    const h = minM + sample * range;
    pos.setZ(i, h);
  }
  geo.rotateX(-Math.PI / 2); // bring +Z up to +Y up so it sits on the grid
  geo.computeVertexNormals();

  const mat = new THREE.MeshStandardMaterial({
    color: 0x6a7a55, roughness: 0.95, metalness: 0.0, flatShading: false,
  });

  if (state.terrainMesh) {
    state.scene.remove(state.terrainMesh);
    state.terrainMesh.geometry.dispose();
    state.terrainMesh.material.dispose();
  }
  if (state.placeholder) {
    state.scene.remove(state.placeholder);
    state.placeholder = null;
  }

  const mesh = new THREE.Mesh(geo, mat);
  mesh.position.y = -minM; // so the lowest point sits on Y=0
  state.scene.add(mesh);
  state.terrainMesh = mesh;

  // Frame the camera so the whole terrain is visible
  if (state.camera && state.controls) {
    const dist = sideMeters * 1.4;
    state.camera.position.set(dist, dist * 0.7, dist);
    state.controls.target.set(0, (maxM - minM) / 2, 0);
    state.controls.update();
  }
}

function loadImage(url) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.crossOrigin = "anonymous";
    img.onload = () => resolve(img);
    img.onerror = (e) => reject(new Error(`Image load failed: ${url}`));
    img.src = url;
  });
}

window.MapNGPreview = { setHeightmap };
