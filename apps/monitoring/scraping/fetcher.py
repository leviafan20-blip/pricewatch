"""Bounded anonymous HTML loading; all requests, including robots, use one policy."""

import codecs
from dataclasses import dataclass
from email.message import Message
from email.utils import parsedate_to_datetime
import math
import re
import time
from urllib.parse import urljoin
import zlib

from django.utils import timezone
from django.db import DatabaseError
import httpx

from .errors import ScrapingError
from .rates import DomainRateLimiter
from . import robots
from .safety import resolve_public_ips, validate_url
from .transport import PinnedTransport


@dataclass(frozen=True)
class FetchPolicy:
    allowed_hosts: tuple[str, ...] = ()
    max_bytes: int = 2 * 1024 * 1024
    robots_max_bytes: int = 512 * 1024
    max_redirects: int = 5
    connect_timeout: float = 5
    read_timeout: float = 10
    total_timeout: float = 45
    min_interval: float = 10
    robots_ttl: float = 21600
    user_agent: str = "PriceWatchBot/1.0"

    def __post_init__(self):
        if not isinstance(self.allowed_hosts, tuple):
            raise ValueError("allowed_hosts must be a tuple of exact hostnames")
        for value in (self.max_bytes, self.robots_max_bytes):
            if not isinstance(value, int) or not 0 < value <= 10 * 1024 * 1024:
                raise ValueError("Response limits must be between 1 byte and 10 MiB")
        if not isinstance(self.max_redirects, int) or not 0 <= self.max_redirects <= 10:
            raise ValueError("Invalid redirect limit")
        for value in (self.connect_timeout, self.read_timeout, self.total_timeout):
            if not math.isfinite(value) or not 0 < value <= 120:
                raise ValueError("Timeouts must be finite and between 0 and 120 seconds")
        if not math.isfinite(self.min_interval) or not 0.01 <= self.min_interval <= 86400:
            raise ValueError("Invalid domain interval")
        if not math.isfinite(self.robots_ttl) or not 0 < self.robots_ttl <= 86400:
            raise ValueError("Invalid robots cache TTL")
        if not re.fullmatch(r"PriceWatchBot/[0-9.]+", self.user_agent):
            raise ValueError("Use the PriceWatchBot product token")


@dataclass(frozen=True)
class FetchedPage:
    url: str
    html: str
    status_code: int
    bytes_received: int


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ScrapingError("timeout", "Превышено общее время загрузки страницы.")
    return remaining


def _decode(body, content_type, *, robots_file=False):
    if robots_file:
        encoding = "utf-8-sig"
    else:
        message = Message()
        message["content-type"] = content_type
        encoding = message.get_content_charset()
        if not encoding:
            meta = re.search(rb"<meta\b[^>]*charset\s*=\s*[\"']?\s*([a-zA-Z0-9_-]+)", body[:8192], re.I)
            encoding = meta[1].decode("ascii") if meta else "utf-8-sig"
    try:
        codecs.lookup(encoding)
        return body.decode(encoding, errors="strict")
    except (LookupError, UnicodeError):
        raise ScrapingError("invalid_encoding", "Не удалось декодировать страницу в заявленной кодировке.") from None


