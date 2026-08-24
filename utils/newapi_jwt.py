"""New-API JWT Bearer 签到分支 — 新版 new-api（nianhua / superapi 等）纯 API 签到

与老版 new-api（cookie session + New-Api-User 头，见 newapi_session.py）不同，
新版 new-api 登录返回 OAuth2 风格凭证：access_token + access_expires_at + session，
之后所有请求都带 `Authorization: Bearer <access_token>`。

本分支仅保留差异逻辑（JWT 登录、Bearer 客户端构造），标准流程
（认证 → 前余额 → 签到 → 后余额）由 utils.checkin_core.run_standard_checkin 编排。

实测（2026-08-24）：
  1. POST {domain}/api/user/login  body {username, password}，无 Turnstile 硬门槛
  2. GET  {domain}/api/user/self    带 Bearer，取 quota / used_quota（×500000=USD）
  3. POST {domain}/api/user/checkin 带 Bearer，空 body，主动签到
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
	"""POST /api/user/login 拿 access_token（共用协议见 checkin_core.newapi_login）。"""
	payload = newapi_login(client, domain, email, password, account_name)
	if payload is None:
		return None
	token = payload.get('access_token') or payload.get('token')
	if not isinstance(token, str) or not token:
		log.failed(f'{account_name}: 登录成功但未取得 access_token')
		return None
	log.detail(f'{account_name}: 登录成功（access_token {len(token)} 字符）')
	return token


def _get_user_info(client: httpx.Client, domain: str, account_name: str) -> dict:
	"""GET /api/user/self → 统一信息 dict（美元）。"""
	try:
		resp = request_with_retry(client, 'GET', f'{domain}/api/user/self', timeout=30)
		return newapi_self_to_info(resp, unit='usd')
	except Exception as e:
		return failed_info(f'获取用户信息失败: {str(e)[:50]}...')


def _perform_checkin(client: httpx.Client, domain: str, account_name: str) -> tuple[bool, str | None]:
	"""POST /api/user/checkin（空 body + Bearer）。返回 (ok, message)。"""
	resp = request_with_retry(client, 'POST', f'{domain}/api/user/checkin', json={}, timeout=30)
	return parse_checkin_response(resp)


def newapi_jwt_checkin(
	account_name: str,
	email: str,
	password: str,
	domain: str,
	use_proxy: bool = False,
) -> tuple[bool, dict | None, dict | None]:
	"""New-API JWT 纯 API 签到：login → self → checkin → self（流程见 checkin_core）。"""
	authed: httpx.Client | None = None
	try:

		def authenticate() -> bool:
			nonlocal authed
			login_client = create_client(use_proxy=use_proxy)
			try:
				token = _login(login_client, domain, email, password, account_name)
				if not token:
					return False
				authed = create_client(
					headers={
						'Origin': domain,
						'Referer': f'{domain}/',
						'Authorization': f'Bearer {token}',
					},
					use_proxy=use_proxy,
				)
				return True
			finally:
				login_client.close()

		return run_standard_checkin(
			account_name,
			unit='usd',
			authenticate=authenticate,
			fetch_user_info=lambda: _get_user_info(authed, domain, account_name),
			perform_checkin=lambda: _perform_checkin(authed, domain, account_name),
		)
	finally:
		if authed is not None:
			authed.close()
