"""HTTPX transport using public HTTPcore APIs for pinned IPs and socket deadlines.

Each request owns its pool: no reused connection can escape a fresh DNS decision.
Host and TLS SNI stay the original hostname; TCP sees only the validated IP.
"""

from contextlib import contextmanager
import ssl
import time

import certifi
import httpcore
import httpx

from .errors import ScrapingError
from .safety import is_public_ip


@contextmanager
def _map_errors(request):
    try:
        yield
    except httpcore.TimeoutException:
        raise httpx.TimeoutException("Network deadline exceeded", request=request) from None
    except (httpcore.NetworkError, httpcore.ProtocolError):
        raise httpx.TransportError("Network request failed", request=request) from None


class DeadlineStream(httpcore.NetworkStream):
    def __init__(self, stream, deadline):
        self.stream = stream
        self.deadline = deadline

    def _timeout(self, timeout):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise httpcore.ReadTimeout("Total deadline exceeded")
        return min(timeout, remaining) if timeout is not None else remaining

    def read(self, max_bytes, timeout=None):
        return self.stream.read(max_bytes, timeout=self._timeout(timeout))

    def write(self, buffer, timeout=None):
        return self.stream.write(buffer, timeout=self._timeout(timeout))

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        stream = self.stream.start_tls(
            ssl_context, server_hostname=server_hostname, timeout=self._timeout(timeout)
        )
        return DeadlineStream(stream, self.deadline)

    def get_extra_info(self, info):
        return self.stream.get_extra_info(info)

    def close(self):
        self.stream.close()


class DeadlineBackend(httpcore.NetworkBackend):
    def __init__(self, backend, deadline, pinned_ip):
        self.backend = backend
        self.deadline = deadline
        self.pinned_ip = pinned_ip

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if host != self.pinned_ip or not is_public_ip(host):
            raise ScrapingError("unsafe_address", "Соединение с непроверенным адресом запрещено.")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise httpcore.ConnectTimeout("Total deadline exceeded")
        stream = self.backend.connect_tcp(
            host, port,
            timeout=min(timeout, remaining) if timeout is not None else remaining,
            local_address=local_address, socket_options=socket_options,
        )
        return DeadlineStream(stream, self.deadline)


class _ResponseStream(httpx.SyncByteStream):
    def __init__(self, response, pool, request):
        self.response, self.pool, self.request = response, pool, request

    def __iter__(self):
        with _map_errors(self.request):
            yield from self.response.iter_stream()

    def close(self):
        try:
            self.response.close()
        finally:
            self.pool.close()


class PinnedTransport(httpx.BaseTransport):
    def __init__(self, network_backend=None):
        self.network_backend = network_backend or httpcore.SyncBackend()
        # Explicit CA store: proxy and certificate environment variables are not used.
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())

    def handle_request(self, request):
        ip = request.extensions.get("pinned_ip")
        deadline = request.extensions.get("deadline")
        if not isinstance(ip, str) or not is_public_ip(ip) or not isinstance(deadline, (int, float)):
            raise ScrapingError("unsafe_address", "Запрос не содержит проверенный IP и срок выполнения.")
        hostname = request.url.raw_host.decode("ascii")
        url = request.url.copy_with(host=ip)
        headers = httpx.Headers(request.headers)
        headers["Host"] = f"[{hostname}]" if ":" in hostname else hostname
        extensions = {**request.extensions, "sni_hostname": hostname}
        pool = httpcore.ConnectionPool(
            ssl_context=self.ssl_context,
            network_backend=DeadlineBackend(self.network_backend, deadline, ip),
            max_connections=1, max_keepalive_connections=0, retries=0, http2=False,
        )
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(scheme=url.raw_scheme, host=url.raw_host, port=url.port, target=url.raw_path),
            headers=headers.raw, content=request.stream, extensions=extensions,
        )
        try:
            with _map_errors(request):
                response = pool.handle_request(core_request)
        except BaseException:
            pool.close()
            raise
        return httpx.Response(
            response.status, headers=response.headers,
            stream=_ResponseStream(response, pool, request), extensions=response.extensions,
        )
