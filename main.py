import asyncio
import cv2
import numpy as np
import os
import uuid
import time
import gc
import logging
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, File, Form, UploadFile, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="SLAP — Stack Like A Pro")

# ─── Directories ──────────────────────────────────────────────────────────────

UPLOAD_DIR = Path("/tmp/slap_uploads")
RESULT_DIR = Path("/tmp/slap_results")
UPLOAD_DIR.mkdir(exist_ok=True)
RESULT_DIR.mkdir(exist_ok=True)

# ─── Config ───────────────────────────────────────────────────────────────────

MAX_FILES = 30
MAX_FILE_SIZE_MB = 15
MAX_LONG_SIDE = 2048
RESULT_TTL_SECONDS = 15 * 60  # 15 minutes
RATE_LIMIT_REQUESTS = 5
RATE_LIMIT_WINDOW = 3600  # per hour

# ─── State ────────────────────────────────────────────────────────────────────

jobs: dict = {}
job_queue: asyncio.Queue = None
executor = ThreadPoolExecutor(max_workers=1)
rate_limit_store: dict = {}


class Job:
    def __init__(self, job_id: str, file_paths: list, fmt: str = "jpeg"):
        self.job_id = job_id
        self.file_paths = file_paths
        self.state = "queued"          # queued | processing | done | error
        self.progress = 0              # 0–100
        self.status_text = "В очереди..."
        self.queue_position = 0
        self.fmt = fmt  # "jpeg" | "png"
        self.result_path: Optional[str] = None
        self.error: Optional[str] = None
        self.created_at = time.time()
        self.completed_at: Optional[float] = None


# ─── Startup / background tasks ───────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    global job_queue
    job_queue = asyncio.Queue()
    asyncio.create_task(queue_worker())
    asyncio.create_task(cleanup_worker())
    logger.info("SLAP started")


async def queue_worker():
    while True:
        job_id = await job_queue.get()
        job = jobs.get(job_id)
        if not job:
            job_queue.task_done()
            continue

        _update_queue_positions()
        job.state = "processing"
        job.progress = 5
        job.status_text = "Загрузка изображений..."

        loop = asyncio.get_event_loop()
        try:
            result_path = await loop.run_in_executor(executor, process_stack, job)
            job.state = "done"
            job.result_path = result_path
            job.progress = 100
            job.status_text = "Готово!"
            job.completed_at = time.time()
        except Exception as e:
            logger.exception(f"Job {job_id} failed")
            job.state = "error"
            job.error = str(e)
            job.status_text = f"Ошибка: {e}"
        finally:
            for path in job.file_paths:
                try:
                    os.remove(path)
                except Exception:
                    pass
            try:
                upload_dir = UPLOAD_DIR / job_id
                if upload_dir.exists():
                    import shutil
                    shutil.rmtree(upload_dir, ignore_errors=True)
            except Exception:
                pass
            job_queue.task_done()


def _update_queue_positions():
    pos = 0
    for j in jobs.values():
        if j.state == "queued":
            j.queue_position = pos
            pos += 1


async def cleanup_worker():
    while True:
        await asyncio.sleep(60)
        now = time.time()
        to_delete = [
            jid for jid, j in jobs.items()
            if (j.completed_at and now - j.completed_at > RESULT_TTL_SECONDS)
            or (j.state == "error" and now - j.created_at > RESULT_TTL_SECONDS)
        ]
        for jid in to_delete:
            job = jobs.pop(jid, None)
            if job and job.result_path:
                try:
                    os.remove(job.result_path)
                except Exception:
                    pass


# ─── Rate limiting ────────────────────────────────────────────────────────────

def check_rate_limit(ip: str) -> bool:
    now = time.time()
    ts = [t for t in rate_limit_store.get(ip, []) if now - t < RATE_LIMIT_WINDOW]
    if len(ts) >= RATE_LIMIT_REQUESTS:
        return False
    ts.append(now)
    rate_limit_store[ip] = ts
    return True


# ─── API endpoints ────────────────────────────────────────────────────────────

@app.post("/api/upload")
async def upload(request: Request, files: list[UploadFile] = File(...), fmt: str = Form("jpeg")):
    ip = request.client.host
    if not check_rate_limit(ip):
        raise HTTPException(429, "Слишком много запросов. Попробуйте через час.")
    if len(files) < 2:
        raise HTTPException(400, "Нужно минимум 2 изображения.")
    if len(files) > MAX_FILES:
        raise HTTPException(400, f"Максимум {MAX_FILES} файлов за один раз.")

    job_id = str(uuid.uuid4())
    job_dir = UPLOAD_DIR / job_id
    job_dir.mkdir(exist_ok=True)

    file_paths = []
    for i, file in enumerate(files):
        if file.content_type not in ("image/jpeg", "image/jpg", "image/png"):
            raise HTTPException(400, f'Файл "{file.filename}" должен быть JPEG или PNG.')
        content = await file.read()
        if len(content) > MAX_FILE_SIZE_MB * 1024 * 1024:
            raise HTTPException(400, f'Файл "{file.filename}" превышает {MAX_FILE_SIZE_MB} МБ.')
        path = str(job_dir / f"{i:03d}.jpg")
        with open(path, "wb") as f:
            f.write(content)
        file_paths.append(path)

    fmt = fmt if fmt in ("jpeg", "png") else "jpeg"
    job = Job(job_id, file_paths, fmt)
    queued_count = sum(1 for j in jobs.values() if j.state == "queued")
    job.queue_position = queued_count
    if queued_count > 0:
        job.status_text = f"В очереди: #{queued_count + 1}"

    jobs[job_id] = job
    await job_queue.put(job_id)

    return {"job_id": job_id}


