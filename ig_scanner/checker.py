"""
Instagram kullanıcı adı durumunu Instagram'ın kayıt (sign-up) attempt endpoint'i
üzerinden tespit eder.

Bu yöntem `instagram.com/<user>/` üzerindeki 404 kontrolüne göre çok daha
güvenilir çünkü Instagram bize doğrudan şu durumları söyler:

- Hiç hata yok                             -> AVAILABLE     (boş, alınabilir)
- code == "username_is_taken"              -> TAKEN         (biri kullanıyor;
                                                             profil deaktive
                                                             veya gizli olsa
                                                             bile)
- code == "username_invalid"               -> RESERVED      (Instagram tarafından
                                                             yasaklı / rezerve)
- code == "username_invalid_substring"     -> BLOCKED_TERM  (yasaklı kelime
                                                             içeriyor)
- HTTP 429 / feedback_required             -> RATE_LIMITED  (IP veya cookie
                                                             bloklandı, retry
                                                             gerekli)

`instagram.com/<user>/` 404 dönerse o nick'in alınamaz / yasaklı / deaktive
olabileceğini ayırt edemiyoruz; bu modül bu sorunu çözer.
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import requests

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sabitler
# ---------------------------------------------------------------------------

SIGNUP_PAGE_URL = "https://www.instagram.com/accounts/emailsignup/"
ATTEMPT_URL = "https://www.instagram.com/accounts/web_create_ajax/attempt/"

# Instagram web uygulamasının App-ID değeri. Sabit ve uzun süredir aynı.
IG_APP_ID = "936619743392459"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Instagram'ın geçerli kullanıcı adı formatı: 1-30 karakter, harf/rakam/nokta/alt çizgi.
USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


# ---------------------------------------------------------------------------
# Tipler
# ---------------------------------------------------------------------------


class UsernameStatus(str, Enum):
    AVAILABLE = "available"
    TAKEN = "taken"
    RESERVED = "reserved"
    BLOCKED_TERM = "blocked_term"
    INVALID_FORMAT = "invalid_format"
    RATE_LIMITED = "rate_limited"
    UNKNOWN = "unknown"


@dataclass
class CheckResult:
    username: str
    status: UsernameStatus
    code: Optional[str] = None
    message: Optional[str] = None
    raw: Optional[dict] = None


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------


class IGChecker:
    """Instagram kullanıcı adı kontrolü için thread-safe olmayan bir oturum.

    Aynı oturumda CSRF token ve cookie'ler tekrar kullanılır. Rate-limit
    yendiğinde `refresh_session()` çağırarak yeni bir cookie alabilirsin.

    Birden fazla IP'yi paralel kullanmak istiyorsan her thread için ayrı bir
    `IGChecker` örneği yarat ve farklı bir `proxy` ver.
    """

    def __init__(
        self,
        proxy: Optional[str] = None,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 15.0,
    ) -> None:
        self.proxy = proxy
        self.user_agent = user_agent
        self.timeout = timeout
        self.session = self._new_session()
        self._csrf: Optional[str] = None

    # -- session lifecycle -------------------------------------------------

    def _new_session(self) -> requests.Session:
        s = requests.Session()
        if self.proxy:
            s.proxies.update({"http": self.proxy, "https": self.proxy})
        s.headers.update(
            {
                "User-Agent": self.user_agent,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "*/*",
            }
        )
        return s

    def refresh_session(self) -> None:
        """Cookie ve csrftoken'ı yeniden al. Rate-limit sonrası kullanışlıdır."""
        self.session = self._new_session()
        self._csrf = None
        self._bootstrap()

    def _bootstrap(self) -> None:
        """Signup sayfasını çekip csrftoken'ı al."""
        resp = self.session.get(SIGNUP_PAGE_URL, timeout=self.timeout)
        resp.raise_for_status()
        # Token önce cookie'de bulunur.
        token = self.session.cookies.get("csrftoken")
        if not token:
            # Bazı durumlarda HTML'den çıkarmak gerekebiliyor.
            m = re.search(r'csrf_token":"([^"]+)"', resp.text)
            if m:
                token = m.group(1)
        if not token:
            raise RuntimeError(
                "csrftoken alınamadı. Proxy/IP bloklanmış olabilir."
            )
        self._csrf = token

    # -- public API --------------------------------------------------------

    def check(self, username: str) -> CheckResult:
        """Tek bir kullanıcı adının Instagram'daki durumunu döndür."""
        if not USERNAME_RE.match(username):
            return CheckResult(
                username=username,
                status=UsernameStatus.INVALID_FORMAT,
                message="Geçersiz biçim (yalnızca a-z, 0-9, ., _ kullanılabilir).",
            )

        if not self._csrf:
            self._bootstrap()

        headers = {
            "X-CSRFToken": self._csrf or "",
            "X-Requested-With": "XMLHttpRequest",
            "X-Instagram-AJAX": "1",
            "X-IG-App-ID": IG_APP_ID,
            "Referer": SIGNUP_PAGE_URL,
            "Origin": "https://www.instagram.com",
            "Content-Type": "application/x-www-form-urlencoded",
        }

        # Email/şifre boş bırakılırsa Instagram başka hatalar döndürür ama
        # `errors.username` alanı yine doğru şekilde dolduğundan bizim için
        # yeterli. `dryrun` rolüne yakın bir form_validation_error response'u
        # döner. Aynı email'i tekrar tekrar gönderirsek IG bizi spam olarak
        # işaretliyor; bu yüzden her istekte rastgele bir email kullanıyoruz.
        rand_email = f"user{random.randint(10**6, 10**9)}@example.com"
        data = {
            "email": rand_email,
            "username": username,
            "first_name": "",
            "opt_into_one_tap": "false",
        }

        try:
            resp = self.session.post(
                ATTEMPT_URL,
                data=data,
                headers=headers,
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            return CheckResult(
                username=username,
                status=UsernameStatus.UNKNOWN,
                message=f"İstek hatası: {e}",
            )

        # Rate limit / spam blok
        if resp.status_code == 429:
            return _rate_limited(username, resp)
        try:
            payload = resp.json()
        except ValueError:
            return CheckResult(
                username=username,
                status=UsernameStatus.UNKNOWN,
                message=f"JSON çözümlenemedi (HTTP {resp.status_code}).",
            )

        if payload.get("message") == "feedback_required":
            return _rate_limited(username, resp, payload)

        return _interpret(username, payload)


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------


def _rate_limited(
    username: str, resp: requests.Response, payload: Optional[dict] = None
) -> CheckResult:
    return CheckResult(
        username=username,
        status=UsernameStatus.RATE_LIMITED,
        code=str(resp.status_code),
        message=(
            (payload or {}).get("feedback_message")
            or f"Rate limit (HTTP {resp.status_code})."
        ),
        raw=payload,
    )


def _interpret(username: str, payload: dict) -> CheckResult:
    """Instagram'ın signup attempt cevabından kullanıcı adı durumunu çıkar."""
    errors = (payload or {}).get("errors") or {}
    user_errors = errors.get("username") or []
    if not user_errors:
        # `username` için hata yoksa nick boştur.
        return CheckResult(
            username=username,
            status=UsernameStatus.AVAILABLE,
            raw=payload,
        )

    # Genelde tek bir hata gelir.
    first = user_errors[0] if isinstance(user_errors, list) else user_errors
    code = (first or {}).get("code")
    message = (first or {}).get("message")

    if code == "username_is_taken":
        status = UsernameStatus.TAKEN
    elif code == "username_invalid":
        status = UsernameStatus.RESERVED
    elif code == "username_invalid_substring":
        status = UsernameStatus.BLOCKED_TERM
    else:
        status = UsernameStatus.UNKNOWN

    return CheckResult(
        username=username,
        status=status,
        code=code,
        message=message,
        raw=payload,
    )


# ---------------------------------------------------------------------------
# Modülün manuel test edilmesi için CLI
# ---------------------------------------------------------------------------


def _main() -> None:  # pragma: no cover - manuel test
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("usernames", nargs="+")
    parser.add_argument("--proxy")
    parser.add_argument("--delay", type=float, default=2.0)
    args = parser.parse_args()

    checker = IGChecker(proxy=args.proxy)
    for u in args.usernames:
        r = checker.check(u)
        print(
            json.dumps(
                {
                    "username": r.username,
                    "status": r.status.value,
                    "code": r.code,
                    "message": r.message,
                },
                ensure_ascii=False,
            )
        )
        sys.stdout.flush()
        time.sleep(args.delay)


if __name__ == "__main__":  # pragma: no cover
    _main()
