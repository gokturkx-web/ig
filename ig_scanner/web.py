"""FastAPI tabanlı web arayüzü.

`/`         -> HTML arayüz
`/api/check` (POST, NDJSON streaming) -> Verilen kullanıcı adlarını sırayla
                                          kontrol eder ve sonuçları satır
                                          satır JSON olarak akıtır.
                                          Body: {"usernames": [...],
                                                 "use_server_proxies": bool,
                                                 "per_request_delay": float}
"""

from __future__ import annotations

import asyncio
import json
import os
import random
from itertools import cycle
from pathlib import Path
from typing import AsyncIterator, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .checker import IGChecker, UsernameStatus


STATIC_DIR = Path(__file__).resolve().parent / "static"


# ---------------------------------------------------------------------------
# Proxy yönetimi
# ---------------------------------------------------------------------------


def load_server_proxies() -> List[str]:
    """Server'da `proxies.txt` varsa oku. Yoksa boş liste."""
    candidates = [
        Path("proxies.txt"),
        Path(os.environ.get("IG_SCANNER_PROXIES_FILE", "")),
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
# Model
# ---------------------------------------------------------------------------


class CheckRequest(BaseModel):
    usernames: List[str] = Field(..., description="Kontrol edilecek nickler")
    use_server_proxies: bool = True
    per_request_delay: float = 2.5


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

    @app.post("/api/check")
    async def check(req: CheckRequest, request: Request) -> StreamingResponse:
        # Boş satırları/biçim olarak çok bariz hatalıları temizle
        cleaned: List[str] = []
        seen: set[str] = set()
        for raw in req.usernames:
            u = raw.strip().lstrip("@")
            if not u:
                continue
            if u in seen:
                continue
            seen.add(u)
            cleaned.append(u)

        proxies: List[Optional[str]] = []
        if req.use_server_proxies:
            proxies = list(load_server_proxies())
        if not proxies:
            proxies = [None]

        return StreamingResponse(
            _stream(cleaned, proxies, req.per_request_delay, request),
            media_type="application/x-ndjson",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return app


# ---------------------------------------------------------------------------
# Stream
# ---------------------------------------------------------------------------


async def _stream(
    usernames: List[str],
    proxies: List[Optional[str]],
    per_request_delay: float,
    request: Request,
) -> AsyncIterator[bytes]:
    """Her nick için sonucu NDJSON satırı olarak akıt."""

    yield _line({
        "type": "start",
        "total": len(usernames),
        "proxy_count": len([p for p in proxies if p]),
    })

    # Proxy başına bir checker; round-robin paylaşıyoruz.
    checkers = [IGChecker(proxy=p) for p in proxies]
    proxy_iter = cycle(range(len(checkers)))

    loop = asyncio.get_running_loop()
    try:
        for i, username in enumerate(usernames):
            if await request.is_disconnected():
                return

            idx = next(proxy_iter)
            checker = checkers[idx]

            # Rate-limit'e takılırsak en fazla 2 farklı checker daha dene.
            attempted_idxs = {idx}
            result = None
            for attempt in range(min(3, len(checkers))):
                result = await loop.run_in_executor(
                    None, checker.check, username
                )
                if result.status != UsernameStatus.RATE_LIMITED:
                    break
                # Başka bir proxy varsa ona geç
                next_idx = None
                for _ in range(len(checkers)):
                    cand = next(proxy_iter)
                    if cand not in attempted_idxs:
                        next_idx = cand
                        break
                if next_idx is None:
                    break
                attempted_idxs.add(next_idx)
                checker = checkers[next_idx]
                idx = next_idx

            assert result is not None
            payload = {
                "type": "result",
                "index": i,
                "username": result.username,
                "status": result.status.value,
                "code": result.code,
                "message": result.message,
                "proxy": _proxy_label(proxies[idx]) if proxies[idx] else None,
            }
            yield _line(payload)

            # Aynı checker'a (proxy'ye) iki istek arası kısa nefes ver.
            if i < len(usernames) - 1:
                await asyncio.sleep(
                    per_request_delay
                    + random.uniform(0, per_request_delay * 0.2)
                )
    finally:
        yield _line({"type": "done"})


def _line(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def _proxy_label(proxy_url: str) -> str:
    # user:pass'i sızdırma; sadece host:port döndür.
    from urllib.parse import urlsplit

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
