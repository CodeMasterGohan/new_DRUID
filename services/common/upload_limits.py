"""Bound multipart requests before form parsing, including chunked transfer.

Spooling keeps request bytes off the Python heap. The semaphore bounds per-
process temporary disk use; disconnects/errors close the temporary file.
"""
import asyncio
import tempfile
from starlette.responses import JSONResponse


class UploadBodyLimit:
    def __init__(self, app, max_bytes, concurrency=4, timeout=120):
        if concurrency < 1 or max_bytes < 1 or timeout <= 0:
            raise ValueError("upload limits must be positive")
        self.app, self.max_bytes, self.timeout = app, max_bytes, timeout
        self.slots = asyncio.Semaphore(concurrency)

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get('headers', []))
        if scope['type'] != 'http' or not headers.get(b'content-type', b'').startswith(b'multipart/'):
            return await self.app(scope, receive, send)
        try:
            await asyncio.wait_for(self.slots.acquire(), timeout=0.1)
        except asyncio.TimeoutError:
            return await JSONResponse({'detail': 'upload capacity busy'}, 503,
                                      headers={'Retry-After': '1'})(scope, receive, send)
        try:
            with tempfile.TemporaryFile() as spool:
                total = 0
                try:
                    async with asyncio.timeout(self.timeout):
                        while True:
                            message = await receive()
                            if message['type'] == 'http.disconnect':
                                return
                            body = message.get('body', b'')
                            total += len(body)
                            if total > self.max_bytes:
                                return await JSONResponse({'detail': 'upload body too large'}, 413)(scope, receive, send)
                            spool.write(body)
                            if not message.get('more_body', False):
                                break
                except asyncio.TimeoutError:
                    return await JSONResponse({'detail': 'upload timed out'}, 408)(scope, receive, send)
                spool.seek(0)
                remaining = total
                async def replay():
                    nonlocal remaining
                    if remaining == 0:
                        return await receive()
                    body = spool.read(min(1024 * 1024, remaining))
                    remaining -= len(body)
                    return {'type': 'http.request', 'body': body, 'more_body': remaining > 0}
                if total == 0:
                    return await JSONResponse({'detail': 'empty upload body'}, 400)(scope, receive, send)
                await self.app(scope, replay, send)
        finally:
            self.slots.release()
