/* vLLM Fleet Dashboard — front end. No external dependencies. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

/* ---------------- formatting ---------------- */

const fmtInt = (v) => (v === null || v === undefined ? "–" : Math.round(v).toLocaleString());
const fmt1 = (v) => (v === null || v === undefined ? "–" : (Math.round(v * 10) / 10).toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 }));

function fmtCompact(v) {
  if (v === null || v === undefined) return "–";
  if (v >= 1e6) return (v / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  if (v >= 1e4) return (v / 1e3).toFixed(1).replace(/\.0$/, "") + "K";
  return Math.round(v).toLocaleString();
}

function gib(mib) {
  if (mib === null || mib === undefined) return "–";
  const g = mib / 1024;
  return g >= 100 ? Math.round(g) + " GiB" : (Math.round(g * 10) / 10) + " GiB";
}

function fmtDur(s) {
  if (s === null || s === undefined) return "–";
  s = Math.max(0, Math.round(s));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m`;
  return `${s}s`;
}

function fmtAgo(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  return s < 5 ? "just now" : s < 90 ? `${s}s ago` : fmtDur(s) + " ago";
}

const clockOf = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

/* ---------------- small components ---------------- */

const ICONS = {
  good: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.5A6.5 6.5 0 1 0 14.5 8 6.5 6.5 0 0 0 8 1.5Zm3.2 4.9-3.9 4a.75.75 0 0 1-1.08 0L4.8 8.7a.75.75 0 1 1 1.08-1.04l1 1.03 3.36-3.44A.75.75 0 1 1 11.2 6.4Z"/></svg>',
  warn: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M7.13 2.2a1 1 0 0 1 1.74 0l6.02 10.6A1 1 0 0 1 14.02 14.3H1.98a1 1 0 0 1-.87-1.5ZM8 5.4a.8.8 0 0 0-.8.85l.2 3a.6.6 0 0 0 1.2 0l.2-3A.8.8 0 0 0 8 5.4Zm0 5.3a.85.85 0 1 0 0 1.7.85.85 0 0 0 0-1.7Z"/></svg>',
  crit: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.5A6.5 6.5 0 1 0 14.5 8 6.5 6.5 0 0 0 8 1.5Zm2.6 8.05a.75.75 0 1 1-1.06 1.06L8 9.06 6.46 10.6A.75.75 0 0 1 5.4 9.55L6.94 8 5.4 6.46A.75.75 0 0 1 6.46 5.4L8 6.94 9.54 5.4a.75.75 0 0 1 1.06 1.06L9.06 8Z"/></svg>',
  mute: '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="4"/></svg>',
};

/* Status colour is never the only cue: every chip ships an icon and a word. */
function statusChip(kind, label) {
  const c = el("span", "chip " + kind);
  c.innerHTML = ICONS[kind] || ICONS.mute;
  c.appendChild(document.createTextNode(label));
  return c;
}

function severity(pct) {
  if (pct === null || pct === undefined) return "ok";
  if (pct >= 92) return "crit";
  if (pct >= 80) return "warn";
  return "ok";
}

/** Meter: fill carries severity, track is a lighter step of the same ramp. */
function meterRow(key, pct, valueText, forceClass) {
  const row = el("div", "meterrow");
  row.appendChild(el("div", "k", key));
  const m = el("div", "meter " + (forceClass || severity(pct)));
  const fill = el("i");
  const p = Math.max(0, Math.min(100, pct ?? 0));
  fill.style.width = p + "%";
  if (p > 98) fill.classList.add("r4");
  m.appendChild(fill);
  row.appendChild(m);
  row.appendChild(el("div", "v", valueText));
  return row;
}

function copyBtn(label, text, title) {
  const b = el("button", "copy", label);
  b.type = "button";
  if (title) b.title = title;
  b.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = el("textarea");
      ta.value = text; document.body.appendChild(ta); ta.select();
      document.execCommand("copy"); ta.remove();
    }
    const old = b.textContent;
    b.textContent = "Copied"; b.classList.add("done");
    setTimeout(() => { b.textContent = old; b.classList.remove("done"); }, 1200);
  });
  return b;
}

/* ---------------- sparkline ---------------- */

const SPARK_W = 320, SPARK_H = 52;

/**
 * Single-series sparkline: 2px line, 10% area wash, end dot with a surface
 * ring, crosshair + tooltip on hover. One series, so no legend — the caption
 * above it names what is plotted.
 */
function sparkline(points, opts = {}) {
  const wrap = el("div", "sparkwrap");
  const cap = el("div", "cap");
  cap.appendChild(el("span", null, opts.title || ""));
  cap.appendChild(el("span", null, opts.right || ""));
  wrap.appendChild(cap);

  const vals = points.map((p) => (p.v === null || p.v === undefined ? null : p.v));
  const real = vals.filter((v) => v !== null);
  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("class", "spark");
  svg.setAttribute("viewBox", `0 0 ${SPARK_W} ${SPARK_H}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `${opts.title || "series"}: ${real.length} samples`);

  if (real.length < 2) {
    wrap.appendChild(svg);
    const empty = el("div", "cap");
    empty.appendChild(el("span", "dim", "collecting history…"));
    wrap.appendChild(empty);
    return wrap;
  }

  const max = opts.max !== undefined ? opts.max : Math.max(...real, opts.floor ?? 1) * 1.15;
  const min = 0;
  const n = points.length;
  const x = (i) => (n === 1 ? 0 : (i / (n - 1)) * SPARK_W);
  const y = (v) => SPARK_H - 2 - ((v - min) / (max - min || 1)) * (SPARK_H - 6);

  let d = "", area = "", started = false, lastI = -1, prevI = -1;
  points.forEach((p, i) => {
    const v = vals[i];
    if (v === null) {
      if (started && prevI >= 0) area += `L${x(prevI).toFixed(2)},${SPARK_H} Z `;  // close the gap
      started = false;
      return;
    }
    d += `${started ? "L" : "M"}${x(i).toFixed(2)},${y(v).toFixed(2)} `;
    area += started
      ? `L${x(i).toFixed(2)},${y(v).toFixed(2)} `
      : `M${x(i).toFixed(2)},${SPARK_H} L${x(i).toFixed(2)},${y(v).toFixed(2)} `;
    started = true; lastI = i; prevI = i;
  });
  if (started && lastI >= 0) area += `L${x(lastI).toFixed(2)},${SPARK_H} Z`;

  const color = opts.color || "var(--s1)";
  const ap = document.createElementNS(svgNS, "path");
  ap.setAttribute("d", area); ap.setAttribute("fill", color);
  ap.setAttribute("fill-opacity", "0.10"); ap.setAttribute("stroke", "none");
  svg.appendChild(ap);

  const lp = document.createElementNS(svgNS, "path");
  lp.setAttribute("d", d.trim()); lp.setAttribute("fill", "none");
  lp.setAttribute("stroke", color); lp.setAttribute("stroke-width", "2");
  lp.setAttribute("stroke-linejoin", "round"); lp.setAttribute("stroke-linecap", "round");
  lp.setAttribute("vector-effect", "non-scaling-stroke");
  svg.appendChild(lp);
  wrap.appendChild(svg);

  /* crosshair + end dot live in CSS pixels so non-uniform scaling can't warp them */
  const xh = el("div", "xhair"); xh.style.opacity = "0";
  const dot = el("div", "dot");
  const endDot = el("div", "dot end");
  wrap.appendChild(xh); wrap.appendChild(dot); wrap.appendChild(endDot);

  const place = (node, i) => {
    const r = svg.getBoundingClientRect(), w = wrap.getBoundingClientRect();
    const top = r.top - w.top;
    node.style.left = (x(i) / SPARK_W) * r.width + "px";
    node.style.top = top + (y(vals[i]) / SPARK_H) * r.height + "px";
  };
  const positionEnd = () => { if (lastI >= 0) { place(endDot, lastI); endDot.style.opacity = "1"; } };
  requestAnimationFrame(positionEnd);
  window.addEventListener("resize", positionEnd, { passive: true });

  const tip = $("#tip");
  const move = (ev) => {
    const r = svg.getBoundingClientRect();
    const frac = Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width));
    let i = Math.round(frac * (n - 1));
    for (let k = 0; k < n && vals[i] === null; k++) {
      if (i - k >= 0 && vals[i - k] !== null) { i -= k; break; }
      if (i + k < n && vals[i + k] !== null) { i += k; break; }
    }
    if (vals[i] === null) return;
    const w = wrap.getBoundingClientRect();
    xh.style.opacity = "1";
    xh.style.left = (x(i) / SPARK_W) * r.width + "px";
    xh.style.top = (r.top - w.top) + "px";
    xh.style.height = r.height + "px";
    place(dot, i); dot.style.opacity = "1";
    tip.innerHTML = "";
    tip.appendChild(el("div", "tt", clockOf(points[i].t)));
    tip.appendChild(el("div", "tv", (opts.fmt || fmt1)(vals[i]) + (opts.unit || "")));
    tip.style.opacity = "1";
    const tw = tip.getBoundingClientRect();
    tip.style.left = Math.min(window.innerWidth - tw.width - 8, Math.max(8, ev.clientX + 12)) + "px";
    tip.style.top = Math.max(8, ev.clientY - tw.height - 12) + "px";
  };
  const leave = () => { tip.style.opacity = "0"; xh.style.opacity = "0"; dot.style.opacity = "0"; };
  svg.addEventListener("mousemove", move);
  svg.addEventListener("mouseleave", leave);
  svg.addEventListener("touchmove", (e) => { if (e.touches[0]) move(e.touches[0]); }, { passive: true });
  svg.addEventListener("touchend", leave);
  return wrap;
}

