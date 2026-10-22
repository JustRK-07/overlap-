# overlay-app

**Standalone web app for superimposing two CAD drawings** — wraps the ISGEC overlay
pipeline in a browser-based UI.

```
ISGEC/
├── A/   B/   C/        ← pipeline scripts (existing — not modified)
├── dxf/                ← source DXFs + cleaned intermediates
└── overlay-app/        ← THIS repo — a web UI on top of the pipeline
```

Upload 2 drawings → pick your grid naming convention → get a superimposed DXF back.

---

## Quick start

```bash
cd overlay-app/
pip install -r requirements.txt
./run.sh                                   # starts on http://localhost:8000
                                           # (use a different port if 8000 is taken:
                                           # PORT=8765 ./run.sh)
```

Open <http://localhost:8000>, drop in two `.dwg` or `.dxf` files, pick strategy A/B/C
and the grid convention. The pipeline runs as 9 stages with live status; when it's
done (~30–75 s), a Download button appears.

### Run the smoke test (verifies everything end-to-end)

```bash
# in a separate terminal
python3 tests/smoke_test.py                # default URL = http://127.0.0.1:8765
# or
PORT=8765 OVERLAY_URL=http://127.0.0.1:8765 ./run.sh &   # if not on default port
python3 tests/smoke_test.py
```

Expected output:
```
✓ SMOKE TEST PASSED
  elapsed: 46.8s
  output:  /tmp/superimposed_smoke.dxf (18.3 MB)
  entities: 2242 modelspace
  transform: x_m=1.0000·x_s+208379.86, y_m=1.0000·y_s-179584.52
  max_residual: 3.4e-7
```

---

## The 9 stages

| # | Stage | What happens | Time |
|---|---|---|---|
| 1 | **Validate** | Read each upload with `ezdxf.recover`; detect format, count entities/layers | <1s |
| 2 | **Inspect** | Walk entities, count types, list layers (no rendering in MVP) | <1s |
| 3 | **Convert** | DWG → DXF via ODA (skipped if input is DXF) | <1s (DXF) or 5–15s (DWG) |
| 4 | **Clean** | Strip annotations (DIMENSION/MTEXT/…), keep grid labels; strip BOX-layer hexagons, orphan blocks | 5–10s |
| 5 | **Axes** | ⏸ Pauses for user; user picks grid convention (e.g. `TP104-` prefix + A–D, 1–3 axes) | user time |
| 6 | **Fit** | Find gridline LINEs (not text positions); least-squares affine | 1–3s |
| 7 | **Overlay** | Apply the affine (per-entity A / wrap-in-blocks B/C) | 5–15s |
| 8 | **Package** | Render preview PNG, aggregate report.json | 5–20s |
| 9 | **Done** | Job complete; Download button shown in UI | — |

**Total: ~30–60 seconds** for typical TP-104-scale drawings.

---

## Three overlay strategies

The strategy picker on the upload form lets the user pick one:

| Strategy | What it does | Editability | Color policy |
|---|---|---|---|
| **A** (per-entity) | Every STR entity copied + transformed into MECH modelspace | HIGHEST (edit any entity directly) | **Original colors preserved** |
| **B** (2-block) | Each drawing wrapped in 1 block; 2 INSERTs at modelspace | MEDIUM (click block to grab whole layer) | **Forced red/blue** (MECH=red ACI 1, STR=blue ACI 5) |
| **C** (1-block) | Both merged into 1 block; 1 INSERT at origin | LOW (single grab) | **Forced red/blue** |

A is the canonical default for editing downstream. B/C are useful when you just
want a clean visual.

---

## Architecture

```
Browser
  │
  │  POST /api/v1/overlay      (multipart: drawing_a, drawing_b, strategy)
  │  GET  /api/v1/overlay/{id}/status    (polled every 500ms)
  │  GET  /api/v1/overlay/{id}/axes      (suggestions + apply)
  │  POST /api/v1/overlay/{id}/axes      (user picks convention)
  │  POST /api/v1/overlay/{id}/axes/confirm    (resumes stages 6-9)
  │  GET  /api/v1/overlay/{id}/download  (final DXF)
  │
  ▼
FastAPI (app.py)
  ├── routers/api.py          upload + status + axes + download endpoints
  ├── pipelines.py            orchestrates 9 stages via thread
  ├── jobs.py                 job state on disk as JSON
  │
  ├── core/
  │   ├── validate.py         stage 1
  │   ├── inspect.py          stage 2
  │   ├── convert.py          stage 3
  │   ├── clean.py            stage 4
  │   ├── axes.py             stage 5 (convention matcher)
  │   ├── fit.py              stage 6 (port from ISGEC/A/scripts/fit.py)
  │   ├── overlay.py          stage 7 (A/B/C strategies)
  │   └── package.py          stage 8
  │
  └── jobs/<uuid>/            per-job storage on disk
       ├── uploads/            raw user uploads
       ├── cleaned/             post-stage-4 stripped DXFs
       ├── axes/                detected + confirmed axes
       ├── transform.json       fitted affine
       └── out/
           ├── superimposed.dxf   ← the deliverable
           ├── preview.png        1000×600 render
           └── report.json        full audit trail
```

