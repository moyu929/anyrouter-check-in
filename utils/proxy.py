"""代理配置：读取环境变量并供浏览器 / HTTP 客户端使用。

按提供商启用：
  代理仅对 use_proxy=true 的提供商生效（内置默认 agentrouter=true，
  anyrouter/gptgod=false）。即使 CHECKIN_PROXY_URL 已设置，use_proxy=false
  的提供商仍走直连。

回退机制：
  - Mihomo 内部 fallback：🇯🇵 日本 → 🇸🇬 新加坡 → 🇭🇰 香港
    自动跳过不可用的区域，选择第一个可用的。全部不可用时 Python 侧回退到直连。
  - Python 级别：若 mihomo 本地代理整体不可达（_test_proxy 失败），
    get_proxy_server() 返回 None，HTTP 客户端直接走直连。
"""

from __future__ import annotations

import os
import urllib.parse

from utils.debug import log
from utils.proxy_selector import current_proxy_node

# 代理连通性测试缓存（进程级，按代理地址缓存）
_proxy_cache: tuple[str, bool] | None = None
_DEFAULT_PROXY_TEST_URL = 'https://www.gstatic.com/generate_204'
_PROXY_TEST_TIMEOUT = 10.0


def get_proxy_test_url() -> str:
	"""代理连通性测试地址，可用 PROXY_TEST_URL 覆盖。"""
	return os.getenv('PROXY_TEST_URL', '').strip() or _DEFAULT_PROXY_TEST_URL


def is_proxy_configured() -> bool:
	"""CHECKIN_PROXY_URL 是否已设置（不检测连通性）。"""
	return bool(os.getenv('CHECKIN_PROXY_URL', '').strip())


def redact_proxy_url(proxy_url: str) -> str:
	"""隐藏代理 URL 中的用户名和密码，避免凭据进入日志。"""
	try:
		parts = urllib.parse.urlsplit(proxy_url)
		if not parts.hostname:
			return '<configured>'
		host = parts.hostname
		if ':' in host and not host.startswith('['):
			host = f'[{host}]'
		port = f':{parts.port}' if parts.port is not None else ''
		return urllib.parse.urlunsplit((parts.scheme, f'{host}{port}', parts.path, '', ''))
	except (TypeError, ValueError):
		return '<configured>'


def _test_proxy(proxy_url: str) -> bool:
	"""测试代理是否可用（通过代理请求测试 URL，超时 10 秒）。"""
	try:
		import httpx

		with httpx.Client(proxy=proxy_url, timeout=_PROXY_TEST_TIMEOUT, trust_env=False) as client:
			r = client.get(get_proxy_test_url())
			return r.status_code in (200, 204)
	except Exception:
		return False


def get_proxy_server(*, use_proxy: bool = True) -> str | None:
	"""按提供商配置读取 CHECKIN_PROXY_URL，自动测试连通性，不可用时回退到直连。

	参数:
	  use_proxy: 提供商是否需要代理。False 时直接返回 None（不读环境变量、
	             不做连通性测试），确保 use_proxy=false 的提供商始终直连。
	"""
	global _proxy_cache

	if not use_proxy:
		return None

	server = os.getenv('CHECKIN_PROXY_URL', '').strip()
	if not server:
		return None

	# 首次使用该地址时测试连通性；环境变量切换后必须重新测试。
	if _proxy_cache is None or _proxy_cache[0] != server:
		working = _test_proxy(server)
		_proxy_cache = (server, working)
		redacted_server = redact_proxy_url(server)
		if working:
			node = current_proxy_node()
			node_info = f'（节点 {node}）' if node else ''
			log.detail(f'代理连通性正常: {redacted_server}{node_info}')
		else:
			log.warn(f'代理 {redacted_server} 不可达（测试地址: {get_proxy_test_url()}），回退到直连')

	return server if _proxy_cache[1] else None


def get_playwright_proxy(*, use_proxy: bool = True) -> dict[str, str] | None:
	server = get_proxy_server(use_proxy=use_proxy)
	if not server:
		return None
	return {'server': server}


def reset_proxy_cache() -> None:
	"""重置代理连通性缓存（测试和节点切换后复测用）。"""
	global _proxy_cache
	_proxy_cache = None


def needs_proxy(app_config, accounts) -> bool:
	"""判断是否存在 use_proxy=true 的提供商（任一账号需要走代理）。

	用于决定是否值得初始化代理 / 执行节点选择，避免无用初始化。
	"""
	for account in accounts:
		provider = app_config.get_provider(account.provider)
		if provider is not None and provider.use_proxy:
			return True
	return False
