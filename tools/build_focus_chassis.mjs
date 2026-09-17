/**
 * build_focus_chassis.mjs — bakes the Focus-panel chassis model.
 *
 *   npm i three@0.160.0          # peer, not committed
 *   node tools/build_focus_chassis.mjs
 *
 * Writes intuition/static/vendor/models/focus-chassis.glb: a CLEAN-ROOM,
 * hand-typed generic 2026-spec open-wheeler. Every vertex here is authored from
 * stylised proportions — no team, marque, scan, or real-car data of any kind,
 * in the same spirit as the dashboard's FOCUS_CIRCUITS geometry. Proportions
 * (short wheelbase, narrow body, simple two-element wings, arched front-wheel
 * wake deflectors) are eyeballed from public 2026-regulation concept imagery
 * for silhouette only. The panel's standing "not affiliated with Formula 1"
 * note covers the styling cue.
 *
 * The mesh is exported as four merged parts, keyed by name so the runtime can
 * bucket them into materials: `hull` (monocoque, floor, airbox, sidepods),
 * `wing` (aero planes, halo, deflectors, suspension), `dark` (cockpit, inlets,
 * mirrors, helmet), `tyre` (wheels). The runtime assigns its own lit
 * MeshStandard materials, so materials here are placeholders.
 *
 * Space: +x nose, -x gearbox, +y up, +z right (z is half-width). Metres-ish.
 */
