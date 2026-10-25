# PLAN_MISMATCH — Step 8b (mismatch detection) and Step 8c (DWG export)

Inserted after Step 8 (preview/report), before Step 9 (API/UI). Existing steps are NOT
renumbered (step-0 strict-xfail reason strings stay valid). New sequencing:
0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 8b → 8c → 9 → 10.
8b.1–8b.4 (pure detection) only need steps 1, 2, 4, 5 and can be built in parallel with 6–8;
8b.5–8b.6 need 6, 7, 8.

## Purpose
After the fit, find the places where MECH does not physically agree with STR/civil. For each one,
say where it is, what kind of problem it is, which way and how far it is off, and how bad it is.
Rank the list and show it in the DXF, the report, the API and the UI. Grid residuals
(Step 5) say whether the *drawings* line up. Step 8b says whether the *items* line up.

## Key decisions
1. **Source geometry = `converted/` docs, not `cleaned/`.** The MECH cleaner keeps only `A$C`/`RTR`
   INSERTs (`core/clean_policy.py:keep_block_prefixes_mech`), so bolt and saddle blocks with other
   names are removed before overlay. Detection reads the converted MECH and STR files. STR is mapped
   to MECH world with `Affine2D.to_matrix44()` from `transform.json`. Fallback if converted files are
   missing: use the cleaned files and set `source_docs:"cleaned"` + a warning.
2. **Everything is computed in MECH world coordinates.** Coordinates are in MECH drawing units.
   All lengths reported (offsets, magnitudes, tolerances) are in **mm**, with an `_mm` suffix.
   `mm_per_unit = ezdxf.units.conversion_factor(mech_insunits, 4)`. If `$INSUNITS` is 0, assume mm
   and add the warning `units_assumed_mm`. Families are classified on *world-mapped* sizes, so STR
   drawn in metres works.
3. **Matrices only.** Uses `grid.walk_world(doc, root=M)` (amend Step 4: add a `root: Matrix44|None`
   parameter, so the first matrix applied is `root` and `m_child = insert.matrix44() * m_parent`).
   Features are extracted once per block definition (block-local) and mapped per INSERT occurrence
   with numpy: `local @ M[:2,:2] + M[3,:2]`.
4. **Deterministic.** Features sorted at extraction; matching greedy over sorted cost tuples;
   RANSAC-style fits enumerate combinations in a fixed order. IDs assigned after the final sort.
   Every finding also gets a content-hash `uid`.
5. **Hierarchical (no duplicates).** Each bolt pattern checked as a whole first (translation and
   rotation). Individual bolts reported only when their residual after the pattern fit is still too
   large. One defect gives one finding.
6. **Local vs systematic.** Every displacement is compared with the median displacement of its
   neighbours and with the fit uncertainty at that point (see §3).
7. **"No findings" must not be confused with "nothing was checked".** `findings.json` carries
   `status` and `coverage` (how many items of each family were found and checked). The UI warns
   when a family count is 0.

## Package layout
- `core/spatial.py` — `GridIndex` (uniform grid hash over bboxes, numpy, deterministic).
- `core/mismatch/__init__.py` — `detect(mech_doc, str_doc, affine, *, policy, fit_ctx=None) -> MismatchResult`;
  `run(job_id, *, pair_dir=None)` (stage entry point; `_run_stage_module("mismatch")` works because the package defines `run`).
- `core/mismatch/model.py` — `EntityRef`, `Finding`, kinds, severities, JSON/CSV serialisers.
- `core/mismatch/policy.py` — `MismatchPolicy`, `DEFAULT_POLICY`, `load_policy(job_id, pair_root)`, `PolicyError`, `policy_hash`.
- `core/mismatch/features.py` — feature extraction (circles, rects, polygons, segments, bands, points) with a per-block cache.
- `core/mismatch/classify.py` — family assignment (layer/block regex + size range).
- `core/mismatch/checks/{bolts.py, support.py, containment.py, clash.py}`.
- `core/mismatch/context.py` — `FitContext` (fit uncertainty field), local/systematic analysis.
- `core/mismatch/render.py` — the `QA|MISMATCH_*` layers in `superimposed.dxf`.
- `core/findings.py` — merges native findings with spotcheck issues; writes `out/findings.{json,csv,docx}`.
- `core/export_dwg.py` — Step 8c.

## 1. Geometry: features, families, checks

### 1.1 Feature extraction (`features.py`)
For each `WorldItem(entity, m, chain)`:
- **Effective layer:** an entity on layer `"0"` inside a block takes the layer of the nearest INSERT
  in its chain whose layer is not `"0"`. When matching, strip the display prefixes `MECH|`, `STR|`, `STR$0$`.
- **Layer prefilter (performance):** a per-layer-name cache holds the set of families that could
  match. If the effective layer matches no family's layer regex and no block in the chain matches a
  family's block regex, skip the entity with one dict lookup.
- **OCS:** CIRCLE/ARC/LWPOLYLINE/ELLIPSE use `e.ocs().to_wcs(...)` before `m` is applied.
- **Scale check:** `sx=|m.transform_direction((1,0,0))|`, `sy=|...(0,1,0)|`. If `|sx−sy|/sx > 1e-6`,
  a circle is non-uniformly scaled. Skip it and count `skipped_nonuniform`.