Each `core/<stage>.py` exposes `run(job_id: str)`. They're called by `pipelines.py`
in order; stages 6-9 only run after the user submits their convention.

---

## What the user sees

The single page (`static/index.html`) has 4 sections that appear in order:

1. **Upload** — drag two files, pick strategy, click Start.
2. **Pipeline progress** — step rail (1–9) with active/done/failed states;
   status message; file cards.
3. **Grid convention** (only at stage 5) — auto-suggestions + free-form
   input field for prefix + axes.
4. **Done** — fitted affine shown + Download button.

Polling interval: **500 ms**.

---

## Path resolution

App is fully self-contained. No hardcoded `/home/rushabh/...` paths anywhere.

- All scripts use `Path(__file__).resolve().parent` to locate themselves
- Job state lives in `overlay-app/jobs/<uuid>/`
- Runtime data is gitignored (`jobs/[0-9a-f]*/`)

You can move the entire `overlay-app/` directory anywhere and run `./run.sh`.

---

## Without ODA (DWG input)

ODA File Converter is only needed if you upload `.dwg` files directly. If you have
DXFs (or if AutoCAD can export DXF for you), the pipeline works without ODA.

To check if ODA is installed:
```bash
which ODAFileConverter
```

If it's missing, the validation stage will show:
> DWG support not available. Please save your DWG as DXF in AutoCAD and re-upload.

---

## Known limitations (V1)

- No auth — single-tenant use only. Don't put this on a public network.
- No DWG without ODA (or pre-converted DXF).
- Grid-label regex is fixed (`^TP\d+-([A-Z]|\d+)\s*$`) — overrides per-job are V2.
- B/C strategies don't fully implement their block-wrap layouts (per-entity A is
  the most polished). B/C work, but the wrapping logic is partial.
- Once you click Start, the worker thread runs to completion even if you close
  the browser — output sits on disk for retrieval later (just reload the page
  with the same `job_id`).
- No deduplication of uploads — same file uploaded twice produces two separate jobs.

---

## File map

```
overlay-app/
├── README.md                       ← (this file)
├── requirements.txt                5 Python deps
├── run.sh                          uvicorn launcher
├── .gitignore                      Python + jobs/ + .env
│
├── app.py                          FastAPI entrypoint (~70 lines)
├── routers/api.py                  REST endpoints (~150 lines)
├── pipelines.py                    Stage orchestrator (~120 lines)
├── jobs.py                         Job state on disk (~150 lines)
│
├── core/
│   ├── validate.py                 Stage 1 — file format sniff
│   ├── inspect.py                  Stage 2 — entity/layer counts
│   ├── convert.py                  Stage 3 — DWG→DXF (ODA optional)
│   ├── clean.py                    Stage 4 — strip annotations/markers
│   ├── axes.py                     Stage 5 — convention matcher
│   ├── fit.py                      Stage 6 — affine from gridline LINEs
│   ├── overlay.py                  Stage 7 — A/B/C strategies
│   └── package.py                  Stage 8 — preview + report
│
├── static/
│   ├── index.html                  Single page
│   ├── styles.css                  Custom CSS (no framework)
│   └── app.js                      Vanilla JS state machine + polling
│
├── jobs/                           Runtime data (gitignored)
│   └── .gitkeep
│
└── tests/
    └── smoke_test.py               end-to-end test (upload → download)
```

---

## Development

```bash
# Run server with auto-reload on file changes
./run.sh                                # uses --reload under the hood
# or
python3 -m uvicorn app:app --host 0.0.0.0 --port 8765 --reload

# Run smoke test
python3 tests/smoke_test.py

# View job outputs
ls -lh jobs/<job_id>/out/
```

---

## License

Same as the underlying ISGEC pipeline scripts (inherit from `ISGEC/{A,B,C}/scripts/`).
