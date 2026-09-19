"""
GPTGod 纯 API 签到模块 — 双档位：网页端 (_jztz 签名) 与 God Agent 客户端档

本分支仅保留差异逻辑（加密签名协议、积分单位、客户端档位令牌），标准流程
（登录 → 前积分 → 签到 → 后积分）由 utils.checkin_core.run_standard_checkin 编排。

网页端点位 (auth_method='gptgod'):
  1. POST /api/user/login 登录拿 cookies（密码 MD5）
  2. GET /api/user/info 查积分与签到状态（checkin 字段预判幂等）
  3. GET /api/user/register-config 拿 {_e, _k, _n}
  4. wa(_e, _k) 解密 _e 得到签名函数的 JS 代码
  5. 从 JS 代码提取 py（重排表）和 mc（XOR 密钥）
  6. 构造伪造的 33 项行为指纹数组，生成 _jztz
  7. POST /api/user/checkin body={_k, _jztz}

God Agent 客户端档位 (auth_method='gptgod_agent'):
  与网页端共用同一签到端口 /api/user/checkin，但凭以下两点拿客户端高积分档：
  1. POST /api/user/device/token 用登录态换 deviceToken（12h 有效），签到请求带
     Authorization: Bearer <deviceToken> ——服务端凭此按 god-agent 那一档发分
  2. GET /api/user/risk/native-session 取一次性风控会话 {enabled, sid, field, key, perm}，
     构造 33 槽快照并编码（perm 重排 → JSON → UTF-8 → 与 key 逐字节 XOR → base64），
     随签到提交 body={_k: sid, [field]: encoded}。
     ⚠️ 不带风控数据时服务端按"无指纹"打分（-5/阈值 30）→ 当天标记照打、积分一分不发，
     因此风控采集失败时本分支宁可中断本次签到，避免白签。
"""

import base64
import hashlib
import json
import os
import random
import re
import time
import uuid
from typing import TypedDict
from urllib.parse import unquote

import httpx

from utils.checkin_core import build_user_info, login_failed_info, run_standard_checkin
from utils.debug import log
from utils.http_client import create_client, request_with_retry

BASE = 'https://gptgod.online'

# God Agent 客户端档位常量
AGENT_DEVICE_TOKEN_TTL_SEC = 12 * 3600  # 服务端签发有效 12h
AGENT_DEVICE_TOKEN_RENEW_MARGIN_SEC = 3600  # 到期前 1h 视为需续签
AGENT_APP_VERSION = '0.9.0'
AGENT_DEFAULT_GROUP = 'default'
AGENT_COOLDOWN_SEC = 600  # 限流（429 / 业务码 -10）后冷却 10 分钟
# device_token / device_id 持久化文件（json: {账号名: {token, expires_at}} + 设备身份）
AGENT_STATE_FILE = 'gptgod_agent_state.json'


class _GptGodState(TypedDict):
	calls: int
	credits: list[int | None]
	raw: list[dict | None]


# ---------------------------------------------------------------------------
# wa() 解密 — Python 实现
# ---------------------------------------------------------------------------


def wa_decrypt(_e: str, _k: str) -> str:
	"""Python 实现 JS 的 wa(s, a) 函数。
	Base64 解码 → XOR 解密。
	"""
	n_bytes = base64.b64decode(_e)
	t = [(ord(_k[h % len(_k)]) ^ 66) + h * 55 & 255 for h in range(16)]
	return ''.join(chr(n_bytes[i] ^ t[i % 16]) for i in range(len(n_bytes)))


# ---------------------------------------------------------------------------
# 签名参数提取
# ---------------------------------------------------------------------------


def extract_sign_params(js_code: str) -> tuple[list[int], list[int]] | None:
	"""从解密 JS 中提取 py（33 项重排表）和 mc（16 项 XOR 密钥）。"""
	arrays = re.findall(r'var\s+(\w+)\s*=\s*\[([\s0-9.,\-]+)\]', js_code)
	py = mc = None
	for _name, items in arrays:
		try:
			nums = [int(float(x.strip())) for x in items.split(',') if x.strip()]
		except ValueError:
			continue
		if len(nums) == 33:
			py = nums
		elif len(nums) == 16:
			mc = nums
	return (py, mc) if py is not None and mc is not None else None


