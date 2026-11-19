import asyncio
import ipaddress
import math
import socket
import ssl
from dataclasses import dataclass
from time import perf_counter, time

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from yarl import URL

TIMEOUT_ERROR = 'Таймаут запроса'
DNS_ERROR = 'Не удалось определить IP-адрес хоста'
BLOCKED_ADDRESS = 'Локальные и приватные адреса запрещены'


class BlockedAddress(aiohttp.ClientError):
    pass


@dataclass(frozen=True, slots=True)
class CheckResult:
    status: str
    code: int | None
    latency_ms: int | None
    ssl_days_left: int | None
    error: str | None = None


def is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip.sixtofour or ip.teredo:
            return False
    return ip.is_global and not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
    )


def parse_target(value: str) -> URL:
    if not value.lower().startswith(("http://", "https://")) or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("Введите полный URL, начинающийся с http:// или https://.")
    try:
        target = URL(value)
        if not target.raw_host or target.raw_user is not None or target.port is None or not 1 <= target.port <= 65535:
            raise ValueError
    except (ValueError, UnicodeError):
        raise ValueError("Укажите корректный хост и порт. Логин и пароль в URL запрещены.") from None
    try:
        ipaddress.ip_address(target.raw_host)
    except ValueError:
        return target.with_fragment(None)
    if not is_public(target.raw_host):
        raise ValueError(BLOCKED_ADDRESS)
    return target.with_fragment(None)


class PublicResolver(AbstractResolver):
    def __init__(self) -> None:
        self._addresses: dict[tuple[str, int], list[tuple]] = {}

    async def addresses(self, host: str, port: int) -> list[tuple]:
        key = (host, port)
        if key not in self._addresses:
            entries = await asyncio.get_running_loop().getaddrinfo(
                host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
            )
            if any(not is_public(entry[4][0]) for entry in entries):
                raise BlockedAddress(BLOCKED_ADDRESS)
            self._addresses[key] = entries
        return self._addresses[key]

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_UNSPEC) -> list[ResolveResult]:
        entries = await self.addresses(host, port)
        return [
            ResolveResult(
                hostname=host, host=sockaddr[0], port=sockaddr[1], family=af,
                proto=proto, flags=socket.AI_NUMERICHOST,
            )
            for af, _, proto, _, sockaddr in entries
            if family == socket.AF_UNSPEC or af == family
        ]

    async def close(self) -> None:
        self._addresses.clear()


async def _validate_redirect(_session, _context, params: aiohttp.TraceRequestRedirectParams) -> None:
    location = params.response.headers.get("Location") or params.response.headers.get("URI")
    if location:
        target = params.url.join(URL(location))
        if target.scheme in {"http", "https"}:
            try:
                parse_target(str(target))
            except ValueError as exc:
                if str(exc) == BLOCKED_ADDRESS:
                    raise BlockedAddress(BLOCKED_ADDRESS) from exc
                raise


def _public_socket(addr_info: tuple) -> socket.socket:
    family, kind, proto, _, sockaddr = addr_info
    if not is_public(sockaddr[0]):
        raise BlockedAddress(BLOCKED_ADDRESS)
    return socket.socket(family, kind, proto)


def _ssl_days(host: str, addresses: list[tuple], timeout: int) -> int | None:
    try:
        context = ssl.create_default_context()
        deadline = perf_counter() + timeout
        for address in addresses:
            try:
                with _public_socket(address) as connection:
                    connection.settimeout(max(0.001, deadline - perf_counter()))
                    connection.connect(address[4])
                    connection.settimeout(max(0.001, deadline - perf_counter()))
                    with context.wrap_socket(connection, server_hostname=host) as tls:
                        expiry = tls.getpeercert()["notAfter"]
                        return math.floor((ssl.cert_time_to_seconds(expiry) - time()) / 86400)
            except (OSError, ValueError, KeyError, BlockedAddress):
                if perf_counter() >= deadline:
                    break
    except (OSError, ValueError):
        pass
    return None


async def _http(target: URL, timeout: float, resolver: PublicResolver, started: float) -> tuple[int | None, int | None, str | None]:
    try:
        trace = aiohttp.TraceConfig()
        trace.on_request_redirect.append(_validate_redirect)
        connector = aiohttp.TCPConnector(resolver=resolver, socket_factory=_public_socket)
        async with aiohttp.ClientSession(
            connector=connector, timeout=aiohttp.ClientTimeout(total=timeout),
            headers={"User-Agent": "SiteWatch/1.0 (availability monitor)"},
            cookie_jar=aiohttp.DummyCookieJar(), trace_configs=[trace],
        ) as session:
            async with session.get(target, allow_redirects=True) as response:
                return response.status, round((perf_counter() - started) * 1000), None
    except BlockedAddress:
        return None, None, BLOCKED_ADDRESS
    except asyncio.TimeoutError:
        return None, None, TIMEOUT_ERROR
    except aiohttp.ClientConnectorCertificateError:
        return None, None, "Ошибка проверки TLS-сертификата"
    except aiohttp.TooManyRedirects:
        return None, None, "Слишком много перенаправлений"
    except (aiohttp.ClientError, ValueError):
        return None, None, "Ошибка HTTP-соединения"


async def _check_ssl(target: URL, addresses: list[tuple], timeout: int) -> int | None:
    if target.scheme != "https":
        return None
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_ssl_days, target.raw_host, addresses, timeout), timeout
        )
    except asyncio.TimeoutError:
        return None


async def check_target(url: str, timeout: int) -> CheckResult:
    try:
        target = parse_target(url)
    except ValueError as exc:
        return CheckResult("down", None, None, None, str(exc))
    started = perf_counter()
    resolver = PublicResolver()
    try:
        addresses = await asyncio.wait_for(resolver.addresses(target.raw_host, target.port), timeout)
        remaining = timeout - (perf_counter() - started)
        if remaining <= 0:
            raise asyncio.TimeoutError
        http, ssl_days = await asyncio.gather(
            _http(target, remaining, resolver, started), _check_ssl(target, addresses, timeout)
        )
    except BlockedAddress:
        return CheckResult("down", None, None, None, BLOCKED_ADDRESS)
    except asyncio.TimeoutError:
        return CheckResult("down", None, None, None, TIMEOUT_ERROR)
    except (OSError, UnicodeError):
        return CheckResult("down", None, None, None, DNS_ERROR)
    finally:
        await resolver.close()
    code, latency, error = http
    status = "up" if code is not None and 200 <= code < 400 else "down"
    return CheckResult(status, code, latency, ssl_days, error)
