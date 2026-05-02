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
  uniform int waterLayerIndex;       // -1 if water not in top-4
  uniform float tileSize;            // metres per tile repeat
  uniform float time;                // seconds, for water animation
  uniform vec3 sunDir;
  uniform vec3 sunColor;
  uniform vec3 ambientColor;
  uniform float macroBlendFreq;      // low-frequency colour variation across terrain
  uniform vec3 fogColor;
  uniform float fogNear;
  uniform float fogFar;
  uniform float terrainHalf;         // half-side metres, for edge fade

  // User-adjustable tint controls (set live from UI sliders)
  uniform vec3 grassTint;            // RGB multiplier on the green-class layers
  uniform float grassSaturation;     // 1.0 = identity, >1 = boost saturation
  uniform float grassBrightness;     // 1.0 = identity
  uniform vec4 grassLayerMask;       // 1.0 per slot if that layer is grass-like

  // Cloud shadow uniforms (drifting FBM-modulated darkening)
  uniform float cloudShadowStrength; // 0..1, 0 = no clouds, 0.35 = typical NI
  uniform float cloudSpeed;          // metres/second of cloud drift
  uniform vec2  cloudDir;            // unit vector, wind direction
  uniform float cloudFreq;           // 1/m spatial frequency

  // Wetness uniform — 0 = dry, 1 = freshly rained. Darkens roads + adds
  // a sun-direction specular highlight on asphalt-flagged pixels only.
  uniform float wetness;
  uniform vec4 asphaltLayerMask;     // 1.0 per slot if that layer is asphalt

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

  // Anti-tile sampler. Three samples at different scales/rotations/offsets
  // blended by overlapping low-frequency noise weights — eliminates the
  // visible cell boundaries that discrete rotation produced.
  vec4 sampleTile(int i, sampler2D maps[4], vec2 worldUV, vec2 worldXZ) {
    // Continuous (not discrete) per-fragment rotation — no hard cell seams.
    float a1 = vnoise(worldXZ * 0.014) * 6.2832;       // 0..2π smooth
    float a2 = vnoise(worldXZ * 0.022 + 17.3) * 6.2832;
    float a3 = vnoise(worldXZ * 0.018 + 41.7) * 6.2832;
    float c1 = cos(a1), s1 = sin(a1);
    float c2 = cos(a2), s2 = sin(a2);
    float c3 = cos(a3), s3 = sin(a3);
    vec2 uvA = mat2(c1, -s1, s1, c1) * worldUV;
    vec2 uvB = mat2(c2, -s2, s2, c2) * (worldUV * 0.57 + vec2(11.1, 27.3));
    vec2 uvC = mat2(c3, -s3, s3, c3) * (worldUV * 1.33 + vec2(31.7, 5.9));
    vec4 dA = sampleLayer(i, maps, uvA);
    vec4 dB = sampleLayer(i, maps, uvB);
    vec4 dC = sampleLayer(i, maps, uvC);
    // Three weights from independent low-freq noise; normalised so they
    // sum to 1 → no banding, smooth blend.
    float w1 = vnoise(worldXZ * 0.010);
    float w2 = vnoise(worldXZ * 0.013 + 7.7);
    float w3 = vnoise(worldXZ * 0.017 + 23.4);
    float wsum = max(w1 + w2 + w3, 1e-3);
    return (dA * w1 + dB * w2 + dC * w3) / wsum;
  }

  // Distance-aware tile scaling. Near the camera the standard tile (~4 m)
  // gives crisp ground detail. Beyond ~250 m we fade in a coarser tile
  // (32×) so the repeat pattern stops aliasing into a fizzling moiré at
  // distance. Returns a smoothly-blended worldUV for both scales.
  vec2 distanceScaledUV(float dist) {
    float farFactor = smoothstep(250.0, 700.0, dist);
    float scale = mix(1.0, 8.0, farFactor);
    return vWorldPos.xz / (tileSize * scale);
  }

  void main() {
    float camDist = length(vWorldPos - cameraPosition);
    vec2 worldUV = distanceScaledUV(camDist);

    vec3 albedo = vec3(0.0);
    vec3 nLocal = vec3(0.0, 0.0, 0.0);
    float total = 0.0;
    float waterAmount = 0.0;
    float asphaltAmount = 0.0;

    // Loop unrolled for WebGL1 compatibility
    for (int i = 0; i < 4; i++) {
      if (i >= numLayers) break;
      float a = sampleLayer(i, opacityMaps, vUv).r;
      if (a <= 0.001) continue;
      vec3 d = sampleTile(i, diffuseMaps, worldUV, vWorldPos.xz).rgb;
      vec3 n = sampleTile(i, normalMaps,  worldUV, vWorldPos.xz).rgb * 2.0 - 1.0;
      // Apply user grass tint to layers flagged as grass via grassLayerMask
      // (e.g. pasture and lawn). Saturation boost via grey-vs-colour mix.
      float gm = 0.0;
      if (i == 0) gm = grassLayerMask.x;
      else if (i == 1) gm = grassLayerMask.y;
      else if (i == 2) gm = grassLayerMask.z;
      else gm = grassLayerMask.w;
      if (gm > 0.5) {
        d *= grassTint * grassBrightness;
        float grey = dot(d, vec3(0.299, 0.587, 0.114));
        d = mix(vec3(grey), d, grassSaturation);
      }
      // Track asphalt-flagged layer coverage for the wet-road effect.
      float am = 0.0;
      if (i == 0) am = asphaltLayerMask.x;
      else if (i == 1) am = asphaltLayerMask.y;
      else if (i == 2) am = asphaltLayerMask.z;
      else am = asphaltLayerMask.w;
      if (am > 0.5) asphaltAmount += a;
      albedo += d * a;
      nLocal += n * a;
      total += a;
      if (i == waterLayerIndex) waterAmount = a;
    }

    if (total > 1e-3) {
      albedo /= total;
      nLocal /= total;
    } else {
      albedo = sampleLayer(0, diffuseMaps, worldUV).rgb;
    }

    // Water override — animated reflective blue where water mask is strong
    if (waterAmount > 0.5) {
      float wave = vnoise(vWorldPos.xz * 0.4 + vec2(time * 0.2, time * 0.15));
      float wave2 = vnoise(vWorldPos.xz * 1.1 - vec2(time * 0.4, 0.0));
      vec3 waterTint = mix(vec3(0.16, 0.32, 0.52), vec3(0.30, 0.50, 0.65), wave);
      // Subtle ripple normal
      nLocal = mix(nLocal, vec3(wave2 * 0.3, 0.0, wave * 0.3), waterAmount);
      albedo = mix(albedo, waterTint, waterAmount);
    }

    // Two-octave macro variation: a slow large-scale shade + a sharper
    // medium-scale tint, multiplied so adjacent "fields" read as visibly
    // different greens — the patchwork-quilt look you see flying over NI.
    float macroSlow = vnoise(vWorldPos.xz * macroBlendFreq);
    float macroMid  = vnoise(vWorldPos.xz * macroBlendFreq * 4.7 + 13.1);
    float macroFast = vnoise(vWorldPos.xz * macroBlendFreq * 13.0 + 27.3);
    // Tighter amplitudes so the cumulative grass-tint gain stays unclipped.
    float macroL = mix(0.86, 1.08, macroSlow) * mix(0.95, 1.03, macroMid);
    albedo *= macroL;
    // Per-cell green hue offset — only on grass-flagged pixels — to push
    // fields toward subtly different yellow-green / blue-green tones.
    // Amplitude trimmed so sunny fields don't blow out neon.
    float anyGrass = max(max(grassLayerMask.x, grassLayerMask.y),
                         max(grassLayerMask.z, grassLayerMask.w));
    if (anyGrass > 0.5) {
      vec3 hueShift = vec3(
        mix(0.95, 1.03, macroFast),
        mix(0.97, 1.05, macroMid),
        mix(0.90, 1.00, macroSlow)
      );
      albedo *= hueShift;
    }
    // Soft highlight knee — Reinhard-style compression on the brightest
    // pixels only. Pixels under ~0.7 luminance pass through unchanged;
    // anything brighter is gently compressed back below 1.0 so we never
    // get blown-white "neon" highlights regardless of slider settings.
    float lum = dot(albedo, vec3(0.299, 0.587, 0.114));
    if (lum > 0.7) {
      float k = (lum - 0.7) / max(lum, 1e-3);
      albedo = mix(albedo, albedo / (1.0 + (lum - 0.7) * 1.6), k);
    }

    // Compute the actual surface normal from screen-space position derivatives.
    // Independent of the geometry attribute → fixes any winding issues and
    // gives real per-fragment hill shading from the displaced heightmap.
    vec3 dposX = dFdx(vWorldPos);
    vec3 dposY = dFdy(vWorldPos);
    vec3 cx = cross(dposY, dposX);
    float cxLen = length(cx);
    vec3 faceN = cxLen > 1e-6 ? cx / cxLen : vec3(0.0, 1.0, 0.0);
    if (faceN.y < 0.0) faceN = -faceN;
    vec3 N = normalize(faceN + vec3(nLocal.x, 0.0, nLocal.y) * 0.6);
    vec3 L = normalize(sunDir);
    float lambert = max(dot(N, L), 0.0);
    float wrap = clamp(dot(N, L) * 0.5 + 0.5, 0.0, 1.0);

    // Cloud shadow modulation. Two-octave FBM at world XZ, drifting in the
    // wind direction at cloudSpeed. Subtracts up to cloudShadowStrength
    // from the sun term so direct light dims under clouds — ambient still
    // illuminates the shaded areas, just like in real overcast NI.
    vec2 cloudUV = vWorldPos.xz * cloudFreq + cloudDir * (time * cloudSpeed * cloudFreq);
    float c1 = vnoise(cloudUV);
    float c2 = vnoise(cloudUV * 2.3 + 11.7);
    float cloudMask = smoothstep(0.40, 0.85, c1 * 0.65 + c2 * 0.35);
    float sunMul = 1.0 - cloudShadowStrength * cloudMask;

    // Wet asphalt: darken the diffuse to ~0.55 at full wetness, mostly on
    // pixels where the asphalt opacity dominates. Real wet tarmac reads
    // ~40% darker than dry tarmac to the camera.
    float wetAsphalt = clamp(asphaltAmount, 0.0, 1.0) * wetness;
    albedo *= mix(1.0, 0.58, wetAsphalt);

    vec3 ambient = albedo * ambientColor * (0.6 + 0.4 * wrap);
    vec3 lit = albedo * sunColor * lambert * sunMul;

    // Specular highlight for water (Blinn–Phong-ish)
    if (waterAmount > 0.3) {
      vec3 V = normalize(cameraPosition - vWorldPos);
      vec3 H = normalize(L + V);
      float spec = pow(max(dot(N, H), 0.0), 64.0) * waterAmount;
      lit += spec * sunColor * 1.4;
    }
    // Wet-road specular — sharper exponent + dimmer than water so it reads
    // as a sheen, not a mirror. Multiplied by sunMul so cloudy ground
    // doesn't unrealistically glint.
    if (wetAsphalt > 0.05) {
      vec3 V2 = normalize(cameraPosition - vWorldPos);
      vec3 H2 = normalize(L + V2);
      float wetSpec = pow(max(dot(N, H2), 0.0), 96.0) * wetAsphalt;
      lit += wetSpec * sunColor * 0.7 * sunMul;
    }

    vec3 colour = ambient + lit;

    // Edge fade — soften the hard cut-off where the terrain ends
    vec2 edgeDist = max(abs(vWorldPos.xz) - terrainHalf * 0.92, 0.0);
    float edgeFade = clamp(1.0 - max(edgeDist.x, edgeDist.y) / (terrainHalf * 0.08), 0.0, 1.0);
    colour = mix(fogColor, colour, edgeFade);

    // Distance fog (atmospheric perspective)
    float dist = length(vWorldPos - cameraPosition);
    float fogF = clamp((dist - fogNear) / max(fogFar - fogNear, 0.001), 0.0, 1.0);
    fogF = pow(fogF, 1.4);    // mild ease so closer ground stays sharp
    colour = mix(colour, fogColor, fogF);

    gl_FragColor = vec4(colour, 1.0);
  }
