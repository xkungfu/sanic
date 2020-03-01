from asyncio import CancelledError
from enum import Enum

from sanic.compat import Header
from sanic.exceptions import (
    HeaderExpectationFailed,
    InvalidUsage,
    PayloadTooLarge,
    RequestTimeout,
    SanicException,
    ServerError,
    ServiceUnavailable,
)
from sanic.headers import format_http1, format_http1_response
from sanic.helpers import has_message_body, remove_entity_headers
from sanic.log import access_logger, logger
from sanic.request import Request
from sanic.response import HTTPResponse


class Stage(Enum):
    IDLE = 0  # Waiting for request
    REQUEST = 1  # Request headers being received
    HANDLER = 3  # Headers done, handler running
    RESPONSE = 4  # Response headers sent, body in progress
    FAILED = 100  # Unrecoverable state (error while sending response)


HTTP_CONTINUE = b"HTTP/1.1 100 Continue\r\n\r\n"

class Http:
    __slots__ = [
        "_send",
        "_receive_more",
        "recv_buffer",
        "protocol",
        "expecting_continue",
        "stage",
        "keep_alive",
        "head_only",
        "request",
        "exception",
        "url",
        "request_chunked",
        "request_bytes_left",
        "response",
        "response_func",
        "response_bytes_left",
    ]
    def __init__(self, protocol):
        self._send = protocol.send
        self._receive_more = protocol.receive_more
        self.recv_buffer = protocol.recv_buffer
        self.protocol = protocol
        self.expecting_continue = False
        self.stage = Stage.IDLE
        self.keep_alive = True
        self.head_only = None
        self.request = None
        self.exception = None
        self.url = None

    async def http1(self):
        """HTTP 1.1 connection handler"""
        while True:  # As long as connection stays keep-alive
            try:
                # Receive and handle a request
                self.stage = Stage.REQUEST
                self.response_func = self.http1_response_start
                await self.http1_request_header()
                await self.protocol.request_handler(self.request)
                # Handler finished, response should've been sent
                if self.stage is Stage.HANDLER:
                    raise ServerError("Handler produced no response")
                if self.stage is Stage.RESPONSE:
                    await self.send(end_stream=True)
                # Consume any remaining request body (TODO: or disconnect?)
                if self.request_bytes_left or self.request_chunked:
                    logger.error(f"{self.request} body not consumed.")
                    async for _ in self:
                        pass
            except CancelledError:
                # Write an appropriate response before exiting
                e = self.exception or ServiceUnavailable(f"Cancelled")
                self.exception = None
                self.keep_alive = False
                await self.error_response(e)
            except Exception as e:
                # Write an error response
                await self.error_response(e)
            # Exit and disconnect if finished
            if self.stage is not Stage.IDLE or not self.keep_alive:
                break
            # Wait for next request
            if not self.recv_buffer:
                await self._receive_more()

    async def http1_request_header(self):
        """Receive and parse request header into self.request."""
        # Receive until full header is in buffer
        buf = self.recv_buffer
        pos = 0
        while len(buf) < self.protocol.request_max_size:
            if buf:
                pos = buf.find(b"\r\n\r\n", pos)
                if pos >= 0:
                    break
                pos = max(0, len(buf) - 3)
            await self._receive_more()
            if self.stage is Stage.IDLE:
                self.stage = Stage.REQUEST
        else:
            raise PayloadTooLarge("Payload Too Large")
       # Parse header content
        try:
            reqline, *raw_headers = buf[:pos].decode().split("\r\n")
            method, self.url, protocol = reqline.split(" ")
            if protocol == "HTTP/1.1":
                self.keep_alive = True
            elif protocol == "HTTP/1.0":
                self.keep_alive = False
            else:
                raise Exception
            self.head_only = method.upper() == "HEAD"
            body = False
            headers = []
            for name, value in (h.split(":", 1) for h in raw_headers):
                name, value = h = name.lower(), value.lstrip()
                if name in ("content-length", "transfer-encoding"):
                    body = True
                elif name == "connection":
                    self.keep_alive = value.lower() == "keep-alive"
                headers.append(h)
        except:
            raise InvalidUsage("Bad Request")
        # Prepare a Request object
        request = self.protocol.request_class(
            url_bytes=self.url.encode(),
            headers=Header(headers),
            version=protocol[-3:],
            method=method,
            transport=self.protocol.transport,
            app=self.protocol.app,
        )
        request.stream = self
        self.protocol.state["requests_count"] += 1
        # Prepare for request body
        self.request_chunked = False
        self.request_bytes_left = 0
        if body:
            headers = request.headers
            expect = headers.get("expect")
            if expect is not None:
                if expect.lower() == "100-continue":
                    self.expecting_continue = True
                else:
                    raise HeaderExpectationFailed(f"Unknown Expect: {expect}")
            request.stream = self
            if headers.get("transfer-encoding") == "chunked":
                self.request_chunked = True
                pos -= 2  # One CRLF stays in buffer
            else:
                self.request_bytes_left = int(headers["content-length"])
        # Remove header and its trailing CRLF
        del buf[: pos + 4]
        self.stage = Stage.HANDLER
        self.request = request

    def http1_response_start(self, data, end_stream) -> bytes:
        res = self.response
        # Compatibility with simple response body
        if not data and res.body:
            data, end_stream = res.body, True
        size = len(data)
        status = res.status
        headers = res.headers
        if res.content_type and "content-type" not in headers:
            headers["content-type"] = res.content_type
        # Not Modified, Precondition Failed
        if status in (304, 412):
            headers = remove_entity_headers(headers)
        if not has_message_body(status):
            # Header-only response status
            self.response_func = None
            if (
                data
                or not end_stream
                or "content-length" in headers
                or "transfer-encoding" in headers
            ):
                # TODO: This matches old Sanic operation but possibly
                # an exception would be more appropriate?
                data, size, end_stream = b"", 0, True
                headers.pop("content-length", None)
                headers.pop("transfer-encoding", None)
                #raise ServerError(
                #    f"A {status} response may only have headers, no body."
                #)
        elif self.head_only and "content-length" in headers:
            self.response_func = None
        elif end_stream:
            # Non-streaming response (all in one block)
            headers["content-length"] = size
            self.response_func = None
        elif "content-length" in headers:
            # Streaming response with size known in advance
            self.response_bytes_left = int(headers["content-length"]) - size
            self.response_func = self.http1_response_normal
        else:
            # Length not known, use chunked encoding
            headers["transfer-encoding"] = "chunked"
            data = b"%x\r\n%b\r\n" % (size, data) if size else None
            self.response_func = self.http1_response_chunked
        if self.head_only:
            # Head request: don't send body
            data = b""
            self.response_func = self.head_response_ignored
        headers["connection"] = "keep-alive" if self.keep_alive else "close"
        ret = format_http1_response(status, headers.items(), data)
        # Send a 100-continue if expected and not Expectation Failed
        if self.expecting_continue:
            self.expecting_continue = False
            if status != 417:
                ret = HTTP_CONTINUE + ret
        # Send response
        self.log_response()
        self.stage = Stage.IDLE if end_stream else Stage.RESPONSE
        return ret

    def head_response_ignored(self, data, end_stream):
        """HEAD response: body data silently ignored."""
        if end_stream:
            self.response_func = None
            self.stage = Stage.IDLE

    def http1_response_chunked(self, data, end_stream) -> bytes:
        """Format a part of response body in chunked encoding."""
        # Chunked encoding
        size = len(data)
        if end_stream:
            self.response_func = None
            self.stage = Stage.IDLE
            if size:
                return b"%x\r\n%b\r\n0\r\n\r\n" % (size, data)
            return b"0\r\n\r\n"
        return b"%x\r\n%b\r\n" % (size, data) if size else None

    def http1_response_normal(self, data: bytes, end_stream: bool) -> bytes:
        """Format / keep track of non-chunked response."""
        self.response_bytes_left -= len(data)
        if self.response_bytes_left <= 0:
            if self.response_bytes_left < 0:
                raise ServerError("Response was bigger than content-length")
            self.response_func = None
            self.stage = Stage.IDLE
        elif end_stream:
            raise ServerError("Response was smaller than content-length")
        return data

    async def error_response(self, exception):
        # Disconnect after an error if in any other state than handler
        if self.stage is not Stage.HANDLER:
            self.keep_alive = False
        # Request failure? Respond but then disconnect
        if self.stage is Stage.REQUEST:
            self.stage = Stage.HANDLER
        # From request and handler states we can respond, otherwise be silent
        if self.stage is Stage.HANDLER:
            app = self.protocol.app
            response = await app.handle_exception(self.request, exception)
            await self.respond(response).send(end_stream=True)

    def log_response(self):
        """
        Helper method provided to enable the logging of responses in case if
        the :attr:`HttpProtocol.access_log` is enabled.

        :param response: Response generated for the current request

        :type response: :class:`sanic.response.HTTPResponse` or
            :class:`sanic.response.StreamingHTTPResponse`

        :return: None
        """
        if self.protocol.access_log:
            req, res = self.request, self.response
            extra = {
                "status": getattr(res, "status", 0),
                "byte": getattr(self, "response_bytes_left", -1),
                "host": "UNKNOWN",
                "request": "nil",
            }
            if req is not None:
                if req.ip:
                    extra["host"] = f"{req.ip}:{req.port}"
                extra["request"] = f"{req.method} {req.url}"
            access_logger.info("", extra=extra)

    # Request methods

    async def __aiter__(self):
        """Async iterate over request body."""
        while True:
            data = await self.read()
            if not data:
                return
            yield data

    async def read(self):
        """Read some bytes of request body."""
        # Send a 100-continue if needed
        if self.expecting_continue:
            self.expecting_continue = False
            await self._send(HTTP_CONTINUE)
        # Receive request body chunk
        buf = self.recv_buffer
        if self.request_chunked and self.request_bytes_left == 0:
            # Process a chunk header: \r\n<size>[;<chunk extensions>]\r\n
            while True:
                pos = buf.find(b"\r\n", 3)
                if pos != -1:
                    break
                if len(buf) > 64:
                    self.keep_alive = False
                    raise InvalidUsage("Bad chunked encoding")
                await self._receive_more()
            try:
                size = int(buf[2:pos].split(b";", 1)[0].decode(), 16)
            except:
                self.keep_alive = False
                raise InvalidUsage("Bad chunked encoding")
            self.request_bytes_left = size
            self.protocol._total_request_size += pos + 2
            del buf[: pos + 2]
            if self.request_bytes_left <= 0:
                self.request_chunked = False
                return None
        # At this point we are good to read/return _request_bytes_left
        if self.request_bytes_left:
            if not buf:
                await self._receive_more()
            data = bytes(buf[: self.request_bytes_left])
            size = len(data)
            del buf[:size]
            self.request_bytes_left -= size
            self.protocol._total_request_size += size
            if self.protocol._total_request_size > self.protocol.request_max_size:
                self.keep_alive = False
                raise PayloadTooLarge("Payload Too Large")
            return data
        return None


    # Response methods

    def respond(self, response):
        """Initiate new streaming response.

        Nothing is sent until the first send() call on the returned object, and
        calling this function multiple times will just alter the response to be
        given."""
        if self.stage is not Stage.HANDLER:
            self.stage = Stage.FAILED
            raise RuntimeError("Response already started")
        if not isinstance(response.status, int) or response.status < 200:
            raise RuntimeError(f"Invalid response status {response.status!r}")
        self.response = response
        return self

    async def send(self, data=None, end_stream=None):
        """Send any pending response headers and the given data as body.
         :param data: str or bytes to be written
         :end_stream: whether to close the stream after this block
        """
        if data is None and end_stream is None:
            end_stream = True
        data = data.encode() if hasattr(data, "encode") else data or b""
        data = self.response_func(data, end_stream)
        if not data:
            return
        await self._send(data)
