"""
GPTGod 纯 API 签到模块 — _jztz 签名算法实现

签到流程:
  1. POST /api/user/login 登录拿 cookies
  2. GET /api/user/register-config 拿 {_e, _k, _n}
  3. wa(_e, _k) 解密 _e 得到签名函数的 JS 代码
  4. 从 JS 代码提取 py（重排表）和 mc（XOR 密钥）
  5. 构造伪造的 33 项行为指纹数组
  6. 用签名算法处理指纹，生成 _jztz
  7. POST /api/user/checkin body={_k, _jztz}
"""

import base64
import hashlib
import json
import os
import random
import re
import secrets
import time
from urllib.parse import unquote

import httpx

from utils.debug import log
from utils.http_client import create_client, request_with_retry

BASE = 'https://gptgod.online'

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

_SCREEN_RESOLUTIONS = [
	'1920x1080x24',
	'2560x1440x24',
	'1366x768x24',
	'1680x1050x24',
	'1440x900x24',
	'1536x864x24',
	'1280x720x24',
	'1600x900x24',
]
_CPU_CORES = [4, 8, 12, 16]
_DEVICE_MEMORIES = [4, 8, 16]
_LANGUAGES = ['zh-CN', 'zh-CN', 'zh-CN', 'en-US']
_DEVICE_FP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.device_fp')
_DEVICE_FP_TTL_DAYS = 5


