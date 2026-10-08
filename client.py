"""OctoPrint REST calls for one configured origin.

Success for a job command is HTTP 204 with an empty body. That accepts the
command. It does not mean the printer state has changed.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

JSON_TIMEOUT_SECONDS = 10.0
CONTROL_TIMEOUT_SECONDS = 60.0
SNAPSHOT_TIMEOUT_SECONDS = 15.0
JSON_MAX_BYTES = 1_000_000
SNAPSHOT_MAX_BYTES = 5_000_000
MAX_REDIRECTS = 2

PERMISSION_ERROR = (
    "You don't have the permission to access the requested resource. "
    "It is either read-protected or not readable by the server."
)
MAY_HAVE_REACHED = (
    "The command may still have reached the printer and take effect later. "
    "Read the state before sending it again."
)
_BAD_URL = (
    "OCTOPRINT_URL must be one http or https origin: scheme, host, and an optional port from 0 to 65535. "
    "Nothing was requested."
)
_BAD_URL_NEXT = "Set OCTOPRINT_URL to a base such as http://printer.example:5000. Nothing was requested."


def _timeout(method: str) -> ApiError:
    # A read that never answered did not send a move. A control POST may have.
    next_step = MAY_HAVE_REACHED if method == "POST" else "This was a read. Nothing was saved."
    return ApiError("timeout", "OctoPrint did not answer before the time limit.", next_step)


class ApiError(Exception):
    def __init__(self, code: str, message: str, next_step: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.next_step = next_step


@dataclass(frozen=True)
class Origin:
    value: str
    scheme: str
    hostname: str
    port: int | None

    def same_origin(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != self.scheme or (parts.hostname or "").lower() != self.hostname.lower():
            return False
        return _port(parts) == self.port

    def same_host(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in {"http", "https"} or parts.username or parts.password:
            return False
        return (parts.hostname or "").lower() == self.hostname.lower()


def _bad_url() -> ApiError:
    return ApiError("bad_url", _BAD_URL, _BAD_URL_NEXT)


def parse_origin(url: str) -> Origin:
    text = (url or "").strip()
    try:
        parts = urllib.parse.urlsplit(text)
        hostname = parts.hostname
        port = _port(parts)
    except ValueError:
        raise _bad_url() from None
    if parts.scheme not in {"http", "https"} or not hostname:
        raise _bad_url()
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in {"", "/"}:
        raise ApiError(
            "bad_url",
            "OCTOPRINT_URL must not include a username, password, path, query, or fragment.",
            "Use only the scheme, host, and port.",
        )
    if "[" in parts.netloc or "]" in parts.netloc:
        try:
            ipaddress.IPv6Address(hostname)
        except ValueError:
            raise _bad_url() from None
    return Origin(f"{parts.scheme}://{parts.netloc}", parts.scheme, hostname, port)


def _port(parts: urllib.parse.SplitResult) -> int | None:
    if parts.port is not None:
        return parts.port
    return 443 if parts.scheme == "https" else 80


def scrub(text: str, api_key: str) -> str:
    if api_key and api_key in text:
        return text.replace(api_key, "[redacted]")
    return text


Transport = Callable[..., tuple[int, dict[str, str], bytes]]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib follows redirects before the caller can check the host. This stops that."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def default_transport(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float, cap: int) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = response.read(cap + 1)
            return response.status, {k.lower(): v for k, v in response.headers.items()}, payload
    except urllib.error.HTTPError as error:
        payload = error.read(cap + 1)
        return error.code, {k.lower(): v for k, v in error.headers.items()}, payload
    except TimeoutError as error:
        raise _timeout(method) from error
    except urllib.error.URLError as error:
        reason = getattr(error, "reason", error)
        if isinstance(reason, TimeoutError):
            raise _timeout(method) from error
        if method == "POST" and isinstance(reason, ConnectionError):
            raise ApiError(
                "disconnected",
                "The connection closed after the command was sent.",
                MAY_HAVE_REACHED,
            ) from error
        raise ApiError(
            "network",
            f"Could not reach OctoPrint ({type(reason).__name__}).",
            "Check OCTOPRINT_URL. Nothing was recorded as a print state.",
        ) from error


class OctoPrint:
    def __init__(self, origin: Origin, api_key: str, transport: Transport | None = None) -> None:
        if not api_key.strip():
            raise ApiError(
                "no_key",
                "OCTOPRINT_API_KEY is empty, so nothing was requested.",
                "Set the API key. If OctoPrint access control is off, a missing key is treated by OctoPrint as an admin, so this plugin refuses to call it.",
            )
        self.origin = origin
        self.api_key = api_key
        self.transport = transport or default_transport

    def get_json(self, path: str, timeout: float = JSON_TIMEOUT_SECONDS) -> tuple[int, Any]:
        status, _headers, payload = self._exchange("GET", path, None, timeout, JSON_MAX_BYTES)
        return status, self._decode(status, payload, write=False)

    def post_json(self, path: str, body: dict[str, Any], timeout: float) -> tuple[int, Any]:
        status, _headers, payload = self._exchange(
            "POST", path, json.dumps(body).encode(), timeout, JSON_MAX_BYTES,
        )
        if status == 204:
            return status, None
        return status, self._decode(status, payload, write=True)

    def get_bytes(self, url: str, cap: int, timeout: float = SNAPSHOT_TIMEOUT_SECONDS) -> tuple[int, dict[str, str], bytes]:
        if not self.origin.same_host(url):
            raise ApiError(
                "snapshot_host",
                "The snapshot URL is not on the same host as OCTOPRINT_URL, so it was not downloaded.",
                "Set OCTOPRINT_SNAPSHOT_URL to an http(s) URL on that host. Another port on the same host is allowed.",
            )
        status, headers, payload = self._exchange_url(
            "GET", url, None, timeout, cap, redirects=0, api=False, send_key=self.origin.same_origin(url),
        )
        return status, headers, payload

    def _exchange(self, method: str, path: str, body: bytes | None, timeout: float, cap: int) -> tuple[int, dict[str, str], bytes]:
        if path.startswith("/api/settings"):
            raise ApiError("settings_blocked", "This plugin does not call /api/settings.", "The settings document can contain the global API key.")
        return self._exchange_url(method, self.origin.value + path, body, timeout, cap, redirects=0, api=True)

    def _exchange_url(self, method: str, url: str, body: bytes | None, timeout: float, cap: int, redirects: int, api: bool, send_key: bool = True) -> tuple[int, dict[str, str], bytes]:
        headers = {"Accept": "application/json"}
        if send_key:
            headers["X-Api-Key"] = self.api_key
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            status, response_headers, payload = self.transport(method, url, headers, body, timeout, cap)
        except ApiError:
            raise
        except Exception as error:
            # After the status line, a cut-off body is an HTTPException, not a refusal to connect.
            if method == "POST" and isinstance(error, (ConnectionError, http.client.HTTPException)):
                raise ApiError(
                    "disconnected",
                    "The connection closed after the command was sent.",
                    MAY_HAVE_REACHED,
                ) from error
            raise ApiError(
                "network",
                scrub(f"Could not reach OctoPrint ({type(error).__name__}).", self.api_key),
                "Check OCTOPRINT_URL. Nothing was recorded as a print state.",
            ) from error
        if status in {301, 302, 303, 307, 308}:
            location = response_headers.get("location") or ""
            if redirects >= MAX_REDIRECTS or not location:
                raise ApiError("redirect", "OctoPrint redirected too many times.", "Use the printer origin directly.")
            target = urllib.parse.urljoin(url, location)
            allowed = self.origin.same_origin(target) if api else self.origin.same_host(target)
            if not allowed:
                raise ApiError("redirect", "A redirect left the configured host, so it was not followed.", "Nothing was sent there.")
            return self._exchange_url(
                method if status in {307, 308} else "GET",
                target,
                None if status in {301, 302, 303} else body,
                timeout,
                cap,
                redirects + 1,
                api,
                send_key=send_key and self.origin.same_origin(target),
            )
        # Status is decided before length, so a short 5xx page stays HTTP 500 or 502.
        if status >= 500:
            return status, response_headers, payload
        after_status = MAY_HAVE_REACHED if method == "POST" else "Nothing was saved."
        declared = response_headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > cap:
            raise ApiError("too_large", "The response declared a body over the size cap, so it was discarded.", after_status)
        if len(payload) > cap:
            raise ApiError("too_large", "The response was over the size cap, so it was discarded.", after_status)
        if declared and declared.isdigit() and len(payload) < int(declared):
            raise ApiError("short_body", "The response was shorter than Content-Length, so it was discarded.", after_status)
        return status, response_headers, payload

    def _decode(self, status: int, payload: bytes, *, write: bool) -> Any:
        # Status is decided before JSON parsing, so an HTML 5xx stays a 5xx.
        if status >= 500:
            detail = f"HTTP {status}"
            parsed = _json_or_none(payload)
            if isinstance(parsed, dict) and parsed.get("error"):
                detail = f"HTTP {status}: " + scrub(str(parsed.get("error")), self.api_key)
            if write:
                raise ApiError("server", detail, MAY_HAVE_REACHED)
            raise ApiError("server", detail, "This was a read. Nothing was saved.")
        if status == 204 or payload == b"":
            return None
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            if write:
                raise ApiError("bad_body", "OctoPrint answered with a body that is not JSON.", MAY_HAVE_REACHED) from error
            raise ApiError(
                "bad_body",
                "OctoPrint answered with a body that is not JSON.",
                "Nothing was saved.",
            ) from error
        if status >= 400:
            message = data.get("error") if isinstance(data, dict) else None
            text = scrub(str(message or "OctoPrint refused the request."), self.api_key)
            if status == 403:
                raise ApiError("unauthorized", text, "Check OCTOPRINT_API_KEY. Nothing was saved.")
            if status == 409:
                raise ApiError("conflict", text, "Read the printer state. Do not send the command again until the state allows it.")
            raise ApiError("http", text, "Read the printer state before trying a different command.")
        if isinstance(data, dict) and "error" in data and "job" not in data and "state" not in data:
            raise ApiError("bad_body", scrub(str(data.get("error")), self.api_key), "The body was an error. Nothing was saved.")
        return data


def _json_or_none(payload: bytes) -> Any:
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
