import importlib.resources
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def test_config_template_is_bundled():
    """config_template.toml must be accessible as package data after install."""
    text = importlib.resources.files("kaiview").joinpath("config_template.toml").read_text()
    cfg = tomllib.loads(text)
    assert cfg["server"]["port"] == 3737
    assert cfg["projects"]["dev_dir"] == "~"
    assert "health" in cfg
    weights = cfg["health"]
    assert sum(weights.values()) == 100


def test_index_html_is_bundled():
    text = importlib.resources.files("kaiview").joinpath("index.html").read_text(encoding="utf-8")
    assert "<html" in text.lower()
