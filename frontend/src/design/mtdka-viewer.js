/*!
 * <mtdka-viewer> — лёгкий WebGL-вьюер GLB с бесконечным вращением вокруг вертикальной оси.
 * Без зависимостей (~13 КБ, ~5 КБ gzip). Работает в любом фреймворке как обычный HTML-тег.
 *
 *   <script src="assets/mtdka-viewer.js" defer></script>   (можно и type="module")
 *   <mtdka-viewer src="assets/mtdka.glb" speed="0.35" tilt="8">
 *     <img src="assets/mtdka-poster.png" alt="МТД-КА">   <!-- запасной вариант без WebGL -->
 *   </mtdka-viewer>
 *
 * Атрибуты: src — путь к GLB; speed — рад/с (по умолчанию 0.2, ~31 с на оборот);
 * tilt — наклон камеры, градусы; start — начальный угол, градусы; drag — "off" отключает вращение мышью.
 * Вращение ставится на паузу вне экрана и во фоновой вкладке; атрибут respect-motion останавливает модель при системной настройке «уменьшить движение».
 *
 * Накладки поверх модели (координаты модели в метрах: X вправо, Y вверх, Z к зрителю):
 *   <div slot="hotspot" data-pos="0 2.04 0.14" data-normal="0 0 1">…</div> — следует за точкой, скрывается, когда точка уходит за модель;
 *   <svg data-dims> с <line data-a="x y z" data-b="x y z"> и <text data-at="x y z"> — размерные линии.
 * Методы: hold() / release() — плавно остановить и снова запустить вращение (счётчик, можно вызывать из разных мест).
 */
const VS = `
attribute vec3 p; attribute vec3 n; attribute vec2 t;
uniform mat4 M, VP; varying vec3 vN, vW; varying vec2 vT;
void main(){ vec4 w = M * vec4(p,1.); vW = w.xyz; vN = mat3(M) * n; vT = t; gl_Position = VP * w; }`;
const FS = `
precision mediump float;
varying vec3 vN, vW; varying vec2 vT;
uniform vec4 base; uniform float metal, rough, hasTex, hasEm; uniform vec3 cam, emF;
uniform sampler2D tex, em;
vec3 lin(vec3 c){ return pow(c, vec3(2.2)); }
vec3 sky(vec3 d){ return mix(vec3(.10,.10,.13), vec3(1.,1.02,1.1), smoothstep(-.3,.8,d.y)); }
vec3 light(vec3 N, vec3 V, vec3 L, vec3 col, vec3 dif, vec3 spc, float sh){
  float d = max(dot(N,L),0.); vec3 H = normalize(L+V);
  float s = pow(max(dot(N,H),0.), sh) * (sh+8.)/25.;
  return col * d * (dif + spc * s);
}
void main(){
  vec3 N = normalize(vN), V = normalize(cam - vW);
  if(!gl_FrontFacing) N = -N;
  vec3 c = base.rgb; if(hasTex > .5) c *= lin(texture2D(tex, vT).rgb);
  vec3 dif = c * (1. - metal), spc = mix(vec3(.04), c, metal);
  float r = clamp(rough, .05, 1.), sh = clamp(2./(r*r*r*r) - 2., 2., 2048.);
  vec3 col = light(N,V, normalize(vec3(.55,.75,.9)), vec3(1.7), dif, spc, sh)
           + light(N,V, normalize(vec3(-.8,.35,.3)), vec3(.55,.58,.7), dif, spc, sh)
           + light(N,V, normalize(vec3(.1,.4,-1.)), vec3(.9), dif, spc, sh);
  col += dif * mix(vec3(.05,.05,.06), vec3(.3,.32,.4), N.y*.5+.5);
  float fr = pow(1. - max(dot(N,V),0.), 5.);
  col += sky(reflect(-V,N)) * (spc + (1.-spc)*fr*(1.-r)) * (1.-r*.85) * .55;
  if(hasEm > .5) col += lin(texture2D(em, vT).rgb) * emF * 1.6;
  col = col*(2.51*col+.03)/(col*(2.43*col+.59)+.14);
  gl_FragColor = vec4(pow(clamp(col,0.,1.), vec3(1./2.2)), 1.);
}`;

