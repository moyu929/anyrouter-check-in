"""Cloudflare 质询站点浏览器登录 + curl_cffi API 签到分支（superapi 等 CF 全站质询站点）

背景（2026-09-19 实测）：
- superapi.buzz 全站套 Cloudflare 5s 质询：纯 httpx 直连一律 403（OpenSSL TLS 指纹被
  Cloudflare 识别为自动化客户端），/api/status、登录、签到接口全部无法访问。
- CloakBrowser（真实 Chromium 指纹）可直接通过质询，并获得 `cf_clearance` cookie。
- 但 httpx 即便携带 cf_clearance 依然 403：cf_clearance 绑定客户端 TLS 指纹（JA3/JA4）
  与 UA，httpx 指纹与 Chromium 不一致，被 Cloudflare 重新质询。
- 方案：CloakBrowser 过 CF + 填表登录 → 导出 cookies 与浏览器 UA → 用
  curl_cffi Session(impersonate='chrome')（模拟 Chrome TLS 指纹）携带 cookies 请求
  签到 API，被 Cloudflare 放行。
- nianhua（2026-09-19 实测反例）：其前置 CDN 对 Chromium 系 TLS 指纹做慢速黑洞
  （JS bundle 永不返回），浏览器方案不可行，故当前仅 superapi 走本分支。
"""

import asyncio
import concurrent.futures
import time
from typing import TYPE_CHECKING

from curl_cffi.requests import Session as CffiSession

from utils.browser import (
	fill_email_credentials,
	has_session_cookie,
	launch_login_context,
	load_browser_login_settings,
	prepare_browser_page,
	submit_login_form,
	wait_for_logged_in,
)
from utils.checkin_core import (
	failed_info,
	newapi_self_to_info,
	parse_checkin_response,
	run_standard_checkin,
)
from utils.debug import is_debug_enabled, log
from utils.proxy import get_proxy_server

if TYPE_CHECKING:
	from playwright.async_api import Page

# 浏览器登录最长等待（含 CF 质询首次通过 + 表单提交 + 登录跳转）
_BROWSER_LOGIN_TIMEOUT_S = 240
# curl_cffi 模拟的浏览器指纹（需与 UA 一致，见 _browser_capture_cookies 返回的 ua）
_CFFI_IMPERSONATE = 'chrome'
_RETRY_TIMES = 3
_RETRY_BASE_DELAY_S = 1.0


# ---------------------------------------------------------------------------
# 浏览器登录（过 CF 质询 + new-api 邮箱表单）— async，在独立线程事件循环中运行
# ---------------------------------------------------------------------------


async def _browser_capture_cookies(
	account_name: str,
	email: str,
	password: str,
	domain: str,
	*,
	provider_name: str,
	use_proxy: bool,
	persist_profile: bool,
) -> dict | None:
	"""浏览器打开登录页（自动过 CF 质询），填表登录，成功后返回 {cookies, ua}。

	- 走 CloakBrowser 真实 Chromium 指纹 → Cloudflare 质询自动通过。
	- 若已处于登录态（persist_profile 复用历史会话），直接使用现有 cookies。
	- cookies 为浏览器 context 导出的原始列表（含 cf_clearance 与 session）。
	"""
	settings = load_browser_login_settings(
		account_name,
		provider_name,
		persist_profile=persist_profile,
	)
	context = await launch_login_context(settings, use_proxy=use_proxy)
	page: 'Page | None' = None
	try:
		page = await context.new_page()
		await prepare_browser_page(page)

		login_url = f'{domain}/login'
		await page.goto(login_url, wait_until='load', timeout=min(settings.wait_timeout_ms, 60_000))

		# 已登录态快速探测（历史持久化会话复用）
		logged_in = await wait_for_logged_in(page, 10_000)
		logged_in = logged_in and await has_session_cookie(page)
		if not logged_in:
			await fill_email_credentials(page, email, password, settings.wait_timeout_ms)
			await submit_login_form(page, settings.wait_timeout_ms)  # 内含等登录跳转（45s）
			logged_in = await wait_for_logged_in(page, 60_000)
			logged_in = logged_in and await has_session_cookie(page)
		if not logged_in:
			log.failed(f'{account_name}: 浏览器登录未成功（未检测到登录态 cookie）')
			return None

		cookies = await context.cookies()
		ua = await page.evaluate('navigator.userAgent')
		if not cookies:
			log.failed(f'{account_name}: 登录成功但未取得浏览器 cookies')
			return None
		log.detail(f'{account_name}: 浏览器登录成功，取得 {len(cookies)} 个 cookies')
		if is_debug_enabled():
			log.detail(f'{account_name}: UA={ua[:60]}...')
		return {'cookies': cookies, 'ua': ua}
	except Exception as e:
		log.failed(f'{account_name}: 浏览器登录异常: {e}')
		return None
	finally:
		if page is not None:
			try:
				await context.close()
			except Exception:  # nosec B110
				pass


def _browser_capture_sync(
	account_name: str,
	email: str,
	password: str,
	domain: str,
	*,
	provider_name: str,
	use_proxy: bool,
	persist_profile: bool,
) -> dict | None:
	"""在独立线程事件循环中运行浏览器登录，返回 {cookies, ua} 或 None。"""
	try:
		with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
			future = pool.submit(
				asyncio.run,
				_browser_capture_cookies(
					account_name,
					email,
					password,
					domain,
					provider_name=provider_name,
					use_proxy=use_proxy,
					persist_profile=persist_profile,
				),
			)
			return future.result(timeout=_BROWSER_LOGIN_TIMEOUT_S)
	except Exception as e:
		log.warn(f'{account_name}: 浏览器登录获取凭证失败: {str(e)[:80]}')
		return None