def _read_body(response, limit, deadline):
    length = response.headers.get("content-length")
    if length is not None:
        if not length.isascii() or not length.isdigit() or len(length) > 12:
            raise ScrapingError("invalid_response", "Некорректный Content-Length.")
        if int(length) > limit:
            raise ScrapingError("response_too_large", "Ответ сайта превышает ограничение размера.")
    encoding = response.headers.get("content-encoding", "identity").lower().strip()
    if encoding not in {"identity", "gzip", "deflate"}:
        raise ScrapingError("unsupported_encoding", "Неподдерживаемое сжатие ответа.")
    decompressor = None
    if encoding != "identity":
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
    output, wire_size = bytearray(), 0
    try:
        for chunk in response.iter_raw():
            _remaining(deadline)
            wire_size += len(chunk)
            if wire_size > limit:
                raise ScrapingError("response_too_large", "Ответ сайта превышает ограничение размера.")
            if decompressor is not None:
                # max_length bounds transient memory too; iter_bytes() would decode unbounded chunks.
                chunk = decompressor.decompress(chunk, limit - len(output) + 1)
                if decompressor.unused_data:
                    raise ScrapingError("invalid_response", "Несколько сжатых потоков не поддерживаются.")
            output.extend(chunk)
            if len(output) > limit or (decompressor is not None and decompressor.unconsumed_tail):
                raise ScrapingError("response_too_large", "Распакованный ответ превышает ограничение размера.")
        if decompressor is not None and not decompressor.eof:
            raise ScrapingError("invalid_response", "Сжатый ответ оборван.")
    except zlib.error:
        raise ScrapingError("invalid_response", "Некорректный сжатый ответ сайта.") from None
    _remaining(deadline)
    return bytes(output)


def _retry_after(headers):
    raw = headers.get("retry-after", "")
    try:
        if raw.isascii() and raw.isdigit() and len(raw) <= 9:
            return min(float(raw), 315360000)
        when = parsedate_to_datetime(raw)
        if when.tzinfo is None:
            return 60.0
        return min(max(0.0, (when - timezone.now()).total_seconds()), 315360000)
    except (ValueError, TypeError, OverflowError):
        return 60.0


def _robots_ttl(headers, maximum):
    control = headers.get("cache-control", "").lower()
    if "no-store" in control or "no-cache" in control:
        return 0
    found = re.search(r"(?:^|,)\s*max-age\s*=\s*\"?(\d{1,10})\b", control)
    return min(maximum, int(found[1])) if found else maximum


