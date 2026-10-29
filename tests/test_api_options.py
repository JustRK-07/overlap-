#!/usr/bin/env python3
"""test_api_options.py — PLAN.md step 9: job.options, the preview routes
and the pair-artifact whitelist.

Four knobs reach the pipeline through `job.options`:

    color_mode  ∈ {original, contrast}   default original
    fit_model   ∈ {similarity, affine}   default similarity
    str_lineweight  int (1/100 mm) or "" default unset
    qa_grid     bool                    default true

Both upload endpoints accept them, `POST /{id}/axes/confirm` can flip
`force` after the fact, and a bad value is a 400 that never creates a job.

`allow_weak_fit` is **gone**: the units question is answered explicitly
(a per-file unit override recorded in `job.json`), so there is no blanket
"accept a bad fit" escape hatch at upload time.  `force` still exists on
`POST /{id}/axes/confirm` for the other quality gates.
"""
from __future__ import annotations

import json
import sys
from io import BytesIO
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app import app as fastapi_app  # noqa: E402
from jobs import (DEFAULT_OPTIONS, job_dir, load_job,  # noqa: E402
                  normalize_options, save_job)


@pytest.fixture
def fx_client(fx_isolated_jobs):
    """TestClient with jobs.JOBS_DIR redirected to tmp_path.

    `app.app` is imported at module scope; it only reads JOBS_DIR when
    building the static mount, and every route resolves paths through
    `jobs.job_dir`, so the monkeypatch still applies.
    """
    return TestClient(fastapi_app)


def _fake_file(name: str, nbytes: int = 256) -> BytesIO:
    buf = BytesIO(b"\x00" * nbytes)
    buf.name = name
    return buf


def _upload2(client, **data):
    """POST the legacy 2-file endpoint with two dummy DXFs."""
    return client.post(
        "/api/v1/overlay",
        files=[
            ("drawing_a", ("mech.dxf", _fake_file("mech.dxf"), "application/dxf")),
            ("drawing_b", ("str.dxf", _fake_file("str.dxf"), "application/dxf")),
        ],
        data=data,
    )


def _upload_n(client, **data):
    files = [
        ("files", ("mech.dxf", _fake_file("mech.dxf"), "application/dxf")),
        ("files", ("str.dxf", _fake_file("str.dxf"), "application/dxf")),
    ]
    data.setdefault("roles", "mech,str")
    return client.post("/api/v1/overlay/upload", files=files, data=data)


# ─────────────────────────────────────────────────────────────────────────────
# Options persisted
# ─────────────────────────────────────────────────────────────────────────────

def test_options_persisted_legacy_upload(fx_client, fx_isolated_jobs, fx_no_worker):
    """Every knob lands in job.options (and is echoed in the response)."""
    r = _upload2(
        fx_client,
        color_mode="contrast",
        fit_model="affine",
        str_lineweight="50",
        qa_grid="false",
    )
    assert r.status_code == 200, r.text
    body = r.json()
    opts = body["options"]

    # The response echoes the resolved options.
    assert opts["color_mode"] == "contrast"
    assert opts["fit_model"] == "affine"
    assert opts["str_lineweight"] == 50
    assert opts["qa_grid"] is False

    # ...and they are persisted, not just returned.
    stored = load_job(body["job_id"]).options
    assert stored["color_mode"] == "contrast"
    assert stored["fit_model"] == "affine"
    assert stored["str_lineweight"] == 50
    assert stored["qa_grid"] is False

    # The raw job.json carries them too.
    raw = json.loads((job_dir(body["job_id"]) / "job.json").read_text())
    assert raw["options"]["color_mode"] == "contrast"


def test_options_defaults_legacy_upload(fx_client, fx_no_worker):
    """Omitting every option yields the documented defaults."""
    r = _upload2(fx_client)
    assert r.status_code == 200, r.text
    opts = r.json()["options"]
    assert opts["color_mode"] == "original"
    assert opts["fit_model"] == "similarity"
    assert opts["str_lineweight"] is None
    assert opts["qa_grid"] is True
    for key, val in DEFAULT_OPTIONS.items():
        assert opts[key] == val