# ---------------------------------------------------------------------------
# 签名生成
# ---------------------------------------------------------------------------


def generate_jztz(fingerprint: list, py: list[int], mc: list[int]) -> str:
	"""Python 实现 JS 签名函数：重排 → JSON → UTF-8 → XOR → Base64。"""
	ywtz = [fingerprint[py[i]] for i in range(33)]
	juv = json.dumps(ywtz, separators=(',', ':'), ensure_ascii=False)
	fu = list(juv.encode('utf-8'))
	for i in range(len(fu)):
		fu[i] = (fu[i] ^ mc[i & 15]) & 0xFF
	return base64.b64encode(bytes(fu)).decode('ascii')


# ---------------------------------------------------------------------------
# 伪造行为指纹
# ---------------------------------------------------------------------------

# 设备指纹：长期保持稳定，不做轮换。
# 站点通常不会对长期不变的指纹起疑，反而短期频繁轮换更易被识别为异常脚本。
# 依据项目"签到分支只含纯签到逻辑、资源/通用能力收敛于中心系统"的架构原则，
# 去掉按账号持久化 + TTL 轮换的机制，改用一份固定指纹；仅保留单次访问的行为
# 参数（停留时长、点击时序等）随机，以贴近真实浏览器访问。
_DEVICE_FP = {
	'canvas': '3f6d5c2a9b8e47f1a2d3e4b5c6012345',
	'webgl': '9c4a8f2b7d3e6015a8f3c2b1d9e4a6f5',
	'audio': '6b2e8c5a4d3f1079e2b8a6c4d0f3e5a7',
	'screen': '1920x1080x24',
	'cpu': 8,
	'memory': 16,
	'fonts': '110010101110101100101010110010',
	'language': 'zh-CN',
	'plugins': 5,
	'ph1': 'a1b2c3d4e5f60718',
	'ph2': '9f8e7d6c5b4a3921',
	'ph3': '0f1e2d3c4b5a6978',
	'ph4': 42,
	'ph5': 57,
	'ph6': 'c0ffeeddccbbaa99',
}


def build_fake_fingerprint() -> list:
	"""构造 33 项行为指纹数组。

	设备身份字段使用固定稳定指纹；仅保留单次访问的行为参数（停留/时序）随机。
	"""
	dev = _DEVICE_FP
	stay_ms = random.randint(3000, 15000)
	return [
		dev['canvas'],  # 0
		dev['webgl'],  # 1
		dev['screen'],  # 2
		-480,  # 3
		dev['language'],  # 4
		'Win32',  # 5
		dev['cpu'],  # 6
		dev['memory'],  # 7
		0,  # 8
		dev['audio'],  # 9
		dev['fonts'],  # 10
		0,
		0,
		1,
		1,  # 11-14
		dev['plugins'],  # 15
		1,  # 16
		stay_ms,  # 17
		random.randint(8, 25),  # 18
		random.randint(500, 2500),  # 19
		random.randint(3, 15),  # 20
		random.randint(1, 6),  # 21
		random.randint(0, 2),  # 22
		random.randint(0, 2),  # 23
		0,  # 24
		round(random.uniform(80, 200), 1),  # 25
		dev['ph1'],
		dev['ph2'],
		dev['ph3'],
		dev['ph4'],
		dev['ph5'],
		dev['ph6'],  # 26-31
		int(time.time() * 1000),  # 32
	]


# ---------------------------------------------------------------------------
# API 客户端
# ---------------------------------------------------------------------------


def _extract_credits(info: dict | None) -> int | None:
	"""从 user/info 数据中提取积分，兼容多种字段名。"""
	if not info:
		return None
	for key in ('tokens', 'credits', 'point', 'points', 'balance', 'integral'):
		val = info.get(key)
		if isinstance(val, (int, float)) and not isinstance(val, bool):
			return int(val)
	return None


def _extract_group_code(info: dict | None) -> str:
	"""从 user/info 提取当前分组；取不到时用客户端默认分组。"""
	if isinstance(info, dict):
		settings = info.get('settings')
		if isinstance(settings, dict):
			group = settings.get('groupCode')
			if isinstance(group, str) and group.strip():
				return group.strip()
	return AGENT_DEFAULT_GROUP


