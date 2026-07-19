import asyncio
import cv2
import numpy as np
import os
import uuid
import time
import gc
import logging
import pywt
from PIL import Image
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
MAX_FILE_SIZE_MB = 40  # промежуточные файлы теперь PNG (без потерь), весят больше JPEG
MAX_LONG_SIDE = 2048
RESULT_TTL_SECONDS = 15 * 60  # 15 minutes
RATE_LIMIT_REQUESTS = 5
RATE_LIMIT_WINDOW = 3600  # per hour

# Названия методов для EXIF-метаданных результата (ASCII — для совместимости
# со всеми просмотрщиками; полные однословные имена — Snap/Weave/Prism)
METHOD_LABELS = {
    "sharp": "Snap (Pixel Select)",
    "wavelet": "Weave (Wavelet Fusion)",
    "hybrid": "Prism (Hybrid Blend)",
}

# ─── State ────────────────────────────────────────────────────────────────────

jobs: dict = {}
job_queue: asyncio.Queue = None
executor = ThreadPoolExecutor(max_workers=1)
rate_limit_store: dict = {}


class Job:
    def __init__(self, job_id: str, file_paths: list, fmt: str = "jpeg", method: str = "sharp", fix_motion: bool = False):
        self.job_id = job_id
        self.file_paths = file_paths
        self.state = "queued"          # queued | processing | done | error
        self.progress = 0              # 0–100
        self.status_text = "В очереди..."
        self.queue_position = 0
        self.fmt = fmt              # "jpeg" | "png"
        self.method = method        # "sharp" | "wavelet" | "hybrid"
        self.fix_motion = fix_motion  # устранять "призраки" от движения объекта
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
async def upload(request: Request, files: list[UploadFile] = File(...), fmt: str = Form("jpeg"), method: str = Form("sharp"), fix_motion: bool = Form(False)):
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
            raise HTTPException(400, f'Подготовленный файл "{file.filename}" превышает {MAX_FILE_SIZE_MB} МБ после ресайза. Попробуйте уменьшить количество кадров.')
        path = str(job_dir / f"{i:03d}.jpg")
        with open(path, "wb") as f:
            f.write(content)
        file_paths.append(path)

    fmt = fmt if fmt in ("jpeg", "png") else "jpeg"
    method = method if method in ("sharp", "wavelet", "hybrid") else "sharp"
    job = Job(job_id, file_paths, fmt, method, fix_motion)
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
    mime = "image/png" if job.fmt == "png" else "image/jpeg"
    return FileResponse(
        job.result_path,
        media_type=mime,
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


def orb_affine_estimate(ref_gray: np.ndarray, gray: np.ndarray, min_matches: int = 12):
    """
    Пытается найти аффинное преобразование через особые точки (ORB + RANSAC).
    Устойчив к большим сдвигам/поворотам между кадрами (в отличие от ECC,
    которому нужен разумный начальный сдвиг для сходимости).

    Возвращает (warp, inlier_count) или (None, matches_count) при неудаче.
    """
    orb = cv2.ORB_create(nfeatures=3000)
    kp1, des1 = orb.detectAndCompute(ref_gray, None)
    kp2, des2 = orb.detectAndCompute(gray, None)

    if des1 is None or des2 is None or len(kp1) < min_matches or len(kp2) < min_matches:
        return None, 0

    bf = cv2.BFMatcher(cv2.NORM_HAMMING)
    raw_matches = bf.knnMatch(des1, des2, k=2)

    # Ratio test Лоу — оставляем только уверенные совпадения
    good = []
    for pair in raw_matches:
        if len(pair) == 2:
            m, n = pair
            if m.distance < 0.75 * n.distance:
                good.append(m)

    if len(good) < min_matches:
        return None, len(good)

    ref_pts = np.float32([kp1[m.queryIdx].pt for m in good])
    img_pts = np.float32([kp2[m.trainIdx].pt for m in good])

    # ref_pts -> img_pts: та же конвенция, что и ECC ниже (для WARP_INVERSE_MAP)
    warp, inlier_mask = cv2.estimateAffine2D(ref_pts, img_pts, method=cv2.RANSAC, ransacReprojThreshold=5.0)

    if warp is None:
        return None, len(good)

    inlier_count = int(inlier_mask.sum()) if inlier_mask is not None else 0
    if inlier_count < min_matches:
        return None, inlier_count

    return warp.astype(np.float32), inlier_count


def align_images(images: list, job: Job) -> tuple:
    """
    Выравнивает все кадры относительно первого. Для каждого кадра автоматически
    выбирается метод:
    1. ORB (особые точки) — пробуется первым, устойчив к большим сдвигам/поворотам
       (типично для съёмки с рук).
    2. ECC — запасной вариант, если ORB не нашёл достаточно надёжных совпадений
       (гладкие кадры без выраженной текстуры).

    Если оба метода не смогли выровнять кадр — обработка останавливается
    с понятной ошибкой (кадр не выбрасывается молча, чтобы не портить результат).
    """
    reference = images[0]
    h, w = reference.shape[:2]
    ref_gray = cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY)
    ref_gray_f32 = ref_gray.astype(np.float32)

    # Уменьшенная версия для ECC (для скорости)
    scale = min(1.0, 1536 / max(h, w))
    ref_small = cv2.resize(ref_gray_f32, None, fx=scale, fy=scale) if scale < 1.0 else ref_gray_f32
    del ref_gray_f32

    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 50, 1e-4)
    aligned = [reference]
    transforms = [np.eye(2, 3, dtype=np.float32)]

    for i, img in enumerate(images[1:], 1):
        job.status_text = f"Выравнивание кадра {i} из {len(images) - 1}..."
        job.progress = 5 + int(30 * i / (len(images) - 1))

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        warp = None
        method_used = None

        # 1. Пробуем ORB — устойчив к большим сдвигам/поворотам
        orb_warp, inliers = orb_affine_estimate(ref_gray, gray)
        if orb_warp is not None:
            warp = orb_warp
            method_used = "ORB"

        # 2. Fallback на ECC — точнее на гладких кадрах без выраженных особых точек
        if warp is None:
            gray_f32 = gray.astype(np.float32)
            gray_small = cv2.resize(gray_f32, None, fx=scale, fy=scale) if scale < 1.0 else gray_f32
            del gray_f32

            warp_ecc = np.eye(2, 3, dtype=np.float32)
            try:
                cc, warp_ecc = cv2.findTransformECC(ref_small, gray_small, warp_ecc, cv2.MOTION_AFFINE, criteria)
                if cc >= 0.5:  # низкий коэффициент корреляции — ECC не сошёлся к разумному результату
                    warp_ecc[0, 2] /= scale
                    warp_ecc[1, 2] /= scale
                    warp = warp_ecc
                    method_used = "ECC"
            except cv2.error:
                pass
            del gray_small

        if warp is None:
            raise ValueError(
                f"Не удалось совместить кадр {i + 1} с остальными — сдвиг между кадрами слишком велик "
                f"(ни ORB, ни ECC не справились). Попробуйте серию, снятую со штатива или с опорой, "
                f"без изменения композиции между кадрами."
            )

        job.status_text = f"Выравнивание кадра {i} из {len(images) - 1} ({method_used})..."

        warped = cv2.warpAffine(
            img, warp, (w, h),
            flags=cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        aligned.append(warped)
        transforms.append(warp)
        del gray
        gc.collect()

    del ref_small
    return aligned, transforms


def match_exposure(aligned: list, valid_area: tuple) -> list:
    """
    Выравнивает яркость/цвет каждого кадра относительно первого (референсного).
    Убирает видимые "швы" между донорскими зонами разных кадров в гладких
    областях (боке), где микроразличия экспозиции иначе становятся заметны.

    Средняя яркость считается только по пересекающейся (valid) области —
    если считать по всему кадру, чёрные каёмки после warpAffine на сильно
    сдвинутых кадрах искажают оценку и портят коррекцию.
    """
    x1, y1, x2, y2 = valid_area
    reference = aligned[0]
    ref_region = reference[y1:y2, x1:x2]
    ref_mean = ref_region.reshape(-1, 3).mean(axis=0).astype(np.float32)

    result = [reference]
    for img in aligned[1:]:
        img_region = img[y1:y2, x1:x2]
        img_mean = img_region.reshape(-1, 3).mean(axis=0).astype(np.float32)
        # Избегаем деления на ноль и экстремальных коррекций (клэмп 0.85–1.15)
        gain = np.clip(ref_mean / np.maximum(img_mean, 1.0), 0.85, 1.15)
        corrected = np.clip(img.astype(np.float32) * gain, 0, 255).astype(np.uint8)
        result.append(corrected)

    return result


def detect_motion_mask(aligned: list, low_sigma: float = 8.0, threshold_factor: float = 2.5) -> np.ndarray:
    """
    Находит зоны, где кадры расходятся по НИЗКОЧАСТОТНОМУ содержимому даже
    после глобального выравнивания — признак реального локального движения
    объекта (шевельнулась лапка/усик у живого насекомого), а не просто разной
    резкости/фокуса (которая влияет в основном на высокие частоты и является
    нормой для focus stacking).

    Возвращает бинарную маску (uint8, 0/255) проблемных зон.
    """
    low_freq_stack = []
    for img in aligned:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
        low = cv2.GaussianBlur(gray, (0, 0), sigmaX=low_sigma)
        low_freq_stack.append(low)
        del gray

    stack = np.stack(low_freq_stack)
    del low_freq_stack
    variance_map = stack.var(axis=0)
    del stack
    gc.collect()

    threshold = variance_map.mean() + threshold_factor * variance_map.std()
    mask = (variance_map > threshold).astype(np.uint8) * 255
    del variance_map

    # Убираем мелкий шум маски (одиночные пиксели) и заполняем небольшие дыры
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))

    return mask