// пол: сетка с затуханием к краям и мягкая тень от основания (вращается вместе с моделью)
const FVS = `attribute vec2 p; uniform mat4 VP; uniform vec3 c; varying vec2 w;
void main(){ w = p + c.xz; gl_Position = VP * vec4(w.x, 0.0, w.y, 1.); }`;
const FFS = `#extension GL_OES_standard_derivatives : enable
precision mediump float; varying vec2 w; uniform vec3 cf; uniform float ang, deriv;
float grid(vec2 q, float k){ vec2 g = q / k; vec2 f = abs(fract(g - .5) - .5);
  vec2 d = deriv > .5 ? fwidth(g) : vec2(.02); vec2 l = 1. - smoothstep(vec2(0.), d * 1.2, f); return max(l.x, l.y); }
void main(){
  vec2 q = w - cf.xz; float r = length(q);
  float fade = 1. - smoothstep(.35, 1.25, r);
  float a = grid(w, .25) * .12 + grid(w, 1.) * .2;
  float cs = cos(ang), sn = sin(ang); vec2 m = vec2(cs * q.x - sn * q.y, sn * q.x + cs * q.y);   // в осях модели
  float sh = exp(-pow(m.x / .6, 2.) - pow(m.y / .34, 2.)) * .22;
  float feet = exp(-pow((abs(m.x) - .395) / .07, 2.) - pow(m.y / .3, 4.)) * .55;
  float al = clamp(a * fade + sh + feet, 0., .75);
  gl_FragColor = vec4(vec3(.035, .10, .35) * al, al);
}`;
const persp = (f, a, n, z) => { const t = 1 / Math.tan(f / 2); return [t / a,0,0,0, 0,t,0,0, 0,0,(z+n)/(n-z),-1, 0,0,2*z*n/(n-z),0]; };
const mul = (a, b) => { const o = new Array(16); for (let i = 0; i < 4; i++) for (let j = 0; j < 4; j++) { let s = 0; for (let k = 0; k < 4; k++) s += a[k*4+j] * b[i*4+k]; o[i*4+j] = s; } return o; };
const lookAt = (e, c) => { const z = norm(sub(e, c)), x = norm(cross([0,1,0], z)), y = cross(z, x);
  return [x[0],y[0],z[0],0, x[1],y[1],z[1],0, x[2],y[2],z[2],0, -dot(x,e),-dot(y,e),-dot(z,e),1]; };
const sub = (a, b) => a.map((v, i) => v - b[i]), dot = (a, b) => a[0]*b[0]+a[1]*b[1]+a[2]*b[2];
const cross = (a, b) => [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]];
const norm = a => { const l = Math.hypot(...a) || 1; return a.map(v => v / l); };
const rotY = (a, cx, cz) => { const c = Math.cos(a), s = Math.sin(a); return [c,0,-s,0, 0,1,0,0, s,0,c,0, cx - c*cx - s*cz, 0, cz + s*cx - c*cz, 1]; };

async function parseGLB(buf) {
  const dv = new DataView(buf); if (dv.getUint32(0, true) !== 0x46546C67) throw new Error('not GLB');
  const jl = dv.getUint32(12, true);
  const json = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 20, jl)));
  const binOff = 20 + jl + 8;
  const view = i => { const v = json.bufferViews[i]; return new Uint8Array(buf, binOff + (v.byteOffset || 0), v.byteLength); };
  const acc = i => { const a = json.accessors[i], v = view(a.bufferView), T = { 5126: Float32Array, 5123: Uint16Array, 5125: Uint32Array }[a.componentType];
    const sz = { SCALAR: 1, VEC2: 2, VEC3: 3, VEC4: 4 }[a.type]; return { data: new T(v.buffer, v.byteOffset + (a.byteOffset || 0), a.count * sz), a }; };
  const imgs = await Promise.all((json.images || []).map(im => createImageBitmap(new Blob([view(im.bufferView)], { type: im.mimeType }))));
  return { json, acc, imgs };
}

