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

---

## Part B — `PLAN.md` "Done when" criteria still unmet

| ID | Criterion | File:line | Fix | Verification | Status |
|---|---|---|---|---|---|
| B1 | "no `ezdxf.readfile(` outside `core/dxfio.py` / `core/package.py`" | `core/mismatch/__init__.py:256` | route through `dxfio.read_internal` | `tests/test_plan_acceptance.py::test_no_readfile_outside_dxfio_and_package` (AST scan of `core/`) | VERIFIED |
| B2 | "inputs (uploads/converted) go through `recover.readfile`" | `inspect.py:33`, `labels.py:84,123`, `clean_policy.py:201-202`, `clean_v1.py:95`, `clean_v2.py:74` | use `dxfio.read_input` for uploads/converted | `tests/test_plan_acceptance.py::test_inspect_accepts_a_recoverable_upload` + `test_input_stages_use_the_recovering_reader` (5 modules) | VERIFIED |
| B3 | "no silent fallback" in the fit | `core/transform2d.py:494-495` | the LOO refit must not silently switch model — it now raises, and `core/fit.py` turns that into a **gate failure** (so `force` can override it like every other gate) | `tests/test_plan_acceptance.py::test_loo_does_not_silently_switch_to_an_affine_model` + `test_a_degenerate_loo_becomes_a_fit_gate_not_a_crash` | VERIFIED |
| B4 | "nothing in `package.py` silently discards geometry" | `core/package.py:562-563` | the preview no longer filters MTEXT out unconditionally; what the picture cannot show is now **reported**: `mtext_entities` / `mtext_drawn` / `mtext_not_drawn` in `report.json`, plus a per-image `mtext_skipped` for the retry path | `tests/test_plan_acceptance.py::test_mtext_is_not_filtered_out_of_the_preview` + `test_the_report_records_the_mtext_it_could_not_draw` | VERIFIED |
| B5 | step 10: `tests/test_e2e_synthetic.py` | new file | 3 named e2e cases | the file itself | OPEN |

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
| 1 | `run(job_id, *, pair_dir=None)` stage entry point | `core/mismatch/__init__.py` | OPEN |
| 2 | `resolve_pair(..., stage="converted"\|"cleaned")` | `core/roles.py:208` | OPEN |
| 3 | `pair.json` gains `mech_converted`/`str_converted` | `core/pair_runner.py` | OPEN |
| 4 | `pipelines.resume_after_axes` stage sequence | `pipelines.py:133-184` | OPEN |
| 5 | `pair_runner.run_pair` stage sequence | `core/pair_runner.py:204-231` | OPEN |
| 6 | `spotcheck.run(dedupe_against=…)` + `superseded` in the JSON | `core/spotcheck.py:539,751` | OPEN |
| 7 | `core/findings.py` — the merger | new | OPEN |
| 8 | `core/mismatch/render.py` — the QA layers | new | OPEN |
| 9 | `report["mismatch"]` section | `core/package.py:833-919` | OPEN |
| 10 | `to_dict()` gains `status` + `source_docs`; flat `features` | `core/mismatch/__init__.py:119` | OPEN |
| 11 | `policy.enabled` short-circuit | stage | OPEN |
| 12 | `policy.source_docs` resolution + fallback warning | stage | OPEN |
| 13 | non-fatal → `findings.json` `status:"error"` | stage | OPEN |

### C-8b.6 — API + UI

| # | Deliverable | State |
|---|---|---|
| 1 | `GET /{job_id}/findings` (filters, paging) | OPEN |
| 2 | `GET /{job_id}/findings/{fid}` | OPEN |
| 3 | `GET /{job_id}/findings.csv`, `/findings.docx` | OPEN |
| 4 | `GET`/`PUT /{job_id}/mismatch/policy` | OPEN |
| 5 | `POST /{job_id}/mismatch/rerun` | OPEN |
| 6 | pair artifact allowlist += findings.{json,csv,docx}, mismatch.json | OPEN |
| 7 | UI Findings panel | OPEN |

### C-8b.7 — performance

| # | Deliverable | State |
|---|---|---|
| 1 | `tests/synth_perf.py` — ~170k entities, 10 seeded defects | OPEN |
| 2 | `tests/test_mismatch_perf.py` — exactly 10 PATTERN_OFFSET, < 90 s | OPEN |

