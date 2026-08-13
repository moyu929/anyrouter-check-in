"""scripts/convert_subscribe.py 订阅转换测试（纯离线，不访问网络）。"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS_DIR))

import convert_subscribe  # noqa: E402


def _b64(s: str) -> str:
	"""将文本编码为 base64（v2rayN 订阅使用）。"""
	return base64.b64encode(s.encode('utf-8')).decode('ascii')


def _vmess_link(name: str = '测试节点', server: str = '1.2.3.4', port: int = 443, **extra) -> str:
	info = {
		'v': '2',
		'ps': name,
		'add': server,
		'port': str(port),
		'id': 'f5c22b43-c7ac-49b6-a322-c59d45de9a9b',
		'aid': '0',
		'scy': 'auto',
		'net': 'tcp',
		'type': 'none',
		'host': '',
		'path': '',
		'tls': '',
	}
	info.update(extra)
	return 'vmess://' + _b64(json.dumps(info, ensure_ascii=False))


class TestParseLine:
	def test_vmess_tcp(self):
		p = convert_subscribe.parse_line(_vmess_link())
		assert p['type'] == 'vmess'
		assert p['server'] == '1.2.3.4'
		assert p['port'] == 443
		assert p['uuid'] == 'f5c22b43-c7ac-49b6-a322-c59d45de9a9b'
		assert p['cipher'] == 'auto'

	def test_vmess_ws_tls(self):
		p = convert_subscribe.parse_line(
			_vmess_link(net='ws', path='/abc', host='h.com', tls='tls', sni='h.com', alpn='h2')
		)
		assert p['network'] == 'ws'
		assert p['ws-opts']['path'] == '/abc'
		assert p['ws-opts']['headers']['Host'] == 'h.com'
		assert p['tls'] is True
		assert p['sni'] == 'h.com'
		assert p['alpn'] == ['h2']

	def test_vmess_name_defaults(self):
		p = convert_subscribe.parse_line(_vmess_link(name='  '))
		assert p['name']

	def test_vless(self):
		link = (
			'vless://uuid@example.com:443?encryption=none&security=tls&sni=ex.com&type=ws&path=%2Fws&host=ex.com#VL节点'
		)
		p = convert_subscribe.parse_line(link)
		assert p['type'] == 'vless'
		assert p['server'] == 'example.com'
		assert p['port'] == 443
		assert p['uuid'] == 'uuid'
		assert p['tls'] is True
		assert p['sni'] == 'ex.com'
		assert p['network'] == 'ws'
		assert p['name'] == 'VL节点'

	def test_trojan(self):
		p = convert_subscribe.parse_line('trojan://pass@example.com:443?sni=ex.com#T节点')
		assert p['type'] == 'trojan'
		assert p['server'] == 'example.com'
		assert p['password'] == 'pass'
		assert p['sni'] == 'ex.com'
		assert p['tls'] is True

	def test_ss_plain(self):
		p = convert_subscribe.parse_line('ss://aes-128-gcm:pass@1.2.3.4:8388#SS')
		assert p['type'] == 'ss'
		assert p['cipher'] == 'aes-128-gcm'
		assert p['password'] == 'pass'
		assert p['server'] == '1.2.3.4'
		assert p['port'] == 8388

	def test_ss_base64_userinfo(self):
		link = 'ss://' + _b64('aes-128-gcm:pass@1.2.3.4:8388') + '#SS2'
		p = convert_subscribe.parse_line(link)
		assert p['server'] == '1.2.3.4'
		assert p['cipher'] == 'aes-128-gcm'

	def test_unknown_and_empty(self):
		assert convert_subscribe.parse_line('') is None
		assert convert_subscribe.parse_line('# 注释') is None
		assert convert_subscribe.parse_line('socks5://x') is None
		assert convert_subscribe.parse_line('vmess://%%%not-json%%%') is None


class TestParseSubscription:
	def test_filters_info_nodes(self):
		content = _b64(
			'\n'.join(
				[
					_vmess_link('剩余流量：272.84 GB'),
					_vmess_link('套餐到期：长期有效'),
					_vmess_link('过滤掉15条线路'),
					_vmess_link('日本-优化'),
				]
			)
		)
		proxies = convert_subscribe.parse_subscription(content)
		assert [p['name'] for p in proxies] == ['日本-优化']

	def test_dedup_names(self):
		content = _b64('\n'.join([_vmess_link('香港-优化'), _vmess_link('香港-优化')]))
		assert len(convert_subscribe.parse_subscription(content)) == 1

	def test_clash_yaml(self):
		content = 'proxies:\n  - name: "日本-优化"\n    type: vmess\n    server: 1.2.3.4\n'
		proxies = convert_subscribe.parse_subscription(content)
		assert len(proxies) == 1
		assert proxies[0]['name'] == '日本-优化'
		assert '_raw' in proxies[0]


class TestBuildConfig:
	def test_structure_and_dynamic_values(self):
		config = convert_subscribe.build_config(
			[{'name': '日本-优化', 'type': 'vmess', 'server': '1.2.3.4', 'port': 443}],
			port=17890,
			controller_port=19090,
			secret='sec123',
		)
		assert 'mixed-port: 17890' in config
		assert 'external-controller: 127.0.0.1:19090' in config
		assert 'secret: "sec123"' in config
		assert '\nproxies:\n  - name: "日本-优化"' in config
		assert '"🇯🇵 日本"' in config
		assert '"AUTO"' in config
		assert 'rules:\n  - MATCH,AUTO' in config

	def test_region_grouping(self):
		proxies = [
			{'name': '日本-优化', 'type': 'vmess', 'server': 'a', 'port': 1},
			{'name': '新加坡-优化', 'type': 'vmess', 'server': 'b', 'port': 2},
			{'name': '香港-优化', 'type': 'vmess', 'server': 'c', 'port': 3},
			{'name': '台湾-优化', 'type': 'vmess', 'server': 'd', 'port': 4},
		]
		config = convert_subscribe.build_config(proxies, port=7890, controller_port=9090, secret='')
		jp = config.split('"🇯🇵 日本"')[1].split('proxy-groups')[0].split('proxies:')[1]
		assert '日本-优化' in jp and '台湾-优化' not in jp
		sg = config.split('"🇸🇬 新加坡"')[1].split('proxy-groups')[0].split('proxies:')[1]
		assert '新加坡-优化' in sg
		hk = config.split('"🇭🇰 香港"')[1].split('proxy-groups')[0].split('proxies:')[1]
		assert '香港-优化' in hk
		# 台湾未匹配任何区域组 → 不进入任何组
		for region in ('🇯🇵 日本', '🇸🇬 新加坡', '🇭🇰 香港'):
			block = config.split(f'"{region}"')[1].split('proxy-groups')[0].split('proxies:')[1]
			assert '台湾-优化' not in block


class TestMain:
	def test_writes_config(self, tmp_path):
		sub = tmp_path / 'sub.raw'
		sub.write_text(_b64(_vmess_link('日本-优化')), encoding='ascii')
		out = tmp_path / 'config.yaml'
		assert convert_subscribe.main([str(sub), str(out)]) == 0
		content = out.read_text(encoding='utf-8')
		assert '日本-优化' in content
		assert 'proxy-groups:' in content

	def test_bad_args(self):
		assert convert_subscribe.main([]) == 2
		assert convert_subscribe.main(['only-one']) == 2

	def test_unparseable(self, tmp_path):
		bad = tmp_path / 'bad.raw'
		bad.write_text('not a subscription', encoding='utf-8')
		out = tmp_path / 'config.yaml'
		assert convert_subscribe.main([str(bad), str(out)]) == 1

	def test_missing_file(self, tmp_path):
		assert convert_subscribe.main([str(tmp_path / 'nope'), str(tmp_path / 'c.yaml')]) == 1

	def test_env_port_injection(self, tmp_path, monkeypatch):
		sub = tmp_path / 'sub.raw'
		sub.write_text(_b64(_vmess_link('日本-优化')), encoding='ascii')
		out = tmp_path / 'config.yaml'
		monkeypatch.setenv('PROXY_PORT', '12345')
		monkeypatch.setenv('MIHOMO_CONTROLLER_PORT', '23456')
		monkeypatch.setenv('PROXY_SECRET', 'sek')
		assert convert_subscribe.main([str(sub), str(out)]) == 0
		content = out.read_text(encoding='utf-8')
		assert 'mixed-port: 12345' in content
		assert '127.0.0.1:23456' in content
		assert 'secret: "sek"' in content


class TestSubprocess:
	def test_cli(self, tmp_path):
		sub = tmp_path / 'sub.raw'
		sub.write_text(_b64(_vmess_link('日本-优化')), encoding='ascii')
		out_path = tmp_path / 'config.yaml'
		env = dict(os.environ)
		env['PROXY_SECRET'] = 'cli-secret'
		result = subprocess.run(
			[sys.executable, str(SCRIPTS_DIR / 'convert_subscribe.py'), str(sub), str(out_path)],
			capture_output=True,
			text=True,
			env=env,
		)
		assert result.returncode == 0
		assert 'cli-secret' in out_path.read_text(encoding='utf-8')