/* ---------------- rendering ---------------- */

let LAST = null;
let VIEW = localStorage.getItem("fleet.view") || "cards";
const openRows = new Set();

function nodeStatusKind(n) {
  if (n.status === "offline") return "crit";
  if (n.status === "degraded") return "warn";
  return "good";
}

function renderSummary(data) {
  const f = data.fleet;
  $("#title").textContent = data.title || "vLLM Fleet";
  $("#heroNum").textContent = f.nodes_online;
  $("#heroOf").textContent = ` / ${f.nodes_total}`;

  const note = $("#heroNote");
  note.innerHTML = "";
  if (f.nodes_offline) note.appendChild(statusChip("crit", `${f.nodes_offline} unreachable`));
  else if (f.nodes_degraded) note.appendChild(statusChip("warn", `${f.nodes_degraded} degraded`));
  else note.appendChild(statusChip("good", "all healthy"));
  note.appendChild(el("span", "dim", `${f.gpus} GPU${f.gpus === 1 ? "" : "s"} · ${f.endpoints_live}/${f.endpoints_total} vLLM up`));

  const fh = data.fleet_history || [];
  const utilPts = fh.map((p) => ({ t: p.t, v: p.u }));
  const tpsPts = fh.map((p) => ({ t: p.t, v: p.g }));

  const tiles = $("#tiles");
  tiles.innerHTML = "";

  const tile = (label, value, unit, foot, spark) => {
    const c = el("div", "card tile");
    c.appendChild(el("div", "label", label));
    const v = el("div", "val");
    v.appendChild(document.createTextNode(value));
    if (unit) { const u = el("span", "unit", unit); v.appendChild(u); }
    c.appendChild(v);
    if (spark) c.appendChild(spark);
    if (foot) c.appendChild(typeof foot === "string" ? el("div", "foot", foot) : foot);
    tiles.appendChild(c);
    return c;
  };

  tile("GPU utilization", f.gpu_util_pct === null ? "–" : fmt1(f.gpu_util_pct), "%",
    null, sparkline(utilPts, { title: "last 2h", unit: "%", max: 100, right: "" }));

  const memTile = tile("GPU memory in use", gib(f.gpu_mem_used_mib).replace(" GiB", ""), "GiB",
    `of ${gib(f.gpu_mem_total_mib)} fleet-wide`);
  memTile.insertBefore(
    meterRow("", f.gpu_mem_used_pct, (f.gpu_mem_used_pct ?? 0).toFixed(0) + "%"),
    memTile.lastChild
  );

  tile("Models loaded", fmtInt(f.models_loaded), null,
    f.models_available
      ? `${f.endpoints_live} live endpoint${f.endpoints_live === 1 ? "" : "s"} · ${f.models_available} pulled and ready`
      : `${f.endpoints_live} live endpoint${f.endpoints_live === 1 ? "" : "s"}`);

  const tpsTile = tile("Generation throughput", fmtCompact(f.gen_tps), "tok/s",
    f.ollama_endpoints ? "vLLM only — Ollama reports no metrics" : null,
    sparkline(tpsPts, { title: "last 2h", unit: " tok/s", fmt: fmt1, floor: 10 }));
  void tpsTile;

  tile("Requests running", fmtInt(f.requests_running), null,
    `${fmtInt(f.requests_waiting)} queued`);
}

