#!/usr/bin/env python3
"""
AnyRouter.top 自动签到脚本
"""

import asyncio
import concurrent.futures
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from urllib.parse import urlencode, urlparse

import httpx

if hasattr(sys.stdout, 'reconfigure'):
	sys.stdout.reconfigure(line_buffering=True)
if hasattr(sys.stderr, 'reconfigure'):
	sys.stderr.reconfigure(line_buffering=True)

from cloakbrowser import launch_async
from dotenv import load_dotenv

# 必须在导入 utils.* 之前加载 .env：notify 是模块级实例，
# 若先导入则 .env 中的通知渠道配置全部读不到。
load_dotenv()

from utils.browser import (
	BrowserLoginResult,
	fetch_user_self_via_browser,
	has_session_cookie,
	is_logged_in,
	launch_login_context,
	load_browser_login_settings,
	login_with_email_form,
	navigate_login_page,
	prepare_browser_page,
	save_login_screenshot,
	take_pending_screenshots,
	verify_browser_login,
	wait_for_waf_ready,
)
from utils.checkin_core import build_user_info, is_already_checked, quota_to_currency
from utils.checkin_core import format_amount as _format_amount
from utils.config import AccountConfig, AppConfig, load_accounts_config
from utils.debug import is_debug_enabled, log
from utils.gptgod import gptgod_checkin
from utils.guyscode import guyscode_checkin
from utils.http_client import API_HEADERS, RetryExhaustedError, create_client, request_with_retry
from utils.newapi_jwt import newapi_jwt_checkin
from utils.newapi_session import newapi_session_checkin
from utils.notify import notify
from utils.proxy import get_playwright_proxy, is_proxy_configured, needs_proxy, redact_proxy_url
from utils.proxy_selector import NodeSelector, available, current_proxy_node, no_available_node

BALANCE_HASH_FILE = 'balance_hash.txt'
BALANCE_SNAPSHOT_FILE = 'balance_snapshot.json'
CHECKIN_RESULT_FILE = 'checkin_result.json'

# 网络层异常（代理节点问题），用于触发"切换节点重试"
_NETWORK_ERRORS = (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError)
# 阿里云 WAF 拦截页特征字符串
_WAF_BLOCK_MARKER = 'aliyun_waf_aa'


class ProxyNodeIssue(Exception):
	"""代理节点问题：目标站点经代理访问被 WAF 拦截 / 5xx / 网络异常 / 超时。

	由外层 check_in_account_with_retry 捕获，切换代理节点后重试。
	"""


def _is_node_issue_exception(e: Exception) -> bool:
	"""判断异常是否属于代理节点问题（网络异常，或 5xx 重试耗尽后的 RuntimeError）。"""
	if isinstance(e, _NETWORK_ERRORS):
		return True
	if isinstance(e, RetryExhaustedError):
		return True
	return False


def load_balance_hash():
	"""加载余额hash"""
	try:
		if os.path.exists(BALANCE_HASH_FILE):
			with open(BALANCE_HASH_FILE, 'r', encoding='utf-8') as f:
				return f.read().strip()
	except Exception:  # nosec B110
		pass
	return None


def save_balance_hash(balance_hash):
	"""保存余额hash"""
	try:
		with open(BALANCE_HASH_FILE, 'w', encoding='utf-8') as f:
			f.write(balance_hash)
	except Exception as e:
		log.warn(f'警告: 保存余额哈希失败: {e}')


def load_balance_snapshot() -> dict[str, float]:
	"""加载跨日总额快照 {账号名: 签到后总额}。

	用于还原"登录即自动签到"类账号（agentrouter/gorouter）的当日签到奖励：
	单次运行拿到的余额已是签到后，无法像手动签到那样前后对比，故用昨日总额快照跨日估算。
	"""
	try:
		if os.path.exists(BALANCE_SNAPSHOT_FILE):
			with open(BALANCE_SNAPSHOT_FILE, 'r', encoding='utf-8') as f:
				data = json.load(f)
			if isinstance(data, dict):
				return {k: float(v) for k, v in data.items() if isinstance(v, (int, float))}
	except Exception:  # nosec B110
		pass
	return {}


def save_balance_snapshot(snapshot: dict[str, float]) -> None:
	"""保存跨日总额快照。"""
	try:
		with open(BALANCE_SNAPSHOT_FILE, 'w', encoding='utf-8') as f:
			json.dump(snapshot, f, ensure_ascii=False, sort_keys=True)
	except Exception as e:
		log.warn(f'保存余额快照失败: {e}')


def generate_balance_hash(balances):
	"""生成余额数据的hash"""
	simple_balances = (
		{k: {'quota': v.get('quota'), 'used': v.get('used')} for k, v in balances.items()} if balances else {}
	)
	balance_json = json.dumps(simple_balances, sort_keys=True, separators=(',', ':'))
	return hashlib.sha256(balance_json.encode('utf-8')).hexdigest()[:16]


def load_retry_indexes() -> list[int] | None:
	"""解析重试模式要处理的账号原始索引（CHECKIN_RETRY_INDEXES，JSON 数组）。

	返回 None 表示非重试模式；返回列表表示仅处理这些账号（单次尝试，不做节点多次重试）。
	索引为账号在 ANYROUTER_ACCOUNTS 中的 0 基位置，与 checkin_result.json 里
	记录的失败索引一一对应，保证跨工作流（签到 → 重试）筛选稳定。
	"""
	raw = os.getenv('CHECKIN_RETRY_INDEXES', '').strip()
	if not raw:
		return None
	try:
		data = json.loads(raw)
		if not isinstance(data, list):
			raise ValueError('不是数组')
		return sorted({int(x) for x in data})
	except Exception as e:
		log.warn(f'CHECKIN_RETRY_INDEXES 无效: {raw!r} ({e})，忽略重试筛选')
		return None


def save_checkin_result(failed_indexes: list[int], total: int) -> None:
	"""保存本次签到结果，供每日重试工作流判断哪些账号需要补签。

	failed_indexes: 失败账号的原始配置索引（0 基）。
	"""
	try:
		payload = {
			'date': datetime.now().strftime('%Y-%m-%d'),
			'total': total,
			'failed': failed_indexes,
		}
		with open(CHECKIN_RESULT_FILE, 'w', encoding='utf-8') as f:
			json.dump(payload, f, ensure_ascii=False)
	except Exception as e:
		log.warn(f'保存签到结果失败: {e}')


def parse_cookies(cookies_data):
	"""解析 cookies 数据"""
	if isinstance(cookies_data, dict):
		return cookies_data

	if isinstance(cookies_data, str):
		cookies_dict = {}
		for cookie in cookies_data.split(';'):
			if '=' in cookie:
				key, value = cookie.strip().split('=', 1)
				cookies_dict[key] = value
		return cookies_dict
	return {}


def _cookies_for_domain(client: httpx.Client, domain: str) -> dict[str, str]:
	"""只提取指定站点域的 cookies，避免 OAuth 跨域 cookie 泄露。"""
	host = urlparse(domain).hostname or ''
	cookies: dict[str, str] = {}
	for cookie in client.cookies.jar:
		cookie_domain = (cookie.domain or '').lstrip('.').lower()
		if cookie_domain and (host == cookie_domain or host.endswith(f'.{cookie_domain}')):
			if cookie.name and cookie.value:
				cookies[cookie.name] = cookie.value
	return cookies


