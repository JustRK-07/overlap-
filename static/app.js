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
    // Step 9 — stage options (shared by the legacy and N-file forms)
    fitModel: $("fit-model"),
    allowWeakFit: $("allow-weak-fit"),
    strLineweight: $("str-lineweight"),
    qaGrid: $("qa-grid"),
    doneSection: $("done-section"),
    transformDisplay: $("transform-display"),
    fitQualityDisplay: $("fit-quality-display"),
    previewLink: $("preview-link"),
    previewGridLink: $("preview-grid-link"),
    reportLink: $("report-link"),
    downloadLink: $("download-link"),
    errorSection: $("error-section"),
    errorMessage: $("error-message"),
    downloadAnnotatedLink: $("download-annotated-link"),
    downloadDocxLink: $("download-docx-link"),
    downloadSpotcheckJsonLink: $("download-spotcheck-json-link"),
    downloadFindingsJsonLink: $("download-findings-json-link"),
    downloadFindingsCsvLink: $("download-findings-csv-link"),
    downloadFindingsDocxLink: $("download-findings-docx-link"),
    // Section 5 — depth analysis + findings panel
    analyzeSection: $("analyze-section"),
    analyzePairSelect: $("analyze-pair-select"),
    analyzeSummary: $("analyze-summary"),
    analyzeTbody: $("analyze-tbody"),
    // the findings panel (PLAN_MISMATCH §4)
    findingsCount: $("findings-count"),
    findingsSummary: $("findings-summary"),
    findingsKindSelect: $("findings-kind-select"),
    findingsIncludeInfo: $("findings-include-info"),
    findingsIncludeSystematic: $("findings-include-systematic"),
    findingsCoverage: $("findings-coverage"),
    findingsTbody: $("findings-tbody"),
    findingsShowMore: $("findings-show-more"),
    findingsPreviewWrap: $("findings-preview-wrap"),
    findingsPreview: $("findings-preview"),
    findingsOverlay: $("findings-overlay"),
    findingsResetZoom: $("findings-reset-zoom"),
    findingsCopyCoords: $("findings-copy-coords"),
  };

  // ─── Analyze panel state ────────────────────────────────────────────

  // ─── Findings panel state ───────────────────────────────────────────
  let findingsCache = null;       // the findings for the active pair
  let findingsMeta = null;        // {png_w, png_h, world_to_px}
  let findingsOffset = 0;         // how many rows are already rendered
  const FINDINGS_PAGE = 100;      // rows per "show more" click
  const FINDINGS_LIMIT = 5000;    // the API's maximum

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
  // Step 9 — stage options
  // ─────────────────────────────────────────────────────────────────────
  // Read once by both upload forms; the server validates and echoes them
  // back, and stores them in job.options for the fit / overlay stages.
  function appendOptions(fd) {
    const colorMode = document.querySelector('input[name="color_mode"]:checked').value;
    const lineweight = els.strLineweight ? els.strLineweight.value.trim() : "";
    fd.append("color_mode", colorMode);
    fd.append("fit_model", els.fitModel ? els.fitModel.value : "similarity");
    fd.append("allow_weak_fit", String(!!(els.allowWeakFit && els.allowWeakFit.checked)));
    fd.append("str_lineweight", lineweight);       // "" = as drawn
    fd.append("qa_grid", String(!(els.qaGrid && !els.qaGrid.checked)));
  }

  // ─────────────────────────────────────────────────────────────────────
  // Start
  // ─────────────────────────────────────────────────────────────────────
  els.startBtn.addEventListener("click", async () => {
    els.uploadError.classList.add("hidden");
    const cleanVersion = document.querySelector('input[name="clean_version"]:checked').value;
    const enabledChecks = Array.from(
      document.querySelectorAll('input[name="check"]:checked')
    ).map((c) => c.value);
    const fd = new FormData();
    fd.append("drawing_a", fileA);
    fd.append("drawing_b", fileB);
    fd.append("clean_version", cleanVersion);
    fd.append("enabled_checks", enabledChecks.join(","));
    appendOptions(fd);

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
        <div class="file-card"><h4>MECH drawing</h4>
          <div class="row"><span>${fileA.name}</span><span class="muted">${(fileA.size/1e6).toFixed(1)} MB</span></div>
        </div>
        <div class="file-card"><h4>STR drawing</h4>
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
      els.suggestions.innerHTML = `<p class="muted">Could not load suggestions: ${_esc(e.message)}</p>`;
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
        <strong>"${_esc(s.prefix || '(no prefix)')}"</strong>
        <span class="samples">${_esc(String(s.count))} found: ${_esc(s.samples.slice(0, 5).join(", "))}${s.count > 5 ? "..." : ""}</span>
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
      els.axesSummary.innerHTML = `<p class="error">Scan failed: ${_esc(e.message)}</p>`;
    }
  }

  function renderAxesResult(j) {
    chosenAxes = j;
    // `labels` is the per-label grid-detection verdict (step 9). Older
    // servers don't return it — fall back to just the delta summary.
    const labelRows = (j.labels || []).map((l) => {
      const mech = l.mech || {};
      const str = l.str || {};
      const badge = (st) => {
        const cls = st.status === "ok" ? "ok" : (st.status === "ambiguous" ? "warn" : "bad");
        const why = (st.reasons || []).join("; ");
        return `<span class="axis-status ${cls}" title="${_esc(why)}">${_esc(st.status)}</span>`;
      };
      return `
        <div class="pair">
          <span><code>${_esc(l.suffix)}</code> ${_esc(l.type || "")}</span>
          <span>MECH ${badge(mech)} · STR ${badge(str)}</span>
        </div>`;
    }).join("");

    els.axesSummary.innerHTML = `
      <div class="axes-summary">
        <div class="pair"><span>MECH drawing</span><span>${j.a_found} / ${j.axes.length} axes</span></div>
        <div class="pair"><span>STR drawing</span><span>${j.b_found} / ${j.axes.length} axes</span></div>
        <div class="pair"><span>All paired?</span><span>${j.all_matched ? "✓ yes" : "✗ no — missing some"}</span></div>
        ${(j.pairs || []).map(p => `
          <div class="pair">
            <span><code>${_esc(p.suffix)}</code>  ${_esc(p.type)}</span>
            <span class="muted">Δ ${p.delta.toFixed(2)}</span>
          </div>
        `).join("")}
        ${labelRows}
      </div>
      ${(j.problems || []).length ? `
        <p class="muted">Not every axis resolved:
          ${j.problems.map(p => `<code>${_esc(p.suffix)}</code> (${_esc(p.source)}: ${_esc(p.status)})`).join(", ")}.
          The fit only uses the axes that resolved in both drawings.</p>` : ""}
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

  // The fit's quality section: what the transform actually is, how well it
  // agreed with the grid, and every warning the gates raised.
  function renderFitQuality(t) {
    if (!t || t.model === undefined) {          // pre-step-5 job: no quality
      els.fitQualityDisplay.innerHTML = "";
      return;
    }
    const q = t.quality || {};
    const status = q.status || "?";
    const cls = status === "forced" ? "warn" : (status === "ok" ? "ok" : "bad");
    const outliers = t.outliers || [];
    const rows = [
      ["Transform model", t.model + (t.mirrored ? " (mirrored)" : "")],
      ["Scale", _fmtF(t.scale)],
      ["Rotation", `${_fmtF(t.rotation_deg)}°`],
      ["Inliers", `${t.n_inliers ?? "?"} / ${t.n_total ?? "?"}`],
      ["RMS residual", `${_fmtF(t.rms_residual)} u`],
      ["Max residual", `${_fmtF(t.max_residual)} u`],
      ["Leave-one-out max", t.loo_max == null ? "n/a" : `${_fmtF(t.loo_max)} u`],
      ["Outlier labels", outliers.length ? outliers.join(", ") : "none"],
    ].map(([k, v]) => `<div class="pair"><span>${_esc(k)}</span><span>${_esc(v)}</span></div>`).join("");

    const warnings = (q.warnings || []).length
      ? `<ul class="muted fit-warnings">${q.warnings.map((w) => `<li>${_esc(w)}</li>`).join("")}</ul>`
      : "";
    els.fitQualityDisplay.innerHTML = `
      <h3 style="font-size: 1em; margin: 1em 0 0.4em;">Fit quality
        <span class="axis-status ${cls}">${_esc(status)}</span></h3>
      <div class="axes-summary">${rows}</div>
      ${warnings}
    `;
  }

  // How many STR entities the overlay could not transform (exploded or
  // dropped). A non-zero count is not fatal but is worth showing.
  async function loadOverlayFailures() {
    try {
      const r = await fetch(`${API}/${jobId}/report.json`);
      if (!r.ok) return;
      const rep = await r.json();
      const ov = rep.overlay || {};
      const n = (ov.failures || []).length;
      if (!n) return;
      const line = `<p class="muted">Overlay: ${n} STR entit${n === 1 ? "y" : "ies"} could not be
        transformed (${ov.dropped || 0} dropped, ${ov.exploded || 0} exploded) —
        see report.json → overlay.failures.</p>`;
      const holder = els.fitQualityDisplay;
      holder.insertAdjacentHTML("beforeend", line);
    } catch (e) {
      console.warn("report fetch failed:", e);
    }
  }

  async function loadDone() {
    try {
      const r = await fetch(`${API}/${jobId}`);
      const j = await r.json();
      els.doneSection.classList.remove("hidden");
      if (j.transform) {
        const t = j.transform;
        // New fits report the real model (scale/rotation/mirror) plus quality;
        // old jobs only have the axis-aligned scale_x/scale_y/tx/ty shape.
        const isSimilarity = t.model !== undefined;
        const head = isSimilarity
          ? `<div class="transform-block">
               scale ${t.scale?.toFixed(6)} · rotation ${t.rotation_deg?.toFixed(4)}°${t.mirrored ? " · mirrored" : ""}<br>
               tx ${t.tx?.toFixed(4)} · ty ${t.ty?.toFixed(4)}
             </div>`
          : `<div class="transform-block">
               x_m = ${t.scale_x.toFixed(6)} · x_s + ${t.tx.toFixed(4)}<br>
               y_m = ${t.scale_y.toFixed(6)} · y_s + ${t.ty.toFixed(4)}
             </div>`;
        const units = (t.units && t.units.expected_scale != null)
          ? `<p class="muted">$INSUNITS says 1:${_fmtF(t.units.expected_scale)}
             (STR ${t.units.str} → MECH ${t.units.mech})</p>`
          : "";
        els.transformDisplay.innerHTML = `${head}${units}`;
        renderFitQuality(t);
      } else {
        els.transformDisplay.innerHTML = `<p class="muted">No transform available.</p>`;
        els.fitQualityDisplay.innerHTML = "";
      }
      els.downloadLink.href = `${API}/${jobId}/download`;
      els.downloadAnnotatedLink.href = `${API}/${jobId}/annotated.dxf`;
      els.downloadDocxLink.href = `${API}/${jobId}/spotcheck.docx`;
      els.downloadSpotcheckJsonLink.href = `${API}/${jobId}/spotcheck.json`;
    if (els.downloadFindingsJsonLink) {
      els.downloadFindingsJsonLink.href = `${API}/${jobId}/findings.json`;
      els.downloadFindingsCsvLink.href = `${API}/${jobId}/findings.csv`;
      els.downloadFindingsDocxLink.href = `${API}/${jobId}/findings.docx`;
    }
      els.previewLink.href = `${API}/${jobId}/preview.png`;
      els.previewGridLink.href = `${API}/${jobId}/preview_grid.png`;
      els.reportLink.href = `${API}/${jobId}/report.json`;
      loadOverlayFailures().catch((e) => console.warn("loadOverlayFailures failed:", e));
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

    // ── the findings panel ─────────────────────────────────────────
    // The canonical findings (native mismatch findings + the remaining
    // spot-check issues) replace the old per-pair issues table.
    await loadFindings();
  }

  // ─────────────────────────────────────────────────────────────────────
  // Section 5b — the findings panel (PLAN_MISMATCH §4)
  // ─────────────────────────────────────────────────────────────────────

  async function loadFindings() {
    try {
      const r = await fetch(
        `${API}/${jobId}/findings?limit=${FINDINGS_LIMIT}`);
      if (!r.ok) {
        // The mismatch stage did not run (or was disabled) — say so
        // instead of showing an empty table that reads like "no defects".
        findingsCache = null;
        els.findingsCount.textContent = "—";
        els.findingsSummary.textContent =
          r.status === 404 ? "no findings were produced for this job"
                           : "the findings could not be loaded";
        els.findingsTbody.innerHTML = "";
        els.findingsCoverage.innerHTML = "";
        return;
      }
      const body = await r.json();
      findingsCache = body.items || [];
      findingsOffset = 0;

      // the coverage banner — a family with 0 items was NOT checked
      const cov = body.coverage || {};
      const zeros = Object.entries(cov)
        .filter(([, c]) => (c.items || 0) === 0)
        .map(([name]) => name);
      const parts = Object.entries(cov).map(
        ([name, c]) => `${name} ${c.matched || 0}/${c.items || 0}`);
      els.findingsCoverage.innerHTML =
        (parts.length ? `<span class="muted">checked: ${_esc(parts.join(" · "))}</span>` : "") +
        (zeros.length
          ? `<span class="findings-warn"> ⚠ ${_esc(zeros.join(", "))} matched no items — not checked</span>`
          : "");
      if (body.status !== "ok") {
        els.findingsCoverage.innerHTML +=
          `<span class="findings-warn"> ⚠ the mismatch check was ${_esc(body.status)}</span>`;
      }

      // the kind filter
      const kinds = [...new Set(findingsCache.map((f) => f.kind))].sort();
      els.findingsKindSelect.innerHTML =
        `<option value="">All kinds</option>` +
        kinds.map((k) => `<option value="${_esc(k)}">${_esc(k)}</option>`).join("");

      const bySev = body.summary?.by_severity || {};
      els.findingsSummary.textContent =
        `${body.total} finding(s)` +
        (bySev.CRITICAL ? ` · ${bySev.CRITICAL} critical` : "") +
        (bySev.WARNING ? ` · ${bySev.WARNING} warning` : "") +
        (bySev.INFO ? ` · ${bySev.INFO} info` : "");

      // the preview + the world → pixel map (for the click-to-locate ring)
      await loadFindingsPreview();

      renderFindings();
    } catch (e) {
      console.warn("findings fetch failed:", e);
    }
  }

  async function loadFindingsPreview() {
    try {
      const r = await fetch(`${API}/${jobId}/preview_meta.json`);
      if (r.ok) {
        findingsMeta = await r.json();
        els.findingsPreview.src = `${API}/${jobId}/preview.png`;
        els.findingsPreviewWrap.classList.remove("hidden");
        const w = findingsMeta.png_w || 1000;
        const h = findingsMeta.png_h || 600;
        els.findingsOverlay.setAttribute("viewBox", `0 0 ${w} ${h}`);
      }
    } catch (e) {
      console.warn("preview_meta fetch failed:", e);
    }
  }

  function _visibleFindings() {
    const all = findingsCache || [];
    const kind = els.findingsKindSelect.value;
    const includeInfo = els.findingsIncludeInfo.checked;
    const includeSystematic = els.findingsIncludeSystematic.checked;
    return all.filter((f) => {
      if (kind && f.kind !== kind) return false;
      if (!includeInfo && f.severity === "INFO") return false;
      if (!includeSystematic && f.systematic) return false;
      return true;
    });
  }

  function renderFindings() {
    const items = _visibleFindings();
    els.findingsCount.textContent = items.length;
    els.findingsTbody.innerHTML = "";
    findingsOffset = 0;
    renderFindingsPage(0);
  }

  function renderFindingsPage(start) {
    const items = _visibleFindings();
    const slice = items.slice(start, start + FINDINGS_PAGE);
    const html = slice.map((f) => {
      const loc = f.location || [0, 0];
      const off = f.offset_mm;
      const mech = f.mech_ref;
      const strc = f.str_ref;
      return `<tr data-idx="${findingsCache.indexOf(f)}" title="click to locate on the preview">
        <td><code>${_esc(f.id)}</code></td>
        <td>${_esc(f.kind)}</td>
        <td><span class="severity sev-${_esc(f.severity)}">${_esc(f.severity)}</span></td>
        <td>${_fmtF(loc[0])}</td>
        <td>${_fmtF(loc[1])}</td>
        <td>${off ? `${_fmtF(off[0])}, ${_fmtF(off[1])}` : "—"}</td>
        <td>${f.local_magnitude_mm != null ? _fmtF(f.local_magnitude_mm) : "—"}</td>
        <td>${_fmtF(f.tolerance_mm)}</td>
        <td>${f.ratio != null ? _fmtF(f.ratio) : "—"}</td>
        <td>${mech ? `${_esc(mech.block || mech.handle || "?")}<br><span class="muted">${_esc(mech.layer || "")}</span>` : "—"}</td>
        <td>${strc ? `${_esc(strc.block || strc.handle || "?")}<br><span class="muted">${_esc(strc.layer || "")}</span>` : "—"}</td>
        <td>${_esc(f.message || "")}</td>
      </tr>`;
    }).join("");
    els.findingsTbody.insertAdjacentHTML("beforeend", html);

    // row click → the ring on the preview
    els.findingsTbody.querySelectorAll("tr[data-idx]").forEach((tr) => {
      tr.addEventListener("click", () => {
        const f = findingsCache[Number(tr.dataset.idx)];
        if (f) focusFinding(f);
      });
    });

    const nextStart = start + FINDINGS_PAGE;
    if (nextStart < items.length) {
      els.findingsShowMore.classList.remove("hidden");
      els.findingsShowMore.onclick = () => renderFindingsPage(nextStart);
    } else {
      els.findingsShowMore.classList.add("hidden");
    }
  }

  function focusFinding(f) {
    if (!findingsMeta || !findingsMeta.world_to_px) return;
    const loc = f.location || [0, 0];
    const m = findingsMeta.world_to_px;
    const px = m[0][0] * loc[0] + m[0][2];
    const py = m[1][1] * loc[1] + m[1][2];
    const r = 18;
    els.findingsOverlay.innerHTML =
      `<circle cx="${px}" cy="${py}" r="${r}" fill="none" stroke="#dc2626" stroke-width="3"/>` +
      `<circle cx="${px}" cy="${py}" r="3" fill="#dc2626"/>`;
    // zoom the preview onto the finding
    const wrap = els.findingsPreviewWrap;
    const scale = 4;
    const rect = els.findingsPreview.getBoundingClientRect();
    const pw = findingsMeta.png_w || rect.width;
    const ph = findingsMeta.png_h || rect.height;
    const cx = (px / pw) * 100;
    const cy = (py / ph) * 100;
    els.findingsPreview.style.transformOrigin = `${cx}% ${cy}%`;
    els.findingsPreview.style.transform = `scale(${scale})`;
    els.findingsResetZoom.classList.remove("hidden");
    els.findingsCopyCoords.classList.remove("hidden");
    els.findingsCopyCoords.onclick = () => {
      const text = `${loc[0].toFixed(1)}, ${loc[1].toFixed(1)}`;
      if (navigator.clipboard) navigator.clipboard.writeText(text);
    };
  }

  function resetFindingsZoom() {
    els.findingsPreview.style.transform = "";
    els.findingsOverlay.innerHTML = "";
    els.findingsResetZoom.classList.add("hidden");
    els.findingsCopyCoords.classList.add("hidden");
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

  // ── the findings panel controls ──────────────────────────────────
  if (els.findingsKindSelect) {
    els.findingsKindSelect.onchange = () => renderFindings();
    els.findingsIncludeInfo.onchange = () => renderFindings();
    els.findingsIncludeSystematic.onchange = () => renderFindings();
    els.findingsResetZoom.onclick = () => resetFindingsZoom();
  }

  els.addFileBtn.addEventListener("click", () => addMultiFileRow(null, "other"));
  // Seed two rows by default (MECH + STR), matching the legacy UX.
  addMultiFileRow(null, "mech");
  addMultiFileRow(null, "str");

  els.multiStartBtn.addEventListener("click", async () => {
    els.multiUploadError.classList.add("hidden");
    els.multiUploadStatus.textContent = "Uploading...";
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
    fd.append("clean_version", cleanVersion);
    fd.append("enabled_checks", enabledChecks.join(","));
    fd.append("prefix", els.mfPrefix.value || "");
    fd.append("axes", els.mfAxes.value || "");
    appendOptions(fd);

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
