"""Shared HTTP helpers: retries with backoff, streaming downloads."""

from __future__ import annotations

import ssl
import time
from pathlib import Path

import certifi
import httpx

USER_AGENT = "caramelo/0.1 (+https://github.com/gabriel-laet/caramelo)"

RETRY_STATUSES = {429, 500, 502, 503, 504}


def get(url: str, *, params: dict | None = None, headers: dict | None = None,
        retries: int = 4, timeout: float = 60.0) -> httpx.Response:
    merged = {"User-Agent": USER_AGENT}
    if headers:
        merged.update(headers)
    delay = 2.0
    last: Exception | None = None
    for _ in range(retries + 1):
        try:
            resp = httpx.get(url, params=params, headers=merged,
                             timeout=timeout, follow_redirects=True)
            if resp.status_code in RETRY_STATUSES:
                last = httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}", request=resp.request, response=resp)
            elif resp.is_error:
                # other 4xx won't change on retry (bad key, bad request, 404)
                raise RuntimeError(f"GET {url} failed: HTTP {resp.status_code}") \
                    from httpx.HTTPStatusError(f"HTTP {resp.status_code}",
                                               request=resp.request,
                                               response=resp)
            else:
                return resp
        except httpx.TransportError as exc:
            last = exc
        time.sleep(delay)
        delay *= 2
    raise RuntimeError(f"GET {url} failed after {retries + 1} attempts") from last


def ssl_with_intermediate(issuer_url: str) -> ssl.SSLContext:
    """Verifying SSL context for servers that omit their intermediate cert.

    Some gov.br hosts (e.g. download.inep.gov.br) serve only the leaf
    certificate. Browsers repair that via the AIA "CA Issuers" URL; we do the
    same explicitly: fetch the intermediate (DER) and add it to the default
    trust store. Verification stays on — the intermediate must still chain
    to a trusted root, so a tampered download simply fails the handshake.
    """
    der = httpx.get(issuer_url, headers={"User-Agent": USER_AGENT},
                    timeout=30.0, follow_redirects=True).content
    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.load_verify_locations(cadata=der)
    return ctx


def download(url: str, dest: Path, *, skip_if_exists: bool = True,
             retries: int = 4, verify: ssl.SSLContext | bool = True) -> Path:
    if skip_if_exists and dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    delay = 2.0
    last: Exception | None = None
    for _ in range(retries + 1):
        try:
            with httpx.stream("GET", url, headers={"User-Agent": USER_AGENT},
                              timeout=300.0, follow_redirects=True,
                              verify=verify) as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_bytes(chunk_size=1 << 20):
                        fh.write(chunk)
            tmp.rename(dest)
            return dest
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            last = exc
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"download {url} failed after {retries + 1} attempts") from last
