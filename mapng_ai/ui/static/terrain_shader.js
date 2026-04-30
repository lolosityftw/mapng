/* Game-quality terrain shader.
   Constructs the ground from up to 4 per-class PBR tiles:
     - Each class has a diffuse + normal + opacity mask
     - Opacity sampled at terrain UV (0..1 across the whole map)
     - Diffuse + normal sampled at world-space UV (tiling at ~`tileSize` m)
     - Per-pixel weighted blend by opacity
     - Macro variation breaks up the tiling pattern visibly at distance
   Output is fed into Three.js's lighting (replaces the satellite-photo overlay).
*/

import * as THREE from "three";

const VERT = `
  varying vec2 vUv;
  varying vec3 vWorldPos;
  varying vec3 vNormal;
  void main() {
    vUv = uv;
    vec4 wp = modelMatrix * vec4(position, 1.0);
    vWorldPos = wp.xyz;
    vNormal = normalize(mat3(modelMatrix) * normal);
    gl_Position = projectionMatrix * viewMatrix * wp;
  }
`;

const FRAG = `
  precision highp float;
  varying vec2 vUv;
  varying vec3 vWorldPos;
  varying vec3 vNormal;

  uniform sampler2D opacityMaps[4];
  uniform sampler2D diffuseMaps[4];
  uniform sampler2D normalMaps[4];
  uniform int numLayers;
  uniform float tileSize;            // metres per tile repeat
  uniform vec3 sunDir;
  uniform vec3 sunColor;
  uniform vec3 ambientColor;
  uniform float macroBlendFreq;      // low-frequency colour variation across terrain
  uniform vec3 fogColor;
  uniform float fogNear;
  uniform float fogFar;

  // Cheap hash → noise for macro variation
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

  vec4 sampleLayer(int i, sampler2D maps[4], vec2 uv) {
    if (i == 0) return texture2D(maps[0], uv);
    if (i == 1) return texture2D(maps[1], uv);
    if (i == 2) return texture2D(maps[2], uv);
    return texture2D(maps[3], uv);
  }

  void main() {
    vec2 worldUV = vWorldPos.xz / tileSize;

    vec3 albedo = vec3(0.0);
    vec3 nLocal = vec3(0.0, 0.0, 0.0);
    float total = 0.0;

    // Loop unrolled for WebGL1 compatibility
    for (int i = 0; i < 4; i++) {
      if (i >= numLayers) break;
      float a = sampleLayer(i, opacityMaps, vUv).r;
      if (a <= 0.001) continue;
      vec3 d = sampleLayer(i, diffuseMaps, worldUV).rgb;
      vec3 n = sampleLayer(i, normalMaps, worldUV).rgb * 2.0 - 1.0;
      albedo += d * a;
      nLocal += n * a;
      total += a;
    }

    if (total > 1e-3) {
      albedo /= total;
      nLocal /= total;
    } else {
      // Where the splat masks have nothing, fall back to the first layer
      albedo = sampleLayer(0, diffuseMaps, worldUV).rgb;
    }

    // Macro variation — slight tint shift at low frequency to break tiling
    float macro = vnoise(vWorldPos.xz * macroBlendFreq);
    albedo *= mix(0.85, 1.10, macro);

    // Build a perturbed normal in world space.
    // The terrain mesh's normal points up after the rotateX(-PI/2) we did,
    // so vNormal ≈ (0, 1, 0). We treat normal-map XY as world XZ perturbation.
    vec3 N = normalize(vNormal + vec3(nLocal.x, 0.0, nLocal.y) * 0.6);

    vec3 L = normalize(sunDir);
    float lambert = max(dot(N, L), 0.0);
    // soft wrap-around for ambient feel
    float wrap = clamp(dot(N, L) * 0.5 + 0.5, 0.0, 1.0);

    vec3 ambient = albedo * ambientColor * (0.6 + 0.4 * wrap);
    vec3 lit = albedo * sunColor * lambert;
    vec3 colour = ambient + lit;

    // Fog
    float dist = length(vWorldPos - cameraPosition);
    float fogF = clamp((dist - fogNear) / max(fogFar - fogNear, 0.001), 0.0, 1.0);
    colour = mix(colour, fogColor, fogF);

    gl_FragColor = vec4(colour, 1.0);
  }
`;


// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------
export function createTerrainMaterial({ tileSize = 4.0, fogColor = 0xc7d6e0, fogNear = 1500, fogFar = 7000 } = {}) {
  const blank = new THREE.DataTexture(
    new Uint8Array([128, 128, 255, 255]), 1, 1, THREE.RGBAFormat,
  );
  blank.needsUpdate = true;
  const blank4 = [blank, blank, blank, blank];

  const mat = new THREE.ShaderMaterial({
    vertexShader: VERT,
    fragmentShader: FRAG,
    uniforms: {
      opacityMaps:   { value: blank4 },
      diffuseMaps:   { value: blank4 },
      normalMaps:    { value: blank4 },
      numLayers:     { value: 0 },
      tileSize:      { value: tileSize },
      sunDir:        { value: new THREE.Vector3(0.5, 0.7, 0.4).normalize() },
      sunColor:      { value: new THREE.Color(0xfff1d6) },
      ambientColor:  { value: new THREE.Color(0xb6cdde) },
      macroBlendFreq:{ value: 0.012 },
      fogColor:      { value: new THREE.Color(fogColor) },
      fogNear:       { value: fogNear },
      fogFar:        { value: fogFar },
    },
  });
  return mat;
}


/**
 * Apply a fresh set of terrain layers to a material returned by
 * createTerrainMaterial. `layers` is an array of up to 4 entries, each:
 *   { opacityUrl, diffuseUrl, normalUrl }
 * All URLs may be null — the layer is ignored if either opacity or diffuse
 * is missing.
 */
export async function applyTerrainLayers(material, layers, { sunDir } = {}) {
  // Pick top 4 with usable diffuse + opacity
  const usable = layers.filter((l) => l.opacityUrl && l.diffuseUrl).slice(0, 4);
  const loader = new THREE.TextureLoader();
  loader.crossOrigin = "anonymous";

  const load = (url, srgb = false, repeat = false) =>
    new Promise((resolve, reject) => {
      if (!url) { resolve(null); return; }
      loader.load(url, (tex) => {
        if (srgb) tex.colorSpace = THREE.SRGBColorSpace;
        if (repeat) tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
        tex.flipY = true;
        tex.anisotropy = 4;
        resolve(tex);
      }, undefined, (e) => reject(e));
    });

  // Pre-load each map
  const opacityTex = await Promise.all(usable.map((l) => load(l.opacityUrl, false, false)));
  const diffuseTex = await Promise.all(usable.map((l) => load(l.diffuseUrl, true, true)));
  const normalTex  = await Promise.all(usable.map((l) => l.normalUrl ? load(l.normalUrl, false, true) : null));

  // Pad to 4 with the first valid texture (uniform array length must be 4)
  const filler = opacityTex[0] || diffuseTex[0];
  while (opacityTex.length < 4) opacityTex.push(filler);
  while (diffuseTex.length < 4) diffuseTex.push(filler);
  while (normalTex.length < 4) normalTex.push(filler);

  // Replace any null normal slots with a flat-blue normal so lighting is fine
  for (let i = 0; i < 4; i++) {
    if (!normalTex[i]) {
      const flat = new THREE.DataTexture(
        new Uint8Array([128, 128, 255, 255]), 1, 1, THREE.RGBAFormat,
      );
      flat.needsUpdate = true;
      normalTex[i] = flat;
    }
  }

  material.uniforms.opacityMaps.value = opacityTex;
  material.uniforms.diffuseMaps.value = diffuseTex;
  material.uniforms.normalMaps.value = normalTex;
  material.uniforms.numLayers.value = usable.length;
  if (sunDir) material.uniforms.sunDir.value.copy(sunDir).normalize();
  material.needsUpdate = true;

  return usable.length;
}
