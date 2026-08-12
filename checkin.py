#!/usr/bin/env python3
"""
AnyRouter.top 自动签到脚本
"""

import asyncio
import hashlib
import json
import os
import re
import sys
from datetime import datetime

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
from utils.config import AccountConfig, AppConfig, load_accounts_config
from utils.debug import is_debug_enabled, log
from utils.gptgod import gptgod_checkin
from utils.http_client import API_HEADERS, QUOTA_PER_DOLLAR, create_client, request_with_retry
from utils.notify import notify
from utils.proxy import get_playwright_proxy, is_proxy_configured

BALANCE_HASH_FILE = 'balance_hash.txt'


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


def generate_balance_hash(balances):
	"""生成余额数据的hash"""
	simple_balances = (
		{k: {'quota': v.get('quota'), 'used': v.get('used')} for k, v in balances.items()} if balances else {}
	)
	balance_json = json.dumps(simple_balances, sort_keys=True, separators=(',', ':'))
	return hashlib.sha256(balance_json.encode('utf-8')).hexdigest()[:16]


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


async def get_waf_cookies_with_browser(
	account_name: str,
	login_url: str,
	required_cookies: list[str],
	*,
	use_proxy: bool = False,
):
	"""使用浏览器获取 WAF cookies"""
	log.info(f'{account_name}: 启动浏览器获取 WAF cookies...')

	launch_kwargs: dict = {'headless': True}
	proxy = get_playwright_proxy(use_proxy=use_proxy)
	if proxy:
		launch_kwargs['proxy'] = proxy
	browser = await launch_async(**launch_kwargs)

	try:
		page = await browser.new_page()
		await prepare_browser_page(page)
		log.info(f'{account_name}: 访问登录页面获取初始 cookies...')

		await page.goto(login_url, wait_until='domcontentloaded')
		await wait_for_waf_ready(page)

		cookies = await page.context.cookies()

		waf_cookies = {}
		for cookie in cookies:
			cookie_name = cookie.get('name')
			cookie_value = cookie.get('value')
			if cookie_name in required_cookies and cookie_value is not None:
				waf_cookies[cookie_name] = cookie_value

		log.info(f'{account_name}: 获取到 {len(waf_cookies)} 个 WAF cookies')

		missing_cookies = [c for c in required_cookies if c not in waf_cookies]

		if missing_cookies:
			log.failed(f'{account_name}: 缺少 WAF cookies: {missing_cookies}')
			await browser.close()
			return None

		log.success(f'{account_name}: 成功获取所有 WAF cookies')
		await browser.close()
		return waf_cookies

	except Exception as e:
		log.failed(f'{account_name}: 获取 WAF cookies 时出错: {e}')
		await browser.close()
		return None


async def login_with_credentials(
	account_name: str,
	provider_config,
	provider_name: str,
	email: str,
	password: str,
) -> BrowserLoginResult | None:
	"""使用邮箱密码通过浏览器登录，返回 cookies 与拦截到的 api user id。"""
	log.info(f'{account_name}: 正在使用邮箱密码登录...')

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

	log.info(f'{account_name}: 提供商代理={"已启用" if provider_config.use_proxy else "已禁用"} ({provider_name})')

	try:
		context = await launch_login_context(settings, use_proxy=provider_config.use_proxy)
	except Exception as e:
		log.failed(f'{account_name}: 浏览器启动失败: {e}')
		return None

	page = None
	try:
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
			log.info(f'{account_name}: 浏览器配置文件已登录')

		console_url = f'{provider_config.domain}/console'
		user_profile = await verify_browser_login(page, console_url, timeout_ms)
		if not user_profile:
			cookies = await context.cookies()
			cookie_names = [c.get('name') for c in cookies if c.get('name')]
			log.failed(f'{account_name}: 登录失败 - /api/user/self 未验证')
			log.debug(f'{account_name}: 当前 URL: {page.url}')
			log.debug(f'{account_name}: 获取到 cookies: {cookie_names}')
			await save_login_screenshot(page, provider_name, account_name, 'not-authenticated')
			await context.close()
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
		await context.close()
		return BrowserLoginResult(cookies=all_cookies, api_user=api_user)

	except Exception as e:
		log.failed(f'{account_name}: 登录过程中出错: {e}')
		if page is not None:
			await save_login_screenshot(page, provider_name, account_name, 'login-error')
		await context.close()
		return None


