import pytest
from pathlib import Path

if __import__("sys").version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


@pytest.fixture
def client(tmp_path):
    """TestClient with isolated config file."""
    import kaiview.server as srv
    cfg_text = (
        '[server]\nport = 3737\n[projects]\ndev_dir = "~"\nskip = []\n'
        '[github]\npat = ""\n[health]\ncommit_weight=40\ndirty_weight=20\n'
        'readme_weight=20\ndescription_weight=20\n'
    )
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(cfg_text, encoding="utf-8")
    # Redirect server's config file pointer
    srv._CFG_FILE = cfg_file
    srv.CFG = srv._load_config_from(cfg_file)
    from fastapi.testclient import TestClient
    return TestClient(srv.app)


def test_get_settings_returns_expected_shape(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    d = r.json()
    for key in ("port", "dev_dir", "github_pat", "skip", "health"):
        assert key in d, f"missing key: {key}"
    for w in ("commit_weight", "dirty_weight", "readme_weight", "description_weight"):
        assert w in d["health"]


def test_get_settings_masks_non_empty_pat(tmp_path):
    import kaiview.server as srv
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        '[server]\nport=3737\n[projects]\ndev_dir="~"\nskip=[]\n'
        '[github]\npat="ghp_realtoken1234"\n[health]\ncommit_weight=40\n'
        'dirty_weight=20\nreadme_weight=20\ndescription_weight=20\n'
    )
    srv._CFG_FILE = cfg_file
    srv.CFG = srv._load_config_from(cfg_file)
    from fastapi.testclient import TestClient
    c = TestClient(srv.app)
    r = c.get("/api/settings")
    assert r.json()["github_pat"] == "__MASKED__"


def test_get_settings_empty_pat_returns_empty_string(client):
    r = client.get("/api/settings")
    assert r.json()["github_pat"] == ""


def _valid_body(**overrides):
    body = {
        "dev_dir": str(Path.home()),
        "port": 3737,
        "github_pat": "",
        "skip": [".git"],
        "health": {
            "commit_weight": 40, "dirty_weight": 20,
            "readme_weight": 20, "description_weight": 20,
        },
    }
    body.update(overrides)
    return body


def test_post_settings_success(client):
    r = client.post("/api/settings", json=_valid_body())
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_post_settings_invalid_dev_dir(client):
    r = client.post("/api/settings", json=_valid_body(dev_dir="/nonexistent/xyz_abc_123"))
    assert r.status_code == 422
    assert r.json()["code"] == "dev_dir_not_found"


def test_post_settings_weights_not_100(client):
    r = client.post("/api/settings", json=_valid_body(
        health={"commit_weight": 50, "dirty_weight": 20,
                "readme_weight": 20, "description_weight": 20}
    ))
    assert r.status_code == 422
    assert r.json()["code"] == "weights_dont_sum_to_100"


def test_post_settings_port_change_signals_restart(tmp_path):
    import kaiview.server as srv
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        '[server]\nport=3737\n[projects]\ndev_dir="~"\nskip=[]\n'
        '[github]\npat=""\n[health]\ncommit_weight=40\ndirty_weight=20\n'
        'readme_weight=20\ndescription_weight=20\n'
    )
    srv._CFG_FILE = cfg_file
    srv.CFG = srv._load_config_from(cfg_file)
    from fastapi.testclient import TestClient
    c = TestClient(srv.app)
    r = c.post("/api/settings", json=_valid_body(port=3738))
    assert r.status_code == 200
    data = r.json()
    assert data["restart_required"] is True
    assert data["new_port"] == 3738


def test_post_settings_masked_pat_preserves_original(tmp_path):
    import kaiview.server as srv
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        '[server]\nport=3737\n[projects]\ndev_dir="~"\nskip=[]\n'
        '[github]\npat="ghp_original_secret"\n[health]\ncommit_weight=40\n'
        'dirty_weight=20\nreadme_weight=20\ndescription_weight=20\n'
    )
    srv._CFG_FILE = cfg_file
    srv.CFG = srv._load_config_from(cfg_file)
    from fastapi.testclient import TestClient
    c = TestClient(srv.app)
    c.post("/api/settings", json=_valid_body(github_pat="__MASKED__"))
    saved = tomllib.loads(cfg_file.read_text())
    assert saved["github"]["pat"] == "ghp_original_secret"
