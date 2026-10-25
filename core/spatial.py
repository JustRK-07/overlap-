#!/usr/bin/env python3
"""core/spatial.py — deterministic uniform-grid spatial hash over bounding boxes.

``GridIndex`` is the single spatial-accelerator of the mismatch stage
(PLAN_MISMATCH step 8b).  It is a plain uniform grid over axis-aligned
boxes with three query flavours:

``query_bbox(bbox)``
    every item whose box overlaps ``(xmin, ymin, xmax, ymax)``
``query_radius(xy, r)``
    every item whose box is within ``r`` of the point ``xy``
    (distance from a point to an axis-aligned box)
``knn(xy, k, rmax)``
    the up-to-``k`` nearest item **centres** within ``rmax``,
    nearest first

Design notes
------------
* **Deterministic.**  Every query returns a ``np.int64`` index array in
  ascending index order (``knn`` orders by distance, breaking ties by
  index).  There is no hashing of floats, no RNG and no set iteration, so
  the same inputs always give byte-identical output.
* **numpy only** (no scipy, no shapely).  Queries are one vectorised
  boolean filter over the candidate ids gathered from the cells the query
  box touches, so the cost is proportional to the *answer* plus the cells
  scanned, not to ``N``.
* **Large items.**  An item whose box covers more than
  ``MAX_CELLS_PER_ITEM`` (64) cells would otherwise be copied into an
  enormous number of buckets; those go to the ``large`` list, which every
  query scans linearly.  They are still *correct* results — only slower.

Items are 4-vectors ``(xmin, ymin, xmax, ymax)`` in **drawing units** of
whichever document the caller is working in; the index knows nothing
about units.
"""

from __future__ import annotations

import math

import numpy as np

__all__ = ["GridIndex"]


