"""
Guyscode 独立签到分支 — 浏览器登录捕获 JWT + httpx 主动签到

与标准 new-api（cookie + new-api-user 头）不同，guyscode 采用纯 JWT Bearer 认证，
登录接口带 Cloudflare Turnstile 验证码。因此不走通用的浏览器表单登录 + cookie 流程，
而是：

  1. 优先用持久化的 refresh_token 续期换 JWT（纯 API，无需 Turnstile）
  2. 无有效凭证时用真浏览器（CloakBrowser）过 Turnstile 登录，捕获 JWT 并存档
  3. GET  /api/v1/auth/me      取当前余额（data.balance，美元）
  4. POST /api/v1/check-in     空 body + Bearer token，主动签到
  5. GET  /api/v1/auth/me      取签到后余额

本分支仅保留差异逻辑（Turnstile 浏览器登录、refresh_token 持久化、美元余额），
标准流程由 utils.checkin_core.run_standard_checkin 编排。
响应统一为 {code, message, data}，code=0 表示成功。
"""

import asyncio
import concurrent.futures
import json
import os
import time
from typing import TYPE_CHECKING

import httpx

from utils.browser import (
	launch_login_context,
	load_browser_login_settings,
	prepare_browser_page,
)
from utils.checkin_core import build_user_info, failed_info, run_standard_checkin
from utils.debug import is_debug_enabled, log
from utils.http_client import create_client, request_with_retry

if TYPE_CHECKING:
	from playwright.async_api import Page

BASE = 'https://www.guyscode.com'
LOGIN_URL = f'{BASE}/login'
LOGIN_API = f'{BASE}/api/v1/auth/login'
REFRESH_API = f'{BASE}/api/v1/auth/refresh'
ME_API = f'{BASE}/api/v1/auth/me'
CHECKIN_API = f'{BASE}/api/v1/check-in'
TIMEZONE = 'Asia/Shanghai'

# 登录接口成功响应的特征（用于拦截响应捕获 token）
# 首次浏览器登录需等待 Turnstile 人工/自动核验，给足时间（180s）
_TOKEN_CAPTURE_TIMEOUT_MS = 180_000
# refresh_token 持久化文件（json: {账号名: refresh_token}）
GUYSCODE_REFRESH_FILE = 'guyscode_refresh.json'

# 通用表单选择器（guyscode 登录表单的兜底）
_EMAIL_SELECTORS = ('#email', 'input[name="email"]', 'input[type="email"]')
_PASSWORD_SELECTORS = ('#password', 'input[name="password"]', 'input[type="password"]')  # nosec B105
_SUBMIT_SELECTORS = ('button[type="submit"]', 'button[data-testid="login-submit"]', 'button[type="button"]')
# 条款弹窗：未同意前账号/密码输入与登录按钮会被禁用，需先点击「同意并继续」
_TERMS_AGREE_BUTTON_TEXT = '同意并继续'


def _parse_payload(resp) -> tuple[int | None, dict]:
	"""从 guyscode 统一响应 {code, message, data} 提取 code 与 data。"""
	try:
		payload = resp.json()
	except Exception:  # nosec B112
		return None, {}
	if not isinstance(payload, dict):
		return None, {}
	return payload.get('code'), payload.get('data') or {}


def _info_from_balance(balance: float) -> dict:
	"""构造与主流程兼容的用户信息字典（单位美元，guyscode 无已用概念）。"""
	return build_user_info(round(float(balance), 2), 0.0, 'usd')


# ---------------------------------------------------------------------------
# refresh_token 持久化
# ---------------------------------------------------------------------------


def _refresh_file_path() -> str:
	return os.getenv('GUYSCODE_REFRESH_FILE', GUYSCODE_REFRESH_FILE)


def load_refresh_tokens() -> dict[str, str]:
	"""加载 {账号名: refresh_token}，失败返回空 dict。"""
	try:
		if os.path.exists(_refresh_file_path()):
			with open(_refresh_file_path(), 'r', encoding='utf-8') as f:
				data = json.load(f)
			if isinstance(data, dict):
				return {k: v for k, v in data.items() if isinstance(v, str) and v}
	except Exception:  # nosec B110
		pass
	return {}