def _make_client(*, use_proxy: bool = False) -> httpx.Client:
	"""创建统一的 httpx 客户端。"""
	return create_client(
		headers={
			'Origin': BASE,
			'Referer': f'{BASE}/',
		},
		use_proxy=use_proxy,
	)


def _get_user_info(client: httpx.Client) -> dict | None:
	"""查询用户信息（含积分、签到状态，带重试）。"""
	try:
		r = request_with_retry(client, 'GET', f'{BASE}/api/user/info', timeout=30)
		if r.status_code == 200:
			d = r.json()
			if d.get('code') == 0:
				data = d.get('data')
				return data if isinstance(data, dict) else None
	except Exception:
		pass
	return None


def _authenticate(client: httpx.Client, account_name: str, email: str, password: str) -> bool:
	"""网页/客户端档位共用的登录流程：预热拿 XSRF-TOKEN → 密码 MD5 登录。"""
	# 预热（拿 XSRF-TOKEN cookie）
	try:
		request_with_retry(
			client,
			'GET',
			f'{BASE}/',
			timeout=30,
			headers={'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'},
		)
		xsrf = client.cookies.get('XSRF-TOKEN')
		if xsrf:
			client.headers.update({'X-XSRF-TOKEN': unquote(xsrf)})
	except Exception as e:
		log.warn(f'{account_name}: 预热请求失败: {e}')

	# 登录（对方协议要求提交 MD5 后的密码，非本地安全用途）
	password_md5 = hashlib.md5(  # nosec B324
		password.encode('utf-8'), usedforsecurity=False
	).hexdigest()
	try:
		login_resp = request_with_retry(
			client,
			'POST',
			f'{BASE}/api/user/login',
			json={'email': email, 'password': password_md5, 'auto_login': True},
			timeout=30,
			retry_non_idempotent=True,
		)
		login_data = login_resp.json() if login_resp.status_code == 200 else {}
		if login_data.get('code') != 0:
			err = login_data.get('msg', f'HTTP {login_resp.status_code}')
			log.failed(f'{account_name}: 登录失败 - {err}')
			return False
	except Exception as e:
		log.failed(f'{account_name}: 登录请求失败: {e}')
		return False
	log.detail(f'{account_name}: 登录成功')
	return True


# ---------------------------------------------------------------------------
# God Agent 客户端档位：deviceToken 持久化 + native-session 风控编码
# ---------------------------------------------------------------------------

# 模块级限流冷却截止（进程内即可，重跑自然重置）
_agent_cooldown_until: float = 0.0


def agent_state_file() -> str:
	"""持久化文件路径（允许测试覆盖）。"""
	return os.getenv('GPTGOD_AGENT_STATE_FILE', AGENT_STATE_FILE)


def _load_agent_state() -> dict:
	"""加载 {账号名: {'token': str, 'expires_at': float}, 设备身份...}。"""
	try:
		if os.path.exists(agent_state_file()):
			with open(agent_state_file(), 'r', encoding='utf-8') as f:
				state = json.load(f)
				return state if isinstance(state, dict) else {}
	except Exception:  # nosec B112
		pass
	return {}


def _save_agent_state(state: dict) -> None:
	"""原子写持久化文件。"""
	try:
		tmp = f'{agent_state_file()}.tmp'
		with open(tmp, 'w', encoding='utf-8') as f:
			json.dump(state, f, ensure_ascii=False, indent=2)
		os.replace(tmp, agent_state_file())
	except Exception as e:  # nosec B112
		log.warn(f'保存 God Agent 状态失败: {e}')


def _ensure_device(state: dict) -> dict:
	"""设备身份（device_id / install_ts / machine_id 等）首次生成后持久化。"""
	device = state.get('device')
	if isinstance(device, dict) and device.get('device_id'):
		return device
	now = int(time.time())
	device = {
		'device_id': uuid.uuid4().hex,
		'install_ts': now,
		'machine_id': uuid.uuid4().hex,
		'os_version': 'Windows 10.0.22631',
		'app_version': AGENT_APP_VERSION,
		'platform': 'Windows',
	}
	state['device'] = device
	_save_agent_state(state)
	return device