async def login_with_github_oauth(
	account_name: str,
	provider_config,
	github_session: str,
) -> BrowserLoginResult | None:
	"""使用 GitHub OAuth 重放登录，返回 cookies 与 api_user。"""
	log.info(f'{account_name}: 正在使用 GitHub OAuth 登录...')

	domain = provider_config.domain
	client = create_client(
		headers={
			'Referer': domain,
			'Origin': domain,
		},
		use_proxy=provider_config.use_proxy,
	)
	if provider_config.use_proxy and not is_proxy_configured():
		log.warn(f'{account_name}: 提供商需要代理但未配置 CHECKIN_PROXY_URL')

	github_client_id = provider_config.oauth_client_id

	with client:
		# Step 1: 获取 OAuth state（同时获取 acw_tc WAF cookie）
		log.debug(f'{account_name}: 第1步 - 获取 OAuth 状态...')
		state_url = f'{domain}{provider_config.oauth_state_path}?mode=login'
		try:
			resp = request_with_retry(client, 'GET', state_url, timeout=30)
		except Exception as e:
			log.failed(f'{account_name}: OAuth 状态请求失败: {e}')
			return None

		if resp.status_code != 200:
			log.failed(f'{account_name}: OAuth 状态返回 HTTP {resp.status_code}')
			return None

		# WAF 拦截检测（与原始项目一致）
		if 'aliyun_waf_aa' in resp.text:
			log.failed(f'{account_name}: 被阿里云 WAF 拦截，请尝试使用代理')
			return None

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

		log.info(f'{account_name}: 获取到 OAuth 状态（cookies: {list(client.cookies.keys())}）')

		# Step 2: 用 GitHub user_session 获取授权 code
		log.debug(f'{account_name}: 第2步 - 获取 GitHub OAuth code...')
		auth_url = f'https://github.com/login/oauth/authorize?client_id={github_client_id}&state={state}'
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
		log.info(f'{account_name}: 获取到 GitHub OAuth code')

		# Step 3: OAuth 回调，触发登录+签到
		log.debug(f'{account_name}: 第3步 - OAuth 回调...')
		callback_url = f'{domain}{provider_config.oauth_callback_path}?code={code}&state={state}&mode=login'
		try:
			cb_resp = request_with_retry(client, 'GET', callback_url, timeout=30)
		except Exception as e:
			log.failed(f'{account_name}: OAuth 回调失败: {e}')
			return None

		if cb_resp.status_code != 200:
			log.failed(f'{account_name}: OAuth 回调返回 HTTP {cb_resp.status_code}')
			return None

		# WAF 拦截检测（与原始项目一致）
		if 'aliyun_waf_aa' in cb_resp.text:
			log.failed(f'{account_name}: OAuth 回调被阿里云 WAF 拦截')
			return None

		try:
			cb_data = cb_resp.json()
		except Exception:
			log.failed(f'{account_name}: OAuth 回调响应非 JSON 格式')
			return None

		if not cb_data.get('success'):
			log.failed(f'{account_name}: OAuth 回调失败: {cb_data}')
			return None

		user_data = cb_data.get('data', {})
		api_user = str(user_data.get('id')) if user_data.get('id') is not None else None
		checked_in = user_data.get('checked_in', False)
		log.info(f'{account_name}: 登录成功，已签到状态={checked_in}')

		all_cookies = dict(client.cookies)
		log.success(f'{account_name}: OAuth 登录成功，获取到 {len(all_cookies)} 个 cookies')
		return BrowserLoginResult(cookies=all_cookies, api_user=api_user)


def get_user_info(client, headers, user_info_url: str):
	"""获取用户信息（带重试）"""
	try:
		response = request_with_retry(client, 'GET', user_info_url, headers=headers, timeout=30)

		if response.status_code == 200:
			data = response.json()
			if data.get('success'):
				user_data = data.get('data', {})
				quota = round(user_data.get('quota', 0) / QUOTA_PER_DOLLAR, 2)
				used_quota = round(user_data.get('used_quota', 0) / QUOTA_PER_DOLLAR, 2)
				return {
					'success': True,
					'quota': quota,
					'used_quota': used_quota,
					'display': f':money: Current balance: ${quota}, Used: ${used_quota}',
				}
		return {'success': False, 'error': f'Failed to get user info: HTTP {response.status_code}'}
	except Exception as e:
		return {'success': False, 'error': f'Failed to get user info: {str(e)[:50]}...'}


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
		log.info(f'{account_name}: 无需绕过 WAF，直接使用用户 cookies')

	return {**waf_cookies, **user_cookies}


