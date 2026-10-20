"""Validate addresses before DNS, then allow only publicly routable destinations."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
import ipaddress
import re
import socket
import threading
from urllib.parse import urlsplit, urlunsplit

import httpx
import idna

from .errors import ScrapingError

_DNS_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pricewatch-dns")
_DNS_SLOTS = threading.BoundedSemaphore(4)


@dataclass(frozen=True)
class ValidatedURL:
    url: str
    scheme: str
    host: str
    port: int
    origin: str


def is_public_ip(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return (
        address.is_global
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
        and not address.is_loopback
        and not address.is_link_local
        and not getattr(address, "ipv4_mapped", None)
        and not getattr(address, "sixtofour", None)
        and not getattr(address, "teredo", None)
        and address not in ipaddress.ip_network("64:ff9b::/96")
    )


def _canonical_host(host):
    host = host.rstrip(".").lower()
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        try:
            result = idna.encode(host, uts46=True, std3_rules=True).decode("ascii")
        except (idna.IDNAError, UnicodeError):
            raise ScrapingError("invalid_url", "Некорректное имя сайта.") from None
        if len(result) > 253 or not re.fullmatch(r"[a-z0-9.-]+", result):
            raise ScrapingError("invalid_url", "Некорректное имя сайта.")
        return result


def validate_url(raw, allowed_hosts):
    if not isinstance(raw, str) or not raw or len(raw) > 2048:
        raise ScrapingError("invalid_url", "URL должен быть непустой строкой до 2048 символов.")
    if any(ord(char) <= 32 or ord(char) == 127 for char in raw) or "\\" in raw or "#" in raw:
        raise ScrapingError("invalid_url", "URL содержит недопустимые символы или фрагмент.")
    try:
        parsed = urlsplit(raw)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError
        if parsed.username is not None or parsed.password is not None or "%" in parsed.netloc:
            raise ValueError
        if parsed.netloc.endswith(":"):
            raise ValueError
        port = parsed.port
        scheme = parsed.scheme.lower()
        default_port = 443 if scheme == "https" else 80
        if port is not None and port != default_port:
            raise ValueError
        host = _canonical_host(parsed.hostname)
    except (ValueError, UnicodeError):
        raise ScrapingError("invalid_url", "Допустим только HTTP(S) URL без логина и нестандартного порта.") from None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise ScrapingError("unsafe_address", "Внутренние адреса запрещены.")
    else:
        if not is_public_ip(host):
            raise ScrapingError("unsafe_address", "Внутренние и служебные IP-адреса запрещены.")
    allowed = {_canonical_host(item) for item in allowed_hosts}
    if host not in allowed:
        raise ScrapingError("host_not_allowed", "Сайт отсутствует в разрешённом списке.")
    authority = f"[{host}]" if ":" in host else host
    origin = f"{scheme}://{authority}"
    try:
        canonical = str(httpx.URL(urlunsplit((scheme, authority, parsed.path or "/", parsed.query, ""))))
    except httpx.InvalidURL:
        raise ScrapingError("invalid_url", "Некорректный URL страницы.") from None
    return ValidatedURL(canonical, scheme, host, default_port, origin)


def resolve_public_ips(host, port, timeout=5.0):
    """A bounded resolver prevents slow OS DNS from blocking the caller indefinitely."""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not is_public_ip(host):
            raise ScrapingError("unsafe_address", "Запрещённый IP-адрес.")
        return (str(literal),)
    if not _DNS_SLOTS.acquire(blocking=False):
        raise ScrapingError("dns_error", "DNS resolver занят; повторите позднее.")
    future = _DNS_POOL.submit(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    future.add_done_callback(lambda _: _DNS_SLOTS.release())
    try:
        records = future.result(timeout=max(0.001, timeout))
    except FutureTimeout:
        future.cancel()
        raise ScrapingError("timeout", "Превышено время разрешения DNS.") from None
    except OSError:
        raise ScrapingError("dns_error", "Не удалось определить адрес сайта.") from None
    addresses = []
    for family, _, _, _, sockaddr in records:
        if family not in {socket.AF_INET, socket.AF_INET6}:
            raise ScrapingError("dns_error", "DNS вернул неподдерживаемый адрес.")
        address = sockaddr[0]
        if not is_public_ip(address):
            raise ScrapingError("unsafe_address", "DNS сайта содержит внутренний или служебный адрес.")
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise ScrapingError("dns_error", "DNS не вернул адрес сайта.")
    return tuple(addresses)
