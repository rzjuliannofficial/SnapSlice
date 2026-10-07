from __future__ import annotations

import argparse
import copy
import ctypes
import errno
import hashlib
import json
import math
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
import warnings
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageTk
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_DIR = Path(__file__).resolve().parent
SESSION_DIR = APP_DIR / "sesi"
SETTINGS_PATH = APP_DIR / "pengaturan.json"
LOG_PATH = APP_DIR / "log_error.txt"
OLD_LOG_PATH = APP_DIR / "log_error.old.txt"
SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
MAX_LOG_BYTES = 1_000_000
MAX_IMAGE_PIXELS_PROMPT = 50_000_000
MAX_BOXES_CONFIRM = 500
UNDO_LIMIT = 30

_log_lock = threading.Lock()


class UserFacingError(Exception):
    """Kesalahan yang aman ditampilkan langsung kepada user."""


class PathTooLongError(UserFacingError):
    pass


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def rotate_log_if_needed() -> None:
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size >= MAX_LOG_BYTES:
            try:
                if OLD_LOG_PATH.exists():
                    OLD_LOG_PATH.unlink()
            except OSError:
                pass
            LOG_PATH.replace(OLD_LOG_PATH)
    except OSError:
        pass


def log_error(context: str, exc: BaseException) -> None:
    rotate_log_if_needed()
    record = (
        f"[{now_text()}] KONTEXT: {context}\n"
        f"ERROR: {type(exc).__name__}: {exc}\n"
        f"TRACEBACK:\n{''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))}\n"
        f"{'-' * 80}\n"
    )
    with _log_lock:
        try:
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(record)
        except OSError:
            pass


def load_settings() -> Dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        with SETTINGS_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        log_error("Membaca pengaturan.json", exc)
        return {}


def atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def save_settings(last_folder: Optional[Path]) -> None:
    data = {"last_folder": str(last_folder) if last_folder else str(Path.home())}
    try:
        atomic_write_json(SETTINGS_PATH, data)
    except OSError as exc:
        log_error("Menyimpan pengaturan.json", exc)


def clean_user_path(raw: str) -> str:
    value = str(raw).strip()
    if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
        value = value[1:-1].strip()
    return value


def normalize_image_path(raw: str, base_dir: Path = APP_DIR) -> Path:
    value = clean_user_path(raw)
    if not value:
        raise UserFacingError("Path gambar kosong. Pilih gambar atau tempel path terlebih dahulu.")
    try:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = (base_dir / path)
        path = path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise UserFacingError(f"Path gambar tidak valid: {exc}") from exc
    if len(str(path)) > 260:
        raise PathTooLongError("Path gambar lebih dari 260 karakter. Gunakan lokasi/folder yang lebih pendek.")
    if not path.exists():
        raise UserFacingError(f"File gambar tidak ditemukan:\n{path}")
    if not path.is_file():
        raise UserFacingError(f"Path bukan file:\n{path}")
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise UserFacingError(
            "Ekstensi gambar tidak didukung. Gunakan PNG, JPG/JPEG, WEBP, BMP, TIF, atau TIFF."
        )
    return path


def validate_output_dir(path: Path) -> Path:
    path = Path(path).expanduser().resolve(strict=False)
    if len(str(path)) > 260:
        raise PathTooLongError("Path folder output lebih dari 260 karakter.")
    return path


def default_output_dir(image_path: Path) -> Path:
    base = image_path.parent / f"{image_path.stem}_pecahan"
    if not base.exists():
        return base
    index = 2
    while True:
        candidate = image_path.parent / f"{image_path.stem}_pecahan_{index}"
        if not candidate.exists():
            return candidate
        index += 1


def file_session_hash(image_path: Path) -> str:
    st = image_path.stat()
    raw = f"{image_path.resolve()}|{st.st_size}|{st.st_mtime_ns}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def session_paths(image_path: Path) -> Tuple[Path, Path]:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    key = file_session_hash(image_path)
    return SESSION_DIR / f"{key}.json", SESSION_DIR / key


def rotate_session_backups(session_file: Path) -> None:
    for i in range(5, 1, -1):
        src = session_file.with_name(session_file.stem + f".bak{i-1}")
        dst = session_file.with_name(session_file.stem + f".bak{i}")
        if src.exists():
            try:
                if dst.exists():
                    dst.unlink()
                src.replace(dst)
            except OSError:
                pass
    if session_file.exists():
        bak1 = session_file.with_name(session_file.stem + ".bak1")
        try:
            if bak1.exists():
                bak1.unlink()
            shutil.copy2(session_file, bak1)
        except OSError:
            pass


def atomic_session_save(session_file: Path, data: Dict[str, Any]) -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    rotate_session_backups(session_file)
    atomic_write_json(session_file, data)


def load_session_file(image_path: Path) -> Optional[Dict[str, Any]]:
    session_file, _ = session_paths(image_path)
    if not session_file.exists():
        return None
    try:
        with session_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("format JSON sesi bukan object")
        st = image_path.stat()
        if str(data.get("image_path", "")) != str(image_path.resolve()):
            raise UserFacingError("Sesi sebelumnya tidak cocok dengan path gambar saat ini.")
        if list(data.get("image_size", [])) != [int(st.st_size), int(st.st_mtime_ns)]:
            raise UserFacingError("Sesi sebelumnya tidak cocok dengan versi file gambar saat ini.")
        if not isinstance(data.get("boxes", []), list):
            raise UserFacingError("Daftar kotak pada sesi tidak valid.")
        return data
    except (OSError, json.JSONDecodeError, ValueError, UserFacingError) as exc:
        log_error("Memuat sesi sebelumnya", exc)
        raise UserFacingError(
            "Sesi sebelumnya rusak atau tidak cocok dengan gambar. Sesi akan diabaikan."
        ) from exc


def open_image_safe(path: Path, allow_large: bool) -> Image.Image:
    try:
        with Image.open(path) as probe:
            size = probe.size
            mode = probe.mode
            fmt = probe.format
            if size[0] * size[1] > MAX_IMAGE_PIXELS_PROMPT and not allow_large:
                raise UserFacingError("__LARGE_IMAGE_PROMPT__")
            img = probe.copy()
    except UserFacingError:
        raise
    except Image.DecompressionBombError as exc:
        raise UserFacingError(
            "Gambar terlalu besar dan Pillow menolaknya sebagai risiko decompression bomb. "
            "Gunakan gambar yang lebih kecil."
        ) from exc
    except MemoryError as exc:
        raise UserFacingError("Memori tidak cukup untuk memuat gambar ini.") from exc
    except (OSError, Image.UnidentifiedImageError) as exc:
        raise UserFacingError(
            f"Gambar tidak dapat dibuka ({type(exc).__name__}). Pastikan file tidak rusak dan format didukung."
        ) from exc
    img.info["_source_format"] = fmt or ""
    img.info["_source_mode"] = mode
    return img


def image_has_alpha_mode(image: Image.Image) -> bool:
    return "A" in image.getbands()


def alpha_mode_should_be_used(image: Image.Image) -> bool:
    if not image_has_alpha_mode(image):
        return False
    alpha = np.asarray(image.getchannel("A"), dtype=np.uint8)
    return float(np.mean(alpha < 250)) >= 0.05


def choose_background_color(rgb_array: np.ndarray) -> Tuple[Tuple[int, int, int], float]:
    h, w, _ = rgb_array.shape
    if h == 0 or w == 0:
        return (255, 255, 255), 0.0
    edge_parts = [rgb_array[0, :, :], rgb_array[-1, :, :]]
    if h > 2:
        edge_parts.extend([rgb_array[1:-1, 0, :], rgb_array[1:-1, -1, :]])
    edge = np.concatenate([p.reshape(-1, 3) for p in edge_parts], axis=0)
    q = (edge // 8).astype(np.int32)
    codes = q[:, 0] * 1024 + q[:, 1] * 32 + q[:, 2]
    counts = np.bincount(codes, minlength=32768)
    code = int(np.argmax(counts))
    qr = code // 1024
    qg = (code % 1024) // 32
    qb = code % 32
    bg = (min(255, qr * 8 + 4), min(255, qg * 8 + 4), min(255, qb * 8 + 4))
    corners = np.array([rgb_array[0, 0], rgb_array[0, -1], rgb_array[-1, 0], rgb_array[-1, -1]], dtype=np.int16)
    spread = 0.0
    for i in range(4):
        for j in range(i + 1, 4):
            spread = max(spread, float(np.max(np.abs(corners[i] - corners[j]))))
    return bg, spread


def build_foreground_mask(
    image: Image.Image,
    tolerance: int = 30,
    alpha_threshold: int = 128,
    bg_override: Optional[Tuple[int, int, int]] = None,
) -> Tuple[np.ndarray, str, Optional[Tuple[int, int, int]], float]:
    rgba = image.convert("RGBA")
    arr = np.asarray(rgba, dtype=np.uint8)
    alpha = arr[..., 3]
    if float(np.mean(alpha < 250)) >= 0.05:
        return alpha > int(alpha_threshold), "ALPHA", None, 0.0
    rgb = arr[..., :3]
    bg, spread = choose_background_color(rgb)
    if bg_override is not None:
        bg = tuple(int(max(0, min(255, x))) for x in bg_override)
    diff = np.max(np.abs(rgb.astype(np.int16) - np.array(bg, dtype=np.int16)), axis=2)
    mask = diff > int(tolerance)
    return mask, "WARNA", bg, spread


def dilate_mask(mask: np.ndarray, distance: int) -> np.ndarray:
    distance = max(0, int(distance))
    if distance == 0:
        return mask.copy()
    size = 2 * distance + 1
    pil_mask = Image.fromarray((mask.astype(np.uint8) * 255), mode="L")
    dilated = pil_mask.filter(ImageFilter.MaxFilter(size=size))
    return np.asarray(dilated, dtype=np.uint8) > 0


def _mask_runs(row: np.ndarray) -> List[Tuple[int, int]]:
    xs = np.flatnonzero(row)
    if xs.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(xs) > 1)
    starts = np.concatenate(([xs[0]], xs[breaks + 1]))
    ends = np.concatenate((xs[breaks], [xs[-1]]))
    return [(int(s), int(e)) for s, e in zip(starts, ends)]


def label_components_8(mask: np.ndarray) -> Tuple[np.ndarray, List[Tuple[int, int, int, int, int]]]:
    """RLE + union-find 8-ketetanggaan; bbox dihitung kemudian dari mask asli."""
    if mask.ndim != 2:
        raise ValueError("Mask harus 2D")
    h, w = mask.shape
    label_map = np.zeros((h, w), dtype=np.int32)
    parent: List[int] = [0]

    def new_label() -> int:
        parent.append(len(parent))
        return len(parent) - 1

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            if ra > rb:
                ra, rb = rb, ra
            parent[rb] = ra

    prev_runs: List[Tuple[int, int, int]] = []
    for y in range(h):
        cur_runs: List[Tuple[int, int, int]] = []
        runs = _mask_runs(mask[y])
        pidx = 0
        for start, end in runs:
            while pidx < len(prev_runs) and prev_runs[pidx][1] < start - 1:
                pidx += 1
            overlaps: List[int] = []
            j = pidx
            while j < len(prev_runs) and prev_runs[j][0] <= end + 1:
                ps, pe, pl = prev_runs[j]
                if ps <= end + 1 and pe >= start - 1:
                    overlaps.append(pl)
                j += 1
            label = overlaps[0] if overlaps else new_label()
            for other in overlaps[1:]:
                union(label, other)
            cur_runs.append((start, end, label))
            label_map[y, start : end + 1] = label
        prev_runs = cur_runs

    nlabels = len(parent) - 1
    if nlabels == 0:
        return label_map, []

    for i in range(1, len(parent)):
        parent[i] = find(i)
    comp_for_label = np.zeros(nlabels + 1, dtype=np.int32)
    root_to_comp: Dict[int, int] = {}
    next_comp = 1
    for i in range(1, nlabels + 1):
        root = parent[i]
        if root not in root_to_comp:
            root_to_comp[root] = next_comp
            next_comp += 1
        comp_for_label[i] = root_to_comp[root]

    # Ubah label map menjadi component id final, per baris untuk menghemat peak memory.
    for y in range(h):
        row = label_map[y]
        nz = row != 0
        if np.any(nz):
            row[nz] = comp_for_label[row[nz]]

    comp_count = next_comp - 1
    min_x = np.full(comp_count + 1, w, dtype=np.int32)
    min_y = np.full(comp_count + 1, h, dtype=np.int32)
    max_x = np.full(comp_count + 1, -1, dtype=np.int32)
    max_y = np.full(comp_count + 1, -1, dtype=np.int32)

    # Bbox dari mask ASLI, bukan hasil dilasi.
    for y in range(h):
        xs = np.flatnonzero(mask[y])
        if xs.size == 0:
            continue
        comps = label_map[y, xs]
        unique = np.unique(comps)
        for comp in unique:
            if comp <= 0:
                continue
            selected = xs[comps == comp]
            xlo = int(selected[0])
            xhi = int(selected[-1])
            c = int(comp)
            min_x[c] = min(min_x[c], xlo)
            max_x[c] = max(max_x[c], xhi)
            min_y[c] = min(min_y[c], y)
            max_y[c] = max(max_y[c], y)

    components: List[Tuple[int, int, int, int, int]] = []
    for c in range(1, comp_count + 1):
        if max_x[c] >= 0:
            # x2/y2 dibuat eksklusif untuk PIL crop().
            components.append((int(min_x[c]), int(min_y[c]), int(max_x[c] + 1), int(max_y[c] + 1), c))
    return label_map, components


def add_padding(
    box: Tuple[int, int, int, int], padding: int, width: int, height: int
) -> Tuple[int, int, int, int]:
    x1, y1, x2, y2 = box
    p = max(0, int(padding))
    return (
        max(0, x1 - p),
        max(0, y1 - p),
        min(width, x2 + p),
        min(height, y2 + p),
    )