def execute_check_in(client, account_name: str, provider_config, headers: dict):
	"""执行签到请求（带重试）"""
	log.debug(f'{account_name}: 执行签到请求')

	checkin_headers = headers.copy()
	checkin_headers.update({'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'})

	sign_in_url = f'{provider_config.domain}{provider_config.sign_in_path}'
	response = request_with_retry(client, 'POST', sign_in_url, headers=checkin_headers, timeout=30)

	log.debug(f'{account_name}: 响应状态码 {response.status_code}')

	if response.status_code == 200:
		try:
			result = response.json()
			if result.get('ret') == 1 or result.get('code') == 0 or result.get('success'):
				log.success(f'{account_name}: 签到成功!')
				return True
			else:
				error_msg = result.get('msg', result.get('message', 'Unknown error'))
				already_checked_keywords = ['已经签到', '已签到', '重复签到', 'already checked', 'already signed']
				if any(keyword in error_msg.lower() for keyword in already_checked_keywords):
					log.success(f'{account_name}: 今日已签到')
					return True
				log.failed(f'{account_name}: 签到失败 - {error_msg}')
				return False
		except json.JSONDecodeError:
			if 'success' in response.text.lower():
				log.success(f'{account_name}: 签到成功!')
				return True
			else:
				log.failed(f'{account_name}: 签到失败 - 响应格式无效')
				return False
	else:
		log.failed(f'{account_name}: 签到失败 - HTTP {response.status_code}')
		return False


def _format_amount(value: float, unit: str) -> str:
	"""按单位渲染金额：美元带 $ 前缀，积分保留整数并加后缀。"""
	if unit == 'credits':
		return f'{value:g} 积分'
	return f'${value:.2f}'


def format_check_in_notification(detail: dict) -> str:
	"""格式化签到通知消息"""
	unit = detail.get('unit', 'usd')

	def amount(value: float) -> str:
		return _format_amount(value, unit)

	lines = [
		f'[CHECK-IN] {detail["name"]}',
		'  ━━━━━━━━━━━━━━━━━━━━',
		'  签到前',
		f'     余额: {amount(detail["before_quota"])}  |  累计消耗: {amount(detail["before_used"])}',
		'  签到后',
		f'     余额: {amount(detail["after_quota"])}  |  累计消耗: {amount(detail["after_used"])}',
	]

	has_reward = detail['check_in_reward'] != 0
	has_usage = detail['usage_increase'] != 0

	if has_reward or has_usage:
		lines.append('  ━━━━━━━━━━━━━━━━━━━━')

		if not has_reward and has_usage:
			lines.append('  今日已签到（期间有使用）')

		if has_reward:
			lines.append(f'  签到获得: +{amount(detail["check_in_reward"])}')

		if has_usage:
			lines.append(f'  期间消耗: {amount(detail["usage_increase"])}')

		if detail['balance_change'] != 0:
			change_symbol = '+' if detail['balance_change'] > 0 else ''
			lines.append(f'  余额变化: {change_symbol}{amount(detail["balance_change"])}')
	else:
		lines.extend(['  ━━━━━━━━━━━━━━━━━━━━', '  今日已签到，无变化'])

	return '\n'.join(lines)