async def get_waf_cookies_with_browser(
	account_name: str,
	login_url: str,
	required_cookies: list[str],
	*,
	use_proxy: bool = False,
):
	"""使用浏览器获取 WAF cookies。"""
	log.detail(f'{account_name}: 启动浏览器获取 WAF cookies...')

	launch_kwargs: dict = {'headless': True}
	proxy = get_playwright_proxy(use_proxy=use_proxy)
	if proxy:
		launch_kwargs['proxy'] = proxy
	browser = None
	try:
		browser = await launch_async(**launch_kwargs)
		page = await browser.new_page()
		await prepare_browser_page(page)
		log.detail(f'{account_name}: 访问登录页面获取初始 cookies...')

		await page.goto(login_url, wait_until='domcontentloaded')
		await wait_for_waf_ready(page)

		cookies = await page.context.cookies()
		waf_cookies = {
			cookie['name']: cookie['value']
			for cookie in cookies
			if cookie.get('name') in required_cookies and cookie.get('value') is not None
		}

		log.detail(f'{account_name}: 获取到 {len(waf_cookies)} 个 WAF cookies')
		missing_cookies = [name for name in required_cookies if name not in waf_cookies]
		if missing_cookies:
			log.failed(f'{account_name}: 缺少 WAF cookies: {missing_cookies}')
			return None

		log.detail(f'{account_name}: 成功获取所有 WAF cookies')
		return waf_cookies
	except Exception as e:
		log.failed(f'{account_name}: 获取 WAF cookies 时出错: {e}')
		return None
	finally:
		if browser is not None:
			await browser.close()


async def login_with_credentials(
	account_name: str,
	provider_config,
	provider_name: str,
	email: str,
	password: str,
) -> BrowserLoginResult | None:
	"""使用邮箱密码通过浏览器登录，返回 cookies 与拦截到的 api user id。"""
	log.detail(f'{account_name}: 正在使用邮箱密码登录...')

	login_url = f'{provider_config.domain}{provider_config.login_path}'
	settings = load_browser_login_settings(
		account_name,
		provider_name,
		persist_profile=provider_config.persist_profile,
	)
	timeout_ms = settings.wait_timeout_ms

	log.debug(
		f'{account_name}: 浏览器配置文件={settings.profile_dir}, '
		f'持久化={settings.persist_profile}, 无头模式={settings.headless}, '
		f'人性化={settings.humanize}, 超时={timeout_ms}ms'
	)

	log.detail(f'{account_name}: 提供商代理={"已启用" if provider_config.use_proxy else "已禁用"} ({provider_name})')

	context = None
	page = None
	try:
		context = await launch_login_context(settings, use_proxy=provider_config.use_proxy)
		page = await context.new_page()
		await prepare_browser_page(page)
		await navigate_login_page(
			page,
			login_url,
			timeout_ms,
			provider=provider_name,
			account_name=account_name,
		)

		if not await is_logged_in(page):
			if await has_session_cookie(page):
				log.warn(f'{account_name}: 登录页面存在过期 session cookie，强制邮箱登录')
			await save_login_screenshot(page, provider_name, account_name, 'before-email-login')
			await login_with_email_form(
				page,
				email,
				password,
				timeout_ms,
				provider=provider_name,
				account_name=account_name,
			)
		else:
			log.detail(f'{account_name}: 浏览器配置文件已登录')

		console_url = f'{provider_config.domain}/console'
		user_profile = await verify_browser_login(page, console_url, timeout_ms)
		if not user_profile:
			cookies = await context.cookies()
			cookie_names = [c.get('name') for c in cookies if c.get('name')]
			log.failed(f'{account_name}: 登录失败 - /api/user/self 未验证')
			log.debug(f'{account_name}: 当前 URL: {page.url}')
			log.debug(f'{account_name}: 获取到 cookies: {cookie_names}')
			await save_login_screenshot(page, provider_name, account_name, 'not-authenticated')
			return None

		cookies = await context.cookies()
		all_cookies: dict[str, str] = {}
		for cookie in cookies:
			cookie_name = cookie.get('name')
			cookie_value = cookie.get('value')
			if cookie_name and cookie_value:
				all_cookies[cookie_name] = cookie_value
		api_user = str(user_profile['id']) if user_profile.get('id') is not None else None

		log.success(f'{account_name}: 登录成功，已获取 {len(all_cookies)} 个 cookies')
		if is_debug_enabled() and api_user:
			log.debug(f'{account_name}: api_user={api_user}')
		return BrowserLoginResult(cookies=all_cookies, api_user=api_user)

	except Exception as e:
		log.failed(f'{account_name}: 登录过程中出错: {e}')
		if page is not None:
			await save_login_screenshot(page, provider_name, account_name, 'login-error')
		return None
	finally:
		if context is not None:
			await context.close()


def _extract_oauth_state(state_data: object) -> str | None:
	"""从 OAuth state 接口响应的 data 字段提取 state 值。

	兼容两种形态：旧版 data 为字符串；新版（gorouter 2026-08 起）为
	{"flow_token": ..., "expires_at": ...} 对象。
	"""
	if isinstance(state_data, str) and state_data:
		return state_data
	if isinstance(state_data, dict):
		token = state_data.get('flow_token')
		if isinstance(token, str) and token:
			return token
	return None