def test_options_shape_matches_stage_readers(fx_client, fx_no_worker):
    """The `fit` / `overlay` namespaces mirror the flat keys.

    core.fit.FitOptions reads the flat keys; core.overlay.OverlayStyle
    reads either the flat keys or the `overlay` sub-dict. Both must
    describe the same run.
    """
    r = _upload2(fx_client, color_mode="contrast",
                 str_lineweight="35", qa_grid="false")
    job = load_job(r.json()["job_id"])

    from core.fit import FitOptions
    from core.overlay import OverlayStyle

    fit = FitOptions.from_job(job)
    style = OverlayStyle.from_job(job)
    assert fit.model == "similarity"
    assert fit.force is False          # `force` is set at /axes/confirm
    assert style.color_mode == "contrast"
    assert style.str_lineweight == 35
    assert style.qa_grid is False

    # And the derived namespaces say the same thing.
    assert job.options["fit"] == {"model": "similarity", "force": False}
    assert job.options["overlay"]["color_mode"] == "contrast"


def test_n_file_upload_options_persisted(fx_client, fx_isolated_jobs, fx_no_worker):
    """The N-file endpoint takes the same five options."""
    r = _upload_n(
        fx_client,
        color_mode="contrast",
        fit_model="similarity",
        str_lineweight="70",
        qa_grid="false",
        prefix="TP104-",
        axes="A B C 1 2 3",
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["options"]["color_mode"] == "contrast"
    assert body["options"]["str_lineweight"] == 70

    stored = load_job(body["job_id"]).options
    assert stored["color_mode"] == "contrast"
    assert stored["fit_model"] == "similarity"
    assert stored["str_lineweight"] == 70
    assert stored["qa_grid"] is False


# ─────────────────────────────────────────────────────────────────────────────
# Validation — a bad value is a 400 and creates no job
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["bogus", "ORIGINAL2", "orig", "contra st"])
def test_bad_color_mode_400(fx_client, fx_isolated_jobs, value, fx_no_worker):
    r = _upload2(fx_client, color_mode=value)
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "color_mode" in detail
    assert "contrast" in detail and "original" in detail
    # Nothing was created for a rejected request.
    assert not list(fx_isolated_jobs.iterdir()), (
        f"a rejected request left {list(fx_isolated_jobs.iterdir())} behind")


def test_blank_color_mode_uses_the_default(fx_client, fx_no_worker):
    """FastAPI reads an empty form field as "absent" — so it means default."""
    r = _upload2(fx_client, color_mode="")
    assert r.status_code == 200, r.text
    assert r.json()["options"]["color_mode"] == "original"


@pytest.mark.parametrize("value", ["perspective", "Similar", "similarty"])
def test_bad_fit_model_400(fx_client, value, fx_no_worker):
    r = _upload2(fx_client, fit_model=value)
    assert r.status_code == 400, r.text
    assert "fit_model" in r.json()["detail"]


@pytest.mark.parametrize("pair", [
    ("CONTRAST", "SIMILARITY"), (" Contrast ", "AFFINE"),
])
def test_enum_values_are_case_and_space_insensitive(fx_client, pair, fx_no_worker):
    """A hand-typed form value is trimmed and case-folded, not rejected."""
    color_mode, fit_model = pair
    r = _upload2(fx_client, color_mode=color_mode, fit_model=fit_model)
    assert r.status_code == 200, r.text
    assert r.json()["options"]["color_mode"] == color_mode.strip().lower()
    assert r.json()["options"]["fit_model"] == fit_model.strip().lower()


@pytest.mark.parametrize("value", ["maybe", "2.5", "tru", "true"])
def test_allow_weak_fit_is_no_longer_an_option(fx_client, value, fx_no_worker):
    """`allow_weak_fit` is gone — the form field is simply ignored."""
    r = _upload2(fx_client, allow_weak_fit=value)
    assert r.status_code == 200, r.text
    assert "allow_weak_fit" not in r.json()["options"]


@pytest.mark.parametrize("value", ["sometimes", "3"])
def test_bad_qa_grid_400(fx_client, value, fx_no_worker):
    r = _upload2(fx_client, qa_grid=value)
    assert r.status_code == 400, r.text
    assert "qa_grid" in r.json()["detail"]


