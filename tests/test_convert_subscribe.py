"""scripts/convert_subscribe.py 订阅转换测试（纯离线，不访问网络）。"""

from __future__ import annotations

import base64
import json
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


class TestMain:
	def test_v2rayn_subscription(self, tmp_path, capsys):
		sub = tmp_path / 'sub.raw'
		sub.write_text(_b64('\n'.join([_vmess_link('A'), _vmess_link('B')])), encoding='ascii')
		code = convert_subscribe.main([str(sub)])
		out = capsys.readouterr().out
		assert code == 0
		assert out.count('  - name:') == 2
		assert out.startswith('proxies:')

	def test_clash_yaml_passthrough(self, tmp_path, capsys):
		y = tmp_path / 'sub.yaml'
		y.write_text('proxies:\n  - name: "x"\n    type: ss\n', encoding='utf-8')
		code = convert_subscribe.main([str(y)])
		assert code == 0
		assert capsys.readouterr().out.startswith('proxies:')

	def test_unparseable_returns_error(self, tmp_path):
		bad = tmp_path / 'bad.raw'
		bad.write_text('not a subscription', encoding='utf-8')
		assert convert_subscribe.main([str(bad)]) == 1

	def test_missing_file_returns_error(self, tmp_path):
		assert convert_subscribe.main([str(tmp_path / 'nope')]) == 1

	def test_stdin_input(self, tmp_path, monkeypatch, capsys):
		raw = _b64(_vmess_link('STDIN节点'))
		monkeypatch.setattr(sys, 'stdin', _FakeStdin(raw))
		assert convert_subscribe.main([]) == 0
		assert 'STDIN节点' in capsys.readouterr().out


class _FakeStdin:
	def __init__(self, text: str):
		self._text = text

	def read(self) -> str:
		return self._text


class TestSubprocess:
	def test_cli(self, tmp_path):
		sub = tmp_path / 'sub.raw'
		sub.write_text(_b64(_vmess_link('CLI节点')), encoding='ascii')
		out = subprocess.run(
			[sys.executable, str(SCRIPTS_DIR / 'convert_subscribe.py'), str(sub)],
			capture_output=True,
			text=True,
		)
		assert out.returncode == 0
		assert 'CLI节点' in out.stdout