def box_area(box: Tuple[int, int, int, int]) -> int:
    x1, y1, x2, y2 = box
    return max(0, x2 - x1) * max(0, y2 - y1)


def containment_ratio(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> float:
    inter_w = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    inter_h = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = inter_w * inter_h
    smaller = min(box_area(a), box_area(b))
    return (inter / smaller) if smaller else 0.0


def merge_contained_boxes(boxes: List[Dict[str, Any]], threshold: float = 0.90) -> List[Dict[str, Any]]:
    work = copy.deepcopy(boxes)
    changed = True
    while changed:
        changed = False
        for i in range(len(work)):
            merged_here = False
            for j in range(i + 1, len(work)):
                if containment_ratio(work[i]["box"], work[j]["box"]) >= threshold:
                    a = work[i]["box"]
                    b = work[j]["box"]
                    merged = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
                    ids = sorted(set(work[i].get("component_ids", [])) | set(work[j].get("component_ids", [])))
                    work[i]["box"] = merged
                    work[i]["component_ids"] = ids
                    work[i]["cleanable"] = work[i].get("cleanable", False) and work[j].get("cleanable", False)
                    work.pop(j)
                    changed = True
                    merged_here = True
                    break
            if merged_here:
                break
    return work


def sort_boxes_reading(boxes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Urutan baca: kelompok berdasarkan pusat-y dan median tinggi kotak."""
    if not boxes:
        return []
    median_h = float(np.median([max(1, b["box"][3] - b["box"][1]) for b in boxes]))
    threshold = 0.5 * median_h
    ordered = sorted(boxes, key=lambda b: (((b["box"][1] + b["box"][3]) / 2.0), b["box"][0]))
    rows: List[Dict[str, Any]] = []
    for box in ordered:
        cy = (box["box"][1] + box["box"][3]) / 2.0
        candidates = []
        for idx, row in enumerate(rows):
            if abs(cy - row["mean_y"]) < threshold:
                candidates.append((abs(cy - row["mean_y"]), idx))
        if candidates:
            _, idx = min(candidates)
            row = rows[idx]
            row["items"].append(box)
            row["mean_y"] = float(np.mean([(x["box"][1] + x["box"][3]) / 2.0 for x in row["items"]]))
        else:
            rows.append({"mean_y": cy, "items": [box]})
    rows.sort(key=lambda r: r["mean_y"])
    result: List[Dict[str, Any]] = []
    for row in rows:
        row["items"].sort(key=lambda b: b["box"][0])
        result.extend(row["items"])
    return result


def detect_automatic(
    image: Image.Image,
    tolerance: int = 30,
    merge_distance: int = 2,
    min_size: int = 16,
    padding: int = 2,
    merge_contained: bool = True,
    alpha_threshold: int = 128,
    bg_override: Optional[Tuple[int, int, int]] = None,
) -> Dict[str, Any]:
    if not (0 <= int(tolerance) <= 100):
        raise UserFacingError("Toleransi warna harus 0-100.")
    if not (0 <= int(merge_distance) <= 20):
        raise UserFacingError("Jarak gabung harus 0-20 piksel.")
    if int(min_size) < 1:
        raise UserFacingError("Ukuran minimum harus >= 1 piksel.")
    if int(padding) < 0:
        raise UserFacingError("Padding tidak boleh negatif.")
    if not (1 <= int(alpha_threshold) <= 254):
        raise UserFacingError("Ambang alpha harus 1-254.")

    mask_original, source_mode, bg, corner_spread = build_foreground_mask(
        image, tolerance=tolerance, alpha_threshold=alpha_threshold, bg_override=bg_override
    )
    mask_dilated = dilate_mask(mask_original, merge_distance)
    label_map, components = label_components_8(mask_dilated)
    width, height = image.size
    raw_boxes: List[Dict[str, Any]] = []
    discarded = 0
    for x1, y1, x2, y2, comp_id in components:
        box = add_padding((x1, y1, x2, y2), padding, width, height)
        bw = box[2] - box[0]
        bh = box[3] - box[1]
        if bw < min_size or bh < min_size or bw < 1 or bh < 1:
            discarded += 1
            continue
        raw_boxes.append(
            {
                "box": box,
                "uid": None,
                "source": "auto",
                "cleanable": source_mode == "ALPHA",
                "component_ids": [int(comp_id)],
            }
        )
    if merge_contained and raw_boxes:
        raw_boxes = merge_contained_boxes(raw_boxes, threshold=0.90)
    result_boxes = sort_boxes_reading(raw_boxes)
    return {
        "boxes": result_boxes,
        "discarded": discarded,
        "mask": mask_original,
        "label_map": label_map,
        "source_mode": source_mode,
        "background": bg,
        "corner_spread": corner_spread,
        "component_count": len(components),
    }


def generate_grid(
    image_size: Tuple[int, int], rows: int, cols: int, margin: int = 0, gap: int = 0
) -> List[Tuple[int, int, int, int]]:
    width, height = map(int, image_size)
    rows, cols, margin, gap = map(int, (rows, cols, margin, gap))
    if rows < 1 or cols < 1:
        raise UserFacingError("Baris dan kolom harus >= 1.")
    if margin < 0 or gap < 0:
        raise UserFacingError("Margin dan gap harus >= 0.")
    area_w = width - 2 * margin
    area_h = height - 2 * margin
    cell_w = (area_w - (cols - 1) * gap) / cols
    cell_h = (area_h - (rows - 1) * gap) / rows
    if cell_w < 1 or cell_h < 1:
        raise UserFacingError("Ukuran sel grid kurang dari 1 piksel. Kurangi margin/gap atau jumlah baris/kolom.")
    boxes: List[Tuple[int, int, int, int]] = []
    for r in range(rows):
        y1 = round(margin + r * (cell_h + gap))
        y2 = round(margin + (r + 1) * cell_h + r * gap)
        for c in range(cols):
            x1 = round(margin + c * (cell_w + gap))
            x2 = round(margin + (c + 1) * cell_w + c * gap)
            x1 = max(0, min(width, x1))
            x2 = max(0, min(width, x2))
            y1 = max(0, min(height, y1))
            y2 = max(0, min(height, y2))
            if x2 - x1 < 1 or y2 - y1 < 1:
                raise UserFacingError("Ada sel grid dengan ukuran kurang dari 1 piksel.")
            boxes.append((x1, y1, x2, y2))
    return boxes


def clamp_box(box: Tuple[int, int, int, int], width: int, height: int) -> Tuple[int, int, int, int]:
    x1, y1, x2, y2 = box
    x1, x2 = sorted((int(round(x1)), int(round(x2))))
    y1, y2 = sorted((int(round(y1)), int(round(y2))))
    x1, x2 = max(0, min(width, x1)), max(0, min(width, x2))
    y1, y2 = max(0, min(height, y1)), max(0, min(height, y2))
    return x1, y1, x2, y2


def image_crop_for_export(image: Image.Image, box: Tuple[int, int, int, int]) -> Image.Image:
    return image.crop(box)


def valid_prefix(prefix: str) -> str:
    value = prefix.strip()
    if not value:
        raise UserFacingError("Awalan nama file tidak boleh kosong.")
    if any(ch in value for ch in '<>:"/\\|?*'):
        raise UserFacingError('Awalan nama file mengandung karakter ilegal Windows: < > : " / \\ | ? *')
    if value.endswith(".") or value.endswith(" ") or value in {".", ".."}:
        raise UserFacingError("Awalan nama file tidak boleh berakhir dengan titik/spasi atau berupa . / ...")
    return value


def unique_export_path(directory: Path, prefix: str, number: int) -> Path:
    base = directory / f"{prefix}_{number:03d}.png"
    if not base.exists():
        return base
    idx = 2
    while True:
        candidate = directory / f"{prefix}_{number:03d}_{idx}.png"
        if not candidate.exists():
            return candidate
        idx += 1


def sample_dominant_background_colors(
    arr_rgb: np.ndarray,
    bg_override: Optional[Tuple[int, int, int]] = None,
) -> List[np.ndarray]:
    """Mengambil kluster warna background dominan dari perimeter (tepi) gambar."""
    h, w, _ = arr_rgb.shape
    if h == 0 or w == 0:
        return [np.array([255.0, 255.0, 255.0], dtype=np.float32)]

    depth = max(1, min(5, min(h, w) // 10))
    rim_parts = [
        arr_rgb[:depth, :, :].reshape(-1, 3),
        arr_rgb[-depth:, :, :].reshape(-1, 3),
        arr_rgb[:, :depth, :].reshape(-1, 3),
        arr_rgb[:, -depth:, :].reshape(-1, 3),
    ]
    rim = np.concatenate(rim_parts, axis=0).astype(np.float32)

    bg_clusters: List[np.ndarray] = []
    if bg_override is not None:
        bg_clusters.append(np.array(bg_override, dtype=np.float32))

    # Kuantisasi warna rim ke bin 16
    q = (np.clip(rim, 0, 255) // 16).astype(np.int32)
    codes = q[:, 0] * 256 + q[:, 1] * 16 + q[:, 2]
    unique_codes, counts = np.unique(codes, return_counts=True)
    sorted_idx = np.argsort(-counts)

    total_rim = len(rim)
    for idx in sorted_idx:
        pct = counts[idx] / total_rim
        if pct >= 0.04:  # Kluster mencakup minimal 4% perimeter
            code = unique_codes[idx]
            mask_code = codes == code
            mean_color = rim[mask_code].mean(axis=0)
            if not any(np.max(np.abs(mean_color - c)) < 15 for c in bg_clusters):
                bg_clusters.append(mean_color)

    if not bg_clusters:
        mean_rim = rim.mean(axis=0)
        bg_clusters.append(mean_rim)

    return bg_clusters


def remove_background_from_crop(
    crop: Image.Image,
    box: Tuple[int, int, int, int],
    rec: Optional[Dict[str, Any]] = None,
    label_map: Optional[np.ndarray] = None,
    bg_color: Optional[Tuple[int, int, int]] = None,
    tolerance: int = 30,
    clean_neighbors: bool = True,
    clean_holes: bool = True,
    defringe: bool = True,
) -> Image.Image:
    """
    Menghapus background dari crop objek dan menghasilkan gambar RGBA dengan background transparan bersih.
    - Multi-modal background detection: mendeteksi background solid, checkerboard (pola catur),
      maupun warna kustom apa pun yang ada pada tepi gambar.
    - clean_holes: membersihkan latar belakang di dalam rongga/lubang huruf (seperti B, O, dsb).
    - defringe: erosi 1px + penghalusan anti-aliasing untuk membuang halo/lis warna sisa di pinggir objek.
    - clean_neighbors: membersihkan potongan objek tetangga yang masuk ke dalam crop.
    """
    crop_rgba = crop.convert("RGBA")
    w, h = crop_rgba.size
    if w <= 0 or h <= 0:
        return crop_rgba

    arr = np.asarray(crop_rgba).copy()
    rgb = arr[..., :3].astype(np.float32)
    orig_alpha = arr[..., 3]

    # 1. Deteksi kluster warna background dari perimeter
    bg_clusters = sample_dominant_background_colors(arr[..., :3], bg_override=bg_color)

    # 2. Hitung jarak minimum piksel ke kluster background
    min_diff = np.full((h, w), 999.0, dtype=np.float32)
    for bg_c in bg_clusters:
        d = np.max(np.abs(rgb - bg_c), axis=2)
        min_diff = np.minimum(min_diff, d)

    eff_tolerance = max(20, int(tolerance))
    is_bg_candidate = min_diff <= eff_tolerance

    # Jika gambar aslinya sudah punya alpha transparan
    if float(np.mean(orig_alpha < 250)) >= 0.05:
        is_bg_candidate |= orig_alpha < 128

    # 3. Pisahkan komponen tetangga jika ada label_map
    other_comps_mask: Optional[np.ndarray] = None
    if rec and label_map is not None:
        sub_labels = label_map[box[1] : box[3], box[0] : box[2]]
        cids = [int(v) for v in rec.get("component_ids", [])]
        if cids:
            this_ids = set(cids)
            if clean_neighbors and sub_labels.shape == (h, w):
                other_comps_mask = (sub_labels > 0) & (~np.isin(sub_labels, list(this_ids)))
                is_definite_foreground = np.isin(sub_labels, list(this_ids))
                is_bg_candidate &= ~is_definite_foreground

    # 4. Deteksi background eksterior via flood-fill dari tepi
    mask_l = Image.fromarray((is_bg_candidate.astype(np.uint8) * 255), mode="L").copy()
    for x in range(w):
        if mask_l.getpixel((x, 0)) == 255:
            ImageDraw.floodfill(mask_l, (x, 0), 128)
        if mask_l.getpixel((x, h - 1)) == 255:
            ImageDraw.floodfill(mask_l, (x, h - 1), 128)
    for y in range(h):
        if mask_l.getpixel((0, y)) == 255:
            ImageDraw.floodfill(mask_l, (0, y), 128)
        if mask_l.getpixel((w - 1, y)) == 255:
            ImageDraw.floodfill(mask_l, (w - 1, y), 128)

    exterior_bg = np.asarray(mask_l) == 128

    if clean_holes:
        final_bg = is_bg_candidate
    else:
        final_bg = exterior_bg

    if other_comps_mask is not None:
        final_bg |= other_comps_mask

    # Mask foreground awal
    fg_mask = ~final_bg

    # 5. Defringing & Smoothing tepi
    pil_fg = Image.fromarray((fg_mask.astype(np.uint8) * 255), mode="L")
    if defringe:
        eroded = pil_fg.filter(ImageFilter.MinFilter(size=3))
        smoothed = eroded.filter(ImageFilter.GaussianBlur(radius=0.6))
        alpha_final = np.asarray(smoothed, dtype=np.uint8)
    else:
        alpha_final = np.asarray(pil_fg, dtype=np.uint8)

    arr[..., 3] = alpha_final
    return Image.fromarray(arr, mode="RGBA")


def export_boxes_pure(
    image: Image.Image,
    boxes: Sequence[Dict[str, Any]],
    indices: Sequence[int],
    output_dir: Path,
    prefix: str,
    label_map: Optional[np.ndarray] = None,
    clean_neighbors: bool = True,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    remove_bg: bool = False,
    bg_color: Optional[Tuple[int, int, int]] = None,
    tolerance: int = 30,
    clean_holes: bool = True,
    defringe: bool = True,
) -> List[Path]:
    if not boxes:
        raise UserFacingError("Tidak ada kotak untuk diekspor.")
    if not indices:
        raise UserFacingError("Tidak ada kotak yang dipilih untuk diekspor.")
    prefix = valid_prefix(prefix)
    output_dir = validate_output_dir(output_dir)
    if len(str(output_dir)) > 260:
        raise PathTooLongError("Path folder output lebih dari 260 karakter.")
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise UserFacingError(f"Folder output tidak bisa ditulis:\n{output_dir}\nPilih folder lain atau periksa izin.") from exc
    except OSError as exc:
        raise UserFacingError(f"Folder output gagal dibuat:\n{output_dir}\n{exc}") from exc

    total = len(indices)
    written: List[Path] = []
    for count, idx in enumerate(indices, start=1):
        if idx < 0 or idx >= len(boxes):
            raise UserFacingError("Indeks kotak tidak valid. Muat ulang sesi atau pilih kotak lagi.")
        rec = boxes[idx]
        box = clamp_box(tuple(rec["box"]), image.size[0], image.size[1])
        if box[2] - box[0] < 1 or box[3] - box[1] < 1:
            raise UserFacingError(f"Kotak nomor {idx + 1:03d} memiliki ukuran nol dan tidak bisa diekspor.")
        crop = image_crop_for_export(image, box)
        if remove_bg:
            crop = remove_background_from_crop(
                crop,
                box,
                rec=rec,
                label_map=label_map,
                bg_color=bg_color,
                tolerance=tolerance,
                clean_neighbors=clean_neighbors,
                clean_holes=clean_holes,
                defringe=defringe,
            )
        else:
            should_clean = (
                clean_neighbors
                and rec.get("source") == "auto"
                and rec.get("cleanable", False)
                and label_map is not None
                and image_has_alpha_mode(image)
            )
            if should_clean:
                ids = set(int(v) for v in rec.get("component_ids", []))
                if ids:
                    labels = label_map[box[1] : box[3], box[0] : box[2]]
                    if "A" in crop.getbands():
                        arr = np.asarray(crop).copy()
                        alpha_index = crop.getbands().index("A")
                        keep = np.isin(labels, list(ids))
                        alpha = arr[..., alpha_index]
                        alpha[~keep] = 0
                        arr[..., alpha_index] = alpha
                        crop = Image.fromarray(arr, mode=crop.mode)
        destination = unique_export_path(output_dir, prefix, idx + 1)
        if len(str(destination)) > 260:
            raise PathTooLongError(f"Path file output terlalu panjang (>260 karakter):\n{destination}")
        try:
            crop.save(destination, format="PNG")
        except PermissionError as exc:
            raise UserFacingError(f"Tidak bisa menulis file:\n{destination}\nPeriksa izin folder output.") from exc
        except OSError as exc:
            if "No space" in str(exc) or getattr(exc, "winerror", None) == 112 or getattr(exc, "errno", None) == errno.ENOSPC:
                raise UserFacingError("Disk penuh saat menulis hasil ekspor. Kosongkan ruang lalu ulangi.") from exc
            raise UserFacingError(f"Gagal menyimpan PNG:\n{destination}\n{exc}") from exc
        written.append(destination)
        if progress_cb:
            progress_cb(count, total)
    return written


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def image_to_canvas(x: float, y: float, zoom: float, pan_x: float, pan_y: float) -> Tuple[float, float]:
    return x * zoom - pan_x, y * zoom - pan_y


def canvas_to_image(x: float, y: float, zoom: float, pan_x: float, pan_y: float) -> Tuple[float, float]:
    if zoom <= 0:
        raise ValueError("zoom harus > 0")
    return (x + pan_x) / zoom, (y + pan_y) / zoom


def roundtrip_coordinate(x: int, y: int, zoom: float, pan_x: float, pan_y: float) -> Tuple[int, int]:
    cx, cy = image_to_canvas(x, y, zoom, pan_x, pan_y)
    ix, iy = canvas_to_image(cx, cy, zoom, pan_x, pan_y)
    return int(round(ix)), int(round(iy))


class BoxEditorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.settings = load_settings()
        self.original: Optional[Image.Image] = None
        self.image_path: Optional[Path] = None
        self.boxes: List[Dict[str, Any]] = []
        self.selected_uids: Set[int] = set()
        self.next_uid = 1
        self.undo_stack: List[List[Dict[str, Any]]] = []
        self.redo_stack: List[List[Dict[str, Any]]] = []
        self.autosave_after_id: Optional[str] = None
        self.current_session_file: Optional[Path] = None
        self.label_map: Optional[np.ndarray] = None
        self.auto_source_mode: Optional[str] = None
        self.auto_background: Optional[Tuple[int, int, int]] = None
        self.auto_background_warning = False
        self.auto_discarded = 0
        self.busy = False
        self.eyedropper_active = False
        self.space_down = False
        self.drag = None
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.min_zoom = 0.05
        self.max_zoom = 8.0
        self.viewport_w = 900
        self.viewport_h = 600
        self.photo: Optional[ImageTk.PhotoImage] = None
        self._suppress_var_trace = False

        self.mode_var = tk.StringVar(value="Manual")
        self.path_var = tk.StringVar(value="")
        self.output_var = tk.StringVar(value="")
        self.prefix_var = tk.StringVar(value="")
        self.rows_var = tk.StringVar(value="3")
        self.cols_var = tk.StringVar(value="3")
        self.margin_var = tk.StringVar(value="0")
        self.grid_gap_var = tk.StringVar(value="0")
        self.tolerance_var = tk.IntVar(value=30)
        self.merge_distance_var = tk.IntVar(value=2)
        self.min_size_var = tk.IntVar(value=16)
        self.padding_var = tk.IntVar(value=2)
        self.alpha_threshold_var = tk.IntVar(value=128)
        self.merge_contained_var = tk.BooleanVar(value=True)
        self.clean_neighbors_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Belum ada gambar.")
        self.bg_info_var = tk.StringVar(value="Background otomatis: -")
        self.zoom_var = tk.StringVar(value="Zoom 100%")
        self.coord_var = tk.StringVar(value="Kursor: (-, -)")
        self.tool_var = tk.StringVar(value="select")
        self.zoom_slider_var = tk.IntVar(value=100)
        self.zoom_percent_var = tk.StringVar(value="100%")
        self._suppress_zoom_slider = False

        self._build_ui()
        self._bind_shortcuts()
        self.refresh_auto_controls()
        self.root.after(150, self._initial_fit_if_ready)

    def _build_ui(self) -> None:
        self.root.title("SnapSlice — Precision Image Splitter")
        self.root.geometry("1350x820")
        self.root.minsize(1000, 600)

        top = ttk.Frame(self.root, padding=(8, 8, 8, 4))
        top.pack(side="top", fill="x")
        ttk.Label(top, text="Path gambar:").grid(row=0, column=0, sticky="w")
        path_entry = ttk.Entry(top, textvariable=self.path_var)
        path_entry.grid(row=0, column=1, sticky="ew", padx=5)
        path_entry.bind("<Return>", lambda e: self.load_from_path())
        ttk.Button(top, text="Buka Gambar", command=self.open_image_picker).grid(row=0, column=2, padx=3)
        ttk.Button(top, text="Muat", command=self.load_from_path).grid(row=0, column=3, padx=3)
        ttk.Button(top, text="Pilih Folder Output", command=self.choose_output_dir).grid(row=1, column=2, padx=3, pady=(5, 0))
        ttk.Label(top, text="Folder output:").grid(row=1, column=0, sticky="w", pady=(5, 0))
        out_label = ttk.Label(top, textvariable=self.output_var, anchor="w")
        out_label.grid(row=1, column=1, columnspan=2, sticky="ew", padx=5, pady=(5, 0))
        ttk.Label(top, text="Awalan:").grid(row=1, column=3, sticky="e", padx=(10, 3), pady=(5, 0))
        ttk.Entry(top, textvariable=self.prefix_var, width=24).grid(row=1, column=4, sticky="ew", padx=(0, 3), pady=(5, 0))
        top.columnconfigure(1, weight=1)
        top.columnconfigure(4, weight=1)

        # KUNCI UTAMA: Dock frame 'bottom' terlebih dahulu di sisi bawah agar TIDAK PERNAH terpotong di layar mana pun
        bottom = ttk.Frame(self.root, padding=(8, 4, 8, 8))
        bottom.pack(side="bottom", fill="x")

        status = ttk.Frame(bottom)
        status.pack(side="bottom", fill="x", pady=(4, 0))
        ttk.Label(status, textvariable=self.status_var, anchor="w").pack(side="left", fill="x", expand=True)
        ttk.Label(status, textvariable=self.zoom_var).pack(side="left", padx=10)
        ttk.Label(status, textvariable=self.coord_var).pack(side="right")

        buttons = ttk.Frame(bottom)
        buttons.pack(side="bottom", fill="x", pady=(2, 0))
        ttk.Button(buttons, text="⚡ Eksekusi Pecah Semua (Simpan)", command=self.export_all).pack(side="left", padx=(0, 4))
        ttk.Button(buttons, text="Eksekusi Pecah Terpilih", command=self.export_selected).pack(side="left", padx=4)
        ttk.Button(buttons, text="Hapus Kotak", command=self.delete_selected).pack(side="left", padx=4)
        ttk.Button(buttons, text="Hapus Semua", command=self.delete_all).pack(side="left", padx=4)
        self.progress = ttk.Progressbar(buttons, mode="determinate", maximum=100)
        self.progress.pack(side="right", fill="x", expand=True, padx=(10, 0))

        # Setelah top dan bottom terpasang permanen, sisa ruang vertikal di tengah diberikan ke canvas & panel kontrol
        main = ttk.Panedwindow(self.root, orient="horizontal")
        main.pack(side="top", fill="both", expand=True, padx=8, pady=4)

        left_frame = ttk.Frame(main)
        right_frame = ttk.Frame(main, width=370)
        main.add(left_frame, weight=4)
        main.add(right_frame, weight=1)

        # Toolbar Navigasi & Zoom (Hand Tool, Select Tool, Zoom Slider, 100%, Pas Layar)
        nav_bar = ttk.Frame(left_frame, padding=(2, 2, 2, 4))
        nav_bar.grid(row=0, column=0, columnspan=2, sticky="ew")

        ttk.Radiobutton(nav_bar, text="✂️ Kotak (V)", value="select", variable=self.tool_var, command=self._on_tool_change).pack(side="left", padx=2)
        ttk.Radiobutton(nav_bar, text="✋ Geser / Hand (H)", value="hand", variable=self.tool_var, command=self._on_tool_change).pack(side="left", padx=2)

        ttk.Separator(nav_bar, orient="vertical").pack(side="left", fill="y", padx=6, pady=2)

        ttk.Button(nav_bar, text="🔍−", width=3, command=lambda: self.adjust_zoom_factor(1 / 1.25)).pack(side="left", padx=1)
        self.zoom_scale = tk.Scale(
            nav_bar,
            from_=10,
            to=500,
            orient="horizontal",
            variable=self.zoom_slider_var,
            showvalue=False,
            length=120,
            width=10,
            command=self._on_zoom_slider,
        )
        self.zoom_scale.pack(side="left", padx=2)
        ttk.Button(nav_bar, text="🔍＋", width=3, command=lambda: self.adjust_zoom_factor(1.25)).pack(side="left", padx=1)
        ttk.Label(nav_bar, textvariable=self.zoom_percent_var, width=5, anchor="center").pack(side="left", padx=2)
        ttk.Button(nav_bar, text="100%", width=4, command=lambda: self.set_zoom(1.0, self.viewport_w / 2, self.viewport_h / 2)).pack(side="left", padx=2)
        ttk.Button(nav_bar, text="⊡ Pas Layar", command=self.fit_to_window).pack(side="left", padx=2)

        self.canvas = tk.Canvas(left_frame, background="#252525", highlightthickness=0, cursor="crosshair")
        self.h_scroll = ttk.Scrollbar(left_frame, orient="horizontal", command=self._xscroll_command)
        self.v_scroll = ttk.Scrollbar(left_frame, orient="vertical", command=self._yscroll_command)
        self.canvas.configure(xscrollcommand=self._set_h_scroll, yscrollcommand=self._set_v_scroll)
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.v_scroll.grid(row=1, column=1, sticky="ns")
        self.h_scroll.grid(row=2, column=0, sticky="ew")
        left_frame.rowconfigure(1, weight=1)
        left_frame.columnconfigure(0, weight=1)
        self.canvas.bind("<Configure>", lambda e: self._on_canvas_configure())
        self.canvas.bind("<Motion>", self._on_canvas_motion)
        self.canvas.bind("<Button-1>", self._on_left_down)
        self.canvas.bind("<B1-Motion>", self._on_left_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_left_up)
        self.canvas.bind("<Button-2>", self._on_middle_down)
        self.canvas.bind("<B2-Motion>", self._on_middle_drag)
        self.canvas.bind("<ButtonRelease-2>", self._on_middle_up)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Shift-MouseWheel>", self._on_shift_mousewheel)
        self.canvas.bind("<Control-MouseWheel>", self._on_ctrl_mousewheel)
        self.canvas.bind("<KeyPress-space>", self._space_down)
        self.canvas.bind("<KeyRelease-space>", self._space_up)
        self.canvas.focus_set()

        mode_box = ttk.LabelFrame(right_frame, text="Mode & parameter", padding=8)
        mode_box.pack(fill="both", expand=True, padx=4, pady=4)
        ttk.Radiobutton(mode_box, text="Manual", value="Manual", variable=self.mode_var, command=self.on_mode_change).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(mode_box, text="Grid", value="Grid", variable=self.mode_var, command=self.on_mode_change).grid(row=0, column=1, sticky="w")
        ttk.Radiobutton(mode_box, text="Otomatis", value="Otomatis", variable=self.mode_var, command=self.on_mode_change).grid(row=0, column=2, sticky="w")

        self.grid_frame = ttk.Frame(mode_box)
        self.grid_frame.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self._grid_param(self.grid_frame, "Baris", self.rows_var, 0)
        self._grid_param(self.grid_frame, "Kolom", self.cols_var, 1)
        self._grid_param(self.grid_frame, "Margin", self.margin_var, 2)
        self._grid_param(self.grid_frame, "Gap", self.grid_gap_var, 3)
        grid_btn_frame = ttk.Frame(self.grid_frame)
        grid_btn_frame.grid(row=4, column=0, columnspan=2, sticky="ew", pady=5)
        ttk.Button(grid_btn_frame, text="Terapkan Grid", command=self.apply_grid).pack(side="left", fill="x", expand=True, padx=(0, 2))
        ttk.Button(grid_btn_frame, text="⚡ Pecah Gambar", command=self.export_all).pack(side="right", fill="x", expand=True, padx=(2, 0))

        self.auto_frame = ttk.Frame(mode_box)
        self.auto_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self._scale_param(self.auto_frame, "Toleransi warna", self.tolerance_var, 0, 0, 100)
        self._scale_param(self.auto_frame, "Jarak gabung", self.merge_distance_var, 1, 0, 20)
        self._scale_param(self.auto_frame, "Ukuran minimum", self.min_size_var, 2, 1, 500)
        self._scale_param(self.auto_frame, "Padding", self.padding_var, 3, 0, 100)
        self.alpha_scale = self._scale_param(self.auto_frame, "Ambang alpha", self.alpha_threshold_var, 4, 1, 254)
        self.merge_check = ttk.Checkbutton(self.auto_frame, text="Gabung kotak jika 90% satu terkandung", variable=self.merge_contained_var)
        self.merge_check.grid(row=5, column=0, columnspan=2, sticky="w")
        self.clean_check = ttk.Checkbutton(self.auto_frame, text="Bersihkan piksel tetangga (alpha)", variable=self.clean_neighbors_var)
        self.clean_check.grid(row=6, column=0, columnspan=2, sticky="w")
        self.bg_info_label = ttk.Label(self.auto_frame, textvariable=self.bg_info_var, wraplength=330)
        self.bg_info_label.grid(row=7, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.eyedropper_button = ttk.Button(self.auto_frame, text="Ambil warna background", command=self.activate_eyedropper)
        self.eyedropper_button.grid(row=8, column=0, columnspan=2, sticky="ew", pady=4)
        
        auto_btn_frame = ttk.Frame(self.auto_frame)
        auto_btn_frame.grid(row=9, column=0, columnspan=2, sticky="ew", pady=5)
        ttk.Button(auto_btn_frame, text="Deteksi Otomatis", command=self.start_auto_detect).pack(side="left", fill="x", expand=True, padx=(0, 2))
        ttk.Button(auto_btn_frame, text="⚡ Eksekusi Pecah Gambar", command=self.execute_auto_split).pack(side="right", fill="x", expand=True, padx=(2, 0))

        ttk.Label(mode_box, text="Daftar kotak:").grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 2))
        list_frame = ttk.Frame(mode_box)
        list_frame.grid(row=4, column=0, columnspan=3, sticky="nsew")
        self.box_list = tk.Listbox(list_frame, selectmode="extended", height=8, exportselection=False)
        list_scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.box_list.yview)
        self.box_list.configure(yscrollcommand=list_scroll.set)
        self.box_list.grid(row=0, column=0, sticky="nsew")
        list_scroll.grid(row=0, column=1, sticky="ns")
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)
        self.box_list.bind("<<ListboxSelect>>", self._on_list_select)
        mode_box.rowconfigure(4, weight=1)
        mode_box.columnconfigure(0, weight=1)

        self._update_param_visibility()
        self._update_title()

    def _grid_param(self, parent: ttk.Frame, label: str, variable: tk.StringVar, row: int) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(parent, textvariable=variable, width=10).grid(row=row, column=1, sticky="ew", pady=2)
        parent.columnconfigure(1, weight=1)

    def _scale_param(self, parent: ttk.Frame, label: str, variable: tk.IntVar, row: int, minimum: int, maximum: int) -> tk.Scale:
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=0, columnspan=2, sticky="ew", pady=1)
        ttk.Label(frame, text=label).pack(side="left")
        scale = tk.Scale(
            frame,
            from_=minimum,
            to=maximum,
            orient="horizontal",
            variable=variable,
            showvalue=True,
            length=180,
            resolution=1,
            width=10,
        )
        scale.pack(side="right", fill="x", expand=True)
        return scale

    def _bind_shortcuts(self) -> None:
        self.root.bind_all("<Control-o>", lambda e: self.open_image_picker())
        self.root.bind_all("<Control-e>", lambda e: self.export_all())
        self.root.bind_all("<Delete>", lambda e: self.delete_selected())
        self.root.bind_all("<Control-a>", lambda e: self.select_all())
        self.root.bind_all("<Escape>", lambda e: self.cancel_selection())
        self.root.bind_all("<Control-z>", lambda e: self.undo())
        self.root.bind_all("<Control-y>", lambda e: self.redo())
        self.root.bind_all("<F1>", lambda e: self.show_help())
        self.root.bind_all("<KeyPress-plus>", lambda e: self.adjust_zoom_factor(1.15))
        self.root.bind_all("<KeyPress-equal>", lambda e: self.adjust_zoom_factor(1.15))
        self.root.bind_all("<KeyPress-minus>", lambda e: self.adjust_zoom_factor(1 / 1.15))
        self.canvas.bind("<Left>", lambda e: self.move_selected_by(-10 if e.state & 0x0001 else -1, 0))
        self.canvas.bind("<Right>", lambda e: self.move_selected_by(10 if e.state & 0x0001 else 1, 0))
        self.canvas.bind("<Up>", lambda e: self.move_selected_by(0, -10 if e.state & 0x0001 else -1))
        self.canvas.bind("<Down>", lambda e: self.move_selected_by(0, 10 if e.state & 0x0001 else 1))
        self.canvas.bind("<KeyPress-0>", lambda e: self.fit_to_window())
        self.canvas.bind("<KeyPress-1>", lambda e: self.set_zoom(1.0, self.viewport_w / 2, self.viewport_h / 2))
        self.canvas.bind("<KeyPress-h>", lambda e: self.set_tool("hand"))
        self.canvas.bind("<KeyPress-H>", lambda e: self.set_tool("hand"))
        self.canvas.bind("<KeyPress-v>", lambda e: self.set_tool("select"))
        self.canvas.bind("<KeyPress-V>", lambda e: self.set_tool("select"))

    def _on_tool_change(self) -> None:
        self._update_canvas_cursor()
        mode_text = "Geser / Hand (Tahan & drag mouse untuk menggeser posisi gambar)" if self.tool_var.get() == "hand" else "Kotak / Seleksi (Drag untuk membuat/mengedit kotak)"
        self.status_var.set(f"Tool aktif: {mode_text}")

    def set_tool(self, tool: str) -> None:
        self.tool_var.set(tool)
        self._on_tool_change()

    def _on_zoom_slider(self, val_str: str) -> None:
        if self._suppress_zoom_slider or not self.original:
            return
        try:
            val = float(val_str)
            target_zoom = val / 100.0
            self.set_zoom(target_zoom, self.viewport_w / 2, self.viewport_h / 2)
        except (ValueError, TypeError):
            pass

    def _update_canvas_cursor(self) -> None:
        if self.busy:
            cursor = "watch"
        elif self.eyedropper_active:
            cursor = "tcross"
        elif self.tool_var.get() == "hand" or self.space_down:
            cursor = "fleur"
        else:
            cursor = "crosshair"
        try:
            self.canvas.configure(cursor=cursor)
        except tk.TclError:
            pass

    def _initial_fit_if_ready(self) -> None:
        self._on_canvas_configure()

    def _on_canvas_configure(self) -> None:
        self.viewport_w = max(1, self.canvas.winfo_width())
        self.viewport_h = max(1, self.canvas.winfo_height())
        self._clamp_pan()
        self.render()

    def _content_size(self) -> Tuple[float, float]:
        if not self.original:
            return 0.0, 0.0
        return self.original.size[0] * self.zoom, self.original.size[1] * self.zoom

    def _clamp_pan(self) -> None:
        content_w, content_h = self._content_size()
        self.pan_x = max(0.0, min(self.pan_x, max(0.0, content_w - self.viewport_w)))
        self.pan_y = max(0.0, min(self.pan_y, max(0.0, content_h - self.viewport_h)))

    def _set_h_scroll(self, first: float, last: float) -> None:
        self.h_scroll.set(first, last)

    def _set_v_scroll(self, first: float, last: float) -> None:
        self.v_scroll.set(first, last)

    def _xscroll_command(self, *args: str) -> None:
        self._scroll_axis("x", args)

    def _yscroll_command(self, *args: str) -> None:
        self._scroll_axis("y", args)

    def _scroll_axis(self, axis: str, args: Sequence[str]) -> None:
        content_w, content_h = self._content_size()
        content = content_w if axis == "x" else content_h
        viewport = self.viewport_w if axis == "x" else self.viewport_h
        maximum = max(0.0, content - viewport)
        current = self.pan_x if axis == "x" else self.pan_y
        if not args:
            return
        if args[0] == "moveto":
            frac = float(args[1])
            new_value = frac * maximum
        elif args[0] == "scroll":
            amount = float(args[1])
            what = args[2] if len(args) > 2 else "units"
            unit = 40.0 if what == "units" else viewport
            new_value = current + amount * unit
        else:
            return
        if axis == "x":
            self.pan_x = max(0.0, min(maximum, new_value))
        else:
            self.pan_y = max(0.0, min(maximum, new_value))
        self.render()

    def _image_point_from_canvas(self, cx: float, cy: float) -> Tuple[int, int]:
        ix, iy = canvas_to_image(cx, cy, self.zoom, self.pan_x, self.pan_y)
        if self.original:
            ix = max(0.0, min(self.original.size[0] - 1e-9, ix))
            iy = max(0.0, min(self.original.size[1] - 1e-9, iy))
        return int(round(ix)), int(round(iy))

    def render(self) -> None:
        self.canvas.delete("all")
        if not self.original:
            self.zoom_var.set("Zoom 100%")
            return
        self._clamp_pan()
        W, H = self.original.size
        x0 = max(0, int(math.floor(self.pan_x / self.zoom)))
        y0 = max(0, int(math.floor(self.pan_y / self.zoom)))
        x1 = min(W, int(math.ceil((self.pan_x + self.viewport_w) / self.zoom)))
        y1 = min(H, int(math.ceil((self.pan_y + self.viewport_h) / self.zoom)))
        if x1 <= x0 or y1 <= y0:
            return
        crop = self.original.crop((x0, y0, x1, y1))
        target_w = max(1, int(round((x1 - x0) * self.zoom)))
        target_h = max(1, int(round((y1 - y0) * self.zoom)))
        if (crop.width, crop.height) != (target_w, target_h):
            resample = Image.Resampling.LANCZOS if self.zoom < 1 else Image.Resampling.NEAREST
            crop = crop.resize((target_w, target_h), resample=resample)
        try:
            self.photo = ImageTk.PhotoImage(crop)
            image_cx, image_cy = image_to_canvas(x0, y0, self.zoom, self.pan_x, self.pan_y)
            self.canvas.create_image(image_cx, image_cy, anchor="nw", image=self.photo, tags="image")
        except tk.TclError as exc:
            log_error("Render gambar Tkinter", exc)
            return

        selected_uid = next(iter(self.selected_uids), None)
        for i, rec in enumerate(self.boxes):
            x1b, y1b, x2b, y2b = rec["box"]
            cx1, cy1 = image_to_canvas(x1b, y1b, self.zoom, self.pan_x, self.pan_y)
            cx2, cy2 = image_to_canvas(x2b, y2b, self.zoom, self.pan_x, self.pan_y)
            area = box_area(rec["box"])
            median_area = self._median_box_area()
            warning = median_area > 0 and area < 0.10 * median_area
            is_selected = rec["uid"] in self.selected_uids
            outline = "#ff884d" if warning else ("#33ff88" if is_selected else "#00d7ff")
            width = 3 if is_selected else 2
            self.canvas.create_rectangle(cx1, cy1, cx2, cy2, outline=outline, width=width, tags=f"box_{rec['uid']}")
            if is_selected and rec["uid"] == selected_uid and len(self.selected_uids) == 1:
                self._draw_handles(cx1, cy1, cx2, cy2)
        self.canvas.tag_lower("image")
        pct_int = int(round(self.zoom * 100))
        pct_str = f"{pct_int}%"
        self.zoom_var.set(f"Zoom {pct_str}")
        self.zoom_percent_var.set(pct_str)
        if hasattr(self, "zoom_scale") and not self._suppress_zoom_slider:
            self._suppress_zoom_slider = True
            try:
                self.zoom_slider_var.set(max(10, min(500, pct_int)))
            finally:
                self._suppress_zoom_slider = False

    def _draw_handles(self, x1: float, y1: float, x2: float, y2: float) -> None:
        size = 5
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        centers = [(x1, y1), (mx, y1), (x2, y1), (x1, my), (x2, my), (x1, y2), (mx, y2), (x2, y2)]
        for cx, cy in centers:
            self.canvas.create_rectangle(cx - size, cy - size, cx + size, cy + size, fill="#ffffff", outline="#111111", width=1, tags="handle")

    def _median_box_area(self) -> float:
        if not self.boxes:
            return 0.0
        return float(np.median([box_area(x["box"]) for x in self.boxes]))

    def _on_canvas_motion(self, event: tk.Event) -> None:
        if not self.original:
            self.coord_var.set("Kursor: (-, -)")
            return
        ix, iy = self._image_point_from_canvas(event.x, event.y)
        self.coord_var.set(f"Kursor: ({ix}, {iy}) px")
        if self.busy or self.eyedropper_active:
            return
        if self.tool_var.get() == "hand" or self.space_down:
            try:
                self.canvas.configure(cursor="fleur")
            except tk.TclError:
                pass
            return
        selected_idx = self._primary_selected_index()
        if selected_idx is not None and len(self.selected_uids) == 1:
            handle = self._handle_at(event.x, event.y, self.boxes[selected_idx]["box"])
            if handle:
                cursor_map = {
                    "nw": "size_nw_se", "se": "size_nw_se",
                    "ne": "size_ne_sw", "sw": "size_ne_sw",
                    "n": "size_ns", "s": "size_ns",
                    "w": "size_we", "e": "size_we",
                }
                try:
                    self.canvas.configure(cursor=cursor_map.get(handle, "crosshair"))
                except tk.TclError:
                    pass
                return
        try:
            self.canvas.configure(cursor="crosshair")
        except tk.TclError:
            pass

    def _on_mousewheel(self, event: tk.Event) -> str:
        if self.busy:
            return "break"
        self._scroll_axis("y", ("scroll", str(-1 if event.delta > 0 else 1), "units"))
        return "break"

    def _on_shift_mousewheel(self, event: tk.Event) -> str:
        if self.busy:
            return "break"
        self._scroll_axis("x", ("scroll", str(-1 if event.delta > 0 else 1), "units"))
        return "break"

    def _on_ctrl_mousewheel(self, event: tk.Event) -> str:
        if self.busy:
            return "break"
        factor = 1.20 if event.delta > 0 else 1 / 1.20
        self.set_zoom(self.zoom * factor, event.x, event.y)
        return "break"

    def _space_down(self, event: tk.Event) -> str:
        self.space_down = True
        self.canvas.configure(cursor="fleur")
        return "break"

    def _space_up(self, event: tk.Event) -> str:
        self.space_down = False
        self._update_canvas_cursor()
        return "break"

    def _on_middle_down(self, event: tk.Event) -> None:
        self.canvas.scan_mark(event.x, event.y)
        self.drag = {"type": "pan", "x": event.x, "y": event.y, "pan_x": self.pan_x, "pan_y": self.pan_y}

    def _on_middle_drag(self, event: tk.Event) -> None:
        if not self.drag or self.drag.get("type") != "pan":
            return
        dx = event.x - self.drag["x"]
        dy = event.y - self.drag["y"]
        self.pan_x = self.drag["pan_x"] - dx
        self.pan_y = self.drag["pan_y"] - dy
        self._clamp_pan()
        self.render()

    def _on_middle_up(self, event: tk.Event) -> None:
        self.drag = None

    def _handle_at(self, cx: float, cy: float, box: Tuple[int, int, int, int]) -> Optional[str]:
        x1, y1, x2, y2 = box
        p = [image_to_canvas(x1, y1, self.zoom, self.pan_x, self.pan_y),
             image_to_canvas((x1 + x2) / 2, y1, self.zoom, self.pan_x, self.pan_y),
             image_to_canvas(x2, y1, self.zoom, self.pan_x, self.pan_y),
             image_to_canvas(x1, (y1 + y2) / 2, self.zoom, self.pan_x, self.pan_y),
             image_to_canvas(x2, (y1 + y2) / 2, self.zoom, self.pan_x, self.pan_y),
             image_to_canvas(x1, y2, self.zoom, self.pan_x, self.pan_y),
             image_to_canvas((x1 + x2) / 2, y2, self.zoom, self.pan_x, self.pan_y),
             image_to_canvas(x2, y2, self.zoom, self.pan_x, self.pan_y)]
        names = ["nw", "n", "ne", "w", "e", "sw", "s", "se"]
        for name, (hx, hy) in zip(names, p):
            if abs(cx - hx) <= 7 and abs(cy - hy) <= 7:
                return name
        return None

    def _box_under(self, cx: float, cy: float) -> Optional[int]:
        ix, iy = canvas_to_image(cx, cy, self.zoom, self.pan_x, self.pan_y)
        for i in range(len(self.boxes) - 1, -1, -1):
            x1, y1, x2, y2 = self.boxes[i]["box"]
            if x1 <= ix <= x2 and y1 <= iy <= y2:
                return i
        return None

    def _on_left_down(self, event: tk.Event) -> None:
        if self.busy or not self.original:
            return
        self.canvas.focus_set()
        if self.eyedropper_active:
            self._sample_background(event.x, event.y)
            return
        if self.tool_var.get() == "hand" or self.space_down:
            self._on_middle_down(event)
            try:
                self.canvas.configure(cursor="fleur")
            except tk.TclError:
                pass
            return
        selected_idx = self._primary_selected_index()
        if selected_idx is not None and len(self.selected_uids) == 1:
            handle = self._handle_at(event.x, event.y, self.boxes[selected_idx]["box"])
            if handle:
                self._push_undo()
                self.drag = {"type": "resize", "uid": self.boxes[selected_idx]["uid"], "handle": handle, "start": self.boxes[selected_idx]["box"]}
                return
        under = self._box_under(event.x, event.y)
        if under is not None:
            uid = self.boxes[under]["uid"]
            self.selected_uids = {uid}
            self.refresh_list(select_uids=self.selected_uids)
            self._push_undo()
            self.drag = {"type": "move", "uid": uid, "start": self.boxes[under]["box"], "x": event.x, "y": event.y}
        else:
            ix, iy = canvas_to_image(event.x, event.y, self.zoom, self.pan_x, self.pan_y)
            ix, iy = int(round(ix)), int(round(iy))
            self.drag = {"type": "create", "x1": ix, "y1": iy, "x2": ix, "y2": iy}

    def _on_left_drag(self, event: tk.Event) -> None:
        if self.busy or not self.drag or not self.original:
            return
        dtype = self.drag.get("type")
        if dtype == "pan":
            self._on_middle_drag(event)
            return
        if dtype == "create":
            ix, iy = canvas_to_image(event.x, event.y, self.zoom, self.pan_x, self.pan_y)
            ix, iy = int(round(ix)), int(round(iy))
            self.drag["x2"], self.drag["y2"] = ix, iy
            x1, y1, x2, y2 = clamp_box((self.drag["x1"], self.drag["y1"], ix, iy), *self.original.size)
            self.canvas.delete("temp_box")
            cx1, cy1 = image_to_canvas(x1, y1, self.zoom, self.pan_x, self.pan_y)
            cx2, cy2 = image_to_canvas(x2, y2, self.zoom, self.pan_x, self.pan_y)
            self.canvas.create_rectangle(cx1, cy1, cx2, cy2, outline="#ffff00", width=2, tags="temp_box")
            return
        if dtype == "move":
            rec = self._find_by_uid(self.drag["uid"])
            if rec is None:
                return
            dx_img = int(round((event.x - self.drag["x"]) / self.zoom))
            dy_img = int(round((event.y - self.drag["y"]) / self.zoom))
            sx1, sy1, sx2, sy2 = self.drag["start"]
            w, h = sx2 - sx1, sy2 - sy1
            nx1 = max(0, min(self.original.size[0] - w, sx1 + dx_img))
            ny1 = max(0, min(self.original.size[1] - h, sy1 + dy_img))
            rec["box"] = (int(nx1), int(ny1), int(nx1 + w), int(ny1 + h))
            rec["cleanable"] = False
            rec["component_ids"] = []
            self.render()
            return
        if dtype == "resize":
            rec = self._find_by_uid(self.drag["uid"])
            if rec is None:
                return
            ix, iy = canvas_to_image(event.x, event.y, self.zoom, self.pan_x, self.pan_y)
            ix, iy = int(round(ix)), int(round(iy))
            sx1, sy1, sx2, sy2 = self.drag["start"]
            handle = self.drag["handle"]
            nx1, ny1, nx2, ny2 = sx1, sy1, sx2, sy2
            if "w" in handle:
                nx1 = min(max(0, ix), sx2 - 1)
            if "e" in handle:
                nx2 = max(min(self.original.size[0], ix), sx1 + 1)
            if "n" in handle:
                ny1 = min(max(0, iy), sy2 - 1)
            if "s" in handle:
                ny2 = max(min(self.original.size[1], iy), sy1 + 1)
            rec["box"] = clamp_box((nx1, ny1, nx2, ny2), *self.original.size)
            if rec["box"][2] - rec["box"][0] < 1:
                rec["box"] = (rec["box"][0], rec["box"][1], min(self.original.size[0], rec["box"][0] + 1), rec["box"][3])
            if rec["box"][3] - rec["box"][1] < 1:
                rec["box"] = (rec["box"][0], rec["box"][1], rec["box"][2], min(self.original.size[1], rec["box"][1] + 1))
            rec["cleanable"] = False
            rec["component_ids"] = []
            self.render()

    def _on_left_up(self, event: tk.Event) -> None:
        if self.drag and self.drag.get("type") == "pan":
            self._on_middle_up(event)
            self._update_canvas_cursor()
            return
        if not self.drag or not self.original:
            return
        dtype = self.drag.get("type")
        if dtype == "create":
            x1, y1, x2, y2 = clamp_box((self.drag["x1"], self.drag["y1"], self.drag["x2"], self.drag["y2"]), *self.original.size)
            if x2 - x1 >= 1 and y2 - y1 >= 1:
                self._push_undo(clear_redo=False)
                self.boxes.append({"box": (x1, y1, x2, y2), "uid": self.next_uid, "source": "manual", "cleanable": False, "component_ids": []})
                self.next_uid += 1
                self.selected_uids = {self.boxes[-1]["uid"]}
                self.sort_and_refresh()
        elif dtype in {"move", "resize"}:
            self.sort_and_refresh()
        self.canvas.delete("temp_box")
        self.drag = None
        self.schedule_autosave()

    def _find_by_uid(self, uid: int) -> Optional[Dict[str, Any]]:
        for rec in self.boxes:
            if rec["uid"] == uid:
                return rec
        return None

    def _primary_selected_index(self) -> Optional[int]:
        if not self.selected_uids:
            return None
        for i, rec in enumerate(self.boxes):
            if rec["uid"] in self.selected_uids:
                return i
        return None

    def _on_list_select(self, event: tk.Event) -> None:
        if self._suppress_var_trace:
            return
        indices = list(self.box_list.curselection())
        self.selected_uids = {self.boxes[i]["uid"] for i in indices if 0 <= i < len(self.boxes)}
        self.render()
        self.schedule_autosave()

    def refresh_list(self, select_uids: Optional[Set[int]] = None) -> None:
        selected = self.selected_uids if select_uids is None else select_uids
        self._suppress_var_trace = True
        try:
            self.box_list.delete(0, tk.END)
            median_area = self._median_box_area()
            for i, rec in enumerate(self.boxes, start=1):
                x1, y1, x2, y2 = rec["box"]
                bw, bh = x2 - x1, y2 - y1
                warning = median_area > 0 and bw * bh < 0.10 * median_area
                marker = "[!] " if warning else "    "
                self.box_list.insert(tk.END, f"{marker}{i:03d}  {bw}x{bh}")
                if warning:
                    self.box_list.itemconfig(i - 1, foreground="#d55d00")
            for i, rec in enumerate(self.boxes):
                if rec["uid"] in selected:
                    self.box_list.selection_set(i)
            self.selected_uids = set(selected)
        finally:
            self._suppress_var_trace = False
        self.render()
        self.update_status()

    def sort_and_refresh(self) -> None:
        selected = set(self.selected_uids)
        self.boxes = sort_boxes_reading(self.boxes)
        self.refresh_list(selected)
        self.schedule_autosave()

    def _push_undo(self, clear_redo: bool = True) -> None:
        self.undo_stack.append(copy.deepcopy(self.boxes))
        if len(self.undo_stack) > UNDO_LIMIT:
            self.undo_stack.pop(0)
        if clear_redo:
            self.redo_stack.clear()

    def undo(self) -> None:
        if self.busy or not self.undo_stack:
            return
        self.redo_stack.append(copy.deepcopy(self.boxes))
        self.boxes = self.undo_stack.pop()
        self.selected_uids.clear()
        self.refresh_list()
        self.schedule_autosave()

    def redo(self) -> None:
        if self.busy or not self.redo_stack:
            return
        self.undo_stack.append(copy.deepcopy(self.boxes))
        self.boxes = self.redo_stack.pop()
        self.selected_uids.clear()
        self.refresh_list()
        self.schedule_autosave()

    def move_selected_by(self, dx: int, dy: int) -> None:
        if self.busy or not self.original or not self.selected_uids:
            return
        self._push_undo()
        W, H = self.original.size
        for rec in self.boxes:
            if rec["uid"] not in self.selected_uids:
                continue
            x1, y1, x2, y2 = rec["box"]
            w, h = x2 - x1, y2 - y1
            nx1 = max(0, min(W - w, x1 + int(dx)))
            ny1 = max(0, min(H - h, y1 + int(dy)))
            rec["box"] = (nx1, ny1, nx1 + w, ny1 + h)
            rec["cleanable"] = False
            rec["component_ids"] = []
        self.sort_and_refresh()

    def delete_selected(self) -> None:
        if self.busy or not self.selected_uids:
            return
        self._push_undo()
        self.boxes = [b for b in self.boxes if b["uid"] not in self.selected_uids]
        self.selected_uids.clear()
        self.refresh_list()
        self.schedule_autosave()

    def delete_all(self) -> None:
        if self.busy or not self.boxes:
            return
        if not messagebox.askyesno("Hapus semua kotak", "Hapus semua kotak? Perubahan masih bisa di-undo.", parent=self.root):
            return
        self._push_undo()
        self.boxes.clear()
        self.selected_uids.clear()
        self.refresh_list()
        self.schedule_autosave()

    def select_all(self) -> None:
        if self.busy or not self.boxes:
            return
        self.selected_uids = {b["uid"] for b in self.boxes}
        self.refresh_list(self.selected_uids)

    def cancel_selection(self) -> None:
        self.eyedropper_active = False
        self.selected_uids.clear()
        self.drag = None
        self.canvas.delete("temp_box")
        self.canvas.configure(cursor="crosshair")
        self.refresh_list(set())

    def open_image_picker(self) -> None:
        if self.busy:
            return
        self.root.update_idletasks()
        try:
            self.root.lift()
        except tk.TclError:
            pass
        initial = self.settings.get("last_folder")
        if not initial or not Path(str(initial)).is_dir():
            initial = str(Path.home())
        filetypes = [
            ("Gambar", "*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff"),
            ("Semua file", "*.*"),
        ]
        try:
            filename = filedialog.askopenfilename(parent=self.root, title="Pilih Gambar Kolase", filetypes=filetypes, initialdir=initial)
        except Exception as exc:
            self.handle_exception("File picker gambar", exc)
            return
        if not filename:
            return
        self.path_var.set(filename)
        try:
            self._load_image(filename)
        except UserFacingError as exc:
            if str(exc) == "__LARGE_IMAGE_PROMPT__":
                self._ask_large_and_retry(filename)
            else:
                self.show_user_error(str(exc), "Muat gambar", exc)
        except Exception as exc:
            self.handle_exception("Memuat gambar dari file picker", exc)

    def _ask_large_and_retry(self, raw: str) -> None:
        ok = messagebox.askyesno(
            "Gambar sangat besar",
            f"Gambar lebih besar dari {MAX_IMAGE_PIXELS_PROMPT:,} piksel.\n\nLanjutkan? Ini dapat membutuhkan RAM besar.",
            parent=self.root,
        )
        if not ok:
            self.status_var.set("Pemuatan dibatalkan karena gambar sangat besar.")
            return
        try:
            self._load_image(raw, allow_large=True)
        except Exception as exc:
            if isinstance(exc, UserFacingError):
                self.show_user_error(str(exc), "Muat gambar", exc)
            else:
                self.handle_exception("Memuat gambar besar", exc)

    def load_from_path(self) -> None:
        if self.busy:
            return
        try:
            self._load_image(self.path_var.get())
        except UserFacingError as exc:
            if str(exc) == "__LARGE_IMAGE_PROMPT__":
                self._ask_large_and_retry(self.path_var.get())
            else:
                self.show_user_error(str(exc), "Muat gambar", exc)
        except Exception as exc:
            self.handle_exception("Muat gambar dari kolom path", exc)

    def _load_image(self, raw: str, allow_large: bool = False) -> None:
        path = normalize_image_path(raw)
        if not allow_large:
            try:
                with Image.open(path) as probe:
                    if probe.size[0] * probe.size[1] > MAX_IMAGE_PIXELS_PROMPT:
                        raise UserFacingError("__LARGE_IMAGE_PROMPT__")
            except Image.DecompressionBombError as exc:
                raise UserFacingError("Gambar terlalu besar dan Pillow menolaknya sebagai risiko decompression bomb.") from exc
            except UserFacingError:
                raise
            except (OSError, Image.UnidentifiedImageError) as exc:
                raise UserFacingError(f"Gambar tidak dapat dibuka ({type(exc).__name__}).") from exc
        image = open_image_safe(path, allow_large=True)
        self.original = image
        self.image_path = path
        self.current_session_file, _ = session_paths(path)
        self.boxes.clear()
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.selected_uids.clear()
        self.next_uid = 1
        self.label_map = None
        self.auto_source_mode = None
        self.auto_background = None
        self.auto_background_warning = False
        self.auto_discarded = 0
        self.output_var.set(str(default_output_dir(path)))
        self.prefix_var.set(path.stem)
        self.settings["last_folder"] = str(path.parent)
        save_settings(path.parent)
        self.zoom = 1.0
        self.pan_x = self.pan_y = 0.0
        self.update_image_info()
        self.refresh_auto_controls()
        self.refresh_list()
        try:
            size_bytes = path.stat().st_size
            size_str = f"{size_bytes:,} byte"
        except OSError:
            size_str = "-"
        self.status_var.set(f"Memuat: {path.name} — {size_str}, mode {image.mode}.")
        self._update_title()
        self._resume_session_if_available(path)

    def _resume_session_if_available(self, path: Path) -> None:
        try:
            session = load_session_file(path)
        except UserFacingError as exc:
            messagebox.showwarning("Sesi diabaikan", str(exc), parent=self.root)
            self.status_var.set("Sesi sebelumnya diabaikan karena rusak/tidak cocok.")
            return
        if not session:
            return
        if not messagebox.askyesno("Lanjutkan sesi sebelumnya?", "Sesi sebelumnya ditemukan untuk gambar ini. Lanjutkan sesi sebelumnya?", parent=self.root):
            return
        try:
            params = session.get("parameters", {})
            self.mode_var.set(str(params.get("mode", self.mode_var.get())))
            self.rows_var.set(str(params.get("rows", self.rows_var.get())))
            self.cols_var.set(str(params.get("cols", self.cols_var.get())))
            self.margin_var.set(str(params.get("margin", self.margin_var.get())))
            self.grid_gap_var.set(str(params.get("grid_gap", self.grid_gap_var.get())))
            self.tolerance_var.set(int(params.get("tolerance", self.tolerance_var.get())))
            self.merge_distance_var.set(int(params.get("merge_distance", self.merge_distance_var.get())))
            self.min_size_var.set(int(params.get("min_size", self.min_size_var.get())))
            self.padding_var.set(int(params.get("padding", self.padding_var.get())))
            self.alpha_threshold_var.set(int(params.get("alpha_threshold", self.alpha_threshold_var.get())))
            self.merge_contained_var.set(bool(params.get("merge_contained", True)))
            self.clean_neighbors_var.set(bool(params.get("clean_neighbors", True)))
            bg = params.get("bg_override")
            self.auto_background = tuple(bg) if isinstance(bg, list) and len(bg) == 3 else None
            saved_out = params.get("output_dir")
            if saved_out:
                try:
                    self.output_var.set(str(validate_output_dir(Path(saved_out))))
                except Exception:
                    pass
            self.prefix_var.set(str(params.get("prefix", self.prefix_var.get())))
            restored: List[Dict[str, Any]] = []
            for rec in session.get("boxes", []):
                box = tuple(rec.get("box", []))
                if len(box) != 4:
                    continue
                box = clamp_box(box, *self.original.size)
                if box[2] - box[0] < 1 or box[3] - box[1] < 1:
                    continue
                restored.append({
                    "box": box,
                    "uid": self.next_uid,
                    "source": rec.get("source", "manual"),
                    "cleanable": bool(rec.get("cleanable", False)),
                    "component_ids": [int(v) for v in rec.get("component_ids", []) if isinstance(v, int)],
                })
                self.next_uid += 1
            self.boxes = sort_boxes_reading(restored)
            self.refresh_auto_controls()
            self.refresh_list()
            self.zoom = float(params.get("zoom", 1.0))
            self.pan_x = float(params.get("pan_x", 0.0))
            self.pan_y = float(params.get("pan_y", 0.0))
            self._clamp_pan()
            self.render()
            self.status_var.set(f"Sesi dipulihkan: {len(self.boxes)} kotak.")
        except Exception as exc:
            log_error("Memulihkan data sesi", exc)
            messagebox.showwarning("Sesi diabaikan", "Sesi tidak dapat dipulihkan secara lengkap dan diabaikan.", parent=self.root)

    def choose_output_dir(self) -> None:
        if self.busy:
            return
        self.root.update_idletasks()
        try:
            self.root.lift()
        except tk.TclError:
            pass
        initial = self.output_var.get().strip() or self.settings.get("last_folder") or str(Path.home())
        try:
            if not Path(initial).is_dir():
                initial = str(Path.home())
        except OSError:
            initial = str(Path.home())
        try:
            selected = filedialog.askdirectory(parent=self.root, title="Pilih Folder Output", initialdir=initial, mustexist=True)
        except Exception as exc:
            self.handle_exception("File picker folder output", exc)
            return
        if not selected:
            return
        try:
            path = validate_output_dir(Path(selected))
            self.output_var.set(str(path))
            self.settings["last_folder"] = str(path)
            save_settings(path)
            self.schedule_autosave()
        except Exception as exc:
            self.show_user_error(str(exc), "Folder output", exc)

    def update_image_info(self) -> None:
        if not self.original or not self.image_path:
            return
        W, H = self.original.size
        self.status_var.set(f"Gambar: {W}x{H} px | {self.original.mode} | 0 kotak")

    def update_status(self) -> None:
        if self.original:
            self.status_var.set(
                f"Kotak: {len(self.boxes)} | Ukuran: {self.original.size[0]}x{self.original.size[1]} | Dibuang: {self.auto_discarded}"
            )
        else:
            self.status_var.set("Belum ada gambar.")

    def on_mode_change(self) -> None:
        self.refresh_auto_controls()
        self._update_param_visibility()

    def _update_param_visibility(self) -> None:
        mode = self.mode_var.get()
        if mode == "Grid":
            self.grid_frame.grid()
            self.auto_frame.grid_remove()
        elif mode == "Otomatis":
            self.grid_frame.grid_remove()
            self.auto_frame.grid()
        else:
            self.grid_frame.grid_remove()
            self.auto_frame.grid_remove()

    def refresh_auto_controls(self) -> None:
        if not hasattr(self, "eyedropper_button"):
            return
        use_alpha = bool(self.original and alpha_mode_should_be_used(self.original))
        if use_alpha:
            self.eyedropper_button.grid_remove()
            self.alpha_scale.configure(state="normal")
            self.clean_check.configure(state="normal")
            self.bg_info_var.set("Mode deteksi: ALPHA — RGB di area transparan diabaikan.")
        else:
            self.eyedropper_button.grid()
            self.alpha_scale.configure(state="disabled")
            self.clean_check.configure(state="disabled")
            if self.auto_background is None:
                self.bg_info_var.set("Mode deteksi: WARNA — background akan ditebak dari tepi gambar.")
            else:
                self.bg_info_var.set(f"Background manual: RGB {self.auto_background}")

    def activate_eyedropper(self) -> None:
        if not self.original or self.mode_var.get() != "Otomatis":
            return
        if alpha_mode_should_be_used(self.original):
            return
        self.eyedropper_active = True
        self.canvas.configure(cursor="tcross")
        self.status_var.set("Eyedropper aktif. Klik piksel background pada canvas.")

    def _sample_background(self, cx: float, cy: float) -> None:
        if not self.original:
            return
        ix, iy = self._image_point_from_canvas(cx, cy)
        rgb = self.original.convert("RGB").getpixel((ix, iy))
        self.auto_background = tuple(int(v) for v in rgb)
        self.eyedropper_active = False
        self.canvas.configure(cursor="crosshair")
        self.refresh_auto_controls()
        self.status_var.set(f"Background manual dipilih: RGB {self.auto_background}. Jalankan Deteksi Otomatis lagi.")
        self.schedule_autosave()

    def _warn_large_box_count(self, count: int) -> bool:
        if count <= MAX_BOXES_CONFIRM:
            return True
        return messagebox.askyesno(
            "Banyak kotak",
            f"Deteksi menghasilkan {count} kotak, lebih dari {MAX_BOXES_CONFIRM}. Lanjutkan?",
            parent=self.root,
        )

    def apply_grid(self) -> None:
        if self.busy or not self.original:
            messagebox.showinfo("Grid", "Muat gambar terlebih dahulu.", parent=self.root)
            return
        if self.boxes and not messagebox.askyesno("Ganti kotak", "Kotak yang ada akan diganti oleh grid. Lanjutkan?", parent=self.root):
            return
        try:
            rows = int(self.rows_var.get().strip())
            cols = int(self.cols_var.get().strip())
            margin = int(self.margin_var.get().strip())
            gap = int(self.grid_gap_var.get().strip())
            boxes = generate_grid(self.original.size, rows, cols, margin, gap)
        except ValueError as exc:
            self.show_user_error("Parameter Grid harus berupa bilangan bulat yang valid.", "Validasi Grid", exc)
            return
        except UserFacingError as exc:
            self.show_user_error(str(exc), "Validasi Grid", exc)
            return
        self._push_undo()
        self.label_map = None
        self.auto_source_mode = None
        self.auto_discarded = 0
        self.boxes = []
        for box in boxes:
            self.boxes.append({"box": box, "uid": self.next_uid, "source": "grid", "cleanable": False, "component_ids": []})
            self.next_uid += 1
        self.boxes = sort_boxes_reading(self.boxes)
        self.selected_uids.clear()
        self.refresh_list()
        self.status_var.set(f"Grid diterapkan: {len(self.boxes)} kotak.")
        self.schedule_autosave()

    def execute_auto_split(self) -> None:
        """Eksekusi pemecahan gambar: jika sudah ada kotak langsung ekspor, jika belum maka deteksi lalu langsung ekspor."""
        if self.busy:
            return
        if not self.original:
            messagebox.showinfo("Pecah Gambar", "Muat gambar terlebih dahulu.", parent=self.root)
            return
        if self.boxes:
            self.export_all()
        else:
            self.start_auto_detect(and_export=True)

    def start_auto_detect(self, and_export: bool = False) -> None:
        if self.busy or not self.original:
            messagebox.showinfo("Deteksi otomatis", "Muat gambar terlebih dahulu.", parent=self.root)
            return
        try:
            tolerance = int(self.tolerance_var.get())
            distance = int(self.merge_distance_var.get())
            min_size = int(self.min_size_var.get())
            padding = int(self.padding_var.get())
            alpha_threshold = int(self.alpha_threshold_var.get())
            merge_contained = bool(self.merge_contained_var.get())
            bg_override = self.auto_background
        except (TypeError, ValueError) as exc:
            self.show_user_error("Parameter Otomatis harus berupa angka yang valid.", "Validasi Otomatis", exc)
            return
        if distance > 8:
            messagebox.showwarning("Jarak gabung besar", "Jarak gabung di atas 8 px dapat menyatukan objek yang berdekatan secara agresif. Pada gambar tertentu bahkan banyak objek dapat runtuh menjadi sangat sedikit kotak.", parent=self.root)
        if self.boxes and not and_export and not messagebox.askyesno("Ganti kotak", "Deteksi otomatis akan mengganti kotak yang ada. Lanjutkan?", parent=self.root):
            return
        self.set_busy(True)
        source_image = self.original.copy()

        def worker() -> None:
            try:
                result = detect_automatic(
                    source_image,
                    tolerance=tolerance,
                    merge_distance=distance,
                    min_size=min_size,
                    padding=padding,
                    merge_contained=merge_contained,
                    alpha_threshold=alpha_threshold,
                    bg_override=bg_override,
                )
                self.root.after(0, lambda r=result: self._auto_detect_done(r, and_export=and_export))
            except Exception as exc:
                self.root.after(0, lambda e=exc: self._worker_error("Deteksi otomatis", e))

        threading.Thread(target=worker, daemon=True).start()

    def _auto_detect_done(self, result: Dict[str, Any], and_export: bool = False) -> None:
        try:
            count = len(result["boxes"])
            if not self._warn_large_box_count(count):
                self.status_var.set("Deteksi dibatalkan karena jumlah kotak terlalu banyak.")
                self.set_busy(False)
                return
            self._push_undo()
            new_boxes = []
            for rec in result["boxes"]:
                rec = copy.deepcopy(rec)
                rec["uid"] = self.next_uid
                self.next_uid += 1
                new_boxes.append(rec)
            self.boxes = sort_boxes_reading(new_boxes)
            self.selected_uids.clear()
            self.label_map = result.get("label_map")
            self.auto_source_mode = result.get("source_mode")
            self.auto_background = result.get("background") or self.auto_background
            self.auto_background_warning = result.get("corner_spread", 0.0) > 50.0 and self.auto_source_mode == "WARNA"
            self.auto_discarded = int(result.get("discarded", 0))
            self.refresh_auto_controls()
            if self.auto_background_warning:
                messagebox.showwarning("Background tidak jelas", "Empat sudut gambar berbeda jauh. Tebakan background mungkin tidak tepat. Gunakan tombol 'Ambil warna background' untuk memilih background secara manual lalu jalankan deteksi lagi.", parent=self.root)
            self.refresh_list()
            if count == 0:
                self.status_var.set("Deteksi menghasilkan 0 kotak. Coba ubah toleransi, jarak gabung, atau periksa background.")
                messagebox.showwarning("Deteksi 0 kotak", "Tidak ada kotak yang ditemukan. Coba naikkan/turunkan toleransi, ubah jarak gabung, atau cek warna background.", parent=self.root)
            else:
                self.status_var.set(f"Deteksi selesai: {count} kotak; {self.auto_discarded} kotak dibuang karena ukuran minimum.")
            self.schedule_autosave()
            if and_export and count > 0:
                self.set_busy(False)
                self.root.after(50, self.export_all)
                return
        except Exception as exc:
            self.handle_exception("Menerapkan hasil deteksi otomatis", exc)
        finally:
            if not and_export or count == 0:
                self.set_busy(False)

    def _worker_error(self, context: str, exc: BaseException) -> None:
        self.set_busy(False)
        self.handle_exception(context, exc)

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        cursor = "watch" if busy else ("tcross" if self.eyedropper_active else "crosshair")
        try:
            self.canvas.configure(cursor=cursor)
        except tk.TclError:
            pass

    def schedule_autosave(self) -> None:
        if not self.image_path or not self.original:
            return
        if self.autosave_after_id:
            try:
                self.root.after_cancel(self.autosave_after_id)
            except tk.TclError:
                pass
        self.autosave_after_id = self.root.after(1000, self.autosave_session)

    def _session_dict(self) -> Dict[str, Any]:
        st = self.image_path.stat() if self.image_path else None
        return {
            "image_path": str(self.image_path.resolve()) if self.image_path else "",
            "image_size": [int(st.st_size), int(st.st_mtime_ns)] if st else [],
            "saved_at": now_text(),
            "boxes": [
                {
                    "box": list(map(int, b["box"])),
                    "source": b.get("source", "manual"),
                    "cleanable": bool(b.get("cleanable", False)),
                    "component_ids": [int(v) for v in b.get("component_ids", [])],
                }
                for b in self.boxes
            ],
            "parameters": {
                "mode": self.mode_var.get(),
                "rows": self.rows_var.get(),
                "cols": self.cols_var.get(),
                "margin": self.margin_var.get(),
                "grid_gap": self.grid_gap_var.get(),
                "tolerance": int(self.tolerance_var.get()),
                "merge_distance": int(self.merge_distance_var.get()),
                "min_size": int(self.min_size_var.get()),
                "padding": int(self.padding_var.get()),
                "alpha_threshold": int(self.alpha_threshold_var.get()),
                "merge_contained": bool(self.merge_contained_var.get()),
                "clean_neighbors": bool(self.clean_neighbors_var.get()),
                "bg_override": list(self.auto_background) if self.auto_background else None,
                "output_dir": self.output_var.get(),
                "prefix": self.prefix_var.get(),
                "zoom": self.zoom,
                "pan_x": self.pan_x,
                "pan_y": self.pan_y,
            },
        }

    def autosave_session(self) -> None:
        self.autosave_after_id = None
        if not self.image_path or not self.original:
            return
        try:
            session_file, _ = session_paths(self.image_path)
            atomic_session_save(session_file, self._session_dict())
        except Exception as exc:
            log_error("Autosave sesi", exc)
            self.status_var.set("Peringatan: sesi otomatis gagal disimpan; cek log_error.txt.")

    def save_session_now(self) -> None:
        if not self.image_path or not self.original:
            return
        if self.autosave_after_id:
            try:
                self.root.after_cancel(self.autosave_after_id)
            except tk.TclError:
                pass
            self.autosave_after_id = None
        self.autosave_session()

    def _ensure_export_label_map(self) -> None:
        if not self.original or not any(b.get("source") == "auto" for b in self.boxes):
            return
        if self.label_map is not None:
            return
        try:
            result = detect_automatic(
                self.original,
                tolerance=int(self.tolerance_var.get()),
                merge_distance=int(self.merge_distance_var.get()),
                min_size=int(self.min_size_var.get()),
                padding=int(self.padding_var.get()),
                merge_contained=bool(self.merge_contained_var.get()),
                alpha_threshold=int(self.alpha_threshold_var.get()),
                bg_override=self.auto_background,
            )
            self.label_map = result.get("label_map")
        except Exception:
            pass

    def _ask_background_removal_option(self) -> Optional[Dict[str, Any]]:
        """Menampilkan dialog popup untuk memilih penanganan background:
        - {"remove_bg": True, "clean_holes": bool, "defringe": bool}: Menghapus background
        - {"remove_bg": False}: Mempertahankan background asli
        - None: Batal
        """
        dialog = tk.Toplevel(self.root)
        dialog.title("Opsi Pemisahan Gambar - SnapSlice")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        result: Dict[str, Optional[Dict[str, Any]]] = {"choice": None}

        main_frame = ttk.Frame(dialog, padding=(24, 20))
        main_frame.pack(fill="both", expand=True)

        lbl_title = ttk.Label(
            main_frame,
            text="Pilih Penanganan Background",
            font=("Segoe UI", 12, "bold"),
        )
        lbl_title.pack(anchor="w", pady=(0, 4))

        lbl_desc = ttk.Label(
            main_frame,
            text="Tentukan apakah latar belakang gambar yang dipisah akan dihapus\nmenjadi transparan atau dipertahankan seperti aslinya:",
            font=("Segoe UI", 9),
            justify="left",
        )
        lbl_desc.pack(anchor="w", pady=(0, 14))

        def choose(val: Optional[Dict[str, Any]]) -> None:
            result["choice"] = val
            dialog.destroy()

        # Opsi 1
        f1 = ttk.LabelFrame(main_frame, text="Pilihan 1: Hapus Background", padding=12)
        f1.pack(fill="x", pady=(0, 12))

        clean_holes_var = tk.BooleanVar(value=True)
        defringe_var = tk.BooleanVar(value=True)

        btn_remove = ttk.Button(
            f1,
            text="✂️ Hapus Background (Transparan PNG Bersih)",
            command=lambda: choose({
                "remove_bg": True,
                "clean_holes": clean_holes_var.get(),
                "defringe": defringe_var.get(),
            }),
        )
        btn_remove.pack(fill="x", pady=(0, 8))

        chk_holes = ttk.Checkbutton(
            f1,
            text="Bersihkan rongga/lubang di dalam objek (misal: huruf B, O, dsb.)",
            variable=clean_holes_var,
        )
        chk_holes.pack(anchor="w", pady=(0, 4))

        chk_defringe = ttk.Checkbutton(
            f1,
            text="Haluskan tepian objek & buang sisa lis/halo background (Anti-halo)",
            variable=defringe_var,
        )
        chk_defringe.pack(anchor="w")

        # Opsi 2
        f2 = ttk.LabelFrame(main_frame, text="Pilihan 2: Pertahankan Asli", padding=12)
        f2.pack(fill="x", pady=(0, 14))
        btn_keep = ttk.Button(
            f2,
            text="🖼️ Pertahankan Background Asli",
            command=lambda: choose({"remove_bg": False}),
        )
        btn_keep.pack(fill="x", pady=(0, 4))
        lbl_sub2 = ttk.Label(
            f2,
            text="Warna latar belakang asli tetap disimpan apa adanya tanpa diubah.",
            font=("Segoe UI", 8),
            foreground="#555555",
            justify="left",
        )
        lbl_sub2.pack(anchor="w")

        btn_cancel = ttk.Button(
            main_frame,
            text="Batal",
            command=lambda: choose(None),
        )
        btn_cancel.pack(anchor="e")

        dialog.protocol("WM_DELETE_WINDOW", lambda: choose(None))
        btn_remove.focus_set()

        dialog.update_idletasks()
        rw = self.root.winfo_width()
        rh = self.root.winfo_height()
        rx = self.root.winfo_rootx()
        ry = self.root.winfo_rooty()
        dw = dialog.winfo_reqwidth()
        dh = dialog.winfo_reqheight()
        x = max(0, rx + (rw - dw) // 2)
        y = max(0, ry + (rh - dh) // 2)
        dialog.geometry(f"+{x}+{y}")

        dialog.wait_window()
        return result["choice"]

    def _validate_export_setup(self) -> Tuple[Path, str]:
        if not self.original or not self.image_path:
            raise UserFacingError("Muat gambar terlebih dahulu.")
        if not self.boxes:
            raise UserFacingError("Tidak ada kotak untuk diekspor.")
        output_raw = self.output_var.get().strip()
        if not output_raw:
            raise UserFacingError("Folder output belum dipilih.")
        output = validate_output_dir(Path(clean_user_path(output_raw)))
        prefix = valid_prefix(self.prefix_var.get() or self.image_path.stem)
        if len(str(output)) > 260:
            raise PathTooLongError("Path folder output lebih dari 260 karakter.")
        return output, prefix

    def export_all(self) -> None:
        self.start_export(list(range(len(self.boxes))), "Ekspor Semua")

    def export_selected(self) -> None:
        indices = [int(i) for i in self.box_list.curselection()]
        self.start_export(indices, "Ekspor Terpilih")

    def start_export(self, indices: List[int], context: str) -> None:
        if self.busy:
            return
        try:
            output, prefix = self._validate_export_setup()
            if not indices:
                raise UserFacingError("Tidak ada kotak yang dipilih untuk diekspor.")
            if len(self.boxes) > MAX_BOXES_CONFIRM:
                if not messagebox.askyesno("Banyak kotak", f"Ada {len(self.boxes)} kotak. Ekspor dapat menghasilkan banyak file. Lanjutkan?", parent=self.root):
                    return

            bg_choice = self._ask_background_removal_option()
            if bg_choice is None:
                return
            remove_bg = bool(bg_choice.get("remove_bg", False))
            clean_holes = bool(bg_choice.get("clean_holes", True))
            defringe = bool(bg_choice.get("defringe", True))

            if remove_bg:
                self._ensure_export_label_map()

            self.save_session_now()
            if not output.exists():
                try:
                    output.mkdir(parents=True, exist_ok=True)
                except PermissionError as exc:
                    raise UserFacingError(f"Folder output tidak bisa dibuat:\n{output}\nPilih folder lain.") from exc
            source_image = self.original.copy()
            boxes_snapshot = copy.deepcopy(self.boxes)
            label_map = None if self.label_map is None else self.label_map.copy()
            clean_neighbors = bool(self.clean_neighbors_var.get())
            bg_color = self.auto_background
            tolerance = int(self.tolerance_var.get())
        except Exception as exc:
            if isinstance(exc, UserFacingError):
                self.show_user_error(str(exc), context, exc)
            else:
                self.handle_exception(context, exc)
            return

        self.progress.configure(value=0, maximum=100)
        self.set_busy(True)

        def progress_cb(done: int, total: int) -> None:
            percent = (done / total) * 100 if total else 0
            self.root.after(0, lambda p=percent: self.progress.configure(value=p))

        def worker() -> None:
            try:
                written = export_boxes_pure(
                    source_image,
                    boxes_snapshot,
                    indices,
                    output,
                    prefix,
                    label_map=label_map,
                    clean_neighbors=clean_neighbors,
                    progress_cb=progress_cb,
                    remove_bg=remove_bg,
                    bg_color=bg_color,
                    tolerance=tolerance,
                    clean_holes=clean_holes,
                    defringe=defringe,
                )
                self.root.after(0, lambda w=written: self._export_done(w, output))
            except Exception as exc:
                self.root.after(0, lambda e=exc: self._worker_error(context, e))

        threading.Thread(target=worker, daemon=True).start()

    def _export_done(self, written: List[Path], output: Path) -> None:
        self.set_busy(False)
        self.progress.configure(value=100)
        self.status_var.set(f"Ekspor selesai: {len(written)} file. Folder: {output}")
        if messagebox.askyesno("Ekspor selesai", f"Berhasil menyimpan {len(written)} file PNG.\n\nBuka folder output sekarang?", parent=self.root):
            try:
                os.startfile(str(output))
            except Exception as exc:
                self.handle_exception("Membuka folder output", exc)

    def set_zoom(self, new_zoom: float, cursor_x: float, cursor_y: float) -> None:
        if not self.original:
            return
        old_zoom = self.zoom
        new_zoom = max(self.min_zoom, min(self.max_zoom, float(new_zoom)))
        if abs(new_zoom - old_zoom) < 1e-9:
            return
        ix, iy = canvas_to_image(cursor_x, cursor_y, old_zoom, self.pan_x, self.pan_y)
        self.zoom = new_zoom
        self.pan_x = ix * new_zoom - cursor_x
        self.pan_y = iy * new_zoom - cursor_y
        self._clamp_pan()
        self.render()
        self.schedule_autosave()

    def adjust_zoom_factor(self, factor: float) -> None:
        self.set_zoom(self.zoom * factor, self.viewport_w / 2, self.viewport_h / 2)

    def fit_to_window(self) -> None:
        if not self.original:
            return
        W, H = self.original.size
        self.zoom = max(self.min_zoom, min(self.max_zoom, min(self.viewport_w / W, self.viewport_h / H)))
        self.pan_x = self.pan_y = 0.0
        self.render()
        self.schedule_autosave()

    def help_short_text(self) -> str:
        return (
            "SnapSlice — Precision Image Splitter\n\n"
            "Navigasi & Tampilan:\n"
            "- Tool Geser / Hand (H): tahan & drag tombol kiri mouse untuk menggeser tampilan gambar.\n"
            "- Tool Kotak / Seleksi (V): drag untuk membuat, memindahkan, atau mengubah ukuran kotak.\n"
            "- Slider Zoom: atur perbesaran gambar (10% - 500%), tombol 🔍− / 🔍＋, 100%, atau ⊡ Pas Layar.\n"
            "- Space + drag / Klik tengah = geser gambar instan kapan saja.\n"
            "- Ctrl+Roda = zoom; Roda = scroll V; Shift+Roda = scroll H.\n\n"
            "Eksekusi & Shortcut:\n"
            "- ⚡ Eksekusi Pecah: tombol di panel Otomatis / Grid / bilah bawah untuk memotong & simpan PNG.\n"
            "- Delete = hapus terpilih; Ctrl+A = pilih semua; Esc = batal pilih.\n"
            "- Ctrl+Z / Ctrl+Y = undo / redo (30 langkah).\n"
            "- Panah = geser 1 px; Shift+panah = geser 10 px.\n"
            "- F1 = bantuan ini."
        )

    def show_help(self) -> None:
        messagebox.showinfo("Bantuan singkat", self.help_short_text(), parent=self.root)

    def show_user_error(self, message: str, context: str, exc: Optional[BaseException] = None) -> None:
        if exc:
            log_error(context, exc)
        messagebox.showerror("Kesalahan", message, parent=self.root)
        self.status_var.set(f"Kesalahan: {message.splitlines()[0][:160]}")

    def handle_exception(self, context: str, exc: BaseException) -> None:
        log_error(context, exc)
        message = f"Terjadi kesalahan pada {context}.\n\n{exc}\n\nDetail lengkap ditulis ke log_error.txt."
        try:
            messagebox.showerror("Kesalahan", message, parent=self.root)
            self.status_var.set(f"Kesalahan pada {context}; detail ada di log_error.txt")
        except Exception:
            pass

    def _update_title(self) -> None:
        name = self.image_path.name if self.image_path else "-"
        self.root.title(f"SnapSlice — Precision Image Splitter | {name}")

    def report_callback_exception(self, exc, val, tb) -> None:
        err = val.with_traceback(tb)
        self.handle_exception("callback Tkinter", err)

    def close(self) -> None:
        try:
            self.save_session_now()
            save_settings(Path(self.image_path.parent) if self.image_path else Path(self.settings.get("last_folder", Path.home())))
        except Exception as exc:
            log_error("Menutup aplikasi", exc)
        self.root.destroy()


def set_dpi_awareness() -> None:
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def run_selftest() -> int:
    print("SnapSlice --selftest")
    failed: List[str] = []
    temp_root = Path(tempfile.mkdtemp(prefix="ykan_selftest_"))
    try:
        # Uji path: spasi, non-ASCII, kutip.
        path_dir = temp_root / "folder dengan spasi" / "uji_é"
        path_dir.mkdir(parents=True, exist_ok=True)
        opaque_path = path_dir / "Kolase putih 3x3.png"
        alpha_path = path_dir / "Kolase transparan 3x3.png"

        W, H = 900, 900
        opaque = Image.new("RGB", (W, H), "white")
        alpha = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw_specs = []
        for r in range(3):
            for c in range(3):
                x1 = 80 + c * 250
                y1 = 80 + r * 250
                x2 = x1 + 100
                y2 = y1 + 100
                color = (40 + c * 70, 80 + r * 50, 200 - c * 40)
                opaque.paste(color, (x1, y1, x2, y2))
                alpha.paste(color + (255,), (x1, y1, x2, y2))
                draw_specs.append((x1, y1, x2, y2))
        opaque.save(opaque_path)
        alpha.save(alpha_path)

        qpath = normalize_image_path(f'  "{opaque_path}"  ', base_dir=APP_DIR)
        if qpath != opaque_path.resolve():
            failed.append("normalisasi path kutip/spasi/non-ASCII")

        o = Image.open(opaque_path)
        a = Image.open(alpha_path)
        try:
            res_o = detect_automatic(o, tolerance=30, merge_distance=2, min_size=16, padding=2, merge_contained=True)
            res_a = detect_automatic(a, tolerance=30, merge_distance=2, min_size=16, padding=2, merge_contained=True, alpha_threshold=128)
            if len(res_o["boxes"]) != 9:
                failed.append(f"otomatis background putih: expected 9 got {len(res_o['boxes'])}")
            if len(res_a["boxes"]) != 9:
                failed.append(f"otomatis transparan: expected 9 got {len(res_a['boxes'])}")
        finally:
            o.close()
            a.close()

        grid = generate_grid((1200, 900), 3, 4, 0, 0)
        if len(grid) != 12:
            failed.append("grid 3x4 jumlah bukan 12")
        else:
            if len(set(grid)) != 12 or any(box_area(b) <= 0 for b in grid):
                failed.append("grid memiliki kotak invalid")
            covered = sum(box_area(b) for b in grid)
            if covered != 1200 * 900:
                failed.append("grid tanpa gap tidak menutup seluruh area")
            if grid != sorted(grid, key=lambda b: (b[1], b[0])):
                failed.append("grid tidak berurutan baca")

        # Uji roundtrip koordinat pada zoom/pan yang diminta.
        for zoom in (0.5, 1.0, 3.0):
            for pan_x, pan_y in ((0, 0), (123.5, 77.25), (912.75, 441.5)):
                for xy in ((0, 0), (1, 2), (511, 299), (899, 899)):
                    if roundtrip_coordinate(xy[0], xy[1], zoom, pan_x, pan_y) != xy:
                        failed.append(f"roundtrip koordinat gagal {zoom} {pan_x} {pan_y} {xy}")
                        break

        # Uji ekspor lossless + tidak menimpa.
        src = Image.open(alpha_path)
        out = temp_root / "hasil ekspor"
        before_hash = hash_file(alpha_path)
        temp_boxes = [
            {"box": draw_specs[0], "uid": 1, "source": "manual", "cleanable": False, "component_ids": []},
            {"box": draw_specs[1], "uid": 2, "source": "manual", "cleanable": False, "component_ids": []},
        ]
        written1 = export_boxes_pure(src, temp_boxes, [0, 1], out, "ikon")
        written2 = export_boxes_pure(src, temp_boxes, [0, 1], out, "ikon")
        if len(written1) != 2 or len(written2) != 2:
            failed.append("ekspor dua kali bukan 4 file")
        if not all(p.exists() for p in written1 + written2):
            failed.append("file ekspor tidak ditemukan")
        if written2[0].name != "ikon_001_2.png" or written2[1].name != "ikon_002_2.png":
            failed.append("suffix ekspor _2 tidak benar")
        crop_check = Image.open(written1[0])
        try:
            if crop_check.size != (100, 100) or crop_check.mode != "RGBA":
                failed.append("crop mempertahankan size/mode alpha")
        finally:
            crop_check.close()
        src.close()
        if hash_file(alpha_path) != before_hash:
            failed.append("gambar asli berubah setelah ekspor")

        # Uji pembersihan alpha: objek tetangga dalam crop harus dibuat transparan.
        clean_img = Image.new("RGBA", (120, 60), (0, 0, 0, 0))
        clean_arr = np.array(clean_img)
        clean_arr[10:50, 10:50] = [255, 0, 0, 255]
        clean_arr[10:50, 60:100] = [0, 255, 0, 255]
        clean_img = Image.fromarray(clean_arr, "RGBA")
        clean_res = detect_automatic(clean_img, merge_distance=2, min_size=1, padding=0, alpha_threshold=128)
        if len(clean_res["boxes"]) != 2:
            failed.append(f"alpha cleanup: expected 2 got {len(clean_res['boxes'])}")
        else:
            clean_box = {
                "box": (8, 8, 102, 52),
                "uid": 1,
                "source": "auto",
                "cleanable": True,
                "component_ids": clean_res["boxes"][0]["component_ids"],
            }
            clean_out = temp_root / "clean"
            clean_written = export_boxes_pure(clean_img, [clean_box], [0], clean_out, "clean", clean_res["label_map"], True)
            clean_export = np.array(Image.open(clean_written[0]))
            if int(clean_export[:, 55:, 3].max()) != 0:
                failed.append("alpha cleanup tetangga tidak menjadi alpha=0")

        # Uji fitur Hapus Background (remove_bg=True vs remove_bg=False)
        src_opaque = Image.open(opaque_path)
        try:
            out_keep = temp_root / "test_keep_bg"
            out_nobg = temp_root / "test_remove_bg"
            box_test = [
                {
                    "box": (70, 70, 190, 190),
                    "uid": 1,
                    "source": "auto",
                    "cleanable": False,
                    "component_ids": [1],
                }
            ]
            # 1. Pertahankan background (remove_bg=False)
            w_keep = export_boxes_pure(src_opaque, box_test, [0], out_keep, "keep", remove_bg=False)
            img_k = Image.open(w_keep[0])
            arr_k = np.array(img_k)
            if tuple(arr_k[0, 0, :3]) != (255, 255, 255):
                failed.append("remove_bg=False tidak mempertahankan background putih")
            img_k.close()

            # 2. Hapus background (remove_bg=True)
            w_nobg = export_boxes_pure(src_opaque, box_test, [0], out_nobg, "nobg", remove_bg=True, bg_color=(255, 255, 255))
            img_nb = Image.open(w_nobg[0])
            arr_nb = np.array(img_nb)
            if img_nb.mode != "RGBA":
                failed.append("remove_bg=True tidak menghasilkan mode RGBA")
            elif arr_nb[0, 0, 3] != 0:
                failed.append(f"remove_bg=True sudut tidak transparan: alpha={arr_nb[0, 0, 3]}")
            elif arr_nb[arr_nb.shape[0] // 2, arr_nb.shape[1] // 2, 3] != 255:
                failed.append("remove_bg=True objek tengah tidak opaque (alpha != 255)")
            img_nb.close()
        finally:
            src_opaque.close()

        # Uji prefix invalid dan grid invalid untuk jalur error pure.
        try:
            valid_prefix("a:b")
            failed.append("prefix ilegal tidak ditolak")
        except UserFacingError:
            pass
        try:
            generate_grid((1200, 900), 0, 4, 0, 0)
            failed.append("grid rows=0 tidak ditolak")
        except UserFacingError:
            pass

    except Exception as exc:
        failed.append(f"exception selftest: {type(exc).__name__}: {exc}")
        log_error("Selftest", exc)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    if failed:
        print("GAGAL")
        for item in failed:
            print("-", item)
        return 1
    print("LULUS")
    print("9/9 objek terdeteksi pada background putih dan transparan.")
    print("Grid 3x4, roundtrip koordinat, ekspor (pertahankan & hapus background), dan hash gambar asli lulus.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="YKAN Manual Reviewer & Image Splitter")
    parser.add_argument("--selftest", action="store_true", help="Jalankan uji fungsi inti tanpa GUI")
    args = parser.parse_args()
    if args.selftest:
        return run_selftest()
    set_dpi_awareness()
    try:
        root = tk.Tk()
    except Exception as exc:
        log_error("Inisialisasi Tkinter", exc)
        print("Gagal memulai Tkinter. Pastikan Python terpasang lengkap dengan Tcl/Tk.")
        return 1
    app = BoxEditorApp(root)
    root.report_callback_exception = app.report_callback_exception
    root.protocol("WM_DELETE_WINDOW", app.close)
    try:
        root.mainloop()
        return 0
    except Exception as exc:
        log_error("Mainloop aplikasi", exc)
        try:
            messagebox.showerror("Kesalahan", "Aplikasi mengalami kesalahan tak terduga. Detail ada di log_error.txt.", parent=root)
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    try:
        raise_system_code = main()
    except BaseException as exc:
        log_error("Main exception", exc)
        print(f"Kesalahan fatal: {exc}")
        raise_system_code = 1
    raise SystemExit(raise_system_code)
