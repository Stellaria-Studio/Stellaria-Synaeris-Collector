import json
import pytest
from synaeris_collector.bgi import make_batch


def test_batch_preserves_route_and_does_not_invent_success(tmp_path):
    route = tmp_path / "route.json"
    route.write_text(json.dumps({"info": {"name": "test"}, "positions": [{"x": 1, "y": 2}]}))
    path = make_batch([route], tmp_path / "batch")
    assert (path / "assets/route_0000.json").read_bytes() == route.read_bytes()
    script = (path / "main.js").read_text(encoding="utf-8")
    assert 'outcome:"unknown", verified:false' in script
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["http_allowed_urls"] == ["http://127.0.0.1:18765/*"]
    with pytest.raises(ValueError):
        make_batch([route], path)