function metric(label, value, cls) {
  const d = el("div", "met" + (cls ? " " + cls : ""));
  d.appendChild(el("span", "mk", label));
  d.appendChild(el("span", "mv", value));
  return d;
}

const ENGINE_LABEL = { vllm: "vLLM", ollama: "Ollama" };

/** One model server on this node: identity, health, live load, copy-ready URL. */
function endpointRow(e, nodeName) {
  const isOllama = e.engine === "ollama";
  const row = el("div", "ep" + (e.reachable ? "" : " down"));

  const r1 = el("div", "r1");
  r1.appendChild(el("span", "portbadge", ":" + e.port));
  r1.appendChild(el("span", "enginebadge " + (e.engine || "vllm"), ENGINE_LABEL[e.engine] || "vLLM"));
  const label = e.models.length
    ? e.models.join(", ")
    : isOllama ? "nothing resident" : "no model reported";
  const mid = el("span", "mid", label);
  if (!e.models.length) mid.classList.add("dim");
  r1.appendChild(mid);
  r1.appendChild(el("span", "spacer"));
  if (!e.reachable) {
    r1.appendChild(statusChip("crit", "down"));
  } else if (isOllama) {
    // An idle Ollama has unloaded everything and is perfectly healthy.
    r1.appendChild(e.models.length ? statusChip("good", "serving") : statusChip("good", "ready"));
  } else {
    const many = (e.restarts || 0) > 3;
    r1.appendChild(statusChip(many ? "warn" : "good", many ? `${e.restarts} restarts` : "serving"));
  }
  row.appendChild(r1);

  const bits = [];
  if (isOllama && e.engine_version) bits.push("ollama " + e.engine_version);
  if (e.container) bits.push(e.container);
  if (e.uptime_s) bits.push("up " + fmtDur(e.uptime_s));
  if (!isOllama && e.restarts !== null && e.restarts !== undefined)
    bits.push(`${e.restarts} restart${e.restarts === 1 ? "" : "s"}`);
  if (!isOllama && e.max_model_len) bits.push("ctx " + fmtCompact(e.max_model_len));
  if (!isOllama && e.tensor_parallel_size > 1) bits.push("TP " + e.tensor_parallel_size);
  if (!e.reachable && e.error) bits.push(e.error.slice(0, 70));
  if (bits.length) row.appendChild(el("div", "r2", bits.join(" · ")));

  if (isOllama && e.reachable) {
    // Per-model residency: what each one costs and when it evaporates.
    (e.loaded || []).forEach((m) => {
      const line = el("div", "mline");
      if ((e.loaded || []).length > 1) line.appendChild(el("span", "mname", m.id));
      const facts = [];
      if (m.vram_mib) facts.push(gib(m.vram_mib) + " VRAM");
      if (m.parameter_size) facts.push(m.parameter_size);
      if (m.quantization) facts.push(m.quantization);
      if (m.expires_in_s) facts.push("unloads in " + fmtDur(m.expires_in_s));
      line.appendChild(el("span", "mfacts", facts.join(" · ")));
      row.appendChild(line);
    });

    const r3 = el("div", "r3");
    r3.appendChild(metric("resident", fmtInt(e.loaded_count ?? 0)));
    r3.appendChild(metric("available", fmtInt(e.available_count ?? 0)));
    r3.appendChild(metric("VRAM", e.vram_mib ? gib(e.vram_mib) : "–"));
    r3.appendChild(metric("next unload", e.next_unload_s ? fmtDur(e.next_unload_s) : "–"));
    row.appendChild(r3);

    if ((e.available || []).length) {
      const det = el("details", "avail");
      // Cards are rebuilt on every poll, so remember which lists the viewer
      // opened — otherwise the list snaps shut under them every few seconds.
      const key = `${nodeName}:${e.port}`;
      det.open = openRows.has(key);
      det.addEventListener("toggle", () => {
        if (det.open) openRows.add(key);
        else openRows.delete(key);
      });
      const sum = el("summary", null, `${e.available.length} models pulled and ready`);
      det.appendChild(sum);
      const list = el("div", "availlist");
      e.available.forEach((m) => {
        const item = el("div", "availrow");
        item.appendChild(el("span", "an", m.id));
        item.appendChild(el("span", "as", [m.parameter_size, m.quantization,
          m.size_mib ? gib(m.size_mib) : null].filter(Boolean).join(" · ")));
        item.appendChild(copyBtn("copy", m.id, m.id));
        list.appendChild(item);
      });
      det.appendChild(list);
      row.appendChild(det);
    }
  } else if (e.reachable) {
    const r3 = el("div", "r3");
    r3.appendChild(metric("tok/s", e.gen_tps === null || e.gen_tps === undefined ? "–" : fmt1(e.gen_tps)));
    r3.appendChild(metric("running", fmtInt(e.requests_running ?? 0)));
    r3.appendChild(metric("queued", fmtInt(e.requests_waiting ?? 0)));
    r3.appendChild(metric("KV cache", e.kv_cache_pct === null || e.kv_cache_pct === undefined ? "–" : fmt1(e.kv_cache_pct) + "%"));
    r3.appendChild(metric("e2e", e.avg_latency_s ? fmt1(e.avg_latency_s) + "s" : "–"));
    r3.appendChild(metric("TTFT", e.ttft_s ? fmt1(e.ttft_s * 1000) + "ms" : "–"));
    row.appendChild(r3);
  }

  const r4 = el("div", "r4");
  r4.appendChild(el("code", "url", e.base_url));
  r4.appendChild(el("span", "spacer"));
  r4.appendChild(copyBtn("URL", e.base_url, "Copy the OpenAI-compatible base URL"));
  if (e.models.length) r4.appendChild(copyBtn("model id", e.models[0], e.models[0]));
  r4.appendChild(copyBtn("curl", e.curl, "Copy a ready-to-run chat completion request"));
  row.appendChild(r4);
  return row;
}