@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Задача не найдена или устарела.")

    resp = {
        "state": job.state,
        "progress": job.progress,
        "status_text": job.status_text,
    }
    if job.state == "queued" and job.queue_position > 0:
        resp["queue_position"] = job.queue_position + 1
    if job.state == "done":
        resp["result_url"] = f"/api/result/{job_id}"
    if job.state == "error":
        resp["error"] = job.error
    return resp


@app.get("/api/result/{job_id}")
async def get_result(job_id: str):
    job = jobs.get(job_id)
    if not job or job.state != "done" or not job.result_path:
        raise HTTPException(404, "Результат не найден.")
    if not os.path.exists(job.result_path):
        raise HTTPException(404, "Файл результата уже удалён.")
    ext = "png" if job.fmt == "png" else "jpg"
    mime = "image/png" if job.fmt == "png" else "image/jpeg"
    return FileResponse(
        job.result_path,
        media_type=mime,
        filename=f"slap_{job_id[:8]}.{ext}",
    )


# ─── Image processing pipeline ────────────────────────────────────────────────

def resize_to_max(img: np.ndarray, max_side: int) -> np.ndarray:
    h, w = img.shape[:2]
    if max(h, w) <= max_side:
        return img
    scale = max_side / max(h, w)
    return cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)


def compute_sharpness(gray: np.ndarray, blur_radius: int = 21) -> np.ndarray:
    """Tenengrad focus measure (Sobel-based)."""
    gx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
    magnitude = gx ** 2 + gy ** 2
    threshold = np.mean(magnitude) * 0.1
    magnitude[magnitude < threshold] = 0
    r = blur_radius | 1  # ensure odd
    magnitude = cv2.GaussianBlur(magnitude, (r, r), 0)
    return magnitude