- **Circle** `(cx, cy, r)` from: CIRCLE; ARC with span ≥ 350°; ELLIPSE with ratio ≥ 0.999;
  a closed 2-vertex polyline with |bulge| = 1 ±1e-6 on both vertices; a regular closed polygon with
  n ∈ {6, 8} (hex nut / octagon) or n ≥ 12 with radial std/mean < 2% (exploded circle). Regular
  hexagons and octagons get `r=None` (centre only).
- **Rect** (oriented: centre, unit axis u, w along u, h): a closed LWPOLYLINE/POLYLINE with 4
  distinct vertices, no bulges, every corner 90° ±0.5° (checked after transform); SOLID/TRACE with
  4 points. Optional `reconstruct_rects_from_lines` (default true, only for families with feature
  `rect` and only after the layer prefilter).
- **Polygon:** any other closed polyline, flattened with `ezdxf.path.make_path(e).flattening(...)`.
  Store whether it is convex and its convex hull.
- **Segment:** a LINE, or an open polyline segment with no bulge.
- **Point:** POINT entities, and INSERTs whose block or layer matches a *point family*.
- **Band** (beam in plan: centreline a→b, half-width hw, `assumed_width` flag), STR member family only:
  (a) a member rect with aspect ≥ 3 → centreline on the long axis, hw = h/2.
  (b) parallel segment pairs: bucket by angle mod 180° in 0.5° bins (also check adjacent bins), find
      partners with GridIndex. A pair qualifies if parallel within 0.5°, perpendicular gap in
      `[band_min_mm=50, band_max_mm=600]`, and overlap along the axis ≥ 50% of the shorter segment.
      Pair greedily by gap, **largest first**, so flange edges are paired rather than web lines.
      Segments inside a band already made are absorbed.
  (c) an unpaired segment with length ≥ `min_member_len_mm=500` → band with
      hw = `default_flange_mm/2` (100) and `assumed_width=True`. Findings from such bands capped at WARNING.
- Every feature has an `EntityRef`, world bbox and size. Per-block cache key is
  `(block_name, effective_layer_of_parent)`. Mapping a cached block to an instance is vectorised.

### 1.2 Families (`classify.py`, configured in the policy)
A feature belongs to family F if its source and feature type match F, **and** (layer regex matches
the effective layer **or** block regex matches any block name in the chain), **and** its size in mm
is inside F's range. A feature can belong to several families.

| family | src | feature | layer regex | block regex | size (mm) |
|---|---|---|---|---|---|
| mech_bolt | MECH | circle | `bolt\|nut\|fastener\|stud` | `bolt\|nut\|stud\|flange` | r 3–40 |
| mech_plate_hole | MECH | circle | `baseplate\|base.?plate\|hole` | `baseplate\|base.?plate` | r 3–45 |
| mech_support | MECH | rect | `saddle\|support\|shoe\|foot\|base.?plate` | `saddle\|shoe\|^SUP` | w,h 50–5000 |
| mech_support_pt | MECH | point/insert | `pipe.?supp\|^ps\b\|support.?point` | `^PS-\|^PS_` | — |
| mech_equipment | MECH | rect/polygon | `equip\|main object\|vessel\|pump\|tank` | — | w 200–30000 |
| mech_skid | MECH | rect/polygon | `skid` | `skid` | w 300–30000 |
| str_hole | STR | circle | `hole\|bolt\|plate` | `hole\|bolt\|plate` | r 3–45 |
| str_anchor | STR | circle | `anchor` | `anchor` | r 3–45 |
| str_plate | STR | rect/polygon | `plate` | `plate` | w 50–3000 |
| str_member | STR | band | `frame\|beam\|member\|girder\|joist` | `beam` | len ≥ 500 |
| str_column | STR | rect/polygon | `\bcol\|column\|post` | `col` | w 100–3000 |
| str_outline | STR | rect/polygon | `civil\|found\|plinth\|pedestal` | `plinth\|found` | w ≥ 300 |

Check pairings (policy `pairs`):
- bolts: `mech_bolt↔str_hole`, `mech_plate_hole↔str_anchor`, with plate fallback `→str_plate`
- support: `mech_support→str_member`, `mech_support_pt→str_member`
- containment: `mech_skid→str_outline`
- clash: `mech_equipment↔str_column`

### 1.3 Checks and kinds
Notation: tol = allowed value in mm; ratio = local_magnitude / tol_eff; offset = **STR position − MECH position**
(the direction the MECH item would have to move).

**A. Bolt/hole concentricity and pattern fit (`checks/bolts.py`)**
1. **Patterns.** Group MECH bolts into connected components (single link, eps `pattern_eps_mm=400`,
   radius equal within 10%). Components never cross root-INSERT instances; msp-level bolts are
   grouped among themselves. Sort patterns by (centroid x, y, first handle).
2. **Per-bolt search radius:** `R_b = min(search_bolt_mm=150, 0.45 × min distance between bolts of the
   pattern, 0.25 × median grid spacing)`. Candidate holes = counterpart-family circles within R_b
   (GridIndex). Assignment: put all (bolt, hole, d) pairs in a list, sort by (d, bolt idx, hole idx),
   take one-to-one greedily.
3. **Rigid fit** (rotation + translation, no scale; Kabsch) on the assigned pairs. With k ≤ 32
   bolts, enumerate every pair of assigned pairs in index order; each gives a rigid motion. Inliers
   are pairs with residual ≤ tol. Pick the most inliers, then the lowest median residual, then the
   first in order. Refit on the inliers. If fewer than 2 inliers, use the least-squares fit on all
   pairs. If k > 32, take every ⌈k/32⌉-th pair, in order.