function endpointList(node) {
  const wrap = el("div", "eps");
  if (!node.endpoints.length) {
    wrap.appendChild(el("div", "ep dim",
      node.status === "offline" ? "No data — node unreachable."
        : node.idle_ok ? "No model loaded right now — machine is up and idle."
        : "No vLLM server detected on this node."));
    return wrap;
  }
  node.endpoints.forEach((e) => wrap.appendChild(endpointRow(e, node.name)));
  return wrap;
}

function nodeCard(node, history) {
  const c = el("div", "card node");
  if (node.status === "offline") c.classList.add("off");

  const head = el("div", "head");
  const idcol = el("div");
  idcol.appendChild(el("div", "name", node.name));
  const hostline = el("div", "host", node.public_host + (node.note ? " · " + node.note : ""));
  idcol.appendChild(hostline);
  head.appendChild(idcol);
  head.appendChild(el("div", "spacer"));

  const right = el("div");
  right.style.textAlign = "right";
  const kind = nodeStatusKind(node);
  right.appendChild(statusChip(kind, node.status === "online" ? "online" : node.status));
  const meta = [];
  if (node.board_model) meta.push(node.board_model);
  if (node.uptime_s) meta.push("up " + fmtDur(node.uptime_s));
  right.appendChild(el("div", "host", meta.join(" · ")));
  head.appendChild(right);
  c.appendChild(head);

  if (node.status_reason) {
    const w = el("div", "warnbar");
    w.innerHTML = ICONS.warn;
    w.appendChild(el("span", null, node.status_reason));
    c.appendChild(w);
  }

  const body = el("div", "body");

  (node.gpus || []).forEach((g) => {
    const line = el("div", "gpuline");
    line.appendChild(el("span", "gname", g.name || `GPU ${g.index}`));
    const bits = [];
    if (g.temp_c !== null && g.temp_c !== undefined) bits.push(`${fmtInt(g.temp_c)}°C`);
    if (g.power_w) bits.push(`${fmtInt(g.power_w)} W`);
    if (g.unified_memory) bits.push("unified memory");
    if (bits.length) line.appendChild(el("span", "gmeta", bits.join(" · ")));
    body.appendChild(line);
    body.appendChild(meterRow("Memory", g.memory_used_pct,
      `${gib(g.memory_used_mib)} / ${gib(g.memory_total_mib)}`));
    body.appendChild(meterRow("Compute", g.util_gpu_pct,
      g.util_gpu_pct === null || g.util_gpu_pct === undefined ? "–" : fmt1(g.util_gpu_pct) + "%", "ok"));
  });

  if (!(node.gpus || []).length) {
    const d = el("div", "gpuline");
    d.appendChild(el("span", "gmeta", node.status === "offline" ? "no data — node unreachable" : "no GPU data from this node"));
    body.appendChild(d);
  }

  const pts = (history || []).map((p) => ({ t: p.t, v: p.u }));
  body.appendChild(sparkline(pts, {
    title: "GPU utilization",
    unit: "%",
    max: 100,
    right: node.status === "offline" ? "" : (node.gen_tps ? `${fmt1(node.gen_tps)} tok/s now` : "idle"),
  }));

  if ((node.web_uis || []).length) {
    const w = el("div", "webuis");
    node.web_uis.forEach((u) => {
      const a = el("a", "webui");
      a.href = u.url; a.target = "_blank"; a.rel = "noopener";
      a.textContent = `${u.name || "web UI"} → :${u.port}`;
      w.appendChild(a);
    });
    body.appendChild(w);
  }

  c.appendChild(body);
  c.appendChild(endpointList(node));
  return c;
}

