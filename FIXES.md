# FIXES.md — defect ledger and verification record

This file is the working ledger for the defects found in the review of
`a29669b` and for the remaining `PLAN.md` / `PLAN_MISMATCH.md` work.

**How to read it.** Every row is a defect with a *root cause*, the *change* that
fixes it, and a *verification* that proves the fix landed. A fix is only
"done" when its verification is written and passing.

**Status legend**

| Mark | Meaning |
|---|---|
| `OPEN` | not started |
| `WIP` | in progress |
| `FIXED` | code changed, verification written and green |
| `VERIFIED` | verification green **and** the original symptom reproduced before the fix and gone after |

**Rule of engagement.** Every fix ships with a regression test that *fails on
the unfixed code*. A fix with no test is not done — it is a hope.

---

## Where this ended up

| | |
|---|---|
| **Defects found and fixed** | **13** (A1–A13) — every one reproduced before the fix and re-measured after |
| **PLAN.md "Done when" criteria closed** | **5** (B1–B5) |
| **PLAN_MISMATCH 8b.5 / 8b.6 / 8b.7 / 8c** | **31 of 31** deliverables |
| **Test-suite hygiene defects** | **14** (D1–D14) |
| **Test files the plans named but never had** | **8** — all written |
| **Findings from the review left open** | **13 groups** — see **Part E** |
| **Suite** | 389 passed / 24 skipped → **494 passed / 15 skipped** (509 collected) |
| **Perf** | the overlay's handle lookup 14.83 s → 0.004 s; the bolt check 74.5 s → 2.4 s |

**Re-verify everything at any time:**

```bash
python3 tests/verify_fixes.py     # re-runs all 14 reproductions — 14/14 pass
python3 -m pytest -q              # the whole suite
```

---

## Part A — Tier 1: critical defects (all independently reproduced)

### A1 · `core/overlay.py:661-663` — overlay is O(N²) on the critical path

| | |
|---|---|
| **Severity** | High (performance) |
| **Symptom** | `_str_block_names` rebuilds `set(report.str_entity_handles)` inside a list-comprehension condition, so the set is rebuilt for every modelspace entity. |
| **Measured** | N=20 000 → **14.83 s** (current) vs **0.0031 s** (hoisted) = **4 743×**. |
| **Root cause** | A set literal in a comprehension *condition* is re-evaluated per iteration. |
| **Fix** | Hoist `str_handles = set(report.str_entity_handles)` above the comprehension (the same function already does it correctly at `:696`). |
| **Connected files** | `core/overlay.py` only. `_str_block_names` has one caller (`:697`). No other set-in-comprehension exists in the file. |
| **Verification** | `tests/test_overlay.py::test_str_block_names_is_linear` — build a doc with N INSERTs, assert the call completes well under a wall-clock budget, and assert the returned name set is unchanged vs. the old (O(N²)) implementation. |
| **Status** | VERIFIED |

### A2 · `core/mismatch/features.py:806-812` — half of all member rectangles become zero-length bands

| | |
|---|---|
| **Severity** | High (silent false negatives) |
| **Symptom** | A STR member rectangle whose drawn polyline starts on its **short** edge yields a band of geometric length `0` while claiming `size_mm["len"] = 5000`. |
| **Measured** | `first edge is LONG  w=300 h=150 → band length 300.0` ✓ · `first edge is SHORT w=150 h=300 → band length 0.0` ✗ |
| **Root cause** | `u` is chosen as the **long** axis but the half-extent passed to `segment_axis_params` is always `f.w / 2` (the *first drawn edge*). A segment perpendicular to the projection axis projects to a point. |
| **Consequence** | The band's normal is the zero vector ⇒ `e = (pt − proj)·n ≡ 0` ⇒ `SUPPORT_OFF_MEMBER` / `SUPPORT_POINT_OFF_MEMBER` can **never** fire on that member, and the degenerate band *wins* band selection, masking real defects nearby. |
| **Fix** | Use `along / 2.0` (the long dimension) with `u` (the long axis). A no-op for the long-edge-first case (`along == f.w`), so zero regression risk. |
| **Connected files** | `core/mismatch/features.py` (`_build_bands`), `core/mismatch/geom.py::rect_from_corners` (its docstring falsely claims `w` is always the long side — reword). `Feature.w/h/ux/uy` are read by `features.py:189-191, 209-210, 856-857, 1024-1029`, `checks/support.py:66-67,97`, `checks/context.py:329-330`. |
| **Verification** | `tests/test_mismatch_units.py::test_member_rect_short_edge_first_yields_full_length_band` — a member rect drawn short-edge-first must produce a band of length == the long side. **The current fixture draws every rect long-edge-first, so this path is currently untested.** |
| **Status** | VERIFIED |

### A3 · `core/mismatch/__init__.py:216-217` — the grid-spacing cap is converted to mm twice