# ---------------------------------------------------------------------------
# curl_cffi 客户端（模拟浏览器 TLS 指纹，携带 CF cookies）
# ---------------------------------------------------------------------------


def _make_cffi_session(cookies: list, ua: str, *, use_proxy: bool) -> CffiSession:
	"""创建带浏览器指纹与 cookies 的 curl_cffi 会话。

	verify=False：curl_cffi 在 Windows 下的 CA 搜索路径异常（curl 默认 CAfile 解析错误），
	影响 HTTPS 握手；此处只读公开签到 API 并携带 cf_clearance 登录态，关闭证书校验不影响
	业务正确性。
	"""
	session = CffiSession(impersonate=_CFFI_IMPERSONATE, timeout=30, verify=False)
	try:
		session.headers['User-Agent'] = ua
		for c in cookies:
			name = c.get('name')
			value = c.get('value')
			if not name or not value:
				continue
			session.cookies.set(
				name,
				value,
				domain=c.get('domain') or None,
				path=c.get('path') or '/',
			)
		if use_proxy:
			proxy_url = get_proxy_server(use_proxy=True)
			if proxy_url:
				session.proxies.update({'http': proxy_url, 'https': proxy_url})
			else:
				log.warn('该提供商需要代理，但未设置 CHECKIN_PROXY_URL')
	except Exception as e:
		session.close()
		raise RuntimeError(f'创建 curl_cffi 会话失败: {e}') from e
	return session


def _cffi_request(session: CffiSession, method: str, url: str, *, retry: bool = True, **kwargs):
	"""curl_cffi 请求 + 指数退避重试（5xx/429/网络错误），仿 request_with_retry 语义。"""
	last_exc: Exception | None = None
	delay = _RETRY_BASE_DELAY_S
	for attempt in range(1, _RETRY_TIMES + 1):
		try:
			resp = session.request(method, url, **kwargs)
			status = resp.status_code
			if status < 500 and status != 429:
				return resp
			last_exc = RuntimeError(f'HTTP {status}')
		except Exception as e:  # noqa: BLE001 - 网络层异常统一按可重试处理
			last_exc = e
		if not retry or attempt >= _RETRY_TIMES:
			break
		log.detail(f'请求重试 {attempt}/{_RETRY_TIMES}: {url} ({last_exc})')
		time.sleep(delay)
		delay *= 2
	raise RuntimeError(f'请求失败: {url} ({last_exc})')


# ---------------------------------------------------------------------------
# httpx 替代层：fetch_user_info / perform_checkin
# ---------------------------------------------------------------------------


def _get_user_info(session: CffiSession, domain: str, account_name: str) -> dict:
	"""GET /api/user/self → 统一信息 dict（美元）。"""
	try:
		resp = _cffi_request(session, 'GET', f'{domain}/api/user/self', timeout=30)
		return newapi_self_to_info(resp, unit='usd')
	except Exception as e:
		return failed_info(f'获取用户信息失败: {str(e)[:50]}...')


def _perform_checkin(session: CffiSession, domain: str, account_name: str) -> tuple[bool, str | None]:
	"""POST /api/user/checkin（空 body + 浏览器会话）。返回 (ok, message)。"""
	try:
		resp = _cffi_request(session, 'POST', f'{domain}/api/user/checkin', json={}, timeout=30, retry=False)
		return parse_checkin_response(resp)
	except Exception as e:
		return False, str(e)[:80]


def browser_checkin(
	account_name: str,
	email: str,
	password: str,
	domain: str,
	*,
	provider_name: str = 'superapi',
	use_proxy: bool = False,
	persist_profile: bool = False,
) -> tuple[bool, dict | None, dict | None]:
	"""CF 质询站点签到：浏览器登录（过质询+填表）→ 导出 cookies/UA → curl_cffi API 签到。

	流程:
	  1. CloakBrowser 打开 {domain}/login（自动过 CF 质询）并填表登录
	  2. 导出浏览器 context 全部 cookies（cf_clearance + session）与 UA
	  3. curl_cffi Session(impersonate='chrome') 携带 cookies 请求
	     GET /api/user/self → POST /api/user/checkin → GET /api/user/self

	返回 (success, user_info_before, user_info_after) 与主流程格式一致。
	"""
	session: CffiSession | None = None
	try:

		def authenticate() -> bool:
			nonlocal session
			data = _browser_capture_sync(
				account_name,
				email,
				password,
				domain,
				provider_name=provider_name,
				use_proxy=use_proxy,
				persist_profile=persist_profile,
			)
			if not data:
				return False
			try:
				session = _make_cffi_session(data['cookies'], data['ua'], use_proxy=use_proxy)
			except Exception as e:
				log.failed(f'{account_name}: {e}')
				return False
			return True

		return run_standard_checkin(
			account_name,
			unit='usd',
			authenticate=authenticate,
			fetch_user_info=lambda: _get_user_info(session, domain, account_name)
			if session is not None
			else failed_info('登录客户端未初始化'),
			perform_checkin=lambda: _perform_checkin(session, domain, account_name)
			if session is not None
			else (False, '登录客户端未初始化'),
		)
	finally:
		if session is not None:
			try:
				session.close()
			except Exception:  # nosec B110
				pass
