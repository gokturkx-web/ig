"""advanced.name/freeproxy → IG-uyumlu proxy listesi.

1. Tüm sayfalardan (HTTP+HTTPS tipi) proxy'leri çek (base64 decode).
2. Her birine paralel olarak:
   a. instagram.com/accounts/emailsignup/ sayfasını yüklemeyi dene (TLS).
   b. Başarılıysa signup endpoint'e probe nick gönder.
3. Sınıflandır: ok / rate_limited / network_fail.
"""

import base64
import concurrent.futures
import re
import sys
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin

import requests

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
ROW_RE = re.compile(
    r'<tr>\s*<td>\d+</td>\s*'
    r'<td data-ip="([^"]+)"></td>\s*'
    r'<td data-port="([^"]+)"></td>\s*'
    r'<td>\s*(?:<a [^>]*>(\w+)</a>)?',
    re.DOTALL,
)


@dataclass
class Proxy:
    ip: str
    port: int
    type: str  # http / https / socks4 / socks5

    @property
    def url(self) -> str:
        scheme = "socks5" if self.type.startswith("socks5") else (
            "socks4" if self.type.startswith("socks4") else "http"
        )
        return f"{scheme}://{self.ip}:{self.port}"


def fetch_page(page: int, ptype: Optional[str] = None) -> str:
    url = "https://advanced.name/freeproxy"
    params = {}
    if page > 1:
        params["page"] = str(page)
    if ptype:
        params["type"] = ptype
    r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    return r.text


def parse(html: str) -> list[Proxy]:
    out: list[Proxy] = []
    for m in ROW_RE.finditer(html):
        ip_b64, port_b64, ptype = m.group(1), m.group(2), m.group(3)
        try:
            ip = base64.b64decode(ip_b64).decode()
            port = int(base64.b64decode(port_b64).decode())
        except Exception:
            continue
        out.append(Proxy(ip=ip, port=port, type=(ptype or "http").lower()))
    return out


def probe_one(p: Proxy, timeout: float = 10.0) -> tuple[Proxy, str, Optional[str]]:
    """Tek bir proxy'yi IG'ye karşı test et (HTTP/HTTPS/SOCKS4/SOCKS5)."""
    proxy_url = p.url
    proxies = {"http": proxy_url, "https": proxy_url}
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        # 1) Signup sayfasını yükle (csrftoken almak için)
        r = s.get(
            "https://www.instagram.com/accounts/emailsignup/",
            proxies=proxies,
            timeout=timeout,
        )
    except Exception as e:
        return p, "network_fail", str(e)[:120]

    if r.status_code != 200:
        return p, f"http_{r.status_code}", None

    csrf = s.cookies.get("csrftoken")
    if not csrf:
        return p, "no_csrf", None

    # 2) Endpoint çağır
    try:
        rr = s.post(
            "https://www.instagram.com/accounts/web_create_ajax/attempt/",
            data={
                "email": "probetest@example.com",
                "username": "cristiano",
                "first_name": "",
                "opt_into_one_tap": "false",
            },
            headers={
                "X-CSRFToken": csrf,
                "X-Instagram-AJAX": "1",
                "X-Requested-With": "XMLHttpRequest",
                "X-IG-App-ID": "936619743392459",
                "Referer": "https://www.instagram.com/accounts/emailsignup/",
            },
            proxies=proxies,
            timeout=timeout,
        )
    except Exception as e:
        return p, "network_fail_on_post", str(e)[:120]

    if rr.status_code == 429:
        return p, "rate_limited", None
    try:
        j = rr.json()
    except Exception:
        return p, f"non_json_{rr.status_code}", rr.text[:80]
    if j.get("message") == "feedback_required":
        return p, "rate_limited", None
    errors = j.get("errors") or {}
    user_errors = errors.get("username") or []
    if user_errors and user_errors[0].get("code") == "username_is_taken":
        return p, "ok", None
    return p, "unexpected", str(j)[:120]


def main() -> int:
    print("advanced.name'den proxy listesi çekiliyor...", flush=True)
    all_proxies: list[Proxy] = []

    for ptype in ["http", "https", "socks4", "socks5"]:
        for page in range(1, 11):  # ihtiyaten 10 sayfa
            try:
                html = fetch_page(page, ptype)
            except Exception as e:
                print(f"  [type={ptype} page={page}] hata: {e}", flush=True)
                break
            rows = parse(html)
            if not rows:
                break
            for r in rows:
                r.type = ptype
            all_proxies.extend(rows)
            print(f"  [type={ptype} page={page}] {len(rows)} proxy", flush=True)
            if len(rows) < 100:
                break
            time.sleep(0.4)

    # Tekrarları temizle
    seen = set()
    uniq: list[Proxy] = []
    for p in all_proxies:
        key = (p.ip, p.port)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)

    print(f"\nToplam {len(uniq)} benzersiz proxy (http/https/socks). Probe ediliyor...\n", flush=True)

    ok: list[Proxy] = []
    rate_limited: list[Proxy] = []
    other: list[tuple[Proxy, str, Optional[str]]] = []
    network_fail = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=40) as ex:
        futures = {ex.submit(probe_one, p): p for p in uniq}
        done_count = 0
        for fut in concurrent.futures.as_completed(futures):
            done_count += 1
            try:
                p, status, detail = fut.result()
            except Exception as e:
                continue
            if status == "ok":
                ok.append(p)
                print(f"  OK   {p.ip}:{p.port} ({p.type})", flush=True)
            elif status == "rate_limited":
                rate_limited.append(p)
            elif status.startswith("network_fail") or status.startswith("http_") or status == "no_csrf":
                network_fail += 1
            else:
                other.append((p, status, detail))
            if done_count % 50 == 0:
                print(f"  [progress] {done_count}/{len(uniq)} (ok={len(ok)}, "
                      f"rate_limited={len(rate_limited)}, fail={network_fail})",
                      flush=True)

    print(f"\n=== ÖZET (toplam {len(uniq)}) ===")
    print(f"  OK            : {len(ok)}")
    print(f"  Rate limited  : {len(rate_limited)}")
    print(f"  Network/HTTP  : {network_fail}")
    print(f"  Diğer         : {len(other)}")

    # Yaz
    if ok:
        with open("advanced_ok.txt", "w") as f:
            for p in ok:
                f.write(f"{p.url}\n")
        print(f"\nÇalışan proxy'ler -> advanced_ok.txt ({len(ok)} adet):")
        for p in ok:
            print(f"  {p.url}")
    if rate_limited:
        with open("advanced_rate_limited.txt", "w") as f:
            for p in rate_limited:
                f.write(f"{p.url}\n")
        print(f"\nIG'de rate-limited (proxy çalışıyor ama IG bloğunda) "
              f"-> advanced_rate_limited.txt ({len(rate_limited)} adet)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
