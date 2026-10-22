import pytest
from services.common.upload_limits import UploadBodyLimit


@pytest.mark.asyncio
async def test_missing_content_length_is_bounded_before_application():
    called = []
    async def app(scope, receive, send): called.append(True)
    messages = iter([{'type':'http.request', 'body': b'x'*8, 'more_body': True}]*3)
    async def receive(): return next(messages)
    sent=[]
    async def send(message): sent.append(message)
    await UploadBodyLimit(app, 10)({'type':'http', 'headers':[(b'content-type',b'multipart/form-data')]}, receive, send)
    assert not called
    assert sent[0]['status'] == 413


@pytest.mark.asyncio
async def test_spooled_body_is_replayed_exactly_and_slot_released_on_disconnect():
    result = []
    async def app(scope, receive, send):
        while True:
            message = await receive()
            result.append(message['body'])
            if not message.get('more_body'): break
    middleware = UploadBodyLimit(app, 20, concurrency=1)
    scope={'type':'http','headers':[(b'content-type',b'multipart/form-data')]}
    async def disconnected(): return {'type':'http.disconnect'}
    async def send(message): pass
    await middleware(scope, disconnected, send)
    messages=iter([{'type':'http.request','body':b'abc','more_body':True},
                   {'type':'http.request','body':b'def','more_body':False}])
    async def receive(): return next(messages)
    await middleware(scope, receive, send)
    assert b''.join(result) == b'abcdef'
