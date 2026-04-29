"""FastAPI tabanlı web arayüzü.

Endpoints:
  GET  /                 -> tek sayfalık HTML arayüz
  GET  /api/proxies      -> server'da yüklü proxy sayısı
  POST /api/jobs         -> yeni iş başlat. Body: bkz. JobRequest
  GET  /api/jobs/{id}    -> iş durumu + birikmiş sonuçlar
  POST /api/jobs/{id}/cancel -> işi iptal et
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import string
import threading
import time
import uuid
from itertools import cycle
from pathlib import Path
from typing import List, Literal, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .checker import IGChecker, UsernameStatus

logger = logging.getLogger(__name__)


STATIC_DIR = Path(__file__).resolve().parent / "static"

# Üretici karakter setleri.
ALPHABETS: dict[str, str] = {
    "alnum": string.ascii_lowercase + string.digits,
    "alpha": string.ascii_lowercase,
    "num": string.digits,
    "alnum_dot": string.ascii_lowercase + string.digits + "._",
}

# UI'dan kabul edilecek maksimum tek-iş bütçesi.
MAX_USERNAMES_PER_JOB = 500
MAX_LENGTH = 12

# Tamamlanmış iş kayıtlarını ne kadar tutalım (sn).
JOB_RETENTION_SEC = 30 * 60


# ---------------------------------------------------------------------------
# Proxy yönetimi
# ---------------------------------------------------------------------------


def load_server_proxies() -> List[str]:
    """Proxy listesini yükle.

    Kaynaklar (sırayla denenir):
      1. `IG_SCANNER_PROXIES` ortam değişkeni (newline veya virgülle ayrılmış).
      2. `IG_SCANNER_PROXIES_FILE` ortam değişkeninin işaret ettiği dosya.
      3. Çalışma dizinindeki `proxies.txt`.
    """
    env = os.environ.get("IG_SCANNER_PROXIES")
    if env:
        out: List[str] = []
        for chunk in env.replace(",", "\n").splitlines():
            chunk = chunk.strip()
            if chunk and not chunk.startswith("#"):
                out.append(chunk)
        if out:
            return out

    candidates = [
        Path(os.environ.get("IG_SCANNER_PROXIES_FILE", "")),
        Path("proxies.txt"),
    ]
    for p in candidates:
        if p and p.is_file():
            with open(p, encoding="utf-8") as f:
                return [
                    line.strip()
                    for line in f
                    if line.strip() and not line.startswith("#")
                ]
    return []


# ---------------------------------------------------------------------------
# Üretici
# ---------------------------------------------------------------------------


def generate_random_usernames(
    length: int, count: int, alphabet_key: str
) -> List[str]:
    """Verilen uzunluk + alfabe ile rastgele kullanıcı adları üret.

    Aynı handle iki kez üretilmez. Eğer toplam mümkün kombinasyon `count`'tan
    azsa, mümkün olan kadarını döndürür.
    """
    if alphabet_key not in ALPHABETS:
        raise ValueError(f"Bilinmeyen alfabe: {alphabet_key}")
    alphabet = ALPHABETS[alphabet_key]
    space = len(alphabet) ** length
    target = min(count, space)
    seen: set[str] = set()
    rng = random.SystemRandom()
    # Üretmeye çalışırken çok fazla deneme yapma — küçük uzayda tüm uzayı tara.
    if space <= max(5000, target * 4):
        # Tüm kombinasyonları üret, karıştır, ilk N'i döndür.
        from itertools import product

        all_combos = (
            "".join(c) for c in product(alphabet, repeat=length)
        )
        # Boyut çok büyükse bu yine yavaş olur ama buraya zaten girmiyoruz.
        pool = list(all_combos)
        rng.shuffle(pool)
        return pool[:target]
    # Büyük uzay: rastgele üret, tekrarları at.
    attempts = 0
    while len(seen) < target and attempts < target * 8:
        s = "".join(rng.choice(alphabet) for _ in range(length))
        if alphabet_key == "alnum_dot" and (
            s[0] in "._" or s[-1] in "._" or ".." in s or "__" in s
        ):
            attempts += 1
            continue
        seen.add(s)
        attempts += 1
    return list(seen)


# ---------------------------------------------------------------------------
# Modeller
# ---------------------------------------------------------------------------


class JobRequest(BaseModel):
    mode: Literal["manual", "generated"] = "manual"
    usernames: Optional[List[str]] = None
    length: Optional[int] = None
    count: Optional[int] = None
    alphabet: Optional[str] = "alnum"
    use_server_proxies: bool = True
    custom_proxies: Optional[List[str]] = None
    per_request_delay: float = 2.5

    @field_validator("per_request_delay")
    @classmethod
    def _bounded_delay(cls, v: float) -> float:
        return max(0.0, min(30.0, v))


class JobResult(BaseModel):
    index: int
    username: str
    status: str
    code: Optional[str] = None
    message: Optional[str] = None
    proxy: Optional[str] = None
    error: Optional[str] = None


class JobState(BaseModel):
    id: str
    state: Literal["queued", "running", "done", "cancelled", "error"]
    total: int
    processed: int
    started_at: float
    updated_at: float
    proxy_count: int
    results: List[JobResult]
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Job yöneticisi (in-memory)
# ---------------------------------------------------------------------------


class _Job:
    def __init__(
        self,
        usernames: List[str],
        proxies: List[str],
        per_request_delay: float,
    ):
        self.id = uuid.uuid4().hex[:16]
        self.usernames = usernames
        self.proxies = proxies
        self.per_request_delay = per_request_delay
        self.state: str = "queued"
        self.results: List[JobResult] = []
        self.started_at = time.time()
        self.updated_at = time.time()
        self.error: Optional[str] = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    def to_state(self) -> JobState:
        with self._lock:
            return JobState(
                id=self.id,
                state=self.state,  # type: ignore[arg-type]
                total=len(self.usernames),
                processed=len(self.results),
                started_at=self.started_at,
                updated_at=self.updated_at,
                proxy_count=len(self.proxies),
                results=list(self.results),
                error=self.error,
            )


_jobs: dict[str, _Job] = {}
_jobs_lock = threading.Lock()


def _gc_old_jobs() -> None:
    now = time.time()
    with _jobs_lock:
        stale = [
            jid for jid, j in _jobs.items()
            if j.state in ("done", "cancelled", "error")
            and now - j.updated_at > JOB_RETENTION_SEC
        ]
        for jid in stale:
            del _jobs[jid]


def _run_job(job: _Job) -> None:
    """Worker thread: usernames'i sırayla kontrol et, results'a ekle."""
    job.state = "running"
    checkers = [IGChecker(proxy=p) for p in job.proxies] if job.proxies else [IGChecker(proxy=None)]
    proxy_idx = cycle(range(len(checkers)))
    try:
        for i, username in enumerate(job.usernames):
            if job._cancel.is_set():
                with job._lock:
                    job.state = "cancelled"
                    job.updated_at = time.time()
                return

            idx = next(proxy_idx)
            checker = checkers[idx]

            # 429 olursa sırayla başka proxy'leri dene (en fazla 3).
            tried = {idx}
            res = None
            err: Optional[str] = None
            for _ in range(min(3, len(checkers))):
                try:
                    res = checker.check(username)
                except Exception as e:
                    err = f"{type(e).__name__}: {str(e)[:120]}"
                    res = None
                    # Bu proxy bozuk olabilir; başka birini dene.
                else:
                    err = None
                    if res.status != UsernameStatus.RATE_LIMITED:
                        break
                # Sıradaki farklı proxy'yi seç
                next_idx: Optional[int] = None
                for _ in range(len(checkers)):
                    cand = next(proxy_idx)
                    if cand not in tried:
                        next_idx = cand
                        break
                if next_idx is None:
                    break
                tried.add(next_idx)
                checker = checkers[next_idx]
                idx = next_idx

            with job._lock:
                if res is not None:
                    job.results.append(
                        JobResult(
                            index=i,
                            username=username,
                            status=res.status.value,
                            code=res.code,
                            message=res.message,
                            proxy=_proxy_label(job.proxies[idx]) if job.proxies else None,
                        )
                    )
                else:
                    job.results.append(
                        JobResult(
                            index=i,
                            username=username,
                            status="unknown",
                            error=err or "unknown error",
                            proxy=_proxy_label(job.proxies[idx]) if job.proxies else None,
                        )
                    )
                job.updated_at = time.time()

            if i < len(job.usernames) - 1:
                # Gecikme; cancel beklerken kısa kontrol döngüsü
                end = time.time() + job.per_request_delay + random.uniform(
                    0, job.per_request_delay * 0.25
                )
                while time.time() < end:
                    if job._cancel.is_set():
                        break
                    time.sleep(0.1)

        with job._lock:
            if job.state != "cancelled":
                job.state = "done"
            job.updated_at = time.time()
    except Exception as e:
        logger.exception("job %s failed", job.id)
        with job._lock:
            job.state = "error"
            job.error = f"{type(e).__name__}: {str(e)[:200]}"
            job.updated_at = time.time()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    app = FastAPI(title="ig-username-scanner")

    if STATIC_DIR.exists():
        app.mount(
            "/static", StaticFiles(directory=str(STATIC_DIR)), name="static"
        )

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/api/proxies")
    def proxies_status() -> dict:
        proxies = load_server_proxies()
        return {"count": len(proxies), "configured": bool(proxies)}

    @app.get("/api/alphabets")
    def alphabets() -> dict:
        return {
            k: {"chars": v, "size": len(v)} for k, v in ALPHABETS.items()
        }

    class GenerateRequest(BaseModel):
        length: int = Field(..., ge=1, le=MAX_LENGTH)
        count: int = Field(..., ge=1, le=MAX_USERNAMES_PER_JOB)
        alphabet: str = "alnum"

    @app.post("/api/generate")
    def gen_usernames(req: GenerateRequest) -> dict:
        try:
            usernames = generate_random_usernames(
                req.length, req.count, req.alphabet
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"usernames": usernames, "count": len(usernames)}

    @app.post("/api/jobs")
    def create_job(req: JobRequest) -> dict:
        _gc_old_jobs()

        # 1) Kullanıcı listesi belirle
        if req.mode == "manual":
            raw = req.usernames or []
        elif req.mode == "generated":
            if not req.length or not req.count:
                raise HTTPException(400, "length ve count gerekli")
            if req.length < 1 or req.length > MAX_LENGTH:
                raise HTTPException(400, f"length 1..{MAX_LENGTH} aralığında olmalı")
            if req.count < 1 or req.count > MAX_USERNAMES_PER_JOB:
                raise HTTPException(
                    400, f"count 1..{MAX_USERNAMES_PER_JOB} aralığında olmalı"
                )
            try:
                raw = generate_random_usernames(
                    req.length, req.count, req.alphabet or "alnum"
                )
            except ValueError as e:
                raise HTTPException(400, str(e))
        else:
            raise HTTPException(400, f"bilinmeyen mode: {req.mode}")

        # 2) Temizle
        cleaned: List[str] = []
        seen: set[str] = set()
        for u in raw:
            uu = (u or "").strip().lstrip("@")
            if not uu or uu in seen:
                continue
            seen.add(uu)
            cleaned.append(uu)

        if not cleaned:
            raise HTTPException(400, "Kontrol edilecek nick yok")
        if len(cleaned) > MAX_USERNAMES_PER_JOB:
            raise HTTPException(
                400, f"En fazla {MAX_USERNAMES_PER_JOB} nick olabilir"
            )

        # 3) Proxy seç
        proxies: List[str] = []
        if req.custom_proxies:
            proxies.extend(p.strip() for p in req.custom_proxies if p.strip())
        if req.use_server_proxies:
            proxies.extend(load_server_proxies())
        # tekrarları at
        proxies = list(dict.fromkeys(proxies))

        # 4) Job oluştur ve başlat
        job = _Job(cleaned, proxies, req.per_request_delay)
        with _jobs_lock:
            _jobs[job.id] = job
        threading.Thread(target=_run_job, args=(job,), daemon=True).start()
        return {
            "job_id": job.id,
            "total": len(cleaned),
            "proxy_count": len(proxies),
        }

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> JobState:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job bulunamadı")
        return job.to_state()

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job bulunamadı")
        job._cancel.set()
        return {"ok": True}

    return app


def _proxy_label(proxy_url: Optional[str]) -> Optional[str]:
    if not proxy_url:
        return None
    u = urlsplit(proxy_url)
    return f"{u.hostname}:{u.port}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli() -> None:  # pragma: no cover
    import argparse
    import uvicorn

    p = argparse.ArgumentParser(description="Instagram nick tarayıcı web arayüzü")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true")
    args = p.parse_args()

    uvicorn.run(
        "ig_scanner.web:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


# uvicorn import path için modül seviyesinde app.
app = create_app()