def save_refresh_token(account_name: str, refresh_token: str) -> None:
	"""保存单个账号的 refresh_token，保留其他账号。"""
	tokens = load_refresh_tokens()
	tokens[account_name] = refresh_token
	try:
		with open(_refresh_file_path(), 'w', encoding='utf-8') as f:
			json.dump(tokens, f, ensure_ascii=False, indent=2)
	except Exception as e:
		log.warn(f'{account_name}: 保存 guyscode refresh_token 失败: {e}')


# ---------------------------------------------------------------------------
# 浏览器登录（捕获 JWT）— async，在独立线程事件循环中运行
# ---------------------------------------------------------------------------


async def _wait_for_email_input(page: 'Page', timeout_ms: int) -> 'object':
	"""等待邮箱输入框出现并可见，返回该 locator。"""
	email_input = page.locator(_EMAIL_SELECTORS[0]).first
	try:
		await email_input.wait_for(state='visible', timeout=timeout_ms)
		return email_input
	except Exception:  # nosec B112
		for selector in _EMAIL_SELECTORS[1:]:
			loc = page.locator(selector).first
			try:
				await loc.wait_for(state='visible', timeout=5_000)
				return loc
			except Exception:  # nosec B112
				continue
		raise TimeoutError(f'未找到 guyscode 邮箱输入框: {_EMAIL_SELECTORS}')


async def _accept_terms_if_present(page: 'Page', timeout_ms: int) -> bool:
	"""若弹出服务条款弹窗，点击「同意并继续」（循环重试直至表单解锁）。

	返回 True 表示检测到条款弹窗并已同意；返回 False 表示无弹窗或无需处理。
	"""
	agree = page.get_by_text(_TERMS_AGREE_BUTTON_TEXT, exact=True).first
	try:
		if not await agree.is_visible(timeout=3_000):
			return False
	except Exception:  # nosec B110
		return False

	deadline = time.monotonic() + min(timeout_ms, 20_000) / 1000
	while time.monotonic() < deadline:
		try:
			if not await agree.is_visible(timeout=1_000):
				break
			await agree.click(timeout=5_000)
		except Exception:  # nosec B110
			pass
		if await _email_field_enabled(page):
			log.detail('已同意服务条款，解锁登录表单')
			return True
		await asyncio.sleep(1)
	log.warn('同意条款后表单仍未解锁，将尝试强制填写')
	return True


async def _email_field_enabled(page: 'Page') -> bool:
	"""判断邮箱输入框是否已可见且可编辑（非 disabled）。"""
	try:
		loc = page.locator(_EMAIL_SELECTORS[0]).first
		return bool(await loc.is_visible(timeout=1_000)) and not bool(await loc.is_disabled(timeout=1_000))
	except Exception:  # nosec B110
		return False