async def login_with_github_oauth(
	account_name: str,
	provider_config,
	github_session: str,
	*,
	force_direct: bool = False,
) -> BrowserLoginResult | None:
	"""使用 GitHub OAuth 重放登录，返回 cookies 与 api_user。

	自动探测站点协议版本：
	  * 新版 new-api（gorouter 2026-08 起）：POST /api/oauth/state（body 带 provider），
	    state 取响应 data.flow_token；authorize 带 scope=user:email；回调不带 mode；
	    回调返回 JWT access_token，后续 API 走 Bearer 鉴权（session cookie 已失效）。
	  * 旧版：GET /api/oauth/state?mode=login，data 为字符串；回调带 mode=login。

	参数:
	  force_direct: 强制直连（忽略代理）。用于节点耗尽后的兜底重试。
	"""
	log.detail(f'{account_name}: 正在使用 GitHub OAuth 登录...')

	domain = provider_config.domain
	use_proxy = provider_config.use_proxy and not force_direct
	client = create_client(
		headers={
			'Referer': domain,
			'Origin': domain,
		},
		use_proxy=use_proxy,
	)
	if use_proxy and not is_proxy_configured():
		log.warn(f'{account_name}: 提供商需要代理但未配置 CHECKIN_PROXY_URL')

	github_client_id = provider_config.oauth_client_id

	with client:
		# Step 1: 获取 OAuth state（先探测新版 POST 协议，未升级站点回退旧版 GET）
		log.debug(f'{account_name}: 第1步 - 获取 OAuth 状态...')
		state_url = f'{domain}{provider_config.oauth_state_path}'
		state = None
		new_protocol = False
		try:
			probe_resp = request_with_retry(
				client, 'POST', state_url, json={'provider': 'github', 'intent': 'login'}, timeout=30
			)
		except Exception as e:
			if _is_node_issue_exception(e):
				raise ProxyNodeIssue(f'OAuth 状态请求异常（可能节点问题）: {e}') from e
			log.failed(f'{account_name}: OAuth 状态请求失败: {e}')
			return None

		# 新版协议探测：仅接受 200 + JSON + success + 可提取 state 的响应；
		# 旧版站点会返回 405/400/WAF 拦截页，一律回退旧版 GET
		if probe_resp.status_code == 200 and _WAF_BLOCK_MARKER not in probe_resp.text:
			try:
				probe_data = probe_resp.json()
			except Exception:
				probe_data = None
			if isinstance(probe_data, dict) and probe_data.get('success'):
				extracted = _extract_oauth_state(probe_data.get('data'))
				if extracted:
					state = extracted
					new_protocol = True
					log.detail(f'{account_name}: 站点使用新版 OAuth 协议（POST state）')

		if not new_protocol:
			try:
				resp = request_with_retry(client, 'GET', f'{state_url}?mode=login', timeout=30)
			except Exception as e:
				if _is_node_issue_exception(e):
					raise ProxyNodeIssue(f'OAuth 状态请求异常（可能节点问题）: {e}') from e
				log.failed(f'{account_name}: OAuth 状态请求失败: {e}')
				return None

			if resp.status_code != 200:
				log.failed(f'{account_name}: OAuth 状态返回 HTTP {resp.status_code}')
				return None

			# WAF 拦截检测（与原始项目一致）
			if _WAF_BLOCK_MARKER in resp.text:
				raise ProxyNodeIssue(f'{account_name}: 被阿里云 WAF 拦截，请尝试使用代理')

			try:
				state_data = resp.json()
			except Exception:
				log.failed(f'{account_name}: OAuth 状态响应非 JSON 格式')
				return None

			if not state_data.get('success'):
				log.failed(f'{account_name}: OAuth 状态失败: {state_data}')
				return None

			state = state_data.get('data', '')
			if not state:
				log.failed(f'{account_name}: OAuth 状态为空')
				return None

			log.detail(f'{account_name}: 获取到 OAuth 状态（cookies: {list(client.cookies.keys())}）')

		# Step 2: 用 GitHub user_session 获取授权 code（新版与站点前端一致带 scope）
		log.debug(f'{account_name}: 第2步 - 获取 GitHub OAuth code...')
		auth_params: dict[str, str] = {'client_id': github_client_id, 'state': state}
		if new_protocol:
			auth_params['scope'] = 'user:email'
		auth_url = f'https://github.com/login/oauth/authorize?{urlencode(auth_params)}'
		try:
			# 用显式 Cookie 头而非 per-request cookies=（后者已被 httpx 弃用），
			# 同时避免 GitHub 会话进入 client 的 cookie jar 后被带到站点域。
			auth_resp = request_with_retry(
				client,
				'GET',
				auth_url,
				headers={'Cookie': f'user_session={github_session}'},
				follow_redirects=False,
				timeout=30,
			)
		except Exception as e:
			if _is_node_issue_exception(e):
				raise ProxyNodeIssue(f'GitHub OAuth 授权请求异常（可能节点问题）: {e}') from e
			log.failed(f'{account_name}: GitHub OAuth 授权失败: {e}')
			return None

		if auth_resp.status_code in (401, 403):
			log.failed(f'{account_name}: GitHub user_session 已过期 (HTTP {auth_resp.status_code})')
			return None

		if auth_resp.status_code != 302:
			log.failed(f'{account_name}: GitHub 未重定向 (HTTP {auth_resp.status_code}), user_session 可能无效')
			return None

		location = auth_resp.headers.get('Location', '')
		code_match = re.search(r'[?&]code=([^&]+)', location)
		if not code_match:
			log.failed(f'{account_name}: 重定向地址中未找到 code')
			return None

		code = code_match.group(1)
		log.detail(f'{account_name}: 获取到 GitHub OAuth code')

		# Step 3: OAuth 回调，触发登录+签到（新版协议不带 mode 参数）
		log.debug(f'{account_name}: 第3步 - OAuth 回调...')
		callback_params: dict[str, str] = {'code': code, 'state': state}
		if not new_protocol:
			callback_params['mode'] = 'login'
		callback_url = f'{domain}{provider_config.oauth_callback_path}?{urlencode(callback_params)}'
		try:
			cb_resp = request_with_retry(client, 'GET', callback_url, timeout=30)
		except Exception as e:
			if _is_node_issue_exception(e):
				raise ProxyNodeIssue(f'OAuth 回调请求异常（可能节点问题）: {e}') from e
			log.failed(f'{account_name}: OAuth 回调失败: {e}')
			return None

		if cb_resp.status_code != 200:
			log.failed(f'{account_name}: OAuth 回调返回 HTTP {cb_resp.status_code}')
			return None

		# WAF 拦截检测（与原始项目一致）
		if _WAF_BLOCK_MARKER in cb_resp.text:
			raise ProxyNodeIssue(f'{account_name}: OAuth 回调被阿里云 WAF 拦截')

		try:
			cb_data = cb_resp.json()
		except Exception:
			log.failed(f'{account_name}: OAuth 回调响应非 JSON 格式')
			return None

		if not cb_data.get('success'):
			log.failed(f'{account_name}: OAuth 回调失败: {cb_data}')
			return None

		user_data = cb_data.get('data', {})
		# api_user：新版在 data.user.id，旧版在 data.id
		user_obj = user_data.get('user') if isinstance(user_data, dict) else None
		if isinstance(user_obj, dict) and user_obj.get('id') is not None:
			api_user = str(user_obj['id'])
		elif isinstance(user_data, dict) and user_data.get('id') is not None:
			api_user = str(user_data['id'])
		else:
			api_user = None
		# 新版回调返回 JWT access_token，后续 API 需 Bearer 鉴权
		bearer_token = user_data.get('access_token') if isinstance(user_data, dict) else None
		checked_in = user_data.get('checked_in') if isinstance(user_data, dict) else None
		if checked_in is not None:
			log.detail(f'{account_name}: 登录成功，已签到状态={checked_in}')
		else:
			log.detail(f'{account_name}: 登录成功（{"新版 JWT 鉴权" if bearer_token else "无用户信息"}）')

		all_cookies = _cookies_for_domain(client, domain)
		log.success(f'{account_name}: OAuth 登录成功，获取到 {len(all_cookies)} 个 cookies')
		return BrowserLoginResult(cookies=all_cookies, api_user=api_user, bearer_token=bearer_token)