class GridIndex:
    """Uniform-grid hash over ``(N, 4)`` ``xmin, ymin, xmax, ymax`` boxes."""

    #: Items spanning more cells than this go to the ``large`` list.
    MAX_CELLS_PER_ITEM = 64

    __slots__ = ("boxes", "n", "centers", "cell", "origin", "large", "_cells", "_bounds")

    def __init__(self, bboxes, cell: float | None = None) -> None:
        boxes = np.asarray(bboxes, dtype=float)
        if boxes.size == 0:
            boxes = np.empty((0, 4), dtype=float)
        if boxes.ndim != 2 or boxes.shape[1] != 4:
            raise ValueError(
                f"bboxes must be an (N, 4) array of xmin,ymin,xmax,ymax, got shape {boxes.shape}")
        boxes = np.ascontiguousarray(boxes, dtype=float)
        if not np.all(np.isfinite(boxes)):
            raise ValueError("bboxes contain non-finite coordinates")

        self.boxes = boxes
        self.n = int(boxes.shape[0])
        self.centers = np.empty((self.n, 2), dtype=float)
        if self.n:
            self.centers[:, 0] = 0.5 * (boxes[:, 0] + boxes[:, 2])
            self.centers[:, 1] = 0.5 * (boxes[:, 1] + boxes[:, 3])

        c = float(cell) if cell is not None else self.auto_cell(boxes)
        if not math.isfinite(c) or c <= 0.0:
            c = 1.0
        self.cell = c

        if self.n:
            self.origin = (float(boxes[:, 0].min()), float(boxes[:, 1].min()))
        else:
            self.origin = (0.0, 0.0)

        self._cells: dict[tuple[int, int], np.ndarray] = {}
        self._bounds: tuple[int, int, int, int] | None = None
        self.large = np.empty(0, dtype=np.int64)
        self._build()

    # ── construction ───────────────────────────────────────────────
    def _build(self) -> None:
        if self.n == 0:
            return
        b = self.boxes
        x0, y0 = self.origin
        inv = 1.0 / self.cell
        i0 = np.floor((b[:, 0] - x0) * inv).astype(np.int64)
        i1 = np.floor((b[:, 2] - x0) * inv).astype(np.int64)
        j0 = np.floor((b[:, 1] - y0) * inv).astype(np.int64)
        j1 = np.floor((b[:, 3] - y0) * inv).astype(np.int64)
        n_cells = (i1 - i0 + 1) * (j1 - j0 + 1)
        big = n_cells > self.MAX_CELLS_PER_ITEM
        self.large = np.flatnonzero(big).astype(np.int64)

        buckets: dict[tuple[int, int], list[int]] = {}
        for idx in np.flatnonzero(~big):
            idx = int(idx)
            for i in range(int(i0[idx]), int(i1[idx]) + 1):
                for j in range(int(j0[idx]), int(j1[idx]) + 1):
                    buckets.setdefault((i, j), []).append(idx)
        # ascending ids inside every bucket, so a query is order-stable
        self._cells = {k: np.asarray(v, dtype=np.int64) for k, v in sorted(buckets.items())}
        if self._cells:
            ks = list(self._cells)
            self._bounds = (min(k[0] for k in ks), max(k[0] for k in ks),
                            min(k[1] for k in ks), max(k[1] for k in ks))
        else:
            self._bounds = None

    @staticmethod
    def auto_cell(bboxes) -> float:
        """A cell size that keeps ~1 item per occupied cell.

        ``span / sqrt(n)`` over the dominant extent, floored by the median
        item size so that uniformly-sized items (bolts, columns) each land
        in one or two cells instead of being scattered.  Returns ``1.0``
        for an empty or degenerate input.
        """
        b = np.asarray(bboxes, dtype=float)
        if b.size == 0:
            return 1.0
        b = b.reshape(-1, 4)
        n = b.shape[0]
        span = max(float(np.max(b[:, 2]) - np.min(b[:, 0])),
                   float(np.max(b[:, 3]) - np.min(b[:, 1])))
        if not math.isfinite(span) or span <= 0.0:
            span = 1.0
        cell = span / math.sqrt(n)
        med = float(np.median(np.maximum(b[:, 2] - b[:, 0], b[:, 3] - b[:, 1])))
        if math.isfinite(med) and med > cell:
            cell = med
        return cell if (math.isfinite(cell) and cell > 0.0) else 1.0

    # ── queries ────────────────────────────────────────────────────
    def _bucket_ids(self, qx0, qy0, qx1, qy1) -> np.ndarray:
        chunks: list[np.ndarray] = []
        if self.large.size:
            chunks.append(self.large)
        if self.n == 0 or not self._cells:
            return chunks[0] if chunks else np.empty(0, dtype=np.int64)
        x0, y0 = self.origin
        inv = 1.0 / self.cell
        bi0, bi1, bj0, bj1 = self._bounds
        i0 = max(int(math.floor((qx0 - x0) * inv)), bi0)
        i1 = min(int(math.floor((qx1 - x0) * inv)), bi1)
        j0 = max(int(math.floor((qy0 - y0) * inv)), bj0)
        j1 = min(int(math.floor((qy1 - y0) * inv)), bj1)
        if i0 > i1 or j0 > j1:
            return chunks[0] if chunks else np.empty(0, dtype=np.int64)
        span = (i1 - i0 + 1) * (j1 - j0 + 1)
        if span <= len(self._cells):
            for i in range(i0, i1 + 1):
                for j in range(j0, j1 + 1):
                    arr = self._cells.get((i, j))
                    if arr is not None:
                        chunks.append(arr)
        else:
            for key, arr in self._cells.items():
                if i0 <= key[0] <= i1 and j0 <= key[1] <= j1:
                    chunks.append(arr)
        return np.concatenate(chunks) if chunks else np.empty(0, dtype=np.int64)

    def query_bbox(self, bbox) -> np.ndarray:
        """Item ids whose box overlaps ``bbox``; ascending, deduplicated."""
        if self.n == 0:
            return np.empty(0, dtype=np.int64)
        b = np.asarray(bbox, dtype=float).ravel()
        if b.size != 4:
            raise ValueError(f"bbox must be 4 numbers (xmin,ymin,xmax,ymax), got {b.size}")
        qx0, qy0, qx1, qy1 = (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
        if qx1 < qx0:
            qx0, qx1 = qx1, qx0
        if qy1 < qy0:
            qy0, qy1 = qy1, qy0
        cand = np.unique(self._bucket_ids(qx0, qy0, qx1, qy1))
        if cand.size == 0:
            return cand
        bx = self.boxes[cand]
        keep = ((bx[:, 0] <= qx1) & (bx[:, 2] >= qx0)
                & (bx[:, 1] <= qy1) & (bx[:, 3] >= qy0))
        return cand[keep]

    def query_radius(self, xy, r) -> np.ndarray:
        """Item ids whose box lies within ``r`` of the point ``xy``; ascending."""
        if self.n == 0:
            return np.empty(0, dtype=np.int64)
        x, y = float(xy[0]), float(xy[1])
        r = float(r)
        if not math.isfinite(r):
            raise ValueError(f"radius must be finite, got {r}")
        if r < 0.0:
            return np.empty(0, dtype=np.int64)
        cand = self.query_bbox((x - r, y - r, x + r, y + r))
        if cand.size == 0:
            return cand
        bx = self.boxes[cand]
        dx = np.maximum(np.maximum(bx[:, 0] - x, x - bx[:, 2]), 0.0)
        dy = np.maximum(np.maximum(bx[:, 1] - y, y - bx[:, 3]), 0.0)
        return cand[np.hypot(dx, dy) <= r]

    def knn(self, xy, k, rmax) -> np.ndarray:
        """Up to ``k`` ids of the nearest centres within ``rmax``, nearest first.

        Ties are broken by ascending id, so the order is reproducible.
        """
        if self.n == 0 or int(k) <= 0:
            return np.empty(0, dtype=np.int64)
        cand = self.query_radius(xy, rmax)
        if cand.size == 0:
            return cand
        x, y = float(xy[0]), float(xy[1])
        d = np.hypot(self.centers[cand, 0] - x, self.centers[cand, 1] - y)
        order = np.lexsort((cand, d))
        return cand[order[: int(k)]]

    # ── misc ───────────────────────────────────────────────────────
    def __len__(self) -> int:
        return self.n

    def __repr__(self) -> str:                       # pragma: no cover - debug aid
        return (f"GridIndex(n={self.n}, cell={self.cell:.6g}, "
                f"cells={len(self._cells)}, large={self.large.size})")