import * as THREE from 'three';
import { GLTFExporter } from 'three/examples/jsm/exporters/GLTFExporter.js';
import { mergeGeometries, mergeVertices } from 'three/examples/jsm/utils/BufferGeometryUtils.js';
import { writeFileSync, mkdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

// GLTFExporter reaches for a DOM FileReader; Node has Blob but not the reader.
globalThis.FileReader = class {
  #done(result) {
    this.result = result;
    this.onload?.({ target: this });
    this.onloadend?.({ target: this });
  }
  readAsArrayBuffer(blob) { blob.arrayBuffer().then((ab) => this.#done(ab)); }
  readAsDataURL(blob) {
    blob.arrayBuffer().then((ab) => this.#done(
      `data:${blob.type || 'application/octet-stream'};base64,${Buffer.from(ab).toString('base64')}`));
  }
};

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = resolve(HERE, '../intuition/static/vendor/models/focus-chassis.glb');

/* ── curve + surface helpers ─────────────────────────────────────────────── */

// Catmull-Rom through scalars — used to smooth a station profile before lofting.
function crScalar(vals, t) {
  const n = vals.length - 1, f = t * n, i = Math.min(n - 1, Math.floor(f)), u = f - i;
  const p0 = vals[Math.max(0, i - 1)], p1 = vals[i], p2 = vals[i + 1], p3 = vals[Math.min(n, i + 2)];
  const u2 = u * u, u3 = u2 * u;
  return 0.5 * ((2 * p1) + (-p0 + p2) * u
    + (2 * p0 - 5 * p1 + 4 * p2 - p3) * u2
    + (-p0 + 3 * p1 - 3 * p2 + p3) * u3);
}

// A superellipse ring in the y-z plane: `seg` [y,z] pairs centred on (cy, 0),
// half-height h, half-width w, corner fullness `pw` (1 = ellipse, →0 = box).
function ring(cy, w, h, seg, pw = 0.62) {
  const p = [];
  for (let i = 0; i < seg; i++) {
    const a = (i / seg) * Math.PI * 2, c = Math.cos(a), s = Math.sin(a);
    p.push(cy + h * Math.sign(s) * Math.abs(s) ** pw, w * Math.sign(c) * Math.abs(c) ** pw);
  }
  return p;
}

// Loft a closed tube through keyframe stations {x,w,h,cy,pw}, Catmull-Rom
// resampled to `xs` rings of `seg` verts. End caps optional.
function loft(stations, seg, xs, { capFront = true, capBack = true, pw = 0.62 } = {}) {
  const xv = stations.map((s) => s.x), wv = stations.map((s) => s.w);
  const hv = stations.map((s) => s.h), cyv = stations.map((s) => s.cy);
  const pwv = stations.map((s) => s.pw ?? pw);
  const pos = [], idx = [];
  for (let r = 0; r < xs; r++) {
    const t = r / (xs - 1);
    const x = crScalar(xv, t), w = crScalar(wv, t), h = crScalar(hv, t);
    const cy = crScalar(cyv, t), p = ring(cy, Math.max(0.006, w), Math.max(0.006, h), seg, crScalar(pwv, t));
    for (let i = 0; i < seg; i++) pos.push(x, p[i * 2], p[i * 2 + 1]);
  }
  for (let r = 0; r < xs - 1; r++) {
    for (let i = 0; i < seg; i++) {
      const a = r * seg + i, b = r * seg + (i + 1) % seg;
      idx.push(a, a + seg, b, b, a + seg, b + seg);
    }
  }
  const centre = (r, sign) => {
    const base = pos.length / 3;
    let cx = 0, cy = 0, cz = 0;
    for (let i = 0; i < seg; i++) { cx += pos[(r * seg + i) * 3]; cy += pos[(r * seg + i) * 3 + 1]; cz += pos[(r * seg + i) * 3 + 2]; }
    pos.push(cx / seg, cy / seg, cz / seg);
    for (let i = 0; i < seg; i++) {
      const a = r * seg + i, b = r * seg + (i + 1) % seg;
      idx.push(sign > 0 ? a : b, sign > 0 ? b : a, base);
    }
  };
  if (capFront) centre(0, 1);
  if (capBack) centre(xs - 1, -1);
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  g.setIndex(idx);
  g.computeVertexNormals();
  return g;
}

function tube(points, r, rad = 6, seg = 26) {
  const curve = new THREE.CatmullRomCurve3(points.map((p) => new THREE.Vector3(...p)));
  return new THREE.TubeGeometry(curve, seg, r, rad, false);
}

// A thin aero plane: a box, cambered by bowing it about the lateral axis.
function plane(chord, thick, span, x, y, z, camber = 0, twist = 0) {
  const g = new THREE.BoxGeometry(chord, thick, span, 8, 1, 1);
  const p = g.attributes.position;
  for (let i = 0; i < p.count; i++) {
    const px = p.getX(i);
    p.setY(i, p.getY(i) + camber * (1 - (px / (chord / 2)) ** 2));
  }
  g.computeVertexNormals();
  if (twist) g.rotateZ(twist);
  g.translate(x, y, z);
  return g;
}

function box(w, h, d, x, y, z, tilt = 0) {
  const g = new THREE.BoxGeometry(w, h, d);
  if (tilt) g.rotateZ(tilt);
  g.translate(x, y, z);
  return g;
}

// A tyre: a torus (rounded shoulders) squished toward a cylinder profile.
function tyre(R, width, x, z) {
  const g = new THREE.TorusGeometry(R - width * 0.28, width * 0.5, 12, 26);
  g.scale(1, 1, 0.86);              // flatten the tread a touch
  g.rotateY(Math.PI / 2);
  g.translate(x, R, z);
  return g;
}

function disc(r, x, y, z, seg = 16) {
  const g = new THREE.CircleGeometry(r, seg);
  g.rotateY(Math.PI / 2);
  g.translate(x, y, z);
  return g;
}

function lathe(profile, x, y, z, seg = 14) {
  const g = new THREE.LatheGeometry(profile.map(([py, pr]) => new THREE.Vector2(pr, py)), seg);
  g.rotateZ(Math.PI / 2);           // spin axis along x
  g.translate(x, y, z);
  return g;
}

/* ── the car ─────────────────────────────────────────────────────────────── */
// Proportioned toward the 2026 regulations — a shorter wheelbase and a ~8%
// narrower body than a 2022-spec car, two-element active-style front and rear
// wings, no bargeboards, and the tall arched front-wheel wake deflectors that
// define the look. Still every vertex hand-typed from stylised numbers: no
// team, marque, scan or real-car data of any kind.

const parts = { hull: [], wing: [], dark: [], tyre: [] };
const mir = (fn) => [1, -1].forEach((s) => parts[fn(s)[0]].push(fn(s)[1]));
const add = (bucket, geo) => parts[bucket].push(geo);

// monocoque + engine cover — one smooth lofted body, nose to gearbox.
// Low slender nose, tight coke-bottle, short tail (2026 wheelbase).
add('hull', loft([
  { x: 2.92, w: 0.035, h: 0.040, cy: 0.20, pw: 0.9 },  // nose tip (low)
  { x: 2.40, w: 0.090, h: 0.080, cy: 0.21 },
  { x: 1.82, w: 0.150, h: 0.125, cy: 0.24 },            // front bulkhead
  { x: 1.18, w: 0.225, h: 0.165, cy: 0.28 },            // dash hoop
  { x: 0.58, w: 0.300, h: 0.195, cy: 0.31, pw: 0.5 },   // cockpit front
  { x: 0.00, w: 0.320, h: 0.215, cy: 0.32, pw: 0.5 },   // cockpit
  { x: -0.52, w: 0.320, h: 0.275, cy: 0.33 },           // airbox root
  { x: -1.05, w: 0.255, h: 0.250, cy: 0.34 },
  { x: -1.55, w: 0.175, h: 0.185, cy: 0.35 },           // coke-bottle
  { x: -2.00, w: 0.115, h: 0.140, cy: 0.37 },
  { x: -2.36, w: 0.070, h: 0.090, cy: 0.39, pw: 0.9 },  // gearbox
], 26, 44));

// nose cone tip — a smooth lathe cap over the loft's blunt front
add('hull', lathe([[0, 0.001], [0.10, 0.026], [0.26, 0.048], [0.46, 0.038]], 2.60, 0.198, 0));

// sidepods — lofted teardrops with a strong top downwash ramp, tucked tight
mir((s) => ['hull', (() => {
  const g = loft([
    { x: 0.70, w: 0.02, h: 0.055, cy: 0.33 },
    { x: 0.40, w: 0.185, h: 0.195, cy: 0.33 },
    { x: -0.08, w: 0.235, h: 0.225, cy: 0.335 },
    { x: -0.62, w: 0.195, h: 0.180, cy: 0.335 },
    { x: -1.20, w: 0.090, h: 0.105, cy: 0.345 },
    { x: -1.52, w: 0.02, h: 0.045, cy: 0.355 },
  ], 18, 22, { pw: 0.55 });
  g.translate(0, 0, s * 0.38);
  return g;
})()]);
// sidepod inlet mouths — higher, slimmer 2026-style
mir((s) => ['dark', box(0.05, 0.15, 0.20, 0.48, 0.335, s * 0.38)]);
mir((s) => ['wing', (() => {
  const g = new THREE.TorusGeometry(0.115, 0.020, 8, 20);
  g.scale(1, 0.72, 1); g.rotateY(Math.PI / 2); g.translate(0.50, 0.335, s * 0.38);
  return g;
})()]);

// airbox / roll hoop + intake + a tall shark fin + T-camera
add('hull', lathe([[0, 0.02], [0.16, 0.14], [0.32, 0.13], [0.42, 0.045]], -0.02, 0.43, 0, 18));
add('dark', box(0.22, 0.14, 0.16, 0.04, 0.50, 0));
add('wing', (() => {
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(
    [-0.26, 0.54, 0, -2.05, 0.40, 0, -1.15, 0.76, 0], 3));
  g.computeVertexNormals();
  return g;
})());
add('dark', box(0.12, 0.05, 0.04, -0.16, 0.74, 0));

