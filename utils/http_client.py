"""
统一的 HTTP 客户端工厂 — 所有签到分支共享的请求头、重试、代理配置。

用法:
  from utils.http_client import create_client, request_with_retry
  client = create_client(use_proxy=provider_config.use_proxy)
  resp = request_with_retry(client, 'GET', 'https://...')
  client.close()
"""

import os
import random
import time
import urllib.parse

import httpx

from utils.debug import log
from utils.proxy import get_proxy_server, redact_proxy_url
from utils.proxy_selector import current_proxy_node

# 所有签到分支统一的 UA（Windows Chrome）
DEFAULT_UA = (
	'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
)

# 通用 API 请求头（含 sec-ch-ua / sec-fetch 等反爬头）
# 注意：UA 与 sec-ch-ua* 必须保持同一平台/版本，否则不一致本身就是反爬指纹特征。
API_HEADERS = {
	'User-Agent': DEFAULT_UA,
	'Accept': 'application/json, text/plain, */*',
	'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
	'Accept-Encoding': 'gzip, deflate, br, zstd',
	'Content-Type': 'application/json',
	'sec-ch-ua': '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
	'sec-ch-ua-mobile': '?0',
	'sec-ch-ua-platform': '"Windows"',
	'sec-fetch-site': 'same-origin',
	'sec-fetch-mode': 'cors',
	'sec-fetch-dest': 'empty',
	'DNT': '1',
	'Priority': 'u=1, i',
}

# NewAPI 通用余额换算（quota / 500000 = 美元）
QUOTA_PER_DOLLAR = 500000

DEFAULT_RETRY_TIMES = 3
_RETRYABLE_STATUS = (429, 500, 502, 503, 504)
_RETRYABLE_EXCEPTIONS = (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError)

# 可能携带敏感凭据的查询参数，日志脱敏时隐藏其值
_SENSITIVE_QUERY_PARAMS = {'code', 'state', 'token', 'access_token', 'user_session', '_k'}


def _redact_url(url: str) -> str:
	"""对 URL 中的敏感查询参数值打码，防止 OAuth code / token 等进入日志。"""
	try:
		parts = urllib.parse.urlsplit(url)
		if not parts.query:
			return url
		params = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
		redacted = [(k, '<redacted>') if k.lower() in _SENSITIVE_QUERY_PARAMS else (k, v) for k, v in params]
		return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(redacted)))
	except Exception:
		return url


class RetryExhaustedError(RuntimeError):
	"""可重试 HTTP 状态持续失败。"""


def get_retry_times() -> int:
	"""重试次数，由 RETRY_TIMES 环境变量控制，默认 3。

	运行时读取而非模块级常量，确保 load_dotenv() 之后的 .env 配置也能生效。
	"""
	raw = os.getenv('RETRY_TIMES', '').strip()
	if not raw:
		return DEFAULT_RETRY_TIMES
	try:
		return max(0, int(raw))
	except ValueError:
		log.warn(f'RETRY_TIMES 值无效: {raw!r}，使用默认值 {DEFAULT_RETRY_TIMES}')
		return DEFAULT_RETRY_TIMES


def request_with_retry(
	client: httpx.Client,
	method: str,
	url: str,
	*,
	max_retries: int | None = None,
	**kwargs,
) -> httpx.Response:
	"""带指数退避重试的 HTTP 请求。

	对 5xx、429 状态码及网络异常（超时/连接断开/网络错误）自动重试。
	4xx 客户端错误直接返回，不重试。

	参数:
	  client: httpx 客户端实例
	  method: 请求方法，如 'GET'、'POST'
	  url: 请求地址
	  max_retries: 最大重试次数，默认使用 RETRY_TIMES 环境变量（默认 3）
	  **kwargs: 透传给 client.request 的参数
	"""
	retries = max_retries if max_retries is not None else get_retry_times()
	last_error: str | Exception | None = None

	for attempt in range(retries + 1):
		try:
			response = client.request(method, url, **kwargs)
			# 4xx 及 2xx/3xx 直接返回，不重试
			if response.status_code not in _RETRYABLE_STATUS:
				return response
			last_error = f'HTTP {response.status_code}'
			if attempt >= retries:
				break
			wait = (2**attempt) + random.uniform(0, 1)  # nosec B311 - 仅用于重试退避抖动，非安全用途
			log.debug(
				f'请求 {_redact_url(url)} 返回 {response.status_code}，{wait:.1f}s 后重试（第 {attempt + 1}/{retries} 次）'
			)
			time.sleep(wait)
		except _RETRYABLE_EXCEPTIONS as e:
			last_error = e
			if attempt >= retries:
				break
			wait = (2**attempt) + random.uniform(0, 1)  # nosec B311 - 仅用于重试退避抖动，非安全用途
			log.debug(f'请求 {_redact_url(url)} 异常: {e}，{wait:.1f}s 后重试（第 {attempt + 1}/{retries} 次）')
			time.sleep(wait)

	if isinstance(last_error, Exception):
		raise last_error
	raise RetryExhaustedError(f'请求 {_redact_url(url)} 返回 {last_error}（已重试 {retries} 次）')


def create_client(
	*,
	headers: dict | None = None,
	timeout: float = 30.0,
	http2: bool = True,
	use_proxy: bool = False,
) -> httpx.Client:
	"""创建统一的 httpx 客户端。

	参数:
	  headers:   要合并的额外请求头（会覆盖 API_HEADERS 中的同名项）
	  timeout:   超时秒数
	  http2:     是否启用 HTTP/2
	  use_proxy: 提供商是否需要代理。仅当为 True、CHECKIN_PROXY_URL 已设置
	             且连通性测试通过时才走代理，否则直连。
	"""
	kwargs: dict = {'http2': http2, 'timeout': timeout, 'trust_env': False}
	proxy_url = get_proxy_server(use_proxy=use_proxy)
	if proxy_url:
		kwargs['proxy'] = proxy_url
		node = current_proxy_node()
		node_info = f'（节点 {node}）' if node else ''
		log.info(f'HTTP 客户端代理已启用: {redact_proxy_url(proxy_url)}{node_info}')
	client = httpx.Client(**kwargs)
	base_headers = dict(API_HEADERS)
	if headers:
		base_headers.update(headers)
	client.headers.update(base_headers)
	return client