### C-8c — DWG export

| # | Deliverable | State |
|---|---|---|
| 1 | `core/export_dwg.py` | OPEN |
| 2 | `GET /{job_id}/download.dwg` | OPEN |
| 3 | `GET /api/v1/capabilities` | OPEN |
| 4 | UI "Download DWG" button | OPEN |
| 5 | `tests/test_export_dwg.py` | OPEN |

### C — missing test files

| File | Named by | State |
|---|---|---|
| `tests/test_e2e_synthetic.py` | PLAN.md step 10 | OPEN |
| `tests/test_mismatch_stage.py` | PLAN_MISMATCH §6.4 | OPEN |
| `tests/test_findings_api.py` | PLAN_MISMATCH §6.4 | OPEN |
| `tests/synth_perf.py` | PLAN_MISMATCH §6.5 | OPEN |
| `tests/test_mismatch_perf.py` | PLAN_MISMATCH §6.5 | OPEN |
| `tests/test_export_dwg.py` | PLAN_MISMATCH 8c | OPEN |
| `test_kabsch_rigid_exact` | PLAN_MISMATCH §6.2 | OPEN |
| `test_fit_uncertainty_inflates_tolerance` | PLAN_MISMATCH §6.2 | OPEN |

---

## Part D — test-suite hygiene (from the review)

| ID | File:line | Defect | State |
|---|---|---|---|
| D1 | `tests/test_mismatch_synth.py:188,220` | the severity assertion is **vacuous** for all 12 CRITICAL expectations (`rank(f) >= rank(e)` is always true) — severity is effectively untested | OPEN |
| D2 | `tests/test_dxfio.py:139,146` | **dead assertion** — the test snapshots `cleaning_policy.json`, production writes `clean_policy.json`; `ref` is always `None` | OPEN |
| D3 | `tests/test_overlay.py:386-396` | the AST check does **not** detect `except Exception: pass` — the exact pattern the plan lists | OPEN |
| D4 | `tests/test_fit.py:349` | `assert t["tx"] == t["tx"]` — always true | OPEN |
| D5 | `tests/test_clean_v1_preserves_protected_labels.py:129` | `assert stats["error"] is None if "error" in stats else True` → `assert True` | OPEN |
| D6 | `tests/test_pair_runner.py:136` | claims `run_pair` rejects a self-loop; the runner has **no such check** | OPEN |
| D7 | `tests/test_fit.py:409-414` | `break` after the first of 12 correspondences | OPEN |
| D8 | `tests/test_grid_robust_extent.py:76` | 50 % tolerance on a "must not shrink" assertion | OPEN |
| D9 | `tests/test_clean_safety.py:92` | the v2 branch asserts only `"KEEP" in layers` | OPEN |
| D10 | `tests/smoke_test.py:28` | `import requests` — **not in `requirements.txt`**; pytest imports it during collection | OPEN |
| D11 | `tests/smoke_test.py:30` | API port 8765 vs `run.sh` port 8000 — the README's documented sequence fails | OPEN |
| D12 | `tests/test_api_options.py` | `fx_no_worker` exists but 8 of ~11 job-creating tests don't use it | OPEN |
| D13 | `tests/test_grid_robust_extent.py:18` | `from tests import synth` vs `import synth` — two distinct module objects | OPEN |
| D14 | `tests/conftest.py:52` | 9 of the 24 TP-104 skips need **no DXF geometry** — they are fixture-cascade artefacts hiding a path-traversal guard and 4 validation guards | OPEN |

---

## Execution log

| Date | Phase | Result |
|---|---|---|
| 2026-10-25 | Analysis | 7 review agents + 3 fix-mapping agents; 84 files; defects independently reproduced |
| 2026-10-25 | Phase 1 (Tier 1) | **A1–A10 all VERIFIED.** 16 new regression tests; suite 389 → **405 passed**, 24 skipped, 0 failed. Every new test proven to FAIL on the original code (A1/A3/A5–A8 by temporary revert; A2/A4/A9/A10 by construction). |
| 2026-10-25 | Phase 2 (PLAN.md criteria) | **B1–B4 VERIFIED.** 13 new tests in `tests/test_plan_acceptance.py`; suite 405 → **418 passed**, 24 skipped, 0 failed. 11 of the 13 proven to FAIL on the original code by temporary revert of all 9 touched modules. |
