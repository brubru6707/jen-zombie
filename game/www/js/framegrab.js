// Pull a JPEG out of the AR camera image, cheaply.
//
// XRWebGLBinding.getCameraImage() hands back a WebGLTexture (1920x886 on this
// phone) that is only valid inside the frame callback. The raw-camera-access
// spec guarantees you can SAMPLE it; attaching it to a framebuffer is not
// promised. So: draw it through a trivial shader into a small offscreen
// target -- the downscale happens on the GPU -- and readPixels only that
// target (800x369 RGBA = 1.2 MB, not 6.8 MB). Encoding to JPEG then runs on a
// 2D canvas, mostly off the main thread.
//
// readPixels is a GPU sync and stalls the frame; grab() reports its cost so
// the caller can see it. It happens once per countdown, never per frame.
//
// GL state touched here is restored, and the caller should still call
// renderer.resetState() so three re-syncs its own cache.

const VS = `attribute vec2 p; varying vec2 v;
void main() { v = p * 0.5 + 0.5; gl_Position = vec4(p, 0.0, 1.0); }`;
const FS = `precision mediump float; uniform sampler2D t; uniform float flip; varying vec2 v;
void main() { gl_FragColor = vec4(texture2D(t, vec2(v.x, mix(v.y, 1.0 - v.y, flip))).rgb, 1.0); }`;

export class FrameGrabber {
  constructor(gl, { maxWidth = 800, flipY = true, quality = 0.8 } = {}) {
    this.gl = gl;
    this.maxWidth = maxWidth;
    this.flipY = flipY;
    this.quality = quality;
    this.prog = null; this.fbo = null; this.tex = null; this.vbo = null;
    this.w = 0; this.h = 0; this.pixels = null;
    this.canvas = null; this.ctx = null;
    this.count = 0; this.lastMs = 0; this.maxMs = 0; this.lastBytes = 0;
    this.fails = 0; this.lastError = null;
  }

