"""
签到中枢 — 各签到分支共享的流程编排、幂等判定、余额换算与用户信息构造

架构约定（重要）：
  * 本模块是"中枢"，只依赖 utils.debug / utils.http_client，**严禁反向导入**
    checkin.py 或任何签到分支（utils/gptgod.py、utils/guyscode.py、
    utils/newapi_jwt.py、utils/newapi_session.py），否则形成循环导入。
  * 签到分支只保留差异逻辑（认证方式、余额字段、签到协议），标准流程
    （认证 → 前余额 → 签到 → 落库等待 → 后余额）统一由 run_standard_checkin 编排。
  * new-api 系（jwt/session 两分支）的共享协议（登录、self 解析、签到响应
    解析）也收敛在本模块；core 不创建/持有 client，请求复用调用方传入的 client。
  * time.sleep 必须经模块属性调用（time.sleep(...)），测试通过 patch
    time 模块属性消除等待；写成 from time import sleep 会绕开 patch。

统一用户信息 dict 契约（与主流程兼容）：
  * 分支形态（带 unit）：{'success', 'quota', 'used_quota', 'unit', 'display'}
  * 主流程 get_user_info 旧形态（无 unit）：{'success', 'quota', 'used_quota', 'display'}
  * display 用 f-string 原样输出（如 '$100.0'）；通知层的定点格式（'$100.00'）
    由 format_amount 单独负责。两套格式是既有契约，分别服务过程日志与结果通知，勿合并。
"""

import time
from collections.abc import Callable

from utils.debug import log
from utils.http_client import QUOTA_PER_DOLLAR, request_with_retry

# 幂等判定：签到接口返回"今日已签到"时视为成功（不发失败通知）。
# 统一关键词表是全项目唯一事实来源（主流程 execute_check_in 与各分支共用）。
ALREADY_CHECKED_KEYWORDS = (
	'already checked',
	'already signed',
	'already claimed',
	'已经签到',
	'已签到',
	'重复签到',
	'请勿重复签到',
)

# 余额单位 → 货币符号（credits 无符号，按积分渲染）
_UNIT_SYMBOL = {'usd': '$', 'cny': '¥'}


def is_already_checked(message: object) -> bool:
	"""判定签到失败消息是否属于"今日已签到"（幂等，视为成功）。"""
	if not message:
		return False
	text = str(message).lower()
	return any(keyword in text for keyword in ALREADY_CHECKED_KEYWORDS)


def quota_to_currency(quota: object, per_unit: float = QUOTA_PER_DOLLAR, rate: float = 1.0) -> float:
	"""quota 原始值 → 货币金额（new-api 系：quota/500000，CNY 站再乘 usd_exchange_rate）。

	注意保持"先除后乘再 round"的顺序，与既有实现同一浮点路径，避免尾数抖动。
	"""
	return round(float(quota or 0) / per_unit * rate, 2)


def format_amount(value: float, unit: str) -> str:
	"""按单位渲染金额（通知层定点格式）：美元 $、人民币 ¥、积分整数后缀。"""
	if unit == 'credits':
		return f'{value:g} 积分'
	symbol = _UNIT_SYMBOL.get(unit, '$')
	return f'{symbol}{value:.2f}'


def build_user_info(quota: object, used: object, unit: str | None = None) -> dict:
	"""构造统一用户信息 dict。

	unit=None → 主流程 get_user_info 旧形态（不含 unit 键）；
	unit='usd'/'cny'/'credits' → 分支形态（含 unit 键）。
	quota/used 原样透传（不强制 float，gptgod 积分为 int）。
	"""
	info: dict = {'success': True, 'quota': quota, 'used_quota': used}
	if unit == 'credits':
		info['display'] = f'💰 当前积分: {quota}'
	else:
		symbol = _UNIT_SYMBOL.get(unit or 'usd', '$')
		info['display'] = f'💰 当前余额: {symbol}{quota}, 已用: {symbol}{used}'
	if unit:
		info['unit'] = unit
	return info


def failed_info(error: str, unit: str = 'usd') -> dict:
	"""查询/签到失败的统一占位信息。

	各分支的 fetch 闭包负责把原始失败（None / 非 JSON / 非 success 响应）包装成
	本结构，错误文案由分支给出。
	"""
	return {'success': False, 'quota': 0, 'used_quota': 0, 'unit': unit, 'error': error}


def login_failed_info(unit: str = 'usd') -> dict:
	"""登录失败的统一占位信息。"""
	return failed_info('Login failed', unit)


def newapi_login(client, domain: str, email: str, password: str, account_name: str) -> dict | None:
	"""new-api 系通用登录：POST /api/user/login，body {username, password}。

	jwt 分支（newapi_jwt）与 session 分支（newapi_session）登录协议完全一致，
	仅提取字段不同（access_token vs data.id），故请求与错误处理收敛于此。
	成功返回 data payload dict（字段提取留在分支），失败返回 None（日志已输出）。
	请求复用调用方传入的 client（session 分支靠它种 cookie）。
	"""
	try:
		resp = request_with_retry(
			client,
			'POST',
			f'{domain}/api/user/login',
			json={'username': email, 'password': password},
			timeout=30,
		)
	except Exception as e:
		log.warn(f'{account_name}: 登录异常: {e}')
		return None
	try:
		data = resp.json()
	except Exception:  # nosec B112
		log.failed(f'{account_name}: 登录响应非 JSON（HTTP {resp.status_code}）')
		return None
	if not isinstance(data, dict) or not data.get('success'):
		msg = data.get('message', f'HTTP {resp.status_code}') if isinstance(data, dict) else f'HTTP {resp.status_code}'
		log.failed(f'{account_name}: 登录失败 - {msg}')
		return None
	payload = data.get('data')
	return payload if isinstance(payload, dict) else {}