def _is_rate_limited(e: Exception) -> bool:
	"""服务端限流判定：HTTP 429，或网关 -10 业务码（与 God Agent 客户端一致）。"""
	status = getattr(e, 'http_status', None)
	code = getattr(e, 'code', None)
	if isinstance(getattr(e, 'response', None), httpx.Response):
		response = getattr(e, 'response')
		status = response.status_code
		try:
			code = response.json().get('code')
		except Exception:  # nosec B112
			code = None
	return status == 429 or code == -10


def _issue_device_token(client: httpx.Client, device_id: str, group_code: str) -> dict | None:
	"""POST /api/user/device/token 用登录态换一枚设备令牌（12h 有效）。"""
	global _agent_cooldown_until
	now = time.time()
	if now < _agent_cooldown_until:
		minutes = max(1, round((_agent_cooldown_until - now) / 60))
		log.warn(f'God Agent 设备令牌接口限流中，冷却剩余 {minutes} 分钟')
		return None
	try:
		resp = request_with_retry(
			client,
			'POST',
			f'{BASE}/api/user/device/token',
			json={'device_id': device_id, 'group_code': group_code},
			timeout=30,
			retry_non_idempotent=True,
		)
		data = resp.json() if resp.status_code == 200 else {}
		if data.get('code', 0) != 0:
			raise RuntimeError(f'device/token 返回业务码 {data.get("code")}')
		token = data.get('token') or (data.get('data') or {}).get('token')
		if not token:
			raise RuntimeError('device/token 响应缺少 token')
		ttl = int(data.get('expires_in') or AGENT_DEVICE_TOKEN_TTL_SEC)
		log.detail(f'God Agent 设备令牌签发成功（有效期 {ttl}s）')
		return {'token': token, 'expires_at': now + ttl, 'ttl_sec': ttl}
	except Exception as e:
		if _is_rate_limited(e):
			_agent_cooldown_until = now + AGENT_COOLDOWN_SEC
			log.warn('God Agent 设备令牌接口限流（429 / -10），冷却 10 分钟')
		else:
			log.warn(f'God Agent 设备令牌签发失败: {e}')
		return None


def _get_device_token(
	client: httpx.Client,
	account_key: str,
	state: dict,
	group_code: str,
) -> str | None:
	"""取当前账号的可用设备令牌：持久化未过期则复用，否则重新签发并落盘。"""
	record = (state.get('tokens') or {}).get(account_key)
	if isinstance(record, dict):
		token = record.get('token')
		expires_at = float(record.get('expires_at') or 0)
		if token and expires_at - time.time() > AGENT_DEVICE_TOKEN_RENEW_MARGIN_SEC:
			return token
	device = _ensure_device(state)
	issued = _issue_device_token(client, device['device_id'], group_code)
	if not issued:
		return None
	tokens = state.setdefault('tokens', {})
	tokens[account_key] = {'token': issued['token'], 'expires_at': issued['expires_at']}
	_save_agent_state(state)
	return issued['token']


