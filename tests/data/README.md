# tests/data — where the TP-104 fixtures live

The two real drawings the suite exercises against are **not committed**: the
MECH file is 15.8 MB and the STR file is 8.4 MB, and they are customer
drawings, not test assets.

## Where they are

They live in the ISGEC checkout, in its `dxf/` directory:

```
<ISGEC>/dxf/TP-104 MECH GA_clean3.dxf
<ISGEC>/dxf/TP-104 STR GA_clean3.dxf
```

On this machine that is:

```
~/Desktop/Rushabh New Laptop Files/desktop/Rushabh/edi_sem_5/ISGEC-4/dxf/
```

Note the `_clean3` suffix — these are the *pre-cleaned* outputs of the
original ISGEC pipeline, not the raw uploads.

The **scoreboard** (`quick_checks/check_project.py`) wants the *raw*
drawings instead, because it uploads them through the HTTP API:

```
<ISGEC>/dxf/TP-104 MECH GA.dxf
<ISGEC>/dxf/TP-104 STR GA.dxf
```

## How the suite finds them

`tests/conftest.py` reads the `ISGEC_DIR` environment variable and falls back
to `<repo>/../ISGEC`:

```python
ISGEC = Path(os.environ.get("ISGEC_DIR", str(ROOT.parent / "ISGEC")))
```

so the tests that need the real drawings **skip** unless you point it at a
checkout:

```bash
ISGEC_DIR=/path/to/ISGEC-4 python3 -m pytest -q
```

With `ISGEC_DIR` unset the five TP-104 tests skip and the rest of the suite
runs on the synthetic fixtures alone.

## What this directory is for

`tests/data/` is the reserved in-tree location for a local copy of the
fixtures, so that a machine without an ISGEC checkout can still run the real
drawing tests. It is gitignored (except this README), because the files are
too big to commit:

```
tests/data/*
!tests/data/README.md
```

**The conftest does not read `tests/data/` yet** — it only looks at
`ISGEC_DIR`. Dropping the two `*_clean3.dxf` files in here changes nothing
until a fallback is added. If you want that fallback, it is a two-line change
in `tests/conftest.py`; it has not been made here because it would change
which files the test suite runs against.