async def check_in_account(account: AccountConfig, account_index: int, app_config: AppConfig):
	"""为单个账号执行签到操作"""
	account_name = account.get_display_name(account_index)
	log.info(f'\n{account_name}: 开始处理')

	provider_config = app_config.get_provider(account.provider)
	if not provider_config:
		log.failed(f'{account_name}: 提供商 "{account.provider}" 未在配置中找到')
		return False, None, None

	log.info(f'{account_name}: 使用提供商 "{account.provider}" ({provider_config.domain})')

	# GPTGod 纯 API 签到（无需浏览器，自带登录+签到全流程）
	if provider_config.auth_method == 'gptgod':
		if not account.has_login_credentials():
			log.failed(f'{account_name}: GPTGod 提供商需要邮箱和密码')
			return False, None, None
		assert account.email is not None and account.password is not None
		log.info(f'{account_name}: 正在尝试 GPTGod API 签到...')
		success, info_before, info_after = gptgod_checkin(
			account_name,
			account.email,
			account.password,
			use_proxy=provider_config.use_proxy,
		)
		return success, info_before, info_after

	# 优先使用 OAuth 登录（如 GitHub OAuth）
	all_cookies = None
	resolved_api_user: str | None = None
	auth_method = None
	if provider_config.is_oauth() and account.has_oauth_credentials():
		log.info(f'{account_name}: 正在尝试 OAuth 登录 (github_session)...')
		assert account.github_session is not None
		login_result = await login_with_github_oauth(
			account_name,
			provider_config,
			account.github_session,
		)
		if login_result:
			all_cookies = login_result.cookies
			resolved_api_user = login_result.api_user
			auth_method = 'GitHub OAuth'
		else:
			log.failed(f'{account_name}: OAuth 登录失败')
			return False, None, None
	elif account.has_login_credentials():
		log.info(f'{account_name}: 正在尝试邮箱密码登录 (优先)...')
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
			return False, None, None
	else:
		user_cookies = parse_cookies(account.cookies)
		if not user_cookies:
			log.failed(f'{account_name}: 配置格式无效')
			return False, None, None
		all_cookies = await prepare_cookies(account_name, provider_config, user_cookies)
		auth_method = 'session cookies'

	if not all_cookies:
		return False, None, None

	log.info(f'{account_name}: 使用认证方式 -> {auth_method}')

	return run_check_in_requests(
		all_cookies,
		account,
		account_name,
		provider_config,
		api_user_override=resolved_api_user,
		use_proxy=provider_config.use_proxy,
	)


def run_check_in_requests(
	all_cookies: dict,
	account: AccountConfig,
	account_name: str,
	provider_config,
	*,
	api_user_override: str | None = None,
	use_proxy: bool = False,
) -> tuple[bool, dict | None, dict | None]:
	"""执行 HTTP 签到请求（同步，避免在 async 上下文中使用阻塞 httpx）。"""
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

			user_info_url = f'{provider_config.domain}{provider_config.user_info_path}'
			user_info_before = get_user_info(client, headers, user_info_url)
			if user_info_before and user_info_before.get('success'):
				log.info(f'{account_name}: {user_info_before["display"]}')
			elif user_info_before:
				log.failed(f'{account_name}: {user_info_before.get("error", "Unknown error")}')

			if provider_config.needs_manual_check_in():
				success = execute_check_in(client, account_name, provider_config, headers)
				user_info_after = get_user_info(client, headers, user_info_url)
				return success, user_info_before, user_info_after

			user_info_after = get_user_info(client, headers, user_info_url)
			if user_info_after and user_info_after.get('success'):
				log.info(f'{account_name}: 签到自动完成（由用户信息请求触发）')
				return True, user_info_before, user_info_after
			error = user_info_after.get('error', 'Unknown error') if user_info_after else 'Unknown error'
			log.failed(f'{account_name}: 自动签到失败 - {error}')
			return False, user_info_before, user_info_after

	except Exception as e:
		log.failed(f'{account_name}: 签到过程中发生错误 - {str(e)[:50]}...')
		return False, None, None