def get_user_info(client, headers, user_info_url: str, *, propagate_network_error: bool = True):
	"""获取用户信息（带重试）。

	propagate_network_error=True（默认）时，网络异常及可重试状态耗尽会向上抛出，
	交由外层触发代理节点切换重试。签到成功后查询余额应传 False，避免已成功的
	签到被误判为节点问题而重复签到。
	"""
	try:
		response = request_with_retry(client, 'GET', user_info_url, headers=headers, timeout=30)

		if response.status_code == 200:
			# 阿里云 WAF 拦截页（HTTP 200 + HTML），会令 response.json() 报
			# "Expecting value: line 1 column 1 (char 0)"。识别后交给外层切换
			# 代理节点重试，避免被误判成普通失败而错失当日签到。
			if _WAF_BLOCK_MARKER in response.text:
				if propagate_network_error:
					raise ProxyNodeIssue('获取用户信息被阿里云 WAF 拦截')
				return {'success': False, 'error': '获取用户信息失败: 被阿里云 WAF 拦截'}
			data = response.json()
			if data.get('success'):
				user_data = data.get('data', {})
				# 无 unit 形态（主流程旧契约，通知层按美元渲染）
				return build_user_info(
					quota_to_currency(user_data.get('quota', 0)),
					quota_to_currency(user_data.get('used_quota', 0)),
				)
		return {'success': False, 'error': f'获取用户信息失败: HTTP {response.status_code}'}
	except ProxyNodeIssue:
		raise
	except _NETWORK_ERRORS as e:
		if propagate_network_error:
			raise
		return {'success': False, 'error': f'获取用户信息失败: {str(e)[:50]}...'}
	except RetryExhaustedError as e:
		if propagate_network_error:
			raise
		return {'success': False, 'error': f'获取用户信息失败: {str(e)[:50]}...'}
	except RuntimeError as e:
		return {'success': False, 'error': f'获取用户信息失败: {str(e)[:50]}...'}
	except Exception as e:
		return {'success': False, 'error': f'获取用户信息失败: {str(e)[:50]}...'}


def _user_info_from_profile(profile: dict, unit: str = 'usd') -> dict:
	"""把浏览器取到的 user/self profile 转成统一的用户信息 dict。"""
	return build_user_info(
		quota_to_currency(profile.get('quota', 0)),
		quota_to_currency(profile.get('used_quota', 0)),
		unit,
	)


def _fetch_user_info_via_browser_sync(
	account_name: str,
	provider: str,
	domain: str,
	cookies: dict,
	*,
	use_proxy: bool = False,
) -> dict | None:
	"""在独立线程事件循环里用真浏览器获取用户余额（WAF JS 挑战兜底）。

	run_check_in_requests 为同步、浏览器为 async，故放进独立线程跑独立 asyncio 循环。
	真浏览器能自动解阿里云 WAF 的 JS 挑战并拿到 /api/user/self 的 JSON。
	"""
	try:
		with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
			future = pool.submit(
				asyncio.run,
				fetch_user_self_via_browser(
					account_name, provider, domain, cookies, use_proxy=use_proxy, persist_profile=False
				),
			)
			profile = future.result(timeout=120)
	except Exception as e:
		log.warn(f'{account_name}: 浏览器兜底获取用户信息失败: {str(e)[:80]}')
		return None
	return _user_info_from_profile(profile, 'usd') if profile else None


def _get_user_info_resilient(
	client,
	headers,
	user_info_url: str,
	account_name: str,
	account: AccountConfig,
	provider_config,
	all_cookies: dict,
	*,
	use_proxy: bool,
	browser_cache: dict,
	propagate_network_error: bool = True,
) -> dict:
	"""httpx 取用户信息；被阿里云 WAF 挑战时改用真浏览器兜底。

	proxy_node_issue 由 get_user_info 在识别到 WAF 拦截页时抛出。此处捕获后
	先尝试浏览器兜底取余额；若浏览器也失败，按 propagate_network_error 决定：
	  True  → 继续向上抛，交由外层切换节点重试（手动签到型默认，签到还没做）
	  False → 返回失败 dict（自动签到型：登录已触发签到，余额仅为展示，
	          不值得反复重新登录去拿余额，反而增加风控风险）
	"""
	try:
		return get_user_info(client, headers, user_info_url)
	except ProxyNodeIssue as e:
		# 仅尝试一次浏览器兜底，结果（含 None）缓存，避免多次 WAF 时重复启动浏览器
		if 'browser_info' not in browser_cache:
			log.warn(f'{account_name}: httpx 获取用户信息被拦截（{e}），改用浏览器获取余额')
			browser_cache['browser_info'] = _fetch_user_info_via_browser_sync(
				account_name,
				account.provider,
				provider_config.domain,
				all_cookies,
				use_proxy=use_proxy,
			)
		if browser_cache.get('browser_info'):
			log.info(f'{account_name}: 浏览器兜底获取余额成功: {browser_cache["browser_info"]["display"]}')
			return browser_cache['browser_info']
		if propagate_network_error:
			raise
		return {'success': False, 'error': f'余额查询失败（WAF/网络异常）: {str(e)[:50]}'}


async def prepare_cookies(account_name: str, provider_config, user_cookies: dict) -> dict | None:
	"""准备请求所需的 cookies（可能包含 WAF cookies）"""
	waf_cookies = {}

	if provider_config.needs_waf_cookies():
		login_url = f'{provider_config.domain}{provider_config.login_path}'
		waf_cookies = await get_waf_cookies_with_browser(
			account_name,
			login_url,
			provider_config.waf_cookie_names,
			use_proxy=provider_config.use_proxy,
		)
		if not waf_cookies:
			log.failed(f'{account_name}: 无法获取 WAF cookies')
			return None
	else:
		log.detail(f'{account_name}: 无需绕过 WAF，直接使用用户 cookies')

	return {**user_cookies, **waf_cookies}


def execute_check_in(client, account_name: str, provider_config, headers: dict):
	"""执行签到请求（带重试）"""
	log.debug(f'{account_name}: 执行签到请求')

	checkin_headers = headers.copy()
	checkin_headers.update({'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'})

	sign_in_url = f'{provider_config.domain}{provider_config.sign_in_path}'
	response = request_with_retry(client, 'POST', sign_in_url, headers=checkin_headers, timeout=30)

	log.debug(f'{account_name}: 响应状态码 {response.status_code}')

	if response.status_code == 200:
		# 阿里云 WAF 拦截页（HTTP 200 + HTML）：识别后交给外层切换代理节点重试，
		# 避免被当作普通签到失败而错失当日签到。
		if _WAF_BLOCK_MARKER in response.text:
			raise ProxyNodeIssue(f'{account_name}: 签到被阿里云 WAF 拦截')
		try:
			result = response.json()
			if result.get('ret') == 1 or result.get('code') == 0 or result.get('success'):
				log.detail(f'{account_name}: 签到 API 返回成功')
				return True
			else:
				error_msg = result.get('msg', result.get('message', 'Unknown error'))
				# 幂等判定统一走中枢（关键词表为全项目唯一事实来源）
				if is_already_checked(error_msg):
					log.detail(f'{account_name}: 签到 API 返回"今日已签到"')
					return True
				log.failed(f'{account_name}: 签到失败 - {error_msg}')
				return False
		except json.JSONDecodeError:
			if 'success' in response.text.lower():
				log.detail(f'{account_name}: 签到 API 返回成功')
				return True
			else:
				log.failed(f'{account_name}: 签到失败 - 响应格式无效')
				return False
	else:
		log.failed(f'{account_name}: 签到失败 - HTTP {response.status_code}')
		return False


