"""New-API 纯 API 签到分支 — 登录协议与显示币种双自适应（nianhua / kuaipao / hcnsec 等）

new-api 系站点存在两版登录协议，本分支按**登录响应**自动识别，无需按站点配置：

  新版（实测 nianhua rc.22 / hcnsec rc.40 / kuaipao）：
    登录返回 `{data: {access_token, access_expires_at, user: {...}}}`，此后所有请求带
    `Authorization: Bearer <access_token>`。
  老版：登录返回 `{data: {id, username}}`，此后靠登录时种下的 session cookie +
    `New-Api-User: <id>` 头鉴权。

显示币种同样自动识别：GET /api/status（公开接口，无需登录）的 `quota_display_type`
为 CNY 时按 `usd_exchange_rate` 换算成人民币显示，其余（USD / CUSTOM）按美元显示。
页面余额 = quota / 500000 × 汇率。

本分支仅保留差异逻辑（协议识别、鉴权头构造、币种识别），标准流程
（认证 → 前余额 → 签到 → 后余额）由 utils.checkin_core.run_standard_checkin 编排。

实测（2026-09-24，api.hcnsec.cn v1.0.0-rc.40-hc2）：该站从老版 session 协议升级到新版
JWT 协议后，原 newapi_session 分支在登录响应里取不到 `data.id`，报
"登录成功但未取得 user id"；本分支自动改走 Bearer 后恢复正常，且保留该站人民币显示。
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


def _resolve_auth(payload: dict) -> tuple[str, str] | None:
	"""判定登录响应所属协议 → ('bearer', access_token) / ('session', user_id)。

	新版取 `access_token`（兼容 `token` 别名，老站点曾用该字段名）；老版取顶层 `id`，
	兼容新版响应里 `user.id` 的位置。两者都取不到返回 None（由调用方报错）。
	"""
	token = payload.get('access_token') or payload.get('token')
	if isinstance(token, str) and token:
		return 'bearer', token
	user_id = payload.get('id')
	if not user_id:
		user = payload.get('user')
		if isinstance(user, dict):
			user_id = user.get('id')
	if isinstance(user_id, int) and user_id:
		return 'session', str(user_id)
	return None


def _detect_currency(client: httpx.Client, domain: str, account_name: str) -> tuple[str, float]:
	"""GET /api/status 识别站点显示币种 → (unit, rate)。

	quota_display_type=CNY 时返回 ('cny', usd_exchange_rate)，其余返回 ('usd', 1.0)。
	CUSTOM 币种（自定义符号，如 kuaipao）沿用美元符号显示：符号需贯穿统一信息 dict 与
	通知层两套格式，收益不足，暂不引入。
	失败按美元显示（仅影响显示，不阻塞签到）。
	"""
	try:
		resp = request_with_retry(client, 'GET', f'{domain}/api/status', timeout=30)
		d = (resp.json() or {}).get('data') or {}
		if str(d.get('quota_display_type', '')).upper() == 'CNY':
			rate = d.get('usd_exchange_rate')
			rate = float(rate) if isinstance(rate, (int, float)) and rate > 0 else 1.0
			log.detail(f'{account_name}: 站点显示币种 CNY（汇率 {rate}）')
			return 'cny', rate
	except Exception as e:  # nosec B112
		log.warn(f'{account_name}: 站点币种探测失败，按美元显示: {str(e)[:50]}')
	return 'usd', 1.0


def _get_user_info(client: httpx.Client, domain: str, account_name: str, unit: str, rate: float) -> dict:
	"""GET /api/user/self → 统一信息 dict（quota÷500000×汇率）。"""
	try:
		resp = request_with_retry(client, 'GET', f'{domain}/api/user/self', timeout=30)
		return newapi_self_to_info(resp, unit=unit, rate=rate)
	except Exception as e:
		return failed_info(f'获取用户信息失败: {str(e)[:50]}...', unit)


def _perform_checkin(client: httpx.Client, domain: str, account_name: str) -> tuple[bool, str | None]:
	"""POST /api/user/checkin（空 body，鉴权头由 authenticate 按协议注入）。返回 (ok, message)。"""
	resp = request_with_retry(
		client,
		'POST',
		f'{domain}/api/user/checkin',
		json={},
		timeout=30,
		retry_non_idempotent=False,
	)
	return parse_checkin_response(resp)


def newapi_checkin(
	account_name: str,
	email: str,
	password: str,
	domain: str,
	use_proxy: bool = False,
) -> tuple[bool, dict | None, dict | None]:
	"""New-API 纯 API 签到（协议与币种自适应）：login → self → checkin → self。

	登录与后续请求共用同一 client：老版协议依赖登录时种下的 session cookie，
	新版协议则靠 authenticate 写入的 Bearer 头，两者都随该 client 生效。
	"""
	client = create_client(
		headers={'Origin': domain, 'Referer': f'{domain}/'},
		use_proxy=use_proxy,
	)
	try:
		# 币种探测放在登录前：公开接口无需鉴权，且认证失败时的占位信息也能用对单位
		unit, rate = _detect_currency(client, domain, account_name)

		def authenticate() -> bool:
			payload = newapi_login(client, domain, email, password, account_name)
			if payload is None:
				return False
			auth = _resolve_auth(payload)
			if auth is None:
				log.failed(f'{account_name}: 登录成功但未取得 access_token / user id')
				return False
			mode, credential = auth
			if mode == 'bearer':
				client.headers['Authorization'] = f'Bearer {credential}'
				log.detail(f'{account_name}: 登录成功（新版协议 JWT，token {len(credential)} 字符）')
			else:
				client.headers['New-Api-User'] = credential
				log.detail(f'{account_name}: 登录成功（老版协议 session，user id={credential}）')
			return True

		return run_standard_checkin(
			account_name,
			unit=unit,
			authenticate=authenticate,
			fetch_user_info=lambda: _get_user_info(client, domain, account_name, unit, rate),
			perform_checkin=lambda: _perform_checkin(client, domain, account_name),
		)
	finally:
		client.close()
