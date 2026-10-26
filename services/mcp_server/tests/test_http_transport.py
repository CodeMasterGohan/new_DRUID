"""Exercise authenticated native MCP and single-request JSON HTTP clients."""
import json

from starlette.testclient import TestClient

from mcp_server.server import Settings, create_app

from .conftest import AUDIENCE, ISSUER, SECRET
from .test_auth import mint


def message(response):
    assert response.status_code == 200, response.text
    if response.headers['content-type'].startswith('application/json'):
        return response.json()
    return json.loads(next(line[6:] for line in response.text.splitlines()
                           if line.startswith('data: ')))


def test_authenticated_http_initialize_list_and_call(deps):
    app = create_app(Settings(jwt_secret=SECRET, jwt_issuer=ISSUER,
                              jwt_audience=AUDIENCE), deps)
    headers = {'Authorization': 'Bearer '+mint(),
               'Accept': 'application/json, text/event-stream'}
    with TestClient(app, base_url='http://127.0.0.1', follow_redirects=False) as client:
        assert client.post('/mcp').status_code == 401
        assert client.post('/mcp', headers={**headers, 'Host': 'untrusted.invalid'}, json={}).status_code == 421
        response = client.post('/mcp', headers=headers, json={
            'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
            'params': {'protocolVersion': '2025-03-26', 'capabilities': {},
                       'clientInfo': {'name': 'http-regression', 'version': '1.0'}},
        })
        assert 'result' in message(response)
        assert 'mcp-session-id' not in response.headers
        # The gateway forwards to this Docker hostname, while direct local
        # clients use the loopback Host exercised by initialize above.
        headers['Host'] = 'mcp_server:8002'
        response = client.post('/mcp', headers=headers, json={
            'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        assert response.status_code == 202
        listed = message(client.post('/mcp', headers=headers, json={
            'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}))
        assert len(listed['result']['tools']) == 8
        called = message(client.post('/mcp', headers=headers, json={
            'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
            'params': {'name': 'list_documents', 'arguments': {}},
        }))
        assert not called['result'].get('isError')


def test_authenticated_single_request_json_tool_call(deps):
    app = create_app(Settings(jwt_secret=SECRET, jwt_issuer=ISSUER,
                              jwt_audience=AUDIENCE), deps)
    with TestClient(app, base_url='http://127.0.0.1', follow_redirects=False) as client:
        response = client.post('/mcp', headers={
            'Authorization': 'Bearer '+mint(),
            'Accept': 'application/json, text/event-stream'}, json={
                'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': 'list_documents', 'arguments': {}}})
        assert response.headers['content-type'].startswith('application/json')
        assert not message(response)['result'].get('isError')