def format_check_in_notification(detail: dict) -> str:
	"""格式化单账号签到通知条目（Markdown，账号名加粗）。

	统一字段：签到前余额 / 签到获得 / 签到后余额 / 累积消耗。
	场景收敛：
	  * 正常签到（有奖励）— 四字段全展示；
	  * 今日已签到（无奖励）— 前后一致不重复，展示当前余额 + 累积消耗；
	  * 跨日估算（agentrouter/gorouter 登录即签到）— 无真实 before 基线，
	    展示估算奖励 + 当前余额 + 累积消耗。
	"""
	unit = detail.get('unit', 'usd')

	def amount(value: float) -> str:
		return _format_amount(value, unit)

	index = detail.get('index')
	header = f'**{index}. {detail["name"]}**' if index else f'**{detail["name"]}**'

	# 自动签到型跨日估算：单次 before/after 均为签到后，无真实基线
	if detail.get('cross_day_estimated'):
		lines = [f'{header} ✅']
		if detail['check_in_reward'] != 0:
			lines.append(f'签到获得(跨日估算): +{amount(detail["check_in_reward"])}')
		lines.append(f'当前余额: {amount(detail["after_quota"])}')
		lines.append(f'累积消耗: {amount(detail["after_used"])}')
		return '\n'.join(lines)

	# 负奖励（总额减少，罕见）按无奖励处理，避免显示 "+$-0.50" 之类的畸形行
	has_reward = detail['check_in_reward'] > 0
	has_usage = detail['usage_increase'] != 0

	if not has_reward and not has_usage:
		# 今日已签到、余额无变化：前后一致不必重复展示
		return '\n'.join(
			[
				f'{header} ✅ 今日已签到',
				f'当前余额: {amount(detail["after_quota"])}',
				f'累积消耗: {amount(detail["after_used"])}',
			]
		)

	if has_reward:
		lines = [
			f'{header} ✅',
			f'签到前余额: {amount(detail["before_quota"])}',
			f'签到获得: +{amount(detail["check_in_reward"])}',
			f'签到后余额: {amount(detail["after_quota"])}',
			f'累积消耗: {amount(detail["after_used"])}',
		]
	else:
		# 今日已签到（期间有消耗）：无奖励，仅展示前后余额与累积消耗
		lines = [
			f'{header} ✅ 今日已签到（期间有消耗）',
			f'签到前余额: {amount(detail["before_quota"])}',
			f'签到后余额: {amount(detail["after_quota"])}',
			f'累积消耗: {amount(detail["after_used"])}',
		]
	return '\n'.join(lines)


async def check_in_account(
	account: AccountConfig,
	account_index: int,
	app_config: AppConfig,
	*,
	force_direct: bool = False,
):
	"""为单个账号执行签到操作

	参数:
	  force_direct: 强制直连（忽略代理）。用于节点耗尽后的兜底重试。
	"""
	account_name = account.get_display_name(account_index)

	provider_config = app_config.get_provider(account.provider)
	if not provider_config:
		log.failed(f'{account_name}: 提供商 "{account.provider}" 未在配置中找到')
		return False, None, {'success': False, 'error': f'提供商 "{account.provider}" 未在配置中找到'}

	proxy_mode = '走代理' if (provider_config.use_proxy and not force_direct) else '直连'
	log.info(f'{account_name}: 使用提供商 "{account.provider}" ({provider_config.domain})，{proxy_mode}')

	# GPTGod 纯 API 签到（无需浏览器，自带登录+签到全流程）
	if provider_config.auth_method == 'gptgod':
		if not account.has_login_credentials():
			log.failed(f'{account_name}: GPTGod 提供商需要邮箱和密码')
			return False, None, {'success': False, 'error': 'GPTGod 提供商需要邮箱和密码'}
		assert account.email is not None and account.password is not None
		log.info(f'{account_name}: 正在尝试 GPTGod API 签到...')
		success, info_before, info_after = gptgod_checkin(
			account_name,
			account.email,
			account.password,
			use_proxy=provider_config.use_proxy and not force_direct,
		)
		return success, info_before, info_after

	# Guyscode 独立分支（浏览器登录捕获 JWT + httpx 主动签到）
	if provider_config.auth_method == 'guyscode':
		if not account.has_login_credentials():
			log.failed(f'{account_name}: Guyscode 提供商需要邮箱和密码')
			return False, None, {'success': False, 'error': 'Guyscode 提供商需要邮箱和密码'}
		assert account.email is not None and account.password is not None
		log.info(f'{account_name}: 正在尝试 Guyscode 签到...')
		success, info_before, info_after = guyscode_checkin(
			account_name,
			account.email,
			account.password,
			use_proxy=provider_config.use_proxy and not force_direct,
		)
		return success, info_before, info_after

	# New-API JWT 纯 API 签到（新版 new-api：登录免 Turnstile，全程 httpx）
	if provider_config.auth_method == 'newapi_jwt':
		if not account.has_login_credentials():
			log.failed(f'{account_name}: {provider_config.name} 提供商需要邮箱和密码')
			return False, None, {'success': False, 'error': f'{provider_config.name} 提供商需要邮箱和密码'}
		assert account.email is not None and account.password is not None
		log.info(f'{account_name}: 正在尝试 {provider_config.name} 纯 API 签到...')
		success, info_before, info_after = newapi_jwt_checkin(
			account_name,
			account.email,
			account.password,
			domain=provider_config.domain,
			use_proxy=provider_config.use_proxy and not force_direct,
		)
		return success, info_before, info_after

	# 老版 New-API 纯 API 签到（hcnsec 等：邮箱 API 登录 + session cookie + New-Api-User 头）
	if provider_config.auth_method == 'newapi_session':
		if not account.has_login_credentials():
			log.failed(f'{account_name}: {provider_config.name} 提供商需要邮箱和密码')
			return False, None, {'success': False, 'error': f'{provider_config.name} 提供商需要邮箱和密码'}
		assert account.email is not None and account.password is not None
		log.info(f'{account_name}: 正在尝试 {provider_config.name} 纯 API 签到...')
		success, info_before, info_after = newapi_session_checkin(
			account_name,
			account.email,
			account.password,
			domain=provider_config.domain,
			use_proxy=provider_config.use_proxy and not force_direct,
		)
		return success, info_before, info_after

	# 优先使用 OAuth 登录（如 GitHub OAuth）
	all_cookies = None
	resolved_api_user: str | None = None
	resolved_bearer_token: str | None = None
	auth_method = None
	if provider_config.is_oauth() and account.has_oauth_credentials():
		log.detail(f'{account_name}: 正在尝试 OAuth 登录 (github_session)...')
		assert account.github_session is not None
		login_result = await login_with_github_oauth(
			account_name,
			provider_config,
			account.github_session,
			force_direct=force_direct,
		)
		if login_result:
			all_cookies = login_result.cookies
			resolved_api_user = login_result.api_user
			resolved_bearer_token = login_result.bearer_token
			auth_method = 'GitHub OAuth'
		else:
			log.failed(f'{account_name}: OAuth 登录失败')
			return False, None, {'success': False, 'error': 'OAuth 登录失败（详见运行日志）'}
	elif account.has_login_credentials():
		log.detail(f'{account_name}: 正在尝试邮箱密码登录 (优先)...')
		assert account.email is not None and account.password is not None
		login_result = await login_with_credentials(
			account_name,
			provider_config,
			account.provider,
			account.email,
			account.password,
		)
		if login_result:
			all_cookies = login_result.cookies
			resolved_api_user = login_result.api_user
			auth_method = 'email/password'
		else:
			log.failed(f'{account_name}: 邮箱密码登录失败，不会使用过期的 session cookies')
			return False, None, {'success': False, 'error': '邮箱密码登录失败（详见运行日志）'}
	else:
		user_cookies = parse_cookies(account.cookies)
		if not user_cookies:
			log.failed(f'{account_name}: 配置格式无效')
			return False, None, {'success': False, 'error': '账号配置格式无效（无有效凭据）'}
		all_cookies = await prepare_cookies(account_name, provider_config, user_cookies)
		auth_method = 'session cookies'

	if not all_cookies and not resolved_bearer_token:
		return False, None, {'success': False, 'error': 'cookies 准备失败（WAF cookies 获取失败，详见运行日志）'}

	log.detail(f'{account_name}: 使用认证方式 -> {auth_method}')

	return run_check_in_requests(
		all_cookies,
		account,
		account_name,
		provider_config,
		api_user_override=resolved_api_user,
		bearer_token=resolved_bearer_token,
		use_proxy=provider_config.use_proxy and not force_direct,
	)


