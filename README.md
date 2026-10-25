# overlay-app

**Standalone web app for superimposing a structural (STR) drawing onto a
mechanical (MECH) drawing** — a browser UI on top of the ISGEC overlay
pipeline.

```
ISGEC/
├── A/   B/   C/        ← original pipeline scripts (superseded by core/)
└── overlay/            ← THIS repo
```

Drop in two drawings, confirm their grid convention, get a superimposed DXF back
with the two sources on separate layers, a QA grid of the fit, a preview PNG and
a full audit report.

---

## Quick start

```bash
cd overlay/
pip install -r requirements.txt
./run.sh                                   # http://localhost:8000
                                           # PORT=8765 ./run.sh for another port
```

Open <http://localhost:8000>, drop a **MECH drawing** and an **STR drawing**
(`.dwg` or `.dxf`), pick the grid convention, wait for the pipeline. When it
finishes (~30–75 s on TP-104-scale drawings) the Done panel shows the transform,
the fit quality, and the download / preview links.

Run the test suite:

```bash
python3 -m pytest -q
```

---

## Roles are explicit, never guessed

Two drawings of one building differ by **units, rotation and offset**. The
pipeline therefore needs to know which file is which *before* it does anything,
and it gets that from **you**, not from the filename:

* the 2-file form labels are literally `MECH drawing` and `STR drawing`;
* the N-file form has a per-file role selector (`MECH` / `STR` / `other`);
* the roles are written once, at convert time, into a manifest
  `jobs/<id>/stages/files.json`, and every later stage reads that.

Nothing in `core/` globs a directory or sorts a filename list to decide a role
(`grep -rn 'sorted(.*glob' core routers` is empty by design).

### The manifest

```jsonc
[
  {"file_id": "a1b2c3d4", "upload":    "uploads/zz_plant.dxf",
   "role": "mech",        "converted": "converted/zz_plant.dxf",
   "cleaned":  "cleaned/zz_plant_clean.dxf",  "insunits": 4},
  {"file_id": "e5f6a7b8", "upload":    "uploads/aa_frame.dxf",
   "role": "str",         "converted": "converted/aa_frame.dxf",
   "cleaned":  "cleaned/aa_frame_clean.dxf",  "insunits": 6}
]
```

`role` ∈ `mech` | `str` | `other`. `insunits` is the drawing's `$INSUNITS`,
recorded at clean time and used as the fit's scale prior.

Job dirs written before the manifest existed have none; `core/roles.py` rebuilds
the same information from `job.json`, so old jobs still load.

---

## `converted/` vs `cleaned/`

Two separate directories, and **no stage ever globs to discover its inputs**:

| Directory | Written by | Contains |
|---|---|---|
| `uploads/` | the API | the bytes you uploaded, under their original names |
| `converted/` | stage 3 (convert) | `<stem>.dxf` — normalised, re-audited, DWG→DXF via ODA if needed |
| `cleaned/` | stage 6 (clean) | `<stem>_clean.dxf` — annotations/markers stripped, grid labels kept |

Splitting them is what removed the whole class of "`*_clean.dxf` gets picked up
as an input on a re-run" bugs. An upload that happens to be *named*
`foo_clean.dxf` still gets cleaned and still gets a `_clean` suffix on its
output; the name is never used to mean anything.

---

## The pipeline

The status rail in the UI has 11 rows; stages 1–6 run unattended, stage 7 pauses
for you, and 8–11 run after you confirm.