4. **Tolerance:** `tol = |r_hole − r_bolt| + concentric_abs_mm(0.5)`. If either radius is None,
   use `concentric_default_mm=2.0`. For the pattern, take the minimum over the assigned pairs.
5. **Pattern finding** (only if pattern size ≥ 2 and ≥ 2 pairs are assigned). Let c = MECH centroid,
   t_c = R·c + t − c, and θ = fitted rotation. Then `d_rot = 2·r_max·sin(|θ|/2)`, where r_max is the
   largest bolt distance from c.
   - |t_c| > tol and d_rot ≤ tol → **PATTERN_OFFSET** (magnitude |t_c|).
   - d_rot > tol and |t_c| ≤ tol → **PATTERN_ROTATED** (magnitude d_rot, `rotation_deg` = θ, CCW positive, STR relative to MECH).
   - both → **PATTERN_MISFIT** (magnitude = largest bolt displacement under the fitted motion).
6. **Per-bolt residual after the inlier fit** > tol → **BOLT_HOLE_OFFSET** at the bolt centre.
   A pattern of size 1 always uses BOLT_HOLE_OFFSET.
7. **Unassigned bolts:**
   - If the pattern has ≥ 1 assigned bolt → **MISSING_HOLE** at each unassigned bolt (magnitude null).
   - If it has none → plate fallback: str_plate polygons within R = pattern radius + 300 mm. Each
     bolt outside the plate → **BOLT_OFF_PLATE** (magnitude = distance outside). Each bolt inside but
     closer than `bolt_edge_min_mm=20` to the edge → BOLT_OFF_PLATE WARNING (magnitude = shortfall).
   - If no plate either → one **MISSING_COUNTERPART** for the pattern.
8. STR holes not assigned to any bolt within a matched pattern's bbox+R_b → **EXTRA_HOLE**
   (INFO, `enabled:false` by default).

**B. Support on member (`checks/support.py`)**
- **Rect support S.** Candidate bands have their bbox within `search_member_mm=1000` of S. For band B:
  - n = band normal; `s_perp` = extent of S along n; `e` = perpendicular offset of S's centroid from
    B's centreline (signed, clamped to the segment).
  - `allowed = |2·hw − s_perp|/2 + support_tol_mm(10)` (covers both narrow saddles on wide flanges
    and wide saddles spanning a flange).
  - Choose the band with the smallest |e|/allowed, then the largest perpendicular overlap, then index.
  - If |e| > allowed → **SUPPORT_OFF_MEMBER**: offset = −e·n, magnitude |e|. Details:
    `bearing_ratio = perp_overlap / min(2hw, s_perp)`, `allowed_mm`, `assumed_width`.
    CRITICAL if bearing_ratio < `crit_bearing=0.5`, else WARNING.
  - Overhang along the beam: the part of S's extent along u past the band ends, if it exceeds
    support_tol → SUPPORT_OFF_MEMBER with an offset along u.
  - No band within search → **MISSING_COUNTERPART**.
- **Point support P.** Distance e to the nearest band centreline (clamped). `allowed = hw + point_tol_mm(5)`.
  If e > allowed → **SUPPORT_POINT_OFF_MEMBER**, CRITICAL, offset = vector to the centreline,
  magnitude e, `details.outside_by_mm = e − hw`. No band within search → MISSING_COUNTERPART.

**C. Containment and edge distance (`checks/containment.py`)**
- MECH footprint F (skid) against str_outline polygons O that overlap F's bbox. Choose the O with
  the largest overlap area (convex clip, Sutherland–Hodgman; for non-convex O, the fraction of F's
  vertices inside). If none overlaps → nothing (`requires_counterpart:false` by default).
- Signed distance of every F vertex to O's boundary (positive = outside).
  - max > `outline_tol_mm=5` → **OUTSIDE_OUTLINE**, CRITICAL, magnitude = max, offset = from the
    worst vertex to its nearest boundary point.
  - Otherwise, if `edge = min(inside distance) < min_edge_mm=50` → **EDGE_DISTANCE**, WARNING,
    magnitude = 50 − edge, offset along the inward normal of the closest O edge,
    `details.edge_distance_mm = edge`.

**D. Clash (`checks/clash.py`)**
- mech_equipment polygons against str_column polygons. Candidates come from GridIndex bbox overlap.
  SAT on convex polygons (convex hull + `details.hull_approx=true` for non-convex) gives the
  penetration depth and minimum translation vector (MTV).
  - Depth > `clash_tol_mm=1` → **CLASH**, CRITICAL, magnitude = depth, offset = MTV (moves MECH out).
  - **CLEARANCE** (gap < `min_clearance_mm`) disabled by default (`min_clearance_mm=0`): in plan
    view without elevations it produces noise.
  - A plan-view overlap with *beams* is normal (equipment sits above floor beams), so beams are
    excluded unless `clash_with_members=true`.

**E. SYSTEMATIC_OFFSET** (regional/global, see §3).

### 1.4 Counterpart matching rules
- Candidates: same pairing family, within a search radius R. R is in mm, converted to units, and
  capped at 0.25 × median MECH grid spacing so tiny or huge drawings do not cross-match.
- Cost: `(d/R) + w_size·|ln(r_a/r_b)| (+1 if feature types differ)`. Global greedy one-to-one
  assignment for bolts; many-to-one for supports→members. If the runner-up cost is within 10% of
  the best, set `details.ambiguous=true`.