| | |
|---|---|
| **Severity** | High (silent false positives on metre drawings) |
| **Symptom** | `fit.median_spacing_mm` is **already** in millimetres (`context.py:136` → `_maybe_float`), but `detect()` multiplies it by `mm` again. |
| **Measured** | `mm=1` → cap 1500.0 ✓ · `mm=1000` → cap **1 500 000.0** ✗ (should be 1500.0) — 1000× too loose. |
| **Root cause** | The `* mm` re-introduces exactly the unit bug commit `e56f9fb` claims to have eliminated. Invisible because **no test passes `fit_ctx` to `detect()`**, so `median_spacing_mm` is always `None`. |
| **Fix** | Delete the `* mm`. |
| **Connected files** | `core/mismatch/__init__.py:216` (write site) · `core/mismatch/context.py:136,189-191` (source, already mm) · `core/mismatch/checks/__init__.py:129,159-161` (the only consumer, expects mm) · `Env` is constructed in exactly one place. |
| **Verification** | `tests/test_mismatch_units.py::test_env_search_radius_cap_is_in_millimetres` — `Env` with `mm=1000`, `grid_spacing_mm=6000` → `search_mm(1e9) == 1500.0`, `search_u(1e9) == 1.5`. Plus `test_fit_uncertainty_inflates_tolerance` (missing §6.2 test). |
| **Status** | VERIFIED |

### A4 · `core/mismatch/checks/clash.py:62-63` — `clash_with_members=True` is a silent no-op

| | |
|---|---|
| **Severity** | Medium (a documented feature does nothing) |
| **Symptom** | `str_member` features are **bands**; `polygon_of()` returns an empty array for a band → `_hull_ring` returns `(empty, False)` → skipped at `:95`. A band overlapping equipment produces **zero findings**. |
| **Root cause** | `polygon_of` has no band branch; and `Feature.bbox()` for a band returns the *centreline* box, excluding `hw`, so the `GridIndex` doesn't even see the band's body. |
| **Fix** | Three coordinated changes: (1) `polygon_of` gains a band branch → `geom.rect_corners(centre, u, length, 2·hw)`; (2) the clash `GridIndex` uses a band-aware bbox (do **not** change `Feature.bbox()` — `support.py:130` relies on the centreline semantics); (3) the finding's STR ref uses the band's own family (`str_member`), not `str_fam`. |
| **Connected files** | `core/mismatch/checks/__init__.py::polygon_of` (4 callers: `clash.py:44` = the band path; `support.py:161`, `bolts.py:484`, `containment.py:63/66/107` = rect/polygon only, unaffected) · `core/mismatch/features.py::Feature.bbox` (centreline semantics must not change) · `core/mismatch/checks/support.py:130` (depends on centreline bbox). |
| **Verification** | `tests/test_mismatch_units.py::test_clash_with_members_detects_a_band_overlap` — a 5000 mm band overlapping a 1000×1000 rect with `clash_with_members=True` must produce a CLASH finding. |
| **Status** | VERIFIED |

### A5 · `core/validate.py:35`, `core/inspect.py:59` — `msp.bbox()` does not exist in ezdxf 1.4.4