| # | Stage | Module | What happens |
|---|---|---|---|
| 1 | Validate | `core/validate.py` | sniff the format, read each upload with `ezdxf.recover`, refuse a broken file |
| 2 | Inspect | `core/inspect.py` | entity + layer counts (no rendering) |
| 3 | Convert | `core/convert.py` | → `converted/<stem>.dxf`; writes the roles manifest; DWG via ODA (`replace=True`, so re-runs are safe) |
| 4 | Scan grid labels | `core/labels.py` | finds the labels both drawings agree on → `protected_handles.json`, `cross_drawing_agreement.json` |
| 5 | Derive cleaning policy | `core/clean_policy.py` | project-specific label regex / keep-prefixes / marker + annotation layers → `stages/clean_policy.json` (editable from the UI's Advanced panel) |
| 6 | Clean | `core/clean_v1.py` / `clean_v2.py` | → `cleaned/<stem>_clean.dxf` |
| 7 | **Grid axes** | `core/axes.py`, `core/grid.py` | ⏸ **waits for you.** Auto-suggest a prefix, then match the axes you confirm |
| 8 | Fit | `core/fit.py` | shared grid intersections → similarity transform (Umeyama + RANSAC) with quality gates → `transform.json`, `grid_mech.json`, `grid_str.json` |
| 9 | Overlay | `core/overlay.py` | import STR via `xref.Loader`, transform in one `ezdxf.transform.inplace`, then paint `MECH\|`/`STR\|` layers and draw the QA grid |
| 9.4 | Mismatch | `core/mismatch/` | MECH⇄STR item-level misalignment (bolts, supports, containment, clash) → `stages/mismatch.json` + `QA|MISMATCH_*` markers |
| 9.5 | Spot-check | `core/spotcheck.py`, `core/{mech,str}_intra.py`, `core/spotcheck_annotate.py` | support / clash issues + an annotated DXF |
| 9.6 | Findings | `core/findings.py` | the native findings + the remaining spot-check issues → `out/findings.{json,csv,docx}` |
| 10 | Package | `core/package.py` | `preview.png` (full extents), `preview_grid.png` (grid close-up), `preview_meta.json`, `report.json` |
| 11 | Done | — | download links |

Spot-check is non-fatal: if it raises, the job still finishes and says so.
The mismatch stage is non-fatal too, and it distinguishes three states —
`ok`, `disabled` (the policy turned it off) and `error`. A check that did
not run is **never** reported as "0 findings".

---

## Layer naming in the output DXF

Every entity in `superimposed.dxf` ends up on a layer that says which drawing it
came from, so you can switch one drawing off in any CAD viewer:

| Layer | Meaning |
|---|---|
| `MECH\|<original layer>` | everything that came from the MECH drawing |
| `STR\|<original layer>` | everything that came from the STR drawing |
| `QA\|GRID` (ACI 3) | the MECH gridlines, a CIRCLE per intersection, and a `"{label} r={residual}"` TEXT per correspondence |
| `QA\|GRID_STR` (ACI 6) | the STR gridlines mapped through the fitted transform, with an X per intersection |
| `QA\|OUTLIER` (ACI 1) | an X at every intersection the fit rejected |
| `QA\|MISMATCH_CRITICAL` (ACI 1) | a mismatch marker: circle + offset arrow + label |
| `QA\|MISMATCH_WARNING` (ACI 30) | the same, for a warning |
| `QA\|MISMATCH_INFO` (ACI 8) | the same, for an informational finding |
| `QA\|MISMATCH_REF` (ACI 4) | ghost outlines of the two items involved |

The separator `|` is the constant `core.overlay.SOURCE_LAYER_SEP`.

Toggling in CAD:

* AutoCAD / BricsCAD / DWG TrueView — use `FILTER` (or the layer-isolate icon):
  isolate `STR|*` to see only the structural drawing, isolate `MECH|*` for the
  mechanical one, and hide all `QA|*` layers for a clean read of the overlap.
  (Layer filters may need `STR\*` rather than `STR|*`.)
* AQLite / ezdxf / any viewer with wildcard layers — `STR|*` and `MECH|*` are
  ordinary layer names; a wildcard match on the prefix works.

Layer `0` inside a **block definition** is deliberately left alone so ByBlock /
ByLayer inheritance still resolves to the INSERT's own `MECH|*` / `STR|*` layer.
Modelspace entities on layer `0` *are* renamed (`MECH|0`, `STR|0`).

### ⚠ AutoCAD `|` caveat

AutoCAD reserves `|` for **xref-dependent block symbols**. ezdxf's DXF validator
accepts `MECH|CENTER` and the round-trip through AutoCAD has not been
re-verified since the separator was introduced. **Do a manual check in AutoCAD /
ODA before shipping a drawing to a reviewer**: open `superimposed.dxf`, confirm
no repair prompt, and confirm the `MECH|*` / `STR|*` / `QA|*` layers are listed.
If a viewer objects, change `SOURCE_LAYER_SEP` in `core/overlay.py` to `"$"` —
nothing else refers to the character, and every layer is renamed consistently on
the next run.

---

## `transform.json`

Written by stage 8 next to the DXF (`jobs/<id>/transform.json`, or
`jobs/<id>/out/pairs/<pair_id>/transform.json` for a multi-pair job).

```jsonc
{
  "model": "similarity",              // or "affine" (diagnostic)
  "matrix": [[a, b, tx], [c, d, ty]],
  "a": 0.9914, "b": -0.1305, "c": 0.1305, "d": 0.9914,
  "tx": 208379.86, "ty": -179584.52,

  "scale": 1000.0,                   // uniform scale (mean column norm)
  "rotation_deg": 7.5,
  "mirrored": false,
  "skew_deg": 0.0,                   // 0 for a similarity; grows for a shear

  "correspondences": [               // one per shared grid intersection
    {"label": "A-1",
     "str": [1.0, 2.0], "mech": [208380.86, -179582.52],
     "predicted": [208380.86, -179582.52],
     "residual": 4.1e-07, "loo_residual": 5.0e-07, "inlier": true}
  ],

  "rms_residual": 8.7e-11,           // over the inliers
  "max_residual": 2.9e-10,
  "loo_max": 9.2e-11,                // worst leave-one-out residual, null if n/a
  "n_inliers": 12, "n_total": 12, "inlier_ratio": 1.0,
  "outliers": [],                    // labels the fit rejected
  "tau": 12.0,                       // RANSAC inlier radius used
  "median_spacing": 6000.0,          // median adjacent-axis distance (MECH units)

  "units": {"mech": 4, "str": 6, "expected_scale": 1000.0},
  "quality": {"status": "ok", "warnings": []},   // ok | forced
  "warnings": [], "skipped_axes": [],

  // legacy keys, still read by package.py / app.js
  "scale_x": 0.9914, "scale_y": 0.9914,
  "x_labels": [...], "y_labels": [...], "intersections": [...]
}
```

`scale_x` / `scale_y` are the **matrix diagonal** (`a` and `d`), *not* the
column norms — for a rotated fit they are `scale·cos(rotation_deg)`. They only
mean something when `rotation_deg == 0`; use `scale` and `rotation_deg`
otherwise. `column_scales` carries the true column norms.

`grid_mech.json` / `grid_str.json` sit next to it: every detected axis with its
world `point`, `dir`, `length`, `n_occurrences`, `status`
(`ok` | `ambiguous` | `missing`) and the detector's `reasons`, plus the computed
`intersections`. When an axis didn't match, that file says why.

---

## Fit quality gates and how to force

The transform is only written if it survives five gates (`core/fit.py`):

| Gate | Fails when |
|---|---|
| enough intersections | fewer than 2 shared grid intersections — **not forceable**, there is nothing to fit |
| not just two points | exactly 2 correspondences: the fit is exact but unverifiable |
| enough inliers | `n_inliers < 3` or the inlier ratio `< 60%` |
| leave-one-out | `loo_max > 0.005 × median grid spacing` — the fit depends on one point |
| `$INSUNITS` prior | `\|scale / expected − 1\| > 2%`, where `expected` comes from the two `$INSUNITS` headers |

Each failure message names the problem and what to do (which labels are
outliers, what the unit headers claim, …).

**Forcing.** Every gate except the first becomes a warning when `force` is set;
the transform is then written with `quality.status == "forced"` and a
`warnings` entry saying which gate was overridden.

Two ways to ask for it:

```bash
# at upload time — the "Allow weak fit" checkbox
curl -F drawing_a=@mech.dxf -F drawing_b=@str.dxf \
     -F allow_weak_fit=true http://localhost:8000/api/v1/overlay

# or after the axes report, when you've seen which gate is complaining
curl -X POST -H 'Content-Type: application/json' \
     -d '{"force": true}' \
     http://localhost:8000/api/v1/overlay/<job_id>/axes/confirm
```

---

## Options (`job.options`)

Five knobs, accepted by **both** upload endpoints, validated with a **400**
naming the offending field, and stored in `job.options`:

| Form field | Type | Default | Effect |
|---|---|---|---|
| `color_mode` | `original` \| `contrast` | `original` | `original` keeps each drawing's own layer colours; `contrast` forces `MECH\|*` to ACI 5 (blue) and `STR\|*` to ACI 1 (red) and sets every entity to BYLAYER |
| `fit_model` | `similarity` \| `affine` | `similarity` | `similarity` = uniform scale + rotation + translation. `affine` allows shear and is diagnostic only |
| `allow_weak_fit` | bool | `false` | `FitOptions.force` — see the gates above |
| `str_lineweight` | int (1/100 mm) or empty | empty | force this lineweight on every `STR\|*` layer. DXF's range is 0–211 (2.11 mm); empty means "as drawn" |
| `qa_grid` | bool | `true` | draw `QA\|GRID` / `QA\|GRID_STR` / `QA\|OUTLIER` |

Booleans accept `true/false`, `1/0`, `on/off`, `yes/no` (case-insensitive);
the enums are trimmed and case-folded. An empty form field means "use the
default".

They are echoed back in the upload response and readable afterwards at
`GET /api/v1/overlay/{id}`. A per-run override is also accepted by
`POST /{job_id}/pairs` (`{"options": {...}}`).

### The stored shape

`job.options` carries the **flat** keys the stage readers use, plus two
**derived** namespaces that are regenerated from them, so the two can never
disagree:

```jsonc
{
  "color_mode": "contrast",
  "fit_model": "similarity",
  "allow_weak_fit": false,
  "str_lineweight": 50,
  "qa_grid": true,

  "fit":     {"model": "similarity", "force": false},
  "overlay": {"color_mode": "contrast", "str_lineweight": 50, "qa_grid": true}
}
```

* `core.fit.FitOptions.from_job` reads the flat `fit_model` / `allow_weak_fit`
  (aliases `model` / `force`).
* `core.overlay.OverlayStyle.from_options` reads the flat keys or the `overlay`
  sub-dict, which wins.

A job.json with no `options` field (every job written before this existed) loads
with `options == {}`, i.e. every default. Unknown fields in an old job.json are
dropped on load, as before.

---

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/overlay` | 2-file upload (`drawing_a` = MECH, `drawing_b` = STR) + options → `{job_id, options, …}` |
| `POST` | `/api/v1/overlay/upload` | N-file upload with `roles` (comma-separated, parallel to `files`) → cross-product of MECH×STR pairs |
| `GET` | `/api/v1/overlay/{id}` | full job state (includes `transform` and `options`) |
| `GET` | `/api/v1/overlay/{id}/status` | lightweight state, polled every 500 ms |
| `GET` | `/api/v1/overlay/{id}/axes` | auto-suggested conventions + the derived cleaning policy |
| `POST` | `/api/v1/overlay/{id}/axes` | `{prefix, axes}` → per-label grid status and the paired axes |
| `POST` | `/api/v1/overlay/{id}/axes/confirm` | optional `{"force": true}`; resumes stages 8–11 |
| `POST` | `/api/v1/overlay/{id}/policy` | edit the cleaning policy while `awaiting_axes` |
| `GET` | `/api/v1/overlay/{id}/download` | the superimposed DXF |
| `GET` | `/api/v1/overlay/{id}/preview.png` | preview over the **full** extents |
| `GET` | `/api/v1/overlay/{id}/preview_grid.png` | preview cropped to the grid box + 10% |
| `GET` | `/api/v1/overlay/{id}/preview_meta.json` | world → pixel map of `preview.png` |
| `GET` | `/api/v1/overlay/{id}/report.json` | counts, fit quality, overlay failures, outliers, audit |
| `GET`/`POST` | `/api/v1/overlay/{id}/pairs` | list / run a pair in the multi-pair layout |
| `GET` | `/api/v1/overlay/{id}/findings` | the findings, filtered (`severity`, `kind`, `origin`, `min_magnitude_mm`, `bbox`, `limit`, `offset`) |
| `GET` | `/api/v1/overlay/{id}/findings/{fid}` | one finding, by id (`MM-0001`) or uid |
| `GET` | `/api/v1/overlay/{id}/findings.csv`, `/findings.docx` | the findings as a file |
| `GET`/`PUT` | `/api/v1/overlay/{id}/mismatch/policy` | the resolved mismatch policy / replace it (a bad policy is a 400) |
| `POST` | `/api/v1/overlay/{id}/mismatch/rerun` | re-run the mismatch check — no refit |
| `GET` | `/api/v1/overlay/{id}/download.dwg` | the superimposed drawing as DWG (needs the ODA File Converter; 501 when it is missing) |
| `GET` | `/api/v1/capabilities` | what this server can do (`dwg_export`, `dwg_versions`) |
| `GET` | `/api/v1/overlay/{id}/pairs/{pair_id}/{artifact}` | one pair artifact (whitelisted; `..` is rejected) |
| `GET` | `/api/v1/overlay/{id}/analyze` | grid-intersection depth analysis + issues |
| `GET` | `/api/v1/overlay/{id}/spotcheck.json`, `/spotcheck.docx`, `/annotated.dxf` | spot-check outputs |

`POST /{id}/axes` returns, per label, what `core.grid.detect_grid` made of it:

```jsonc
{
  "all_matched": true,
  "pairs":  [{"suffix": "A", "type": "letter", "a_pos": [...], "b_pos": [...], "delta": 0.0}],
  "labels": [
    {"suffix": "A", "type": "letter", "matched": true, "status": "ok",
     "mech": {"status": "ok", "reasons": [], "n_occurrences": 2, "n_agree": 2, "point": [...], "dir": [...]},
     "str":  {"status": "ok", "reasons": [], "n_occurrences": 2, "n_agree": 2, "point": [...], "dir": [...]}}
  ],
  "all_ok": true,
  "problems": []          // {"suffix": "D", "source": "str", "status": "ambiguous", "reasons": [...]}
}
```

---

## Output layout

```
jobs/<job_id>/
├── job.json                  full state (atomic write)
├── status.json               legacy lightweight state
├── uploads/                  raw uploads
├── converted/<stem>.dxf      normalised inputs
├── cleaned/<stem>_clean.dxf  stage-6 output
├── axes/convention.json      the confirmed grid convention (+ per-label status)
├── stages/
│   ├── files.json            the roles manifest
│   ├── clean_policy.json     the derived (and editable) cleaning policy
│   ├── audit.json            per-stage recover/audit summaries
│   ├── overlay_report.json   what stage 9 imported, renamed and failed on
│   └── mismatch.json         the MECH⇄STR mismatch result (diagnostic)
├── transform.json            the fit (see above)
├── grid_mech.json, grid_str.json
└── out/
    ├── superimposed.dxf              ← the deliverable
    ├── superimposed_annotated.dxf   + spot-check clouds
    ├── preview.png                   full extents
    ├── preview_grid.png              grid box + 10%
    ├── preview_meta.json             world → pixel map
    ├── report.json                   the audit trail
    ├── spotcheck.{json,docx}
    └── findings.{json,csv,docx}   the merged findings
```

A multi-pair job instead nests each pair under `out/pairs/<pair_id>/` with the
same layout plus `pair.json` (the pair's own roles) and `metadata.json`.

---

## Synthetic fixtures and the test suite

`tests/synth.py` builds a MECH/STR pair **with a known transform**, so every
stage can be checked against ground truth instead of eyeballed:

```python
from synth import build_pair, HARD, LEGACY_COMPATIBLE
pair = build_pair(tmp_path, HARD)     # 7.5° rotation, metres, rotated inserts,
                                      # MTEXT + ATTRIB labels, a decoy line,
                                      # nested/mirrored blocks
print(pair.mech_path, pair.str_path, pair.truth.scale, pair.truth.rotation_deg)
```

`LEGACY_COMPATIBLE` is the easy case (rotation 0, mm, plain labels) that the
old pipeline could already handle; `HARD` is everything it could not. The two
filenames are `zz_plant.dxf` and `aa_frame.dxf` — deliberately chosen to sort
the *wrong* way and to contain neither `MECH` nor `STR`, so any stage that
guesses a role from a filename fails immediately.

`tests/geomtruth.py` is the other half: it walks a document with
`ezdxf.disassemble.recursive_decompose`, flattens everything into
world-space primitives (curves, text, hatch boundaries) and compares two sets
by symmetric Hausdorff distance. That is how "did the overlay preserve the
geometry?" is answered without trusting a screenshot.

Running them:

```bash
python3 -m pytest -q                       # everything
python3 -m pytest -q tests/test_fit.py     # one stage
python3 -m pytest -q -m "not slow"         # skip the full-pipeline runs
python3 -m pytest -q -k synth              # only the synthetic fixtures
```

Fixtures available in `tests/conftest.py`:

| Fixture | What it gives you |
|---|---|
| `fx_isolated_jobs` | `jobs.JOBS_DIR` redirected to `tmp_path` for the test |
| `fx_tp104_dxfs` | the real TP-104 DXFs, renamed to the `_clean.dxf` convention (skips if absent) |
| `fx_job_with_cleaned_dxfs` | a legacy job with those two files already in `cleaned/` |
| `fx_synth_simple` / `fx_synth_hard` | a `SynthPair` built from `LEGACY_COMPATIBLE` / `HARD` |
| `fx_synth_job` | a factory: `fx_synth_job(opts) -> (job_id, SynthPair)`, uploads staged, pipeline not started |

Tests that need the real TP-104 files (`ISGEC/dxf/`, or `$ISGEC_DIR`) **skip**
when they are missing rather than fail. Point `ISGEC_DIR` at a checkout to run
them:

```bash
ISGEC_DIR=/path/to/ISGEC python3 -m pytest -q
```

There is also a live-server smoke test that does not use pytest:

```bash
./run.sh &
python3 tests/smoke_test.py          # upload → poll → pick axes → download
```

---

## Architecture

```
Browser (static/index.html + app.js, vanilla JS state machine, 500 ms polling)
  │
  │  POST   /api/v1/overlay              (multipart, + options)
  │  GET    /{id}/status                 (polled every 500 ms)
  │  GET    /{id}/axes                   POST /{id}/axes
  │  POST   /{id}/axes/confirm           (optional {"force": true})
  │  GET    /{id}/download, /preview*.png, /report.json
  ▼
FastAPI (app.py)
├── routers/api.py    every endpoint; validation; the worker threads
├── pipelines.py      the stage driver (stages 1–6, then 8–11)
├── jobs.py           JobState on disk + the roles manifest helpers
└── core/
    ├── roles.py      the manifest + resolve_pair
    ├── dxfio.py      read_input / read_internal / save / record_audit
    ├── grid.py       world-coordinate grid detection
    ├── transform2d.py Affine2D, Umeyama, RANSAC, leave-one-out
    ├── package.py    extents, previews, report.json
    └── …             one module per stage
```

Each `core/<stage>.py` exposes `run(job_id, *, pair_dir=None)`. `pair_dir`
scopes a run to one pair's directory (`out/pairs/<pair_id>/`); `None` is the
legacy single-pair layout at the job root.

---

## Known limitations

- **No auth** — single-tenant. Don't expose this on a public network.
- **DWG needs ODA File Converter** (`which ODAFileConverter`). Without it,
  convert to DXF first. DXF input never needs it.
- **The grid convention is user-supplied.** The stages cannot infer which
  labels are grid axes; the UI asks, and `grid_*.json` reports what happened.
- **Once you press Start the worker runs to completion** even if you close the
  browser. The output stays on disk — reopen the page and the job is still
  there.
- **Affine is diagnostic.** It allows shear, which two drawings of one building
  should never have; it exists to diagnose, not to ship.
- **The `|` layer separator has not been re-verified in AutoCAD** — see the
  caveat above.

---

## Development

```bash
./run.sh                                                  # --reload under the hood
python3 -m uvicorn app:app --host 0.0.0.0 --port 8765 --reload
python3 -m pytest -q
ls -lh jobs/<job_id>/out/
```

Runtime data lives in `jobs/` and is gitignored, so the whole directory can be
moved anywhere and `./run.sh` still works.

---

## License

Same terms as the underlying ISGEC pipeline scripts it grew out of.