### 1.5 Policy JSON (`MismatchPolicy`, strict `from_dict`; unknown key → `PolicyError`)
Precedence (each overrides the one before it): `DEFAULT_POLICY` → `jobs/<id>/mismatch_policy.json`
→ `<pair_root>/mismatch_policy.json` → `job.options["mismatch"]`.

```json
{
  "enabled": true,
  "source_docs": "converted",
  "families": { "see table §1.2" },
  "pairs": {"bolts": [["mech_bolt","str_hole"],["mech_plate_hole","str_anchor"]],
            "plate_fallback": "str_plate",
            "support": [["mech_support","str_member"],["mech_support_pt","str_member"]],
            "containment": [["mech_skid","str_outline"]],
            "clash": [["mech_equipment","str_column"]]},
  "checks": {"bolts": true, "support": true, "containment": true, "clash": true, "extra_hole": false},
  "tol_mm": {"concentric_abs": 0.5, "concentric_default": 2.0, "bolt_edge_min": 20,
             "support": 10, "point": 5, "outline": 5, "min_edge": 50, "clash": 1, "min_clearance": 0},
  "search_mm": {"bolt": 150, "pattern_eps": 400, "member": 1000, "outline": 1000},
  "band": {"min_mm": 50, "max_mm": 600, "default_flange_mm": 200, "min_member_len_mm": 500},
  "crit_bearing": 0.5,
  "context": {"k_neighbours": 8, "radius_mm": 5000, "min_neighbours": 3, "k_fit": 3.0,
              "systematic_min_items": 5, "systematic_min_fraction": 0.3, "emit_systematic_items": "info"},
  "detect_polygon_circles": true, "reconstruct_rects_from_lines": true,
  "restrict_to": "all",
  "render": {"marker_radius_mm": "auto", "draw_info": false, "draw_refs": true},
  "max_findings": 5000
}
```

`restrict_to` may also be `"grid_bbox"` with `grid_margin_bays: 1`.

## 2. Data model (`model.py`)
```python
@dataclass(frozen=True)
class EntityRef:
    source: str                 # "MECH" | "STR"
    handle: str                 # leaf entity handle in the *converted* source doc
    insert_chain: tuple[str, ...]
    block: str                  # innermost block name ("" = modelspace)
    root_block: str
    layer: str                  # effective layer, display name (prefixes stripped)
    family: str
    feature: str                # circle|rect|polygon|band|point|pattern
    size_mm: dict
    in_overlay: bool | None

@dataclass
class Finding:
    id: str                     # "MM-0001" (assigned after ranking)
    uid: str                    # sha1(kind|mech chain+handle|str chain+handle)[:12]
    kind: str
    severity: str               # CRITICAL | WARNING | INFO
    origin: str                 # "mismatch" | "spotcheck"
    mech_ref: EntityRef | None
    str_ref: EntityRef | None
    location: tuple[float, float]          # MECH world, drawing units
    str_location: tuple[float, float] | None
    offset_mm: tuple[float, float] | None  # STR − MECH
    magnitude_mm: float | None
    local_offset_mm: tuple[float, float] | None
    local_magnitude_mm: float | None
    tolerance_mm: float
    tolerance_eff_mm: float
    ratio: float | None
    rotation_deg: float | None = None
    systematic: bool = False
    within_fit_uncertainty: bool = False
    confidence: str = "normal"
    message: str = ""
    details: dict = field(default_factory=dict)
```

`KINDS = (BOLT_HOLE_OFFSET, MISSING_HOLE, BOLT_OFF_PLATE, PATTERN_OFFSET, PATTERN_ROTATED,
PATTERN_MISFIT, EXTRA_HOLE, SUPPORT_OFF_MEMBER, SUPPORT_POINT_OFF_MEMBER, OUTSIDE_OUTLINE,
EDGE_DISTANCE, CLASH, CLEARANCE, MISSING_COUNTERPART, SYSTEMATIC_OFFSET)`.

Messages are templates, e.g.
`"Bolt M-pattern BOLT-M20-4 bolt #3 is 6.0 mm off hole Part-PLATE300-H4 (allowed 1.5 mm)"`.

**Ranking:** sort by severity (CRITICAL < WARNING < INFO), then `-round(ratio, 6)` (None last), then
kind, then `round(x, 6)`, `round(y, 6)`, mech handle, str handle. Stable sort. Truncate at
`max_findings` (lowest-ranked first), set `truncated`.

**Spatial index (`core/spatial.py`):**
```python
class GridIndex:
    def __init__(self, bboxes: np.ndarray, cell: float | None = None)  # (N,4) xmin,ymin,xmax,ymax
    @staticmethod
    def auto_cell(bboxes) -> float
    def query_bbox(self, bbox) -> np.ndarray
    def query_radius(self, xy, r) -> np.ndarray
    def knn(self, xy, k, rmax) -> np.ndarray
```
Items spanning more than 64 cells go in a `large` list every query scans.

## 3. Fit quality vs local mismatch (`context.py`)
- `FitContext.from_transform_json(t)`: inlier correspondences (MECH point, `loo_residual` or
  `residual`), `rms_residual`, `quality.status`. With no correspondences, σ ≡ 0.
  - `sigma_at(p) = max(rms, IDW_k3_power2(loo_residuals))`, in mm.
  - `tol_eff = hypot(tol, k_fit · sigma_at(p))`.
