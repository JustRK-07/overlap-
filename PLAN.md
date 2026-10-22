# PLAN` heading down to that path as-is.

I checked every line reference against the working tree. Note that the working tree has uncommitted changes: strategies B/C were removed from `core/overlay.py`, plus edits in 12 other files. Those line numbers are what the plan cites.

---

# PLAN — Correct, verifiable MECH+STR superimposition (synthetic-fixture driven)

## Preconditions
- Commit or stash the current uncommitted work-in-progress first (B/C strategy removal in `core/overlay.py`, edits to `README.md`, `pipelines.py`, `routers/api.py`, `static/*`, `jobs.py`, `core/package.py`, `core/pair_runner.py`, tests). All line numbers below are for that working tree.
- Baseline: `python3 -m pytest -q` gives 133 passed / 24 skipped. The skipped tests need the TP-104 files. Every step must end green with `python3 -m pytest -q`.
- Convention for proving bug fixes: step 0 adds tests for every known bug, marked `@pytest.mark.xfail(strict=True, reason="<step N>")`. The step that fixes a bug **removes** its marker. Strict xfail turns the suite red if a bug gets fixed by accident, so markers can't go stale.

## Verified defects (with line refs)
| Area | Location | Defect |
|---|---|---|
| Roles | `core/clean_v1.py:288-292`, `core/clean_v2.py:246-250` | `is_str = "STR" in dxf.stem.upper()` |
| Roles | `core/fit.py:229-238`, `core/overlay.py:445-454`, `core/spotcheck.py:594-606`, `core/mech_intra.py:427-434`, `core/str_intra.py:363-370`, `core/analyze.py:207-209`, `core/axes.py:581-583`, `routers/api.py:467-474` | alphabetical `sorted(glob)[0]` = MECH |
| Roles | `routers/api.py:71-73` | legacy upload overwrites when `drawing_a` and `drawing_b` have the same filename |
| Files | `core/clean_policy.py:295` | globs `*.dxf` (picks up `*_clean.dxf` on re-runs); `:311` takes `cleaned[0]`/`cleaned[1]` |
| Files | `core/clean_v1.py:289`, `core/labels.py:60-63` | an upload whose stem ends `_clean` is silently skipped (never cleaned, never label-scanned) |
| Files | `core/convert.py:57-58` | `odafc.convert(...)` defaults `replace=False` and fails on re-run |
| Files | `core/validate.py:29` | uses `recover.readfile`; every later stage uses plain `ezdxf.readfile` |
| Cleaning | `core/clean_common.py:194-245` | `_strip_triangles_in` deletes every SOLID/TRACE/3DFACE, any 3-vertex LWPOLYLINE and 3-vertex HATCH paths |
| Cleaning | `core/clean_policy.py:59-71` + `core/clean_common.py:512-513` | `startswith` layer matching: `CIVIL` (62) and `FRAME` (69) delete `FRAME-STEEL`, `CIVIL-FOUNDATION` etc.; `clean_common.py:533` hard-codes another list |
| Cleaning | `core/clean_common.py:102-109` (`_is_grid_label` TEXT only), `:160-163` (MTEXT always deleted), `ANNOT_TYPES_V1` `:43` | MTEXT grid labels deleted; ATTRIB labels on INSERTs are never considered (attribs aren't yielded by layout iteration) |
| Cleaning | `core/clean_common.py:330-332`, `:414-416` | reference collection skips `*` blocks, so blocks used only from `*U`/`*D` blocks get deleted |
| Cleaning | (nowhere) | `$INSUNITS` never read |
| Grid | `core/fit.py:155-166`, `core/axes.py:253-292` | block-to-world uses insert + xscale only; first INSERT wins; ignores base point, nesting, mirror (fit also ignores rotation) |
| Grid | `core/fit.py:79-94` | nearest-*midpoint* line selection; absolute 1.0/100.0 thresholds `:39-46`; hard-coded layer set `:33-36`; block-name prefixes `:121-122` |
| Grid | `core/fit.py:250-251` | letters = X, digits = Y |
| Grid | `core/axes.py:211-225` | `extract_labels` dedups to the first occurrence |
| Fit | `core/fit.py:204-216` | separable 1-D least squares; degenerate case silently returns slope 1 (`:212-213`); no outlier rejection; two points give residual 0; no rotation |
| Overlay | `core/overlay.py:196-206` | `import_block(rename=False)` on a name that exists in MECH returns MECH's block, which is then **renamed** to `_STR_x` (MECH hijack); `:262` keeps STR INSERTs pointing at same-named MECH blocks |
| Overlay | `core/overlay.py:329-351` | first INSERT's position/scale baked into the shared block, then every INSERT reset to the origin, so repeated blocks collapse; rotation/base/nesting ignored |
| Overlay | `core/overlay.py:128-171` | ELLIPSE axis unchanged; ARC angles not mirrored; CIRCLE `max(|ax|,|ay|)`; bulge sign not flipped on mirror; HATCH only polyline paths; TEXT height/rotation unchanged; bare `except: pass` everywhere (e.g. `:124-125`, `:170-171`, `:226-227`, `:352-353`) |
| Overlay | `core/overlay.py:378-433` | only STR layers prefixed `_STR_`; MECH layers untouched, so overlap can't be told apart by layer |
| Package | `core/package.py:134-311` | percentile/IQR crop hides misplaced geometry; `:314-340` silently drops dangling INSERTs |
| Consumers of transform | `core/spotcheck.py:456-467,613`, `core/package.py:504-511`, `core/analyze.py:274-276`, `static/app.js:407-408` | assume axis-aligned `scale_x, scale_y, tx, ty` |

## Key design decisions
1. **Role source of truth = a manifest** (`jobs/<id>/stages/files.json`), written once and read everywhere. Never infer roles from filenames or sort order. Roles come from the API: legacy `drawing_a` = MECH, `drawing_b` = STR; N-file uploads use `roles`.
2. **Separate directories.** Converted inputs go in `converted/` and cleaned outputs in `cleaned/`. No stage ever globs to discover inputs. *Why:* removes the whole class of `_clean` re-pick bugs.
3. **Transform model = 2-D similarity** (uniform scale + rotation + translation, no mirror by default), solved with Umeyama. An optional `affine` model is diagnostic only. *Why:* two drawings of the same building differ by units, rotation and offset. Independent x/y scales overfit and are physically meaningless. A similarity also keeps `Insert.transform()` exact, so blocks never need to be modified.
4. **Correspondences = grid-line intersections** (label pair), not individual axis coordinates. *Why:* this is rotation-invariant and drops the letters=X assumption.
5. **Deterministic RANSAC.** Enumerate every pair of correspondences as minimal sets (N ≤ ~100, so at most 4,950 pairs). *Why:* reproducible, so tests are exact.
6. **Overlay import uses `ezdxf.xref.Loader(str_doc, mech_doc, conflict_policy=ConflictPolicy.XREF_PREFIX)` + `execute(xref_prefix="STR")`, then `ezdxf.transform.inplace(new_entities, M)`.** Block definitions are never modified. *Why:* I spiked this in memory on ezdxf 1.4.4. Conflicting names become `STR$0$<name>`, nested block references are remapped, and an INSERT with rotation 90 / xscale −1 transformed correctly with an empty log. This fixes the hijack, the collapse and the nested/rotation problems in one go. Fallback if xref misbehaves on a fixture: `Importer.import_block(name, rename=True)` + `import_modelspace()`.
7. **Per-source layers `MECH|<name>` / `STR|<name>` / `QA|GRID`**, with the separator in a constant `SOURCE_LAYER_SEP = "|"`. *Risk:* AutoCAD reserves `|` for xref-dependent symbols. ezdxf's validator accepts it (checked). If a manual AutoCAD/ODA check shows problems, change the constant to `"$"`; nothing else changes. Inside block definitions, layer `"0"` is left as is (keeps ByBlock/ByLayer inheritance) and `"Defpoints"` is left as is.
8. **File reading:** inputs (uploads/converted) go through `recover.readfile` and the auditor result is recorded. Pipeline-internal files (cleaned, superimposed) use `ezdxf.readfile` + `doc.audit()`. *Why:* `core/package.py:385-390` claims recover dropped about 90% of entities on the old superimposed output. Step 2 adds a test that recover and readfile agree on entity counts for the synthetic files. Until step 8, `package.py` keeps readfile-first.

---

## Step 0 — Synthetic fixture generator + ground-truth harness
**Files (new):** `tests/synth.py`, `tests/geomtruth.py`, `tests/test_synth_fixtures.py`, `tests/test_known_bugs.py`; `tests/conftest.py` (add fixtures).

**`tests/synth.py`**
- `@dataclass GroundTruth(scale: float, rotation_deg: float, tx: float, ty: float)` with `matrix() -> Matrix44` (STR→MECH: scale, then z-rotate, then translate), `apply(xy)`, `as_affine() -> (a,b,c,d,tx,ty)`.
- `@dataclass SynthOptions`: `prefix="TP104-"`, `x_labels=("A","B","C","D")`, `y_labels=("1","2","3")`, `x_spacing=(6000,7500,6000)`, `y_spacing=(5000,5000)` (mm), `mech_offset=(208379.86,-179584.52)`, `str_rotation_deg=0.0`, `str_units="mm"|"m"`, `rotated_grid_inserts=False` (STR GridLine blocks defined horizontally and inserted with rotation 90 for verticals), `letters_on_y=False`, `duplicate_labels=True` (labels at both ends), `mtext_label="D"` (that MECH label written as MTEXT; `None` disables), `attrib_label="3"` (that STR label carried as an ATTRIB on the GridLine INSERT), `shift_str_axis: dict[str,float] = {}` (outlier injection, mm), `noise_mm=0.0` (seeded), `decoy_line=True`, `stray_str_entity_at=None`, `mech_name="zz_plant.dxf"`, `str_name="aa_frame.dxf"`. The names are deliberately chosen to sort the wrong way and contain neither MECH nor STR.
- Presets: `LEGACY_COMPATIBLE` (rotation 0, mm, `rotated_grid_inserts=False`, `mtext_label=None`, `attrib_label=None`, `decoy_line=False`), which today's fit can handle; and `HARD` (rotation 7.5°, units m, rotated inserts, MTEXT + ATTRIB labels, decoy, nested/mirrored blocks).
- `build_pair(out_dir: Path, opts) -> SynthPair(mech_path, str_path, truth, grid_p: dict[label -> (point, dir)], intersections_mech: dict[(lx,ly) -> Vec2], intersections_str: dict[...])`.
- Construction method:
  1. Build content once in a project frame P (mm) with a `_populate(doc, role)` function.
  2. MECH doc: `$INSUNITS=4`. Apply `B = translate(mech_offset)` to all **modelspace** entities via `ezdxf.transform.inplace`.
  3. STR doc: `$INSUNITS = 4 | 6`. Apply `A = scale(u)·rotate(−θ)·translate(t_s)` to modelspace, where u = 1 or 0.001.
  4. Truth STR→MECH = `A⁻¹·B`. Block definitions stay untouched, so INSERT composition is exercised.
- Content:
  - MECH: grid LINEs on layer `CENTER` plus TEXT labels (two per axis when duplicated) and bubble CIRCLEs on `GRID-BUBBLE`.
  - MECH: equipment block `A$C1234` with CIRCLE, ARC, ELLIPSE, bulged LWPOLYLINE, SPLINE, HATCH with an edge path (line + arc edges), TEXT tag. Inserted twice (rotation 0 and 30). The `A$C` prefix is used so the current MECH keep-prefix rule keeps it.
  - MECH: block `TAG` (a 100×100 square) inserted once. A MECH `TITLE` border and a `REV-A` revision triangle.
  - STR: `GridLine-<label>` blocks (LINE + TEXT, or ATTDEF/ATTRIB for `attrib_label`) inserted at the axis position. When `rotated_grid_inserts=True`, the block base point is non-zero.
  - STR: shared block `Part-COL300` (closed square LWPOLYLINE + 2 diagonals + HATCH) inserted at **every** intersection (12×).
  - STR: nested `Part-BAY` containing two `Part-COL300` INSERTs, one with `xscale=-1` and one with `rotation=45`, plus a LINE. Inserted twice.
  - STR: block `TAG` with *different* content (a circle), for the collision test.
  - STR: gusset `Part-GUSSET` (3-vertex closed LWPOLYLINE on `STEEL-PLATE` + SOLID on `STEEL-FILL`).
  - STR: beams on `FRAME-STEEL` and footing on `CIVIL-FOUNDATION`.
  - STR: an anonymous `*U1` block that INSERTs `Part-ANCHOR`; `*U1` is referenced from modelspace.
  - STR: beam TEXT `ISMB 600`. Decoy long LINE on `CENTER` whose midpoint is nearer to a label than the true gridline's midpoint.

**`tests/geomtruth.py`**
- `world_primitives(doc, *, layer_pred=lambda l: True) -> list[Prim]`. Walk modelspace with `ezdxf.disassemble.recursive_decompose`. For each graphic entity use `ezdxf.path.make_path(e).flattening(FLAT)`; TEXT/MTEXT/ATTRIB give `Prim(kind="TEXT", pts=[insert], text=...)`. `Prim(kind, layer, pts: np.ndarray)`, where kind ∈ {CURVE, TEXT, POINT}. HATCH uses `ezdxf.path.from_hatch`.
- `map_prims(prims, truth)` applies the truth matrix.
- `assert_prims_match(actual, expected, tol)`: bucket by kind, greedily pair by centroid (KD via sort or brute force with numpy), then require symmetric point-to-polyline Hausdorff ≤ `tol` and equal counts. Tolerance rule: `tol = 2*FLAT + 1e-9*extent`, with `FLAT = 1e-3 * drawing unit`.
- `grid_intersections_from_lines(doc, layer_pred)` is a helper for the end-to-end test.

**New tests**
- `test_synth_fixtures.py`:
  - `test_generator_truth_maps_str_grid_to_mech`: for every (letter, digit), `truth.apply(intersections_str[k])` lies within 1e-9 (relative) of `intersections_mech[k]`. The STR intersection is computed analytically from P-frame spacing with cos/sin, independent of ezdxf.
  - `test_generator_files_reopen_and_audit_clean`: `ezdxf.readfile` then `doc.audit().has_errors is False`.
  - `test_generator_units_header`: `$INSUNITS` is 4 (MECH) and 6 (STR, m variant).
  - `test_world_primitives_self_consistency`: STR primitives mapped by truth match the same primitives computed after `transform.inplace` on a copy.
- `test_known_bugs.py`: all `xfail(strict=True)`:
  - `test_roles_not_inferred_from_filename` (step 1)
  - `test_clean_keeps_gusset_triangles_and_frame_layers` (step 3)
  - `test_mtext_grid_label_survives_clean` (step 3)
  - `test_grid_detection_rotated_inserts` (step 4)
  - `test_fit_recovers_rotation_and_units` (step 5)
  - `test_overlay_repeated_blocks_not_collapsed` (step 6)
  - `test_overlay_no_mech_block_hijack` (step 6)
  - `test_overlay_source_layers` (step 7)
  - `test_preview_extents_include_stray` (step 8)
  - `test_e2e_synthetic` (step 10)

  Each one gets the body described in its target step.
- conftest: `fx_synth_simple(tmp_path)` and `fx_synth_hard(tmp_path)` return `SynthPair`. `fx_synth_job(fx_isolated_jobs, ...)` is a factory that creates a legacy job with the synthetic pair in `uploads/`.

**Risks:** matplotlib ships numpy, so numpy is available; confirm with `python3 -c "import numpy"` before relying on it. Flattening tolerance needs to be relative for the m-unit variant.
**Done when:** generator and harness are in, 4 real tests pass, 10 strict xfails are recorded, suite is green.

---

## Step 1 — Explicit MECH/STR roles through every stage
**Files:** new `core/roles.py`; `jobs.py` (`create_job_legacy` adds `"kind": "mech"` / `"str"` to the `drawing_a`/`drawing_b` dicts); `routers/api.py` (`create_overlay` rejects identical filenames with 400; `apply_axes` 467-474; `_maybe_run_pairs_after_cleaning` 283-302; `download` 536-543 picks the active pair, not `pair_dirs[0]`); `core/convert.py`; `core/labels.py:60-63`; `core/clean_policy.py:295-311`; `core/clean_v1.py:288-292`; `core/clean_v2.py:246-250`; `core/axes.py:581-583`; `core/fit.py:229-238`; `core/overlay.py:445-454`; `core/spotcheck.py:594-606`; `core/mech_intra.py:427-434`; `core/str_intra.py:363-370`; `core/analyze.py:207-209`; `core/pair_runner.py:run_pair` (writes `<root>/pair.json` **before** running stages).

**Behavior**
- `core/roles.py`:
  - `@dataclass FileEntry(file_id, upload, role, converted, cleaned)`, with paths relative to the job dir.
  - `write_manifest(job_id, entries)` writes `stages/files.json`; `load_manifest(job_id)`.
  - `entries_from_job(job)` builds from `JobState.files`. Legacy dicts: `drawing_a` = mech, `drawing_b` = str (or the `kind` key). FileMeta uses `.kind`.
  - `resolve_inputs(job_id) -> list[FileEntry]` for stages 3-6. Fallback when there is no manifest: `entries_from_job`, with `converted` located in `converted/` → `cleaned/` by name. This keeps `tests/test_clean_v1_preserves_protected_labels.py::test_clean_policy_run_augments...` and `tests/test_labels.py` working.
  - `resolve_pair(job_id, pair_dir=None) -> tuple[Path, Path]` (mech, str):
    - If `pair_dir` is set, read `<pair_dir.parent>/pair.json` `{mech, str}`.
    - Otherwise use the manifest's single mech + single str `cleaned`.
    - Fallback: job.files names, located in `cleaned/` as `name` or `stem + "_clean.dxf"` (keeps `fx_job_with_cleaned_dxfs` working).
    - Raise `RoleError` (a RuntimeError subclass) with a clear message if there are 0 or more than 1 of a role. Never sort.
- convert writes the manifest (step 1: `converted = cleaned/<stem>.dxf`, `cleaned = cleaned/<stem>_clean.dxf`). Cleaners iterate `resolve_inputs`, use `is_str = entry.role == "str"`, treat role `other` with MECH rules, and set `FileMeta.cleaned_filename`.
- The `_maybe_run_pairs_after_cleaning` reconciliation is replaced by reading `FileMeta.cleaned_filename`. labels/clean_policy use the mech + str entries; with multiple of either, take the first of each and log it.

**Tests (new `tests/test_roles.py`)**
- `test_resolve_pair_uses_manifest_not_names`: manifest with mech=`zz_plant_clean.dxf`, str=`aa_frame_clean.dxf` gives `(zz…, aa…)`.
- `test_resolve_pair_raises_on_ambiguous`: two mech entries raise `RoleError`.
- `test_resolve_pair_pair_dir_reads_pair_json`.
- `test_legacy_fallback_drawing_a_is_mech`.
- `test_clean_marks_str_by_role`: run stages 1-6 on `fx_synth_simple` through `pipelines.run_pipeline(job_id)` (synchronous). The `clean_stats.json` entry for `aa_frame.dxf` has `is_str is True`.
- `test_upload_same_filename_rejected` (legacy endpoint returns 400).
- Flip `test_roles_not_inferred_from_filename`. Body: run the full legacy flow on `LEGACY_COMPATIBLE` up to fit; `transform.json` gives `tx, ty` within 1e-6 of `truth.tx, truth.ty` and scale 1. With swapped roles the sign would flip.

**Risks:** N-file jobs with more than 2 files (axes stage). `axes.run` must use the first mech + first str from the manifest. Old job dirs on disk have no manifest, which the fallback covers.
**Done when:** `grep -rn "sorted(.*glob(\"\*_clean.dxf\"))" core routers` returns nothing, and `grep -n '"STR" in' core` returns nothing.

---

## Step 2 — Robust file handling
**Files:** new `core/dxfio.py`; `core/convert.py`; `core/validate.py`; replace `ezdxf.readfile` in `core/{inspect,labels,clean_policy,clean_v1,clean_v2,axes,fit,overlay,spotcheck,mech_intra,str_intra,analyze,spotcheck_annotate}.py`; `core/roles.py` (converted path → `converted/`); `jobs.py:_ensure_dirs` (add `converted/`).

**Behavior**
- `dxfio.read_input(path) -> (doc, AuditSummary)`: `recover.readfile`, then `auditor = doc.audit()`. Summary is `{fixes: len(auditor.fixes), errors: [str(e.message) for e in auditor.errors][:50]}`. If `auditor.has_errors` after recovery, raise `DXFReadError` with the first 5 messages.
- `dxfio.read_internal(path)` = `ezdxf.readfile` + `doc.audit()`; raises on errors.
- `dxfio.save(doc, path)`: `doc.audit()` first, then `saveas`.
- Each stage appends its summary to `stages/audit.json` keyed `{stage}:{filename}`.
- convert:
  - DWG: `odafc.convert(src, dst, version="R2018", audit=True, replace=True)`.
  - DXF: `read_input` + `save` into `converted/<stem>.dxf`. This normalizes and re-audits; no blind copy.
  - Output names are always `<stem>.dxf` in `converted/` and `<stem>_clean.dxf` in `cleaned/`, so the `_clean` stem skip at `clean_v1.py:289` / `labels.py:62` is deleted.
  - Validate that no two uploads map to the same stem; raise if they do.
- Cleaners read `converted/`, write `cleaned/`, and are idempotent on re-run (overwrite).

**Tests (`tests/test_dxfio.py`)**
- `test_recover_and_readfile_agree_on_synth`: entity counts (modelspace + per block) are equal for both synthetic files.
- `test_convert_rerun_is_idempotent`: run `convert.run` twice; no exception, same output hash.
- `test_upload_named_clean_is_cleaned`: an upload named `foo_clean.dxf` produces `cleaned/foo_clean_clean.dxf` with `clean_stats` present.
- `test_clean_policy_ignores_previous_outputs`: put a stale `cleaned/x_clean.dxf` in place; the policy still derives from the manifest pair only.
- `test_read_input_reports_audit_fixes`: write a synthetic DXF with a dangling layer reference via ezdxf (entity on an undefined layer); the summary has fixes ≥ 1.

**Risks:** the `package.py:385-390` claim about recover dropping entities. Keep `package` on readfile until step 8 and re-check there. ODA isn't installed in CI, so DWG paths are only covered by a mock (`monkeypatch odafc.convert` asserting `replace=True`).
**Done when:** no `ezdxf.readfile(` remains outside `core/dxfio.py` and `core/package.py`.

---

## Step 3 — Safe cleaning rules
**Files:** `core/clean_common.py` (`_strip_triangles_in` 194-245, `_is_grid_label` 102-109, `_strip_annotations_in` 112-136, `_strip_non_grid_text_in` 139-166, `_strip_orphan_blocks` 302-392, `_strip_orphan_blocks_aggressive` 395-488, `_strip_annotation_layer_entities` 491-517, `_strip_annotation_layer_text` 520-546); `core/clean_policy.py` (dataclass 27-96, defaults 59-71); `core/clean_v1.py` / `core/clean_v2.py` (call sites + stats); `routers/api.py:save_policy` (new list fields).

**Behavior**
- **Layer matching:** new `layer_matches(layer, entry)`, case-insensitive. An entry ending in `-`, `_`, `$` or `*` is a prefix (with `*` stripped). Any other entry is an **exact** name. Defaults become `["DIM", "AM_", "REV-", "DEFPOINTS", "TITLE", "BORDER"]`; **`CIVIL` and `FRAME` are removed**. Old saved policies containing them now mean exact match. Line 533's hard-coded tuple is replaced by `policy.annotation_layers`.
- **Triangles:** new policy fields `marker_block_prefixes = ["SectionMark-", "Mark-"]` and `triangle_marker_layers = ["REV-", "SECTION", "AM_"]`. A SOLID/TRACE/3DFACE/3-vertex LWPOLYLINE/3-vertex HATCH path is removed **only if** its layer matches `triangle_marker_layers` or `annotation_layers`, or its container block name starts with a `marker_block_prefixes` entry. Shape alone never triggers removal. `_strip_triangles_in` gains `container_name` and `policy` parameters.
- **Labels:** `_label_text(e)`: TEXT → `dxf.text`, MTEXT → `e.plain_text()`, ATTRIB → `dxf.text`. `_is_grid_label` accepts TEXT and MTEXT. `_strip_annotations_in` and `_strip_non_grid_text_in` keep grid-matching MTEXT. New `_strip_non_grid_attribs(container)`: for each INSERT, keep grid-matching `attribs` and `insert.delete_attrib(tag)` the rest. ATTDEF is still stripped; existing ATTRIBs don't depend on it.
- **Orphan walk:** new `reachable_blocks(doc) -> set[str]`. BFS from modelspace + all paperspace layouts, plus DIMSTYLE block attributes (current list at 345-346), plus `DIMENSION.dxf.geometry`, plus any INSERT found in a reachable block, including `*` blocks. Both orphan functions delete only `name not in reachable` and keep the existing safe-name criteria. Layout blocks (`*Model_Space`, `*Paper_Space*`) are never deleted.
- **Units:** record `$INSUNITS` per file in `clean_stats` (`"insunits": int`) and in the manifest (`FileEntry.insunits`) for step 5.

**Tests (`tests/test_clean_safety.py`, on `fx_synth_hard` STR/MECH via `clean_v1._clean_one` and `clean_v2._clean_one`, parametrized)**
- `test_gusset_triangle_and_solid_survive`: a 3-vertex LWPOLYLINE on `STEEL-PLATE` and a SOLID on `STEEL-FILL` inside `Part-GUSSET` are still present.
- `test_revision_triangle_removed`: `REV-A` triangle in MECH is gone.
- `test_frame_and_civil_layers_survive`: entity counts on `FRAME-STEEL` and `CIVIL-FOUNDATION` are unchanged.
- `test_annotation_prefix_still_matches_am_and_rev`.
- `test_mtext_grid_label_survives` (flips the step-0 xfail); `test_attrib_grid_label_survives_and_non_grid_attrib_removed`.
- `test_block_referenced_only_from_anonymous_block_kept`: `Part-ANCHOR` still exists.
- `test_unreachable_block_removed`: an unreferenced `Part-ORPHAN` is gone.
- `test_insunits_recorded`.
- Existing `tests/test_clean_v1_preserves_protected_labels.py` must stay green (beam label still stripped).
- Flip `test_clean_keeps_gusset_triangles_and_frame_layers`.

**Risks:** real TP-104 behavior may change (more geometry kept). Acceptable and intended; the TP-104 tests are skipped here, so call this out in the commit message.
**Done when:** no shape-only deletion remains, and the default `annotation_layers` contains no bare words that could prefix-match structural layers.

---

## Step 4 — World-coordinate grid detection with ezdxf matrices
**Files:** new `core/grid.py`; `core/axes.py` (`_build_insert_lookup` 253-292, `_build_world_gridline_cache` 323-352, `_compute_world_positions` 355-370 become wrappers over `grid.walk_world`; `find_axes` 522-565 uses world positions and MTEXT/ATTRIB); `core/fit.py` (`_find_mech_axis` 57-100 and `_find_str_axis` 103-201 deleted and replaced by `grid.detect_grid`); `core/spotcheck.py` (`_extract_str_gridlines` should use `grid.detect_grid` if it parses GridLine blocks — check during implementation).

**Behavior (`core/grid.py`)**
- `walk_world(doc, layout=None, *, max_depth=16) -> Iterator[WorldItem(entity, m: Matrix44, chain: tuple[str,...], insert_handle_chain)]`.
  - Recursion: `m_child = insert.matrix44() * m_parent`. ezdxf row-vector convention: left factor is applied first. `matrix44()` includes the block base point.
  - Emits each INSERT's `attribs` with `m_parent`, because attribs are already in the INSERT's parent coordinates.
  - Cycle guard via chain names.
- `label_occurrences(doc, prefix, suffixes) -> dict[suffix, list[LabelOcc(world_pt, chain, kind)]]`: TEXT, MTEXT (`plain_text()`), ATTRIB. **All occurrences**, no dedup. Match = `^\s*{re.escape(prefix)}\s*(sfx)\s*$` (case-insensitive).
- `candidate_segments(doc)`: world-space straight segments from LINE and from open LWPOLYLINE/POLYLINE non-bulged segments, with length ≥ `0.15 × max(extents_w, extents_h)` (extents from `ezdxf.bbox.extents(msp, fast=True)`). Unit-independent; no layer whitelist. `GRID_LAYERS_MECH` becomes a *score bonus* (+0.5) instead of a filter.
- `merge_collinear(segs, ang_tol=1e-6 rad, off_tol=1e-6×extent)` gives `GridLineCand(point, dir, p_min, p_max, members, chains)`.
- Association per label occurrence, choosing the candidate that minimizes:
  1. **Same-INSERT rule:** if the occurrence's chain is non-empty and a candidate has a member from the same INSERT handle chain, that candidate wins outright (STR GridLine block pattern, independent of block-name prefix).
  2. Otherwise cost = `perp_dist(label, infinite line) + max(0, along_gap_beyond_endpoints)`, accepted only if `along_gap ≤ 0.25 × line length` and `perp_dist ≤ 0.05 × line length`.

  Each label takes the majority vote of its occurrences. A tie or no candidate gives `status="ambiguous"/"missing"` with reasons; nothing is silently dropped.
- `detect_grid(doc, prefix, suffixes) -> GridResult(axes: dict[sfx, GridAxis(point, dir, length, n_occ, status, reasons)], families: (list[sfx], list[sfx]))`. Families are formed by clustering directions (two clusters, |angle diff| > 20°). Letter/digit is only used for naming.
- `intersections(result) -> dict[tuple[sfx_a, sfx_b], Vec2]`, for axis pairs from different families. The key is ordered `(family0_label, family1_label)`, where family0 is the family containing the alphabetically-first label, for stability across drawings.

**Tests (`tests/test_grid.py`)**
- `test_walk_world_nested_rotated_mirrored_base_point`: hand-built doc, block with base point (5,5), INSERT rotation 30, nested INSERT xscale −1. Each LINE endpoint equals the hand-computed cos/sin value within 1e-9.
- `test_attrib_world_position`.
- `test_detect_grid_str_hard_matches_truth`: on `fx_synth_hard` STR, every axis line is within 1e-6×extent of the generator's STR axis (distance of two truth points to the detected line).
- `test_duplicate_labels_both_ends_vote_same_line`.
- `test_decoy_line_not_chosen` (the nearest-midpoint bug).
- `test_letters_on_y_axis`.
- `test_m_units_thresholds`.
- `test_missing_label_reported_not_silent`.
- `test_intersections_mech_match_truth` (within 1e-6).
- Flip `test_grid_detection_rotated_inserts`.
- Existing `tests/test_axes_detection.py` stays green. Keep `_transform_pt`, `_build_insert_lookup` and `extract_labels` dedup semantics; dedup is fine for *suggestion scoring*.

**Risks:** dashed gridlines split into many LINEs (handled by merging). Grid "bubbles" drawn as long polylines. Performance on large drawings: walking blocks once is O(entities × insert count). Cap the depth and use cached per-block segment lists transformed by matrices.
**Done when:** `fit.py` no longer contains block-name prefixes or absolute unit thresholds.

---

## Step 5 — Robust fit: similarity + RANSAC + leave-one-out + quality gates + `$INSUNITS` prior
**Files:** new `core/transform2d.py`; rewrite `core/fit.py` (`run` 219-310, delete `_fit_axis`'s silent fallback 212-213); `core/spotcheck.py:456-467,608-613,638,651` (use `Affine2D.inverse()`); `core/package.py:501-511`; `core/analyze.py:274-276`; `static/app.js:407-408` (display only, reading new fields with a fallback).

**Behavior**
- `transform2d.Affine2D(a,b,c,d,tx,ty)` with `apply`, `apply_many(np.ndarray)`, `inverse()`, `compose()`, `to_matrix44()`, `decompose() -> (scale, rotation_deg, mirrored, skew)`, `to_json()`, `from_json(d)`. `from_json` accepts the legacy format `{scale_x, scale_y, tx, ty}`.
- `fit_similarity(src, dst, allow_mirror=False)`: Umeyama closed form. `fit_affine(src, dst)` uses lstsq.
- `ransac_similarity(src, dst, tau)`: enumerate all pairs with distinct points (deterministic order). Inliers = residual ≤ tau. Best = max inliers, then min inlier RMS. Refit on the inliers.
- `tau = 0.002 × median MECH grid spacing` (median of adjacent axis distances, per family), with a floor of `1e-9 × extent`.
- `loo_residuals`: for each inlier i (requires ≥ 3 inliers), refit without i and measure the residual at i.
- Units prior: `expected = ezdxf.units.conversion_factor(str_ins, mech_ins)` when both are non-zero; otherwise None.
- `FitOptions(model="similarity", min_inliers=3, min_inlier_ratio=0.6, loo_tol_rel=0.005, scale_prior_tol=0.02, force=False)`, read from `job.options` (step 9) with defaults.
- Gates:
  - fewer than 2 correspondences → `FitError` ("need ≥2 shared grid intersections; found N: …")
  - 2 → `quality.status="weak"` and allowed only if `force`
  - inliers < min or ratio < min → error listing outlier labels
  - `loo_max > loo_tol_rel × median_spacing` → error
  - `|s/expected − 1| > scale_prior_tol` → error mentioning `$INSUNITS`, expected and found

  With `force=True`, every gate becomes a warning and `status="forced"`.
- `transform.json` (superset, back-compatible):
  - `model`, `matrix: [[a,b,tx],[c,d,ty]]`, `scale`, `rotation_deg`, `mirrored`
  - `correspondences: [{label, str, mech, predicted, residual, loo_residual, inlier}]`
  - `rms_residual`, `max_residual` (inliers), `loo_max`, `n_inliers`, `n_total`, `outliers: [label]`
  - `units: {mech, str, expected_scale}`, `quality: {status, warnings}`
  - legacy `scale_x=a`, `scale_y=d`, `tx`, `ty`, plus `x_labels`/`y_labels` (family 0/1 axis summaries) and `intersections` (alias of correspondences in the old shape) so `package.py`/`app.js` keep working
- Also write `grid_mech.json` / `grid_str.json` (axes with point/dir/status) under the pair root for steps 7/8.

**Tests (`tests/test_fit.py`)**
- `test_umeyama_exact`: random points, s=1000, θ=7.5°; parameters within 1e-9.
- `test_ransac_rejects_shifted_axis`: `shift_str_axis={"D": 300}`. The fit matches truth within 1e-6, `outliers` = the 3 `D-*` intersections, `quality.status=="ok"`.
- `test_two_correspondences_weak_requires_force`.
- `test_insufficient_raises_fiterror`.
- `test_loo_positive_with_noise`: noise 1 mm gives `0 < rms < loo_max`.
- `test_units_prior_mismatch_raises`: STR geometry in metres but `$INSUNITS=4`; message contains `INSUNITS`.
- `test_fit_stage_hard_pair`: job with `fx_synth_hard` through stages 1-6, then `fit.run`. For every shared intersection, `Affine2D.from_json(t).apply(str_pt)` is within 1e-6 (relative to MECH extent) of the MECH point. `|rotation_deg − 7.5| < 1e-9`, `|scale − 1000| < 1e-6`.
- `test_transform_json_legacy_keys_present`.
- `test_spotcheck_uses_inverse_affine`: with a rotated transform, the saddle-to-STR mapping round-trips (`inverse(apply(p)) ≈ p`).
- Flip `test_fit_recovers_rotation_and_units`.

**Risks:** spotcheck's internal logic assumes axis-aligned STR beams. It operates in STR coordinates after the inverse map, so it's still valid. Keep the legacy keys exact only when rotation is 0 and document that `scale_x/scale_y` are the matrix diagonal.
**Done when:** no code path returns a transform without `quality`, and there's no silent fallback.

---

## Step 6 — Overlay rewrite (matrix-based, block-preserving, logged)
**Files:** rewrite `core/overlay.py` (delete `_transform_xy`, `_transform_vertex`, `_prescale_entity`, `_transform_entity_inplace`, `_block_lookup`, `_overlay_per_entity` 31-435; new `overlay_docs()`; `run` 442-472 uses `roles.resolve_pair` + `dxfio` + `Affine2D`).

**Behavior**
- `overlay_docs(mech_doc, str_doc, affine, *, options) -> OverlayReport`:
  1. `mech_handles = {e.dxf.handle for e in msp}`, `mech_blocks = {b.name for b in mech_doc.blocks}`.
  2. `loader = xref.Loader(str_doc, mech_doc, conflict_policy=xref.ConflictPolicy.XREF_PREFIX)`; `loader.load_modelspace()`; `loader.execute(xref_prefix="STR")`.
  3. `new = [e for e in msp if e.dxf.handle not in mech_handles]`; `str_blocks = {b.name for b in mech_doc.blocks} - mech_blocks`. Build a `name_map` (STR original name → final name) by matching `STR$0$<n>` or the identical name.
  4. `log = ezdxf.transform.inplace(new, affine.to_matrix44())`. For every log entry, try fallback: if INSERT, `exploded = entity.explode()` (real entities in WCS, in msp), then `transform.inplace(exploded, m)`, and record `{handle, type, error, action:"exploded", n}`. Otherwise `msp.delete_entity(entity)` and record `action:"dropped"`. **Never leave an untransformed entity.**
  5. No bare `except`. Per-entity `except Exception as exc` appends to `report.failures`. Stage raises `OverlayError` if `dropped / len(new) > options.max_drop_ratio` (default 0.0 for similarity, i.e. any drop fails; 0.01 for affine).
  6. `OverlayReport`: `mech_entities`, `str_entities_imported`, `by_type_str`, `renamed_resources` (blocks/layers/linetypes/styles whose final name ≠ source), `failures`, `matrix`.
- `run` writes `out/superimposed.dxf` via `dxfio.save` and `stages/overlay_report.json` (pair root).
- Block definitions are never edited; repeated INSERTs keep their own transforms.

**Tests (`tests/test_overlay.py`)**, using `geomtruth` on cleaned synthetic files + truth (call `overlay_docs` directly on the generator output to isolate it from cleaning):
- `test_str_world_geometry_matches_truth`: `world_primitives(out, layer_pred=STR-origin)` matches `map_prims(world_primitives(str_doc), truth)`. During step 6, "STR-origin" = entities from `new` plus their INSERT expansions; collect via handles before step 7's layers.
- `test_mech_geometry_unchanged`: MECH primitives are identical (tol 1e-12).
- `test_repeated_block_inserts_not_collapsed`: 12 `Part-COL300` world centroids within 1e-6 of the truth-mapped intersections, all distinct. Flips the xfail.
- `test_nested_mirrored_rotated_bay`.
- `test_block_name_collision_no_hijack`: MECH `TAG` INSERT geometry is still the square; the STR `TAG` INSERT references `STR$0$TAG` (a circle). Flips the xfail.
- `test_entity_types_transform`: parametrized over ARC (with mirrored INSERT), ELLIPSE, CIRCLE, bulged LWPOLYLINE, HATCH edge path, TEXT (height scaled ×1000, rotation +7.5), SPLINE. Per-type match within tolerance.
- `test_affine_skew_triggers_logged_fallback`: an affine with skew on an INSERT gives `failures[0].action == "exploded"` and geometry still matches.
- `test_overlay_output_audits_clean`.
- `test_no_bare_except_in_overlay`: `ast` walk of `core/overlay.py`; no `ExceptHandler` whose body is only `pass`.

**Risks:** xref may emit warnings for dimension-style overrides (cleaned out anyway). Large files: `transform.inplace` is linear. HATCH with pattern scale: ezdxf scales the pattern under uniform scale (verify visually; not geometry-critical). `explode()` of INSERTs with ATTRIBs converts attribs to TEXT (acceptable, logged).
**Done when:** `core/overlay.py` is under ~200 lines, has no geometry-type switch, and the step-6 xfails are flipped.

---

## Step 7 — Per-source layers, styling options, QA|GRID layer
**Files:** `core/overlay.py` (new `apply_source_layers()`, `draw_qa_grid()`, `OverlayStyle`); `core/spotcheck_annotate.py` (no change expected; verify its layer is untouched).

**Behavior**
- `SOURCE_LAYER_SEP = "|"`. `OverlayStyle(color_mode="original"|"contrast", mech_color=5, str_color=1, str_lineweight: int|None = None, str_linetype: str|None = None, qa_grid=True)`.
- `apply_source_layers(doc, mech_entities, mech_blocks, str_entities, str_blocks, str_layer_map)`:
  - For each source and each layer used (msp entities of that source + entities in that source's blocks, excluding `"0"` and `"Defpoints"` **inside blocks**), create `"{SRC}|{orig}"`, copying `color`, `true_color`, `linetype`, `lineweight`, `plot`, `on`/`off`, `freeze`, `lock` from the source layer entry. STR source layers are looked up via `str_layer_map`, since the xref may have produced `STR$0$<n>`; strip that prefix for the display name.
  - Reassign `e.dxf.layer`. Delete now-unused original layer entries (skip `0`, `Defpoints`, current layer `$CLAYER` → set `$CLAYER="0"`).
  - Modelspace entities on `"0"` get `MECH|0` / `STR|0`.
- `contrast`: layer colors forced to `mech_color`/`str_color`; entity `dxf.color` set to 256 (BYLAYER) and `true_color` discarded for entities of that source (msp + its blocks), leaving 0 (BYBLOCK) as is. `str_lineweight`/`str_linetype` apply to `STR|*` layers; ensure the linetype exists (`ezdxf.tools.standards.linetypes()` → `doc.linetypes.add`).
- `draw_qa_grid(doc, transform_json, grid_mech, grid_str)` on layer `QA|GRID` (ACI 3):
  - MECH axes as LINEs
  - transformed STR axes as LINEs on `QA|GRID_STR` (ACI 6)
  - a CIRCLE at each MECH intersection (r = 0.01 × median spacing)
  - an X at each transformed STR intersection
  - TEXT `"{label} r={residual:.3g}"` on `QA|GRID` (height = 0.005 × median spacing)
  - outlier intersections highlighted on `QA|OUTLIER` (ACI 1)

**Tests (`tests/test_overlay_layers.py`)**
- `test_every_entity_on_source_or_qa_layer`: all modelspace entities' layers start with `MECH|`, `STR|` or `QA|`. Flips the xfail.
- `test_block_layer_zero_preserved`.
- `test_original_mode_preserves_layer_props`: `STR|FRAME-STEEL` color/linetype/lineweight equal the STR source.
- `test_contrast_mode_colors`: all `MECH|*` = 5, `STR|*` = 1; no STR-sourced entity has an explicit color other than 0/256.
- `test_str_lineweight_option`.
- `test_qa_grid_circles_at_mech_intersections`: circle centers within 1e-6 of `intersections_mech`; X centers within 1e-6 too (`fx_synth_hard`, exact fit).
- `test_output_reopens_audits_clean_with_pipe_layers`: `ezdxf.readfile` + `audit()` has no errors and layer names round-trip.

**Risks:** the `|` separator in AutoCAD (decision 7). Add a manual QA checklist item in the README. Original layers referenced by layer states/filters get deleted; keep them if `doc.layers` reports references, or skip deletion when unsure.
**Done when:** someone can toggle `STR|*` / `MECH|*` in a CAD viewer and inspect the overlap.

---

## Step 8 — Preview and report
**Files:** `core/package.py` (`_compute_extents` 134-311 replaced; `_drop_dangling_inserts` 314-340 counts and reports; `_render_preview` 343-465; `_build_report` 468-513).

**Behavior**
- Extents = full `ezdxf.bbox.extents(msp)` (non-fast, recursive through INSERTs), padded 5%. No percentile/IQR crop.
- `outliers`: entities whose bbox lies farther than `10 × grid diagonal` from the MECH grid bbox, listed with handle/layer/source and `fraction_of_extents`. They are reported, never hidden.
- Preview: in-memory recoloring (doc not saved): `MECH|*` → ACI 8 (grey), `STR|*` → ACI 1, `QA|*` → ACI 3, by setting layer colors and `Configuration(color_policy=ColorPolicy.COLOR)` default. Render the full extents; additionally render `preview_grid.png` cropped to the grid bbox + 10%. Switch reading to `dxfio.read_internal` and remove the readfile/recover comment once `test_recover_and_readfile_agree_on_superimposed` passes.
- `report.json` adds:
  - `roles` (manifest)
  - `counts: {mech_in, str_in, mech_out, str_out, by_layer_source, by_type_source}` (in = cleaned files, out = superimposed by layer prefix after recursive decomposition)
  - `fit` (quality, scale, rotation_deg, loo_max, outliers)
  - `overlay` (overlay_report: failures, renamed resources)
  - `extents: {full, mech, str, grid}`, `outliers`, `dangling_inserts_dropped`
  - `audit` (from `stages/audit.json`)
  - `transform` keeps the legacy keys.

**Tests (`tests/test_package.py`)**
- `test_preview_extents_include_stray`: `stray_str_entity_at=(1e7, 1e7)`; computed extents contain the transformed stray point; `report.outliers` lists its handle. Flips the xfail.
- `test_report_counts_consistent`: `str_out` decomposed primitive count == `str_in` decomposed count (excluding exploded/dropped from `overlay.failures`).
- `test_report_includes_fit_quality_and_failures`.
- `test_dangling_inserts_zero_on_synth`.
- `test_recover_and_readfile_agree_on_superimposed`.
- `test_preview_png_nonblank`: PIL image has more than 1 unique color.

**Risks:** rendering time on full extents with huge outliers (tiny drawing); acceptable, plus the grid-cropped second preview.
**Done when:** nothing in `package.py` silently discards geometry or entities.

---

## Step 9 — API/UI surface + README
**Files:** `jobs.py` (`JobState.options: dict = field(default_factory=dict)`; `create_job`/`create_job_legacy` accept `options`); `routers/api.py` (`create_overlay` 33-90 and `upload_n_files` 98-245 gain Form fields; `confirm_axes` 509-524 accepts an optional JSON body `{force: bool}`; `apply_axes` 452-506 uses `grid.detect_grid` and returns per-label status); `core/pair_runner.py` (pass options); `core/fit.py`/`core/overlay.py` (read `load_job(job_id).options`); `static/index.html`, `static/app.js` (controls + result display); `README.md`.

**Behavior**
- New form fields (validated, 400 on bad values): `color_mode` ∈ {original, contrast} (default original), `fit_model` ∈ {similarity, affine} (default similarity), `allow_weak_fit` (bool, default false → `FitOptions.force`), `str_lineweight` (int or empty), `qa_grid` (bool, default true). Persisted in `job.options` and echoed in the response.
- UI:
  - legacy upload labels read "MECH drawing" / "STR drawing" (explicit roles)
  - color-mode radio and "allow weak fit" checkbox
  - the result panel (`app.js:~400`) shows scale, rotation, n_inliers/n_total, rms, loo_max, quality status + warnings, outlier labels, overlay failure count, and links to `preview_grid.png`
- `/preview_grid.png` endpoint mirrors `/preview.png`.
- README:
  - stage list updated (roles manifest, `converted/`)
  - layer naming + QA layers
  - `transform.json` schema
  - quality gates and how to force
  - synthetic test fixtures (`tests/synth.py`) and how to run them
  - AutoCAD `|` caveat

**Tests (`tests/test_api_options.py`, TestClient)**
- `test_options_persisted_legacy_upload`.
- `test_bad_color_mode_400`.
- `test_n_file_upload_options_persisted`.
- `test_confirm_with_force_sets_option`.
- Existing `tests/test_n_file_upload.py` and `tests/test_legacy_migration.py` stay green (`load_job` filters unknown fields, so old job.json without `options` loads with the default).

**Done when:** every new option can be reached from the API and UI and is covered by a test.

---

## Step 10 — End-to-end synthetic test through the API
**Files:** `tests/test_e2e_synthetic.py` (remove the strict xfail added in step 0).

**Test `test_e2e_synthetic_legacy_flow`**, parametrized over `[HARD, HARD with str_units="mm" and rotation −3°]`, using `fx_isolated_jobs` + `TestClient(app)`:
1. `POST /api/v1/overlay` with `drawing_a = zz_plant.dxf` (MECH), `drawing_b = aa_frame.dxf` (STR), `color_mode=contrast`.
2. Poll `GET /{id}/status` (0.1 s interval, 60 s timeout) until `awaiting_axes`. Fail fast on `failed`, printing `error`.
3. `GET /{id}/axes`: `"TP104-"` is among the suggestion prefixes.
4. `POST /{id}/axes {"prefix": "TP104-", "axes": [A,B,C,D,1,2,3]}`: `all_matched` is true.
5. `POST /{id}/axes/confirm`; poll until `done`.
6. `GET /{id}/download`, then `ezdxf.read(io.StringIO(resp.text))`.
7. Assertions:
   - a) `grid.detect_grid` on `STR|*`-layer content of the output (layer-filtered doc copy) vs on `MECH|*`: every shared intersection within `1e-6 × MECH grid diagonal`.
   - b) `world_primitives(out, STR|*)` matches `map_prims(world_primitives(cleaned STR from jobs/<id>/cleaned/aa_frame_clean.dxf), truth)`.
   - c) `world_primitives(out, MECH|*)` matches the cleaned MECH primitives (tol 1e-9).
   - d) `transform.json`: `|rotation_deg − truth| < 1e-9`, `|scale − truth.scale|/truth.scale < 1e-9`, `quality.status == "ok"`, `outliers == []`.
   - e) `GET /{id}/report.json`: `overlay.failures == []`, `counts.str_out == counts.str_in`, `dangling_inserts_dropped == 0`.
   - f) `GET /{id}/preview.png` returns 200 and the PNG has more than 1 color.
   - g) Gusset triangle, `FRAME-STEEL` beams and the MTEXT/ATTRIB labels are present in the output (cleaning safety end to end).