// cockpit rim + helmet + halo (hoop + front stay)
add('wing', (() => {
  const g = new THREE.TorusGeometry(0.225, 0.020, 8, 28);
  g.scale(1, 0.72, 1); g.rotateX(Math.PI / 2); g.translate(0.38, 0.44, 0);
  return g;
})());
add('dark', lathe([[0, 0.001], [0.06, 0.085], [0.15, 0.108], [0.23, 0.095], [0.29, 0.038]], 0.32, 0.44, 0, 16));
add('wing', tube([[0.05, 0.42, 0.24], [0.28, 0.70, 0.185], [0.68, 0.80, 0.075], [0.86, 0.76, 0]], 0.021, 8, 44));
add('wing', tube([[0.05, 0.42, -0.24], [0.28, 0.70, -0.185], [0.68, 0.80, -0.075], [0.86, 0.76, 0]], 0.021, 8, 44));
add('wing', tube([[0.86, 0.76, 0], [0.99, 0.40, 0]], 0.018, 6, 14));

// mirrors
mir((s) => ['dark', box(0.09, 0.05, 0.10, 0.52, 0.42, s * 0.36)]);

// floor pan + edge fences + upswept diffuser + strakes (narrower 2026 floor)
add('hull', box(3.15, 0.03, 1.20, 0.02, 0.070, 0));
mir((s) => ['wing', box(3.05, 0.09, 0.02, 0.06, 0.11, s * 0.60)]);
add('wing', box(0.60, 0.03, 1.12, -2.06, 0.13, 0, 0.36));
for (let i = -1; i <= 1; i++) add('wing', box(0.48, 0.16, 0.02, -2.08, 0.11, i * 0.32, 0.36));

// front-wheel wake control — a cambered winglet riding above each front tyre
// on a short strut off the chassis; the defining 2026 cue, kept tight and clean
mir((s) => ['wing', plane(0.62, 0.02, 0.30, 1.98, 0.76, s * 0.58, 0.06, s * -0.06)]);
mir((s) => ['wing', box(0.05, 0.30, 0.20, 1.82, 0.60, s * 0.44, s * 0.10)]);   // strut
// a low turning blade inboard of each front wheel
mir((s) => ['wing', box(0.44, 0.20, 0.02, 1.92, 0.27, s * 0.44, 0.06)]);

// front wing — two cambered elements + curved endplates + nose pylons
[[2.62, 0.045, 0.035], [2.46, 0.130, -0.09]].forEach(([x, y, cam], k) =>
  add('wing', plane(0.36 + k * 0.06, 0.018, 1.86, x, y, 0, cam)));
