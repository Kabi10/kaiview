import sys
from pathlib import Path
from unittest.mock import patch


def test_build_default_config_has_required_keys():
    from kaiview.server import _build_default_config
    cfg = _build_default_config()
    assert cfg["server"]["port"] == 3737
    assert cfg["projects"]["dev_dir"] == "~"
    assert cfg["github"]["pat"] == ""
    assert "health" in cfg


def test_dev_dir_expands_tilde():
    from kaiview.server import _build_default_config
    cfg = _build_default_config()
    expanded = Path(cfg["projects"]["dev_dir"]).expanduser().resolve()
    assert expanded.exists()  # home dir always exists


def test_load_config_from_reads_toml(tmp_path):
    from kaiview.server import _load_config_from
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        '[server]\nport = 9999\n[projects]\ndev_dir = "~"\nskip = []\n'
        '[github]\npat = ""\n[health]\ncommit_weight=40\ndirty_weight=20\n'
        'readme_weight=20\ndescription_weight=20\n',
        encoding="utf-8",
    )
    cfg = _load_config_from(cfg_file)
    assert cfg["server"]["port"] == 9999


def test_db_path_is_in_kaiview_dir():
    import kaiview.server as srv
    assert ".kaiview" in str(srv.DB_PATH)


def test_migrate_config_keys_old_kaiview_section():
    from kaiview.server import _migrate_config_keys
    old = {"kaiview": {"dev_dir": "~/projects", "skip": []}, "github": {"token": "ghp_abc"}}
    new = _migrate_config_keys(old)
    assert "projects" in new
    assert new["projects"]["dev_dir"] == "~/projects"
    assert "kaiview" not in new
    assert new["github"]["pat"] == "ghp_abc"
    assert "token" not in new["github"]
