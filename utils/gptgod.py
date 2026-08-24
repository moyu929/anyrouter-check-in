"""
GPTGod 纯 API 签到模块 — _jztz 签名算法实现

本分支仅保留差异逻辑（_jztz 加密签名协议、积分单位），标准流程
（登录 → 前积分 → 签到 → 后积分）由 utils.checkin_core.run_standard_checkin 编排。

签到协议:
  1. POST /api/user/login 登录拿 cookies（密码 MD5）
  2. GET /api/user/info 查积分与签到状态（checkin 字段预判幂等）
  3. GET /api/user/register-config 拿 {_e, _k, _n}
  4. wa(_e, _k) 解密 _e 得到签名函数的 JS 代码
  5. 从 JS 代码提取 py（重排表）和 mc（XOR 密钥）
  6. 构造伪造的 33 项行为指纹数组，生成 _jztz
  7. POST /api/user/checkin body={_k, _jztz}
"""

import base64
import hashlib
import json
import random
import re
import time
from urllib.parse import unquote

import httpx

from utils.checkin_core import build_user_info, login_failed_info, run_standard_checkin
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
	"""构造与主流程兼容的用户信息字典（GPTGod 余额单位是积分，unit 供通知层区分渲染）。"""
	return build_user_info(credits_value or 0, 0, 'credits')


def _login_failed_info() -> dict:
	"""登录失败时的占位用户信息。"""
	return login_failed_info('credits')


def gptgod_checkin(
	account_name: str,
	email: str,
	password: str,
	use_proxy: bool = False,
) -> tuple[bool, dict | None, dict | None]:
	"""GPTGod 纯 API 签到：登录 → 获取配置 → 生成 _jztz → 签到（流程见 checkin_core）。

	返回 (success, user_info_before, user_info_after) 与主流程格式一致。
	"""
	client = _make_client(use_proxy=use_proxy)
	try:
		# 状态记录：首查/次查积分与原始 info（None 表示查询失败，防假成功校验需区分）
		state = {'calls': 0, 'credits': [None, None], 'raw': [None, None]}

		def authenticate() -> bool:
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

		def fetch_user_info() -> dict:
			"""查积分与签到状态；查询失败时仍返回成功形态（quota=0），不阻塞签到。"""
			info = _get_user_info(client)
			credits = _extract_credits(info)
			idx = min(state['calls'], 1)
			state['raw'][idx] = info
			state['credits'][idx] = credits
			state['calls'] += 1
			if idx == 0:
				checked = bool(info.get('checkin')) if info else False
				log.detail(f'{account_name}: 签到前积分={credits}, 已签到={checked}')
			else:
				log.detail(f'{account_name}: 签到后积分={credits}')
			return _credits_info(credits)

		def already_checked_via_info(_before: dict) -> bool:
			"""GPTGod 幂等预判：用户信息自带签到状态，已签到则跳过签到请求。"""
			first = state['raw'][0]
			return bool(first.get('checkin')) if first else False

		def perform_checkin() -> tuple[bool, str | None]:
			"""register-config → 解密 → 提取参数 → 指纹 → 签名 → 签到（gptgod 专有 _jztz 协议）。"""
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
			)
			checkin_data = checkin_resp.json() if checkin_resp.status_code == 200 else {}
			if checkin_data.get('code') != 0:
				return False, checkin_data.get('msg', f'HTTP {checkin_resp.status_code}')
			return True, None

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
