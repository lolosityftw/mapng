/* MapNG-AI 3D preview pane.
   Phase 0: empty grid stage with orbit controls — proves the rendering path works.
   Phase 1+: a setHeightmap() / setBuildings() API will be added to swap content. */

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const container = document.getElementById("preview");

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0d1117);

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(window.devicePixelRatio);
container.appendChild(renderer.domElement);

const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 5000);
camera.position.set(180, 140, 220);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.target.set(0, 0, 0);

// Lights
scene.add(new THREE.AmbientLight(0xffffff, 0.5));
const sun = new THREE.DirectionalLight(0xffffff, 1.1);
sun.position.set(120, 220, 80);
scene.add(sun);

// Reference grid — 200×200 units = ~2 km at 10 m/unit (placeholder scale)
const grid = new THREE.GridHelper(200, 20, 0x2a2f37, 0x1a1d23);
scene.add(grid);

// Axes hint at the origin
const axes = new THREE.AxesHelper(20);
scene.add(axes);

// "Empty stage" placeholder — a low cylinder so users see the renderer is alive
const placeholder = new THREE.Mesh(
  new THREE.CylinderGeometry(8, 8, 1, 32),
  new THREE.MeshStandardMaterial({ color: 0x2a2f37, roughness: 0.9 }),
);
placeholder.position.y = 0.5;
scene.add(placeholder);

function resize() {
  const w = container.clientWidth;
  const h = container.clientHeight || 300;
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

// Public API for later phases
window.MapNGPreview = {
  scene,
  camera,
  setHeightmap(/* heightmapUrl, widthMeters, heightMeters */) {
    // Phase 1 will implement this — load a 16-bit PNG, build a PlaneGeometry,
    // displace vertices, replace `placeholder` with the resulting mesh.
    console.info("[preview] setHeightmap() not yet implemented (Phase 1)");
  },
  setBuildings(/* buildings */) {
    console.info("[preview] setBuildings() not yet implemented (Phase 3)");
  },
};
