"""redact_hot 表测试."""

import json

import pytest

from amane.observability.models import REDACTION_PLACEHOLDER
from amane.observability.redact import (
    hot_slice_for_task,
    needs_secrets_file,
    redact_dsn,
    redact_hot,
    redact_plugin_config,
    redact_proxy,
    restore_redacted_placeholders,
)


def test_hot_slice_scrape_drops_noise():
    full = {
        "scraping": {"a": 1},
        "network": {"b": 2},
        "llm": {},
        "r18": {},
        "plugins": {},
        "watcher": {"x": 1},
        "worker": {"y": 1},
        "logging": {"level": "INFO"},
        "sr": {},
    }
    assert set(hot_slice_for_task(full, "scrape").keys()) == {"scraping", "network", "llm", "r18", "plugins"}


@pytest.mark.parametrize(
    ("dump", "expected"),
    [
        ({"scraping": {"site_config": {"javdb": {"cookie": {}}}}, "llm": {}, "network": {}, "r18": {}}, False),
        ({"scraping": {"site_config": {"javdb": {"cookie": {"a": "b"}}}}, "llm": {}, "network": {}, "r18": {}}, True),
        ({"scraping": {"site_config": {}}, "llm": {"api_key": "k"}, "network": {}, "r18": {}}, True),
        (
            {"scraping": {"site_config": {}}, "llm": {}, "network": {}, "r18": {}, "media_server": {"api_key": "m"}},
            True,
        ),
        (
            {"scraping": {"site_config": {}}, "llm": {}, "network": {}, "r18": {}, "media_server": {"api_key": None}},
            False,
        ),
        ({"scraping": {"site_config": {}}, "llm": {}, "network": {}, "r18": {"dsn": "postgresql://u:p@h/db"}}, True),
        ({"scraping": {"site_config": {}}, "llm": {}, "network": {}, "r18": {"read_password": "default"}}, False),
        ({"plugins": {"acme.fake": {"config": {"api_token": "secret"}}}}, True),
    ],
)
def test_needs_secrets_file(dump: dict, expected: bool):
    assert needs_secrets_file(dump) is expected


@pytest.mark.parametrize(
    ("input_hot", "assertions"),
    [
        pytest.param(
            {
                "scraping": {
                    "site_config": {
                        "javdb": {"cookie": {"session": "secret", "dv": "1"}, "api_token": None},
                    }
                }
            },
            lambda out: (
                out["scraping"]["site_config"]["javdb"]["cookie"] == {"session": "***", "dv": "***"}
                and out["scraping"]["site_config"]["javdb"]["api_token"] is None
            ),
            id="cookie-values-redacted-keys-kept",
        ),
        pytest.param(
            {"scraping": {"site_config": {"theporndb": {"cookie": {}, "api_token": "tok_abc"}}}},
            lambda out: out["scraping"]["site_config"]["theporndb"]["api_token"] == REDACTION_PLACEHOLDER,
            id="api-token-redacted",
        ),
        pytest.param(
            {"scraping": {"site_config": {"javdb": {"cookie": {}}}}},
            lambda out: out["scraping"]["site_config"]["javdb"]["cookie"] == {},
            id="empty-cookie-unchanged",
        ),
        pytest.param(
            {"llm": {"api_key": "sk-live", "enabled": True}},
            lambda out: out["llm"]["api_key"] == REDACTION_PLACEHOLDER and out["llm"]["enabled"] is True,
            id="llm-api-key",
        ),
        pytest.param({"llm": {"api_key": None}}, lambda out: out["llm"]["api_key"] is None, id="llm-api-key-null"),
        pytest.param(
            {"media_server": {"kind": "jellyfin", "url": "http://media.local", "api_key": "live"}},
            lambda out: (
                out["media_server"]["api_key"] == REDACTION_PLACEHOLDER
                and out["media_server"]["url"] == "http://media.local"
            ),
            id="media-server-api-key",
        ),
        pytest.param(
            {"network": {"proxy": "socks5://user:pass@127.0.0.1:7890"}},
            lambda out: out["network"]["proxy"] == "socks5://127.0.0.1:7890",
            id="proxy-strip-userinfo",
        ),
        pytest.param(
            {"network": {"proxy": "http://127.0.0.1:8080"}},
            lambda out: out["network"]["proxy"] == "http://127.0.0.1:8080",
            id="proxy-no-userinfo",
        ),
        pytest.param({"network": {"proxy": None}}, lambda out: out["network"]["proxy"] is None, id="proxy-null"),
        pytest.param(
            {
                "r18": {
                    "dsn": "postgresql://admin:s3cret@db:5432/postgres",
                    "read_password": "ro_pass",
                }
            },
            lambda out: (
                out["r18"]["read_password"] == REDACTION_PLACEHOLDER
                and "***" in out["r18"]["dsn"]
                and "s3cret" not in out["r18"]["dsn"]
            ),
            id="r18-dsn-and-password",
        ),
        pytest.param(
            {"plugins": {"acme.fake": {"config": {"api_token": "secret", "endpoint": "https://example.test"}}}},
            lambda out: (
                out["plugins"]["acme.fake"]["config"]["api_token"] == REDACTION_PLACEHOLDER
                and out["plugins"]["acme.fake"]["config"]["endpoint"] == "https://example.test"
            ),
            id="plugin-secret",
        ),
    ],
)
def test_redact_hot(input_hot: dict, assertions):
    out = redact_hot(input_hot)
    assert assertions(out)