mir((s) => ['wing', box(0.58, 0.30, 0.03, 2.52, 0.15, s * 0.93)]);
mir((s) => ['wing', tube([[2.80, 0.18, 0], [2.52, 0.085, s * 0.14]], 0.017, 6, 10)]);

// rear wing — a clean two-element active-style plane, lower + more forward
// than a 2022-spec car, on a single swan-neck with two flush endplates
add('wing', plane(0.40, 0.025, 1.00, -2.30, 0.82, 0, 0.05, -0.10));
add('wing', plane(0.26, 0.02, 0.98, -2.46, 0.66, 0, 0.03, 0.22));
add('wing', tube([[-2.10, 0.44, 0], [-2.22, 0.66, 0], [-2.30, 0.80, 0]], 0.018, 6, 12));
mir((s) => ['wing', box(0.44, 0.30, 0.02, -2.36, 0.71, s * 0.50)]);

// wheels — tyres + rim discs + spoke covers (2026: slimmer tyres, shorter
// wheelbase, narrower track)
[{ x: 1.98, R: 0.39, w: 0.30 }, { x: -1.62, R: 0.44, w: 0.40 }].forEach((ax) => {
  [1, -1].forEach((s) => {
    const zc = s * (0.80 + ax.w / 2);
    add('tyre', tyre(ax.R, ax.w, ax.x, zc));
    add('dark', disc(ax.R * 0.60, ax.x, ax.R, zc + s * 0.001));
    add('wing', (() => {
      const g = new THREE.CylinderGeometry(ax.R * 0.90, ax.R * 0.90, 0.02, 16, 1, true);
      g.rotateZ(Math.PI / 2); g.translate(ax.x, ax.R, zc + s * (ax.w * 0.30));
      return g;
    })());
  });
});

// suspension — front & rear wishbones + push/pullrod, as thin tubes
[1, -1].forEach((s) => {
  const armF = (ya, yb) => [
    tube([[1.98, ya, s * 0.50], [1.50, yb, s * 0.15]], 0.013, 5, 6),
    tube([[1.98, ya, s * 0.50], [1.24, yb, s * 0.15]], 0.013, 5, 6),
  ];
  const armR = (ya, yb) => [
    tube([[-1.62, ya, s * 0.52], [-1.20, yb, s * 0.15]], 0.013, 5, 6),
    tube([[-1.62, ya, s * 0.52], [-1.40, yb, s * 0.15]], 0.013, 5, 6),
  ];
  [...armF(0.44, 0.40), ...armF(0.24, 0.22), tube([[1.98, 0.42, s * 0.46], [1.46, 0.24, s * 0.15]], 0.012, 5, 6),
    ...armR(0.50, 0.42), ...armR(0.28, 0.26), tube([[-1.62, 0.48, s * 0.50], [-1.30, 0.26, s * 0.15]], 0.012, 5, 6)]
    .forEach((g) => add('wing', g));
});

/* ── merge, name, export ─────────────────────────────────────────────────── */

const scene = new THREE.Scene();
scene.name = 'FocusChassis';
const K = { hull: 1, wing: 0.85, dark: 0.32, tyre: 1 };   // legacy per-bucket hint, kept in `extras`

// mergeGeometries needs every input to carry the same attribute set and the
// same indexed-ness: reduce each to a bare non-indexed position stream.
function bare(g) {
  const ni = g.index ? g.toNonIndexed() : g;
  const out = new THREE.BufferGeometry();
  out.setAttribute('position', ni.attributes.position.clone());
  return out;
}

let tris = 0;
for (const [name, geos] of Object.entries(parts)) {
  // weld coincident verts, then drop everything but position — the runtime
  // recomputes normals and needs no uv.
  let clean = mergeVertices(mergeGeometries(geos.map(bare), false), 1e-4);
  const idx = clean.index;
  const stripped = new THREE.BufferGeometry();
  stripped.setAttribute('position', clean.attributes.position);
  stripped.setIndex(idx);
  tris += idx.count / 3;
  const mesh = new THREE.Mesh(stripped, new THREE.MeshStandardMaterial({ color: 0x39d3ff }));
  mesh.name = name;
  mesh.userData.k = K[name];        // survives as glTF `extras`
  scene.add(mesh);
}

const result = await new Promise((res, rej) => {
  new GLTFExporter().parse(scene, res, rej,
    { binary: true, onlyVisible: false, includeCustomExtensions: true });
});
mkdirSync(dirname(OUT), { recursive: true });
writeFileSync(OUT, Buffer.from(result));
console.log(`wrote ${OUT}  (${(Buffer.from(result).length / 1024).toFixed(1)} KiB, ~${Math.round(tris)} tris)`);
