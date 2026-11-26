"""Compare low resolution, aligned Kinect color with the empty wall."""

from collections import deque

import cv2
import numpy as np


class RgbAssist:
    def __init__(self, scale):
        self.scale = scale
        self.reset()

    def reset(self):
        self.samples = deque(maxlen=12)
        self.reference = self.region = None
        self.last = self.usable = self.foreground = None
        self.status = "Waiting for color"

    def capture(self, sample, *, complete, progress, mask):
        if not progress:
            self.reset()
            return
        if sample is not None and sample is not self.last:
            self.last = sample
            self.samples.append(sample[1])
        if complete:
            if len(self.samples) >= 3:
                self.reference = np.median(self.samples, axis=0).astype(np.uint8)
                center = self.scale // 2
                self.region = mask[center::self.scale, center::self.scale] > 0
            self.samples.clear()
            self.last = None
        self.status = "Capturing color baseline"

    def update(self, sample):
        if sample is None:
            self.last = self.usable = self.foreground = None
            self.status = "No recent color"
            return
        if self.reference is None:
            self.status = "Press B for a color baseline"
            return
        if sample is self.last:
            return
        self.last = sample
        self.usable = self.foreground = None
        image = sample[1]
        # Black registration holes and dark patches carry little evidence.
        valid = self.region & (self.reference.max(axis=2) >= 18) & (image.max(axis=2) >= 18)
        if np.count_nonzero(valid) < .55 * np.count_nonzero(self.region):
            self.status = "Color too dark or incomplete"
            return

        delta = cv2.subtract(image, self.reference, dtype=cv2.CV_16S)
        exposure = np.rint(np.median(delta[valid], axis=0)).astype(np.int16)
        foreground = valid & (np.abs(delta - exposure).max(axis=2) >= 24)
        if np.any(np.abs(exposure) > 20) or np.count_nonzero(foreground) > .30 * np.count_nonzero(valid):
            self.status = "Color changed across the wall"
            return
        self.usable, self.foreground = valid, foreground
        self.status = "Color ready"

    def strength(self, blob):
        """Changed pixels relative to the minimum local match, capped at twice that minimum."""
        if self.usable is None:
            return None
        x, y, width, height = blob.bbox
        scale = self.scale
        left, top = max(0, x // scale - 2), max(0, y // scale - 2)
        right, bottom = (x + width + scale - 1) // scale + 2, (y + height + scale - 1) // scale + 2
        area = np.s_[top:bottom, left:right]
        valid = self.usable[area]
        count = np.count_nonzero(valid)
        if not count or count < .65 * valid.size:
            return None
        return min(2.0, float(np.count_nonzero(self.foreground[area])) / max(3, .06 * count))

    def supports(self, blob):
        strength = self.strength(blob)
        return None if strength is None else strength >= 1