function renderCards(data) {
  const grid = $("#cards");
  grid.innerHTML = "";
  data.nodes.forEach((n) => grid.appendChild(nodeCard(n, (data.history || {})[n.name] || [])));
}

function renderTable(data) {
  const tb = $("#flat tbody");
  tb.innerHTML = "";
  data.nodes.forEach((n) => {
    const rows = n.endpoints.length ? n.endpoints : [null];
    rows.forEach((e, i) => {
      const tr = el("tr");
      tr.appendChild(el("td", null, i === 0 ? n.name : ""));
      tr.appendChild(el("td", "mono", i === 0 ? n.public_host : ""));
      tr.appendChild(el("td", "num port", e ? String(e.port) : "–"));
      const mtd = el("td", "model", e && e.models.length ? e.models.join(", ") : "—");
      if (e && e.engine === "ollama" && e.available_count)
        mtd.appendChild(el("div", "dim", `+${e.available_count} available`));
      tr.appendChild(mtd);
      const st = el("td");
      if (!e) st.appendChild(statusChip(nodeStatusKind(n), n.status));
      else if (!e.reachable) st.appendChild(statusChip("crit", "down"));
      else st.appendChild(statusChip("good", e.models.length ? "serving" : "ready"));
      if (e) st.appendChild(el("div", "dim", ENGINE_LABEL[e.engine] || "vLLM"));
      tr.appendChild(st);
      tr.appendChild(el("td", "mono", (e && e.container) || "—"));
      tr.appendChild(el("td", "num", e && e.restarts !== null && e.restarts !== undefined ? String(e.restarts) : "–"));
      tr.appendChild(el("td", "num", e && e.uptime_s ? fmtDur(e.uptime_s) : "–"));
      tr.appendChild(el("td", "num", i === 0 && n.gpu_util_pct !== null && n.gpu_util_pct !== undefined ? fmt1(n.gpu_util_pct) + "%" : i === 0 ? "–" : ""));
      tr.appendChild(el("td", "num", i === 0 ? `${gib(n.gpu_mem_used_mib)} / ${gib(n.gpu_mem_total_mib)}` : ""));
      tr.appendChild(el("td", "num", e && e.kv_cache_pct !== null && e.kv_cache_pct !== undefined ? fmt1(e.kv_cache_pct) + "%" : "–"));
      tr.appendChild(el("td", "num", e ? `${fmtInt(e.requests_running ?? 0)} / ${fmtInt(e.requests_waiting ?? 0)}` : "–"));
      tr.appendChild(el("td", "num", e && e.gen_tps !== null && e.gen_tps !== undefined ? fmt1(e.gen_tps) : "–"));
      tr.appendChild(el("td", "num", e && e.avg_latency_s ? fmt1(e.avg_latency_s) + "s" : "–"));
      const ep = el("td");
      if (e) ep.appendChild(copyBtn(e.base_url, e.base_url, "Copy base URL"));
      tr.appendChild(ep);
      tb.appendChild(tr);
    });
  });
}

