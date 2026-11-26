"""Kinect depth intrinsics and robust planar projection in millimetres."""

from dataclasses import dataclass
from math import isfinite

import numpy as np


WIDTH, HEIGHT = 640, 480
NOMINAL_FOCAL = 580.0
MIN_DEPTH, MAX_WALL_DEPTH = 500, 4000


@dataclass(frozen=True)
class Intrinsics:
    cx: float = WIDTH / 2
    cy: float = HEIGHT / 2
    fx: float = NOMINAL_FOCAL
    fy: float = NOMINAL_FOCAL
    calibrated: bool = False

    @classmethod
    def from_values(cls, cx, cy, fx, fy, *, calibrated=True):
        if (all(isfinite(x) for x in (cx, cy, fx, fy)) and 0 <= cx < WIDTH and 0 <= cy < HEIGHT
                and 200 <= fx <= 1500 and 200 <= fy <= 1500):
            return cls(cx, cy, fx, fy, calibrated)
        return cls()

    def ray(self, u, v):
        return np.array(((u - self.cx) / self.fx, (v - self.cy) / self.fy, 1.0))

    def pixel(self, point):
        x, y, z = point
        return self.cx + self.fx * x / z, self.cy + self.fy * y / z


@dataclass(frozen=True)
class WallPlane:
    normal: np.ndarray
    offset: float
    support: float
    error_mm: float

    @classmethod
    def fit(cls, depth, mask, camera):
        # Sample across the projected area. Prefer a dominant plane over
        # transient foreground or gaps at the edges of the Kinect image.
        grid = np.s_[::8, ::8]
        sample = depth[grid]
        valid = (mask[grid] > 0) & (sample >= MIN_DEPTH) & (sample <= MAX_WALL_DEPTH)
        row, column = np.nonzero(valid)
        if len(row) < 80:
            return None
        z = sample[valid].astype(np.float64)
        points = np.stack(((column * 8 - camera.cx) * z / camera.fx,
                           (row * 8 - camera.cy) * z / camera.fy, z), axis=1)
        rng = np.random.default_rng(17)
        triples = points[rng.integers(len(points), size=(64, 3))]
        origins = triples[:, 0]
        normals = np.cross(triples[:, 1] - origins, triples[:, 2] - origins)
        length = np.linalg.norm(normals, axis=1)
        good = length > 10
        if not good.any():
            return None
        normals = normals[good] / length[good, np.newaxis]
        offsets = -np.einsum("ij,ij->i", normals, origins[good])
        close = np.abs(points @ normals.T + offsets) < 23
        inliers = close[:, np.argmax(close.sum(axis=0))]
        if inliers.mean() < .65:
            return None
        supported = points[inliers]
        center = supported.mean(axis=0)
        _, singular, vectors = np.linalg.svd(supported - center, full_matrices=False)
        normal = vectors[-1]
        if normal[2] > 0:
            normal = -normal
        if -normal[2] < .26 or singular[1] / np.sqrt(len(supported)) < 16:
            return None
        offset = -float(normal @ center)
        if offset <= 0:
            return None
        residuals = np.abs(points @ normal + offset)
        robust = residuals < 23
        support = float(robust.mean())
        if support < .65:
            return None
        return cls(normal, offset, support, float(np.median(residuals[robust])))

    def ray_factors(self, camera):
        u = (np.arange(WIDTH, dtype=np.float32) - camera.cx) / camera.fx
        v = (np.arange(HEIGHT, dtype=np.float32) - camera.cy) / camera.fy
        return -(self.normal[0] * u[None, :] + self.normal[1] * v[:, None] + self.normal[2])

    def height(self, point):
        return float(self.normal @ point + self.offset)

    def flatten(self, point):
        return point - self.height(point) * self.normal