@pytest.mark.parametrize("field, value", [
    ("qa_grid", "false"), ("qa_grid", "no"), ("qa_grid", "0"),
])
def test_bool_tokens_accepted(fx_client, field, value, fx_no_worker):
    """A browser checkbox sends "on"/""; a JSON client sends true/false."""
    r = _upload2(fx_client, **{field: value})
    assert r.status_code == 200, r.text
    assert r.json()["options"][field] == (value in ("1", "yes"))


@pytest.mark.parametrize("value", ["-1", "9999", "thick", "1e3"])
def test_bad_str_lineweight_400(fx_client, value, fx_no_worker):
    r = _upload2(fx_client, str_lineweight=value)
    assert r.status_code == 400, r.text
    assert "str_lineweight" in r.json()["detail"]


def test_empty_str_lineweight_is_unset(fx_client, fx_no_worker):
    """`str_lineweight=""` means "as drawn", not zero."""
    r = _upload2(fx_client, str_lineweight="")
    assert r.status_code == 200, r.text
    assert r.json()["options"]["str_lineweight"] is None


def test_bad_color_mode_400_on_n_file_upload(fx_client, fx_no_worker):
    """The N-file endpoint validates the same way."""
    r = _upload_n(fx_client, color_mode="rainbow")
    assert r.status_code == 400, r.text
    assert "color_mode" in r.json()["detail"]


# ─────────────────────────────────────────────────────────────────────────────
# confirm_axes {force}
# ─────────────────────────────────────────────────────────────────────────────

def test_confirm_with_force_sets_option(fx_client, fx_isolated_jobs, fx_no_worker):
    """POST /axes/confirm {"force": true} → FitOptions.force for this run."""
    r = _upload2(fx_client)
    job_id = r.json()["job_id"]

    job = load_job(job_id)
    job.state = "awaiting_axes"
    save_job(job)

    rc = fx_client.post(f"/api/v1/overlay/{job_id}/axes/confirm",
                        json={"force": True})
    assert rc.status_code == 200, rc.text
    assert rc.json()["status"] == "resuming"

    from core.fit import FitOptions
    opts = load_job(job_id).options
    assert opts["force"] is True
    assert opts["fit"]["force"] is True
    assert FitOptions.from_job(load_job(job_id)).force is True


def test_confirm_without_body_keeps_options(fx_client, fx_no_worker):
    """A bodiless confirm (what the UI and smoke test send) is unchanged."""
    r = _upload2(fx_client, color_mode="contrast")
    job_id = r.json()["job_id"]
    job = load_job(job_id)
    job.state = "awaiting_axes"
    save_job(job)

    rc = fx_client.post(f"/api/v1/overlay/{job_id}/axes/confirm")
    assert rc.status_code == 200, rc.text
    opts = load_job(job_id).options
    assert opts["color_mode"] == "contrast"
    assert "force" not in opts

    from core.fit import FitOptions
    assert FitOptions.from_job(load_job(job_id)).force is False


def test_confirm_bad_force_body_400(fx_client, fx_no_worker):
    r = _upload2(fx_client)
    job_id = r.json()["job_id"]
    job = load_job(job_id)
    job.state = "awaiting_axes"
    save_job(job)

    assert fx_client.post(f"/api/v1/overlay/{job_id}/axes/confirm",
                          json={"force": "maybe"}).status_code == 400
    assert fx_client.post(f"/api/v1/overlay/{job_id}/axes/confirm",
                          content=b"not json").status_code == 400
    # Neither attempt changed anything.
    assert "force" not in load_job(job_id).options


# ─────────────────────────────────────────────────────────────────────────────
# Back-compat: an old job.json with no `options` still loads
# ─────────────────────────────────────────────────────────────────────────────