def run_check_in_requests(
	all_cookies: dict,
	account: AccountConfig,
	account_name: str,
	provider_config,
	*,
	api_user_override: str | None = None,
	bearer_token: str | None = None,
	use_proxy: bool = False,
) -> tuple[bool, dict | None, dict | None]:
	"""执行 HTTP 签到请求（同步，避免在 async 上下文中使用阻塞 httpx）。

	bearer_token：新版 new-api 站点（gorouter 2026-08 起）OAuth 回调返回的 JWT，
	存在时请求带 Authorization: Bearer（session cookie 已不参与鉴权）。
	"""
	try:
		client = create_client(
			headers={
				'Referer': provider_config.domain,
				'Origin': provider_config.domain,
			},
			use_proxy=use_proxy,
		)
		if use_proxy and not is_proxy_configured():
			log.warn(f'{account_name}: 提供商需要代理但未配置 CHECKIN_PROXY_URL')

		with client:
			client.cookies.update(all_cookies)

			# 复用 API_HEADERS 保证 UA 与 sec-ch-ua* 指纹一致，仅覆盖站点相关项
			headers = dict(API_HEADERS)
			headers.update(
				{
					'Referer': provider_config.domain,
					'Origin': provider_config.domain,
					'Connection': 'keep-alive',
				}
			)

			api_user = api_user_override or account.api_user
			if api_user and provider_config.api_user_key:
				headers[provider_config.api_user_key] = api_user
			if bearer_token:
				headers['Authorization'] = f'Bearer {bearer_token}'

			user_info_url = f'{provider_config.domain}{provider_config.user_info_path}'
			# httpx 取余额；被阿里云 WAF 挑战时用真浏览器兜底（能解 JS 挑战）
			browser_cache: dict = {}

			if not provider_config.needs_manual_check_in():
				# 自动签到型（agentrouter/gorouter）：走到这里意味着登录已成功，
				# 服务端签到已随登录触发完成。余额查询仅为展示（WAF/网络失败时先
				# 浏览器兜底），失败不判签到失败、也不切节点重新登录——反复 OAuth
				# 登录同一账号只会增加 WAF/风控风险，重试工作流也无需再补签。
				user_info_before = _get_user_info_resilient(
					client,
					headers,
					user_info_url,
					account_name,
					account,
					provider_config,
					all_cookies,
					use_proxy=use_proxy,
					browser_cache=browser_cache,
					propagate_network_error=False,
				)
				if user_info_before.get('success'):
					log.info(f'{account_name}: {user_info_before["display"]}')
				else:
					log.warn(f'{account_name}: {user_info_before.get("error", "未知错误")}（签到已随登录完成）')
				user_info_after = _get_user_info_resilient(
					client,
					headers,
					user_info_url,
					account_name,
					account,
					provider_config,
					all_cookies,
					use_proxy=use_proxy,
					browser_cache=browser_cache,
					propagate_network_error=False,
				)
				if user_info_after.get('success'):
					log.detail(f'{account_name}: 签到自动完成（由用户信息请求触发）')
				else:
					log.warn(f'{account_name}: 签到后余额查询失败: {user_info_after.get("error", "未知错误")}')
				return True, user_info_before, user_info_after

			user_info_before = _get_user_info_resilient(
				client,
				headers,
				user_info_url,
				account_name,
				account,
				provider_config,
				all_cookies,
				use_proxy=use_proxy,
				browser_cache=browser_cache,
			)
			if user_info_before and user_info_before.get('success'):
				log.info(f'{account_name}: {user_info_before["display"]}')
			elif user_info_before:
				log.failed(f'{account_name}: {user_info_before.get("error", "未知错误")}')

			success = execute_check_in(client, account_name, provider_config, headers)
			# 签到已发出，查询余额的网络异常不再触发节点重试，避免已成功的签到被重复提交
			user_info_after = get_user_info(client, headers, user_info_url, propagate_network_error=False)
			return success, user_info_before, user_info_after

	except ProxyNodeIssue:
		# 让代理节点问题（含阿里云 WAF 拦截）继续向上，交由 check_in_account_with_retry 切换节点重试
		raise
	except _NETWORK_ERRORS as e:
		# 网络/超时/连接异常 → 可能为代理节点问题，交由外层切换节点重试
		raise ProxyNodeIssue(f'{account_name}: 签到请求网络异常: {str(e)[:50]}') from e
	except Exception as e:
		log.failed(f'{account_name}: 签到过程中发生错误 - {str(e)[:50]}...')
		return False, None, {'success': False, 'error': f'签到过程异常: {str(e)[:50]}'}