function render(data) {
  LAST = data;
  renderSummary(data);
  renderCards(data);
  renderTable(data);
  const f = data.fleet;
  $("#subtitle").textContent =
    `${f.nodes_total} machines · ${f.endpoints_total} vLLM endpoints · polling every ${data.poll_interval_s}s`;
  $("#footPoll").textContent =
    `last poll ${fmtAgo(data.generated_at)} · took ${data.poll_ms} ms · ${(data.fleet_history || []).length} history points`;
}

/* ---------------- data loop ---------------- */

let timer = null;

async function load() {
  try {
    const r = await fetch("api/fleet", { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();
    $("#footErr").textContent = "";
    $("#pulse").style.background = "var(--good)";
    render(data);
    $("#freshness").textContent = "updated " + fmtAgo(data.generated_at);
    if (timer) clearInterval(timer);
    timer = setInterval(() => {
      if (LAST) $("#freshness").textContent = "updated " + fmtAgo(LAST.generated_at);
    }, 1000);
    return data.poll_interval_s || 10;
  } catch (err) {
    $("#footErr").textContent = "dashboard server unreachable: " + err.message;
    $("#pulse").style.background = "var(--crit)";
    return 5;
  }
}

async function loop() {
  const wait = await load();
  setTimeout(loop, wait * 1000);
}

/* ---------------- controls ---------------- */

/* ---------------- admin unlock ----------------
   Everyone who can reach the host sees the dashboard. Controls that change
   server state (a forced poll) need a token, unlocked once per browser with
   ?admin=TOKEN and then remembered locally. */

let ADMIN = { admin: true, admin_required: false };

function adminToken() {
  return localStorage.getItem("fleet.admin") || "";
}

function adminHeaders() {
  const t = adminToken();
  return t ? { "X-Admin-Token": t } : {};
}

function captureAdminFromUrl() {
  const u = new URL(location.href);
  const t = u.searchParams.get("admin");
  if (t !== null) {
    if (t) localStorage.setItem("fleet.admin", t);
    else localStorage.removeItem("fleet.admin");   // ?admin= signs out
    u.searchParams.delete("admin");
    history.replaceState(null, "", u.pathname + u.search + u.hash);
  }
}

async function checkAdmin() {
  try {
    const r = await fetch("api/whoami", { headers: adminHeaders(), cache: "no-store" });
    ADMIN = await r.json();
  } catch {
    ADMIN = { admin: false, admin_required: true };
  }
  const btn = $("#refresh");
  btn.classList.toggle("hidden", !ADMIN.admin);
  $("#adminMark").classList.toggle("hidden", !(ADMIN.admin && ADMIN.admin_required));
}

function setView(v) {
  VIEW = v;
  localStorage.setItem("fleet.view", v);
  $("#cards").classList.toggle("hidden", v !== "cards");
  $("#tableview").classList.toggle("hidden", v !== "table");
  $("#viewCards").setAttribute("aria-pressed", String(v === "cards"));
  $("#viewTable").setAttribute("aria-pressed", String(v === "table"));
}

function initTheme() {
  const saved = localStorage.getItem("fleet.theme");
  if (saved === "dark" || saved === "light") document.documentElement.dataset.theme = saved;
  else document.documentElement.removeAttribute("data-theme");
}

$("#viewCards").addEventListener("click", () => setView("cards"));
$("#viewTable").addEventListener("click", () => setView("table"));
$("#theme").addEventListener("click", () => {
  const cur = document.documentElement.dataset.theme;
  const dark = cur ? cur === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  const next = dark ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("fleet.theme", next);
  if (LAST) render(LAST);
});
$("#refresh").addEventListener("click", async () => {
  const b = $("#refresh");
  b.disabled = true; b.textContent = "Refreshing…";
  try {
    const r = await fetch("api/refresh", { method: "POST", headers: adminHeaders() });
    if (r.status === 403) $("#footErr").textContent = "admin token rejected — open the dashboard with ?admin=YOUR_TOKEN";
  } catch {}
  await load();
  b.disabled = false; b.textContent = "Refresh";
});

captureAdminFromUrl();
initTheme();
setView(VIEW);
checkAdmin();
loop();