def _random_hex(length: int) -> str:
	return secrets.token_hex(length // 2)


def _random_font_fingerprint() -> str:
	length = random.randint(20, 40)
	return ''.join(str(random.randint(0, 1)) for _ in range(length))


def _generate_device_fingerprint() -> dict:
	return {
		'canvas': _random_hex(32),
		'webgl': _random_hex(32),
		'audio': _random_hex(32),
		'screen': random.choice(_SCREEN_RESOLUTIONS),
		'cpu': random.choice(_CPU_CORES),
		'memory': random.choice(_DEVICE_MEMORIES),
		'fonts': _random_font_fingerprint(),
		'language': random.choice(_LANGUAGES),
		'plugins': random.randint(3, 7),
		'ph1': _random_hex(16),
		'ph2': _random_hex(16),
		'ph3': _random_hex(16),
		'ph4': random.randint(0, 100),
		'ph5': random.randint(0, 100),
		'ph6': _random_hex(16),
		'born_at': int(time.time()),
	}


def _load_device_fingerprint(account_key: str) -> dict:
	"""加载或生成设备指纹，按账号隔离、带 TTL 轮换。"""
	account_hash = hashlib.md5(  # nosec B324 - 仅用于文件名派生，非安全用途
		account_key.encode('utf-8'), usedforsecurity=False
	).hexdigest()[:8]
	fp_file = os.path.join(_DEVICE_FP_DIR, f'device_{account_hash}.json')

	fp: dict | None = None
	try:
		if os.path.exists(fp_file):
			with open(fp_file, 'r', encoding='utf-8') as f:
				fp = json.load(f)
			born_at = fp.get('born_at', 0)
			if (time.time() - born_at) / 86400 > _DEVICE_FP_TTL_DAYS:
				fp = None
	except Exception:
		fp = None

	if fp is None:
		fp = _generate_device_fingerprint()
		try:
			os.makedirs(_DEVICE_FP_DIR, exist_ok=True)
			with open(fp_file, 'w', encoding='utf-8') as f:
				json.dump(fp, f, ensure_ascii=False)
		except OSError:
			pass

	return fp


def build_fake_fingerprint(account_key: str) -> list:
	"""构造 33 项行为指纹数组。"""
	dev = _load_device_fingerprint(account_key)
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


def _credits_info(credits_value: int | None) -> dict:
	"""构造与主流程兼容的用户信息字典。

	GPTGod 的余额单位是积分而非美元，unit 字段供通知层区分渲染。
	"""
	amount = credits_value or 0
	return {
		'success': True,
		'quota': amount,
		'used_quota': 0,
		'unit': 'credits',
		'display': f'💰 当前积分: {amount}',
	}


def _login_failed_info() -> dict:
	"""登录失败时的占位用户信息。"""
	return {'success': False, 'quota': 0, 'used_quota': 0, 'unit': 'credits', 'error': 'Login failed'}


def gptgod_checkin(
	account_name: str,
	email: str,
	password: str,
	use_proxy: bool = False,
) -> tuple[bool, dict | None, dict | None]:
	"""GPTGod 纯 API 签到：登录 → 获取配置 → 生成 _jztz → 签到。

	返回 (success, user_info_before, user_info_after) 与主流程格式一致。
	"""
	client = _make_client(use_proxy=use_proxy)

	try:
		# ---- 1. 预热 ----
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

		# ---- 2. 登录 ----
		# 对方协议要求提交 MD5 后的密码，非本地安全用途
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
			)
			login_data = login_resp.json() if login_resp.status_code == 200 else {}
			if login_data.get('code') != 0:
				err = login_data.get('msg', f'HTTP {login_resp.status_code}')
				log.failed(f'{account_name}: 登录失败 - {err}')
				return False, _login_failed_info(), None
		except Exception as e:
			log.failed(f'{account_name}: 登录请求失败: {e}')
			return False, _login_failed_info(), None

		log.detail(f'{account_name}: 登录成功')

		# ---- 3. 登录后查用户信息（含签到状态 + 积分）----
		info_before = _get_user_info(client)
		credits_before = _extract_credits(info_before)
		already_checked = bool(info_before.get('checkin')) if info_before else False

		log.detail(f'{account_name}: 签到前积分={credits_before}, 已签到={already_checked}')

		user_info_before = _credits_info(credits_before)

		if already_checked:
			log.success(f'{account_name}: 今日已签到')
			return True, user_info_before, dict(user_info_before)

		# ---- 4. 获取 register-config ----
		try:
			cfg_resp = request_with_retry(client, 'GET', f'{BASE}/api/user/register-config', timeout=30)
			cfg_data = cfg_resp.json() if cfg_resp.status_code == 200 else {}
			cfg = cfg_data.get('data', {})
			_e, _k, _n = cfg['_e'], cfg['_k'], cfg['_n']
		except Exception as e:
			log.failed(f'{account_name}: 获取注册配置失败: {e}')
			return False, user_info_before, None

		# ---- 5. 解密并提取签名参数 ----
		try:
			js_code = wa_decrypt(_e, _k)
		except Exception as e:
			log.failed(f'{account_name}: 解密失败: {e}')
			return False, user_info_before, None

		params = extract_sign_params(js_code)
		if not params:
			log.failed(f'{account_name}: 提取签名参数失败')
			return False, user_info_before, None
		py, mc = params

		# ---- 6. 生成 _jztz 并签到 ----
		fingerprint = build_fake_fingerprint(email)
		try:
			_jztz = generate_jztz(fingerprint, py, mc)
		except Exception as e:
			log.failed(f'{account_name}: 生成 _jztz 签名失败: {e}')
			return False, user_info_before, None

		try:
			checkin_resp = request_with_retry(
				client,
				'POST',
				f'{BASE}/api/user/checkin',
				json={'_k': _k, _n: _jztz},
				timeout=30,
			)
			checkin_data = checkin_resp.json() if checkin_resp.status_code == 200 else {}
			if checkin_data.get('code') != 0:
				err = checkin_data.get('msg', f'HTTP {checkin_resp.status_code}')
				log.failed(f'{account_name}: 签到失败 - {err}')
				return False, user_info_before, None
		except Exception as e:
			log.failed(f'{account_name}: 签到请求失败: {e}')
			return False, user_info_before, None

		log.success(f'{account_name}: 签到 API 请求成功')

		# ---- 7. 等待后端落库，再查积分 ----
		time.sleep(3)
		info_after = _get_user_info(client)
		credits_after = _extract_credits(info_after)
		log.info(f'{account_name}: 签到后积分={credits_after}')

		# ---- 8. 校验积分是否真正增加（反爬假成功检测）----
		if credits_before is not None and credits_after is not None:
			diff = credits_after - credits_before
			if diff > 0:
				log.success(f'{account_name}: 积分增加 {diff}，签到确认成功！')
			elif diff == 0:
				log.warn(f'{account_name}: 积分未变化（{credits_before}），可能为假成功')
			else:
				log.warn(f'{account_name}: 积分减少（{credits_before} -> {credits_after}）')

		return True, user_info_before, _credits_info(credits_after)

	finally:
		client.close()
