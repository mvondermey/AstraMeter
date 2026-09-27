import json
from unittest.mock import Mock

from astrameter.web_server import WebServer


def local_request():
    return Mock(remote="127.0.0.1")


async def test_priority_load_not_configured_is_404():
    resp = await WebServer()._handle_priority_load(local_request())
    assert resp.status == 404


async def test_priority_load_returns_status_json():
    server = WebServer()
    server.priority_load_status = lambda: {"power_w": 1400.0, "misses": 2}
    resp = await server._handle_priority_load(local_request())
    assert resp.status == 200
    assert json.loads(resp.body) == {"power_w": 1400.0, "misses": 2}


async def test_priority_load_rejects_remote_callers():
    server = WebServer()
    server.priority_load_status = lambda: {"power_w": 1400.0}
    resp = await server._handle_priority_load(Mock(remote="192.168.178.50"))
    assert resp.status == 403
