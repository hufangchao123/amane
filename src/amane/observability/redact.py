"""HotSettings 脱敏与任务记录用配置切片."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.engine.url import make_url

from .models import REDACTION_PLACEHOLDER

# scrape 回放只需这些 section; watcher/worker/logging/sr 对解析无贡献
_SCRAPE_HOT_KEYS: tuple[str, ...] = ("scraping", "network", "llm", "r18", "plugins")


def hot_slice_for_task(hot_dump: dict[str, Any], task_type: str) -> dict[str, Any]:
    """去掉与回放无关的噪声 section."""
    if task_type == "scrape":
        return {k: deepcopy(hot_dump[k]) for k in _SCRAPE_HOT_KEYS if k in hot_dump}
    drop = {"watcher", "worker", "logging"}
    return {k: deepcopy(v) for k, v in hot_dump.items() if k not in drop}


def _proxy_has_userinfo(value: object) -> bool:
    """代理地址是否带 ``user:pass@``; 只有这种才需要旁路保存明文."""
    if not value:
        return False
    after_scheme = str(value).split("://", 1)[-1]
    return "@" in after_scheme.split("/", 1)[0]


def needs_secrets_file(hot_dump: dict[str, Any]) -> bool:
    """默认 r18 密码不算需旁路保存的明文密钥."""
    scraping = hot_dump.get("scraping")
    if isinstance(scraping, dict):
        site_config = scraping.get("site_config")
        if isinstance(site_config, dict):
            for site_cfg in site_config.values():
                if not isinstance(site_cfg, dict):
                    continue
                if site_cfg.get("cookie") or site_cfg.get("api_token") or _proxy_has_userinfo(site_cfg.get("proxy")):
                    return True
    llm = hot_dump.get("llm")
    if isinstance(llm, dict) and llm.get("api_key"):
        return True
    agent = hot_dump.get("agent")
    if isinstance(agent, dict) and agent.get("api_key"):
        return True
    media_server = hot_dump.get("media_server")
    if isinstance(media_server, dict) and media_server.get("api_key"):
        return True
    subtitles = hot_dump.get("subtitles")
    if isinstance(subtitles, dict):
        generation = subtitles.get("generation")
        if isinstance(generation, dict) and generation.get("api_key"):
            return True
    network = hot_dump.get("network")
    if isinstance(network, dict):
        if _proxy_has_userinfo(network.get("proxy")):
            return True
        # 浏览器解题服务地址与 proxy 同口径: 都可能写成 http://user:pass@host:8191.
        browser = network.get("browser")
        if isinstance(browser, dict) and _proxy_has_userinfo(browser.get("solver_url")):
            return True
    r18 = hot_dump.get("r18")
    if isinstance(r18, dict) and r18.get("dsn"):
        return True
    # notify.url 是风险最低的一类密钥 (Bark 设备键 / Server 酱 SENDKEY): 它为真不该把整份含站点
    # cookie 与 LLM 密钥的配置搬进明文旁路文件, 因此不再参与这个判定; 它本身仍进脱敏名单.
    plugins = hot_dump.get("plugins")
    if isinstance(plugins, dict):
        for plugin in plugins.values():
            if isinstance(plugin, dict) and _has_secret_value(plugin.get("config")):
                return True
    return False


def redact_hot(data: dict[str, Any]) -> dict[str, Any]:
    """返回 HotSettings model_dump 的脱敏副本.

    空值保持 null/{}; 有值的密钥类字段替换为 ``***``.
    """
    out = deepcopy(data)

    scraping = out.get("scraping")
    if isinstance(scraping, dict):
        site_config = scraping.get("site_config")
        if isinstance(site_config, dict):
            for site_cfg in site_config.values():
                if not isinstance(site_cfg, dict):
                    continue
                cookie = site_cfg.get("cookie")
                if isinstance(cookie, dict) and cookie:
                    site_cfg["cookie"] = dict.fromkeys(cookie, REDACTION_PLACEHOLDER)
                if site_cfg.get("api_token"):
                    site_cfg["api_token"] = REDACTION_PLACEHOLDER
                # 站点专用代理由用户填写, 允许 http://user:pass@host:port; 与 network.proxy 同口径.
                if site_cfg.get("proxy"):
                    site_cfg["proxy"] = redact_proxy(str(site_cfg["proxy"]))

    llm = out.get("llm")
    if isinstance(llm, dict) and llm.get("api_key"):
        llm["api_key"] = REDACTION_PLACEHOLDER

    agent = out.get("agent")
    if isinstance(agent, dict) and agent.get("api_key"):
        agent["api_key"] = REDACTION_PLACEHOLDER

    media_server = out.get("media_server")
    if isinstance(media_server, dict) and media_server.get("api_key"):
        media_server["api_key"] = REDACTION_PLACEHOLDER

    network = out.get("network")
    if isinstance(network, dict):
        if network.get("proxy"):
            network["proxy"] = redact_proxy(str(network["proxy"]))
        # 浏览器解题服务地址允许带 userinfo (http://user:pass@host:8191), 与 proxy 同一口径处理.
        browser = network.get("browser")
        if isinstance(browser, dict) and browser.get("solver_url"):
            browser["solver_url"] = redact_proxy(str(browser["solver_url"]))

    r18 = out.get("r18")
    if isinstance(r18, dict):
        if r18.get("read_password"):
            r18["read_password"] = REDACTION_PLACEHOLDER
        if r18.get("dsn"):
            r18["dsn"] = redact_dsn(str(r18["dsn"]))

    plugins = out.get("plugins")
    if isinstance(plugins, dict):
        for plugin in plugins.values():
            if isinstance(plugin, dict):
                plugin["config"] = _redact_secret_values(plugin.get("config"))

    # 媒体运维的密钥: 顶层 section 不走 _redact_secret_values 递归, 只能逐字段登记
    notify = out.get("notify")
    if isinstance(notify, dict) and notify.get("url"):
        notify["url"] = REDACTION_PLACEHOLDER

    subtitles = out.get("subtitles")
    if isinstance(subtitles, dict):
        generation = subtitles.get("generation")
        if isinstance(generation, dict) and generation.get("api_key"):
            generation["api_key"] = REDACTION_PLACEHOLDER

    return out


_SECRET_KEY_PARTS = ("api_key", "apikey", "token", "secret", "password", "cookie", "credential", "dsn")


def _is_secret_key(key: object) -> bool:
    normalized = str(key).lower().replace("-", "_")
    return any(part in normalized for part in _SECRET_KEY_PARTS)


def _has_secret_value(value: object, *, key: object | None = None) -> bool:
    if key is not None and _is_secret_key(key):
        return bool(value)
    if isinstance(value, dict):
        return any(_has_secret_value(v, key=k) for k, v in value.items())
    if isinstance(value, list):
        return any(_has_secret_value(v) for v in value)
    return False


def _redact_secret_values(value: object, *, key: object | None = None) -> object:
    if key is not None and _is_secret_key(key):
        return REDACTION_PLACEHOLDER if value else value
    if isinstance(value, dict):
        return {key: _redact_secret_values(item, key=key) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_secret_values(item) for item in value]
    return value


def redact_plugin_config(config: dict[str, Any]) -> dict[str, Any]:
    """插件配置的脱敏副本: 按字段名判据替换, 与任务记录导出走同一套规则."""
    redacted = _redact_secret_values(config)
    return redacted if isinstance(redacted, dict) else {}


_OMIT = object()


def _is_redacted_form(patch: Any, current: Any) -> bool:
    """补丁值是否是当前值的脱敏形式: 占位符本身, 剥掉 userinfo 的地址, 或密码打码的 DSN.

    代理串的脱敏保留 host 与 port, 只去掉 userinfo; DSN 的脱敏只把密码换成占位符. 前端回写这
    两份时若不还原, 代理口令与数据库密码都会被悄悄丢掉. 想去掉口令时清空后再填新值.
    """
    if not isinstance(patch, str) or not isinstance(current, str):
        return False
    if patch == current:
        return False
    if patch == REDACTION_PLACEHOLDER:
        return True
    return patch in {redact_proxy(current), redact_dsn(current)}


def _restore_placeholder(patch: Any, current: Any) -> Any:
    """单值还原: 脱敏形式换成当前值, 当前值也没有时丢弃该键."""
    if patch == REDACTION_PLACEHOLDER or _is_redacted_form(patch, current):
        if current is None or current == REDACTION_PLACEHOLDER:
            return _OMIT
        return deepcopy(current)
    if isinstance(patch, dict):
        base = current if isinstance(current, dict) else {}
        restored: dict[str, Any] = {}
        for key, value in patch.items():
            item = _restore_placeholder(value, base.get(key))
            if item is not _OMIT:
                restored[key] = item
        return restored
    return patch


def restore_redacted_placeholders(patch: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """把补丁里的脱敏占位符还原成当前值.

    配置接口返回的密钥字段是 ``***``; 前端原样回写时若照抄占位符, 热配置会被覆盖成占位符而
    丢失密钥, 因此合并前先按当前配置还原. 当前值本身也没有密钥时丢弃该键, 不写入占位符.
    """
    restored = _restore_placeholder(patch, current)
    return restored if isinstance(restored, dict) else patch


def redact_proxy(proxy: str) -> str:
    """剥离 proxy URL 中的 userinfo, 保留 scheme/host/port/path."""
    try:
        parts = urlsplit(proxy)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return REDACTION_PLACEHOLDER
    if host is None:
        # 解析不出主机就不能原样回吐: 畸形串里可能就夹着 user:pass.
        return REDACTION_PLACEHOLDER
    # IPv6 主机必须补回方括号, 否则 ``2001:db8::1:1080`` 与端口无法区分.
    display = f"[{host}]" if ":" in host else host
    netloc = display if port is None else f"{display}:{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def redact_dsn(dsn: str) -> str:
    try:
        return make_url(dsn).render_as_string(hide_password=True)
    except Exception:
        return REDACTION_PLACEHOLDER
