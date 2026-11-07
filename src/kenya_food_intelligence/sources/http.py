"""Small bounded HTTP client shared by public-source profilers."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
from ssl import SSLContext
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class HTTPStatusError(Exception):
    """An HTTP response used a non-retryable status."""

    def __init__(self, status: int, url: str) -> None:
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.url = url


class HTTPRequestError(Exception):
    """A bounded HTTP request failed after its retry policy."""

    def __init__(self, url: str, attempts: int, *, status: int | None = None) -> None:
        detail = f" with HTTP {status}" if status is not None else ""
        super().__init__(f"GET failed after {attempts} attempts{detail}: {url}")
        self.url = url
        self.attempts = attempts
        self.status = status


class HTTPResponseTooLarge(Exception):
    """A response exceeded the caller's configured size limit."""


class HTTPResponseLengthMismatch(Exception):
    """A streamed response did not match its declared Content-Length."""


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    content_type: str | None
    final_url: str
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class HTTPDownloadResponse:
    status: int
    content_type: str | None
    final_url: str
    headers: dict[str, str]
    size_bytes: int
    checksum_sha256: str


class HTTPClient:
    """Perform GET requests with timeouts and bounded exponential retries."""

    def __init__(
        self,
        *,
        timeout: float,
        max_retries: int,
        backoff_factor: float,
        user_agent: str,
        ssl_context: SSLContext | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if backoff_factor < 0:
            raise ValueError("backoff_factor cannot be negative")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.user_agent = user_agent
        self.ssl_context = ssl_context

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        max_bytes: int | None = None,
    ) -> HTTPResponse:
        request_headers = {
            "Accept": "*/*",
            "User-Agent": self.user_agent,
            **(headers or {}),
        }
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            request = Request(url, headers=request_headers)
            try:
                with urlopen(
                    request,
                    timeout=self.timeout,
                    context=self.ssl_context,
                ) as response:
                    if max_bytes is None:
                        body = response.read()
                    else:
                        body = response.read(max_bytes + 1)
                        if len(body) > max_bytes:
                            raise HTTPResponseTooLarge(
                                f"Response from {url} exceeds {max_bytes} bytes"
                            )
                    return HTTPResponse(
                        status=response.status,
                        content_type=response.headers.get("Content-Type"),
                        final_url=response.geturl(),
                        headers=dict(response.headers.items()),
                        body=body,
                    )
            except HTTPError as error:
                if error.code not in {408, 429} and error.code < 500:
                    raise HTTPStatusError(error.code, url) from error
                last_error = error
            except HTTPResponseTooLarge:
                raise
            except (URLError, TimeoutError, OSError) as error:
                last_error = error

            if attempt < self.max_retries:
                time.sleep(self.backoff_factor * (2**attempt))

        status = last_error.code if isinstance(last_error, HTTPError) else None
        raise HTTPRequestError(url, self.max_retries + 1, status=status) from last_error

    def download_to(
        self,
        url: str,
        output_path: Path,
        *,
        headers: dict[str, str] | None = None,
        max_bytes: int,
        chunk_size: int = 64 * 1024,
    ) -> HTTPDownloadResponse:
        """Stream a bounded response to disk and atomically replace the target."""
        if max_bytes <= 0:
            raise ValueError("max_bytes must be greater than zero")
        if chunk_size <= 0:
            raise ValueError("chunk_size must be greater than zero")
        request_headers = {
            "Accept": "*/*",
            "User-Agent": self.user_agent,
            **(headers or {}),
        }
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            temporary_path: Path | None = None
            request = Request(url, headers=request_headers)
            try:
                with urlopen(
                    request,
                    timeout=self.timeout,
                    context=self.ssl_context,
                ) as response:
                    response_headers = dict(response.headers.items())
                    content_length = response.headers.get("Content-Length")
                    declared_size: int | None = None
                    if content_length is not None:
                        if re.fullmatch(r"[0-9]+", content_length.strip()) is None:
                            raise HTTPResponseLengthMismatch(
                                f"Response from {url} has an invalid Content-Length"
                            )
                        declared_size = int(content_length)
                        if declared_size > max_bytes:
                            raise HTTPResponseTooLarge(
                                f"Response from {url} exceeds {max_bytes} bytes"
                            )

                    digest = hashlib.sha256()
                    size_bytes = 0
                    with tempfile.NamedTemporaryFile(
                        mode="wb",
                        dir=output_path.parent,
                        prefix=f".{output_path.name}.",
                        suffix=".part",
                        delete=False,
                    ) as temporary_file:
                        temporary_path = Path(temporary_file.name)
                        while chunk := response.read(chunk_size):
                            size_bytes += len(chunk)
                            if size_bytes > max_bytes:
                                raise HTTPResponseTooLarge(
                                    f"Response from {url} exceeds {max_bytes} bytes"
                                )
                            digest.update(chunk)
                            temporary_file.write(chunk)

                    if declared_size is not None and size_bytes != declared_size:
                        raise HTTPResponseLengthMismatch(
                            f"Response from {url} has {size_bytes} bytes but declared "
                            f"{declared_size}"
                        )
                    os.replace(temporary_path, output_path)
                    return HTTPDownloadResponse(
                        status=response.status,
                        content_type=response.headers.get("Content-Type"),
                        final_url=response.geturl(),
                        headers=response_headers,
                        size_bytes=size_bytes,
                        checksum_sha256=digest.hexdigest(),
                    )
            except HTTPError as error:
                if error.code not in {408, 429} and error.code < 500:
                    raise HTTPStatusError(error.code, url) from error
                last_error = error
            except HTTPResponseTooLarge:
                raise
            except HTTPResponseLengthMismatch:
                raise
            except (URLError, TimeoutError, OSError, HTTPException) as error:
                last_error = error
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)

            if attempt < self.max_retries:
                time.sleep(self.backoff_factor * (2**attempt))

        status = last_error.code if isinstance(last_error, HTTPError) else None
        raise HTTPRequestError(url, self.max_retries + 1, status=status) from last_error