def test_old_job_without_options_loads_with_defaults(fx_isolated_jobs):
    """A v1 job.json (no `options`, plus a long-removed extra field) loads."""
    d = fx_isolated_jobs / "oldjob"
    d.mkdir()
    (d / "job.json").write_text(json.dumps({
        "job_id": "oldjob", "created_at": "2026-01-01", "state": "done",
        "files": {
            "drawing_a": {"name": "old_mech.dxf", "size": 10},
            "drawing_b": {"name": "old_str.dxf", "size": 20},
        },
        "color_policy": "auto",           # unknown to JobState — stripped
    }))
    job = load_job("oldjob")
    assert job is not None
    assert job.options == {}
    assert job.legacy_v2 is True

    from core.fit import FitOptions
    from core.overlay import OverlayStyle
    assert FitOptions.from_job(job) == FitOptions()
    assert OverlayStyle.from_job(job).color_mode == "original"

    # ...and normalising fills in the defaults rather than raising.
    assert normalize_options(job.options)["color_mode"] == "original"


def test_create_job_rejects_non_dict_options(fx_isolated_jobs):
    from jobs import create_job
    with pytest.raises(ValueError):
        create_job(files=[{"name": "a.dxf", "size": 1, "kind": "mech"},
                          {"name": "b.dxf", "size": 1, "kind": "str"}],
                   options=["color_mode"])


# ─────────────────────────────────────────────────────────────────────────────
# The two new preview routes
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name, media", [
    ("preview.png", "image/png"),
    ("preview_grid.png", "image/png"),
    ("preview_meta.json", "application/json"),
])
def test_single_job_preview_routes(fx_client, fx_isolated_jobs, name, media):
    """preview_grid.png / preview_meta.json are siblings of preview.png."""
    from jobs import create_job_legacy

    job = create_job_legacy(drawing_a_name="m.dxf", drawing_a_size=1,
                            drawing_b_name="s.dxf", drawing_b_size=1)
    d = job_dir(job.job_id) / "out"
    d.mkdir(parents=True, exist_ok=True)

    r = fx_client.get(f"/api/v1/overlay/{job.job_id}/{name}")
    assert r.status_code == 404, "a missing file must 404 like its siblings"

    body = b"\x89PNG\r\n\x1a\n" if name.endswith(".png") else b'{"ok": true}'
    (d / name).write_bytes(body)

    r = fx_client.get(f"/api/v1/overlay/{job.job_id}/{name}")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith(media)
    assert r.content == body


# ─────────────────────────────────────────────────────────────────────────────
# Pair artifacts — preview_grid.png must not be an "unknown artifact"
# ─────────────────────────────────────────────────────────────────────────────

def test_preview_grid_not_rejected_as_unknown_pair_artifact(
        fx_client, fx_isolated_jobs):
    """It reaches the file lookup instead of dying at the 400 whitelist."""
    from core.pair_runner import pair_root
    from jobs import create_job_legacy

    job = create_job_legacy(drawing_a_name="m.dxf", drawing_a_size=1,
                            drawing_b_name="s.dxf", drawing_b_size=1)
    pair_id = "m_vs_s"
    out = pair_root(job.job_id, pair_id) / "out"
    out.mkdir(parents=True, exist_ok=True)
    png = b"\x89PNG\r\n\x1a\nfake"
    (out / "preview_grid.png").write_bytes(png)

    r = fx_client.get(
        f"/api/v1/overlay/{job.job_id}/pairs/{pair_id}/preview_grid.png")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/png")
    assert r.content == png
    # Downloaded under a pair-qualified name, like preview.png.
    assert "preview_grid_m_vs_s.png" in r.headers["content-disposition"]


def test_preview_meta_is_a_known_pair_artifact(fx_client, fx_isolated_jobs):
    from core.pair_runner import pair_root
    from jobs import create_job_legacy

    job = create_job_legacy(drawing_a_name="m.dxf", drawing_a_size=1,
                            drawing_b_name="s.dxf", drawing_b_size=1)
    pair_id = "m_vs_s"
    out = pair_root(job.job_id, pair_id) / "out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "preview_meta.json").write_text('{"scale": 1.0}')

    r = fx_client.get(
        f"/api/v1/overlay/{job.job_id}/pairs/{pair_id}/preview_meta.json")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    assert r.json() == {"scale": 1.0}


