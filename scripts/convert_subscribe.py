#!/usr/bin/env python3
"""将机场订阅转换为 Clash 格式的 proxies 片段，供 mihomo 以 file provider 加载。

用法:
  python3 convert_subscribe.py < subscription.raw > subscription.yaml

输入支持两种格式:
  1. Clash YAML（内容含 `proxies:` 列表）—— 原样输出
  2. v2rayN 通用订阅（base64 编码的节点链接）—— 解析 vmess / vless / trojan / ss 链接，
     生成 `proxies:` 列表输出

mihomo 的 proxy-providers 无法直接解析 v2rayN base64 订阅，必须在喂给 mihomo 前先转换。
解析不出任何节点时以非零码退出，由调用方决定是否降级直连。
"""

from __future__ import annotations

import base64
import json
import re
import sys
import urllib.parse


def b64d(s: str) -> str:
	"""Base64 解码（去除全部空白，兼容 URL-safe 与缺失 padding），失败返回空串。"""
	s = re.sub(r'\s+', '', s)
	s = s.replace('-', '+').replace('_', '/')
	s += '=' * (-len(s) % 4)
	try:
		return base64.b64decode(s).decode('utf-8', 'ignore')
	except Exception:
		return ''


def parse_vmess(link: str):
	info = json.loads(b64d(link[len('vmess://') :]) or '{}')
	proxy = {
		'name': (info.get('ps') or info.get('add') or 'vmess').strip() or 'vmess',
		'type': 'vmess',
		'server': info.get('add', ''),
		'port': int(info.get('port', 0) or 0),
		'uuid': info.get('id', ''),
		'alterId': int(info.get('aid', 0) or 0),
		'cipher': info.get('scy') or 'auto',
	}
	net = info.get('net', 'tcp')
	if net == 'ws':
		proxy['network'] = 'ws'
		proxy['ws-opts'] = {'path': info.get('path', ''), 'headers': {'Host': info.get('host', '')}}
	elif net == 'grpc':
		proxy['network'] = 'grpc'
		proxy['grpc-opts'] = {'grpc-service-name': info.get('path', '')}
	if info.get('tls') == 'tls':
		proxy['tls'] = True
		proxy['sni'] = info.get('sni') or info.get('host') or info.get('add')
		if info.get('alpn'):
			proxy['alpn'] = [info['alpn']]
	return proxy


def parse_vless(link: str):
	u = urllib.parse.urlparse(link)
	q = urllib.parse.parse_qs(u.query)
	security = q.get('security', [''])[0]
	proxy = {
		'name': urllib.parse.unquote(u.fragment or 'vless').strip() or 'vless',
		'type': 'vless',
		'server': u.hostname or '',
		'port': u.port or 0,
		'uuid': u.username or '',
	}
	flow = q.get('flow', [''])[0]
	if flow:
		proxy['flow'] = flow
	net = q.get('type', ['tcp'])[0]
	if net == 'ws':
		proxy['network'] = 'ws'
		proxy['ws-opts'] = {'path': q.get('path', [''])[0], 'headers': {'Host': q.get('host', [''])[0]}}
	if security in ('tls', 'reality'):
		proxy['tls'] = True
		proxy['sni'] = q.get('sni', [''])[0] or q.get('host', [''])[0] or u.hostname or ''
		if security == 'reality':
			proxy['reality-opts'] = {'public-key': q.get('pbk', [''])[0], 'short-id': q.get('sid', [''])[0]}
	return proxy


def parse_trojan(link: str):
	u = urllib.parse.urlparse(link)
	q = urllib.parse.parse_qs(u.query)
	proxy = {
		'name': urllib.parse.unquote(u.fragment or 'trojan').strip() or 'trojan',
		'type': 'trojan',
		'server': u.hostname or '',
		'port': u.port or 0,
		'password': u.username or '',
		'tls': True,
		'sni': q.get('sni', [''])[0] or u.hostname or '',
	}
	net = q.get('type', ['tcp'])[0]
	if net == 'ws':
		proxy['network'] = 'ws'
		proxy['ws-opts'] = {'path': q.get('path', [''])[0], 'headers': {'Host': q.get('host', [''])[0]}}
	return proxy


def parse_ss(link: str):
	body = link[len('ss://') :]
	fragment = ''
	if '#' in body:
		body, fragment = body.split('#', 1)
	if '@' in body:
		userinfo, hostpart = body.rsplit('@', 1)
		method, password = userinfo.split(':', 1) if ':' in userinfo else ('', '')
	else:
		dec = b64d(body)
		if '@' not in dec or ':' not in dec.split('@', 1)[0]:
			return None
		userinfo, hostpart = dec.rsplit('@', 1)
		method, password = userinfo.split(':', 1)
	if ':' not in hostpart or not hostpart.rsplit(':', 1)[1].isdigit():
		return None
	host, port = hostpart.rsplit(':', 1)
	return {
		'name': urllib.parse.unquote(fragment or 'ss').strip() or 'ss',
		'type': 'ss',
		'server': host,
		'port': int(port),
		'cipher': method,
		'password': password,
	}


def parse_line(line: str):
	line = line.strip()
	if not line or line.startswith('#'):
		return None
	try:
		if line.startswith('vmess://'):
			return parse_vmess(line)
		if line.startswith('vless://'):
			return parse_vless(line)
		if line.startswith('trojan://'):
			return parse_trojan(line)
		if line.startswith('ss://'):
			return parse_ss(line)
	except Exception:
		return None
	return None


def dump_yaml(proxies: list[dict]) -> str:
	"""将节点 dict 列表输出为 Clash proxies 片段。"""
	lines = ['proxies:']
	for p in proxies:
		lines.append('  - name: %s' % json.dumps(p['name'], ensure_ascii=False))
		for k, v in p.items():
			if k == 'name':
				continue
			if k == 'type':
				lines.append('    type: %s' % v)
			else:
				lines.append('    %s: %s' % (k, json.dumps(v, ensure_ascii=False)))
	return '\n'.join(lines) + '\n'


def _parse_all(text: str) -> list[dict]:
	"""逐行解析节点链接，返回可用的节点 dict 列表。"""
	return [p for p in (parse_line(line) for line in text.splitlines()) if p]


def main(argv: list[str] | None = None) -> int:
	args = sys.argv[1:] if argv is None else argv
	if args:
		try:
			content = open(args[0], 'r', encoding='utf-8', errors='ignore').read()
		except OSError as e:
			sys.stderr.write(f'错误: 无法读取订阅文件: {e}\n')
			return 1
	else:
		content = sys.stdin.read()

	# 1) 已是 Clash YAML（含 proxies: 列表）则原样输出
	if re.search(r'^\s*proxies\s*:', content, re.M):
		sys.stdout.write(content)
		return 0

	# 2) 已是节点链接列表则直接解析
	proxies = _parse_all(content)
	if not proxies:
		# 3) v2rayN 订阅：整体 base64 编码，解码后再尝试（解码结果可能是 YAML 或链接列表）
		decoded = b64d(content)
		if re.search(r'^\s*proxies\s*:', decoded, re.M):
			sys.stdout.write(decoded)
			return 0
		proxies = _parse_all(decoded)
	if not proxies:
		sys.stderr.write('错误: 订阅中未解析出任何可用节点\n')
		return 1
	sys.stdout.write(dump_yaml(proxies))
	return 0


if __name__ == '__main__':
	sys.exit(main())
