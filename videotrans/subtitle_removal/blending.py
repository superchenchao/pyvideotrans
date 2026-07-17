from __future__ import annotations

from collections.abc import Sequence

import numpy as np


def _gaussian_blur(mask: np.ndarray, sigma: float) -> np.ndarray:
    try:
        import cv2
    except ModuleNotFoundError:
        radius = max(1, int(round(sigma * 3)))
        axis = np.arange(-radius, radius + 1, dtype=np.float32)
        kernel = np.exp(-(axis ** 2) / (2 * sigma ** 2))
        kernel /= kernel.sum()
        horizontal = np.apply_along_axis(
            lambda row: np.convolve(row, kernel, mode="same"), 1, mask,
        )
        return np.apply_along_axis(
            lambda column: np.convolve(column, kernel, mode="same"),
            0,
            horizontal,
        )
    return cv2.GaussianBlur(mask, (0, 0), sigmaX=sigma, sigmaY=sigma)


def soft_alpha_mask(mask: np.ndarray, feather_pixels: int = 10) -> np.ndarray:
    """Return a solid core mask with a soft transition outside its boundary."""
    binary = (np.asarray(mask).squeeze() > 0).astype(np.float32)
    if feather_pixels <= 0 or not np.any(binary):
        return binary[:, :, None]
    blurred = _gaussian_blur(binary, max(1.0, feather_pixels / 2))
    alpha = np.maximum(binary, blurred)
    alpha[alpha < 0.01] = 0.0
    return np.clip(alpha, 0.0, 1.0)[:, :, None]


def composite_repaired_frames(
        original_frames: Sequence[np.ndarray],
        repaired_frames: Sequence[np.ndarray],
        mask: np.ndarray, *, feather_pixels: int = 10) -> list[np.ndarray]:
    if len(original_frames) != len(repaired_frames):
        raise ValueError("original and repaired frame counts differ")
    alpha = soft_alpha_mask(mask, feather_pixels=feather_pixels)
    output: list[np.ndarray] = []
    for original, repaired in zip(original_frames, repaired_frames):
        if original.shape != repaired.shape or original.shape[:2] != alpha.shape[:2]:
            raise ValueError("frame and mask sizes differ")
        blended = (
            repaired.astype(np.float32) * alpha
            + original.astype(np.float32) * (1.0 - alpha)
        )
        output.append(np.clip(blended, 0, 255).astype(np.uint8))
    return output


def repair_is_suspiciously_black(
        original_frames: Sequence[np.ndarray],
        repaired_frames: Sequence[np.ndarray],
        mask: np.ndarray) -> bool:
    """Catch the known ProPainter NaN-to-black failure without rejecting dark scenes."""
    selected = np.asarray(mask).squeeze() > 0
    if not np.any(selected) or not original_frames or not repaired_frames:
        return False
    sample_count = min(3, len(original_frames), len(repaired_frames))
    original_pixels = np.concatenate(
        [frame[selected].reshape(-1, 3) for frame in original_frames[:sample_count]], axis=0)
    repaired_pixels = np.concatenate(
        [frame[selected].reshape(-1, 3) for frame in repaired_frames[:sample_count]], axis=0)
    repaired_near_zero = float((repaired_pixels.max(axis=1) < 3).mean())
    original_near_zero = float((original_pixels.max(axis=1) < 3).mean())
    return repaired_near_zero > 0.98 and original_near_zero < 0.70
