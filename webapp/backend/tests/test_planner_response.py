import gzip
import json

import httpx

from webapp.backend.app.planner import _response_json


def test_response_json_accepts_plain_json():
    response = httpx.Response(200, content=b'{"ok": true}')

    assert _response_json(response) == {"ok": True}


def test_response_json_accepts_headerless_gzip():
    payload = {"choices": [{"message": {"content": "{}"}}]}
    response = httpx.Response(200, content=gzip.compress(json.dumps(payload).encode("utf-8")))

    assert _response_json(response) == payload
