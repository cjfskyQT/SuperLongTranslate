# -*- coding: utf-8 -*-
"""配置层回归测试。

重点：``save_settings`` 持锁期间会调用 ``load_settings``，锁必须可重入。
一旦退化成普通 ``threading.Lock``，保存请求会永久挂起（界面表现为"保存不了 key"），
而这个缺陷不会抛异常，只会静默卡死——所以必须显式守护。

**所有写入都被重定向到临时文件**：测试绝不能污染用户真实的
``data/settings.json``（曾经把假凭据写进去，导致界面显示"已配置密钥"
却根本调不通百度接口）。
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """把配置读写重定向到临时目录，测试绝不碰真实 settings.json。"""
    fake = tmp_path / "settings.json"
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SETTINGS_PATH", fake)
    yield fake


def test_lock_is_reentrant():
    assert isinstance(config._LOCK, type(threading.RLock())), \
        "config._LOCK 必须是可重入锁，否则 save_settings 会自死锁"


def test_save_settings_does_not_hang():
    """死锁会让测试套件整体超时而不是失败，所以用带超时的守护线程来守。"""
    box = {}

    def worker():
        try:
            box["r"] = config.save_settings(
                {"baidu_appid": "test_appid", "baidu_secret": "test_secret"})
        except Exception as e:  # noqa: BLE001
            box["err"] = e

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "save_settings 发生死锁（5 秒未返回）"
    assert "err" not in box, f"save_settings 抛异常：{box.get('err')}"
    assert "r" in box, "save_settings 未返回"


def test_persist_and_reload(isolated_settings):
    config.save_settings({"baidu_appid": "my_appid", "baidu_secret": "my_secret"})
    reloaded = config.load_settings()
    assert reloaded["baidu_appid"] == "my_appid"
    assert reloaded["baidu_secret"] == "my_secret"


def test_secret_not_leaked_to_client():
    saved = config.save_settings({"baidu_secret": "supersecretkey"})
    assert saved["baidu_secret"] != "supersecretkey", "接口回显泄露了明文 secret"
    assert saved["baidu_secret_set"] is True
    assert "*" in saved["baidu_secret"]


def test_unknown_keys_ignored(isolated_settings):
    config.save_settings({"evil_key": "x"})
    assert "evil_key" not in json.loads(
        config.SETTINGS_PATH.read_text(encoding="utf-8"))


def test_settings_file_is_written(isolated_settings):
    config.save_settings({"engine": "google"})
    assert config.SETTINGS_PATH.exists()
    data = json.loads(config.SETTINGS_PATH.read_text(encoding="utf-8"))
    assert "baidu_appid" in data and "engine" in data


def test_partial_update_keeps_secret(isolated_settings):
    """前端只改 appid 时，不应把已存的 secret 清掉。"""
    config.save_settings({"baidu_appid": "a1", "baidu_secret": "s1"})
    config.save_settings({"baidu_appid": "a2"})
    d = config.load_settings()
    assert d["baidu_appid"] == "a2"
    assert d["baidu_secret"] == "s1"


def test_tests_do_not_touch_real_settings():
    """守卫：确认重定向生效，真实路径未被本次测试写入。"""
    real = config.ROOT / "data" / "settings.json"
    before = real.read_bytes() if real.exists() else None
    config.save_settings({"baidu_appid": "SHOULD_NOT_ESCAPE"})
    after = real.read_bytes() if real.exists() else None
    assert before == after, "测试写到了真实的 data/settings.json"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))