"""代理开关与 HTTP 客户端重试测试（全离线，用 MockTransport 拦截，不出网）。"""

import httpx
import pytest

from utils import proxy as proxy_module
from utils.http_client import (
	API_HEADERS,
	DEFAULT_RETRY_TIMES,
	create_client,
	get_retry_times,
	request_with_retry,
)
from utils.proxy import (
	get_playwright_proxy,
	get_proxy_server,
	get_proxy_test_url,
	is_proxy_configured,
	reset_proxy_cache,
)


@pytest.fixture(autouse=True)
def _clean_proxy_env(monkeypatch):
	"""每个用例前清空代理相关环境变量与连通性缓存。"""
	monkeypatch.delenv('CHECKIN_PROXY_URL', raising=False)
	monkeypatch.delenv('PROXY_TEST_URL', raising=False)
	monkeypatch.delenv('RETRY_TIMES', raising=False)
	reset_proxy_cache()
	yield
	reset_proxy_cache()


def _stub_proxy_test(monkeypatch, *, reachable: bool) -> list[str]:
	"""替换 _test_proxy，记录调用并返回预设结果，确保不发真实请求。"""
	calls: list[str] = []

	def fake_test(proxy_url: str) -> bool:
		calls.append(proxy_url)
		return reachable

	monkeypatch.setattr(proxy_module, '_test_proxy', fake_test)
	return calls


class TestProxySwitch:
	def test_use_proxy_false_returns_none_even_when_configured(self, monkeypatch):
		monkeypatch.setenv('CHECKIN_PROXY_URL', 'http://127.0.0.1:7890')
		calls = _stub_proxy_test(monkeypatch, reachable=True)

		assert get_proxy_server(use_proxy=False) is None
		# use_proxy=False 时不应触发连通性测试
		assert calls == []

	def test_use_proxy_true_returns_configured_proxy(self, monkeypatch):
		monkeypatch.setenv('CHECKIN_PROXY_URL', 'http://127.0.0.1:7890')
		_stub_proxy_test(monkeypatch, reachable=True)

		assert get_proxy_server(use_proxy=True) == 'http://127.0.0.1:7890'

	def test_returns_none_when_env_missing(self, monkeypatch):
		calls = _stub_proxy_test(monkeypatch, reachable=True)

		assert get_proxy_server(use_proxy=True) is None
		assert calls == []

	def test_blank_env_is_treated_as_unset(self, monkeypatch):
		monkeypatch.setenv('CHECKIN_PROXY_URL', '   ')

		assert get_proxy_server(use_proxy=True) is None

	def test_unreachable_proxy_falls_back_to_direct(self, monkeypatch):
		monkeypatch.setenv('CHECKIN_PROXY_URL', 'http://127.0.0.1:1')
		_stub_proxy_test(monkeypatch, reachable=False)

		assert get_proxy_server(use_proxy=True) is None

	def test_connectivity_result_is_cached_across_calls(self, monkeypatch):
		monkeypatch.setenv('CHECKIN_PROXY_URL', 'http://127.0.0.1:7890')
		calls = _stub_proxy_test(monkeypatch, reachable=True)

		get_proxy_server(use_proxy=True)
		get_proxy_server(use_proxy=True)
		get_proxy_server(use_proxy=True)

		assert len(calls) == 1

	def test_reset_cache_forces_retest(self, monkeypatch):
		monkeypatch.setenv('CHECKIN_PROXY_URL', 'http://127.0.0.1:7890')
		calls = _stub_proxy_test(monkeypatch, reachable=True)

		get_proxy_server(use_proxy=True)
		reset_proxy_cache()
		get_proxy_server(use_proxy=True)

		assert len(calls) == 2

	def test_playwright_proxy_shape(self, monkeypatch):
		monkeypatch.setenv('CHECKIN_PROXY_URL', 'http://127.0.0.1:7890')
		_stub_proxy_test(monkeypatch, reachable=True)

		assert get_playwright_proxy(use_proxy=True) == {'server': 'http://127.0.0.1:7890'}
		assert get_playwright_proxy(use_proxy=False) is None

	def test_is_proxy_configured_ignores_connectivity(self, monkeypatch):
		assert is_proxy_configured() is False

		monkeypatch.setenv('CHECKIN_PROXY_URL', 'http://127.0.0.1:7890')
		_stub_proxy_test(monkeypatch, reachable=False)

		assert is_proxy_configured() is True
		assert get_proxy_server(use_proxy=True) is None

	def test_proxy_test_url_can_be_overridden(self, monkeypatch):
		assert get_proxy_test_url() == 'https://www.gstatic.com/generate_204'

		monkeypatch.setenv('PROXY_TEST_URL', 'https://example.com/204')

		assert get_proxy_test_url() == 'https://example.com/204'


class TestCreateClient:
	def test_no_proxy_when_use_proxy_false(self, monkeypatch):
		monkeypatch.setenv('CHECKIN_PROXY_URL', 'http://127.0.0.1:7890')
		_stub_proxy_test(monkeypatch, reachable=True)

		with create_client(use_proxy=False) as client:
			assert client.headers['User-Agent'] == API_HEADERS['User-Agent']

	def test_default_headers_are_applied(self):
		with create_client() as client:
			assert client.headers['sec-ch-ua-platform'] == '"Windows"'
			assert client.headers['Accept-Language'] == API_HEADERS['Accept-Language']

	def test_extra_headers_override_defaults(self):
		with create_client(headers={'Referer': 'https://example.com', 'Accept': 'text/html'}) as client:
			assert client.headers['Referer'] == 'https://example.com'
			assert client.headers['Accept'] == 'text/html'
			# 未覆盖的默认头保持不变
			assert client.headers['sec-ch-ua-mobile'] == '?0'