// Vite supplies a bundled same-origin GLB. A load failure keeps the poster;
// never inject a fallback script or fetch a second source.
async function getGLB(src, signal) {
  const r = await fetch(src, { signal, credentials: 'omit' });
  if (!r.ok) throw new Error('Model unavailable');
  return r.arrayBuffer();
}

class MtdkaViewer extends HTMLElement {
  connectedCallback() {
    if (this._init) return; this._init = true;
    this.life = new AbortController(); this.resources = { buffers: [], textures: [], shaders: [], programs: [] };
    this.loading = false; this.ready = false; this.target = null; this.dragging = false; this.raf = 0;
    const cs = getComputedStyle(this); if (cs.display === 'inline') this.style.display = 'block'; if (cs.position === 'static') this.style.position = 'relative';
    const cv = this.cv = document.createElement('canvas');
    cv.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;touch-action:pan-y;cursor:grab;opacity:0;transition:opacity .4s';
    cv.setAttribute('role', 'img'); cv.setAttribute('aria-label', this.getAttribute('label') || '3D-модель металлодетектора МТД-КА');
    const gl = this.gl = cv.getContext('webgl', { antialias: true, alpha: true, premultipliedAlpha: true, powerPreference: 'low-power' });
    if (!gl) { this.dispatchEvent(new Event('viewer-error')); return; } // остаётся запасное содержимое (картинка)
    this.append(cv);
    // ленивая загрузка: начинаем, когда элемент рядом с экраном
    this.io = new IntersectionObserver(es => { this.visible = es[0].isIntersecting; if (this.visible) { this.load(); this.kick(); } }, { rootMargin: '200px' });
    this.io.observe(this);
    this.ro = new ResizeObserver(() => { this.resize(); this.draw(); }); this.ro.observe(this);
    document.addEventListener('visibilitychange', () => this.kick(), { signal: this.life.signal });
    cv.addEventListener('webglcontextlost', e => { e.preventDefault(); this.disconnectedCallback(); this.dispatchEvent(new Event('viewer-error')); }, { signal: this.life.signal });
    this.reduce = this.hasAttribute('respect-motion') ? matchMedia('(prefers-reduced-motion: reduce)') : { matches: false }; // по умолчанию крутится всегда; respect-motion — уважать системную настройку
    this.reduce.addEventListener?.('change', () => this.kick(), { signal: this.life.signal });
    this.angle = (+this.getAttribute('start') || 25) * Math.PI / 180; this.vel = 0; this.holds = 0; this.cur = null;
    // мышь над моделью — плавная остановка, чтобы можно было попасть в точку
    this.addEventListener('pointerenter', e => { if (e.pointerType === 'mouse') this.hold(); }, { signal: this.life.signal });
    this.addEventListener('pointerleave', e => { if (e.pointerType === 'mouse') this.release(); }, { signal: this.life.signal });
    if (this.getAttribute('drag') !== 'off') this.drag();
  }
  hold() { this.holds++; this.kick(); }
  // плавно повернуть к углу (градусы) и держать; pose(null) — снова вращать
  pose(deg) {
    if (deg == null) { if (this.target != null) { this.target = null; this.release(); } return; }
    if (this.target == null) this.hold();
    this.target = deg * Math.PI / 180; this.kick();
  }
  release() { this.holds = Math.max(0, this.holds - 1); this.kick(); }
  // точка модели → пиксели элемента; z<1 — перед камерой
  project(p) {
    const r = rotY(this.angle, this.box.c[0], this.box.c[2]), w = [r[0]*p[0]+r[8]*p[2]+r[12], p[1], r[2]*p[0]+r[10]*p[2]+r[14]], V = this.VP;
    const x = V[0]*w[0]+V[4]*w[1]+V[8]*w[2]+V[12], y = V[1]*w[0]+V[5]*w[1]+V[9]*w[2]+V[13], q = V[3]*w[0]+V[7]*w[1]+V[11]*w[2]+V[15];
    return { x: (x / q * .5 + .5) * this.clientWidth, y: (.5 - y / q * .5) * this.clientHeight, w };
  }
  overlays() {
    const vec = a => (a || '').trim().split(/\s+/).map(Number);
    for (const h of this.querySelectorAll(':scope>[slot=hotspot]')) {
      const P = this.project(vec(h.dataset.pos)); h.style.transform = `translate(${P.x.toFixed(1)}px,${P.y.toFixed(1)}px)`;
      let vis = true;
      if (h.dataset.normal) { const n = vec(h.dataset.normal), c = Math.cos(this.angle), s = Math.sin(this.angle);
        const nw = [c*n[0]+s*n[2], n[1], -s*n[0]+c*n[2]]; vis = dot(nw, norm(sub(this.eye, P.w))) > .12; }
      if (h.hidden === vis) h.hidden = !vis;
    }
    const svg = this.querySelector(':scope>svg[data-dims]');
    if (svg && !svg.hasAttribute('hidden')) {
      svg.setAttribute('viewBox', `0 0 ${this.clientWidth} ${this.clientHeight}`);
      for (const l of svg.querySelectorAll('[data-a]')) { const a = this.project(vec(l.dataset.a)), b = this.project(vec(l.dataset.b));
        l.setAttribute('x1', a.x.toFixed(1)); l.setAttribute('y1', a.y.toFixed(1)); l.setAttribute('x2', b.x.toFixed(1)); l.setAttribute('y2', b.y.toFixed(1)); }
      for (const t of svg.querySelectorAll('[data-at]')) { const a = this.project(vec(t.dataset.at)); t.setAttribute('x', a.x.toFixed(1)); t.setAttribute('y', a.y.toFixed(1)); }
    }
  }
  disconnectedCallback() {
    this.life?.abort(); this.io?.disconnect(); this.ro?.disconnect(); cancelAnimationFrame(this.raf);
    this.raf = 0; this.ready = false; this.loading = false; this._init = false;
    const gl = this.gl;
    if (gl && this.resources) {
      for (const b of this.resources.buffers) gl.deleteBuffer(b);
      for (const t of this.resources.textures) gl.deleteTexture(t);
      for (const p of this.resources.programs) gl.deleteProgram(p);
      for (const s of this.resources.shaders) gl.deleteShader(s);
      gl.getExtension('WEBGL_lose_context')?.loseContext();
    }
    this.cv?.remove(); this.classList.remove('ready');
    for (const ch of this.children) ch.style.visibility = '';
  }
  async load() {
    if (this.loading || this.life.signal.aborted) return; this.loading = true;
    const signal = this.life.signal;
    try {
      const { json, acc, imgs } = await parseGLB(await getGLB(this.getAttribute('src'), signal));
      if (signal.aborted) { imgs.forEach(im => im.close()); return; }
      const gl = this.gl, sh = (t, s) => { const o = gl.createShader(t); this.resources.shaders.push(o); gl.shaderSource(o, s); gl.compileShader(o); if (!gl.getShaderParameter(o, gl.COMPILE_STATUS)) throw new Error('Shader unavailable'); return o; };
      const pr = this.pr = gl.createProgram(); this.resources.programs.push(pr); gl.attachShader(pr, sh(gl.VERTEX_SHADER, VS)); gl.attachShader(pr, sh(gl.FRAGMENT_SHADER, FS)); gl.linkProgram(pr); if (!gl.getProgramParameter(pr, gl.LINK_STATUS)) throw new Error('WebGL unavailable');
      this.u = {}; for (const k of ['M','VP','base','metal','rough','hasTex','hasEm','cam','emF','tex','em']) this.u[k] = gl.getUniformLocation(pr, k);
      this.loc = { p: gl.getAttribLocation(pr, 'p'), n: gl.getAttribLocation(pr, 'n'), t: gl.getAttribLocation(pr, 't') };
      const texs = imgs.map(im => { const t = gl.createTexture(); this.resources.textures.push(t); gl.bindTexture(gl.TEXTURE_2D, t); gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, im);
        const pot = !(im.width & (im.width - 1)) && !(im.height & (im.height - 1)); if (pot) gl.generateMipmap(gl.TEXTURE_2D); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, pot ? gl.LINEAR_MIPMAP_LINEAR : gl.LINEAR); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE); im.close(); return t; });
      const texOf = ti => ti == null ? null : texs[json.textures[ti].source];
      const buf = (d, tgt = gl.ARRAY_BUFFER) => { const b = gl.createBuffer(); this.resources.buffers.push(b); gl.bindBuffer(tgt, b); gl.bufferData(tgt, d, gl.STATIC_DRAW); return b; };
      let lo = [1e9,1e9,1e9], hi = [-1e9,-1e9,-1e9];
      this.prims = json.meshes[0].primitives.map(p => {
        const P = acc(p.attributes.POSITION), I = acc(p.indices), m = json.materials[p.material] || {}, pb = m.pbrMetallicRoughness || {};
        P.a.min.forEach((v, i) => lo[i] = Math.min(lo[i], v)); P.a.max.forEach((v, i) => hi[i] = Math.max(hi[i], v));
        if (I.data instanceof Uint32Array) gl.getExtension('OES_element_index_uint');
        return { p: buf(P.data), n: buf(acc(p.attributes.NORMAL).data), t: p.attributes.TEXCOORD_0 != null ? buf(acc(p.attributes.TEXCOORD_0).data) : null,
          i: buf(I.data, gl.ELEMENT_ARRAY_BUFFER), count: I.data.length, type: I.data instanceof Uint32Array ? gl.UNSIGNED_INT : gl.UNSIGNED_SHORT,
          base: pb.baseColorFactor || [1,1,1,1], metal: pb.metallicFactor ?? 1, rough: pb.roughnessFactor ?? 1,
          tex: texOf(pb.baseColorTexture?.index), em: texOf(m.emissiveTexture?.index), emF: m.emissiveFactor || [0,0,0] };
      });
      this.box = { lo, hi, c: lo.map((v, i) => (v + hi[i]) / 2) };
      gl.enable(gl.DEPTH_TEST); gl.enable(gl.CULL_FACE);
      if (this.getAttribute('floor') !== 'off') {
        const deriv = !!gl.getExtension('OES_standard_derivatives');
        const fp = this.fp = gl.createProgram(); this.resources.programs.push(fp); gl.attachShader(fp, sh(gl.VERTEX_SHADER, FVS)); gl.attachShader(fp, sh(gl.FRAGMENT_SHADER, deriv ? FFS : FFS.replace(/^#extension.*\n/, '').replace('fwidth(g)', 'vec2(.02)'))); gl.linkProgram(fp);
        if (!gl.getProgramParameter(fp, gl.LINK_STATUS)) this.fp = null;
        else { this.fu = { VP: gl.getUniformLocation(fp, 'VP'), c: gl.getUniformLocation(fp, 'c'), cf: gl.getUniformLocation(fp, 'cf'), ang: gl.getUniformLocation(fp, 'ang'), deriv: gl.getUniformLocation(fp, 'deriv') };
          this.fa = gl.getAttribLocation(fp, 'p'); this.fderiv = deriv; const S = 1.6; this.fbuf = buf(new Float32Array([-S,-S, S,-S, -S,S, S,S])); }
      }
      this.resize(); this.ready = true; this.draw(); this.cv.style.opacity = 1;
      [...this.children].forEach(ch => ch !== this.cv && !ch.matches('[slot=hotspot],svg[data-dims]') && (ch.style.visibility = 'hidden'));
      this.classList.add('ready');
      this.dispatchEvent(new Event('load')); this.kick();
    } catch { if (!signal.aborted) { this.disconnectedCallback(); this.dispatchEvent(new Event('viewer-error')); } }
  }
  resize() {
    const r = Math.min(devicePixelRatio || 1, 2), w = Math.max(1, Math.round(this.clientWidth * r)), h = Math.max(1, Math.round(this.clientHeight * r));
    if (this.cv.width !== w || this.cv.height !== h) { this.cv.width = w; this.cv.height = h; }
  }
  draw() {
    if (!this.ready) return;
    const gl = this.gl, { lo, hi, c } = this.box, a = this.cv.width / this.cv.height, fov = 30 * Math.PI / 180;
    const tilt = (+this.getAttribute('tilt') || 8) * Math.PI / 180;
    const R = Math.hypot(hi[0] - lo[0], hi[2] - lo[2]) / 2, halfH = (hi[1] - lo[1]) / 2;
    const floor = !!this.fp, k = floor ? 1.16 : 1.06;            // с полом — чуть больше воздуха снизу, чтобы было видно, на чём стоит
    const dist = Math.max(halfH / Math.tan(fov / 2), R / (Math.tan(fov / 2) * a)) * k + R;
    const tgt = floor ? [c[0], c[1] - halfH * .1, c[2]] : c;
    const eye = [tgt[0], tgt[1] + Math.sin(tilt) * dist, tgt[2] + Math.cos(tilt) * dist];
    const VP = mul(persp(fov, a, 0.05, 50), lookAt(eye, tgt)), M = rotY(this.angle, c[0], c[2]);
    this.VP = VP; this.eye = eye;
    gl.viewport(0, 0, this.cv.width, this.cv.height); gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.useProgram(this.pr); const u = this.u, L = this.loc;
    gl.uniformMatrix4fv(u.VP, false, VP); gl.uniformMatrix4fv(u.M, false, M); gl.uniform3fv(u.cam, eye);
    gl.uniform1i(u.tex, 0); gl.uniform1i(u.em, 1);
    for (const p of this.prims) {
      gl.bindBuffer(gl.ARRAY_BUFFER, p.p); gl.enableVertexAttribArray(L.p); gl.vertexAttribPointer(L.p, 3, gl.FLOAT, false, 0, 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, p.n); gl.enableVertexAttribArray(L.n); gl.vertexAttribPointer(L.n, 3, gl.FLOAT, false, 0, 0);
      if (p.t && L.t >= 0) { gl.bindBuffer(gl.ARRAY_BUFFER, p.t); gl.enableVertexAttribArray(L.t); gl.vertexAttribPointer(L.t, 2, gl.FLOAT, false, 0, 0); }
      else if (L.t >= 0) { gl.disableVertexAttribArray(L.t); gl.vertexAttrib2f(L.t, 0, 0); }
      gl.uniform4fv(u.base, p.base); gl.uniform1f(u.metal, p.metal); gl.uniform1f(u.rough, p.rough);
      gl.uniform1f(u.hasTex, p.tex ? 1 : 0); gl.uniform1f(u.hasEm, p.em ? 1 : 0); gl.uniform3fv(u.emF, p.emF);
      gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, p.tex); gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, p.em);
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, p.i); gl.drawElements(gl.TRIANGLES, p.count, p.type, 0);
    }
    if (this.fp) {   // пол рисуем после модели: корпус закрывает сетку за собой, тень лежит под опорами
      if (L.n >= 0) gl.disableVertexAttribArray(L.n); if (L.t >= 0) gl.disableVertexAttribArray(L.t);
      gl.useProgram(this.fp); gl.disable(gl.CULL_FACE); gl.depthMask(false); gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      gl.uniformMatrix4fv(this.fu.VP, false, VP); gl.uniform3fv(this.fu.c, [c[0], 0, c[2]]); gl.uniform3fv(this.fu.cf, [c[0], 0, c[2]]); gl.uniform1f(this.fu.ang, this.angle); gl.uniform1f(this.fu.deriv, this.fderiv ? 1 : 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.fbuf); gl.enableVertexAttribArray(this.fa); gl.vertexAttribPointer(this.fa, 2, gl.FLOAT, false, 0, 0);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      gl.disableVertexAttribArray(this.fa); gl.depthMask(true); gl.disable(gl.BLEND); gl.enable(gl.CULL_FACE);
    }
    this.overlays();
    this.onframe?.();
  }
  kick() {
    const run = this.isConnected && this.ready && this.visible && !document.hidden;
    if (!run || this.raf) return;
    let last = performance.now();
    const tick = now => {
      if (!this.ready || this.life.signal.aborted || !this.isConnected) { this.raf = 0; return; }
      this.raf = 0; const dt = Math.min((now - last) / 1000, .1); last = now;
      const target = this.reduce.matches || this.holds ? 0 : (+this.getAttribute('speed') || 0.2);
      if (this.cur === null) this.cur = target;
      this.cur += (target - this.cur) * Math.min(1, dt * 5);      // плавный разгон и остановка
      const speed = Math.abs(this.cur) < 1e-3 && !target ? 0 : this.cur;
      if (this.target != null && !this.dragging) {
        let d = ((this.target - this.angle) % (Math.PI * 2) + Math.PI * 3) % (Math.PI * 2) - Math.PI;   // кратчайший путь
        this.vel = 0; this.angle += Math.abs(d) < 1e-3 ? d : d * Math.min(1, dt * 4);
      } else if (!this.dragging) { this.vel *= Math.pow(.04, dt); this.angle += (speed + this.vel) * dt; }
      this.angle %= Math.PI * 2;               // замкнутый цикл: угол по модулю 2π, без скачков
      this.draw();
      const posing = this.target != null && Math.abs(((this.target - this.angle) % (Math.PI * 2) + Math.PI * 3) % (Math.PI * 2) - Math.PI) > 1e-3;
      if (this.ready && this.visible && !document.hidden && (speed || posing || this.dragging || Math.abs(this.vel) > 1e-3)) this.raf = requestAnimationFrame(tick);
    };
    this.raf = requestAnimationFrame(tick);
  }
  drag() {
    let x0 = 0, t0 = 0;
    this.addEventListener('pointerdown', e => { if (e.target !== this.cv) return; this.dragging = true; x0 = e.clientX; t0 = performance.now(); this.vel = 0; this.cv.style.cursor = 'grabbing'; this.setPointerCapture(e.pointerId); this.kick(); }, { signal: this.life.signal });
    this.addEventListener('pointermove', e => { if (!this.dragging) return; const dx = (e.clientX - x0) / this.clientWidth * Math.PI * 2, now = performance.now();
      this.angle += dx; this.vel = dx / Math.max((now - t0) / 1000, .016); x0 = e.clientX; t0 = now; this.draw(); }, { signal: this.life.signal });
    const up = () => { if (!this.dragging) return; this.dragging = false; if (this.target != null) this.target = this.angle; this.vel = Math.max(-6, Math.min(6, this.vel)); this.cv.style.cursor = 'grab'; this.kick(); };
    this.addEventListener('pointerup', up, { signal: this.life.signal }); this.addEventListener('pointercancel', up, { signal: this.life.signal });
  }
}
customElements.get('mtdka-viewer') || customElements.define('mtdka-viewer', MtdkaViewer);