def parse_checkin_response(resp) -> tuple[bool, str | None]:
	"""解析 new-api 系 {success, message} 风格的签到响应 → (ok, message)。

	message 同时用于幂等判定（is_already_checked）与失败日志，
	分支自行决定返回解析后的 message 还是原始文本片段。
	"""
	try:
		data = resp.json()
	except Exception:  # nosec B112
		data = {}
	if not isinstance(data, dict):
		return False, f'HTTP {resp.status_code}'
	if data.get('success'):
		return True, None
	message = data.get('message') or f'HTTP {resp.status_code}'
	return False, message


def newapi_self_to_info(resp, *, unit: str = 'usd', rate: float = 1.0) -> dict:
	"""解析 new-api 系 GET /api/user/self 响应 → 统一信息 dict。

	quota/used_quota ÷500000（CNY 站再乘汇率），单位由调用方指定。
	"""
	try:
		data = resp.json()
	except Exception:  # nosec B112
		return failed_info(f'获取用户信息失败: 响应非 JSON (HTTP {resp.status_code})', unit)
	if not isinstance(data, dict) or not data.get('success'):
		return failed_info(f'获取用户信息失败: HTTP {resp.status_code}', unit)
	d = data.get('data') or {}
	return build_user_info(
		quota_to_currency(d.get('quota', 0), rate=rate),
		quota_to_currency(d.get('used_quota', 0), rate=rate),
		unit,
	)


def run_standard_checkin(
	account_name: str,
	*,
	authenticate: Callable[[], bool],
	fetch_user_info: Callable[[], dict],
	perform_checkin: Callable[[], tuple[bool, str | None]],
	already_checked_via_info: Callable[[dict], bool] | None = None,
	post_checkin: Callable[[dict, dict], None] | None = None,
	unit: str = 'usd',
	success_detail: str | None = '签到成功',
	wait_seconds: float = 3.0,
) -> tuple[bool, dict | None, dict | None]:
	"""标准纯 API 签到流程编排（模板）。

	流程: authenticate → 前余额 → [信息级已签到短路] → 签到(幂等判定)
	      → 落库等待 → 后余额 → [post_checkin 校验钩子]

	钩子契约:
	  authenticate()    → bool；False 视为登录失败（含分支自有的降级链路）
	  fetch_user_info() → 统一信息 dict；success=False 时流程中断（登录失败信息除外，
	                      gptgod 这类"信息查询永远成功"的分支由其闭包自行包装）
	  perform_checkin() → (ok, message)；message 同时用于幂等判定与失败日志；
	                      抛出的异常统一按"签到请求失败"处理
	  already_checked_via_info(before) → True 则跳过签到请求（gptgod 的 checkin 预判）
	  post_checkin(before, after)      → 签到成功后的校验（gptgod 积分防假成功）

	异常兜底：所有钩子的异常均由本函数捕获，不会穿透到主流程把
	"已成功签到"误记为处理异常（各分支闭包目前也各自捕获，此处是结构性保证）。
	perform_checkin 的异常类型会被抹平（含网络异常）：当前分支均 use_proxy=False
	不参与节点切换重试；未来分支若需把网络异常转译为节点问题，应在自己的
	perform_checkin 内捕获并按协议语义返回 (False, message)。

	返回 (success, user_info_before, user_info_after) 与主流程格式一致。
	"""
	try:
		authed = authenticate()
	except Exception as e:  # nosec B112
		log.failed(f'{account_name}: 认证异常: {e}')
		authed = False
	if not authed:
		return False, login_failed_info(unit), None

	def _safe_fetch() -> dict:
		"""余额查询兜底：分支闭包漏接异常时按"查询失败"处理。"""
		try:
			return fetch_user_info()
		except Exception as e:  # nosec B112
			return failed_info(f'获取用户信息失败: {str(e)[:50]}...', unit)

	info_before = _safe_fetch()
	if not info_before.get('success'):
		log.failed(f'{account_name}: {info_before.get("error", "未知错误")}')
		return False, info_before, None
	log.info(f'{account_name}: {info_before["display"]}')

	if already_checked_via_info is not None:
		try:
			already = already_checked_via_info(info_before)
		except Exception as e:  # nosec B112
			log.warn(f'{account_name}: 已签到预判异常，继续发起签到: {e}')
			already = False
		if already:
			log.detail(f'{account_name}: 今日已签到')
			return True, info_before, dict(info_before)

	try:
		ok, message = perform_checkin()
	except Exception as e:
		log.failed(f'{account_name}: 签到请求失败: {e}')
		return False, info_before, None

	if ok:
		if success_detail:
			log.detail(f'{account_name}: {success_detail}')
	elif is_already_checked(message):
		log.detail(f'{account_name}: 今日已签到（重复打卡），视为成功')
	else:
		log.failed(f'{account_name}: 签到失败 - {message}')
		return False, info_before, None

	time.sleep(wait_seconds)

	info_after = _safe_fetch()
	if not info_after.get('success'):
		# 签到已成功，后余额查不到不能反转为失败；沿用前值并明示
		log.warn(f'{account_name}: 签到后余额查询失败，沿用签到前数值')
		info_after = info_before
	log.info(f'{account_name}: 签到后: {info_after["display"]}')

	if post_checkin is not None:
		try:
			post_checkin(info_before, info_after)
		except Exception as e:  # nosec B112
			log.warn(f'{account_name}: 签到后校验异常: {e}')

	return True, info_before, info_after