def build_agent_risk_snapshot(device: dict) -> list:
	"""构造 God Agent 客户端 33 槽风控快照（槽位顺序即服务端契约，与客户端对齐）。

	设备身份字段（来源标识"app"、machine_id、os_version、is_vm、install_ts、
	app_version）使用持久化稳定值；仅保留单次访问的行为参数（停留/点击时序）随机。
	"""
	stay_ms = random.randint(3000, 15000)
	return [
		'',  # 0  canvas（native 采集，客户端留空即可）
		'',  # 1  webgl
		'1920x1080',  # 2  screen
		-480,  # 3  timezoneOffset（东八区）
		'zh-CN',  # 4  language
		device.get('platform', 'Windows'),  # 5  platform
		8,  # 6  hardwareConcurrency
		0,  # 7  （memory 未知）
		0,  # 8  maxTouchPoints>0（桌面 false）
		'',  # 9  audio
		'',  # 10 fonts
		0,  # 11 webdriver
		0,  # 12 （保留）
		1,  # 13 hasLocalStorage
		1,  # 14 cookieEnabled
		5,  # 15 plugins.length
		1,  # 16 （固定 1）
		stay_ms,  # 17 距 startedAt 停留
		random.randint(2, 12),  # 18 mouseMoves
		random.randint(100, 800),  # 19 mouseDistance
		random.randint(0, 4),  # 20 keyPresses
		random.randint(0, 3),  # 21 scrolls
		0,  # 22 focusChanges
		0,  # 23 formFocusCount
		0,  # 24 （保留）
		0,  # 25 （保留）
		'app',  # 26 来源标识（客户端固定）
		device.get('machine_id', ''),  # 27 machine_id
		device.get('os_version', ''),  # 28 os_version
		0,  # 29 is_vm
		int(device.get('install_ts', 0)),  # 30 install_ts
		device.get('app_version', AGENT_APP_VERSION),  # 31 app_version
		int(time.time() * 1000),  # 32 采集时刻（毫秒）
	]


def _risk_key_bytes(key) -> bytes | None:
	"""风控 key 归一化：兼容 int 字节数组（服务端 2026-09 起返回 [31,48,...]）与字符串（旧格式）。

	返回 16 字节以上的有效密钥字节；无效返回 None。
	"""
	if isinstance(key, bytes):
		kbytes = key
	elif isinstance(key, str):
		kbytes = key.encode('utf-8')
	elif isinstance(key, list) and key and all(isinstance(v, int) and 0 <= v <= 255 for v in key):
		kbytes = bytes(key)
	else:
		return None
	return kbytes if len(kbytes) >= 16 else None


