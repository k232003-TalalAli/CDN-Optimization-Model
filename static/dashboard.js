(() => {
  const SID = document.body.dataset.sid;
  const POLL_MS = 1500;
  const HISTORY = 80;
  const COLORS = { KHI: "#5b9dff", LHR: "#36c5b0", ISB: "#e7b04a", QTA: "#e87fb0" };
  const CDN_ORDER = ["KHI", "LHR", "ISB", "QTA"];
  const $ = (id) => document.getElementById(id);

  const state = { metrics: null, content: [], history: {}, congestion: null, lastQualityKey: "", lastRequest: null };
  CDN_ORDER.forEach((c) => (state.history[c] = { latency: [], throughput: [], bandwidth: [] }));

  async function api(path, body) {
    const url = `${path}${path.includes("?") ? "&" : "?"}sid=${encodeURIComponent(SID)}`;
    const opts = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    const res = await fetch(url, opts);
    if (res.status === 401) { window.location.href = "/"; throw new Error("no session"); }
    return { status: res.status, data: await res.json() };
  }

  const fmt = (v, d = 1) => (v === null || v === undefined || Number.isNaN(v) ? "–" : Number(v).toFixed(d));
  const mb = (bytes) => `${(bytes / 1e6).toFixed(1)} MB`;
  const show = (el, on) => { el.hidden = !on; };
  const selected = (name) => document.querySelector(`input[name=${name}]:checked`).value;
  const clock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour12: false });

  /* ---------- data loading ---------- */

  async function loadContent() {
    const { data } = await api("/api/content");
    state.content = data.content;
    $("contentSelect").innerHTML = state.content.map((c) => `<option value="${c.name}">${c.name}</option>`).join("");
  }

  async function poll() {
    try {
      const { status, data } = await api("/api/metrics");
      if (status !== 200) { setStatus("down", data.error.message); return; }
      setStatus("live");
      state.metrics = data;
      pushHistory(data.servers);
      render();
    } catch (e) {
      if (e.message !== "no session") setStatus("down", "Dashboard cannot reach the server.");
    }
  }

  function setStatus(kind, message) {
    const pill = $("netStatus");
    pill.className = `status-pill ${kind}`;
    pill.textContent = kind === "live" ? "network live" : "network down";
    $("banner").textContent = message || "";
    show($("banner"), kind === "down");
  }

  function pushHistory(servers) {
    for (const s of servers) {
      const h = state.history[s.cdn];
      h.latency.push(s.latency_ms);
      h.throughput.push(s.throughput_mbps);
      h.bandwidth.push(s.available_bandwidth_mbps);
      for (const key of Object.keys(h)) if (h[key].length > HISTORY) h[key].shift();
    }
  }

  /* ---------- rendering ---------- */

  function render() {
    const m = state.metrics;
    renderControls(m);
    renderServer(m);
    renderTable(m);
    renderRecent(m);
    renderTransfer(m);
    renderCharts(m);
    $("scaleNote").textContent = `${m.bw_scale}×`;
  }

  function syncSelect(select, options, value) {
    const key = options.map((o) => o.value).join("|");
    if (select.dataset.key !== key) {
      select.innerHTML = options.map((o) => `<option value="${o.value}">${o.label}</option>`).join("");
      select.dataset.key = key;
    }
    if (document.activeElement !== select) select.value = value;
  }

  function renderControls(m) {
    const { prefs, selection } = m;
    document.querySelector(`input[name=cdnmode][value=${prefs.cdn_mode}]`).checked = true;
    document.querySelector(`input[name=qmode][value=${prefs.quality_mode}]`).checked = true;
    const auto = prefs.cdn_mode === "auto";
    $("cdnSelect").disabled = auto;
    syncSelect($("cdnSelect"), m.servers.map((s) => ({ value: s.cdn, label: s.name })), auto ? selection.cdn || prefs.cdn : prefs.cdn);
    if (document.activeElement !== $("contentSelect")) $("contentSelect").value = prefs.content;

    const resolutions = selection.available_resolutions;
    const isVideo = resolutions.length > 0 || (contentInfo() || {}).type === "video";
    $("qualitySelect").disabled = prefs.quality_mode === "auto" || !isVideo;
    const shown = prefs.quality_mode === "auto" ? selection.resolution : prefs.quality;
    syncSelect($("qualitySelect"), resolutions.map((r) => ({ value: r, label: r })), shown || "");
    document.querySelectorAll("input[name=qmode]").forEach((r) => (r.disabled = !isVideo));

    const info = contentInfo();
    if (info) {
      const size = info.resolutions
        ? Object.entries(info.resolutions).map(([r, i]) => `${r} ${mb(i.size_bytes)}`).join(" · ")
        : mb(info.size_bytes);
      $("contentMeta").textContent = `Type: ${info.mime} · ${size}`;
    }

    const err = selection.error;
    const note = $("qualityNote");
    note.className = err ? "note error" : "note warn";
    note.textContent = err ? err.message : selection.warnings.join(" ");
    show(note, Boolean(err) || selection.warnings.length > 0);
  }

  function contentInfo() {
    return state.content.find((c) => c.name === (state.metrics ? state.metrics.prefs.content : ""));
  }

  function renderServer(m) {
    const { selection, servers, user } = m;
    const row = servers.find((s) => s.cdn === selection.cdn);
    $("mLatency").textContent = row ? fmt(row.latency_ms, 1) : "–";
    $("mBw").textContent = row ? fmt(row.available_bandwidth_mbps, 1) : "–";
    $("mCap").textContent = row ? `of ${fmt(row.capacity_mbps, 0)} Mbps link` : "";
    $("mThr").textContent = row ? fmt(row.throughput_mbps, 1) : "–";
    $("mLoad").textContent = row ? fmt(row.server_load_pct, 0) : "–";
    $("mScore").textContent = row && row.score !== null ? fmt(row.score, 3) : "–";
    $("pathText").textContent = selection.path.length
      ? `${selection.path.join(" → ")} · ${fmt(selection.path_cost_ms, 0)} ms one-way`
      : "–";

    const note = $("switchNote");
    const last = m.last;
    const nowCdn = selection.cdn;
    if (selection.cdn_mode === "auto" && last && last.mode === "auto" && nowCdn && nowCdn !== last.cdn && last.content === m.prefs.content) {
      const prev = servers.find((s) => s.cdn === last.cdn);
      const next = servers.find((s) => s.cdn === nowCdn);
      const prevScore = prev && prev.score !== null ? ` (score now ${fmt(prev.score, 3)})` : "";
      note.textContent = `New requests will use ${nowCdn} (score ${fmt(next.score, 3)}) instead of ${last.cdn}${prevScore}. Downloads already running are not interrupted.`;
      show(note, true);
    } else {
      show(note, false);
    }
  }

  function renderTable(m) {
    const body = $("serverTable").querySelector("tbody");
    body.innerHTML = m.servers.map((s) => {
      const badges = [];
      if (s.active) badges.push('<span class="badge active">ACTIVE</span>');
      if (!s.has_replica) badges.push('<span class="badge">NO REPLICA</span>');
      if (s.injected_mbps > 0) badges.push(`<span class="badge traffic">TRAFFIC ${fmt(s.injected_mbps, 0)} Mbps</span>`);
      return `<tr class="${s.active ? "active" : ""} ${s.has_replica ? "" : "none"}">
        <td><span class="srv"><span class="dot" style="background:${COLORS[s.cdn]}"></span>${s.cdn}</span></td>
        <td class="num">${fmt(s.latency_ms, 1)} ms</td>
        <td class="num">${fmt(s.available_bandwidth_mbps, 1)} Mbps</td>
        <td class="num">${fmt(s.throughput_mbps, 1)} Mbps</td>
        <td class="num">${fmt(s.server_load_pct, 0)}%</td>
        <td class="num">${fmt(s.path_cost_ms, 0)} ms</td>
        <td class="num">${s.score === null ? "–" : fmt(s.score, 3)}</td>
        <td class="breakdown-col">${breakdownBar(s)}</td>
        <td>${badges.join(" ")}</td></tr>`;
    }).join("");
  }

  function breakdownBar(s) {
    if (!s.weighted) return "";
    const parts = ["latency", "bandwidth", "load", "path"].map(
      (k) => `<span class="s-${k}" style="width:${(s.weighted[k] * 100).toFixed(1)}%" title="${k}: ${s.weighted[k].toFixed(3)}"></span>`);
    return `<div class="bar">${parts.join("")}</div>`;
  }

  function renderRecent(m) {
    const box = $("recent");
    if (!m.recent.length) { box.textContent = "No requests yet."; return; }
    box.innerHTML = m.recent.map((r) => `<div><time>${clock(r.time)}</time>${r.user} → <b>${r.cdn}</b>
      ${r.resolution ? ` ${r.resolution}` : ""} · ${r.content} <span class="muted">(${r.mode})</span></div>`).join("");
  }

  function renderTransfer(m) {
    const live = m.transfers.find((t) => t.active);
    const rate = $("transferRate");
    if (rate) rate.textContent = live ? `${fmt(live.rate_mbps, 1)} Mbps (${mb(live.bytes)} received)` : "idle";
  }

  /* ---------- charts ---------- */

  function drawChart(canvas, key, activeCdn) {
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (canvas.width !== w * dpr) { canvas.width = w * dpr; canvas.height = h * dpr; }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const pad = { l: 38, r: 8, t: 8, b: 16 };
    const all = CDN_ORDER.flatMap((c) => state.history[c][key]).filter((v) => v !== null && v !== undefined);
    const max = Math.max(1, ...all) * 1.15;
    const x = (i) => pad.l + ((w - pad.l - pad.r) * i) / (HISTORY - 1);
    const y = (v) => pad.t + (h - pad.t - pad.b) * (1 - v / max);

    ctx.font = "11px ui-monospace, Menlo, monospace";
    ctx.fillStyle = "#8a8f98"; ctx.strokeStyle = "#1f2226"; ctx.lineWidth = 1;
    for (let i = 0; i <= 3; i++) {
      const v = (max * i) / 3, yy = y(v);
      ctx.beginPath(); ctx.moveTo(pad.l, yy); ctx.lineTo(w - pad.r, yy); ctx.stroke();
      ctx.textAlign = "right"; ctx.fillText(v.toFixed(max < 10 ? 1 : 0), pad.l - 6, yy + 4);
    }
    for (const cdn of CDN_ORDER) {
      const series = state.history[cdn][key];
      const offset = HISTORY - series.length;
      ctx.beginPath();
      let started = false;
      series.forEach((v, i) => {
        if (v === null || v === undefined) { started = false; return; }
        const px = x(i + offset), py = y(v);
        started ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
        started = true;
      });
      ctx.strokeStyle = COLORS[cdn];
      ctx.globalAlpha = cdn === activeCdn ? 1 : 0.55;
      ctx.lineWidth = cdn === activeCdn ? 2.4 : 1.4;
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
  }

  function renderCharts(m) {
    drawChart($("chLatency"), "latency", m.selection.cdn);
    drawChart($("chThr"), "throughput", m.selection.cdn);
    drawChart($("chBw"), "bandwidth", m.selection.cdn);
  }

  $("legend").innerHTML = CDN_ORDER.map((c) => `<span><span class="dot" style="background:${COLORS[c]}"></span> ${c}</span>`).join("");

  /* ---------- user actions ---------- */

  async function savePrefs(path, body) {
    const { status, data } = await api(path, body);
    if (status === 400) { $("banner").textContent = data.error.message; show($("banner"), true); }
    await poll();
  }

  document.querySelectorAll("input[name=cdnmode]").forEach((r) =>
    r.addEventListener("change", () => {
      const body = { cdn_mode: r.value };
      if (r.value === "manual" && state.metrics && state.metrics.selection.cdn) body.cdn = state.metrics.selection.cdn;
      savePrefs("/api/select-cdn", body);
    }));
  $("cdnSelect").addEventListener("change", (e) => savePrefs("/api/select-cdn", { cdn: e.target.value }));
  $("contentSelect").addEventListener("change", (e) => savePrefs("/api/select-content", { content: e.target.value }));
  document.querySelectorAll("input[name=qmode]").forEach((r) =>
    r.addEventListener("change", () => {
      const body = { quality_mode: r.value };
      const shown = $("qualitySelect").value;
      if (r.value === "manual" && shown) body.quality = shown;
      savePrefs("/api/select-quality", body);
    }));
  $("qualitySelect").addEventListener("change", (e) => savePrefs("/api/select-quality", { quality: e.target.value }));

  async function request(download) {
    const buttons = [$("btnStream"), $("btnDownload")];
    buttons.forEach((b) => (b.disabled = true));
    try {
      const { status, data } = await api("/api/optimize", {});
      await poll();
      if (status === 401 || status === 503) return;
      if (!data.ok) { showDecision(data, null); return; }
      showDecision(data, download);
      if (download) {
        const a = document.createElement("a");
        a.href = data.download_url; a.download = "";
        document.body.appendChild(a); a.click(); a.remove();
      }
    } finally {
      buttons.forEach((b) => (b.disabled = false));
    }
  }
  $("btnStream").addEventListener("click", () => request(false));
  $("btnDownload").addEventListener("click", () => request(true));

  function showDecision(d, download) {
    show($("playback"), true);
    const player = $("player");
    const info = state.content.find((c) => c.name === d.content);
    if (!d.ok) {
      player.innerHTML = `<div class="empty">Request not served.</div>`;
    } else if (download) {
      player.innerHTML = `<div class="empty">Downloading ${d.content}${d.resolution ? " (" + d.resolution + ")" : ""} from ${d.cdn}.</div>`;
    } else if (info.type === "video") {
      player.innerHTML = `<video controls autoplay playsinline src="${d.stream_url}"></video>`;
    } else if (info.type === "image") {
      player.innerHTML = `<img alt="${d.content}" src="${d.stream_url}">`;
    } else {
      player.innerHTML = `<div class="empty">PDF served by ${d.cdn}. <a class="link" target="_blank" href="${d.stream_url}">Open</a></div>`;
    }
    const rows = d.ok ? [
      ["CDN", `${d.cdn} (${d.cdn_mode})`],
      ["Resolution", d.resolution || "n/a"],
      ["Source URL", d.url],
      ["Path", `${d.path.join(" → ")} · ${fmt(d.path_cost_ms, 0)} ms`],
      ["Transfer", '<span id="transferRate">starting</span>'],
    ] : [["Result", "Not served"]];
    $("decisionList").innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
    $("decisionReason").textContent = d.error ? d.error.message : d.reason;
    $("decisionWarn").textContent = d.warnings.join(" ");
    show($("decisionWarn"), d.warnings.length > 0);
  }

  /* ---------- congestion ---------- */

  async function loadCongestion(body) {
    const { status, data } = await api("/api/congestion", body);
    if (!data.ok) {
      $("congStatus").textContent = data.error.message;
      return;
    }
    state.congestion = data.cdns;
    renderCongestion();
  }

  function renderCongestion() {
    const cdn = $("congCdn").value, info = state.congestion[cdn];
    const slider = $("congMbps");
    slider.max = Math.floor(info.uplink_mbps);
    $("congUplink").textContent = `${info.uplink_mbps} Mbps`;
    $("congOut").textContent = slider.value;
    const active = Object.entries(state.congestion).filter(([, i]) => i.mbps > 0);
    $("congStatus").textContent = active.length
      ? "Running: " + active.map(([c, i]) => `${c} ${i.mbps} Mbps`).join(", ")
      : "No background traffic.";
  }

  $("congCdn").addEventListener("change", () => { if (state.congestion) renderCongestion(); });
  $("congMbps").addEventListener("input", () => ($("congOut").textContent = $("congMbps").value));
  document.querySelectorAll("[data-leave]").forEach((b) =>
    b.addEventListener("click", () => {
      const uplink = state.congestion[$("congCdn").value].uplink_mbps;
      $("congMbps").value = Math.max(Math.round(uplink - Number(b.dataset.leave)), 1);
      $("congOut").textContent = $("congMbps").value;
    }));
  $("congStart").addEventListener("click", () => {
    const mbps = Number($("congMbps").value);
    if (mbps > 0) loadCongestion({ action: "start", cdn: $("congCdn").value, mbps });
  });
  $("congStop").addEventListener("click", () => loadCongestion({ action: "stop", cdn: "all" }));

  /* ---------- start ---------- */

  window.addEventListener("resize", () => state.metrics && renderCharts(state.metrics));
  (async () => {
    await loadContent();
    await loadCongestion().catch(() => {});
    await poll();
    setInterval(poll, POLL_MS);
    setInterval(() => loadCongestion().catch(() => {}), 5000);
  })();
})();
