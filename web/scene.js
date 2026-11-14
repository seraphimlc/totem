/* 图腾 · vector drawing surface. Pan/zoom only; edits come from chat. */
(function (global) {
  "use strict";

  const NS = "http://www.w3.org/2000/svg";
  const INK = "#1C2B33";
  const PAPER = "#FFFFFF";

  function svgEl(name, attrs) {
    const el = document.createElementNS(NS, name);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v != null && v !== false) el.setAttribute(k, String(v));
      }
    }
    return el;
  }

  function numPair(p, i) {
    return [p[i], p[i + 1]];
  }


  function insertWorldBBox(rec, symbols) {
    if (!rec) return null;
    if (rec.bbox && rec.bbox.length === 4 && (rec.t === "cover" || rec.t === "poly")) {
      return rec.bbox.slice();
    }
    let x = rec.x, y = rec.y;
    if (x == null || y == null) {
      if (!rec.xy || rec.xy.length < 2) return null;
      x = rec.xy[0];
      y = rec.xy[1];
    }
    x = Number(x);
    y = Number(y);
    if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
    let sx = 1, sy = 1;
    if (Array.isArray(rec.scale) && rec.scale.length) {
      sx = Number(rec.scale[0]) || 1;
      sy = rec.scale.length > 1 ? Number(rec.scale[1]) || sx : sx;
    }
    if (rec.sx != null) sx = Number(rec.sx) || sx;
    if (rec.sy != null) sy = Number(rec.sy) || sy;
    const rot = Number(rec.rot || 0);
    const key = rec.sym || rec.block || "";
    const sym = (symbols && (symbols[key] || Object.values(symbols).find((s) => s && s.name === rec.block))) || {};
    const bb = sym.bbox;
    if (bb && bb.length === 4) {
      const corners = [
        [bb[0] * sx, bb[1] * sy],
        [bb[2] * sx, bb[1] * sy],
        [bb[2] * sx, bb[3] * sy],
        [bb[0] * sx, bb[3] * sy],
      ];
      const rad = (rot * Math.PI) / 180;
      const c = Math.cos(rad), s = Math.sin(rad);
      const xs = [], ys = [];
      for (const [lx, ly] of corners) {
        xs.push(x + lx * c - ly * s);
        ys.push(y + lx * s + ly * c);
      }
      return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
    }
    const ext = 800;
    return [x - ext, y - ext, x + ext, y + ext];
  }

  function suggestedFitBBox(patch, scene) {
    const boxes = [];
    if (patch && patch.fit_bbox && patch.fit_bbox.length === 4) boxes.push(patch.fit_bbox);
    else if (patch && patch.dirty_bbox && patch.dirty_bbox.length === 4) boxes.push(patch.dirty_bbox);
    const symbols = Object.assign({}, (scene && scene.symbols) || {}, (patch && patch.symbols) || {});
    const recs = [].concat((patch && patch.inserts) || [], (patch && patch.covers) || []);
    recs.forEach((rec) => {
      if (rec && rec.bbox && rec.bbox.length === 4) boxes.push(rec.bbox);
      else {
        const bb = insertWorldBBox(rec, symbols);
        if (bb) boxes.push(bb);
      }
    });
    if (!boxes.length) return null;
    let x0 = boxes[0][0], y0 = boxes[0][1], x1 = boxes[0][2], y1 = boxes[0][3];
    boxes.forEach((b) => {
      x0 = Math.min(x0, b[0]);
      y0 = Math.min(y0, b[1]);
      x1 = Math.max(x1, b[2]);
      y1 = Math.max(y1, b[3]);
    });
    const w = Math.max(x1 - x0, 1);
    const h = Math.max(y1 - y0, 1);
    let pad = Math.max(w, h) * 0.15;
    if (w < 1800) pad = Math.max(pad, (1800 - w) / 2);
    if (h < 1800) pad = Math.max(pad, (1800 - h) / 2);
    return [x0 - pad, y0 - pad, x1 + pad, y1 + pad];
  }

  function appendShape(parent, item) {
    if (!item || !item.t) return null;
    const color = item.c || INK;
    if (item.t === "cover" || (item.t === "poly" && item.fill)) {
      const p = item.p || [];
      if (p.length < 6) return null;
      const pts = [];
      for (let i = 0; i < p.length - 1; i += 2) pts.push(`${p[i]},${p[i + 1]}`);
      const el = svgEl("polygon", {
        points: pts.join(" "),
        fill: item.t === "cover" ? PAPER : color,
        stroke: "none",
        "data-id": item.id || "",
      });
      parent.appendChild(el);
      return el;
    }
    if (item.t === "path") {
      const el = svgEl("path", { d: item.d, "data-id": item.id || "" });
      if (item.fill) {
        el.setAttribute("fill", color);
        el.setAttribute("fill-rule", "evenodd");
        el.setAttribute("stroke", "none");
      } else {
        el.setAttribute("fill", "none");
        el.setAttribute("stroke", color);
        el.setAttribute("stroke-width", item.w && item.w > 0.4 ? item.w : 0.9);
        el.setAttribute("vector-effect", "non-scaling-stroke");
        el.setAttribute("stroke-linecap", "round");
        el.setAttribute("stroke-linejoin", "round");
      }
      parent.appendChild(el);
      return el;
    }
    if (item.t === "lines") {
      const p = item.p || [];
      const g = svgEl("g", { "data-id": item.id || "" });
      for (let i = 0; i < p.length - 3; i += 4) {
        const line = svgEl("line", {
          x1: p[i],
          y1: p[i + 1],
          x2: p[i + 2],
          y2: p[i + 3],
          stroke: color,
          fill: "none",
          "stroke-width": item.w && item.w > 0.4 ? item.w : 0.9,
          "vector-effect": "non-scaling-stroke",
          "stroke-linecap": "round",
        });
        g.appendChild(line);
      }
      parent.appendChild(g);
      return g;
    }
    if (item.t === "pt") {
      const el = svgEl("circle", {
        cx: item.x,
        cy: item.y,
        r: 8,
        fill: color,
        "data-id": item.id || "",
      });
      parent.appendChild(el);
      return el;
    }
    if (item.t === "text") {
      const g = svgEl("g", {
        transform: `translate(${item.x} ${item.y}) rotate(${item.r || 0}) scale(1 -1)`,
        "data-id": item.id || "",
      });
      const t = svgEl("text", {
        x: 0,
        y: 0,
        fill: color,
        "font-size": item.h || 175,
        "font-family": '"Noto Sans SC", Syne, sans-serif',
        "text-anchor": item.a || "start",
        "dominant-baseline": "alphabetic",
      });
      t.textContent = item.s || "";
      g.appendChild(t);
      parent.appendChild(g);
      return g;
    }
    if (item.t === "insert") {
      const sx = item.sx == null ? 1 : item.sx;
      const sy = item.sy == null ? 1 : item.sy;
      const rot = item.rot || 0;
      const el = svgEl("use", {
        href: `#${item.sym}`,
        transform: `translate(${item.x} ${item.y}) rotate(${rot}) scale(${sx} ${sy})`,
        "data-id": item.id || "",
        "data-block": item.block || "",
      });
      parent.appendChild(el);
      return el;
    }
    return null;
  }

  function fillGroup(group, items) {
    group.textContent = "";
    (items || []).forEach((it) => appendShape(group, it));
  }

  function writeSymbols(defs, symbols) {
    Object.values(symbols || {}).forEach((sym) => {
      if (!sym || !sym.key) return;
      const prev = defs.querySelector(`#${cssEscape(sym.key)}`);
      if (prev) prev.remove();
      const g = svgEl("g", { id: sym.key, "data-block": sym.name || "" });
      fillGroup(g, sym.items || []);
      defs.appendChild(g);
    });
  }

  function cssEscape(id) {
    if (global.CSS && CSS.escape) return CSS.escape(id);
    return String(id).replace(/[^a-zA-Z0-9_-]/g, "\\$&");
  }

  function hideById(root, id) {
    if (!id || !root) return;
    root.querySelectorAll(`[data-id="${id}"]`).forEach((el) => {
      el.setAttribute("visibility", "hidden");
      el.setAttribute("data-hidden", "1");
    });
  }

  const api = {
    stage: null,
    view: null,
    svg: null,
    defs: null,
    world: null,
    base: null,
    patchLayer: null,
    stamp: null,
    scene: null,
    mode: "scene",
    viewBox: null,
    fitted: null,
    dragging: false,
    lastX: 0,
    lastY: 0,
    _bound: false,

    mount(stage) {
      this.stage = stage;
      this.view = document.getElementById("scene-view");
      this.svg = document.getElementById("scene-svg");
      this.defs = document.getElementById("scene-defs");
      this.world = document.getElementById("scene-world");
      this.base = document.getElementById("scene-base");
      this.patchLayer = document.getElementById("scene-patch");
      this.stamp = document.getElementById("scene-stamp");
      if (!this.view || !this.svg) return this;
      this._bind();
      return this;
    },

    _bind() {
      if (this._bound || !this.view) return;
      this._bound = true;
      this.view.addEventListener(
        "wheel",
        (ev) => {
          if (!this.isActive()) return;
          ev.preventDefault();
          ev.stopPropagation();
          const factor = ev.deltaY < 0 ? 1.12 : 1 / 1.12;
          this.zoomBy(factor, ev.clientX, ev.clientY);
        },
        { passive: false }
      );
      this.view.addEventListener("pointerdown", (ev) => {
        if (!this.isActive() || ev.button !== 0) return;
        this.dragging = true;
        this.lastX = ev.clientX;
        this.lastY = ev.clientY;
        if (this.stage) this.stage.classList.add("is-panning", "is-zoomed");
        try {
          ev.currentTarget.setPointerCapture(ev.pointerId);
        } catch (_) {}
        ev.preventDefault();
        ev.stopPropagation();
      });
      this.view.addEventListener("pointermove", (ev) => {
        if (!this.dragging) return;
        this.panBy(ev.clientX - this.lastX, ev.clientY - this.lastY);
        this.lastX = ev.clientX;
        this.lastY = ev.clientY;
      });
      const endPan = () => {
        this.dragging = false;
        if (this.stage) this.stage.classList.remove("is-panning");
      };
      this.view.addEventListener("pointerup", endPan);
      this.view.addEventListener("pointercancel", endPan);
      this.view.addEventListener("dblclick", (ev) => {
        if (!this.isActive()) return;
        ev.preventDefault();
        ev.stopPropagation();
        this.fit();
      });
    },

    hasScene() {
      return !!(this.scene && this.scene.items);
    },

    isActive() {
      return this.mode === "scene" && this.hasScene() && this.view && !this.view.hidden;
    },

    load(scene, { keepView = false, pending, stamp, activate = true } = {}) {
      if (!this.svg) this.mount(document.getElementById("preview-stage"));
      this.scene = scene;
      if (!scene) {
        this.clear();
        return;
      }
      writeSymbols(this.defs, scene.symbols || {});
      fillGroup(this.base, scene.items || []);
      this.patchLayer.textContent = "";
      const isPending = pending != null ? pending : !!scene.pending;
      this.setStamp(isPending ? stamp || scene.stamp : null);
      const bb = scene.bbox;
      if (bb && bb.length === 4 && !keepView) this.fitTo(bb);
      if (activate !== false) this.show();
    },

    applyPatch(patch, { scene, pending, stamp, fitBbox } = {}) {
      if (!this.svg) this.mount(document.getElementById("preview-stage"));
      const isPending = pending == null ? !!(scene && scene.pending) || pending !== false : pending !== false;
      // Prefer the full post-apply scene when the server sent one, even if an older
      // scene (including a previous pending plan) is already on screen.
      if (scene && Array.isArray(scene.items)) {
        this.load(scene, {
          keepView: true,
          pending: isPending || !!scene.pending,
          stamp: stamp || scene.stamp,
        });
        this.fitChange(fitBbox || suggestedFitBBox(patch, scene) || (patch && patch.dirty_bbox));
        this.show();
        return;
      }
      if (!this.hasScene()) return;
      if (!this.base) return;
      writeSymbols(this.defs, (patch && patch.symbols) || {});
      (patch && patch.hide ? patch.hide : []).forEach((id) => hideById(this.world, id));
      this.patchLayer.textContent = "";
      (patch && patch.covers ? patch.covers : []).forEach((it) => appendShape(this.patchLayer, it));
      (patch && patch.inserts ? patch.inserts : []).forEach((it) => appendShape(this.patchLayer, it));
      (patch && patch.items ? patch.items : []).forEach((it) => appendShape(this.patchLayer, it));
      this.setStamp(pending === false ? null : stamp || (patch && patch.stamp) || "方案预览，尚未写入工作图");
      if (this.scene) this.scene.pending = pending !== false;
      this.fitChange(fitBbox || suggestedFitBBox(patch, this.scene) || (patch && patch.dirty_bbox));
      this.show();
    },

    fitChange(bbox) {
      const box = bbox && bbox.length === 4 ? bbox : null;
      if (!box) return;
      this.fitTo(box);
    },

    setStamp(text) {
      if (!this.stamp) return;
      if (text) {
        this.stamp.textContent = text;
        this.stamp.hidden = false;
      } else {
        this.stamp.hidden = true;
        this.stamp.textContent = "";
      }
    },

    clearPatch() {
      if (this.patchLayer) this.patchLayer.textContent = "";
      if (this.world) {
        this.world.querySelectorAll('[data-hidden="1"]').forEach((el) => {
          el.removeAttribute("visibility");
          el.removeAttribute("data-hidden");
        });
      }
      this.setStamp(null);
      if (this.scene) this.scene.pending = false;
    },

    show() {
      this.mode = "scene";
      if (this.view) this.view.hidden = false;
      const img = document.getElementById("preview-image");
      if (img) img.hidden = true;
      const empty = document.getElementById("preview-empty");
      if (empty) empty.hidden = true;
      this._syncToggle();
    },

    hide() {
      if (this.view) this.view.hidden = true;
    },

    showImageFallback() {
      this.mode = "image";
      if (this.view) this.view.hidden = true;
      const img = document.getElementById("preview-image");
      if (img && img.getAttribute("src")) img.hidden = false;
      this._syncToggle();
    },

    preferScene() {
      this.mode = "scene";
      if (this.hasScene()) this.show();
      this._syncToggle();
    },

    _syncToggle() {
      const sceneBtn = document.getElementById("view-scene-btn");
      const imageBtn = document.getElementById("view-image-btn");
      if (sceneBtn) sceneBtn.classList.toggle("is-on", this.mode === "scene" && this.hasScene());
      if (imageBtn) imageBtn.classList.toggle("is-on", this.mode === "image");
    },

    clear() {
      this.scene = null;
      if (this.defs) this.defs.textContent = "";
      if (this.base) this.base.textContent = "";
      if (this.patchLayer) this.patchLayer.textContent = "";
      this.setStamp(null);
      this.hide();
      this._syncToggle();
    },

    fitTo(bbox) {
      const x0 = bbox[0],
        y0 = bbox[1],
        x1 = bbox[2],
        y1 = bbox[3];
      const w = Math.max(x1 - x0, 1);
      const h = Math.max(y1 - y0, 1);
      const pad = Math.max(w, h) * 0.02;
      this.fitted = [x0 - pad, y0 - pad, w + pad * 2, h + pad * 2];
      this.viewBox = this.fitted.slice();
      this._applyView();
    },

    fit() {
      if (this.fitted) {
        this.viewBox = this.fitted.slice();
        this._applyView();
      } else if (this.scene && this.scene.bbox) {
        this.fitTo(this.scene.bbox);
      }
    },

    _applyView() {
      if (!this.svg || !this.viewBox || !this.world) return;
      const [x, y, w, h] = this.viewBox;
      this.svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
      this.svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
      this.world.setAttribute(
        "transform",
        `translate(0 ${h}) scale(1 -1) translate(${-x} ${-y})`
      );
      if (this.stage) this.stage.classList.toggle("is-zoomed", this._zoomLevel() > 1.02);
    },

    _zoomLevel() {
      if (!this.viewBox || !this.fitted) return 1;
      return this.fitted[2] / Math.max(this.viewBox[2], 1);
    },

    zoomBy(factor, clientX, clientY) {
      if (!this.viewBox || !this.svg) return;
      const [x, y, w, h] = this.viewBox;
      const nextW = w / factor;
      const nextH = h / factor;
      const minW = (this.fitted ? this.fitted[2] : w) / 12;
      const maxW = (this.fitted ? this.fitted[2] : w) * 4;
      if (nextW < minW || nextW > maxW) {
        if (factor < 1 && nextW > maxW) this.fit();
        return;
      }
      const rect = this.svg.getBoundingClientRect();
      const fx = rect.width ? ((clientX == null ? rect.left + rect.width / 2 : clientX) - rect.left) / rect.width : 0.5;
      const fy = rect.height ? ((clientY == null ? rect.top + rect.height / 2 : clientY) - rect.top) / rect.height : 0.5;
      // viewBox y grows downward in the svg mapping, but world y is flipped.
      const newX = x + w * fx - nextW * fx;
      const newY = y + h * (1 - fy) - nextH * (1 - fy);
      this.viewBox = [newX, newY, nextW, nextH];
      this._applyView();
    },

    panBy(dxPx, dyPx) {
      if (!this.viewBox || !this.svg) return;
      const rect = this.svg.getBoundingClientRect();
      const [x, y, w, h] = this.viewBox;
      const sx = rect.width ? w / rect.width : 1;
      const sy = rect.height ? h / rect.height : 1;
      this.viewBox = [x - dxPx * sx, y + dyPx * sy, w, h];
      this._applyView();
    },

    zoomLabel() {
      return `${Math.round(this._zoomLevel() * 100)}%`;
    },
  };

  global.TutanScene = api;
})(window);