def align_images(images: list, job: Job) -> tuple:
    """Align all images to the first frame using ECC (affine)."""
    reference = images[0]
    h, w = reference.shape[:2]
    ref_gray = cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY).astype(np.float32)

    # Coarse alignment at reduced resolution
    scale = min(1.0, 1536 / max(h, w))
    ref_small = cv2.resize(ref_gray, None, fx=scale, fy=scale)
    del ref_gray  # освобождаем — больше не нужна

    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 50, 1e-4)
    aligned = [reference]
    transforms = [np.eye(2, 3, dtype=np.float32)]

    for i, img in enumerate(images[1:], 1):
        job.status_text = f"Выравнивание кадра {i} из {len(images) - 1}..."
        job.progress = 5 + int(30 * i / (len(images) - 1))

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
        gray_small = cv2.resize(gray, None, fx=scale, fy=scale)
        del gray  # освобождаем сразу после resize

        warp = np.eye(2, 3, dtype=np.float32)
        try:
            _, warp = cv2.findTransformECC(ref_small, gray_small, warp, cv2.MOTION_AFFINE, criteria)
            warp[0, 2] /= scale
            warp[1, 2] /= scale
        except cv2.error:
            logger.warning(f"ECC failed for frame {i}, using identity")
        del gray_small

        warped = cv2.warpAffine(
            img, warp, (w, h),
            flags=cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        aligned.append(warped)
        transforms.append(warp)
        gc.collect()

    del ref_small
    return aligned, transforms


def match_exposure(aligned: list) -> list:
    """
    Выравнивает яркость/цвет каждого кадра относительно первого (референсного).
    Убирает видимые "швы" между донорскими зонами разных кадров в гладких
    областях (боке), где микроразличия экспозиции иначе становятся заметны.
    """
    reference = aligned[0]
    # Средняя яркость по каждому каналу референса (используем всё изображение —
    # после ECC-выравнивания края почти совпадают, крайние случаи погоды не делают)
    ref_mean = reference.reshape(-1, 3).mean(axis=0).astype(np.float32)

    result = [reference]
    for img in aligned[1:]:
        img_mean = img.reshape(-1, 3).mean(axis=0).astype(np.float32)
        # Избегаем деления на ноль и экстремальных коррекций (клэмп 0.85–1.15)
        gain = np.clip(ref_mean / np.maximum(img_mean, 1.0), 0.85, 1.15)
        corrected = np.clip(img.astype(np.float32) * gain, 0, 255).astype(np.uint8)
        result.append(corrected)

    return result


def compute_valid_area(shape: tuple, transforms: list) -> tuple:
    """Find the intersection rectangle of all aligned frames using masks."""
    h, w = shape[:2]
    white = np.ones((h, w), dtype=np.uint8) * 255
    combined = white.copy()

    for warp in transforms[1:]:
        mask = cv2.warpAffine(
            white, warp, (w, h),
            flags=cv2.INTER_NEAREST + cv2.WARP_INVERSE_MAP,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        combined = cv2.bitwise_and(combined, mask)

    coords = cv2.findNonZero(combined)
    if coords is None:
        margin = 50
        return margin, margin, w - margin, h - margin

    x, y, rw, rh = cv2.boundingRect(coords)
    margin = 5
    return (
        max(0, x + margin),
        max(0, y + margin),
        min(w, x + rw - margin),
        min(h, y + rh - margin),
    )


def denoise_topology(topology: np.ndarray) -> np.ndarray:
    """Remove outliers: single-pixel neighbour check + median blur for smooth zones."""
    result = topology.copy()

    # Шаг 1: одиночные выбросы (4-сосед)
    c = topology[1:-1, 1:-1]
    neighbours = np.stack([
        topology[1:-1, :-2],
        topology[1:-1, 2:],
        topology[:-2, 1:-1],
        topology[2:, 1:-1],
    ])
    all_differ = np.all(neighbours != c, axis=0)
    median_n = np.median(neighbours, axis=0).astype(np.int32)
    result[1:-1, 1:-1] = np.where(all_differ, median_n, c)

    # Шаг 2: медианный фильтр 7x7 — сглаживает "острова" в боке-зонах
    topo_u8 = result.astype(np.uint8)
    topo_u8 = cv2.medianBlur(topo_u8, 7)
    result = topo_u8.astype(np.int32)

    return result


def process_stack(job: Job) -> str:
    """Full stacking pipeline: load → resize → align → focusmeasure → merge → crop → save."""

    # 1. Load + resize (читаем по одному, не храним оригиналы)
    job.status_text = "Загрузка изображений..."
    job.progress = 5
    images = []
    for path in job.file_paths:
        img = cv2.imread(path)
        if img is None:
            raise ValueError(f"Не удалось прочитать: {os.path.basename(path)}")
        img = resize_to_max(img, MAX_LONG_SIDE)
        images.append(img)
    gc.collect()

    # 2. Align
    aligned, transforms = align_images(images, job)
    del images  # оригиналы больше не нужны — освобождаем
    gc.collect()

    # 2b. Match exposure — убираем видимые швы между кадрами в гладких зонах
    job.status_text = "Выравнивание экспозиции..."
    job.progress = 38
    aligned = match_exposure(aligned)
    gc.collect()

    # 3. Focus measure + topology
    # best_sharpness храним как float32 вместо float64 — вдвое меньше памяти
    h, w = aligned[0].shape[:2]
    best_sharpness = np.full((h, w), -np.inf, dtype=np.float32)
    topology = np.zeros((h, w), dtype=np.int32)
    result = np.zeros_like(aligned[0])

    for i, img in enumerate(aligned):
        job.status_text = f"Слияние: кадр {i + 1} из {len(aligned)}..."
        job.progress = 40 + int(38 * (i + 1) / len(aligned))
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
        sharpness = compute_sharpness(gray).astype(np.float32)
        del gray
        mask = sharpness > best_sharpness
        best_sharpness[mask] = sharpness[mask]
        del sharpness
        topology[mask] = i
        result[mask] = img[mask]
        del mask
        gc.collect()

    del best_sharpness
    gc.collect()

    # 4. Denoise topology
    job.status_text = "Сглаживание маски..."
    job.progress = 82
    topology = denoise_topology(topology)
    for i, img in enumerate(aligned):
        m = topology == i
        result[m] = img[m]
        del m

    del topology
    del aligned
    gc.collect()

    # 5. Crop valid area
    job.status_text = "Обрезка краёв..."
    job.progress = 90
    x1, y1, x2, y2 = compute_valid_area(result.shape, transforms)
    result = result[y1:y2, x1:x2].copy()  # .copy() освобождает ссылку на большой массив
    gc.collect()

    # 6. Save
    job.status_text = "Сохранение результата..."
    job.progress = 96
    if job.fmt == "png":
        result_path = str(RESULT_DIR / f"{job.job_id}.png")
        cv2.imwrite(result_path, result)
    else:
        result_path = str(RESULT_DIR / f"{job.job_id}.jpg")
        cv2.imwrite(result_path, result, [
            cv2.IMWRITE_JPEG_QUALITY, 98,
            cv2.IMWRITE_JPEG_SAMPLING_FACTOR, cv2.IMWRITE_JPEG_SAMPLING_FACTOR_444,
        ])

    del result
    gc.collect()
    return result_path


# ─── Health check (keep-alive for Render free tier) ──────────────────────────

@app.get("/health")
@app.head("/health")
@app.get("/ping")
async def health():
    return {"status": "ok", "service": "slap-web", "jobs": len(jobs)}


# ─── Static files (must be last) ─────────────────────────────────────────────
app.mount("/", StaticFiles(directory="static", html=True), name="static")
