(() => {
  const LAST_KEY = "tuteng:last-session";
  const state = {
    user: null,
    projects: [],
    project: null,
    regions: [],
    regionId: null,
    model: "Tifa-6.1-sol",
    sessionGen: 0,
    viewGen: 0,
    chats: {},
    sessionAbort: null,
    init: null,
    apply: null,
    previewSource: "work",
    previewPending: false,
    pdfBusy: false,
    pollTimer: null,
    listTimer: null,
    sceneAbort: null,
    creating: false,
    zoom: { scale: 1, x: 0, y: 0, dragging: false, lastX: 0, lastY: 0 },
  };

  const $ = (id) => document.getElementById(id);

  async function api(path, options = {}) {
    const opts = { credentials: "same-origin", ...options };
    opts.headers = { ...(options.headers || {}) };
    const res = await fetch(path, opts);
    const ctype = res.headers.get("content-type") || "";
    const body = ctype.includes("application/json") ? await res.json() : await res.text();
    if (!res.ok) {
      const detail = (body && body.detail) || (typeof body === "string" && body) || `请求失败（${res.status}）`;
      const err = new Error(detail);
      err.status = res.status;
      err.body = body;
      throw err;
    }
    return body;
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function showLogin(errorText) {
    stopPolling();
    $("workbench").hidden = true;
    $("login-screen").hidden = false;
    const box = $("login-error");
    if (errorText) {
      box.hidden = false;
      box.textContent = errorText;
    } else {
      box.hidden = true;
      box.textContent = "";
    }
    $("login-username").focus();
  }

  function showWorkbench() {
    $("login-screen").hidden = true;
    $("workbench").hidden = false;
    $("who").textContent = state.user || "";
  }

  function setPane({ upload = false, init = false, chat = false } = {}) {
    $("upload-panel").hidden = !upload;
    $("init-panel").hidden = !init;
    $("chat-log").hidden = !chat;
    $("chat-form").hidden = !chat;
    if (!chat) {
      $("preview-image").hidden = true;
      clearScene();
    }
  }

  function showIdle() {
    state.project = null;
    state.regions = [];
    state.regionId = null;
    state.init = null;
    state.apply = null;
    state.creating = false;
    $("current-project-name").textContent = "未选择会话";
    $("current-region-name").textContent = "";
    $("preview-empty").hidden = false;
    $("preview-empty").textContent = "新建会话并上传图纸，或从左侧切换到一个已有会话。";
    $("preview-error").hidden = true;
    $("preview-loading").hidden = true;
    $("preview-image").hidden = true;
    clearScene();
    $("region-select").innerHTML = "";
    $("view-work-btn").hidden = true;
    $("view-official-btn").hidden = true;
    const vs = $("view-scene-btn");
    const vi = $("view-image-btn");
    if (vs) vs.hidden = true;
    if (vi) vi.hidden = true;
    $("pdf-btn").hidden = true;
    $("zoom-controls").hidden = true;
    resetZoom();
    setPane({ upload: true, init: false, chat: false });
    renderProjects();
  }

  $("login-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const username = $("login-username").value.trim();
    const password = $("login-password").value;
    const btn = $("login-submit");
    btn.disabled = true;
    try {
      const data = await api("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      state.user = data.username;
      $("login-password").value = "";
      showWorkbench();
      await bootWorkbench();
    } catch (err) {
      showLogin(err.message || "登录没有成功，请再试一次。");
    } finally {
      btn.disabled = false;
    }
  });

  $("logout-btn").addEventListener("click", async () => {
    try {
      await api("/api/logout", { method: "POST" });
    } catch (_) {}
    state.user = null;
    state.project = null;
    showLogin("");
  });

  document.querySelectorAll(".model-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.model = btn.dataset.model;
      document.querySelectorAll(".model-btn").forEach((b) => {
        const on = b === btn;
        b.classList.toggle("is-on", on);
        b.setAttribute("aria-checked", on ? "true" : "false");
      });
    });
  });

  function sessionMeta(p) {
    if (!p.selectable && !p.available) return { text: p.missing || "图纸未就绪", cls: "" };
    if (p.ready) return { text: "可以对话", cls: "is-ready" };
    const st = (p.init && p.init.state) || "";
    if (st === "error") return { text: p.init.message || "准备失败", cls: "" };
    if (st === "running" || st === "pending") return { text: (p.init && p.init.message) || "正在准备", cls: "is-prep" };
    return { text: "正在准备", cls: "is-prep" };
  }

  function renderProjects() {
    const ul = $("project-list");
    ul.innerHTML = "";
    if (!state.projects.length) {
      const li = document.createElement("li");
      li.innerHTML = '<p class="rail-hint">还没有会话。点击上方「新建会话」上传图纸。</p>';
      ul.appendChild(li);
      return;
    }
    for (const p of state.projects) {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      const meta = sessionMeta(p);
      btn.className = [
        state.project && state.project.id === p.id ? "is-on" : "",
        p.selectable || p.available ? "" : "punavail",
        !p.ready && (p.selectable || p.available) ? "is-prep" : "",
      ]
        .filter(Boolean)
        .join(" ");
      const spin = meta.cls === "is-prep" ? '<span class="spin" aria-hidden="true"></span>' : "";
      btn.innerHTML = `<span class="pname">${escapeHtml(p.name)}</span><span class="pmeta ${meta.cls}">${spin}${escapeHtml(meta.text)}</span>`;
      btn.disabled = !(p.selectable || p.available);
      btn.addEventListener("click", () => selectProject(p.id));
      li.appendChild(btn);
      ul.appendChild(li);
    }
  }

  function fillRegions() {
    const sel = $("region-select");
    sel.innerHTML = "";
    const groups = [
      { kind: "sheet", label: "图幅" },
      { kind: "subview", label: "分区" },
      { kind: "room", label: "房间" },
    ];
    for (const g of groups) {
      const items = state.regions.filter((r) => r.kind === g.kind);
      if (!items.length) continue;
      const og = document.createElement("optgroup");
      og.label = g.label;
      for (const r of items) {
        const opt = document.createElement("option");
        opt.value = r.id;
        const floor = r.floor ? `（${r.floor}）` : "";
        const sub = r.kind !== "sheet" && r.subtitle ? `（${r.subtitle}）` : "";
        opt.textContent = `${r.title}${floor}${sub}`;
        og.appendChild(opt);
      }
      sel.appendChild(og);
    }
    if (state.regionId) sel.value = state.regionId;
  }

  function currentRegion() {
    return state.regions.find((r) => r.id === state.regionId) || null;
  }

  function updateRegionLabel() {
    const r = currentRegion();
    $("current-region-name").textContent = r
      ? [r.subtitle, r.title].filter(Boolean).filter((v, i, a) => a.indexOf(v) === i).join(" / ")
      : "";
    $("current-project-name").textContent = state.project ? state.project.name : "未选择会话";
  }

  function previewUrl(overlay) {
    if (!state.project) return "";
    const q = new URLSearchParams();
    if (state.regionId) q.set("region", state.regionId);
    if (overlay) q.set("overlay", overlay);
    if (state.previewSource === "official") q.set("source", "official");
    return `/api/projects/${encodeURIComponent(state.project.id)}/preview?${q.toString()}`;
  }

  function pendingOverlay() {
    const pending = state.apply && state.apply.pending_plan;
    if (!pending || !pending.plan) return null;
    if (pending.region && state.regionId && pending.region !== state.regionId) return null;
    return pending.message_id || null;
  }

  function resetZoom() {

    state.zoom.scale = 1;
    state.zoom.x = 0;
    state.zoom.y = 0;
    state.zoom.dragging = false;
    applyZoom();
  }

  function applyZoom() {
    const img = $("preview-image");
    const stage = $("preview-stage");
    const z = state.zoom;
    if (img) img.style.transform = `translate(${z.x}px, ${z.y}px) scale(${z.scale})`;
    if (stage) {
      stage.classList.toggle("is-zoomed", z.scale > 1.02);
      stage.classList.toggle("is-panning", !!z.dragging);
    }
    const label = $("zoom-label");
    if (label) label.textContent = `${Math.round(z.scale * 100)}%`;
  }

  function zoomBy(factor, clientX, clientY) {
    const img = $("preview-image");
    if (!img || img.hidden) return;
    const z = state.zoom;
    const next = Math.min(8, Math.max(1, z.scale * factor));
    if (Math.abs(next - z.scale) < 0.001) {
      if (next === 1) resetZoom();
      return;
    }
    if (next === 1) {
      resetZoom();
      return;
    }
    const rect = img.getBoundingClientRect();
    const cx = clientX == null ? rect.left + rect.width / 2 : clientX;
    const cy = clientY == null ? rect.top + rect.height / 2 : clientY;
    const ratioX = rect.width ? (cx - rect.left) / rect.width : 0.5;
    const ratioY = rect.height ? (cy - rect.top) / rect.height : 0.5;
    z.scale = next;
    applyZoom();
    const after = img.getBoundingClientRect();
    z.x += cx - (after.left + ratioX * after.width);
    z.y += cy - (after.top + ratioY * after.height);
    applyZoom();
  }

  function setPreviewLoading(on, text) {
    const loading = $("preview-loading");
    const stage = $("preview-stage");
    if (text) loading.textContent = text;
    loading.hidden = !on;
    if (stage) stage.classList.toggle("is-waiting", !!on);
  }

  function beginViewChange(loadingText) {
    state.viewGen += 1;
    if (state.sceneAbort) {
      try { state.sceneAbort.abort(); } catch (_) {}
      state.sceneAbort = null;
    }
    const img = $("preview-image");
    if (img) {
      const prev = img.dataset.objurl;
      img.removeAttribute("src");
      img.hidden = true;
      if (prev) {
        URL.revokeObjectURL(prev);
        delete img.dataset.objurl;
      }
    }
    clearScene();
    resetZoom();
    const err = $("preview-error");
    const empty = $("preview-empty");
    if (err) err.hidden = true;
    if (empty) empty.hidden = true;
    if (loadingText) setPreviewLoading(true, loadingText);
    return state.viewGen;
  }

  function viewIsCurrent(gen, projectId, regionId) {
    return (
      gen === state.viewGen &&
      state.project &&
      state.project.id === projectId &&
      state.regionId === regionId
    );
  }

  function showPreviewImage() {
    const s = sceneApi();
    if (s && s.showImageFallback) s.showImageFallback();
    else {
      const img = $("preview-image");
      if (img && img.getAttribute("src")) img.hidden = false;
      if (s && s.hide) s.hide();
    }
  }

  async function loadPreview(overlay, { keepZoom = false, loadingText = "正在出图…", quiet = false, show = false } = {}) {
    if (!state.project || !isReady()) {
      if (!quiet) setPreviewLoading(false);
      return;
    }
    const gen = state.viewGen;
    const projectId = state.project.id;
    const regionId = state.regionId;
    if (!keepZoom) resetZoom();
    const img = $("preview-image");
    const err = $("preview-error");
    const empty = $("preview-empty");
    err.hidden = true;
    empty.hidden = true;
    // Keep an already-updated vector scene only when we are not explicitly showing the image
    // (for example a later crop of the same plan). Session/region switches pass show=true.
    const keepScene = !show && sceneActive();
    if (!quiet && !keepScene) setPreviewLoading(true, loadingText);
    const url = previewUrl(overlay);
    try {
      const res = await fetch(url, { credentials: "same-origin" });
      if (!viewIsCurrent(gen, projectId, regionId)) return;
      if (!res.ok) {
        let detail = "这张图没能画出来。请换一个分区后再试。";
        try {
          const body = await res.json();
          if (body.detail) detail = body.detail;
        } catch (_) {}
        throw new Error(detail);
      }
      const blob = await res.blob();
      if (!viewIsCurrent(gen, projectId, regionId)) return;
      const obj = URL.createObjectURL(blob);
      const prev = img.dataset.objurl;
      img.src = obj;
      img.dataset.objurl = obj;
      if (prev && prev !== obj) URL.revokeObjectURL(prev);
      if (show) showPreviewImage();
      else img.hidden = sceneActive() || keepScene;
    } catch (e) {
      if (!viewIsCurrent(gen, projectId, regionId)) return;
      if (show || (!sceneActive() && !keepScene)) {
        img.hidden = true;
        err.hidden = false;
        err.textContent = e.message || "预览失败。";
      } else {
        console.warn("crop preview failed; vector scene kept", e);
      }
    } finally {
      if (viewIsCurrent(gen, projectId, regionId) && !quiet && !keepScene) setPreviewLoading(false);
    }
  }

  // --- scene view hook (vector drawing). Most logic lives in web/scene.js. ---
  function sceneApi() {
    return window.TutanScene || null;
  }

  function sceneActive() {
    const s = sceneApi();
    return !!(s && s.isActive());
  }

  function sceneUrl(extra) {
    if (!state.project) return "";
    const q = new URLSearchParams();
    if (state.regionId) q.set("region", state.regionId);
    if (state.previewSource === "official") q.set("source", "official");
    if (extra && extra.pending) q.set("pending", "1");
    return `/api/projects/${encodeURIComponent(state.project.id)}/scene?${q.toString()}`;
  }

  function mountScene() {
    const s = sceneApi();
    if (s) s.mount($("preview-stage"));
    return s;
  }

  async function loadScene({ pending = false, keepView = false, loadingText = "正在加载图面…", activate = true } = {}) {
    if (!state.project || !isReady()) return false;
    const gen = state.viewGen;
    const projectId = state.project.id;
    const regionId = state.regionId;
    const s = mountScene();
    if (!s) return false;
    const err = $("preview-error");
    const empty = $("preview-empty");
    if (err) err.hidden = true;
    if (empty) empty.hidden = true;
    if (activate) setPreviewLoading(true, loadingText);
    if (state.sceneAbort) {
      try { state.sceneAbort.abort(); } catch (_) {}
    }
    const ac = new AbortController();
    state.sceneAbort = ac;
    try {
      const data = await api(sceneUrl({ pending }), { signal: ac.signal });
      if (state.sceneAbort === ac) state.sceneAbort = null;
      if (!viewIsCurrent(gen, projectId, regionId)) return false;
      s.load(data, { keepView, pending: !!data.pending, stamp: data.stamp, activate });
      if ($("zoom-label") && s.isActive()) $("zoom-label").textContent = s.zoomLabel();
      if (activate) setPreviewLoading(false);
      return true;
    } catch (e) {
      if (state.sceneAbort === ac) state.sceneAbort = null;
      if (e && e.name === "AbortError") {
        if (activate && viewIsCurrent(gen, projectId, regionId)) setPreviewLoading(false);
        return false;
      }
      if (!viewIsCurrent(gen, projectId, regionId)) return false;
      if (activate) setPreviewLoading(false);
      if (!s.hasScene()) {
        // Keep the PNG path as the fallback; do not surface a hard error if the image can load.
        console.warn("scene load failed", e);
      }
      return false;
    }
  }

  function applySceneEvent(ev) {
    const s = mountScene();
    if (!s) return false;
    const empty = $("preview-empty");
    if (empty) empty.hidden = true;
    if (ev.type === "scene" && ev.scene) {
      const pending = !!ev.pending || !!ev.scene.pending;
      s.load(ev.scene, { keepView: pending, pending, stamp: ev.stamp || ev.scene.stamp });
      if (pending) {
        const fit = ev.fit_bbox || (ev.patch && (ev.patch.fit_bbox || ev.patch.dirty_bbox));
        if (fit) s.fitChange(fit);
      }
      setPreviewLoading(false);
      if ($("zoom-label") && s.isActive()) $("zoom-label").textContent = s.zoomLabel();
      return true;
    }
    if (ev.type === "scene_patch" && (ev.patch || ev.scene)) {
      s.applyPatch(ev.patch || {}, {
        scene: ev.scene,
        pending: ev.pending !== false,
        stamp: ev.stamp,
        fitBbox: ev.fit_bbox || (ev.patch && (ev.patch.fit_bbox || ev.patch.dirty_bbox)),
      });
      setPreviewLoading(false);
      if ($("zoom-label") && s.isActive()) $("zoom-label").textContent = s.zoomLabel();
      return true;
    }
    return false;
  }

  function clearScene() {
    const s = sceneApi();
    if (s) s.clear();
  }
  // --- end scene view hook ---

  $("region-select").addEventListener("change", async () => {
    state.regionId = $("region-select").value;
    updateRegionLabel();
    beginViewChange("正在切换图面…");
    await refreshInventory();
    await loadPreview(pendingOverlay(), { show: true, loadingText: "正在切换图面…" });
    updateExportLinks();
  });

  async function refreshInventory() {
    if (!state.project || !state.regionId || !isReady()) return;
    try {
      const data = await api(
        `/api/projects/${encodeURIComponent(state.project.id)}/region?region=${encodeURIComponent(state.regionId)}`
      );
      if (data.inventory && data.inventory.summary) {
        $("preview-note").textContent = data.inventory.summary;
      }
    } catch (_) {}
  }

  function isReady() {
    return !!(state.project && state.init && state.init.ready);
  }

  function renderInit(st) {
    state.init = st;
    const stepsEl = $("init-steps");
    stepsEl.innerHTML = "";
    const steps = (st && st.steps) || [];
    for (const step of steps) {
      const li = document.createElement("li");
      const running = step.state === "running";
      const mark = step.state === "done" ? "✓" : running ? "" : step.state === "error" ? "!" : "○";
      li.className = step.state === "done" ? "is-done" : running ? "is-running" : step.state === "error" ? "is-error" : "";
      const markClass = running ? "mark spin" : "mark";
      li.innerHTML = `<span class="${markClass}" aria-hidden="true">${mark}</span><span>${escapeHtml(step.label)}</span>`;
      stepsEl.appendChild(li);
    }
    const done = Number(st.done || 0);
    const total = Number(st.total || steps.length || 3);
    const progress = (st && st.progress) || {};
    let percent = Number(progress.percent);
    if (!Number.isFinite(percent)) {
      percent = (done / Math.max(total, 1)) * 100;
    }
    percent = Math.max(0, Math.min(100, percent));
    $("init-bar").style.width = `${percent}%`;
    const barWrap = $("init-progress");
    if (barWrap) barWrap.setAttribute("aria-valuenow", String(Math.round(percent)));
    const cur = Number(progress.current);
    const tot = Number(progress.total);
    const showFrac = Number.isFinite(cur) && Number.isFinite(tot) && tot > 1;
    $("init-percent").textContent = showFrac ? `${Math.round(percent)}% · ${cur}/${tot}` : `${Math.round(percent)}%`;
    $("init-message").textContent = st.message || "正在准备。";
    const running = steps.find((s) => s.state === "running") || steps.find((s) => s.state === "error");
    $("init-title").textContent = (running && running.label) || st.message || "正在准备这张图";
    const err = $("init-error");
    const cont = $("init-continue");
    const retry = $("init-retry");
    const stopped = st.state === "error" || st.state === "interrupted";
    if (st.state === "error") {
      err.hidden = false;
      err.textContent = st.error || st.message || "准备没有成功。";
    } else {
      err.hidden = true;
    }
    if (cont) cont.hidden = !stopped;
    retry.hidden = !stopped;
  }

  function updateApplyUi() {
    const apply = state.apply || {};
    $("apply-btn").hidden = !(apply.pending_plan && apply.pending_plan.plan);
    $("undo-btn").hidden = !apply.can_undo;
    $("download-btn").hidden = !isReady();
    $("pdf-btn").hidden = !isReady();
    $("zoom-controls").hidden = !isReady();
    const vs = $("view-scene-btn");
    const vi = $("view-image-btn");
    if (vs) vs.hidden = !isReady();
    if (vi) vi.hidden = !isReady();
    if (window.TutanScene) window.TutanScene._syncToggle();
    const dirty = !!apply.dirty;
    $("view-work-btn").hidden = !dirty;
    $("view-official-btn").hidden = !dirty;
    $("view-work-btn").classList.toggle("is-on", state.previewSource === "work");
    $("view-official-btn").classList.toggle("is-on", state.previewSource === "official");
    updateExportLinks();
    if (!dirty && !apply.pending_plan) {
      // keep inventory line if present
    } else if (apply.pending_plan) {
      $("preview-note").textContent = state.previewPending
        ? "方案已出，正在生成改后预览…"
        : "方案已出，右侧已是改后效果。确认后写入当前图纸，并可撤回。";
    } else if (dirty) {
      $("preview-note").textContent =
        state.previewSource === "official" ? "正在看改前原图。当前图纸已有改动，可以撤回。" : "正在看当前图纸。可以撤回上一步，也可对照原图。";
    }
  }

  function updateExportLinks() {
    if (!state.project || !isReady()) {
      $("download-btn").removeAttribute("href");
      return;
    }
    const id = encodeURIComponent(state.project.id);
    $("download-btn").href = `/api/projects/${id}/download?source=${state.previewSource === "official" ? "official" : "work"}`;
    $("download-btn").textContent = state.apply && state.apply.dirty ? "下载工作图" : "下载图纸";
  }

  function enterReady() {
    setPane({ upload: false, init: false, chat: true });
    $("chat-input").disabled = false;
    syncComposer(state.project && state.project.id);
    updateApplyUi();
  }

  function enterInit() {
    setPane({ upload: false, init: true, chat: false });
    $("preview-empty").hidden = false;
    $("preview-empty").textContent = "预览会在准备完成后显示。";
    $("preview-image").hidden = true;
    $("preview-error").hidden = true;
    $("preview-loading").hidden = true;
  }

  function stopPolling() {
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  function startPolling() {
    stopPolling();
    state.polls = 0;
    state.pollTimer = setInterval(async () => {
      if (!state.project || isReady()) {
        if (isReady()) stopPolling();
        return;
      }
      try {
        const st = await api(`/api/projects/${encodeURIComponent(state.project.id)}/init`);
        renderInit(st);
        state.polls = (state.polls || 0) + 1;
        if (state.polls % 8 === 0) await refreshProjectList();
        if (st.ready) await onInitReady();
      } catch (_) {}
    }, 400);
  }

  async function onInitReady() {
    stopPolling();
    const id = state.project.id;
    const detail = await api(`/api/projects/${encodeURIComponent(id)}`);
    state.project = detail;
    state.init = detail.init;
    state.apply = detail.apply;
    state.regions = detail.regions || [];
    state.regionId = detail.default_region || (state.regions[0] && state.regions[0].id);
    fillRegions();
    updateRegionLabel();
    enterReady();
    const chat = await api(`/api/projects/${encodeURIComponent(id)}/chat`);
    renderMessages(chat.messages || []);
    if (detail.inventory && detail.inventory.summary) {
      $("preview-note").textContent = detail.inventory.summary;
    }
    beginViewChange("正在准备图面…");
    await loadPreview(pendingOverlay(), { show: true, loadingText: "正在准备图面…" });
    updateApplyUi();
    await refreshProjectList();
  }

  async function refreshProjectList() {
    try {
      const data = await api("/api/projects");
      state.projects = data.projects || [];
      renderProjects();
    } catch (_) {}
  }

  function startListWatch() {
    if (state.listTimer) clearInterval(state.listTimer);
    state.listTimer = setInterval(() => {
      const busy = (state.projects || []).some((p) => p.init && !p.init.ready && p.init.state !== "error");
      if (busy) refreshProjectList();
    }, 4000);
  }

  function cancelInFlightWork() {
    state.sessionGen += 1;
    // Keep in-flight chat POSTs alive so other sessions can finish and be saved
    // even if the user switches mid-reply. Do not abort or overwrite those jobs.
    if (state.sessionAbort) {
      try { state.sessionAbort.abort(); } catch (_) {}
      state.sessionAbort = null;
    }
    if (state.sceneAbort) {
      try { state.sceneAbort.abort(); } catch (_) {}
      state.sceneAbort = null;
    }
    $("chat-submit").disabled = false;
    $("apply-btn").disabled = false;
    $("undo-btn").disabled = false;
  }

  async function selectProject(id, { force = false } = {}) {
    cancelInFlightWork();
    const gen = state.sessionGen;
    const ac = new AbortController();
    state.sessionAbort = ac;
    state.creating = false;
    const listed = (state.projects || []).find((p) => p.id === id);
    if (listed) {
      state.project = Object.assign({}, listed);
      renderProjects();
      $("current-project-name").textContent = listed.name || id;
    }
    beginViewChange("正在切换图面…");
    let startedPreview = false;
    try {
      const detail = await api(`/api/projects/${encodeURIComponent(id)}`, { signal: ac.signal });
      if (gen !== state.sessionGen) return;
      state.project = detail;
      state.init = detail.init;
      state.apply = detail.apply;
      state.regions = detail.regions || [];
      state.regionId = detail.default_region || (state.regions[0] && state.regions[0].id) || null;
      state.previewSource = "work";
      try {
        localStorage.setItem(LAST_KEY, id);
      } catch (_) {}
      renderProjects();
      fillRegions();
      updateRegionLabel();
      if (!detail.ready) {
        enterInit();
        renderInit(detail.init || { message: "开始准备这张图。", steps: [], done: 0, total: 4 });
        startPolling();
        return;
      }
      stopPolling();
      enterReady();
      const chat = await api(`/api/projects/${encodeURIComponent(id)}/chat`, { signal: ac.signal });
      if (gen !== state.sessionGen) return;
      renderMessages(chat.messages || []);
      attachChatDraftIfNeeded(id, chat.messages || []);
      if (detail.inventory && detail.inventory.summary) {
        $("preview-note").textContent = detail.inventory.summary;
      }
      beginViewChange("正在切换图面…");
      startedPreview = true;
      await loadPreview(pendingOverlay(), { show: true, loadingText: "正在切换图面…" });
      updateApplyUi();
    } catch (err) {
      if (gen !== state.sessionGen || err.name === "AbortError") return;
      $("preview-empty").hidden = false;
      $("preview-empty").textContent = err.message || "打不开这个会话。";
      setPreviewLoading(false);
    } finally {
      if (gen === state.sessionGen && !startedPreview && isReady()) setPreviewLoading(false);
    }
  }

  function openNewSession() {
    cancelInFlightWork();
    state.creating = true;
    state.project = null;
    stopPolling();
    renderProjects();
    $("current-project-name").textContent = "新建会话";
    $("current-region-name").textContent = "";
    $("upload-error").hidden = true;
    $("preview-empty").hidden = false;
    $("preview-empty").textContent = "上传完成后，会先建立索引并准备预览。";
    $("preview-image").hidden = true;
    clearScene();
    $("region-select").innerHTML = "";
    $("view-work-btn").hidden = true;
    $("view-official-btn").hidden = true;
    const vs = $("view-scene-btn");
    const vi = $("view-image-btn");
    if (vs) vs.hidden = true;
    if (vi) vi.hidden = true;
    $("pdf-btn").hidden = true;
    $("zoom-controls").hidden = true;
    resetZoom();
    setPane({ upload: true, init: false, chat: false });
    $("upload-input").value = "";
  }

  $("new-session-btn").addEventListener("click", () => {
    openNewSession();
    $("upload-input").click();
  });

  async function requestInit({ force = false } = {}) {
    if (!state.project) return;
    const query = force ? "?force=true" : "";
    try {
      const st = await api(`/api/projects/${encodeURIComponent(state.project.id)}/init${query}`, { method: "POST" });
      renderInit(st);
      startPolling();
    } catch (err) {
      $("init-error").hidden = false;
      $("init-error").textContent = err.message || (force ? "无法重新准备。" : "无法继续准备。");
    }
  }

  $("init-continue").addEventListener("click", () => requestInit({ force: false }));
  $("init-retry").addEventListener("click", () => requestInit({ force: true }));

  async function uploadFile(file) {
    const err = $("upload-error");
    err.hidden = true;
    if (!file) return;
    const name = (file.name || "").toLowerCase();
    if (!name.endsWith(".dwg") && !name.endsWith(".dxf")) {
      err.hidden = false;
      err.textContent = "请上传 DWG 或 DXF 文件。";
      return;
    }
    if (file.size > 80 * 1024 * 1024) {
      err.hidden = false;
      err.textContent = "文件大于 80MB，请先精简后再上传。";
      return;
    }
    const form = new FormData();
    form.append("file", file);
    $("upload-drop").querySelector("span").textContent = "正在上传…";
    try {
      const created = await api("/api/projects/upload", { method: "POST", body: form });
      await refreshProjectList();
      await selectProject(created.id, { force: true });
    } catch (e) {
      err.hidden = false;
      err.textContent = e.message || "上传没有成功，请再试一次。";
      setPane({ upload: true, init: false, chat: false });
    } finally {
      $("upload-drop").querySelector("span").textContent = "把图纸拖到这里，或点击选择文件";
      $("upload-input").value = "";
    }
  }

  $("upload-input").addEventListener("change", (ev) => {
    const file = ev.target.files && ev.target.files[0];
    if (file) uploadFile(file);
  });

  const drop = $("upload-drop");
  ["dragenter", "dragover"].forEach((type) => {
    drop.addEventListener(type, (ev) => {
      ev.preventDefault();
      drop.classList.add("is-over");
    });
  });
  ["dragleave", "drop"].forEach((type) => {
    drop.addEventListener(type, (ev) => {
      ev.preventDefault();
      drop.classList.remove("is-over");
    });
  });
  drop.addEventListener("drop", (ev) => {
    const file = ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files[0];
    if (file) uploadFile(file);
  });

  function chatIsOnScreen(projectId) {
    return !!(state.project && state.project.id === projectId);
  }

  function chatJob(projectId) {
    return (projectId && state.chats[projectId]) || null;
  }

  function isProjectBusy(projectId) {
    const job = chatJob(projectId);
    return !!(job && job.busy);
  }

  function ensureChatJob(projectId) {
    if (!state.chats[projectId]) {
      state.chats[projectId] = { abort: null, draft: null, busy: false, status: "" };
    }
    return state.chats[projectId];
  }

  function clearChatJob(projectId, ac) {
    const job = chatJob(projectId);
    if (!job) return;
    if (ac && job.abort && job.abort !== ac) return;
    job.abort = null;
    job.draft = null;
    job.busy = false;
    job.status = "";
  }

  function syncComposer(projectId) {
    if (!chatIsOnScreen(projectId)) return;
    const busy = isProjectBusy(projectId);
    $("chat-submit").disabled = busy;
    const job = chatJob(projectId);
    if (busy && job && job.status) $("chat-status").textContent = job.status;
  }

  function ensureChatDraft(projectId) {
    const job = ensureChatJob(projectId);
    if (job.draft && job.draft.isConnected) return job.draft;
    const empty = $("chat-empty");
    if (empty) empty.hidden = true;
    const draft = document.createElement("article");
    draft.className = "msg";
    draft.dataset.chatDraft = "1";
    draft.innerHTML = `<div class="msg-meta"><span class="who">图腾</span><span></span></div><div class="msg-body">正在回复…</div>`;
    $("chat-log").appendChild(draft);
    $("chat-log").scrollTop = $("chat-log").scrollHeight;
    job.draft = draft;
    return draft;
  }

  function attachChatDraftIfNeeded(projectId, messages) {
    const job = chatJob(projectId);
    if (!job || (!job.abort && !job.busy)) return;
    const last = messages && messages.length ? messages[messages.length - 1] : null;
    if (last && last.role === "assistant") {
      if (job.status) $("chat-status").textContent = job.status;
      syncComposer(projectId);
      return;
    }
    if (job.abort) ensureChatDraft(projectId);
    $("chat-status").textContent = job.status || "正在回复…";
    syncComposer(projectId);
  }

  function renderMessages(messages) {
    const log = $("chat-log");
    const empty = $("chat-empty");
    log.querySelectorAll(".msg").forEach((n) => n.remove());
    if (!messages || !messages.length) {
      empty.hidden = false;
      return;
    }
    empty.hidden = true;
    for (const m of messages) log.appendChild(messageEl(m));
    log.scrollTop = log.scrollHeight;
  }

  function messageEl(m) {
    const el = document.createElement("article");
    el.className = "msg" + (m.role === "user" ? " is-user" : "");
    el.dataset.id = m.id || "";
    const who = m.role === "user" ? "你" : m.model === "system" ? "图腾" : m.model || "图腾";
    const ts = m.ts ? m.ts.replace("T", " ") : "";
    const bits = planBits(m.plan);
    el.innerHTML = `
      <div class="msg-meta"><span class="who">${escapeHtml(who)}</span><span>${escapeHtml(ts)}</span></div>
      <div class="msg-body">${escapeHtml(m.text || "")}</div>
      ${bits}
    `;
    return el;
  }

  function planHasEdits(plan) {
    if (!plan) return false;
    return ["inserts", "pipes", "texts", "text_edits", "deletes", "replaces", "moves"].some(
      (k) => Array.isArray(plan[k]) && plan[k].length
    );
  }

  function planBits(plan) {
    if (!plan || !planHasEdits(plan)) return "";
    const items = [];
    for (const ins of plan.inserts || []) {
      items.push(`<li>插入 ${escapeHtml(ins.block || "图块")}  @ ${fmtPt(ins.xy)}</li>`);
    }
    for (const d of plan.deletes || []) {
      items.push(`<li class="text">删除 ${escapeHtml(d.block || "图块")}  @ ${fmtPt(d.xy || d.from_xy)}</li>`);
    }
    for (const r of plan.replaces || []) {
      items.push(
        `<li>替换 ${escapeHtml(r.from_block || "原块")} → ${escapeHtml(r.block || "新块")}  @ ${fmtPt(r.from_xy || r.xy)}</li>`
      );
    }
    for (const mv of plan.moves || []) {
      items.push(`<li>移动 ${escapeHtml(mv.block || "图块")}  ${fmtPt(mv.from)} 至 ${fmtPt(mv.to)}</li>`);
    }
    for (const p of plan.pipes || []) {
      items.push(`<li class="pipe">管道 ${escapeHtml(p.layer || "")}  ${fmtPt(p.from)} 至 ${fmtPt(p.to)}</li>`);
    }
    for (const t of plan.texts || []) {
      items.push(`<li class="text">注记 ${escapeHtml(t.text || "")}  @ ${fmtPt(t.xy)}</li>`);
    }
    for (const e of plan.text_edits || []) {
      items.push(
        `<li class="text">改文字 ${escapeHtml(e.from_text || "")} → ${escapeHtml(e.text || "")}  @ ${fmtPt(e.xy)}</li>`
      );
    }
    if (!items.length) return "";
    return `<ul class="plan-bits">${items.join("")}</ul>`;
  }

  function fmtPt(xy) {
    if (!xy || xy.length < 2) return "";
    return `${Number(xy[0]).toFixed(1)}, ${Number(xy[1]).toFixed(1)}`;
  }

  $("chat-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const projectId = state.project && state.project.id;
    if (!isReady() || !projectId || isProjectBusy(projectId)) return;
    const text = $("chat-input").value.trim();
    if (!text) return;
    const job = ensureChatJob(projectId);
    job.busy = true;
    job.status = "正在回复…";
    $("chat-submit").disabled = true;
    $("chat-input").value = "";
    $("chat-empty").hidden = true;
    $("chat-log").appendChild(
      messageEl({ role: "user", text, ts: new Date().toISOString().slice(0, 19).replace("T", " ") })
    );
    const draft = document.createElement("article");
    draft.className = "msg";
    draft.dataset.chatDraft = "1";
    draft.innerHTML = `<div class="msg-meta"><span class="who">图腾</span><span></span></div><div class="msg-body">正在回复…</div>`;
    $("chat-log").appendChild(draft);
    const bodyEl = draft.querySelector(".msg-body");
    $("chat-status").textContent = "正在回复…";
    $("chat-log").scrollTop = $("chat-log").scrollHeight;
    const ac = new AbortController();
    try {
      job.abort = ac;
      job.draft = draft;
      const res = await fetch(`/api/projects/${encodeURIComponent(projectId)}/chat`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, model: state.model, region: state.regionId }),
        signal: ac.signal,
      });
      const onScreen = () => chatIsOnScreen(projectId);
      if (!res.ok) {
        let detail = `请求失败（${res.status}）`;
        try {
          const body = await res.json();
          if (body.detail) detail = body.detail;
        } catch (_) {}
        throw new Error(detail);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      let acc = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const parts = buf.split("\n\n");
        buf = parts.pop();
        for (const part of parts) {
          const line = part.split("\n").find((l) => l.startsWith("data: "));
          if (!line) continue;
          let ev;
          try {
            ev = JSON.parse(line.slice(6));
          } catch (_) {
            continue;
          }
          if (ev.type === "delta" && ev.text) {
            acc += ev.text;
            if (onScreen()) {
              const live = ensureChatDraft(projectId).querySelector(".msg-body");
              if (live) live.textContent = "正在回复…";
              $("chat-log").scrollTop = $("chat-log").scrollHeight;
            }
          } else if (ev.type === "heartbeat") {
            // keep the stream alive while a preview is rendering
          } else if (ev.type === "status") {
            if (onScreen()) $("chat-status").textContent = ev.text;
            if (onScreen() && ev.text && ev.text.indexOf("仍显示当前图纸") >= 0) {
              state.previewPending = false;
              setPreviewLoading(false);
              updateApplyUi();
            }
          } else if (ev.type === "scene" || ev.type === "scene_patch") {
            // Do not auto-render vector scenes from chat; the crop preview follows.
            if (onScreen() && (ev.type === "scene_patch" || ev.pending)) {
              $("chat-status").textContent = "方案已出，右侧是改动区域预览。确认后才会写入当前图纸。";
            }
          } else if (ev.type === "preview" && ev.overlay) {
            state.previewPending = false;
            if (onScreen()) {
              $("chat-status").textContent = "方案已出，右侧是改动区域预览。确认后才会写入当前图纸。";
              try {
                state.apply = await api(`/api/projects/${encodeURIComponent(projectId)}/apply`);
              } catch (_) {}
              updateApplyUi();
              await loadPreview(ev.overlay, {
                keepZoom: false,
                show: true,
                loadingText: "正在加载改动区域预览…",
              });
            }
          } else if (ev.type === "done" && ev.message) {
            if (onScreen()) {
              const live = job.draft && job.draft.isConnected ? job.draft : null;
              const node = messageEl(ev.message);
              const existing = ev.message.id
                ? $("chat-log").querySelector('.msg[data-id="' + ev.message.id + '"]')
                : null;
              if (existing) existing.replaceWith(node);
              else if (live) live.replaceWith(node);
              else $("chat-log").appendChild(node);
            }
            job.draft = null;
            if (onScreen()) {
              if (ev.apply) state.apply = Object.assign({}, state.apply || {}, ev.apply, ev.apply.pending_plan === undefined ? {} : ev.apply);
              if (ev.message.apply) state.apply = Object.assign({}, state.apply || {}, ev.message.apply);
              const waitingPreview = !!(ev.preview_pending || ev.message.preview_pending) && !ev.message.overlay;
              state.previewPending = waitingPreview;
              if (waitingPreview) {
                setPreviewLoading(true, "正在生成改后预览…");
                $("chat-status").textContent = "方案已出，正在生成改后预览…";
              } else if (ev.message.overlay) {
                await loadPreview(ev.message.overlay, { show: true, loadingText: "正在加载改动区域预览…" });
              }
              $("chat-status").textContent =
                ev.message.mode === "reject"
                  ? "已拒绝与图纸无关的话题。"
                  : waitingPreview
                    ? "方案已出，正在生成改后预览…"
                    : ev.message.overlay || planHasEdits(ev.message.plan) || ev.message.visibility_followup
                      ? "方案已出，右侧是改动区域预览。确认后才会写入当前图纸。"
                      : ev.message.mode === "apply"
                        ? "已写入当前图纸，可以撤回。"
                        : ev.message.mode === "undo"
                          ? "已撤回上一步。"
                          : ev.message.text
                            ? "已按当前图纸回答。"
                            : "这次没有给出完整回答，请换一种说法再试。";
              try {
                state.apply = await api(`/api/projects/${encodeURIComponent(projectId)}/apply`);
                updateApplyUi();
              } catch (_) {}
              if (!waitingPreview && !ev.message.overlay && (ev.message.mode === "apply" || ev.message.mode === "undo")) {
                await loadPreview(ev.message.mode === "undo" ? pendingOverlay() : null);
              }
              job.busy = false;
              job.status = $("chat-status").textContent || "";
              $("chat-submit").disabled = false;
            } else {
              job.busy = ev.preview_pending || (ev.message && ev.message.preview_pending && !ev.message.overlay);
              job.status = ev.preview_pending || (ev.message && ev.message.preview_pending)
                ? "方案已出，正在生成改后预览…"
                : "";
            }
          } else if (ev.type === "error") {
            throw new Error(ev.message || "回复失败。");
          }
        }
      }
    } catch (err) {
      if (err && err.name === "AbortError") return;
      if (chatIsOnScreen(projectId)) {
        const live = ensureChatDraft(projectId).querySelector(".msg-body");
        if (live) live.textContent = err.message || "回复失败，请稍后重试。";
        $("chat-status").textContent = err.message || "回复失败。";
      }
    } finally {
      const still = chatJob(projectId);
      if (still && still.abort === ac) {
        still.abort = null;
        still.draft = null;
        still.busy = false;
      }
      if (chatIsOnScreen(projectId)) {
        syncComposer(projectId);
        $("chat-submit").disabled = isProjectBusy(projectId);
        $("chat-log").scrollTop = $("chat-log").scrollHeight;
      }
    }
  });

  $("apply-btn").addEventListener("click", async () => {
    const projectId = state.project && state.project.id;
    if (!isReady() || !projectId || isProjectBusy(projectId)) return;
    const job = ensureChatJob(projectId);
    job.busy = true;
    job.status = "正在写入当前图纸…";
    $("apply-btn").disabled = true;
    $("chat-submit").disabled = true;
    $("chat-status").textContent = job.status;
    try {
      const result = await api(`/api/projects/${encodeURIComponent(projectId)}/apply`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ region: state.regionId }),
      });
      if (!chatIsOnScreen(projectId)) return;
      state.apply = Object.assign({}, state.apply, result, { pending_plan: null, dirty: true, can_undo: true });
      $("chat-status").textContent = "已写入当前图纸，可以撤回。";
      const chat = await api(`/api/projects/${encodeURIComponent(projectId)}/chat`);
      if (!chatIsOnScreen(projectId)) return;
      renderMessages(chat.messages || []);
      beginViewChange("正在刷新图面…");
      await loadPreview(null, { show: true, loadingText: "正在刷新图面…" });
      updateApplyUi();
    } catch (err) {
      if (chatIsOnScreen(projectId)) $("chat-status").textContent = err.message || "写入图纸失败。";
    } finally {
      if (chatJob(projectId) && !chatJob(projectId).abort) {
        job.busy = false;
        job.status = "";
      }
      if (chatIsOnScreen(projectId)) {
        $("apply-btn").disabled = false;
        syncComposer(projectId);
      }
    }
  });

  $("undo-btn").addEventListener("click", async () => {
    const projectId = state.project && state.project.id;
    if (!isReady() || !projectId || isProjectBusy(projectId)) return;
    const job = ensureChatJob(projectId);
    job.busy = true;
    job.status = "正在撤回…";
    $("undo-btn").disabled = true;
    $("chat-submit").disabled = true;
    $("chat-status").textContent = job.status;
    try {
      const result = await api(`/api/projects/${encodeURIComponent(projectId)}/undo`, { method: "POST" });
      if (!chatIsOnScreen(projectId)) return;
      state.apply = Object.assign({}, state.apply, result);
      $("chat-status").textContent = result.restored_official ? "已撤回，当前图纸已恢复到改前状态。" : "已撤回上一步。";
      const chat = await api(`/api/projects/${encodeURIComponent(projectId)}/chat`);
      if (!chatIsOnScreen(projectId)) return;
      renderMessages(chat.messages || []);
      try {
        state.apply = await api(`/api/projects/${encodeURIComponent(projectId)}/apply`);
      } catch (_) {}
      beginViewChange("正在刷新图面…");
      await loadPreview(pendingOverlay(), { show: true, loadingText: "正在刷新图面…" });
      updateApplyUi();
    } catch (err) {
      if (chatIsOnScreen(projectId)) $("chat-status").textContent = err.message || "撤回失败。";
    } finally {
      if (chatJob(projectId) && !chatJob(projectId).abort) {
        job.busy = false;
        job.status = "";
      }
      if (chatIsOnScreen(projectId)) {
        $("undo-btn").disabled = false;
        syncComposer(projectId);
      }
    }
  });

  function pdfUrl() {
    if (!state.project) return "";
    const q = new URLSearchParams();
    if (state.regionId) q.set("region", state.regionId);
    if (state.previewSource === "official") q.set("source", "official");
    return `/api/projects/${encodeURIComponent(state.project.id)}/pdf?${q.toString()}`;
  }

  function filenameFromDisposition(header, fallback) {
    if (!header) return fallback;
    const star = /filename\*=UTF-8''([^;]+)/i.exec(header);
    if (star) {
      try {
        return decodeURIComponent(star[1].trim());
      } catch (_) {
        return star[1].trim();
      }
    }
    const plain = /filename="?([^";]+)"?/i.exec(header);
    return plain ? plain[1].trim() : fallback;
  }

  $("pdf-btn").addEventListener("click", async () => {
    if (!isReady() || state.pdfBusy) return;
    state.pdfBusy = true;
    const btn = $("pdf-btn");
    const prev = btn.textContent;
    btn.textContent = "正在导出PDF…";
    btn.disabled = true;
    setPreviewLoading(true, "正在导出PDF…");
    try {
      const res = await fetch(pdfUrl(), { credentials: "same-origin" });
      if (!res.ok) {
        let detail = "这份 PDF 没能导出，请换一个分区再试。";
        try {
          const body = await res.json();
          if (body.detail) detail = body.detail;
        } catch (_) {}
        throw new Error(detail);
      }
      const blob = await res.blob();
      const name = filenameFromDisposition(
        res.headers.get("content-disposition"),
        `${(state.project && state.project.name) || "图纸"}.pdf`
      );
      const obj = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = obj;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(obj), 4000);
      $("chat-status").textContent = "PDF 已开始下载。";
    } catch (err) {
      $("chat-status").textContent = err.message || "导出 PDF 失败。";
    } finally {
      state.pdfBusy = false;
      btn.textContent = prev || "导出PDF";
      btn.disabled = false;
      setPreviewLoading(false);
    }
  });

  $("view-work-btn").addEventListener("click", () => {
    state.previewSource = "work";
    updateApplyUi();
    beginViewChange("正在切换图面…");
    loadPreview(null, { show: true, loadingText: "正在切换图面…" });
  });
  $("view-official-btn").addEventListener("click", () => {
    state.previewSource = "official";
    updateApplyUi();
    beginViewChange("正在切换图面…");
    loadPreview(null, { show: true, loadingText: "正在切换图面…" });
  });
  if ($("view-scene-btn")) {
    $("view-scene-btn").addEventListener("click", () => {
      const s = sceneApi();
      if (s && s.hasScene()) s.preferScene();
      else loadScene({ pending: !!pendingOverlay() });
      updateApplyUi();
    });
  }
  if ($("view-image-btn")) {
    $("view-image-btn").addEventListener("click", () => {
      const s = sceneApi();
      if (s) s.showImageFallback();
      const img = $("preview-image");
      if (img && img.getAttribute("src")) img.hidden = false;
      updateApplyUi();
    });
  }

  $("chat-input").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      $("chat-form").requestSubmit();
    }
  });


  $("preview-stage").addEventListener("wheel", (ev) => {
    if (sceneActive()) return;
    if ($("preview-image").hidden) return;
    ev.preventDefault();
    const factor = ev.deltaY < 0 ? 1.12 : 1 / 1.12;
    zoomBy(factor, ev.clientX, ev.clientY);
  }, { passive: false });

  $("preview-stage").addEventListener("pointerdown", (ev) => {
    if (sceneActive()) return;
    if ($("preview-image").hidden || state.zoom.scale <= 1.02) return;
    if (ev.button !== 0) return;
    state.zoom.dragging = true;
    state.zoom.lastX = ev.clientX;
    state.zoom.lastY = ev.clientY;
    $("preview-stage").classList.add("is-panning");
    try { ev.currentTarget.setPointerCapture(ev.pointerId); } catch (_) {}
    ev.preventDefault();
  });

  $("preview-stage").addEventListener("pointermove", (ev) => {
    if (!state.zoom.dragging) return;
    state.zoom.x += ev.clientX - state.zoom.lastX;
    state.zoom.y += ev.clientY - state.zoom.lastY;
    state.zoom.lastX = ev.clientX;
    state.zoom.lastY = ev.clientY;
    applyZoom();
  });

  const endPan = (ev) => {
    if (!state.zoom.dragging) return;
    state.zoom.dragging = false;
    $("preview-stage").classList.remove("is-panning");
  };
  $("preview-stage").addEventListener("pointerup", endPan);
  $("preview-stage").addEventListener("pointercancel", endPan);

  $("preview-stage").addEventListener("dblclick", (ev) => {
    if (sceneActive()) return;
    if ($("preview-image").hidden) return;
    ev.preventDefault();
    if (state.zoom.scale > 1.02) resetZoom();
    else zoomBy(2, ev.clientX, ev.clientY);
  });

  $("zoom-in-btn").addEventListener("click", () => {
    if (sceneActive()) {
      sceneApi().zoomBy(1.25);
      $("zoom-label").textContent = sceneApi().zoomLabel();
      return;
    }
    zoomBy(1.25);
  });
  $("zoom-out-btn").addEventListener("click", () => {
    if (sceneActive()) {
      sceneApi().zoomBy(1 / 1.25);
      $("zoom-label").textContent = sceneApi().zoomLabel();
      return;
    }
    zoomBy(1 / 1.25);
  });
  $("zoom-reset-btn").addEventListener("click", () => {
    if (sceneActive()) {
      sceneApi().fit();
      $("zoom-label").textContent = sceneApi().zoomLabel();
      return;
    }
    resetZoom();
  });

  async function bootWorkbench() {
    await refreshProjectList();
    startListWatch();
    let last = null;
    try {
      last = localStorage.getItem(LAST_KEY);
    } catch (_) {}
    if (last && state.projects.some((p) => p.id === last && (p.selectable || p.available))) {
      await selectProject(last);
      return;
    }
    showIdle();
  }

  async function start() {
    try {
      const me = await api("/api/me");
      state.user = me.username;
      showWorkbench();
      await bootWorkbench();
    } catch (err) {
      showLogin(err.status === 401 ? "" : err.message);
    }
  }

  start();
})();
