"""Her proxy için Instagram erişilebilirliğini ölç.

Her proxy için:
  1) signup sayfasını çek (csrftoken alabilir miyiz?)
  2) tek bir kullanıcı adı kontrolü yap
  3) sonucu sınıflandır:
       OK             -> sınıflandırılmış cevap döndü, kullanılabilir
       RATE_LIMITED   -> 429 / feedback_required (IP IG bloğunda)
       NETWORK_FAIL   -> proxy timeout / TLS hata / DNS
       NO_CSRF        -> bağlandı ama signup sayfasından token çıkmadı
       UNKNOWN        -> başka bir durum
"""

from __future__ import annotations

import argparse
import sys
import time
from urllib.parse import urlsplit

from ig_scanner.checker import IGChecker, UsernameStatus


def _ipport(proxy_url: str) -> str:
    u = urlsplit(proxy_url)
    return f"{u.hostname}:{u.port}"


def probe(proxy_url: str, username: str, timeout: float) -> tuple[str, str]:
    label = _ipport(proxy_url)
    try:
        c = IGChecker(proxy=proxy_url, timeout=timeout)
    except Exception as e:
        return label, f"INIT_FAIL: {e}"
    try:
        c._bootstrap()
    except Exception as e:
        msg = str(e)
        # Yaygın ağ hataları
        if "csrftoken" in msg.lower():
            return label, "NO_CSRF (signup sayfası geldi ama token yok)"
        return label, f"NETWORK_FAIL: {msg[:200]}"
    if not c._csrf:
        return label, "NO_CSRF"
    try:
        r = c.check(username)
    except Exception as e:
        return label, f"NETWORK_FAIL_ON_CHECK: {str(e)[:200]}"
    if r.status == UsernameStatus.RATE_LIMITED:
        return label, "RATE_LIMITED (IP IG bloğunda)"
    if r.status in (
        UsernameStatus.AVAILABLE,
        UsernameStatus.TAKEN,
        UsernameStatus.RESERVED,
        UsernameStatus.BLOCKED_TERM,
    ):
        return label, f"OK -> {r.status.value} ({r.code or '-'})"
    return label, f"UNKNOWN -> {r.status.value} {r.code or ''} {r.message or ''}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxies-file", default="proxies.txt")
    ap.add_argument("--username", default="cristiano",
                    help="probe için kullanılacak nick (alındığı bilinen biri)")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--delay", type=float, default=2.0,
                    help="proxy'ler arası bekleme")
    args = ap.parse_args()

    with open(args.proxies_file, encoding="utf-8") as f:
        proxies = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    print(f"Toplam {len(proxies)} proxy probe ediliyor...\n", flush=True)
    results: list[tuple[str, str]] = []
    for i, p in enumerate(proxies, 1):
        label, status = probe(p, args.username, args.timeout)
        print(f"[{i}/{len(proxies)}] {label:25s} -> {status}", flush=True)
        results.append((label, status))
        time.sleep(args.delay)

    print("\n=== ÖZET ===")
    ok = [l for l, s in results if s.startswith("OK")]
    rl = [l for l, s in results if s.startswith("RATE_LIMITED")]
    nf = [l for l, s in results if s.startswith("NETWORK_FAIL") or s.startswith("INIT_FAIL")]
    other = [(l, s) for l, s in results if l not in ok + rl + nf]
    print(f"Kullanılabilir: {len(ok)} -> {ok}")
    print(f"Rate-limited:   {len(rl)} -> {rl}")
    print(f"Ağ hatası:      {len(nf)} -> {nf}")
    if other:
        print(f"Diğer:          {other}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