async def check_in_account_with_retry(
	account: AccountConfig,
	account_index: int,
	app_config: AppConfig,
	node_selector: NodeSelector | None,
):
	"""带代理节点切换重试的签到。

	仅对 use_proxy=true 的代办商生效；非代理代办商直接走 check_in_account。
	遇 ProxyNodeIssue（WAF 拦截 / 5xx / 网络异常 / 超时）时切换代理节点重试，
	最多 PROXY_RETRY_TIMES 次（默认 3）。若节点选择器已无可用节点，则直连兜底一次。
	"""
	provider_config = app_config.get_provider(account.provider)
	uses_proxy = bool(provider_config and provider_config.use_proxy)
	if not uses_proxy or node_selector is None:
		return await check_in_account(account, account_index, app_config)

	account_name = account.get_display_name(account_index)
	max_retries = int(os.getenv('PROXY_RETRY_TIMES', '3').strip() or 3)
	proxy_url = os.getenv('CHECKIN_PROXY_URL', '').strip()

	# 已知无可用代理节点：直接直连，避免先用默认节点白试一次代理
	if no_available_node():
		log.warn(f'{account_name}: 无可用代理节点，直接尝试直连')
		return await check_in_account(account, account_index, app_config, force_direct=True)

	attempt = 0

	while True:
		try:
			return await check_in_account(account, account_index, app_config)
		except ProxyNodeIssue as e:
			attempt += 1
			if attempt > max_retries:
				log.warn(f'{account_name}: 节点重试已达上限（{max_retries} 次），放弃该账号')
				return False, None, {'success': False, 'error': f'节点重试耗尽（WAF 拦截/网络异常）: {str(e)[:50]}'}

			current = current_proxy_node()
			if current:
				node_selector.exclude_node(current)
			log.warn(f'{account_name}: 检测到节点问题，切换代理节点重试（{attempt}/{max_retries}）: {str(e)[:60]}')

			new_node = node_selector.select_node(proxy_url)
			if new_node is None:
				log.warn(f'{account_name}: 无可用代理节点，尝试直连一次')
				return await check_in_account(account, account_index, app_config, force_direct=True)

			log.info(f'{account_name}: 已切换节点 {new_node}，重试签到')


