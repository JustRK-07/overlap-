// app.js — vanilla JS for the overlay UI
//
// State machine:
//   IDLE  → START  (user clicked Start, files uploaded)
//          → POLL  (status polled every 500ms)
//          → AXES  (state=awaiting_axes, convention picker visible)
//          → DONE  (state=done, download button shown)
//          → FAILED

(() => {
  "use strict";

  const API = "/api/v1/overlay";
  let jobId = null;
  let pollTimer = null;

  // ─────────────────────────────────────────────────────────────────────
  // DOM refs
  // ─────────────────────────────────────────────────────────────────────
  const $ = (id) => document.getElementById(id);
  const els = {
    fileA: $("file-a"),
    fileB: $("file-b"),
    dropA: document.querySelector('[data-target="file-a"]'),
    dropB: document.querySelector('[data-target="file-b"]'),
    startBtn: $("start-btn"),
    uploadError: $("upload-error"),
    statusSection: $("status-section"),
    fileCards: $("file-cards"),
    stepRail: $("step-rail"),
    statusMessage: $("status-message"),
    axesSection: $("axes-section"),
    suggestions: $("suggestions"),
    prefixInput: $("prefix-input"),
    axesInput: $("axes-input"),
    tryConventionBtn: $("try-convention-btn"),
    axesResults: $("axes-results"),
    axesSummary: $("axes-summary"),
    confirmAxesBtn: $("confirm-axes-btn"),
    // Advanced policy panel
    policyPanel: $("policy-panel"),
    policyLabelRegex: $("policy-label-regex"),
    policyStrPrefixes: $("policy-str-prefixes"),
    policyMechPrefixes: $("policy-mech-prefixes"),
    policyMarkerLayer: $("policy-marker-layer"),
    policyAnnotationLayers: $("policy-annotation-layers"),
    policySaveBtn: $("policy-save-btn"),
    policySaveStatus: $("policy-save-status"),

    // Slice 2 — multi-file upload
    multiFileDetails: $("multi-file-details"),
    filesContainer: $("files-container"),
    addFileBtn: $("add-file-btn"),
    mfPrefix: $("mf-prefix"),
    mfAxes: $("mf-axes"),
    multiStartBtn: $("multi-start-btn"),
    multiUploadError: $("multi-upload-error"),
    multiUploadStatus: $("multi-upload-status"),
    doneSection: $("done-section"),
    transformDisplay: $("transform-display"),
    downloadLink: $("download-link"),
    errorSection: $("error-section"),
    errorMessage: $("error-message"),
    downloadAnnotatedLink: $("download-annotated-link"),
    downloadDocxLink: $("download-docx-link"),
    downloadSpotcheckJsonLink: $("download-spotcheck-json-link"),
    // Section 5 — depth analysis + issues panel
    analyzeSection: $("analyze-section"),
    analyzePairSelect: $("analyze-pair-select"),
    analyzeSummary: $("analyze-summary"),
    analyzeTbody: $("analyze-tbody"),
    analyzeIssuesCount: $("analyze-issues-count"),
    issuesTbody: $("issues-tbody"),
    issuesShowMore: $("issues-show-more"),
  };

  // ─── Analyze panel state ────────────────────────────────────────────
  let analyzeIssuesCache = null;  // sorted issues for the active pair
  const ISSUES_PAGE = 100;        // rows per "show more" click

  // Current selections
  let fileA = null;
  let fileB = null;
  let chosenAxes = null;     // {prefix, axes, a_found, b_found, all_matched}

  // ─────────────────────────────────────────────────────────────────────
  // Upload UI: drag/drop + click-to-browse
  // ─────────────────────────────────────────────────────────────────────
  function wireDrop(zone, fileInput, setter) {
    zone.addEventListener("dragover", (e) => {
      e.preventDefault();
      zone.classList.add("drag-over");
    });
    zone.addEventListener("dragleave", () => zone.classList.remove("drag-over"));
    zone.addEventListener("drop", (e) => {
      e.preventDefault();
      zone.classList.remove("drag-over");
      if (e.dataTransfer.files.length) {
        fileInput.files = e.dataTransfer.files;
        setter(fileInput.files[0]);
      }
    });
    fileInput.addEventListener("change", () => {
      if (fileInput.files.length) setter(fileInput.files[0]);
    });
  }

  function setFile(file, setter) {
    setter(file);
    if (!file) return;
    const zone = setter === setFileA ? els.dropA : els.dropB;
    zone.classList.add("has-file");
    let hint = zone.querySelector(".drop-filename");
    if (!hint) {
      hint = document.createElement("span");
      hint.className = "drop-filename";
      zone.appendChild(hint);
    }
    hint.textContent = `${file.name} (${(file.size / 1024 / 1024).toFixed(1)} MB)`;
    refreshStartEnabled();
  }
  function setFileA(f) { fileA = f; }
  function setFileB(f) { fileB = f; }

  wireDrop(els.dropA, els.fileA, (f) => setFile(f, setFileA));
  wireDrop(els.dropB, els.fileB, (f) => setFile(f, setFileB));

  function refreshStartEnabled() {
    els.startBtn.disabled = !(fileA && fileB);
  }

  // ─────────────────────────────────────────────────────────────────────
  // Start
  // ─────────────────────────────────────────────────────────────────────
  els.startBtn.addEventListener("click", async () => {
    els.uploadError.classList.add("hidden");
    const strategy = document.querySelector('input[name="strategy"]:checked').value;
    const cleanVersion = document.querySelector('input[name="clean_version"]:checked').value;
    const enabledChecks = Array.from(
      document.querySelectorAll('input[name="check"]:checked')
    ).map((c) => c.value);
    const fd = new FormData();
    fd.append("drawing_a", fileA);
    fd.append("drawing_b", fileB);
    fd.append("strategy", strategy);
    fd.append("clean_version", cleanVersion);
    fd.append("enabled_checks", enabledChecks.join(","));

    els.startBtn.disabled = true;
    els.startBtn.textContent = "Uploading...";

    try {
      const r = await fetch(API, { method: "POST", body: fd });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error(j.detail || `HTTP ${r.status}`);
      }
      const j = await r.json();
      jobId = j.job_id;
      // Reveal status section
      els.statusSection.classList.remove("hidden");
      // Show file cards
      els.fileCards.innerHTML = `
        <div class="file-card"><h4>Drawing A</h4>
          <div class="row"><span>${fileA.name}</span><span class="muted">${(fileA.size/1e6).toFixed(1)} MB</span></div>
        </div>
        <div class="file-card"><h4>Drawing B</h4>
          <div class="row"><span>${fileB.name}</span><span class="muted">${(fileB.size/1e6).toFixed(1)} MB</span></div>
        </div>`;
      // Start polling
      pollStatus();
    } catch (e) {
      els.uploadError.textContent = `Upload failed: ${e.message}`;
      els.uploadError.classList.remove("hidden");
      els.startBtn.disabled = false;
    } finally {
      els.startBtn.textContent = "Start Overlay";
    }
  });

  // ─────────────────────────────────────────────────────────────────────
  // Polling
  // ─────────────────────────────────────────────────────────────────────
  function pollStatus() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      try {
        const r = await fetch(`${API}/${jobId}/status`);
        if (!r.ok) return;
        const s = await r.json();
        renderStatus(s);
        if (s.state === "done" || s.state === "failed") {
          clearInterval(pollTimer);
          if (s.state === "done") loadDone();
        } else if (s.state === "awaiting_axes" && !chosenAxes) {
          loadAxes();
        }
      } catch (e) {
        // network blip — keep polling
      }
    }, 500);
  }

  function renderStatus(s) {
    // Update step rail
    const steps = els.stepRail.querySelectorAll("li");
    steps.forEach((el) => {
      const n = parseInt(el.dataset.step, 10);
      el.classList.remove("active", "done", "failed");
      el.querySelector(".step-state").textContent = "pending";
      if (s.current_step > n) {
        el.classList.add("done");
        el.querySelector(".step-state").textContent = "done";
      } else if (s.current_step === n) {
        if (s.state === "failed") {
          el.classList.add("failed");
          el.querySelector(".step-state").textContent = "failed";
        } else if (s.state === "awaiting_axes" && n === 5) {
          el.classList.add("active");
          el.querySelector(".step-state").textContent = "waiting";
        } else {
          el.classList.add("active");
          el.querySelector(".step-state").textContent = "running";
        }
      }
    });
    els.statusMessage.textContent = s.message || "(no message)";
    if (s.error) {
      els.errorSection.classList.remove("hidden");
      els.errorMessage.textContent = s.error;
    }
    // If we somehow reloaded during stage 5, the policy panel may need
    // to load even if loadAxes() was the trigger. Re-fetch on each
    // status poll once we're at the axes stage.
    if (s.state === "awaiting_axes" && els.policyLabelRegex
        && !els.policyLabelRegex.value) {
      fetch(`${API}/${jobId}/axes`).then(r => r.ok ? r.json() : null)
        .then(j => { if (j && j.clean_policy) renderPolicyPanel(j.clean_policy); })
        .catch(() => {});
    }
  }

  // ─────────────────────────────────────────────────────────────────────
  // Axes picker (stage 5)
  // ─────────────────────────────────────────────────────────────────────
  async function loadAxes() {
    els.axesSection.classList.remove("hidden");
    // Fetch suggested conventions from backend
    try {
      const r = await fetch(`${API}/${jobId}/axes`);
      if (!r.ok) return;
      const j = await r.json();
      renderSuggestions(j.suggestions || []);
      // Stage 3.5 also wrote a clean_policy — pre-fill the Advanced panel
      if (j.clean_policy) {
        renderPolicyPanel(j.clean_policy);
      }
    } catch (e) {
      els.suggestions.innerHTML = `<p class="muted">Could not load suggestions: ${e.message}</p>`;
    }
  }

  function renderPolicyPanel(p) {
    if (els.policyLabelRegex) els.policyLabelRegex.value = p.label_regex || "";
    if (els.policyStrPrefixes)
      els.policyStrPrefixes.value = (p.keep_block_prefixes_str || []).join(", ");
    if (els.policyMechPrefixes)
      els.policyMechPrefixes.value = (p.keep_block_prefixes_mech || []).join(", ");
    if (els.policyMarkerLayer) els.policyMarkerLayer.value = p.marker_layer || "";
    if (els.policyAnnotationLayers)
      els.policyAnnotationLayers.value = (p.annotation_layers || []).join(", ");
  }

  function readPolicyPanel() {
    return {
      label_regex: els.policyLabelRegex.value.trim(),
      keep_block_prefixes_str: els.policyStrPrefixes.value
        .split(",").map(s => s.trim()).filter(Boolean),
      keep_block_prefixes_mech: els.policyMechPrefixes.value
        .split(",").map(s => s.trim()).filter(Boolean),
      marker_layer: els.policyMarkerLayer.value.trim(),
      annotation_layers: els.policyAnnotationLayers.value
        .split(",").map(s => s.trim()).filter(Boolean),
    };
  }

  async function savePolicy() {
    els.policySaveStatus.textContent = "Saving…";
    els.policySaveStatus.className = "muted";
    const body = readPolicyPanel();
    try {
      const r = await fetch(`${API}/${jobId}/policy`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error(j.detail || `HTTP ${r.status}`);
      }
      els.policySaveStatus.textContent = "✓ saved";
      els.policySaveStatus.className = "saved";
    } catch (e) {
      els.policySaveStatus.textContent = `Error: ${e.message}`;
      els.policySaveStatus.className = "error";
    }
  }

  function renderSuggestions(sugs) {
    if (!sugs.length) {
      els.suggestions.innerHTML = `<p class="muted">No auto-detected patterns. Type your convention below.</p>`;
      return;
    }
    els.suggestions.innerHTML = sugs.map((s, i) => `
      <button class="suggestion" data-idx="${i}">
        <strong>"${s.prefix || '(no prefix)'}"</strong>
        <span class="samples">${s.count} found: ${s.samples.slice(0, 5).join(", ")}${s.count > 5 ? "..." : ""}</span>
      </button>`).join("");
    els.suggestions.querySelectorAll(".suggestion").forEach((btn) => {
      btn.addEventListener("click", () => {
        els.suggestions.querySelectorAll(".suggestion").forEach(b => b.classList.remove("selected"));
        btn.classList.add("selected");
        const idx = parseInt(btn.dataset.idx, 10);
        const sug = sugs[idx];
        els.prefixInput.value = sug.prefix;
        els.axesInput.value = sug.axes.join(" ");
        applyConvention();
      });
    });
  }

  els.tryConventionBtn.addEventListener("click", applyConvention);
  if (els.policySaveBtn) {
    els.policySaveBtn.addEventListener("click", savePolicy);
  }

  async function applyConvention() {
    const prefix = els.prefixInput.value;
    const axes = els.axesInput.value.trim().split(/\s+/).filter(Boolean);
    els.axesResults.classList.remove("hidden");
    els.axesSummary.innerHTML = `<p class="muted">Scanning...</p>`;
    try {
      const r = await fetch(`${API}/${jobId}/axes`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prefix, axes }),
      });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error(j.detail || `HTTP ${r.status}`);
      }
      const j = await r.json();
      renderAxesResult(j);
    } catch (e) {
      els.axesSummary.innerHTML = `<p class="error">Scan failed: ${e.message}</p>`;
    }
  }

  function renderAxesResult(j) {
    chosenAxes = j;
    els.axesSummary.innerHTML = `
      <div class="axes-summary">
        <div class="pair"><span>Drawing A</span><span>${j.a_found} / ${j.axes.length} axes</span></div>
        <div class="pair"><span>Drawing B</span><span>${j.b_found} / ${j.axes.length} axes</span></div>
        <div class="pair"><span>All paired?</span><span>${j.all_matched ? "✓ yes" : "✗ no — missing some"}</span></div>
        ${(j.pairs || []).map(p => `
          <div class="pair">
            <span><code>${p.suffix}</code>  ${p.type}</span>
            <span class="muted">Δ ${p.delta.toFixed(2)}</span>
          </div>
        `).join("")}
      </div>
    `;
    if (j.all_matched) {
      els.confirmAxesBtn.classList.remove("hidden");
    } else {
      els.confirmAxesBtn.classList.add("hidden");
    }
  }

  els.confirmAxesBtn.addEventListener("click", async () => {
    els.confirmAxesBtn.disabled = true;
    els.confirmAxesBtn.textContent = "Continuing...";
    try {
      // Save any policy edits first — so they apply to stage 4 cleanly.
      if (els.policySaveBtn) {
        await savePolicy();
      }
      await fetch(`${API}/${jobId}/axes/confirm`, { method: "POST" });
      els.axesSection.classList.add("hidden");
    } catch (e) {
      els.confirmAxesBtn.disabled = false;
      els.confirmAxesBtn.textContent = "Continue →";
    }
  });

  // ─────────────────────────────────────────────────────────────────────
  // Done (stage 9)
  // ─────────────────────────────────────────────────────────────────────
  async function loadDone() {
    try {
      const r = await fetch(`${API}/${jobId}`);
      const j = await r.json();
      els.doneSection.classList.remove("hidden");
      if (j.transform) {
        const t = j.transform;
        els.transformDisplay.innerHTML = `
          <div class="transform-block">
            x_m = ${t.scale_x.toFixed(6)} · x_s + ${t.tx.toFixed(4)}<br>
            y_m = ${t.scale_y.toFixed(6)} · y_s + ${t.ty.toFixed(4)}
          </div>
          <p class="muted">max residual: ${t.max_residual?.toFixed(4)} u  ·  rms: ${t.rms_residual?.toFixed(4)} u  ·  entities: ${j.outputs?.entity_count ?? "?"}</p>
        `;
      } else {
        els.transformDisplay.innerHTML = `<p class="muted">No transform available.</p>`;
      }
      els.downloadLink.href = `${API}/${jobId}/download`;
      els.downloadAnnotatedLink.href = `${API}/${jobId}/annotated.dxf`;
      els.downloadDocxLink.href = `${API}/${jobId}/spotcheck.docx`;
      els.downloadSpotcheckJsonLink.href = `${API}/${jobId}/spotcheck.json`;
      // Section 5 — populate the analyze panel after we know we have a
      // completed pipeline run. Failures here are non-fatal (the user
      // still has the legacy transforms + downloads).
      loadAnalyze().catch((e) => console.warn("loadAnalyze failed:", e));
    } catch (e) {
      els.errorSection.classList.remove("hidden");
      els.errorMessage.textContent = `Could not load done state: ${e.message}`;
    }
  }

  // ─────────────────────────────────────────────────────────────────────
  // Section 5 — Depth analysis + Issues panel
  // ─────────────────────────────────────────────────────────────────────
  // Cache for the active pair. Issues are reused across the
  // intersections table + the per-pair Issues sub-table.
  let _activePairId = null;

  function _fmt(n)   { return (n ?? 0).toLocaleString(); }
  function _fmtF(n) { return (n ?? 0).toFixed(2); }
  function _esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function _depthBucketClass(nd) {
    // Returns CSS class string. The depth bar gradient is fixed (in CSS)
    // — the cell background buckets it for quick scanning.
    if (nd < 0.25) return "depth-cell-0";
    if (nd < 0.50) return "depth-cell-25";
    if (nd < 0.75) return "depth-cell-50";
    if (nd < 1.00) return "depth-cell-75";
    return "depth-cell-100";
  }

  async function loadAnalyze() {
    const r = await fetch(`${API}/${jobId}/analyze`);
    if (!r.ok) {
      // Stage 6/7.5 not done yet, or job hasn't been run — skip silently.
      return;
    }
    const body = await r.json();

    // Two response shapes:
    //   • {pairs: [...]}    — Slice-1 multi-pair layout.
    //   • single payload    — legacy single-pair layout (pair_id="_legacy").
    const pairs = body.pairs ? body.pairs : [body];
    const validPairs = pairs.filter((p) => !p.error);

    if (validPairs.length === 0) {
      return;  // all pairs errored — keep the panel hidden.
    }

    // Populate the pair selector.
    els.analyzePairSelect.innerHTML = validPairs
      .map((p) => `<option value="${_esc(p.pair_id)}">${_esc(p.mech_file || "?")} vs ${_esc(p.str_file || "?")}</option>`)
      .join("");
    els.analyzePairSelect.onchange = () => {
      const pid = els.analyzePairSelect.value;
      const pair = validPairs.find((p) => p.pair_id === pid);
      if (pair) renderAnalyze(pair);
    };

    els.analyzeSection.classList.remove("hidden");
    renderAnalyze(validPairs[0]);
  }

  async function renderAnalyze(pair) {
    _activePairId = pair.pair_id;

    // ── summary line ──────────────────────────────────────────────
    const mech_diag = pair.bbox_diagonals?.mech ?? 0;
    const str_diag  = pair.bbox_diagonals?.str  ?? 0;
    const sev = pair.severity_breakdown || {};
    const sevStr = ["CRITICAL", "WARNING", "INFO"]
      .filter((k) => sev[k])
      .map((k) => `${k}=${sev[k]}`)
      .join(" · ");
    els.analyzeSummary.innerHTML = `
      ${pair.n_issues} issues${sevStr ? " · " + sevStr : ""} ·
      rms ${_fmtF(pair.rms_residual)} u ·
      max ${_fmtF(pair.max_residual)} u
    `;

    // ── intersections table ───────────────────────────────────────
    const ixRows = (pair.intersections || []).map((ix) => {
      const pct = (ix.normalized_depth * 100).toFixed(1);
      const barW = Math.max(2, Math.round(ix.normalized_depth * 60));
      const nearby = (pair.hotspots || []).find((h) => h.intersection === ix.label);
      const nearbyCount = nearby ? nearby.issue_count : 0;
      return `<tr class="${_depthBucketClass(ix.normalized_depth)}">
        <td><code>${_esc(ix.label)}</code></td>
        <td>${_fmtF(ix.mech && ix.mech[0])}</td>
        <td>${_fmtF(ix.mech && ix.mech[1])}</td>
        <td>${_fmtF(ix.residual)}</td>
        <td>
          <span class="depth-bar" style="width:${barW}px"></span>
          ${pct}%
        </td>
        <td>${nearbyCount}</td>
      </tr>`;
    }).join("");
    els.analyzeTbody.innerHTML = ixRows ||
      `<tr><td colspan="6" class="muted">No intersections in this pair.</td></tr>`;

    // ── issues sub-table ──────────────────────────────────────────
    // Fetch /spotcheck.json — that file is always present for completed
    // jobs (legacy layout at out/spotcheck.json, pair layout at
    // out/<pair>/out/spotcheck.json). We point at the legacy URL
    // because that endpoint reads the active pair's spotcheck.json
    // via the smoke-test convention.
    try {
      const r = await fetch(`${API}/${jobId}/spotcheck.json`);
      if (r.ok) {
        const j = await r.json();
        analyzeIssuesCache = (j.issues || []).slice();
        // Sort by severity (CRITICAL first), then by id for stability.
        const sevRank = { CRITICAL: 0, WARNING: 1, INFO: 2, "": 3 };
        analyzeIssuesCache.sort((a, b) => {
          const ra = sevRank[a.severity] ?? 9;
          const rb = sevRank[b.severity] ?? 9;
          return ra - rb || String(a.id).localeCompare(String(b.id));
        });
        els.analyzeIssuesCount.textContent = analyzeIssuesCache.length;
        renderIssuesPage(0);
      }
    } catch (e) {
      console.warn("issues fetch failed:", e);
    }
  }

  function renderIssuesPage(start) {
    const issues = analyzeIssuesCache || [];
    const slice = issues.slice(start, start + ISSUES_PAGE);
    if (start === 0) {
      els.issuesTbody.innerHTML = "";
    }
    const html = slice.map((i) => `
      <tr>
        <td><code>${_esc(i.id)}</code></td>
        <td>${_esc(i.type)}</td>
        <td><span class="severity sev-${_esc(i.severity)}">${_esc(i.severity)}</span></td>
        <td>${_fmtF(i.mech_x)}</td>
        <td>${_fmtF(i.mech_y)}</td>
        <td>${_fmtF(i.distance)}</td>
        <td>${_esc(i.note || "")}</td>
      </tr>
    `).join("");
    els.issuesTbody.insertAdjacentHTML("beforeend", html);

    // Show "Show more" only if there's another page.
    const nextStart = start + ISSUES_PAGE;
    if (nextStart < issues.length) {
      els.issuesShowMore.classList.remove("hidden");
      els.issuesShowMore.onclick = () => {
        renderIssuesPage(nextStart);
      };
    } else {
      els.issuesShowMore.classList.add("hidden");
    }
  }

  // ─────────────────────────────────────────────────────────────────────
  // Slice 2 — Multi-file upload (POST /upload)
  // ─────────────────────────────────────────────────────────────────────

  let mfRows = [];   // [{file, role}, ...]

  function addMultiFileRow(file, role) {
    if (mfRows.length >= 16) return;
    mfRows.push({ file, role });

    const row = document.createElement("div");
    row.className = "file-row";
    row.style.cssText = "display: grid; grid-template-columns: 1fr 130px 36px; gap: 8px; align-items: center; margin-bottom: 6px;";

    const fileInput = document.createElement("input");
    fileInput.type = "file";
    fileInput.accept = ".dwg,.dxf";
    if (file) {
      // Note: setting files on inputs programmatically requires DataTransfer; we
      // just show the name + size for now and rely on the file stored in mfRows.
      const dataTransfer = new DataTransfer();
      dataTransfer.items.add(file);
      fileInput.files = dataTransfer.files;
    }
    fileInput.addEventListener("change", (e) => {
      const f = e.target.files[0];
      const idx = mfRows.findIndex((r) => r._row === row);
      if (idx >= 0) mfRows[idx].file = f;
      refreshMultiStart();
    });

    const roleSelect = document.createElement("select");
    roleSelect.className = "select-base";
    ["mech", "str", "other"].forEach((opt) => {
      const o = document.createElement("option");
      o.value = opt;
      o.textContent = opt.toUpperCase();
      roleSelect.appendChild(o);
    });
    roleSelect.value = role;
    roleSelect.addEventListener("change", (e) => {
      const idx = mfRows.findIndex((r) => r._row === row);
      if (idx >= 0) mfRows[idx].role = e.target.value;
      refreshMultiStart();
    });

    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.textContent = "✕";
    removeBtn.className = "secondary";
    removeBtn.style.cssText = "padding: 4px 8px;";
    removeBtn.addEventListener("click", () => {
      const idx = mfRows.findIndex((r) => r._row === row);
      if (idx >= 0) mfRows.splice(idx, 1);
      row.remove();
      refreshMultiStart();
    });

    row.appendChild(fileInput);
    row.appendChild(roleSelect);
    row.appendChild(removeBtn);
    els.filesContainer.appendChild(row);

    mfRows[mfRows.length - 1]._row = row;
    refreshMultiStart();
  }

  function refreshMultiStart() {
    // Need ≥1 mech + ≥1 str, all rows have files.
    const allHaveFile = mfRows.length > 0 && mfRows.every((r) => r.file);
    const mechCount = mfRows.filter((r) => r.role === "mech").length;
    const strCount  = mfRows.filter((r) => r.role === "str").length;
    els.multiStartBtn.disabled = !(allHaveFile && mechCount >= 1 && strCount >= 1);
  }

  els.addFileBtn.addEventListener("click", () => addMultiFileRow(null, "other"));
  // Seed two rows by default (MECH + STR), matching the legacy UX.
  addMultiFileRow(null, "mech");
  addMultiFileRow(null, "str");

  els.multiStartBtn.addEventListener("click", async () => {
    els.multiUploadError.classList.add("hidden");
    els.multiUploadStatus.textContent = "Uploading...";
    const strategy = document.querySelector('input[name="strategy"]:checked').value;
    const cleanVersion = document.querySelector('input[name="clean_version"]:checked').value;
    const enabledChecks = Array.from(
      document.querySelectorAll('input[name="check"]:checked')
    ).map((c) => c.value);

    const fd = new FormData();
    const roles = [];
    for (const r of mfRows) {
      if (!r.file) continue;
      fd.append("files", r.file, r.file.name);
      roles.push(r.role);
    }
    fd.append("roles", roles.join(","));
    fd.append("strategy", strategy);
    fd.append("clean_version", cleanVersion);
    fd.append("enabled_checks", enabledChecks.join(","));
    fd.append("prefix", els.mfPrefix.value || "");
    fd.append("axes", els.mfAxes.value || "");

    els.multiStartBtn.disabled = true;
    els.multiStartBtn.textContent = "Uploading...";

    try {
      const r = await fetch(`${API}/upload`, { method: "POST", body: fd });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error(j.detail || `HTTP ${r.status}`);
      }
      const j = await r.json();
      jobId = j.job_id;
      els.multiUploadStatus.textContent =
        `Job ${j.job_id}: ${j.pair_count} pair(s) queued (${j.file_count} files).`;

      // Reuse the same status section + file cards as the legacy flow.
      els.statusSection.classList.remove("hidden");
      els.fileCards.innerHTML = mfRows.filter((r) => r.file).map((r, i) => `
        <div class="file-card"><h4>File ${i + 1} <span class="muted">(${r.role.toUpperCase()})</span></h4>
          <div class="row"><span>${r.file.name}</span><span class="muted">${(r.file.size/1e6).toFixed(1)} MB</span></div>
        </div>`).join("");

      pollStatus();
    } catch (e) {
      els.multiUploadError.textContent = `Upload failed: ${e.message}`;
      els.multiUploadError.classList.remove("hidden");
      els.multiUploadStatus.textContent = "";
    } finally {
      els.multiStartBtn.disabled = false;
      els.multiStartBtn.textContent = "Start Multi-file Overlay";
    }
  });
})();