def fix_motion_ghosting(result: np.ndarray, aligned: list, mask: np.ndarray) -> np.ndarray:
    """
    В зонах, отмеченных detect_motion_mask, заменяет смешанный результат на
    ОДИН цельный кадр (тот, что резче всего именно в этой зоне) — убирает
    "призрак"/раздвоение от локального движения объекта между кадрами.
    Края перехода смягчаются (feather), чтобы не было жёсткого шва.
    """
    n_labels, labels = cv2.connectedComponents(mask)
    if n_labels <= 1:
        return result  # проблемных зон не найдено

    sharpness_maps = []
    for img in aligned:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
        sharpness_maps.append(compute_sharpness(gray))
        del gray
    gc.collect()

    result_fixed = result.astype(np.float32)

    for label in range(1, n_labels):
        component_mask = (labels == label)
        if component_mask.sum() < 20:
            continue  # игнорируем совсем мелкие шумовые пятна

        # Кадр с максимальной суммарной резкостью именно в этой зоне
        scores = [sm[component_mask].sum() for sm in sharpness_maps]
        best_idx = int(np.argmax(scores))

        # Мягкие края перехода — растушёвка бинарной маски компонента
        soft = component_mask.astype(np.float32)
        soft = cv2.GaussianBlur(soft, (0, 0), sigmaX=2.0)
        alpha = soft[:, :, np.newaxis]

        result_fixed = alpha * aligned[best_idx].astype(np.float32) + (1 - alpha) * result_fixed
        del component_mask, soft, alpha

    del sharpness_maps
    gc.collect()
    return np.clip(result_fixed, 0, 255).astype(np.uint8)


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