def encode_risk_snapshot(key, perm: list[int], slots: list) -> str:
	"""客户端 encodeRiskSnapshot 的 Python 实现。

	五步顺序不能动：打乱 → JSON → UTF-8 → 与密钥逐字节 XOR → base64。
	key 兼容 int 字节数组与字符串两种格式（见 _risk_key_bytes）。
	"""
	key_bytes = _risk_key_bytes(key)
	if key_bytes is None:
		raise ValueError('风控 key 无效或长度不足')
	shuffled = [slots[p] for p in perm]
	raw = json.dumps(shuffled, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
	xored = bytes(b ^ key_bytes[i & 15] for i, b in enumerate(raw))
	return base64.b64encode(xored).decode('ascii')


def _collect_risk_fields(client: httpx.Client, device: dict) -> dict | None:
	"""取一次性风控会话并编码，返回 {_k, <field>: encoded}；失败返回 None（需中断）。"""
	try:
		resp = request_with_retry(client, 'GET', f'{BASE}/api/user/risk/native-session', timeout=30)
		session = resp.json() if resp.status_code == 200 else {}
	except Exception as e:  # nosec B112
		log.warn(f'God Agent 风控会话获取失败: {e}')
		return None
	if not isinstance(session, dict):
		return None
	sid, field, key = session.get('sid'), session.get('field'), session.get('key')
	perm = session.get('perm')
	if (
		session.get('enabled') is False
		or not sid
		or not field
		or _risk_key_bytes(key) is None
		or not isinstance(perm, list)
		or len(perm) != 33
	):
		log.warn('God Agent 风控会话不可用（enabled=false 或参数缺失/key 无效）')
		return None
	try:
		encoded = encode_risk_snapshot(key, perm, build_agent_risk_snapshot(device))
	except Exception as e:  # nosec B112
		log.warn(f'God Agent 风控快照编码失败: {e}')
		return None
	return {'_k': sid, field: encoded}


def _account_key(email: str) -> str:
	"""账号标识（持久化 token 使用；双账号同邮箱必须同档位，故用 email 即够）。"""
	return email


def _credits_info(credits_value: int | None) -> dict:
	"""构造与主流程兼容的用户信息字典（GPTGod 余额单位是积分，unit 供通知层区分渲染）。"""
	return build_user_info(credits_value or 0, 0, 'credits')


def _login_failed_info() -> dict:
	"""登录失败时的占位用户信息。"""
	return login_failed_info('credits')


def _run_common(
	account_name: str,
	email: str,
	password: str,
	use_proxy: bool,
	make_checkin: object,
) -> tuple[bool, dict | None, dict | None]:
	"""网页/客户端档位共享的签到编排：登录 → 前积分 → 签到 → 后积分。

	make_checkin 为工厂闭包，接收 (client, group_holder)，返回无参签到闭包
	（参考类型 Callable[[httpx.Client, dict], Callable[[], tuple[bool, str | None]]]）。
	group_holder 在首次查积分时填充账号当前分组，供 token 签发与签到闭包使用。
	"""
	client = _make_client(use_proxy=use_proxy)
	try:
		# 状态记录：首查/次查积分与原始 info（None 表示查询失败，防假成功校验需区分）
		state: _GptGodState = {'calls': 0, 'credits': [None, None], 'raw': [None, None]}
		group_holder: dict = {'code': AGENT_DEFAULT_GROUP}

		def authenticate() -> bool:
			return _authenticate(client, account_name, email, password)

		def fetch_user_info() -> dict:
			"""查积分与签到状态；查询失败时仍返回成功形态（quota=0），不阻塞签到。"""
			info = _get_user_info(client)
			credits = _extract_credits(info)
			idx = min(state['calls'], 1)
			state['raw'][idx] = info
			state['credits'][idx] = credits
			state['calls'] += 1
			if idx == 0:
				group_holder['code'] = _extract_group_code(info)
				checked = bool(info.get('checkin')) if info else False
				log.detail(f'{account_name}: 签到前积分={credits}, 已签到={checked}')
			else:
				log.detail(f'{account_name}: 签到后积分={credits}')
			return _credits_info(credits)

		def already_checked_via_info(_before: dict) -> bool:
			"""GPTGod 幂等预判：用户信息自带签到状态，已签到则跳过签到请求。"""
			first = state['raw'][0]
			return bool(first.get('checkin')) if first else False

		def post_checkin(_before: dict, _after: dict) -> None:
			"""积分防假成功校验（任一次查询失败则跳过，与原实现一致）。"""
			credits_before, credits_after = state['credits']
			if credits_before is None or credits_after is None:
				return
			diff = credits_after - credits_before
			if diff > 0:
				log.detail(f'{account_name}: 积分增加 {diff}，签到确认成功！')
			elif diff == 0:
				log.warn(f'{account_name}: 积分未变化（{credits_before}），可能为假成功')
			else:
				log.warn(f'{account_name}: 积分减少（{credits_before} -> {credits_after}）')

		perform_checkin = make_checkin(client, group_holder)  # type: ignore[operator]
		return run_standard_checkin(
			account_name,
			unit='credits',
			authenticate=authenticate,
			fetch_user_info=fetch_user_info,
			already_checked_via_info=already_checked_via_info,
			perform_checkin=perform_checkin,
			post_checkin=post_checkin,
			success_detail='签到 API 请求成功',
		)
	finally:
		client.close()


def _make_web_checkin(client: httpx.Client, _group_holder: dict) -> object:
	"""网页档签到闭包工厂：register-config → 解密 → 指纹 → _jztz → 签到。"""

	def perform_checkin() -> tuple[bool, str | None]:
		try:
			cfg_resp = request_with_retry(client, 'GET', f'{BASE}/api/user/register-config', timeout=30)
			cfg_data = cfg_resp.json() if cfg_resp.status_code == 200 else {}
			cfg = cfg_data.get('data', {})
			_e, _k, _n = cfg['_e'], cfg['_k'], cfg['_n']
		except Exception as e:
			return False, f'获取注册配置失败: {e}'

		try:
			js_code = wa_decrypt(_e, _k)
		except Exception as e:
			return False, f'解密失败: {e}'

		params = extract_sign_params(js_code)
		if not params:
			return False, '提取签名参数失败'
		py, mc = params

		fingerprint = build_fake_fingerprint()
		try:
			_jztz = generate_jztz(fingerprint, py, mc)
		except Exception as e:
			return False, f'生成 _jztz 签名失败: {e}'

		checkin_resp = request_with_retry(
			client,
			'POST',
			f'{BASE}/api/user/checkin',
			json={'_k': _k, _n: _jztz},
			timeout=30,
			retry_non_idempotent=False,
		)
		checkin_data = checkin_resp.json() if checkin_resp.status_code == 200 else {}
		if checkin_data.get('code') != 0:
			return False, checkin_data.get('msg', f'HTTP {checkin_resp.status_code}')
		return True, None

	return perform_checkin


def _make_agent_checkin(
	client: httpx.Client,
	group_holder: dict,
	account_name: str,
	email: str,
) -> object:
	"""God Agent 客户端档签到闭包工厂：device/token + native-session 风控 + 签到。"""
	state: dict = _load_agent_state()
	account_key = _account_key(email)

	def perform_checkin() -> tuple[bool, str | None]:
		# 分组随签发写死进令牌，用首次 user/info 拿到的账号当前分组
		token = _get_device_token(client, account_key, state, group_holder['code'])
		if not token:
			return False, 'God Agent 设备令牌获取失败'

		# 风控会话必须每次重新取（一次性、服务端读完即删、10 分钟过期）
		try:
			resp = request_with_retry(client, 'GET', f'{BASE}/api/user/risk/native-session', timeout=30)
			session = resp.json() if resp.status_code == 200 else {}
		except Exception as e:
			return False, f'获取风控会话失败: {e}'

		if not isinstance(session, dict):
			return False, '风控会话响应格式错误'
		sid, field, key = session.get('sid'), session.get('field'), session.get('key')
		perm = session.get('perm')
		enabled = session.get('enabled')
		if enabled is False:
			# enabled=false 说明站点当前关闭/降级风控：跟随客户端照常签
			log.warn(f'{account_name}: 风控会话 disabled，按无风控提交')
			risk_body: dict = {}
		elif not sid or not field or _risk_key_bytes(key) is None or not isinstance(perm, list) or len(perm) != 33:
			return False, '风控会话参数不完整（sid/field/key/perm）'
		else:
			device = _ensure_device(state)
			try:
				encoded = encode_risk_snapshot(key, perm, build_agent_risk_snapshot(device))
			except Exception as e:
				return False, f'风控快照编码失败: {e}'
			risk_body = {'_k': sid, field: encoded}

		checkin_resp = request_with_retry(
			client,
			'POST',
			f'{BASE}/api/user/checkin',
			json=risk_body,
			headers={'Authorization': f'Bearer {token}'},
			timeout=30,
			retry_non_idempotent=False,
		)
		checkin_data = checkin_resp.json() if checkin_resp.status_code == 200 else {}
		if checkin_data.get('code') != 0:
			return False, checkin_data.get('msg', f'HTTP {checkin_resp.status_code}')
		credits = checkin_data.get('credits')
		if isinstance(credits, (int, float)):
			log.detail(f'{account_name}: 客户端档签到成功，档位积分 {int(credits)}')
		return True, None

	return perform_checkin


def gptgod_checkin(
	account_name: str,
	email: str,
	password: str,
	use_proxy: bool = False,
) -> tuple[bool, dict | None, dict | None]:
	"""GPTGod 网页档纯 API 签到：登录 → 获取配置 → 生成 _jztz → 签到（流程见 checkin_core）。"""
	return _run_common(
		account_name,
		email,
		password,
		use_proxy,
		lambda _c, _g: _make_web_checkin(_c, _g),
	)


def gptgod_agent_checkin(
	account_name: str,
	email: str,
	password: str,
	use_proxy: bool = False,
) -> tuple[bool, dict | None, dict | None]:
	"""GPTGod God Agent 客户端档纯 API 签到（auth_method='gptgod_agent'）。

	与网页端共用 /api/user/checkin，差异只在签到载荷：
	  1. device/token 换设备令牌（持久化+TTL 复用，带登录态）
	  2. native-session 取一次性风控会话并编码
	  3. 签到带 Authorization: Bearer <deviceToken> + body={_k, <field>: encoded}

	风控采集失败时不发签到（宁错过本次、避免"标记照打积分不发"白签）。
	"""
	return _run_common(
		account_name,
		email,
		password,
		use_proxy,
		lambda _c, _g: _make_agent_checkin(_c, _g, account_name, email),
	)