def test_preview_artifact_still_renamed_per_pair(fx_client, fx_isolated_jobs):
    """The pre-existing preview.png rewrite must not have regressed."""
    from core.pair_runner import pair_root
    from jobs import create_job_legacy

    job = create_job_legacy(drawing_a_name="m.dxf", drawing_a_size=1,
                            drawing_b_name="s.dxf", drawing_b_size=1)
    pair_id = "m_vs_s"
    out = pair_root(job.job_id, pair_id) / "out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "preview.png").write_bytes(b"\x89PNG\r\n\x1a\nx")

    r = fx_client.get(f"/api/v1/overlay/{job.job_id}/pairs/{pair_id}/preview.png")
    assert r.status_code == 200, r.text
    assert "preview_m_vs_s.png" in r.headers["content-disposition"]


def test_genuinely_unknown_artifact_still_400(fx_client, fx_isolated_jobs):
    """Widening `startswith("preview")` must not whitelist everything."""
    r = fx_client.get(
        "/api/v1/overlay/whatever/pairs/m_vs_s/preview_secrets.env")
    assert r.status_code == 400, r.text
    assert "Unknown artifact" in r.json()["detail"]


# ─────────────────────────────────────────────────────────────────────────────
# pair_runner options pass-through
# ─────────────────────────────────────────────────────────────────────────────

def test_run_pair_records_and_applies_options(fx_client, fx_isolated_jobs,
                                              monkeypatch):
    """run_pair(options=…) reaches the stages and lands in metadata.json."""
    from jobs import create_job_legacy
    from core import pair_runner

    job = create_job_legacy(drawing_a_name="m.dxf", drawing_a_size=1,
                            drawing_b_name="s.dxf", drawing_b_size=1,
                            options={"color_mode": "original"})
    d = job_dir(job.job_id) / "cleaned"
    d.mkdir(parents=True, exist_ok=True)
    (d / "m_clean.dxf").write_bytes(b"x")
    (d / "s_clean.dxf").write_bytes(b"x")

    # Stop at the point where the options have been applied + recorded;
    # the DXF stubs would not survive the real stages.
    calls = {}

    def _boom(*a, **k):
        raise RuntimeError("stop here")

    monkeypatch.setattr("core.fit.run", _boom)

    with pytest.raises(RuntimeError):
        pair_runner.run_pair(
            job.job_id,
            mech_filename="m_clean.dxf", str_filename="s_clean.dxf",
            pair_id="m_vs_s", prefix="TP104-", axes=["A", "1"],
            options={"color_mode": "contrast", "str_lineweight": 25},
        )
    calls["options"] = load_job(job.job_id).options

    # Applied to the job, so core.fit / core.overlay (which re-read
    # job.options) see them.
    assert calls["options"]["color_mode"] == "contrast"
    assert calls["options"]["str_lineweight"] == 25
    # Unspecified knobs keep the job's value.
    assert calls["options"]["qa_grid"] is True

    # And the pair directory records them for a later inspection.
    pj = json.loads(
        (job_dir(job.job_id) / "out" / "pairs" / "m_vs_s" / "pair.json").read_text())
    assert pj["mech"] == "m_clean.dxf" and pj["str"] == "s_clean.dxf"
    assert pj["options"]["color_mode"] == "contrast"


def test_run_pair_without_options_is_a_noop(fx_client, fx_isolated_jobs,
                                            monkeypatch):
    from jobs import create_job_legacy
    from core import pair_runner

    job = create_job_legacy(drawing_a_name="m.dxf", drawing_a_size=1,
                            drawing_b_name="s.dxf", drawing_b_size=1,
                            options={"color_mode": "contrast"})
    d = job_dir(job.job_id) / "cleaned"
    d.mkdir(parents=True, exist_ok=True)
    (d / "m_clean.dxf").write_bytes(b"x")
    (d / "s_clean.dxf").write_bytes(b"x")

    def _boom(*a, **k):
        raise RuntimeError("stop here")

    monkeypatch.setattr("core.fit.run", _boom)
    with pytest.raises(RuntimeError):
        pair_runner.run_pair(
            job.job_id,
            mech_filename="m_clean.dxf", str_filename="s_clean.dxf",
            pair_id="m_vs_s", prefix="TP104-", axes=["A", "1"],
        )
    assert load_job(job.job_id).options["color_mode"] == "contrast"