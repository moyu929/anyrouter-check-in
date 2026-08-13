import os
import socket
import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


NETWORK_ENV_VARS = (
	'ENABLE_REAL_TEST',
	'ANYROUTER_ACCOUNTS',
	'PROVIDERS',
	'CHECKIN_PROXY_URL',
	'MIHOMO_CONTROLLER',
	'MIHOMO_SECRET_FILE',
	'PROXY_SUBSCRIPTION_URL',
	'HTTP_PROXY',
	'HTTPS_PROXY',
	'ALL_PROXY',
	'http_proxy',
	'https_proxy',
	'all_proxy',
	'EMAIL_USER',
	'EMAIL_PASS',
	'EMAIL_TO',
	'PUSHPLUS_TOKEN',
	'SERVERPUSHKEY',
	'DINGDING_WEBHOOK',
	'FEISHU_WEBHOOK',
	'WEIXIN_WEBHOOK',
	'GOTIFY_URL',
	'GOTIFY_TOKEN',
	'TELEGRAM_BOT_TOKEN',
	'TELEGRAM_CHAT_ID',
	'BARK_KEY',
)

for env_name in NETWORK_ENV_VARS:
	os.environ.pop(env_name, None)

# 防止 checkin.py 在测试收集阶段通过 load_dotenv() 重新注入本地真实凭据。
os.environ['PYTHON_DOTENV_DISABLED'] = '1'


def pytest_addoption(parser):
	parser.addoption(
		'--allow-network',
		action='store_true',
		default=False,
		help='允许执行显式标记为 network 的集成测试',
	)


def pytest_configure(config):
	config.addinivalue_line('markers', 'network: 需要真实网络的显式集成测试')


@pytest.fixture(autouse=True)
def clean_network_environment(monkeypatch):
	"""每个用例恢复到无生产凭据、无系统代理的环境。"""
	for env_name in NETWORK_ENV_VARS:
		monkeypatch.delenv(env_name, raising=False)


@pytest.fixture(autouse=True)
def block_real_network(request, monkeypatch):
	"""默认禁止外部 socket 连接，同时允许 asyncio 所需的本机回环连接。"""
	if request.node.get_closest_marker('network'):
		if not request.config.getoption('--allow-network'):
			pytest.skip('真实网络测试默认禁用；显式传入 --allow-network 才可运行')
		return

	original_create_connection = socket.create_connection
	original_connect = socket.socket.connect
	original_connect_ex = socket.socket.connect_ex

	def is_loopback(address) -> bool:
		if not isinstance(address, tuple) or not address:
			return True
		return address[0] in {'127.0.0.1', '::1', 'localhost'}

	def guarded_create_connection(address, *args, **kwargs):
		if not is_loopback(address):
			raise RuntimeError(f'单元测试禁止真实网络连接: {address[0]}')
		return original_create_connection(address, *args, **kwargs)

	def guarded_connect(sock, address):
		if not is_loopback(address):
			raise RuntimeError(f'单元测试禁止真实网络连接: {address[0]}')
		return original_connect(sock, address)

	def guarded_connect_ex(sock, address):
		if not is_loopback(address):
			raise RuntimeError(f'单元测试禁止真实网络连接: {address[0]}')
		return original_connect_ex(sock, address)

	monkeypatch.setattr(socket, 'create_connection', guarded_create_connection)
	monkeypatch.setattr(socket.socket, 'connect', guarded_connect)
	monkeypatch.setattr(socket.socket, 'connect_ex', guarded_connect_ex)