class PageLoader:
    def __init__(self, policy=None, *, transport=None, limiter=None):
        self.policy = policy or FetchPolicy()
        self.limiter = limiter or DomainRateLimiter()
        self.client = httpx.Client(
            transport=transport if transport is not None else PinnedTransport(),
            trust_env=False, follow_redirects=False,
            headers={"User-Agent": self.policy.user_agent, "Accept-Encoding": "gzip, deflate"},
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.client.close()

    def fetch(self, url):
        deadline = time.monotonic() + self.policy.total_timeout
        try:
            final, status, headers, body = self._walk(url, deadline, robots_file=False)
        except DatabaseError:
            raise ScrapingError("rate_limit_unavailable", "Не удалось проверить общий лимит запросов; загрузка остановлена.") from None
        if status != 200:
            raise ScrapingError("http_error", "Сайт не вернул доступную страницу.", details={"status_code": status})
        return FetchedPage(final.url, _decode(body, headers.get("content-type", "")), status, len(body))

    def _robots(self, target, deadline):
        key = (target.origin, self.policy.user_agent)
        rules = robots.get_cached(key)
        if rules is None:
            try:
                _, status, headers, body = self._walk(target.origin + "/robots.txt", deadline, robots_file=True)
                if status in {404, 410}:
                    rules = robots.RobotsRules()
                elif status in {401, 403}:
                    rules = robots.RobotsRules(deny_all=True)
                elif status == 200:
                    text = _decode(body, "", robots_file=True)
                    noncomments = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
                    if re.search(r"<(?:!doctype\b|/?(?:html|head|body|script)\b|!--)", noncomments, re.I):
                        raise ScrapingError("robots_unavailable", "Вместо robots.txt получена HTML-страница.")
                    rules = robots.parse_robots(text, self.policy.user_agent)
                else:
                    raise ScrapingError("robots_unavailable", "Правила robots.txt временно недоступны.")
                # The just-completed robots request must obey the newly learned delay too.
                if rules.crawl_delay and hasattr(self.limiter, "defer"):
                    self.limiter.defer(target.host, rules.crawl_delay)
                robots.cache_rules(key, rules, _robots_ttl(headers, self.policy.robots_ttl))
            except ScrapingError as error:
                failure = ScrapingError("robots_unavailable", "Не удалось безопасно получить robots.txt.", details={"reason": error.code})
                robots.cache_rules(key, failure, min(60, self.policy.robots_ttl))
                raise failure from None
        if isinstance(rules, ScrapingError):
            raise ScrapingError(rules.code, rules.message, details=rules.details.copy())
        if not rules.can_fetch(target.url):
            raise ScrapingError("robots_denied", "robots.txt запрещает проверку этой страницы.")
        return rules

    def _walk(self, url, deadline, *, robots_file):
        seen = set()
        for hop in range(self.policy.max_redirects + 1):
            target = validate_url(url, self.policy.allowed_hosts)
            if target.url in seen:
                raise ScrapingError("redirect_limit", "Обнаружен цикл перенаправлений.")
            seen.add(target.url)
            rules = None if robots_file else self._robots(target, deadline)
            interval = max(self.policy.min_interval, rules.crawl_delay if rules else 0)
            # DNS resolution and every HTTP hop have their own fresh validation.
            addresses = resolve_public_ips(target.host, target.port, timeout=min(self.policy.connect_timeout, _remaining(deadline)))
            while True:
                try:
                    with self.limiter.slot(target.host, interval, _remaining(deadline) + 5):
                        result = self._send(target, addresses[0], deadline, robots_file)
                    break
                except ScrapingError as error:
                    if error.code != "rate_limited":
                        raise
                    delay = float(error.details.get("retry_after_seconds", 1))
                    if delay >= _remaining(deadline):
                        raise
                    time.sleep(max(0.001, delay))
            status, headers, body = result
            if status in {301, 302, 303, 307, 308}:
                if hop >= self.policy.max_redirects:
                    raise ScrapingError("redirect_limit", "Превышен предел перенаправлений.")
                location = headers.get("location")
                if not location:
                    raise ScrapingError("invalid_redirect", "Перенаправление не содержит Location.")
                try:
                    url = urljoin(target.url, location)
                except ValueError:
                    raise ScrapingError("invalid_redirect", "Сайт вернул некорректный адрес перенаправления.") from None
                destination = validate_url(url, self.policy.allowed_hosts)
                if target.scheme == "https" and destination.scheme != "https":
                    raise ScrapingError("unsafe_redirect", "Переход с HTTPS на HTTP запрещён.")
                continue
            return target, status, headers, body
        raise ScrapingError("redirect_limit", "Превышен предел перенаправлений.")

    def _send(self, target, ip, deadline, robots_file):
        remaining = _remaining(deadline)
        timeout = httpx.Timeout(
            min(self.policy.read_timeout, remaining),
            connect=min(self.policy.connect_timeout, remaining),
        )
        # Never carry cookies between requests or sites. No credentials/custom headers are accepted.
        self.client.cookies.clear()
        try:
            with self.client.stream(
                "GET", target.url, timeout=timeout,
                headers={"Accept": "text/plain" if robots_file else "text/html, application/xhtml+xml"},
                extensions={"pinned_ip": ip, "deadline": deadline},
            ) as response:
                status, headers = response.status_code, response.headers
                if status in {429, 503}:
                    delay = _retry_after(headers)
                    if hasattr(self.limiter, "defer"):
                        self.limiter.defer(target.host, delay)
                if status != 200:
                    return status, headers, b""
                media_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
                allowed_types = {"", "text/plain", "text/x-robots-txt"} if robots_file else {"text/html", "application/xhtml+xml"}
                if media_type not in allowed_types:
                    raise ScrapingError("unsupported_content_type", "Сайт вернул неподдерживаемый тип содержимого.")
                limit = self.policy.robots_max_bytes if robots_file else self.policy.max_bytes
                body = _read_body(response, limit, deadline)
                return status, headers, body
        except httpx.TimeoutException:
            raise ScrapingError("timeout", "Сайт не ответил за отведённое время.") from None
        except httpx.RequestError:
            raise ScrapingError("network_error", "Ошибка соединения с сайтом или проверки TLS.") from None