  _shader(type, src) {
    const gl = this.gl, s = gl.createShader(type);
    gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
      throw new Error('shader: ' + gl.getShaderInfoLog(s));
    }
    return s;
  }

  _init() {
    const gl = this.gl;
    const prog = gl.createProgram();
    gl.attachShader(prog, this._shader(gl.VERTEX_SHADER, VS));
    gl.attachShader(prog, this._shader(gl.FRAGMENT_SHADER, FS));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
      throw new Error('program: ' + gl.getProgramInfoLog(prog));
    }
    this.prog = prog;
    this.aPos = gl.getAttribLocation(prog, 'p');
    this.uTex = gl.getUniformLocation(prog, 't');
    this.uFlip = gl.getUniformLocation(prog, 'flip');
    this.vbo = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, this.vbo);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    this.fbo = gl.createFramebuffer();
    this.tex = gl.createTexture();
  }

  /**
   * Compile the program and allocate the target ahead of time, outside the
   * frame loop, so the first capture pays only for the draw + readPixels
   * (measured: 34 ms cold, 10-17 ms warm on the SM-S156V). Never throws.
   */
  warm(sw = 886, sh = 1920) {
    try {
      if (!this.prog) this._init();
      this._ensureTarget(sw, sh);
      const gl = this.gl;
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      gl.bindTexture(gl.TEXTURE_2D, null);
      return true;
    } catch (e) { this.lastError = String(e && e.message || e); return false; }
  }

  /** Target size: LONG edge capped at maxWidth, aspect kept. The phone's camera
   *  image is 886x1920 (portrait) in practice, not the 1920x886 Stage 0 noted. */
  targetSize(sw, sh) {
    const scale = Math.min(1, this.maxWidth / Math.max(1, sw, sh));
    return [Math.max(1, Math.round(sw * scale)), Math.max(1, Math.round(sh * scale))];
  }

  _ensureTarget(sw, sh) {
    const gl = this.gl;
    const [w, h] = this.targetSize(sw, sh);
    if (w === this.w && h === this.h) return;
    gl.bindTexture(gl.TEXTURE_2D, this.tex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.fbo);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, this.tex, 0);
    const st = gl.checkFramebufferStatus(gl.FRAMEBUFFER);
    if (st !== gl.FRAMEBUFFER_COMPLETE) throw new Error('framebuffer incomplete: ' + st);
    this.w = w; this.h = h;
    this.pixels = new Uint8Array(w * h * 4);
  }

  /**
   * Synchronous: draw the camera texture downscaled and read it back.
   * Returns {width, height, ms}; pixels are left in this.pixels for encode().
   * Throws on GL failure (the caller falls back to the canned world).
   */
  grab(texture, sw, sh) {
    const gl = this.gl;
    const t0 = (typeof performance !== 'undefined' ? performance.now() : Date.now());
    try {
      if (!this.prog) this._init();
      this._ensureTarget(sw, sh);
      const prevFbo = gl.getParameter(gl.FRAMEBUFFER_BINDING);
      const prevVp = gl.getParameter(gl.VIEWPORT);
      const prevProg = gl.getParameter(gl.CURRENT_PROGRAM);
      let prevVao;
      if (typeof gl.bindVertexArray === 'function') {
        prevVao = gl.getParameter(gl.VERTEX_ARRAY_BINDING);
        gl.bindVertexArray(null);
      }
      gl.bindFramebuffer(gl.FRAMEBUFFER, this.fbo);
      gl.viewport(0, 0, this.w, this.h);
      gl.disable(gl.DEPTH_TEST); gl.disable(gl.BLEND); gl.disable(gl.SCISSOR_TEST);
      gl.disable(gl.CULL_FACE); gl.disable(gl.STENCIL_TEST);
      gl.colorMask(true, true, true, true);
      gl.useProgram(this.prog);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, texture);
      gl.uniform1i(this.uTex, 0);
      gl.uniform1f(this.uFlip, this.flipY ? 1 : 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.vbo);
      gl.enableVertexAttribArray(this.aPos);
      gl.vertexAttribPointer(this.aPos, 2, gl.FLOAT, false, 0, 0);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      gl.readPixels(0, 0, this.w, this.h, gl.RGBA, gl.UNSIGNED_BYTE, this.pixels);
      gl.bindTexture(gl.TEXTURE_2D, null);
      gl.disableVertexAttribArray(this.aPos);
      if (prevVao !== undefined) gl.bindVertexArray(prevVao);
      gl.useProgram(prevProg);
      gl.bindFramebuffer(gl.FRAMEBUFFER, prevFbo);
      gl.viewport(prevVp[0], prevVp[1], prevVp[2], prevVp[3]);
      const ms = (typeof performance !== 'undefined' ? performance.now() : Date.now()) - t0;
      this.count++; this.lastMs = ms; if (ms > this.maxMs) this.maxMs = ms;
      this.lastError = null;
      return { width: this.w, height: this.h, ms };
    } catch (e) {
      this.fails++; this.lastError = String(e && e.message || e);
      throw e;
    }
  }

  /** JPEG of the last grab. Resolves null if the canvas path is unavailable. */
  encode() {
    if (!this.pixels || typeof document === 'undefined') return Promise.resolve(null);
    if (!this.canvas) {
      this.canvas = document.createElement('canvas');
      this.ctx = this.canvas.getContext('2d');
    }
    if (this.canvas.width !== this.w || this.canvas.height !== this.h) {
      this.canvas.width = this.w; this.canvas.height = this.h;
    }
    const img = new ImageData(new Uint8ClampedArray(this.pixels.buffer, 0, this.w * this.h * 4), this.w, this.h);
    this.ctx.putImageData(img, 0, 0);
    return new Promise(resolve => {
      try {
        this.canvas.toBlob(b => { this.lastBytes = b ? b.size : 0; resolve(b); }, 'image/jpeg', this.quality);
      } catch (e) { resolve(null); }
    });
  }

  stats() {
    return { captures: this.count, fails: this.fails, lastMs: +this.lastMs.toFixed(1),
             maxMs: +this.maxMs.toFixed(1), lastBytes: this.lastBytes,
             size: this.w ? `${this.w}x${this.h}` : null, error: this.lastError };
  }
}
