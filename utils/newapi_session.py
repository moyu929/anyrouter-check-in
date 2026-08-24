"""老版 New-API 纯 API 签到分支 — hcnsec 等（session cookie + New-Api-User 头）

与新版 new-api（JWT Bearer，见 newapi_jwt.py）不同，老版 new-api 登录
`POST /api/user/login {username, password}` 直接返回 `{data:{id,...}, success:true}`
并种下 session cookie；此后 user 级接口需带 **cookie + `New-Api-User` 头**（用户 id）。

本分支仅保留差异逻辑（cookie 登录、New-Api-User 头、CNY 显示汇率），标准流程
（认证 → 前余额 → 签到 → 后余额）由 utils.checkin_core.run_standard_checkin 编排。

实测（2026-08-25，api.hcnsec.cn，新疆幻城网安科技公益大模型安全网关）：
  1. POST {domain}/api/user/login  → session cookie + data.id（无 Turnstile 硬门槛）
  2. GET  {domain}/api/status      → usd_exchange_rate（该站 7.3，quota_display_type=CNY）
  3. GET  {domain}/api/user/self   带 cookie + New-Api-User → data.quota/used_quota
  4. POST {domain}/api/user/checkin 带 cookie + New-Api-User → success / "今日已签到"

页面余额 = quota/500000×usd_exchange_rate（CNY 站点换算仍 ÷500000，仅显示单位用 ¥）。
"""

import httpx

from utils.checkin_core import (
	failed_info,
	newapi_login,
	newapi_self_to_info,
	parse_checkin_response,
	run_standard_checkin,
)
from utils.debug import log
from utils.http_client import create_client, request_with_retry


def _login(client: httpx.Client, domain: str, email: str, password: str, account_name: str) -> str | None:
	"""POST /api/user/login 拿 user id（session cookie 由 client 自动管理，
	共用协议见 checkin_core.newapi_login）。失败返回 None。"""
	payload = newapi_login(client, domain, email, password, account_name)
	if payload is None:
		return None
	user_id = payload.get('id')
	if isinstance(user_id, int) and user_id:
		log.detail(f'{account_name}: 登录成功（user id={user_id}，session cookie 已记录）')
		return str(user_id)
	log.failed(f'{account_name}: 登录成功但未取得 user id')
	return None


def _get_exchange_rate(client: httpx.Client, domain: str, account_name: str) -> float:
	"""GET /api/status 取 usd_exchange_rate（美元兑人民币汇率，new-api 显示用）。

	失败返回 1.0（仅显示影响，不阻塞签到）。
	"""
	try:
		resp = request_with_retry(client, 'GET', f'{domain}/api/status', timeout=30)
		data = resp.json()
		d = (data or {}).get('data') or {}
		rate = d.get('usd_exchange_rate')
		if isinstance(rate, (int, float)) and rate > 0:
			log.detail(f'{account_name}: usd_exchange_rate={rate}')
			return float(rate)
	except Exception as e:  # nosec B112
		log.warn(f'{account_name}: 获取汇率失败，按 1.0 显示: {str(e)[:50]}')
	return 1.0


def _get_user_info(client: httpx.Client, domain: str, account_name: str, rate: float = 1.0) -> dict:
	"""GET /api/user/self → 统一信息 dict（quota÷500000×汇率 → 元）。"""
	try:
		resp = request_with_retry(client, 'GET', f'{domain}/api/user/self', timeout=30)
		return newapi_self_to_info(resp, unit='cny', rate=rate)
	except Exception as e:
		return failed_info(f'获取用户信息失败: {str(e)[:50]}...', unit='cny')


def _perform_checkin(client: httpx.Client, domain: str, account_name: str) -> tuple[bool, str | None]:
	"""POST /api/user/checkin（空 body + cookie + New-Api-User）。返回 (ok, message)。"""
	resp = request_with_retry(client, 'POST', f'{domain}/api/user/checkin', json={}, timeout=30)
	return parse_checkin_response(resp)


def newapi_session_checkin(
	account_name: str,
	email: str,
	password: str,
	domain: str,
	use_proxy: bool = False,
) -> tuple[bool, dict | None, dict | None]:
	"""老版 New-API 纯 API 签到：login(cookie) → self → checkin → self（流程见 checkin_core）。"""
	# 同一 client 保持 session cookie；New-Api-User 头登录拿到 id 后再覆写
	client = create_client(
		headers={'Origin': domain, 'Referer': f'{domain}/', 'New-Api-User': '0'},
		use_proxy=use_proxy,
	)
	rate_cell = {'rate': 1.0}
	try:

		def authenticate() -> bool:
			user_id = _login(client, domain, email, password, account_name)
			if not user_id:
				return False
			client.headers['New-Api-User'] = user_id
			# 显示汇率（usd_exchange_rate）：页面余额 = quota/500000×汇率
			rate_cell['rate'] = _get_exchange_rate(client, domain, account_name)
			return True

		return run_standard_checkin(
			account_name,
			unit='cny',
			authenticate=authenticate,
			fetch_user_info=lambda: _get_user_info(client, domain, account_name, rate_cell['rate']),
			perform_checkin=lambda: _perform_checkin(client, domain, account_name),
		)
	finally:
		client.close()