def wavelet_merge(aligned: list, job: Job) -> np.ndarray:
    """
    Слияние в вейвлет-области (аналог Method C / focus-stack task_wavelet+task_merge).

    Для каждого канала (B, G, R):
    - многоуровневое вейвлет-разложение каждого кадра
    - коэффициенты детализации (края/текстуры) — выбираем по максимальной
      энергии между кадрами на каждом уровне и субполосе (аналог focus-stack)
    - коэффициенты аппроксимации (грубые/гладкие зоны — боке) — усредняем
      между кадрами, что естественно убирает "швы" от разной экспозиции

    Это корректно обрабатывает полупрозрачные наложения (крылья, блики на
    хитине), где простой "выбор одного пикселя" даёт видимые артефакты.
    """
    n = len(aligned)
    h, w = aligned[0].shape[:2]
    wavelet = "sym8"  # симлеты — меньше фазовых искажений на краях, чем у Добеши (db4)
    level = 4
    mode = "periodization"  # даёт предсказуемый размер коэффициентов для реконструкции

    result = np.zeros((h, w, 3), dtype=np.float32)

    for c in range(3):
        job.status_text = f"Вейвлет-слияние: канал {c + 1} из 3..."
        job.progress = 45 + int(35 * c / 3)

        # Разложение каждого кадра по текущему каналу
        coeffs_list = []
        for img in aligned:
            channel = img[:, :, c].astype(np.float32)
            coeffs_list.append(pywt.wavedec2(channel, wavelet, level=level, mode=mode))
        del channel
        gc.collect()

        merged = []

        # Аппроксимация (самый грубый уровень) — усредняем между кадрами
        cA_stack = np.stack([coeffs_list[i][0] for i in range(n)])
        merged.append(cA_stack.mean(axis=0))
        del cA_stack

        # Детализация на каждом уровне — плавное взвешенное смешивание вместо
        # жёсткого выбора "победителя". Резкое переключение между кадрами
        # само по себе создаёт звон (ringing) в частотной области — вес по
        # степени магнитуды даёт доминирующему кадру почти весь вклад, но
        # без разрывного скачка на границе, что и убирает рябь.
        n_levels = len(coeffs_list[0])
        power = 4.0  # чем выше — тем ближе к жёсткому выбору (но без разрыва)

        for lvl in range(1, n_levels):
            subbands = []
            for sub_idx in range(3):  # cH, cV, cD
                stack = np.stack([coeffs_list[i][lvl][sub_idx] for i in range(n)])
                magnitude = np.abs(stack)
                raw_weights = magnitude ** power

                # Пространственное сглаживание весов по каждому кадру — гасит
                # одиночные всплески/звон на очень контрастных тонких структурах
                # (аналог denoise_subbands из focus-stack, адаптированный под
                # непрерывное взвешивание вместо дискретного выбора)
                smoothed = np.empty_like(raw_weights)
                for f in range(n):
                    smoothed[f] = cv2.GaussianBlur(raw_weights[f], (0, 0), sigmaX=0.8)
                del raw_weights

                weights_sum = smoothed.sum(axis=0, keepdims=True)
                weights_sum = np.maximum(weights_sum, 1e-8)  # защита от деления на 0
                weights = smoothed / weights_sum
                chosen = (stack * weights).sum(axis=0)
                subbands.append(chosen)
                del stack, magnitude, smoothed, weights, weights_sum, chosen
            merged.append(tuple(subbands))
            gc.collect()

        del coeffs_list
        gc.collect()

        channel_result = pywt.waverec2(merged, wavelet, mode=mode)
        # periodization иногда даёт размер чуть больше исходного — обрезаем
        result[:, :, c] = channel_result[:h, :w]
        del merged, channel_result
        gc.collect()

    return np.clip(result, 0, 255).astype(np.uint8)