**Test `test_e2e_synthetic_n_file_reversed_roles`:** `POST /upload` with files `[aa_frame.dxf, zz_plant.dxf]`, roles `"str,mech"`, prefix/axes given. Poll until done; check the pair's `out/superimposed.dxf` with assertions (a), (b) and (d).

**Test `test_e2e_outlier_axis_reported`:** `shift_str_axis={"D": 300}`. Done with `quality.status == "ok"`, `outliers` = D intersections, and a `QA|OUTLIER` layer with 3 markers.

**Risks:** thread timing in TestClient. Use the polling helper with a timeout and assert on status. Runtime should stay under 10 s per case at the synthetic sizes. Mark them `slow` but keep them in the default run (`pytest.ini` doesn't deselect `slow`).
**Done when:** all strict xfails are removed, the full suite is green, and `README.md` documents the end-to-end test.

---

## Sequencing and dependencies
0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10.
- Steps 1 and 2 are independent of the geometry steps but must come first so later tests can drive the pipeline by role.
- Step 4 is needed by step 5 (fit input) and step 7 (QA grid).
- Step 5's transform schema is needed by step 6. Step 6's handle/source tracking is needed by step 7. Step 7's layers are needed by step 8's counts and coloring.
- Each step is one commit. Commit message lists the flipped xfails and any behavior change visible on real drawings (step 3: more geometry kept; step 5: fits that pass today can now fail the quality gates; step 7: layer names change).

---

**Step list in short:**
- 0: synthetic MECH/STR generator with known truth, plus the comparison harness and strict-xfail tests for each known bug.
- 1: a roles manifest (`stages/files.json`) and `roles.resolve_pair`, replacing every filename/alphabetical role guess.
- 2: `core/dxfio.py` (recover/audit everywhere), a separate `converted/` directory, odafc `replace=True`, no re-globbing.
- 3: safe cleaning — no shape-only triangle deletion, exact-vs-prefix layer matching (FRAME/CIVIL removed), MTEXT/ATTRIB grid labels kept, reachability-based orphan-block walk, `$INSUNITS` recorded.
- 4: `core/grid.py` grid detection in world coordinates using ezdxf matrices (all label occurrences, merged collinear lines, rotation- and unit-independent), producing intersection correspondences.
- 5: `Affine2D` + similarity fit with deterministic RANSAC, leave-one-out residuals, quality gates and a `$INSUNITS` scale prior; transform consumers updated.
- 6: overlay rewritten on `xref.Loader` + `transform.inplace`, block definitions never edited, explode/drop fallbacks logged, no bare excepts.
- 7: `MECH|` / `STR|` layers with original or contrast coloring, lineweight/linetype options, and `QA|GRID` / `QA|OUTLIER` layers.
- 8: preview on full extents (plus a grid-cropped preview) and a report with counts, failures, fit quality, outliers and audit results.
- 9: API/UI options persisted in `job.options`, explicit MECH/STR labels in the UI, README.
- 10: end-to-end TestClient runs (legacy flow, N-file upload with reversed roles, outlier case) checking `superimposed.dxf` against ground truth.