async def _force_fill(page: 'Page', selector: str, value: str) -> None:
	"""强制向输入框赋值（绕开 disabled 检查，适用于表单刚由条款解锁的时刻）。"""
	loc = page.locator(selector).first
	try:
		await loc.fill(value, timeout=10_000)
		return
	except Exception:  # nosec B110
		pass
	await loc.evaluate(
		"""(el, v) => {
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
            setter?.call(el, v);
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
		value,
	)


async def _wait_for_turnstile_ready(page: 'Page', account_name: str, timeout_ms: int) -> bool:
	"""等待 Cloudflare Turnstile 令牌就绪（隐藏字段 cf-turnstile-response 有值）。

	登录按钮在 Turnstile 通过前保持 disabled；Turnstile 完成后该字段出现有效 token，
	按钮自动解锁。CloakBrowser 有拟人化能力，可能自动通过交互式复选框；若为手动核验
	模式，则等待人工在弹出窗口完成勾选（需有头模式）。

	返回 True 表示 Turnstile 已通过。
	"""
	deadline = time.monotonic() + timeout_ms / 1000
	while time.monotonic() < deadline:
		loaded = await page.evaluate(
			"""() => {
                const el = document.querySelector('input[name="cf-turnstile-response"]');
                const widget = document.querySelector(
                    '[data-turnstile], .cf-turnstile, [id*="turnstile_widget"]'
                );
                return { hasToken: !!(el && el.value && el.value.length > 20), hasWidget: !!widget };
            }"""
		)
		if loaded.get('hasToken'):
			log.detail(f'{account_name}: Turnstile 已通过')
			await asyncio.sleep(1)
			return True
		# 尝试点击 Turnstile 复选框/外壳触发核验（managed 模式通常无需交互）
		try:
			checkbox = page.locator(
				'iframe[src*="challenges.cloudflare.com"] input[type="checkbox"], '
				'input[type="checkbox"][name="turnstile"]'
			).first
			if await checkbox.is_visible(timeout=400):
				await checkbox.check(timeout=2_000)
		except Exception:  # nosec B110
			pass
		# 等待循环：给手动核验/自动通过留时间
		await asyncio.sleep(1)
	return False


async def _read_tokens_from_storage(
	page: 'Page', account_name: str, *, short_timeout_ms: int | None = None
) -> dict | None:
	"""从 localStorage 读取 JWT（auth_token / refresh_token）。

	登录是整页跳转，无法拦截响应；站点把 JWT 写进 localStorage。轮询等待写入完成。
	short_timeout_ms 用于"已登录态快速探测"，省略时用完整登录超时。
	"""
	timeout_ms = short_timeout_ms if short_timeout_ms is not None else _TOKEN_CAPTURE_TIMEOUT_MS
	deadline = time.monotonic() + timeout_ms / 1000
	while time.monotonic() < deadline:
		tokens = await page.evaluate(
			"""() => ({
                token: localStorage.getItem('auth_token') || '',
                refresh_token: localStorage.getItem('refresh_token') || '',
            })"""
		)
		if tokens.get('token') and tokens.get('refresh_token'):
			log.detail(f'{account_name}: 已从 localStorage 读取到 JWT')
			return {'token': tokens['token'], 'refresh_token': tokens['refresh_token']}
		# 若已跳离登录页但还没写入，等待
		await asyncio.sleep(1)
	return None


async def _browser_login_get_token(
	account_name: str,
	email: str,
	password: str,
	*,
	use_proxy: bool,
	persist_profile: bool,
) -> dict | None:
	"""用浏览器打开登录页，过 Turnstile 后提交，登录成功后从 localStorage 读 JWT。

	登录是整页跳转（非 SPA fetch），无法靠拦截 login 响应捕获 token；
	登录成功后站点把 JWT 存入 localStorage 的 `auth_token` / `refresh_token` 键。

	返回 dict 含 token / refresh_token；任一环节失败返回 None。
	"""
	settings = load_browser_login_settings(account_name, 'guyscode', persist_profile=persist_profile)
	context = await launch_login_context(settings, use_proxy=use_proxy)
	page: 'Page | None' = None
	try:
		page = await context.new_page()
		await prepare_browser_page(page)

		await page.goto(LOGIN_URL, wait_until='load', timeout=min(settings.wait_timeout_ms, 60_000))

		# 若已处于登录态（localStorage 已有 token，可能复用历史登录），直接读取
		pre_logged = await _read_tokens_from_storage(page, account_name, short_timeout_ms=8_000)
		if pre_logged and pre_logged.get('token'):
			log.detail(f'{account_name}: 检测到已登录（localStorage 已有 JWT），直接使用')
			return pre_logged

		# 条款弹窗：未同意前输入框/登录按钮禁用，需点击「同意并继续」解锁
		await _accept_terms_if_present(page, settings.wait_timeout_ms)
		await _wait_for_email_input(page, settings.wait_timeout_ms)
		await _force_fill(page, _EMAIL_SELECTORS[0], email)
		await _force_fill(page, _PASSWORD_SELECTORS[0], password)

		try:
			# Turnstile 通过前登录按钮保持 disabled；等待令牌就绪（需真实浏览器手动核验）
			if not await _wait_for_turnstile_ready(page, account_name, _TOKEN_CAPTURE_TIMEOUT_MS):
				log.failed(f'{account_name}: Turnstile 未通过（需在真实浏览器手动核验）')
				return None

			submit = page.locator(_SUBMIT_SELECTORS[0]).first
			for selector in _SUBMIT_SELECTORS:
				loc = page.locator(selector).first
				try:
					if await loc.is_visible(timeout=3_000):
						submit = loc
						break
				except Exception:  # nosec B112
					continue
			# Turnstile 通过后登录按钮异步解锁；等待其 enabled 再点击，否则 click 会被忽略
			try:
				try:
					await submit.wait_for(state='attached', timeout=15_000)
				except Exception:  # nosec B112
					pass
				deadline = time.monotonic() + 15
				while time.monotonic() < deadline:
					if await submit.is_enabled(timeout=1_000):
						break
					await asyncio.sleep(0.5)
				await submit.click(timeout=20_000)
				log.detail(f'{account_name}: 已点击登录按钮')
			except Exception as e:
				log.warn(f'{account_name}: 点击登录按钮失败: {e}')

			# 点击后等待登录跳转完成，再从 localStorage 读取 JWT
			login_data = await _read_tokens_from_storage(page, account_name)
		except Exception as e:
			log.failed(f'{account_name}: 浏览器登录异常: {e}')
			return None

		if not login_data or not login_data.get('token'):
			log.failed(f'{account_name}: 登录未成功，未能从 localStorage 读取到 token')
			return None
		if is_debug_enabled():
			log.detail(f'{account_name}: 已获取 JWT token（{len(login_data["token"])} 字符）')
		return login_data

	except Exception as e:
		log.failed(f'{account_name}: 浏览器登录异常: {e}')
		return None
	finally:
		if page is not None:
			try:
				await context.close()
			except Exception:  # nosec B110
				pass


def _browser_login_sync(
	account_name: str,
	email: str,
	password: str,
	*,
	use_proxy: bool,
	persist_profile: bool,
) -> dict | None:
	"""在独立线程事件循环中运行浏览器登录，返回登录响应 data（含 token/refresh_token）。"""
	try:
		with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
			future = pool.submit(
				asyncio.run,
				_browser_login_get_token(
					account_name,
					email,
					password,
					use_proxy=use_proxy,
					persist_profile=persist_profile,
				),
			)
			return future.result(timeout=240)
	except Exception as e:
		log.warn(f'{account_name}: 浏览器登录获取凭证失败: {str(e)[:80]}')
		return None


# ---------------------------------------------------------------------------
# httpx 主动签到
# ---------------------------------------------------------------------------


def _make_client(*, use_proxy: bool, token: str) -> httpx.Client:
	"""创建带 Bearer 认证的 httpx 客户端。"""
	return create_client(
		headers={
			'Origin': BASE,
			'Referer': f'{BASE}/',
			'Authorization': f'Bearer {token}',
		},
		use_proxy=use_proxy,
	)


def _extract_token(data: dict) -> str | None:
	"""从登录/刷新响应 data 中提取 JWT，兼容 access_token / token 字段名。"""
	if not isinstance(data, dict):
		return None
	for key in ('access_token', 'token'):
		val = data.get(key)
		if isinstance(val, str) and val:
			return val
	return None


def _refresh_access_token(client: httpx.Client, refresh_token: str, account_name: str) -> str | None:
	"""用 refresh_token 换新 JWT（POST /api/v1/auth/refresh，无需 Turnstile）。

	返回新 token；失败返回 None。
	若响应同时携带新的 refresh_token（轮换机制），自动回写持久化，避免旧 token 被消费后过期。
	"""
	try:
		resp = request_with_retry(client, 'POST', REFRESH_API, json={'refresh_token': refresh_token}, timeout=30)
		code, data = _parse_payload(resp)
		token = _extract_token(data)
		if code == 0 and token:
			# 轮换：刷新接口可能返回新的 refresh_token，回写保存，保证下次续期仍有效
			new_refresh = data.get('refresh_token')
			if isinstance(new_refresh, str) and new_refresh:
				save_refresh_token(account_name, new_refresh)
				log.detail(f'{account_name}: refresh_token 已轮换并回写')
			return token
		log.warn(f'{account_name}: 刷新 token 失败 code={code}（refresh_token 可能已过期）')
	except Exception as e:
		log.warn(f'{account_name}: 刷新 token 异常: {e}')
	return None


def _get_balance(client: httpx.Client, account_name: str) -> float | None:
	"""GET /api/v1/auth/me 取当前余额（美元）。"""
	try:
		resp = request_with_retry(client, 'GET', f'{ME_API}?timezone={TIMEZONE}', timeout=30)
		code, data = _parse_payload(resp)
		if code == 0 and isinstance(data.get('balance'), (int, float)):
			return float(data['balance'])
		log.warn(f'{account_name}: 获取余额失败 code={code}')
	except Exception as e:
		log.warn(f'{account_name}: 获取余额异常: {e}')
	return None


def _fetch_balance_info(client: httpx.Client, account_name: str) -> dict:
	"""GET /api/v1/auth/me 取余额 → 统一信息 dict（查询失败包装为 failed_info，交由中枢中断）。"""
	balance = _get_balance(client, account_name)
	if balance is None:
		return failed_info('获取余额失败', 'usd')
	return _info_from_balance(balance)


def _perform_checkin(client: httpx.Client, account_name: str) -> tuple[bool, str | None]:
	"""POST /api/v1/check-in（空 body + Bearer）。返回 (ok, message)。

	成功时自行打印奖励明细（成功文案含金额，故关闭中枢的通用成功日志）。
	"""
	resp = request_with_retry(client, 'POST', CHECKIN_API, json={}, timeout=30)
	code, data = _parse_payload(resp)
	if code == 0:
		reward = data.get('reward_amount_usd')
		if isinstance(reward, (int, float)):
			log.detail(f'{account_name}: 签到成功，奖励 ${reward}')
		return True, None
	try:
		msg = (resp.json() or {}).get('message') or ''
	except Exception:  # nosec B112
		msg = ''
	return False, msg or resp.text[:80]


def guyscode_checkin(
	account_name: str,
	email: str,
	password: str,
	use_proxy: bool = False,
	persist_profile: bool = False,
) -> tuple[bool, dict | None, dict | None]:
	"""Guyscode 签到：refresh_token 续期优先（纯 API），失败再浏览器登录。

	流程:
	  1. 读持久化 refresh_token → POST /auth/refresh 换新 JWT（无需 Turnstile）
	  2. 刷新失败（过期/无效）→ 浏览器登录过 Turnstile 拿新 {token, refresh_token} 并存档
	  3. 查余额 → 主动签到 → 查余额（编排见 checkin_core.run_standard_checkin）

	返回 (success, user_info_before, user_info_after) 与主流程格式一致。
	"""
	authed: httpx.Client | None = None
	try:

		def authenticate() -> bool:
			nonlocal authed
			refresh_client = _make_client(use_proxy=use_proxy, token='')
			token: str | None = None
			try:
				# ---- 1. 优先用 refresh_token 续期（纯 API）----
				cached_refresh = load_refresh_tokens().get(account_name)
				if cached_refresh:
					token = _refresh_access_token(refresh_client, cached_refresh, account_name)
					if token:
						log.detail(f'{account_name}: 使用 refresh_token 续期成功（纯 API，无需验证码）')

				# ---- 2. 无有效 token → 浏览器登录（首次/refresh 过期）----
				if not token:
					log.info(f'{account_name}: 无有效凭证，改用浏览器登录（需过 Turnstile）...')
					login_data = _browser_login_sync(
						account_name,
						email,
						password,
						use_proxy=use_proxy,
						persist_profile=persist_profile,
					)
					if not login_data or not login_data.get('token'):
						return False
					token = login_data['token']
					new_refresh = login_data.get('refresh_token')
					if new_refresh:
						save_refresh_token(account_name, new_refresh)
						log.detail(f'{account_name}: 已保存新的 refresh_token')
			finally:
				refresh_client.close()

			authed = _make_client(use_proxy=use_proxy, token=token)
			return True

		return run_standard_checkin(
			account_name,
			unit='usd',
			authenticate=authenticate,
			fetch_user_info=lambda: _fetch_balance_info(authed, account_name),
			perform_checkin=lambda: _perform_checkin(authed, account_name),
			success_detail=None,  # 成功文案含奖励金额，由 _perform_checkin 自行打印
		)
	finally:
		if authed is not None:
			authed.close()