def sharp_merge(aligned: list) -> np.ndarray:
    """
    Классический метод: выбор пикселя с максимальной резкостью (Tenengrad),
    с денойзом topology-карты. Вынесено в отдельную функцию для переиспользования
    в process_stack и в hybrid_merge.
    """
    h, w = aligned[0].shape[:2]
    best_sharpness = np.full((h, w), -np.inf, dtype=np.float32)
    topology = np.zeros((h, w), dtype=np.int32)
    result = np.zeros_like(aligned[0])

    for i, img in enumerate(aligned):
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

    topology = denoise_topology(topology)
    for i, img in enumerate(aligned):
        m = topology == i
        result[m] = img[m]
        del m

    del topology
    gc.collect()
    return result


def compute_confidence_map(aligned: list) -> np.ndarray:
    """
    Карта уверенности "победителя" по резкости для каждого пикселя: нормализованная
    разница между лучшим и вторым по резкости кандидатом среди всех кадров.

    Большая разница (~1.0) — явный победитель, типичная резкая деталь без наложений.
    Малая разница (~0.0) — кандидаты близки, признак наложения/неоднозначности
    (полупрозрачные структуры), где вейвлет-слияние работает корректнее.
    """
    sharpness_stack = []
    for img in aligned:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
        sharpness_stack.append(compute_sharpness(gray))
        del gray

    stack = np.stack(sharpness_stack, axis=0)
    del sharpness_stack
    gc.collect()

    # np.sort по оси кадров даёт стабильный порядок без риска ошибок
    # индексации (в отличие от предыдущей ручной сортировки "пузырьком")
    sorted_desc = np.sort(stack, axis=0)[::-1]
    del stack
    best = sorted_desc[0]
    second = sorted_desc[1] if sorted_desc.shape[0] > 1 else sorted_desc[0]
    del sorted_desc
    gc.collect()

    eps = 1e-6
    confidence = (best - second) / (best + second + eps)
    confidence = np.clip(confidence, 0.0, 1.0).astype(np.float32)

    # Сглаживаем границы, чтобы переход между sharp/wavelet вкладом был плавным
    confidence = cv2.GaussianBlur(confidence, (0, 0), sigmaX=15)
    return confidence


