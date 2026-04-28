"""
Toplu kullanıcı adı tarayıcısı.

Özellikler:

- 4/5/6 (veya istediğin) hanede otomatik üretim **veya** verilen wordlist
- Sonuçları durumlarına göre ayrı dosyalara yazma:
    available.txt   -> Boş (alınabilir) nickler
    taken.txt       -> Alınmış (deaktive olsa bile)
    reserved.txt    -> Instagram'ın yasakladığı/rezerve nickler
    blocked.txt     -> Yasaklı substring içerenler
    invalid.txt     -> Biçimsel olarak geçersiz olanlar
    unknown.txt     -> Yorumlanamayan cevaplar
- `state.json` ile **kaldığı yerden devam** (resume)
- IP başına bir worker (paralel) ve her worker için ayrı proxy
- 429 / spam block geldiğinde otomatik backoff ve session yenileme
- Saniyede istek hızını sınırlamak için `--qps`

Kullanım:

    # 4 haneli tüm a-z 0-9 _ . kombinasyonlarını tara
    python -m ig_scanner.scanner --length 4 --out results/

    # 5 ve 6 haneli, sadece a-z 0-9 (alfanumerik), tek proxy ile
    python -m ig_scanner.scanner --length 5 6 --alphabet alnum \
        --proxies http://user:pass@1.2.3.4:8080

    # Kendi listenden tara
    python -m ig_scanner.scanner --wordlist my_words.txt --out results/

Önemli: Instagram tek IP'den yaklaşık 10-20 istekten sonra `feedback_required`
cevabı verir. Ciddi bir tarama için **proxy havuzu** zorunludur. Bu araç sana
dakika başına o havuzun kapasitesi kadar istek atma imkânı verir.
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import queue
import random
import string
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Sequence

from .checker import (
    CheckResult,
    IGChecker,
    UsernameStatus,
)


logger = logging.getLogger("ig_scanner")


# ---------------------------------------------------------------------------
# Üretim
# ---------------------------------------------------------------------------

ALPHABETS = {
    # Instagram'ın izin verdiği tüm karakterler
    "full": string.ascii_lowercase + string.digits + "._",
    # Sadece harf+rakam (en sık taranan)
    "alnum": string.ascii_lowercase + string.digits,
    # Sadece harf
    "alpha": string.ascii_lowercase,
    # Sadece rakam
    "digits": string.digits,
}


def generate_combinations(length: int, alphabet: str) -> Iterator[str]:
    """Sözlük sırasıyla verilen uzunlukta tüm kombinasyonları üret.

    Not: Bir kullanıcı adı `.` veya `_` ile başlayamaz/bitemez ve ardışık iki
    nokta içeremez (Instagram kuralı). Bu kuralları burada uygulamıyoruz —
    `IGChecker` zaten geçersiz biçimleri ayıklar; ama üretim aşamasında en
    bariz olanları (ilk/son karakter) atlamak işlem sayısını azaltır.
    """
    chars = ALPHABETS[alphabet]
    # `.` ve `_` ilk veya son karakter olamaz; üretirken bunu uygula.
    has_special = "." in chars or "_" in chars
    for tup in itertools.product(chars, repeat=length):
        if has_special:
            if tup[0] in "._" or tup[-1] in "._":
                continue
            # ardışık iki nokta yasak
            joined = "".join(tup)
            if ".." in joined:
                continue
            yield joined
        else:
            yield "".join(tup)


def iter_wordlist(path: str) -> Iterator[str]:
    with open(path, encoding="utf-8") as f:
        for line in f:
            u = line.strip()
            if u:
                yield u


# ---------------------------------------------------------------------------
# Sonuç dosyaları
# ---------------------------------------------------------------------------


class ResultWriter:
    """Status -> dosya eşlemesi. Thread-safe."""

    FILE_FOR = {
        UsernameStatus.AVAILABLE: "available.txt",
        UsernameStatus.TAKEN: "taken.txt",
        UsernameStatus.RESERVED: "reserved.txt",
        UsernameStatus.BLOCKED_TERM: "blocked.txt",
        UsernameStatus.INVALID_FORMAT: "invalid.txt",
        UsernameStatus.UNKNOWN: "unknown.txt",
    }

    def __init__(self, out_dir: Path) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir = out_dir
        self._files = {
            status: open(out_dir / name, "a", encoding="utf-8", buffering=1)
            for status, name in self.FILE_FOR.items()
        }
        self._jsonl = open(out_dir / "all.jsonl", "a", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()

    def write(self, result: CheckResult) -> None:
        with self._lock:
            f = self._files.get(result.status)
            if f is not None:
                f.write(result.username + "\n")
            self._jsonl.write(
                json.dumps(
                    {
                        "username": result.username,
                        "status": result.status.value,
                        "code": result.code,
                        "message": result.message,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    def close(self) -> None:
        with self._lock:
            for f in self._files.values():
                f.close()
            self._jsonl.close()


# ---------------------------------------------------------------------------
# Resume / state
# ---------------------------------------------------------------------------


class StateStore:
    """Tarayıcının hangi kullanıcı adlarını işlediğini diske kaydeder."""

    def __init__(self, out_dir: Path) -> None:
        self.path = out_dir / "state.json"
        self._lock = threading.Lock()
        self._processed: set[str] = set()
        if self.path.exists():
            try:
                with open(self.path, encoding="utf-8") as f:
                    self._processed = set(json.load(f).get("processed", []))
            except Exception:
                self._processed = set()
        self._dirty = False
        # Mevcut sonuç dosyalarındaki nickleri de processed kabul et.
        for name in ResultWriter.FILE_FOR.values():
            p = out_dir / name
            if p.exists():
                with open(p, encoding="utf-8") as f:
                    for line in f:
                        u = line.strip()
                        if u:
                            self._processed.add(u)

    def is_done(self, username: str) -> bool:
        return username in self._processed

    def mark(self, username: str) -> None:
        with self._lock:
            self._processed.add(username)
            self._dirty = True

    def flush(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"processed": sorted(self._processed)}, f)
            tmp.replace(self.path)
            self._dirty = False


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class Worker(threading.Thread):
    def __init__(
        self,
        name: str,
        proxy: Optional[str],
        in_q: "queue.Queue[Optional[str]]",
        writer: ResultWriter,
        state: StateStore,
        stats: "Stats",
        per_request_delay: float,
        backoff_seconds: float,
    ) -> None:
        super().__init__(name=name, daemon=True)
        self.proxy = proxy
        self.in_q = in_q
        self.writer = writer
        self.state = state
        self.stats = stats
        self.per_request_delay = per_request_delay
        self.backoff_seconds = backoff_seconds
        self.checker = IGChecker(proxy=proxy)

    def run(self) -> None:
        while True:
            username = self.in_q.get()
            try:
                if username is None:
                    return
                self._process(username)
            finally:
                self.in_q.task_done()

    def _process(self, username: str) -> None:
        attempts = 0
        while True:
            attempts += 1
            try:
                result = self.checker.check(username)
            except Exception as e:  # ağ vs.
                logger.warning("[%s] %s -> exception: %s", self.name, username, e)
                time.sleep(self.backoff_seconds)
                if attempts >= 3:
                    return
                continue

            if result.status == UsernameStatus.RATE_LIMITED:
                self.stats.incr_rate_limited()
                if attempts >= 3:
                    # 3 deneme sonra hâlâ rate-limit ise vazgeç. Bu nick
                    # state'e işaretlenmediği için resume sırasında başka
                    # bir worker (başka proxy) ile tekrar denenir.
                    logger.error(
                        "[%s] %s için ısrarlı rate limit (%d deneme), atlıyorum",
                        self.name,
                        username,
                        attempts,
                    )
                    return
                wait = self.backoff_seconds * (2 ** (attempts - 1))
                wait = min(wait, 300.0)
                wait += random.uniform(0, wait * 0.25)
                logger.warning(
                    "[%s] rate limit, %.1fs bekliyor ve session yeniliyor (deneme %d)",
                    self.name,
                    wait,
                    attempts,
                )
                time.sleep(wait)
                try:
                    self.checker.refresh_session()
                except Exception as e:
                    logger.warning(
                        "[%s] session yenilenemedi: %s", self.name, e
                    )
                continue

            self.writer.write(result)
            self.state.mark(result.username)
            self.stats.record(result.status)
            logger.info(
                "[%s] %s -> %s%s",
                self.name,
                result.username,
                result.status.value,
                f" ({result.code})" if result.code else "",
            )
            time.sleep(self.per_request_delay)
            return


# ---------------------------------------------------------------------------
# İstatistik
# ---------------------------------------------------------------------------


class Stats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counts: dict[str, int] = {s.value: 0 for s in UsernameStatus}
        self.rate_limited = 0
        self.start = time.time()

    def record(self, status: UsernameStatus) -> None:
        with self._lock:
            self.counts[status.value] += 1

    def incr_rate_limited(self) -> None:
        with self._lock:
            self.rate_limited += 1

    def snapshot(self) -> dict:
        with self._lock:
            elapsed = max(time.time() - self.start, 1e-6)
            total = sum(self.counts.values())
            return {
                **self.counts,
                "rate_limit_events": self.rate_limited,
                "total_processed": total,
                "rps": round(total / elapsed, 3),
                "elapsed_s": round(elapsed, 1),
            }


# ---------------------------------------------------------------------------
# Çalıştırıcı
# ---------------------------------------------------------------------------


def run_scan(
    sources: Iterable[str],
    out_dir: Path,
    proxies: Sequence[Optional[str]],
    per_request_delay: float,
    backoff_seconds: float,
    flush_every: int = 200,
) -> None:
    writer = ResultWriter(out_dir)
    state = StateStore(out_dir)
    stats = Stats()

    # Kuyruk: worker sayısının iki katı kadar buffer.
    in_q: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=max(8, len(proxies) * 4))

    workers: List[Worker] = []
    for i, proxy in enumerate(proxies):
        w = Worker(
            name=f"w{i}",
            proxy=proxy,
            in_q=in_q,
            writer=writer,
            state=state,
            stats=stats,
            per_request_delay=per_request_delay,
            backoff_seconds=backoff_seconds,
        )
        w.start()
        workers.append(w)

    last_flush = 0
    fed = 0
    skipped = 0
    try:
        for u in sources:
            if state.is_done(u):
                skipped += 1
                continue
            in_q.put(u)
            fed += 1
            if fed - last_flush >= flush_every:
                state.flush()
                last_flush = fed
                logger.info("ilerleme: %s | atlanan: %d", stats.snapshot(), skipped)
        in_q.join()
    finally:
        for _ in workers:
            in_q.put(None)
        for w in workers:
            w.join(timeout=5)
        state.flush()
        writer.close()
        logger.info("bitti: %s | atlanan: %d", stats.snapshot(), skipped)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Instagram'da boş kullanıcı adlarını signup endpoint üzerinden "
            "tespit eder (404 değil)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = p.add_argument_group("Kaynak (biri zorunlu)")
    src.add_argument(
        "--length",
        type=int,
        nargs="+",
        help="Üretilecek kullanıcı adı uzunluğu/uzunlukları (örn. 4 5 6).",
    )
    src.add_argument(
        "--alphabet",
        choices=sorted(ALPHABETS.keys()),
        default="alnum",
        help="Üretim alfabesi (varsayılan: alnum). full = a-z 0-9 . _",
    )
    src.add_argument(
        "--wordlist",
        help="Satır başına bir kullanıcı adı içeren dosya.",
    )

    p.add_argument(
        "--out",
        default="results",
        help="Sonuç dizini (varsayılan: results)",
    )
    p.add_argument(
        "--proxies",
        nargs="*",
        default=[],
        help=(
            "Worker başına bir proxy. Boş bırakılırsa kendi IP'nle tek "
            "worker çalışır (önerilmez, hızla rate-limit yersin)."
        ),
    )
    p.add_argument(
        "--proxies-file",
        help=(
            "Satır başına bir proxy URL içeren dosya. `--proxies` ile "
            "birleştirilir. # ile başlayan satırlar yorum olarak atlanır."
        ),
    )
    p.add_argument(
        "--per-request-delay",
        type=float,
        default=2.5,
        help=(
            "Aynı worker'ın iki istek arasında bekleyeceği saniye "
            "(varsayılan: 2.5). Düşürmek rate-limit ihtimalini artırır."
        ),
    )
    p.add_argument(
        "--backoff-seconds",
        type=float,
        default=30.0,
        help="429 sonrası ilk backoff süresi (saniye, üstel artar).",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.length and not args.wordlist:
        raise SystemExit("--length veya --wordlist parametrelerinden birini ver.")

    proxies: List[Optional[str]] = []
    if args.proxies:
        proxies.extend(args.proxies)
    if args.proxies_file:
        with open(args.proxies_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    proxies.append(line)
    if not proxies:
        proxies = [None]
    logger.info("toplam %d worker (proxy) ile başlıyor", len(proxies))

    def sources() -> Iterator[str]:
        if args.wordlist:
            yield from iter_wordlist(args.wordlist)
        if args.length:
            for L in args.length:
                yield from generate_combinations(L, args.alphabet)

    run_scan(
        sources=sources(),
        out_dir=Path(args.out),
        proxies=proxies,
        per_request_delay=args.per_request_delay,
        backoff_seconds=args.backoff_seconds,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