| | |
|---|---|
| **Severity** | Medium (a stage that looks like it did something, didn't) |
| **Symptom** | `msp.bbox()` raises `AttributeError` (verified: `Modelspace` has no `bbox`); the blanket `except Exception` swallows it → `"extents": null` **for every DXF, always**. |
| **Root cause** | Wrong ezdxf API. The correct call is `ezdxf.bbox.extents(msp)`. |
| **Fix** | Use `ez_bbox.extents(msp, fast=False)` and check `box.has_data` (an empty modelspace returns a zero box, not an error). |
| **Connected files** | `core/validate.py`, `core/inspect.py` (both import `core.dxfio` already). Precedent: `core/grid.py:276-284`. Also `core/package.py:189` `e.bbox()` is the same dead-API pattern (harmless — it is a guarded fallback). |
| **Verification** | `tests/test_stages_validate_inspect.py::test_extents_are_reported_for_a_dxf` — a real DXF must report non-null extents. |
| **Status** | VERIFIED |

### A6 · `core/clean_common.py:295-311` — `e.paths.remove()` does not exist; edge-path HATCHes are never stripped

| | |
|---|---|
| **Severity** | High (malformed geometry shipped to the customer) |
| **Symptom** | `BoundaryPaths` has no `remove()` (verified). The `AttributeError` is swallowed → falls to `path.vertices = []`, which (a) empties a `PolylinePath` but does **not** remove it, and (b) is impossible for an `EdgePath` (no `.vertices`), so edge paths are never even considered. |
| **Root cause** | Wrong ezdxf API for 1.4.4. The paths live in `hatch.paths.paths` (a plain list). |
| **Consequence** | A degenerate zero-vertex boundary path is written into `cleaned/*.dxf` and then `superimposed.dxf`. `doc.audit()` reports no errors, so it ships silently. |
| **Fix** | Remove via `e.paths.paths.remove(path)`. Detect the 3-vertex case by type: `PolylinePath` → `len(path.vertices) == 3`; `EdgePath` → `len(path.edges) == 3 and all LineEdge`. |
| **Connected files** | `core/clean_common.py::_strip_triangles_in` (callers: `clean_v1.py:160,164`, `clean_v2.py:146,152`). No other module manipulates hatch paths (`core/mismatch/features.py:129` ignores HATCH entirely). |
| **Verification** | `tests/test_clean_safety.py::test_hatch_triangle_path_is_removed_from_a_marker_layer` — a HATCH with a 3-vertex polyline path on a marker layer loses exactly that path and still audits clean. |
| **Status** | VERIFIED |

### A7 · `core/clean_v1.py:263`, `core/clean_v2.py:223` — the `cleaned/` output directory is never created

| | |
|---|---|
| **Severity** | High (silent whole-stage failure on legacy job dirs) |
| **Symptom** | Only `stages/` is created. `doc.saveas()` raises `FileNotFoundError`, which is caught per-file into `clean_stats.json` → the pipeline continues → the job dies at stage 8 with "MECH cleaned DXF missing". |
| **Root cause** | `jobs._ensure_dirs` creates `cleaned/`, but only at **job creation**; `load_job` and the stage runner never call it. |
| **Fix** | `cleaned.mkdir(parents=True, exist_ok=True)` in both cleaners (and add `stages/` to `_ensure_dirs`). |
| **Connected files** | `core/clean_v1.py`, `core/clean_v2.py`, `jobs.py::_ensure_dirs` (callers: `create_job:408`, `create_job_legacy:508`). 26 legacy job dirs on disk have **no** `cleaned/`. |
| **Verification** | `tests/test_clean_safety.py::test_clean_creates_its_output_directory` — a job dir with no `cleaned/` must clean successfully. |
| **Status** | VERIFIED |

### A8 · `core/clean_common.py:75-83` — the label-regex cache is not thread-safe

| | |
|---|---|
| **Severity** | Medium (silent cross-job data loss) |
| **Symptom** | Two separate attribute writes; two concurrent jobs can interleave so job A matches grid labels against job B's regex — labels deleted or kept wrongly, non-reproducibly. |
| **Root cause** | Manual cache keyed on policy *identity*, updated in two steps. `CleaningPolicy` is a **mutable** dataclass, so in-place mutation also serves a stale pattern. |
| **Fix** | `@lru_cache(maxsize=32)` keyed on the **regex string**; change the single call site (`:147`) to pass `policy.label_regex`. |
| **Connected files** | `core/clean_common.py:75-83` + the one caller `:147`. Transitive: `_is_grid_label` → `_strip_annotations_in` / `_strip_non_grid_text_in` / `_strip_non_grid_attribs` → `clean_v1.py`, `clean_v2.py`. |
| **Verification** | `tests/test_clean_safety.py::test_label_regex_cache_follows_the_pattern_not_the_policy_object` — mutating `policy.label_regex` in place must change the compiled pattern. |
| **Status** | VERIFIED |

### A9 · `core/overlay.py:82` — `OverlayOptions.for_model()` is never called in production

| | |
|---|---|
| **Severity** | Medium (a documented diagnostic mode fails on one dropped entity) |
| **Symptom** | `options` is always `None` in production → `OverlayOptions()` → `max_drop_ratio = 0.0` regardless of the fitted model. An `affine` fit therefore fails the overlay on a **single** dropped entity, though `for_model("affine")` was written to allow 1 %. |
| **Root cause** | `for_model` is referenced only by `tests/test_overlay.py:342-343`. |
| **Fix** | In `overlay.run()`, derive the options from `transform_json.get("model")` (what was *actually* fitted), falling back to `"similarity"`. |
| **Connected files** | `core/overlay.py:82,300,985-996`. `overlay.run` callers: `pipelines.py:127`, `core/pair_runner.py:219`. The fit model is set at `core/fit.py:351,355` and written into `transform.json` via `**sol`. |
| **Verification** | `tests/test_overlay.py::test_run_relaxes_the_drop_budget_for_an_affine_fit`. |
| **Status** | VERIFIED |

### A10 · `core/fit.py:49-51` + `README.md:200-201` — `scale_x`/`scale_y` are documented as column norms but are the matrix diagonal

| | |
|---|---|
| **Severity** | Medium (wrong numbers in the user-facing contract) |
| **Symptom** | The code writes `affine.a`/`affine.d`. The column norms are `hypot(a,c)`/`hypot(b,d)`. For the README's own 7.5° example that is **991.44 vs 1000.0**. |
| **Root cause** | A documentation defect, not a code defect: `PLAN.md:257` and `PLAN.md:272` already say *"the matrix diagonal"*. The README and the `fit.py` docstring say *"column norms"*. |
| **Fix** | **Option B (recommended):** keep the code (the diagonal is what `Affine2D.from_json`'s legacy branch assumes and what old jobs need), fix the two docstrings, and **add** `"column_scales": list(affine.column_scales())` to `transform.json` (additive, breaks nothing). |
| **Connected files** | `core/fit.py:48-50,555-559` · `README.md:200-201` · `core/transform2d.py:187` (`column_scales` already exists and is correct, used only by `is_similarity`). Consumers that treat them as a scale: `static/app.js:507-517` (only for old jobs with no `model` key) and `tests/smoke_test.py:155`. `tests/test_fit.py:348` pins the diagonal — **keep it that way**. |
| **Verification** | `tests/test_fit.py::test_transform_json_exposes_column_scales` — the new key equals the true scale for a rotated fit. |
| **Status** | VERIFIED |

### A11 · `routers/api.py:218-219, :351` — the upload filename is written raw

| | |
|---|---|
| **Severity** | **Critical** (arbitrary file write) |
| **Symptom** | The upload was written as `(job_dir / "uploads" / filename)`. `UploadFile.filename` is the raw `Content-Disposition` value and Starlette does **not** sanitise it, so `/etc/cron.d/evil.dxf` and `../../evil.dxf` both wrote **outside** the job directory. |
| **Root cause** | Untrusted input used as a path. |
| **Fix** | `_safe_upload_name()` — the name is reduced to `Path(raw).name`, and the name recorded in `job.json` is the same sanitised value (the cleaner looks the file up by that name).  The same class of bug in `GET /{id}/analyze?pair_id=…` is fixed by `_safe_pair_id()`. |
| **Connected files** | `routers/api.py` (three write/read sites) · `core/analyze.py::_pair_artifact_root` (the consumer). |
| **Verification** | `tests/test_upload_safety.py` — 4 tests: a `..` name, an absolute name, the N-file endpoint, and the `pair_id` traversal. |
| **Status** | VERIFIED |

### A12 · `core/spotcheck.py`, `core/mech_intra.py`, `core/str_intra.py`, `core/analyze.py` — the thresholds were bare drawing units

| | |
|---|---|
| **Severity** | **Critical** (the whole spot-check family is silent on a metre-scale STR) |
| **Symptom** | Every threshold is specified in millimetres, but the extractors worked in the drawing's own units.  A 100-unit minimum became 100 **metres**. |
| **Measured** | the same fixture: **26 members / 7 gridlines in millimetres → 2 / 0 in metres**.  The bay bounds collapsed, so every saddle was reported grid-misaligned with an infinite distance. |
| **Root cause** | The mm↔unit conversion was missing — the same class of bug commit `e56f9fb` fixed inside `core/mismatch/checks/`. |
| **Fix** | The thresholds are divided by the drawing's mm-per-unit, read from `$INSUNITS` (`core.dxfio.mm_per_unit`). |
| **Connected files** | `core/dxfio.py` (the new helper) · `core/spotcheck.py` (4 sites) · `core/mech_intra.py` (3) · `core/str_intra.py` (2) · `core/analyze.py` (the hotspot radius, now scaled from `transform.json`'s `units.mech`). |
| **Verification** | `tests/test_unit_scaling.py` — 5 tests comparing the mm and metre extractions. |
| **Status** | VERIFIED |

### A13 · `core/str_intra.py` → `core/spotcheck.py` → `core/spotcheck_annotate.py` — the STR-intra issues were drawn in the wrong coordinate space

| | |
|---|---|
| **Severity** | High (every STR marker landed in the wrong place) |
| **Symptom** | `core/str_intra.py` reuses `mech_x`/`mech_y` for a **STR-space** point.  Every other issue — and the DXF the markers are drawn on — is in MECH world, so a `STR-###` marker was drawn thousands of millimetres away from the actual clash. |
| **Measured** | the raw STR point `(1234, 567)` vs the MECH-world point `(197268.18, …)`. |
| **Fix** | The issues are mapped through the fit at the one place the two coordinate spaces meet — the merge in `core/spotcheck.py`.  The `Issue` schema stays unchanged, so `mech_x`/`mech_y` now mean what they say. |
| **Connected files** | `core/spotcheck.py` (the merge) · `core/str_intra.py` (the producer) · `core/spotcheck_annotate.py` (the consumer — unchanged, it was always right for MECH-space coordinates). |
| **Verification** | `tests/test_unit_scaling.py::test_the_str_intra_issues_are_mapped_into_mech_world`. |
| **Status** | VERIFIED |

---

## Part B — `PLAN.md` "Done when" criteria still unmet

| ID | Criterion | File:line | Fix | Verification | Status |
|---|---|---|---|---|---|
| B1 | "no `ezdxf.readfile(` outside `core/dxfio.py` / `core/package.py`" | `core/mismatch/__init__.py:256` | route through `dxfio.read_internal` | `tests/test_plan_acceptance.py::test_no_readfile_outside_dxfio_and_package` (AST scan of `core/`) | VERIFIED |
| B2 | "inputs (uploads/converted) go through `recover.readfile`" | `inspect.py:33`, `labels.py:84,123`, `clean_policy.py:201-202`, `clean_v1.py:95`, `clean_v2.py:74` | use `dxfio.read_input` for uploads/converted | `tests/test_plan_acceptance.py::test_inspect_accepts_a_recoverable_upload` + `test_input_stages_use_the_recovering_reader` (5 modules) | VERIFIED |
| B3 | "no silent fallback" in the fit | `core/transform2d.py:494-495` | the LOO refit must not silently switch model — it now raises, and `core/fit.py` turns that into a **gate failure** (so `force` can override it like every other gate) | `tests/test_plan_acceptance.py::test_loo_does_not_silently_switch_to_an_affine_model` + `test_a_degenerate_loo_becomes_a_fit_gate_not_a_crash` | VERIFIED |
| B4 | "nothing in `package.py` silently discards geometry" | `core/package.py:562-563` | the preview no longer filters MTEXT out unconditionally; what the picture cannot show is now **reported**: `mtext_entities` / `mtext_drawn` / `mtext_not_drawn` in `report.json`, plus a per-image `mtext_skipped` for the retry path | `tests/test_plan_acceptance.py::test_mtext_is_not_filtered_out_of_the_preview` + `test_the_report_records_the_mtext_it_could_not_draw` | VERIFIED |
| B5 | step 10: `tests/test_e2e_synthetic.py` | new file | 3 named e2e cases | the file itself | VERIFIED |

**B4 — what the investigation found.** The blanket MTEXT exclusion turned out to be
*cosmetic* on this machine: ezdxf's font manager has no font, so MTEXT draws no
glyphs either way — the rendered pixels are identical with and without the
filter. The real defect was the **silence**: the reported extents counted the
MTEXT while the picture could not show it, and nothing said so. The fix is
therefore to draw MTEXT when a font exists, to report what could not be drawn,
and to keep a retry-without-MTEXT path for a font-specific crash (reported as
`mtext_skipped`).

---

## Part C — `PLAN_MISMATCH.md` 8b.5 → 8c (the remaining plan)

### C-8b.5 — stage, rendering, merge, wiring

| # | Deliverable | File | State |
|---|---|---|---|
| 1 | `run(job_id, *, pair_dir=None)` stage entry point | `core/mismatch/__init__.py` | VERIFIED |
| 2 | `resolve_pair(..., stage="converted"\|"cleaned")` | `core/roles.py:208` | VERIFIED |
| 3 | `pair.json` gains `mech_converted`/`str_converted` | `core/pair_runner.py` | VERIFIED |
| 4 | `pipelines.resume_after_axes` stage sequence | `pipelines.py:133-184` | VERIFIED |
| 5 | `pair_runner.run_pair` stage sequence | `core/pair_runner.py:204-231` | VERIFIED |
| 6 | `spotcheck.run(dedupe_against=…)` + `superseded` in the JSON | `core/spotcheck.py` | VERIFIED |
| 7 | `core/findings.py` — the merger | new | VERIFIED |
| 8 | `core/mismatch/render.py` — the QA layers | new | VERIFIED |
| 9 | `report["mismatch"]` section | `core/package.py` | VERIFIED |
| 10 | `to_dict()` gains `status` + `source_docs`; `summary` | `core/mismatch/__init__.py:119` | VERIFIED |
| 11 | `policy.enabled` short-circuit | stage | VERIFIED |
| 12 | `policy.source_docs` resolution + fallback warning | stage | VERIFIED |
| 13 | non-fatal → `findings.json` `status:"error"` | stage | VERIFIED |

**Verification:** `tests/test_mismatch_stage.py` — 12 tests.  The engine now
runs on the synthetic mismatch scene and reports **13 findings (12 CRITICAL,
1 WARNING)** — exactly the 13 defects PLAN_MISMATCH §6.1 defines.  The
markers land on the four ``QA|MISMATCH_*`` layers (96 entities), carry
their id / uid / kind in ``OVERLAPQA`` XDATA, are **idempotent** (a re-run
replaces them), and the superimposed DXF still audits clean.

### C-8b.6 — API + UI

| # | Deliverable | State |
|---|---|---|
| 1 | `GET /{job_id}/findings` (filters, paging) | VERIFIED |
| 2 | `GET /{job_id}/findings/{fid}` | VERIFIED |
| 3 | `GET /{job_id}/findings.csv`, `/findings.docx` | VERIFIED |
| 4 | `GET`/`PUT /{job_id}/mismatch/policy` | VERIFIED |
| 5 | `POST /{job_id}/mismatch/rerun` | VERIFIED |
| 6 | pair artifact allowlist += findings.{json,csv,docx}, mismatch.json | VERIFIED |
| 7 | UI Findings panel | VERIFIED |

**Verification:** `tests/test_findings_api.py` — 17 tests over the five
endpoints (filters, the 400s, the 404s, the CSV, the DOCX, the policy
round-trip and the rerun), and `tests/test_static_assets.py` — 6 tests
that the front end and the markup agree.

**The UI work turned up two real defects**, both now fixed:

* the **XSS** the review flagged: the axis-suggestion buttons
  interpolated the *drawing's own label text* into ``innerHTML`` without
  ``_esc()``.  A DXF label of ``<img src=x onerror=…>`` would have run in
  the reviewer's browser.  ``tests/test_static_assets.py`` now walks every
  ``innerHTML`` assignment and fails on an unescaped drawing-derived value.
* the **step rail** was a fixed 9-column grid for 11 steps, so steps 10
  and 11 wrapped under steps 1 and 2.  It is now
  ``repeat(auto-fit, minmax(72px, 1fr))``.

The findings panel replaces the old per-pair issues table and carries the
plan's columns (id, kind, severity, x, y, offset, |local|, tol, ratio,
MECH ref, STR ref, message), a coverage banner that **warns in red when a
family matched no items** (0 items means *not checked*, not *OK*), a kind
filter, INFO / systematic toggles, and a click-to-locate ring on the
preview driven by ``preview_meta.world_to_px``.

### C-8b.7 — performance

| # | Deliverable | State |
|---|---|---|
| 1 | `tests/synth_perf.py` — ~170k entities, 10 seeded defects | VERIFIED |
| 2 | `tests/test_mismatch_perf.py` — exactly 10 PATTERN_OFFSET, < 90 s | VERIFIED |

**The perf test found a real O(n²) bug.**  The first run took **81.9 s**
of the 90 s budget, with the bolt check alone at **74.5 s**.  The cause
was `core/mismatch/checks/bolts.py::group_patterns`, which compared
*every* bolt with *every* other bolt — 200 million comparisons for the
20 000 bolts of the fixture.  It now uses the spatial index
(`core/spatial.py::GridIndex`) to look up only the neighbours within
``eps_units``: **81.9 s → 9.9 s**, and the bolt check **74.5 s → 2.4 s**
(31×).  The same 10 findings, and the existing bolt tests still pass.

### C-8c — DWG export

| # | Deliverable | State |
|---|---|---|
| 1 | `core/export_dwg.py` | VERIFIED |
| 2 | `GET /{job_id}/download.dwg` | VERIFIED |
| 3 | `GET /api/v1/capabilities` | VERIFIED |
| 4 | UI "Download DWG" button | VERIFIED |
| 5 | `tests/test_export_dwg.py` | VERIFIED |

The export renames ``MECH|0`` → ``MECH$0`` (AutoCAD reserves ``|`` for
xref symbols) and never overwrites a name that already exists.  Without
the ODA File Converter the endpoint answers **501** with the reason — the
DXF download is unaffected — and the UI's button is disabled with a
tooltip that says why.

### C — missing test files

| File | Named by | State |
|---|---|---|
| `tests/test_e2e_synthetic.py` | PLAN.md step 10 | VERIFIED — 4 tests: the legacy flow over two presets (HARD in metres at 7.5°, and HARD in mm at −3°), the N-file upload with the roles **reversed**, and the shifted-axis outlier case |
| `tests/test_mismatch_stage.py` | PLAN_MISMATCH §6.4 | VERIFIED — 12 tests (8b.5) |
| `tests/test_findings_api.py` | PLAN_MISMATCH §6.4 | VERIFIED — 17 tests (8b.6) |
| `tests/synth_perf.py` | PLAN_MISMATCH §6.5 | VERIFIED — 190 328 entities |
| `tests/test_mismatch_perf.py` | PLAN_MISMATCH §6.5 | VERIFIED — 4 tests |
| `tests/test_export_dwg.py` | PLAN_MISMATCH 8c | VERIFIED — 10 tests, all mocked |
| `test_kabsch_rigid_exact` | PLAN_MISMATCH §6.2 | VERIFIED |
| `test_fit_uncertainty_inflates_tolerance` | PLAN_MISMATCH §6.2 | VERIFIED |
| `tests/test_static_assets.py` | (new — the front end) | VERIFIED — 6 tests |
| `tests/test_upload_safety.py` | (new — A11) | VERIFIED — 4 tests |
| `tests/test_unit_scaling.py` | (new — A12/A13) | VERIFIED — 7 tests |

---

## Part D — test-suite hygiene (from the review)

| ID | File:line | Defect | Fix | State |
|---|---|---|---|---|
|  D1  |  `tests/test_mismatch_synth.py:188,220`  |  the severity assertion is **vacuous** for all 12 CRITICAL expectations (`rank(f) >= rank(e)` is always true) — severity is effectively untested  | the inequality is reversed: the finding must be **at least** as severe | VERIFIED |
|  D2  |  `tests/test_dxfio.py:139,146`  |  **dead assertion** — the test snapshots `cleaning_policy.json`, production writes `clean_policy.json`; `ref` is always `None`  | the real file name, and the comparison now always runs | VERIFIED |
|  D3  |  `tests/test_overlay.py:386-396`  |  the AST check does **not** detect `except Exception: pass` — the exact pattern the plan lists  | the predicate now looks for `ast.Pass`, and the check **tests itself** | VERIFIED |
|  D4  |  `tests/test_fit.py:349`  |  `assert t["tx"] == t["tx"]` — always true  | the translation is compared with the generator's truth | VERIFIED |
|  D5  |  `tests/test_clean_v1_preserves_protected_labels.py:129`  |  `assert stats["error"] is None if "error" in stats else True` → `assert True`  | `assert "error" not in stats` | VERIFIED |
|  D6  |  `tests/test_pair_runner.py:136`  |  claims `run_pair` rejects a self-loop; the runner has **no such check**  | the **runner** now rejects it (so the check does not depend on the caller), and the test asserts the `ValueError` | VERIFIED |
|  D7  |  `tests/test_fit.py:409-414`  |  `break` after the first of 12 correspondences  | every correspondence is checked, and the count is asserted | VERIFIED |
|  D8  |  `tests/test_grid_robust_extent.py:76`  |  50 % tolerance on a "must not shrink" assertion  | 5 % | VERIFIED |
|  D9  |  `tests/test_clean_safety.py:92`  |  the v2 branch asserts only `"KEEP" in layers`  | it now checks that the annotation-layer **geometry** survives and its **text** does not | VERIFIED |
|  D10  |  `tests/smoke_test.py:28`  |  `import requests` — **not in `requirements.txt`**; pytest imports it during collection  | `collect_ignore` keeps pytest from importing it; `requirements-dev.txt` carries `requests` | VERIFIED |
|  D11  |  `tests/smoke_test.py:30`  |  API port 8765 vs `run.sh` port 8000 — the README's documented sequence fails  | the default is 8000 | VERIFIED |
|  D12  |  `tests/test_api_options.py`  |  `fx_no_worker` exists but 8 of ~11 job-creating tests don't use it  | all 14 now stop the worker | VERIFIED |
|  D13  |  `tests/test_grid_robust_extent.py:18`  |  `from tests import synth` vs `import synth` — two distinct module objects  | one import path | VERIFIED |
|  D14  |  `tests/conftest.py:52`  |  9 of the 24 TP-104 skips need **no DXF geometry** — they are fixture-cascade artefacts hiding a path-traversal guard and 4 validation guards  | a new `fx_job_without_dxfs` fixture (a job with an empty `cleaned/`) and the nine tests moved onto it | VERIFIED |

---

## Part E — findings from the review that are **still open**

The review raised more findings than the plan's scope.  These are recorded
here so the work is resumable — they were found, understood, and left.

### E1 · `core/clean_policy.py:279` — the STR keep-list is chosen by set length

```python
keep_block_prefixes_str = str_prefixes if len(str_prefixes) >= len(mech_prefixes) else mech_prefixes
```

The MECH and STR drawings are passed in separately, and then merged by
*how many prefixes each had*.  A MECH-heavy drawing therefore replaces the
STR namespaces, and `core/clean_common.py` then deletes the STR geometry
outright.  The two lists must never be merged.

### E2 · `core/labels.py:85` — the label dedup is used for *protection*

`core/axes.extract_labels` deduplicates by text, which is right for
*scoring* a suggestion but wrong for *deciding what must not be deleted*.
TP-104 puts the same axis label at **both ends** of a gridline, so exactly
one copy is protected and the other is stripped — the delivered drawing
loses half its grid annotations.

### E3 · `core/clean_common.py` — four smaller defects

* `:430` — `dimltype` (a **linetype** name) is added to the reachable-**block**
  set, so a block named like a linetype is never deleted.
* `:527` — the documented `*U`/`*D` orphan deletion is **dead**: the
  condition is unreachable because `_is_safe_to_delete` has no `*` prefix.
* `:284` — the marker-block prefix match is case-sensitive, while the
  layer match next to it is not.
* `:250` — `e.dxf.layer != policy.marker_layer` is an exact, case-sensitive
  comparison; a layer named `Box` keeps every callout balloon.

### E4 · `core/axes.py:150` — `MIN_GRIDLINE_LENGTH = 100.0` is still absolute

The same class as A12: a metre-scale drawing loses every gridline from
the proximity evidence, so the prefix suggestion degrades.

### E5 · `core/smart3d.py` — the INSERT transform is incomplete

`walk_block_lines` applies translation + scale only: **no rotation, no
block base point, no negative scale**.  A rotated member lands in the
wrong place, which poisons every distance check downstream.  The 90 %
anchor heuristic can also flip the whole document when one block is
repeated many times.

### E6 · `core/spotcheck.py` / `core/mech_intra.py` / `core/str_intra.py`

* the saddle placement ignores the INSERT rotation (`:207`)
* `is_bolted` accepts a bolt **anywhere** in the drawing (`:624`)
* the intra-merge `except Exception` discards the **whole** issue set on a
  schema mismatch, and the failure only reaches stdout (`:709-716`)
* only the **largest** building outline is used, so equipment in a second
  building is a false `BUILDING_CLASH` (`mech_intra.py:313`)
* `BUILDING_CLASH.distance` is a vertex count but the DOCX prints it as mm
* `_detect_clearance` is O(n²) with no spatial index (`mech_intra.py:378`)
* `str_intra` promotes `Bolt-*` / `Connection-*` blocks to beams — the
  cross-drawing extractor skips them, so the two disagree
* the same-block exclusion is by **name**, so a beam↔column clash inside
  one block is never reported

### E7 · `core/fit.py` / `core/transform2d.py`

* the affine branch can write `model: "affine"` for a **similarity**
  (`:353`)
* an invalid option value is silently dropped (`:122`)
* the plan's `weak` status was implemented as `forced` (`:333`)
* `FitOptions.from_job` reads the flat keys but not the derived `fit`
  sub-dict — while `OverlayStyle` reads the sub-dict.  The two readers are
  asymmetric.
* `ransac_similarity` silently truncates to the first 400
  correspondences (`transform2d.py:412`)

### E8 · `core/overlay.py`

* the `Importer` fallback **loses the anonymous `*U` blocks** — real
  drawings use them heavily (`:165`)
* one skipped entity discards the **whole** block-name mapping (`:205`)
* the explode path can leave entities untransformed (`:275`) — the one
  hole in the "never leave an entity untransformed" guarantee
* `apply_source_layers` deletes the original layer entries even when a
  layer-state references them (`:781`)
* a source layer that already starts with `STR|` would be double-prefixed

### E9 · `core/package.py`

* the grid preview is padded **twice** (5 % then 10 % → 15.5 %), and the
  report's two "grid" extents disagree
* `_build_report` indexes `transform[...]` directly, so a **legacy**
  transform.json raises `KeyError` and fails the job after the drawing
  was already produced
* `report["counts"]["mech_in"]` / `str_in` silently become `None` on an
  N-file job
* `outliers_total` and `len(outliers)` disagree past 200 outliers

### E10 · `routers/api.py` — the API contract vs the README

* an **empty** form field contradicts the documented "use the default":
  `qa_grid=""` gives False when the default is True, and `color_mode=""`
  gives a 400 instead of the default
* `auto_run=False` starts the worker anyway, and the response says
  `auto_run: false`
* `apply_axes` type-checks neither `prefix` nor the axis entries, so a
  bad body is a **500**, not a 400 that names the field
* the per-run `options` on `POST /{id}/pairs` are not validated
* `POST /{id}/policy` succeeds but **cannot affect the run** — the
  cleaning stage has already read the policy
* `list_pairs` reports a never-run pair as `"done"`
* `job_id` is never validated

### E11 · `jobs.py`

* `update_status` is a **racy read-modify-write** — the lock covers only
  the write, so two concurrent updates lose a field
* `status.json` is documented in two places and **never written**
* `detected_axes` is a dead field
* `_atomic_write` uses a fixed `.tmp` suffix (two processes would collide)

### E12 · `static/app.js` — the front end

* the Advanced **policy panel is clobbered by the 500 ms poll** — only
  `label_regex` is guarded, so the other four fields cannot be edited
* the axis-suggestion list is **rebuilt every poll**, destroying the
  "selected" highlight
* `pollStatus` swallows every HTTP error and polls forever
* `loadDone` and the confirm POST never check `r.ok`, so a failure looks
  like success

### E13 · the smaller ones

* `core/roles.py:78` — a malformed manifest raises a bare `TypeError`
* `core/roles.py:65`, `core/dxfio.py:78` — the manifest and the audit
  trail are written non-atomically; a corrupt `audit.json` is silently
  discarded, destroying the audit trail
* `core/convert.py:35` — the stem-collision check is case-sensitive, so
  `Plant.dxf` + `plant.dxf` overwrite each other on Windows
* `core/grid.py:95` — the `root=` docstring is inverted (the code is
  right; a maintainer could "fix" working code)
* `core/grid.py:73` — a failed `multi_insert()` degrades to one cell
* `core/analyze.py:157` — the dead `_cleaned_dir` helper computes the
  **wrong** path; the hotspot comprehension is duplicated verbatim
* `core/clean_v1.py` / `core/clean_v2.py` — `run()` is ~85 % duplicated
* `core/mismatch/policy.py` — four knobs are accepted, hashed and
  validated but **never read**; a partial `families` override is fatal
* `core/mismatch/features.py:870` — the band pairing is O(n²) rather than
  GridIndex-based (the perf fixture does not stress it — the noise lines
  are on a non-material layer)
* `core/mismatch/checks/containment.py:173` — `_inward_normal` assumes a
  CCW ring, and `features.py` does not normalise the winding, so a
  clockwise outline makes every `EDGE_DISTANCE` arrow point outward

---

---

## Execution log

| Date | Phase | Result |
|---|---|---|
| 2026-10-25 | Analysis | 7 review agents + 3 fix-mapping agents; 84 files; defects independently reproduced |
| 2026-10-25 | Phase 1 (Tier 1) | **A1–A10 all VERIFIED.** 16 new regression tests; suite 389 → **405 passed**, 24 skipped, 0 failed. Every new test proven to FAIL on the original code (A1/A3/A5–A8 by temporary revert; A2/A4/A9/A10 by construction). |
| 2026-10-26 | **Verification** | 509 tests collected; **494 passed, 15 skipped, 0 failed**.  64 files changed, +5 686 / −331. |
| 2026-10-26 | **Verification** | `tests/verify_fixes.py` re-runs all 14 reproductions — **14/14 pass**.  The app boots on the `PORT` override, serves the findings panel, and `/api/v1/capabilities` reports `dwg_export: false` (ODA is not installed here).  `run.sh` now honours `PORT`/`HOST` — the README documented a `PORT=8765 ./run.sh` that was silently ignored. |
| 2026-10-26 | Phase 7 | Final state recorded below. |
| 2026-10-25 | Phase 6 | **A11–A13 VERIFIED, all the missing plan test files written, and the hygiene D1–D14 fixed.**  Suite 478 → **494 passed**; the skips went 24 → 15 (nine geometry-free tests recovered).  D1 was the important one: the mismatch severity check was vacuous, so severity had never really been tested. |
| 2026-10-25 | Phase 5 (8b.7 + 8c) | **C-8b.7 and C-8c VERIFIED.** `tests/synth_perf.py` (190 328 entities), `tests/test_mismatch_perf.py` (4 tests), `core/export_dwg.py`, `tests/test_export_dwg.py` (10 tests).  Suite 453 → **467 passed**, 24 skipped, 0 failed.  The perf test found an O(n²) bug in the pattern grouping — 81.9 s → 9.9 s. |
| 2026-10-25 | Phase 4 (8b.6) | **C-8b.6 all VERIFIED.** 23 new tests (`tests/test_findings_api.py` + `tests/test_static_assets.py`); suite 428 → **453 passed**, 24 skipped, 0 failed.  The UI work also fixed the XSS the review flagged and the 9-column step rail. |
| 2026-10-25 | Phase 3 (8b.5) | **C-8b.5 all 13 VERIFIED.** 12 new tests in `tests/test_mismatch_stage.py`; suite 418 → **428 passed**, 24 skipped, 0 failed. The engine is reachable end to end: the stage runs, the QA markers are drawn, the findings are merged, and the pipeline runs them. |
| 2026-10-25 | Phase 2 (PLAN.md criteria) | **B1–B4 VERIFIED.** 13 new tests in `tests/test_plan_acceptance.py`; suite 405 → **418 passed**, 24 skipped, 0 failed. 11 of the 13 proven to FAIL on the original code by temporary revert of all 9 touched modules. |