async def main():
	"""主函数"""
	if is_debug_enabled():
		log.info('调试模式已开启')
		proxy_server = os.getenv('CHECKIN_PROXY_URL', '').strip()
		if proxy_server:
			log.info(f'代理端点可用: {proxy_server}（根据提供商 use_proxy 启用）')
		else:
			log.info('未设置 CHECKIN_PROXY_URL；use_proxy=true 的提供商将在无代理下运行')
	else:
		log.info('调试模式未开启（设置 DEBUG_MODE=true 启用截图和详细日志）')

	log.info('AnyRouter.top 多账号自动签到脚本启动')
	log.info(f'执行时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')

	app_config = AppConfig.load_from_env()
	log.info(f'加载了 {len(app_config.providers)} 个提供商配置')
	if is_debug_enabled():
		for provider_name, provider in sorted(app_config.providers.items()):
			log.info(f'提供商 "{provider_name}": use_proxy={provider.use_proxy}')

	accounts = load_accounts_config()
	if not accounts:
		log.failed('无法加载账号配置，程序退出')
		notify.push_message('AnyRouter Check-in Alert', '无法加载账号配置，程序退出', msg_type='text')
		sys.exit(1)

	log.info(f'发现 {len(accounts)} 个账号配置')

	last_balance_hash = load_balance_hash()

	success_count = 0
	total_count = len(accounts)
	notification_content = []
	current_balances = {}
	account_check_in_details = {}
	notified_account_keys: set[str] = set()
	need_notify = False
	balance_changed = False

	for i, account in enumerate(accounts):
		account_key = f'account_{i + 1}'
		try:
			success, user_info_before, user_info_after = await check_in_account(account, i, app_config)
			if success:
				success_count += 1

			should_notify_this_account = False

			if not success:
				should_notify_this_account = True
				need_notify = True
				account_name = account.get_display_name(i)
				log.notify(f'{account_name} 失败，将发送通知')

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

					check_in_reward = total_after - total_before
					usage_increase = after_used - before_used
					balance_change = after_quota - before_quota

					account_check_in_details[account_key] = {
						'name': account.get_display_name(i),
						'unit': user_info_after.get('unit', 'usd'),
						'before_quota': before_quota,
						'before_used': before_used,
						'after_quota': after_quota,
						'after_used': after_used,
						'check_in_reward': check_in_reward,
						'usage_increase': usage_increase,
						'balance_change': balance_change,
						'success': success,
					}

			if should_notify_this_account:
				account_name = account.get_display_name(i)
				status = '[SUCCESS]' if success else '[FAIL]'
				account_result = f'{status} {account_name}'
				if user_info_after and user_info_after.get('success'):
					account_result += f'\n{user_info_after["display"]}'
				elif user_info_after:
					account_result += f'\n{user_info_after.get("error", "Unknown error")}'
				notification_content.append(account_result)
				notified_account_keys.add(account_key)

		except Exception as e:
			account_name = account.get_display_name(i)
			log.failed(f'{account_name} 处理异常: {e}')
			need_notify = True
			notification_content.append(f'[FAIL] {account_name} exception: {str(e)[:50]}...')
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
			log.info('未检测到余额变化')

	if balance_changed:
		for i, account in enumerate(accounts):
			account_key = f'account_{i + 1}'
			# 按 account_key 精确判重，避免"账号1"被"账号11"的条目误判为已通知
			if account_key in account_check_in_details and account_key not in notified_account_keys:
				notification_content.append(format_check_in_notification(account_check_in_details[account_key]))
				notified_account_keys.add(account_key)

	if current_balance_hash:
		save_balance_hash(current_balance_hash)

	if need_notify and notification_content:
		summary = [
			'[STATS] Check-in result statistics:',
			f'[SUCCESS] Success: {success_count}/{total_count}',
			f'[FAIL] Failed: {total_count - success_count}/{total_count}',
		]

		if success_count == total_count:
			summary.append('[SUCCESS] All accounts check-in successful!')
		elif success_count > 0:
			summary.append('[WARN] Some accounts check-in successful')
		else:
			summary.append('[ERROR] All accounts check-in failed')

		time_info = f'[TIME] Execution time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'

		notify_content = '\n\n'.join([time_info, '\n'.join(notification_content), '\n'.join(summary)])
		screenshot_paths = take_pending_screenshots() if is_debug_enabled() else []
		if screenshot_paths:
			github_run_id = os.getenv('GITHUB_RUN_ID', '').strip()
			github_repo = os.getenv('GITHUB_REPOSITORY', '').strip()
			screenshot_hint = f'[SCREENSHOT] {len(screenshot_paths)} debug screenshot(s) saved'
			if github_run_id and github_repo:
				run_url = f'https://github.com/{github_repo}/actions/runs/{github_run_id}'
				screenshot_hint += f'. Download artifact `checkin-screenshots-{github_run_id}` from: {run_url}'
			else:
				screenshot_hint += ' to `checkin_screenshots/`'
			notify_content += f'\n\n{screenshot_hint}'

		print(notify_content)
		notify.push_message('AnyRouter Check-in Alert', notify_content, msg_type='text')
		log.notify('由于失败或余额变化已发送通知')
	else:
		log.info('所有账号成功且无余额变化，跳过通知')

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