async def main():
	"""主函数"""
	if is_debug_enabled():
		log.info('调试模式已开启')
		proxy_server = os.getenv('CHECKIN_PROXY_URL', '').strip()
		if proxy_server:
			log.info(f'代理端点可用: {redact_proxy_url(proxy_server)}（根据提供商 use_proxy 启用）')
		else:
			log.info('未设置 CHECKIN_PROXY_URL；use_proxy=true 的提供商将在无代理下运行')
	else:
		log.detail('调试模式未开启（设置 DEBUG_MODE=true 启用截图和详细日志）')

	log.info('AnyRouter.top 多账号自动签到脚本启动')
	log.info(f'执行时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')

	app_config = AppConfig.load_from_env()
	log.detail(f'加载了 {len(app_config.providers)} 个提供商配置')
	if is_debug_enabled():
		for provider_name, provider in sorted(app_config.providers.items()):
			log.info(f'提供商 "{provider_name}": use_proxy={provider.use_proxy}')

	accounts = load_accounts_config()
	if not accounts:
		log.failed('无法加载账号配置，程序退出')
		notify.push_message('签到程序错误', '无法加载账号配置，程序退出', msg_type='text')
		sys.exit(1)

	# 重试模式：仅处理 CHECKIN_RETRY_INDEXES 指定的原始索引账号，且单次尝试
	# （不做节点多次重试——补签是独立工作流在数小时后运行，靠时间差规避临时波动即可）。
	retry_indexes = load_retry_indexes()
	is_retry = retry_indexes is not None
	original_accounts: list[tuple[int, AccountConfig]] = list(enumerate(accounts))
	if is_retry:
		wanted = set(retry_indexes or [])
		original_accounts = [(i, a) for (i, a) in original_accounts if i in wanted]
		if not original_accounts:
			log.info('重试列表为空或无匹配账号，跳过重试')
			sys.exit(0)
		accounts = [a for _, a in original_accounts]

	log.info(f'发现 {len(accounts)} 个账号配置' + ('（重试模式）' if is_retry else ''))

	# 初始化代理节点选择器；仅当存在 use_proxy=true 的账号需要代理时才选初始节点
	node_selector = NodeSelector() if available() else None
	if needs_proxy(app_config, accounts):
		if node_selector is None:
			log.warn('存在使用代理的账号，但未检测到 mihomo controller；相关账号将尝试直连')
		else:
			proxy_url = os.getenv('CHECKIN_PROXY_URL', '').strip()
			if proxy_url:
				initial_node = node_selector.select_node(proxy_url)
				if initial_node is None:
					log.warn('无可用的代理节点，使用代理的账号将尝试直连')
			else:
				log.warn('存在使用代理的账号，但未设置 CHECKIN_PROXY_URL')
	else:
		log.detail('所有账号均不使用代理，跳过代理节点选择')

	last_balance_hash = load_balance_hash()
	balance_snapshot = load_balance_snapshot()

	success_count = 0
	total_count = len(accounts)
	notification_content = []
	current_balances = {}
	account_check_in_details = {}
	notified_account_keys: set[str] = set()
	failed_indexes: list[int] = []
	need_notify = False
	balance_changed = False

	for i, account in original_accounts:
		account_key = f'account_{i + 1}'
		account_name = account.get_display_name(i)
		log.info(f'=============== [{i + 1}/{total_count}] {account_name} 开始 ===============')
		try:
			if is_retry:
				# 补签：单次尝试，不做节点切换多次重试
				success, user_info_before, user_info_after = await check_in_account(account, i, app_config)
			else:
				success, user_info_before, user_info_after = await check_in_account_with_retry(
					account, i, app_config, node_selector
				)
			if success:
				success_count += 1

			should_notify_this_account = False

			if not success:
				should_notify_this_account = True
				need_notify = True
				failed_indexes.append(i)
				account_name = account.get_display_name(i)
				log.notify(f'{account_name} 失败，将发送通知')

			elif user_info_after is None or not user_info_after.get('success'):
				# 签到成功但余额查询失败（如自动签到型遇 WAF）：账号不会出现在
				# 明细与失败条目中，这里补一条成功条目，避免通知里凭空消失
				reason = user_info_after.get('error', '未知原因') if user_info_after else '未获取到用户信息'
				notification_content.append(
					(i, f'**{i + 1}. {account_name}** ✅ 签到成功\n余额未获取: {str(reason)[:60]}')
				)
				notified_account_keys.add(account_key)
				need_notify = True  # 余额状态变化（未知）也值得通知一次

			if user_info_after and user_info_after.get('success'):
				current_quota = user_info_after['quota']
				current_used = user_info_after['used_quota']
				current_balances[account_key] = {'quota': current_quota, 'used': current_used}

				if user_info_before and user_info_before.get('success'):
					before_quota = user_info_before['quota']
					before_used = user_info_before['used_quota']
					after_quota = user_info_after['quota']
					after_used = user_info_after['used_quota']

					total_before = before_quota + before_used
					total_after = after_quota + after_used
				else:
					# 签到前查询失败但签到后成功：无 before 可对比，按 0 变化记账，
					# 避免沿用上一账号旧值跨账号串值 / 首个账号 NameError
					before_quota = after_quota = user_info_after['quota']
					before_used = after_used = user_info_after['used_quota']
					total_before = total_after = before_quota + before_used

				check_in_reward = total_after - total_before
				usage_increase = after_used - before_used
				balance_change = after_quota - before_quota

				# 自动签到型（agentrouter/gorouter，登录即签到）：单次 before/after 对比无效
				# （能拿到的 before 已是签到后），改为用"昨日总额快照"跨日估算今日奖励。
				cross_day_estimated = False
				provider_cfg = app_config.get_provider(account.provider)
				if provider_cfg and not provider_cfg.needs_manual_check_in():
					prev_total = balance_snapshot.get(account.get_display_name(i))
					if prev_total is not None:
						cross_day_reward = total_after - prev_total
						if cross_day_reward > 0:
							check_in_reward = cross_day_reward
							cross_day_estimated = True

				account_check_in_details[account_key] = {
					'name': account.get_display_name(i),
					'index': i + 1,
					'unit': user_info_after.get('unit', 'usd'),
					'before_quota': before_quota,
					'before_used': before_used,
					'after_quota': after_quota,
					'after_used': after_used,
					'check_in_reward': check_in_reward,
					'usage_increase': usage_increase,
					'balance_change': balance_change,
					'cross_day_estimated': cross_day_estimated,
					'success': success,
				}

				# 实时输出该账号签到结果明细（统一各路径日志，避免只体现在最终通知里）
				unit = user_info_after.get('unit', 'usd')
				if success and check_in_reward != 0:
					suffix = '（跨日估算）' if cross_day_estimated else ''
					if cross_day_estimated:
						log.success(
							f'{account_name}: 签到获得 {_format_amount(check_in_reward, unit)}{suffix}，'
							f'当前余额 {_format_amount(after_quota, unit)}'
						)
					else:
						log.success(
							f'{account_name}: 签到获得 {_format_amount(check_in_reward, unit)}'
							f'（余额 {_format_amount(before_quota, unit)} → {_format_amount(after_quota, unit)}）'
						)
				elif success and usage_increase != 0:
					log.success(f'{account_name}: 今日已签到，期间消耗 {_format_amount(usage_increase, unit)}')
				elif success:
					log.success(f'{account_name}: 今日已签到，余额无变化')
				else:
					log.warn(f'{account_name}: 签到失败，当前余额 {_format_amount(after_quota, unit)}')

			if should_notify_this_account:
				account_name = account.get_display_name(i)
				# 失败条目：序号 + 原因（+最后已知余额，如有）
				entry_lines = [f'**{i + 1}. {account_name}** ❌']
				if user_info_after:
					if user_info_after.get('success'):
						entry_lines.append(f'当前余额: {user_info_after["display"]}')
					else:
						entry_lines.append(f'失败原因: {user_info_after.get("error", "未知错误")}')
				else:
					entry_lines.append('失败原因: 未知（详见运行日志）')
				notification_content.append((i, '\n'.join(entry_lines)))
				notified_account_keys.add(account_key)

		except Exception as e:
			account_name = account.get_display_name(i)
			log.failed(f'{account_name}: 处理异常: {e}')
			need_notify = True
			if i not in failed_indexes:
				failed_indexes.append(i)
			notification_content.append((i, f'**{i + 1}. {account_name}** ❌\n失败原因: 处理异常: {str(e)[:50]}'))
			notified_account_keys.add(account_key)

	current_balance_hash = generate_balance_hash(current_balances) if current_balances else None
	if current_balance_hash:
		if last_balance_hash is None:
			balance_changed = True
			need_notify = True
			log.notify('首次运行检测到，将发送当前余额通知')
		elif current_balance_hash != last_balance_hash:
			balance_changed = True
			need_notify = True
			log.notify('检测到余额变化，将发送通知')
		else:
			log.detail('未检测到余额变化')

	if balance_changed:
		for i, account in original_accounts:
			account_key = f'account_{i + 1}'
			# 按 account_key 精确判重，避免"账号1"被"账号11"的条目误判为已通知
			if account_key in account_check_in_details and account_key not in notified_account_keys:
				notification_content.append((i, format_check_in_notification(account_check_in_details[account_key])))
				notified_account_keys.add(account_key)

	if current_balance_hash:
		save_balance_hash(current_balance_hash)

	# 更新跨日总额快照：仅成功取到余额的账号写入最新总额，供次日还原自动签到奖励
	for i, account in original_accounts:
		account_key = f'account_{i + 1}'
		if account_key in current_balances:
			bal = current_balances[account_key]
			balance_snapshot[account.get_display_name(i)] = bal['quota'] + bal['used']
	save_balance_snapshot(balance_snapshot)

	# 签到总结：无论是否发通知都输出，用专门的分隔符标出，便于一眼看清整体结果
	log.info('==================== [签到总结] ====================')
	log.stats(f'成功: {success_count}/{total_count}, 失败: {total_count - success_count}/{total_count}')
	if success_count == total_count:
		log.success('全部账号签到成功!')
	elif success_count > 0:
		log.warn('部分账号签到成功')
	else:
		log.failed('全部账号签到失败')

	if need_notify and notification_content:
		summary = [
			'**📊 统计**',
			f'成功: {success_count}/{total_count}，失败: {total_count - success_count}/{total_count}',
		]

		if success_count == total_count:
			summary.append('全部账号签到成功 ✅')
		elif success_count > 0:
			summary.append('部分账号签到成功 ⚠️')
		else:
			summary.append('全部账号签到失败 ❌')

		time_info = f'执行时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'

		# 条目按账号序号排序后拼接，账号与数据一一对应
		entries = [entry for _, entry in sorted(notification_content, key=lambda item: item[0])]
		notify_content = '\n\n'.join([time_info, *entries, '\n'.join(summary)])
		screenshot_paths = take_pending_screenshots() if is_debug_enabled() else []
		if screenshot_paths:
			github_run_id = os.getenv('GITHUB_RUN_ID', '').strip()
			github_repo = os.getenv('GITHUB_REPOSITORY', '').strip()
			screenshot_hint = f'[截图] 已保存 {len(screenshot_paths)} 张调试截图'
			if github_run_id and github_repo:
				run_url = f'https://github.com/{github_repo}/actions/runs/{github_run_id}'
				screenshot_hint += f'。可从 Actions 下载 artifact `checkin-screenshots-{github_run_id}`：{run_url}'
			else:
				screenshot_hint += '，保存到 `checkin_screenshots/` 目录'
			notify_content += f'\n\n{screenshot_hint}'

		print(notify_content)
		if is_retry:
			notify_title = f'签到重试完成，成功 {success_count}，失败 {total_count - success_count}'
		else:
			notify_title = f'每日签到完成，成功 {success_count}，失败 {total_count - success_count}'
		# Markdown 正文（NotifyX/飞书等直接渲染；纯文本渠道由 notify 层降级）
		if notify.push_message(
			notify_title, notify_content, msg_type='markdown', description=f'成功 {success_count}/{total_count}'
		):
			log.notify('通知已发送')
	elif need_notify:
		log.warn('有失败或余额变化，但缺少通知内容，未发送通知')
	else:
		log.info('未检测到余额变化，跳过通知')
	log.info('====================================================')

	# 落盘本次结果，供每日重试工作流判断哪些账号需要补签
	save_checkin_result(failed_indexes, total_count)

	sys.exit(0 if success_count > 0 else 1)


def run_main():
	"""运行主函数的包装函数"""
	try:
		asyncio.run(main())
	except KeyboardInterrupt:
		log.warn('程序被用户中断')
		sys.exit(1)
	except Exception as e:
		log.failed(f'程序执行过程中发生错误: {e}')
		sys.exit(1)


if __name__ == '__main__':
	run_main()