`;


// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------
export function createTerrainMaterial({ tileSize = 4.0, fogColor = 0xc7d6e0, fogNear = 1500, fogFar = 7000, terrainHalf = 1000 } = {}) {
  const blank = new THREE.DataTexture(
    new Uint8Array([128, 128, 255, 255]), 1, 1, THREE.RGBAFormat,
  );
  blank.needsUpdate = true;
  const blank4 = [blank, blank, blank, blank];

  const mat = new THREE.ShaderMaterial({
    vertexShader: VERT,
    fragmentShader: FRAG,
    extensions: { derivatives: true },
    uniforms: {
      opacityMaps:   { value: blank4 },
      diffuseMaps:   { value: blank4 },
      normalMaps:    { value: blank4 },
      numLayers:     { value: 0 },
      waterLayerIndex: { value: -1 },
      tileSize:      { value: tileSize },
      time:          { value: 0 },
      sunDir:        { value: new THREE.Vector3(0.5, 0.7, 0.4).normalize() },
      sunColor:      { value: new THREE.Color(0xfff1d6) },
      ambientColor:  { value: new THREE.Color(0xb6cdde) },
      // 1/80 ≈ one slow-noise wavelength per ~80 m, which matches the
      // typical NI field size. Two faster octaves (4.7×, 13×) layer on top
      // inside the shader to break up monotone patches.
      macroBlendFreq:{ value: 0.0125 },
      fogColor:      { value: new THREE.Color(fogColor) },
      fogNear:       { value: fogNear },
      fogFar:        { value: fogFar },
      terrainHalf:   { value: terrainHalf },
      // Adjustable grass tint (settable from main page UI). Defaults
      // match the user-tuned slider values: hue=60° (mildly blue-green
      // shift), saturation 1.15, brightness 0.65 — duller and bluer than
      // a sunny day, which is bang-on for a typical Cookstown sky.
      grassTint:       { value: new THREE.Color(0.54, 1.34, 0.92) },
      grassSaturation: { value: 1.15 },
      grassBrightness: { value: 0.65 },
      grassLayerMask:  { value: new THREE.Vector4(0, 0, 0, 0) },
      // Cloud shadow controls — defaults match an overcast-but-not-grim
      // NI day. cloudShadowStrength=0 disables, =1 makes shadows opaque.
      cloudShadowStrength: { value: 0.32 },
      cloudSpeed:          { value: 6.5 },          // m/s, ≈ 23 km/h
      cloudDir:            { value: new THREE.Vector2(0.65, 0.76).normalize() },
      cloudFreq:           { value: 1.0 / 220.0 },  // ~220 m cloud cells
      // Wet-road controls (toggle from the main page UI).
      wetness:            { value: 0.0 },
      asphaltLayerMask:   { value: new THREE.Vector4(0, 0, 0, 0) },
    },
  });
  mat.userData.tickTime = (dt) => {
    mat.uniforms.time.value += dt;
  };
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
  const waterIdx = usable.findIndex((l) => l.key === "water");
  material.uniforms.waterLayerIndex.value = waterIdx;
  // Flag the grass-like classes so the tint uniforms only affect them.
  // Forest is included so its dark "forest_floor" tile reads as a richer
  // green canopy under the same tint slider — important because the bare
  // forest_floor PBR slug from Poly Haven is browny-grey by default.
  const GRASS_KEYS = new Set(["pasture", "lawn", "forest"]);
  const ASPHALT_KEYS = new Set(["asphalt"]);
  const mask = new THREE.Vector4(0, 0, 0, 0);
  const aspMask = new THREE.Vector4(0, 0, 0, 0);
  for (let i = 0; i < usable.length && i < 4; i++) {
    const k = ["x", "y", "z", "w"][i];
    if (GRASS_KEYS.has(usable[i].key)) mask[k] = 1.0;
    if (ASPHALT_KEYS.has(usable[i].key)) aspMask[k] = 1.0;
  }
  material.uniforms.grassLayerMask.value = mask;
  material.uniforms.asphaltLayerMask.value = aspMask;
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