def hybrid_merge(aligned: list, job: Job) -> np.ndarray:
    """
    Смешивает sharp и wavelet методы по карте уверенности:
    - высокая уверенность (явный победитель по резкости) → берём sharp-результат
    - низкая уверенность (наложение/неоднозначность) → берём wavelet-результат
    Граница между вкладами сглаживается, без резких швов.
    """
    job.status_text = "Гибрид: базовое слияние (sharp)..."
    job.progress = 40
    sharp_result = sharp_merge(aligned)
    gc.collect()

    job.status_text = "Гибрид: вейвлет-слияние..."
    job.progress = 55
    wavelet_result = wavelet_merge(aligned, job)
    gc.collect()

    job.status_text = "Гибрид: карта уверенности..."
    job.progress = 88
    confidence = compute_confidence_map(aligned)

    job.status_text = "Гибрид: смешивание..."
    job.progress = 92
    confidence_3ch = confidence[:, :, np.newaxis]
    blended = (
        confidence_3ch * sharp_result.astype(np.float32)
        + (1.0 - confidence_3ch) * wavelet_result.astype(np.float32)
    )
    del sharp_result, wavelet_result, confidence, confidence_3ch
    gc.collect()

    return np.clip(blended, 0, 255).astype(np.uint8)


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

    # 2a. Valid area — считаем один раз, переиспользуем в match_exposure и финальном кропе
    valid_area = compute_valid_area(aligned[0].shape, transforms)

    # 2b. Match exposure — убираем видимые швы между кадрами в гладких зонах
    job.status_text = "Выравнивание экспозиции..."
    job.progress = 38
    aligned = match_exposure(aligned, valid_area)
    gc.collect()

    # 3. Merge — выбор метода
    h, w = aligned[0].shape[:2]

    if job.method == "wavelet":
        # Вейвлет-слияние — корректно обрабатывает наложения/полупрозрачность
        result = wavelet_merge(aligned, job)
    elif job.method == "hybrid":
        # Гибрид: sharp там, где явный победитель; wavelet — где наложение
        result = hybrid_merge(aligned, job)
    else:
        # Классический метод: выбор самого резкого пикселя (Tenengrad)
        job.status_text = "Слияние (sharp)..."
        job.progress = 60
        result = sharp_merge(aligned)

    # 4b. Устранение "призраков" от локального движения объекта (опционально)
    if job.fix_motion:
        job.status_text = "Устранение смаза от движения..."
        job.progress = 85
        motion_mask = detect_motion_mask(aligned)
        result = fix_motion_ghosting(result, aligned, motion_mask)
        del motion_mask
        gc.collect()

    del aligned
    gc.collect()

    # 5. Crop valid area
    job.status_text = "Обрезка краёв..."
    job.progress = 90
    x1, y1, x2, y2 = valid_area
    result = result[y1:y2, x1:x2].copy()  # .copy() освобождает ссылку на большой массив
    gc.collect()

    # 6. Save (через Pillow — умеет писать EXIF-метаданные и для JPEG, и для PNG)
    job.status_text = "Сохранение результата..."
    job.progress = 96

    # OpenCV хранит цвет как BGR, Pillow ожидает RGB
    result_rgb = cv2.cvtColor(result, cv2.COLOR_BGR2RGB)
    pil_img = Image.fromarray(result_rgb)
    del result_rgb

    method_label = METHOD_LABELS.get(job.method, job.method)
    if job.fix_motion:
        method_label += " + motion fix"
    exif = pil_img.getexif()
    exif[0x010E] = f"Stacked with SLAP - method: {method_label} - stacklikea.pro"  # ImageDescription
    exif[0x0131] = "SLAP - Stack Like A Pro"  # Software

    if job.fmt == "png":
        result_path = str(RESULT_DIR / f"{job.job_id}.png")
        pil_img.save(result_path, exif=exif)
    else:
        result_path = str(RESULT_DIR / f"{job.job_id}.jpg")
        pil_img.save(result_path, quality=98, subsampling=0, exif=exif)  # subsampling=0 — это 4:4:4

    del pil_img
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