- **Displacement items:** pattern translations t_c, single-bolt offsets, and for supports only the
  normal component. For each item i:
  - N_i = up to `k_neighbours` other items within `radius_mm` (GridIndex.knn).
  - If |N_i| ≥ `min_neighbours`: m_i = component-wise median of their offsets. Otherwise
    m_i = global median (if ≥ min_neighbours items in total), else (0, 0).
  - For supports, project m_i onto the band normal.
  - `local_i = o_i − m_i`.
- **Decision for displacement kinds:**
  - |local| > tol_eff → a local finding. Severity per kind; ratio = |local|/tol_eff.
  - tol < |local| ≤ tol_eff → INFO, `within_fit_uncertainty=True`.
  - |local| ≤ tol < |o| → `systematic=True`, INFO if `emit_systematic_items="info"`, else dropped.
- **Systematic clusters:** single-link groups of systematic items within `radius_mm`. Each cluster
  with ≥ `systematic_min_items` items (and ≥ `systematic_min_fraction` of all displacement items if
  it is global) becomes one **SYSTEMATIC_OFFSET** finding (WARNING):
  - location = cluster centroid, offset = median, magnitude = |median|, `details.n_items`, `details.bbox`.
  - Message says whether fit residuals near the cluster are small ("grids agree; MECH items are
    consistently shifted by X mm — likely drawing-wide datum/offset discrepancy") or large
    ("consistent with fit residual — check grid/fit").
- **Fit quality:** if `quality.status ∈ {weak, forced}`, every finding gets `confidence="low"` and
  `findings.json.warnings` includes `fit_quality_<status>`. A finding within 0.5 bay of a fit outlier
  intersection gets `details.near_grid_outlier=<label>`.
- Non-displacement kinds (MISSING_*, CLASH, OUTSIDE_OUTLINE, EDGE_DISTANCE) use tol_eff but no
  neighbour median.

## 4. Outputs
- **`stages/mismatch.json`** (raw, diagnostic): `status`, `policy` (resolved), `policy_hash`, `units`
  `{mech_insunits, mm_per_unit, assumed}`, `source_docs`, `features` `{family: n}`, `skipped`,
  `coverage` `{check: {items, matched, unmatched}}`, `fit_context`, `timings_s`, `findings[]`.
- **`out/findings.json`** (canonical, merged, see §5): `{status: "ok|error|disabled", generated_at,
  units, summary: {by_severity, by_kind, by_origin}, coverage, warnings, truncated, findings[]}`.
- **`out/findings.csv`** columns:
  `id,uid,kind,severity,origin,x,y,str_x,str_y,dx_mm,dy_mm,magnitude_mm,local_dx_mm,local_dy_mm,local_magnitude_mm,tolerance_mm,tolerance_eff_mm,ratio,rotation_deg,systematic,within_fit_uncertainty,confidence,mech_handle,mech_block,mech_layer,str_handle,str_block,str_layer,message`
- **`out/findings.docx`:** factor the severity-heading/colour code out of
  `spotcheck_annotate._build_docx` into `_severity_heading(doc, sev, n)`. Summary table (counts per
  severity/kind, coverage, fit status, units) then a section per finding.
- **DXF layers `QA|MISMATCH_CRITICAL` (ACI 1), `_WARNING` (ACI 30), `_INFO` (ACI 8), `_REF` (ACI 4)**,
  drawn into `out/superimposed.dxf` (re-saved with `dxfio.save`).
  - Idempotent: delete existing `QA|MISMATCH*` entities first.
  - `r = marker_radius_mm` ("auto" = max(50 mm, 0.03 × median grid spacing)).
  - CIRCLE at the location with XDATA appid `OVERLAPQA` `[(1000,id),(1000,uid),(1000,kind)]`.
  - Arrow LINE from location to location + k·offset (k = 1 if |offset| ≥ 0.5r, else ⌈0.5r/|offset|⌉,
    capped 1000), plus two arrowhead LINEs at ±25°.
  - TEXT `"{id} {kind} {mag:.1f}mm"` + `" (x{k})"` when k > 1, height 0.4r, at location + (r, r).
  - PATTERN_ROTATED: an ARC showing θ. SYSTEMATIC_OFFSET: a large circle around the cluster bbox.
  - On `_REF`, ghost outlines of the MECH ref and the STR ref.
  - INFO drawn only if `draw_info`.
- **`report.json["mismatch"]`**: `enabled, status, policy_hash, units, source_docs, features,
  coverage, summary, systematic[], fit_context, top` (first 10), `warnings, timings_s, truncated`.
- **Preview (amend Step 8):** `_render_preview` runs `fig.canvas.draw()` and maps two world corners
  through `ax.transData.transform`, flipping y by figure height in pixels. Writes
  `out/preview_meta.json` `{png_w, png_h, world_to_px: [[sx,0,tx],[0,sy,ty]], world_extents}`.
  `QA|MISMATCH*` colours are left alone by the preview recolouring.
- **API** (prefix `/api/v1/overlay`):
  - `GET /{job_id}/findings?pair_id=&severity=CRITICAL,WARNING&kind=&origin=mismatch|spotcheck|all&min_magnitude_mm=&bbox=x0,y0,x1,y1&limit=200&offset=0`
    → `{job_id, pair_id, status, units, summary` (unfiltered)`, coverage, warnings, total, returned, items}`.
    Bad severity/kind/bbox → 400. Limit ≤ 5000. Missing file → 404.
  - `GET /{job_id}/findings/{fid}` (id or uid) → 404 if unknown.
  - `GET /{job_id}/findings.csv`, `GET /{job_id}/findings.docx`.
  - `GET|PUT /{job_id}/mismatch/policy` (PUT validates → 400 with message).
  - `POST /{job_id}/mismatch/rerun?pair_id=` → run in threadpool; mismatch.run → spotcheck dedupe →
    findings.run → re-render → report refresh. No refit.
  - Pair artifact allowlist: add `findings.json`, `findings.csv`, `findings.docx` (subdir `out`)
    and `mismatch.json` (new subdir `stages`).
- **UI:** a "Findings" panel replacing the issues table, reusing `renderIssuesPage` paging. Severity
  chips, kind select, "include systematic/INFO" toggle. Columns: id, kind, severity, x, y, offset
  (dx, dy mm), |local| mm, tol mm, ratio, MECH ref, STR ref, message. Coverage banner; red warning
  when a family has 0 items. Row click maps world→pixel via `preview_meta.world_to_px`, draws an SVG
  ring over the preview `<img>`, zooms with CSS `transform: scale(4)`. Buttons: "Reset zoom",
  "Copy coords".
- **Pipeline wiring:** `pipelines.resume_after_axes` and `core/pair_runner.run_pair`:
  fit → overlay → **mismatch** → mech_intra → str_intra → spotcheck (dedupe) → **findings** →
  spotcheck_annotate → package.
  - The mismatch stage is non-fatal; on exception it writes `findings.json` `status:"error"` +
    message. The UI shows "mismatch check failed", **never** "0 findings".
  - `pair.json` gains `mech_converted` / `str_converted`.
  - `roles.resolve_pair(job_id, pair_dir, stage="converted"|"cleaned")`.

## 5. Reconciliation with spotcheck
- Keep `mech_intra` / `str_intra` unchanged.
- `spotcheck.run` gains `dedupe_against: Path | None` (default `<root>/stages/mismatch.json` if it exists).
  - A cross-drawing issue is **superseded** if a native finding of an equivalent kind exists with the
    same MECH leaf handle, **or** its location is within `dedupe_radius = max(500 mm, half the
    footprint diagonal)`.
  - Equivalences: NO_BEAM_SUPPORT ≡ {SUPPORT_OFF_MEMBER, SUPPORT_POINT_OFF_MEMBER, MISSING_COUNTERPART};
    COLUMN_CLASH ≡ CLASH; BAY_OVERHANG ≡ OUTSIDE_OUTLINE.
  - Superseded issues move to `superseded: [{...issue, by: uid}]` in `spotcheck.json`.
- `core/findings.run` merges native findings with the remaining spotcheck issues (cross + intra).
  Each issue becomes a `Finding` with `origin="spotcheck"`, `kind="SPOT_"+type`,
  `magnitude_mm = distance·mm_per_unit` (None for area-typed), `id` keeps `ISSUE-/MECH-/STR-###`,
  `message=note`.
- Spotcheck's absolute-mm thresholds stay (documented as legacy).

## 6. Synthetic fixtures and tests

### 6.1 `tests/synth.py` extensions (existing fixtures unchanged)
New `SynthOptions` fields: `mismatch_cases: tuple[str,...] = ()`, `mismatch_params: dict = {}`,
`mismatch_global_shift_mm = (0.0, 0.0)` (applied to MECH scene entities only, in frame P),
`scene_origin = (30000.0, 0.0)`, `scene_pitch = 6000.0`, `scene_reverse_order = False`.

New dataclasses: `ExpectedFinding(case, kind, severity, location_mech, magnitude_mm, offset_mm,
rotation_deg=None, local_magnitude_mm=None, details={})`, `ControlRegion(case, center_mech, radius_mm)`.

`SynthPair` gains `mismatch_expected` and `mismatch_controls`, computed analytically. MECH is only
translated, so `location_mech = P + mech_offset` and the offsets are in P axes. Scene blocks defined
once. MECH blocks deliberately NOT named `A$C*` (proves reading converted files). STR blocks named
`Part-*`.

Cell (c, r) origin O = `scene_origin + (pitch·c, pitch·r)`. Constraint: every scene LINE must be
shorter than 0.15 × extent so Step 4 never takes it for a grid line.

| case | cell | MECH | STR | expected |
|---|---|---|---|---|
| ctl_bp4 | (0,0) | `BOLT-M20-4`: 4 circles r10 at (±100,±100), layer BOLT | `Part-PLATE300-H4`: rect 300² (STEEL-PLATE) + 4 circles r11 (layer 0, INSERT on STEEL-HOLE) | control r=400 |
| ctl_bp_rot | (1,0) | `BOLT-ASYM` 5 bolts (−150,−75),(150,−75),(150,75),(−150,75),(0,75), INSERT rot 30 | `Part-PLATE-ASYM` with the same points pre-rotated 30° and pre-mirrored, INSERT xscale −1 | control |
| bolt_offset | (2,0) | as ctl_bp4, but bolt (100,100) moved 6 mm at 35° | `Part-PLATE300-H4` | BOLT_HOLE_OFFSET, CRITICAL, mag 6.0, offset −6(cos35°, sin35°), tol 1.5 |
| pattern_offset | (3,0) | ctl_bp4 block inserted at O + 8(cos120°, sin120°) | H4 at O | PATTERN_OFFSET, CRITICAL, mag 8.0, offset (4, −6.928); exactly 1 finding |
| anchor_rotated | (4,0) | `BASEPLATE-400`: rect 400² (BASEPLATE) + 4 holes r15 at (±150,±150) | `Part-ANCHOR4`: 4 circles r12 (ANCHOR-BOLT), INSERT rot +4° | PATTERN_ROTATED, CRITICAL, rotation +4.0°, mag 2·212.132·sin2° = 14.806 |
| flange_pcd_offset | (5,0) | `FLANGE-8B`: 8 bolts r8 on PCD r150, INSERT at O + (0, 5) | `Part-FLGPLATE-8H`: holes r9 | PATTERN_OFFSET, mag 5.0, offset (0, −5) |
| missing_hole | (0,1) | ctl_bp4 bolts | `Part-PLATE300-H3` (hole (−100,−100) omitted) | MISSING_HOLE, CRITICAL, at bolt (−100,−100) |
| bolt_off_plate | (1,1) | ctl_bp4 bolts at O + (90, 0) | `Part-PLATE300` (plate, no holes) | 2 × BOLT_OFF_PLATE, CRITICAL, mag 40 |
| saddle_off_beam | (2,1) | LWPOLYLINE 300×150 on SADDLE, centre (0, 120) | 2 LINEs FRAME-STEEL y = ±100, x ∈ [−2500, 2500] | SUPPORT_OFF_MEMBER, CRITICAL, mag 120, offset (0, −120), bearing 0.367 ±0.01 |
| ctl_saddle | (3,1) | saddles at (0,0) and (20,500) → perpendicular shift 20 ≤ 35 | `Part-BEAM-W200` (closed rect 5000×200), INSERT rot 90 | control ×2 |
| pipe_support_off_flange | (4,1) | `PS-01` (circle r50 + cross, layer 0), INSERT on PIPE-SUPPORT at (0, 160) | 2-line band y = ±100 | SUPPORT_POINT_OFF_MEMBER, CRITICAL, mag 160, outside_by 60 |
| ctl_pipe_support | (5,1) | PS-01 at (−1000, 0) and (1000, 40) | same band | control ×2 |
| missing_counterpart | (0,2) | saddle 400×250 on SADDLE | nothing within 1000 | MISSING_COUNTERPART, CRITICAL |
| outside_outline | (1,2) | skid 1800×1000 on EQUIP-SKID at (+250, 0) | plinth 2000×1200 CIVIL-FOUNDATION | OUTSIDE_OUTLINE, CRITICAL, mag 150, offset (−150, 0) |
| edge_distance | (2,2) | skid at (+70, 0) | plinth | EDGE_DISTANCE, WARNING, mag 20, edge_distance 30 |
| ctl_outline | (3,2) | skid centred | plinth | control |
| clash | (4,2) | rect 1000² on EQUIPMENT, centre (−570, 0) | `Part-COL300` at O | CLASH, CRITICAL, mag 80, offset (−80, 0) |
| ctl_clash | (5,2) | rect centre (−850, 0) (200 gap) | `Part-COL300` | control |

Presets: `ALL_MISMATCH_CASES`, `MISMATCH = with_opts(LEGACY_COMPATIBLE, mismatch_cases=ALL_MISMATCH_CASES)`,
`MISMATCH_HARD = with_opts(HARD, mismatch_cases=ALL_MISMATCH_CASES)`,
`MISMATCH_GLOBAL = with_opts(LEGACY_COMPATIBLE, mismatch_cases=("pattern_array",), mismatch_global_shift_mm=(25.0, 0.0))`
where `pattern_array` is a 4×4 grid of ctl_bp4 at 1500 mm pitch from the scene origin, element (2,1)
getting an extra MECH shift of (0, +8).

Base content (A$C1234, Part-BAY, columns, FRAME-STEEL lines, footing) must give **zero** findings.
If it does not, fix the defaults, not the test.

### 6.2 Unit tests (`tests/test_mismatch_units.py`)
`test_gridindex_matches_bruteforce`, `test_kabsch_rigid_exact`, `test_circle_from_bulge_polyline_and_hexnut`,
`test_extrusion_minus_z_circle_center`, `test_nonuniform_insert_circle_skipped_and_counted`,
`test_rect_detection_mirrored_rotated_insert`, `test_band_pairing_prefers_flange_edges`,
`test_unpaired_segment_assumed_width`, `test_sat_penetration_axis_aligned_and_rotated`,
`test_vertex_outside_distance`, `test_layer_zero_inherits_insert_layer`, `test_policy_unknown_key_raises`,
`test_policy_bad_regex_raises`, `test_policy_precedence_job_over_default`, `test_policy_hash_stable`,
`test_fit_uncertainty_inflates_tolerance`.

### 6.3 Detection tests (`tests/test_mismatch_synth.py`)
Parametrized over `[MISMATCH, MISMATCH_HARD]`: `test_each_defect_detected` (exactly one finding per
ExpectedFinding; kind, severity, location within 1 mm, magnitude within 5%, offset cosine similarity
≥ 0.99, rotation within 5%, details within 5%), `test_no_false_positives`,
`test_pattern_offset_single_finding`, `test_coverage_counts`, `test_ranking_order`,
`test_deterministic_and_order_independent`, `test_mech_units_metres`,
`test_policy_override_changes_result`, `test_global_shift_systematic`.

### 6.4 Stage/integration/API tests
`tests/test_mismatch_stage.py`, `tests/test_findings_api.py`, plus a preview_meta test and
Step 10's `test_e2e_mismatch_flow`.

### 6.5 Performance (`tests/test_mismatch_perf.py`, `@pytest.mark.slow`)
`tests/synth_perf.py: build_perf_docs(...)`: n_side=71 → ~5041 patterns (~20k bolts, 20k holes),
50k MECH + 100k STR noise lines (>170k entities), 10 seeded defects. Assert exactly 10 PATTERN_OFFSET
findings and `detect()` under `MISMATCH_PERF_BUDGET_S` (env, default 90 s).

## 7. Sub-steps (each one commit; suite green at each)
- **8b.1** synth scene + expected truth + fixture tests + strict xfail.
- **8b.2** `core/spatial.py`, `model.py`, `policy.py` + unit tests.
- **8b.3** `features.py`, `classify.py` (+ `walk_world(root=)` amendment) + unit tests.
- **8b.4** checks + `context.py` + `detect()`; detection tests; flip the xfail.
- **8b.5** `run()` stage, `render.py`, `core/findings.py`, spotcheck dedupe, pipeline and
  pair_runner wiring, report section, `resolve_pair(stage=)`.
- **8b.6** API endpoints, allowlist, preview_meta, UI panel.
- **8b.7** perf fixture + tests.

## 8. Limitations and tunables (to go in the README)
- **Plan view only.** Z and elevations ignored. A plan overlap is not a clash (beams excluded from
  clash, clearance off by default, containment opt-in per family pair). Sections/elevations placed
  beside the plan → `restrict_to:"grid_bbox"`.
- **Naming conventions.** Families depend on layer and block regexes; real projects need tuning.
  `coverage` and `features` counts show what was recognised; 0 means "not checked", not "OK".
- **Exploded geometry.** Polygon circles, bulge circles, hex nuts and 4-LINE rects handled. NOT
  handled: splines and hatch-only depictions, ACAD_PROXY_ENTITY / AEC/MEP custom objects (counted in
  `skipped.proxy`), XREFs (not resolved), paper space, polyline widths, dashed lines broken into
  many short segments.
- **Bands.** Flange width from paired edges or `default_flange_mm`, not from section text. Double
  beams closer than `band_max_mm` can be mis-paired.
- **Patterns.** If an offset is at least half the bolt pitch, bolts are assigned to the wrong holes
  (aliasing). Highly symmetric patterns report the smallest consistent rotation.
- **Local vs systematic.** A defect shared by most neighbours is labelled systematic (still reported
  as a SYSTEMATIC_OFFSET region). Sparse areas fall back to the global median.
- **Fit dependency.** Everything inherits the Step 5 transform. Weak/forced fits make all findings
  `confidence:"low"`. Under an affine model, circles under skew become ellipses and are skipped.
- **Units.** A wrong `$INSUNITS` in the MECH file scales every tolerance.
- **Performance.** ~10–20 µs per entity. >1M entities may need the layer prefilter tightened.

---

## Step 8c — Optional DWG export of the superimposed result
**Files:** new `core/export_dwg.py`; `routers/api.py`; `static/app.js`/`index.html`; `tests/test_export_dwg.py`.
- `available() -> bool` = `odafc.is_installed()`.
- `export(dxf_path, dwg_path, *, version="R2018", safe_layers=True) -> DwgExportResult(ok, path, message, layer_renames)`:
  - Not available → `ok=False`, message: "DWG export unavailable: ODA File Converter not installed
    (configure ezdxf [odafc-addon] unix_exec_path / win_exec_path). DXF download is unaffected."
  - `version` must be in `odafc.VERSION_MAP`, else `ValueError` → 400.
  - `doc = dxfio.read_internal(dxf_path)`. If `safe_layers`, rename layers containing `|` to `$`
    via `layer.rename(...)`; on collision append `_2`, `_3`, …
  - `odafc.export_dwg(doc, dwg_path, version=version, audit=True, replace=True)`.
  - `odafc.ODAFCError` → `ok=False` with the message. No bare except.
- `GET /{job_id}/download.dwg?variant=superimposed|annotated&version=R2018&pair_id=`:
  - Cached at `out/<variant>_<version>.dwg`, rebuilt when the source DXF mtime is newer.
  - Unavailable → 501 `{"detail": message, "available": false}`; converter error → 502; bad
    version/variant → 400. Media type `image/vnd.dwg`.
- `GET /api/v1/capabilities` → `{"dwg_export": bool, "dwg_versions": [...]}`. Never return paths.
- UI: a "Download DWG" button, disabled with a tooltip when `dwg_export` is false.
- Optional `job.options.export_dwg=true` → the pipeline exports after package (non-fatal; result in
  `report.json.dwg_export`).
- **Tests (mocked; ODA is not installed):** `test_unavailable_returns_501`, `test_export_mocked`
  (asserts version "R2018", `replace is True`, `audit is True`, no layer name contains `|`,
  `layer_renames` contains `MECH|0 → MECH$0`), `test_export_cached`, `test_bad_version_400`,
  `test_odafc_error_502`, `test_capabilities_flag`.

## Amendments to existing steps (summary)
- Step 1/2: `pair.json` stores converted paths; `roles.resolve_pair(..., stage=)`.
- Step 3 (optional): `clean_policy` keeps blocks that contain mismatch-family features.
- Step 4: `walk_world(doc, layout=None, *, root=None, max_depth=16)`.
- Step 8: preview keeps the `QA|MISMATCH*` colours; `preview_meta.json`; `report.mismatch`.
- Step 9: `mismatch` options and policy endpoints exposed in the UI; README section on findings,
  policy and limitations.
- Step 10: `test_e2e_mismatch_flow`.