def test_redact_hot_covers_media_ops_secrets():
    """媒体运维的密钥字段必须登记进脱敏: 任务记录导出走的就是这条路径."""
    dump = {
        "r18": {"enabled": True, "dsn": "postgresql://user:pw@host/db"},
        "subtitles": {"enabled": True, "providers": ["subtitlecat"]},
    }
    sliced = hot_slice_for_task(dump, "subtitle_fetch")
    out = redact_hot(sliced)
    assert "SECRET-TOKEN" not in json.dumps(out, ensure_ascii=False)
    assert "postgresql://user:pw@host/db" not in json.dumps(out, ensure_ascii=False)
    assert out["subtitles"]["enabled"] is True, "非密钥字段保持原值"
    assert needs_secrets_file(sliced) is True


def test_redact_hot_does_not_mutate_input():
    original = {
        "scraping": {"site_config": {"javdb": {"cookie": {"a": "b"}, "api_token": "t"}}},
        "llm": {"api_key": "k"},
    }
    redact_hot(original)
    assert original["scraping"]["site_config"]["javdb"]["cookie"]["a"] == "b"
    assert original["llm"]["api_key"] == "k"


@pytest.mark.parametrize(
    ("proxy", "expected"),
    [
        ("socks5://u:p@host:1", "socks5://host:1"),
        ("http://host", "http://host"),
        ("http://user@host:80/path", "http://host:80/path"),
    ],
)
def test_redact_proxy(proxy: str, expected: str):
    assert redact_proxy(proxy) == expected


def test_redact_dsn_hides_password():
    assert "secret" not in redact_dsn("postgresql://u:secret@localhost/db")
    assert "localhost" in redact_dsn("postgresql://u:secret@localhost/db")


def test_needs_secrets_file_flags_solver_url_userinfo():
    """浏览器解题服务地址带 userinfo 时同样要落密钥文件: 与 network.proxy 同一口径."""
    dump = {
        "scraping": {"site_config": {}},
        "llm": {},
        "network": {"browser": {"solver_url": "http://user:pw@127.0.0.1:8191"}},
        "r18": {},
    }
    assert needs_secrets_file(dump) is True


def test_needs_secrets_file_ignores_plain_solver_url():
    dump = {
        "scraping": {"site_config": {}},
        "llm": {},
        "network": {"browser": {"solver_url": "http://127.0.0.1:8191"}},
        "r18": {},
    }
    assert needs_secrets_file(dump) is False


def test_redact_hot_hides_solver_url_userinfo():
    out = redact_hot({"network": {"browser": {"solver_url": "http://user:pw@127.0.0.1:8191"}}})
    assert out["network"]["browser"]["solver_url"] == "http://127.0.0.1:8191"


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (
            {"api_token": "secret-value", "endpoint": "https://plugin.example.test"},
            {"api_token": REDACTION_PLACEHOLDER, "endpoint": "https://plugin.example.test"},
        ),
        ({"nested": {"password": "pw"}}, {"nested": {"password": REDACTION_PLACEHOLDER}}),
        ({"items": [{"secret": "s"}]}, {"items": [{"secret": REDACTION_PLACEHOLDER}]}),
        ({"api_token": None, "endpoint": ""}, {"api_token": None, "endpoint": ""}),
    ],
)
def test_redact_plugin_config(config: dict, expected: dict):
    assert redact_plugin_config(config) == expected


def test_redact_plugin_config_keeps_input():
    config = {"api_token": "secret-value"}
    redact_plugin_config(config)
    assert config == {"api_token": "secret-value"}, "脱敏返回副本, 调用方的配置不被改动"


@pytest.mark.parametrize(
    ("patch", "current", "expected"),
    [
        # 占位符换成当前值: 前端原样回写时不能把密钥覆盖成 ***
        ({"llm": {"api_key": "***"}}, {"llm": {"api_key": "real-key"}}, {"llm": {"api_key": "real-key"}}),
        # 新值原样保留: 写入方向仍然接受明文
        ({"llm": {"api_key": "new-key"}}, {"llm": {"api_key": "real-key"}}, {"llm": {"api_key": "new-key"}}),
        # 当前值也没有密钥时丢弃该键, 不写入占位符 (外层留下空表, 合并时等价于不动)
        ({"llm": {"api_key": "***"}}, {}, {"llm": {}}),
        ({"notify": {"url": "***"}}, {"notify": {"url": None}}, {"notify": {}}),
        ({"wash": {"enabled": True}}, {"wash": {"enabled": False}}, {"wash": {"enabled": True}}),
        # 代理串的脱敏只丢 userinfo: 回写这一份时按原值还原, 口令不被悄悄丢掉
        (
            {"network": {"proxy": "http://127.0.0.1:7890"}},
            {"network": {"proxy": "http://user:pw@127.0.0.1:7890"}},
            {"network": {"proxy": "http://user:pw@127.0.0.1:7890"}},
        ),
        # 真正的新地址照常写入
        (
            {"network": {"proxy": "http://other.host:7890"}},
            {"network": {"proxy": "http://user:pw@127.0.0.1:7890"}},
            {"network": {"proxy": "http://other.host:7890"}},
        ),
        (
            {"plugins": {"acme.fake": {"config": {"api_token": "***", "endpoint": "https://new.test"}}}},
            {"plugins": {"acme.fake": {"config": {"api_token": "tok", "endpoint": "https://old.test"}}}},
            {"plugins": {"acme.fake": {"config": {"api_token": "tok", "endpoint": "https://new.test"}}}},
        ),
    ],
)
def test_restore_redacted_placeholders(patch: dict, current: dict, expected: dict):
    assert restore_redacted_placeholders(patch, current) == expected