class TestGetRetryTimes:
	def test_default_when_unset(self):
		assert get_retry_times() == DEFAULT_RETRY_TIMES

	def test_reads_env_at_call_time(self, monkeypatch):
		monkeypatch.setenv('RETRY_TIMES', '5')

		assert get_retry_times() == 5

	def test_negative_is_clamped_to_zero(self, monkeypatch):
		monkeypatch.setenv('RETRY_TIMES', '-3')

		assert get_retry_times() == 0

	def test_invalid_value_falls_back_to_default(self, monkeypatch):
		monkeypatch.setenv('RETRY_TIMES', 'abc')

		assert get_retry_times() == DEFAULT_RETRY_TIMES

	def test_blank_value_falls_back_to_default(self, monkeypatch):
		monkeypatch.setenv('RETRY_TIMES', '   ')

		assert get_retry_times() == DEFAULT_RETRY_TIMES


class TestRequestWithRetry:
	@staticmethod
	def _client(handler) -> httpx.Client:
		return httpx.Client(transport=httpx.MockTransport(handler))

	def test_returns_first_success_without_retry(self, monkeypatch):
		monkeypatch.setattr('utils.http_client.time.sleep', lambda _s: None)
		calls = []

		def handler(request: httpx.Request) -> httpx.Response:
			calls.append(request.url.path)
			return httpx.Response(200, json={'ok': True})

		with self._client(handler) as client:
			resp = request_with_retry(client, 'GET', 'https://example.com/api')

		assert resp.status_code == 200
		assert len(calls) == 1

	def test_retries_on_503_then_succeeds(self, monkeypatch):
		monkeypatch.setattr('utils.http_client.time.sleep', lambda _s: None)
		statuses = [503, 502, 200]
		calls = []

		def handler(request: httpx.Request) -> httpx.Response:
			calls.append(request.url.path)
			return httpx.Response(statuses[len(calls) - 1])

		with self._client(handler) as client:
			resp = request_with_retry(client, 'GET', 'https://example.com/api', max_retries=3)

		assert resp.status_code == 200
		assert len(calls) == 3

	def test_does_not_retry_on_404(self, monkeypatch):
		monkeypatch.setattr('utils.http_client.time.sleep', lambda _s: None)
		calls = []

		def handler(request: httpx.Request) -> httpx.Response:
			calls.append(request.url.path)
			return httpx.Response(404)

		with self._client(handler) as client:
			resp = request_with_retry(client, 'GET', 'https://example.com/api', max_retries=3)

		assert resp.status_code == 404
		assert len(calls) == 1

	def test_raises_runtime_error_after_exhausting_retries(self, monkeypatch):
		monkeypatch.setattr('utils.http_client.time.sleep', lambda _s: None)
		calls = []

		def handler(request: httpx.Request) -> httpx.Response:
			calls.append(request.url.path)
			return httpx.Response(429)

		with self._client(handler) as client, pytest.raises(RuntimeError, match='429'):
			request_with_retry(client, 'GET', 'https://example.com/api', max_retries=2)

		# 首次 + 2 次重试
		assert len(calls) == 3

	def test_retries_on_timeout_then_succeeds(self, monkeypatch):
		monkeypatch.setattr('utils.http_client.time.sleep', lambda _s: None)
		calls = []

		def handler(request: httpx.Request) -> httpx.Response:
			calls.append(request.url.path)
			if len(calls) == 1:
				raise httpx.ConnectTimeout('boom', request=request)
			return httpx.Response(200)

		with self._client(handler) as client:
			resp = request_with_retry(client, 'GET', 'https://example.com/api', max_retries=2)

		assert resp.status_code == 200
		assert len(calls) == 2

	def test_reraises_network_error_after_exhausting_retries(self, monkeypatch):
		monkeypatch.setattr('utils.http_client.time.sleep', lambda _s: None)
		calls = []

		def handler(request: httpx.Request) -> httpx.Response:
			calls.append(request.url.path)
			raise httpx.ConnectError('refused', request=request)

		with self._client(handler) as client, pytest.raises(httpx.ConnectError):
			request_with_retry(client, 'GET', 'https://example.com/api', max_retries=1)

		assert len(calls) == 2

	def test_max_retries_zero_disables_retry(self, monkeypatch):
		monkeypatch.setattr('utils.http_client.time.sleep', lambda _s: None)
		calls = []

		def handler(request: httpx.Request) -> httpx.Response:
			calls.append(request.url.path)
			return httpx.Response(500)

		with self._client(handler) as client, pytest.raises(RuntimeError):
			request_with_retry(client, 'GET', 'https://example.com/api', max_retries=0)

		assert len(calls) == 1

	def test_retry_times_env_is_honored(self, monkeypatch):
		monkeypatch.setattr('utils.http_client.time.sleep', lambda _s: None)
		monkeypatch.setenv('RETRY_TIMES', '1')
		calls = []

		def handler(request: httpx.Request) -> httpx.Response:
			calls.append(request.url.path)
			return httpx.Response(500)

		with self._client(handler) as client, pytest.raises(RuntimeError):
			request_with_retry(client, 'GET', 'https://example.com/api')

		assert len(calls) == 2

	def test_kwargs_are_forwarded_to_request(self, monkeypatch):
		monkeypatch.setattr('utils.http_client.time.sleep', lambda _s: None)
		seen: dict = {}

		def handler(request: httpx.Request) -> httpx.Response:
			seen['body'] = request.content
			seen['header'] = request.headers.get('X-Test')
			return httpx.Response(200)

		with self._client(handler) as client:
			request_with_retry(
				client,
				'POST',
				'https://example.com/api',
				json={'a': 1},
				headers={'X-Test': 'yes'},
			)

		assert seen['body'] == b'{"a":1}'
		assert seen['header'] == 'yes'
