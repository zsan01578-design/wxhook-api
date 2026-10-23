"""Bound both wire bytes and decoder output before consumers buffer a response."""
from __future__ import annotations

import zlib

import httpx


class ResponseReadError(Exception):
    pass


class LimitedStream(httpx.AsyncByteStream):
    chunk_size = 65536

    def __init__(self, stream, limit: int, *, encoding: str = "", headers_only: bool = False):
        self.stream, self.limit = stream, limit
        self.encoding, self.headers_only = encoding, headers_only

    async def __aiter__(self):
        if self.headers_only:
            return
        if self.encoding not in {"", "identity", "gzip", "deflate"}:
            raise ResponseReadError("encoding")
        size = wire_size = 0
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if self.encoding == "gzip" else None
        prefix = b""
        async for chunk in self.stream:
            wire_size += len(chunk)
            if wire_size > self.limit:
                raise ResponseReadError("size")
            if self.encoding not in {"gzip", "deflate"}:
                for start in range(0, len(chunk), self.chunk_size):
                    yield chunk[start:start + self.chunk_size]
                continue
            if self.encoding == "deflate" and decoder is None:
                prefix += chunk
                if len(prefix) < 2:
                    continue
                wrapped = prefix[0] & 15 == 8 and prefix[0] >> 4 <= 7 and int.from_bytes(prefix[:2], "big") % 31 == 0
                decoder = zlib.decompressobj(zlib.MAX_WBITS if wrapped else -zlib.MAX_WBITS)
                chunk, prefix = prefix, b""
            pending = chunk
            while True:
                if decoder.eof and pending:
                    if self.encoding != "gzip":
                        raise ResponseReadError("format")
                    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                limit = min(self.chunk_size, self.limit - size + 1)
                try:
                    decoded = decoder.decompress(pending, limit)
                except zlib.error:
                    raise ResponseReadError("format") from None
                size += len(decoded)
                if size > self.limit:
                    raise ResponseReadError("size")
                if decoded:
                    yield decoded
                pending = decoder.unused_data if decoder.eof else decoder.unconsumed_tail
                if not pending and len(decoded) < limit:
                    break
        # Empty encoded redirect responses are compatible with the SDK probes.
        if wire_size and self.encoding in {"gzip", "deflate"} and (decoder is None or not decoder.eof):
            raise ResponseReadError("incomplete")

    async def aclose(self):
        await self.stream.aclose()


async def response_bytes(response: httpx.Response, limit: int):
    if response.is_stream_consumed:
        # In-memory/mock responses may already have been decoded by HTTPX.
        content = response.content
        if len(content) > limit:
            raise ResponseReadError("size")
        for start in range(0, len(content), LimitedStream.chunk_size):
            yield content[start:start + LimitedStream.chunk_size]
        return
    length = response.headers.get("content-length", "")
    if length.isdigit() and int(length) > limit:
        raise ResponseReadError("size")
    encoding = response.headers.get("content-encoding", "").strip().lower()
    async for chunk in LimitedStream(response.aiter_raw(), limit, encoding=encoding):
        yield chunk
