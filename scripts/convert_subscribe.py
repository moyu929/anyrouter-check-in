#!/usr/bin/env python3
"""将机场订阅转换为 mihomo 可直接使用的完整配置（节点直接写入主配置 proxies）。

用法:
  python3 convert_subscribe.py <订阅文件> <输出配置文件>

动态参数通过环境变量传入:
  PROXY_PORT             本地 mixed-port（默认 7890）
  MIHOMO_CONTROLLER_PORT external-controller 端口（默认 9090）
  PROXY_SECRET           controller 访问密钥

设计说明:
  - 不依赖 proxy-providers：mihomo 的 file/http provider 无法直接解析 v2rayN base64 订阅，
    且异步加载在 CI 中曾导致节点恒为 0、卡在"等待订阅节点加载"。这里把转换后的节点
    直接写入主配置 proxies，mihomo 启动时同步加载，立即就绪。
  - 输入支持 Clash YAML（含 proxies: 列表）或 v2rayN base64 订阅（vmess/vless/trojan/ss）。
  - 按节点名把节点分组到 🇯🇵 日本 / 🇸🇬 新加坡 / 🇭🇰 香港 三个 selector 组，
    供 utils/proxy_selector.py 按区域优先级主动选择；信息占位节点与未匹配区域节点被剔除。
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.parse

# 占位/信息节点（机场用来展示流量/到期信息），连通性必然失败，直接剔除
_INFO_NAME_KEYWORDS = ('剩余流量', '套餐到期', '过滤', '过期', '官网', '备用更新')

# 区域 selector 组名 → 节点名匹配正则（顺序与 utils/proxy_selector.py REGION_GROUPS 一致）
REGION_FILTERS: list[tuple[str, str]] = [
	('🇯🇵 日本', r'日本|JP|Japan|东京|Tokyo|大阪|Osaka'),
	('🇸🇬 新加坡', r'新加坡|SG|Singapore'),
	('🇭🇰 香港', r'香港|HK|Hong ?Kong|HongKong|HGC'),
]
AUTO_GROUP = 'AUTO'


def _env_int(name: str, default: int) -> int:
	raw = os.getenv(name, '').strip()
	return int(raw) if raw.isdigit() else default


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


def _clash_proxies_blocks(content: str) -> list[dict]:
	"""尽力而为地从 Clash YAML 提取 proxies 节点块（无 yaml 依赖）。

	返回 [{'name': 节点名, '_raw': 原始节点块}]；非 Clash YAML 返回空列表。
	"""
	m = re.search(r'^proxies:\s*$', content, re.M)
	if not m:
		return []
	tail = content[m.end() :]
	blocks: list[dict] = []
	for node_m in re.finditer(r'^  - name:\s*(.+?)\s*$', tail, re.M):
		name = node_m.group(1).strip().strip('"\'')
		if not name:
			continue
		start = node_m.start()
		nxt = re.search(r'^  - ', tail[start + 1 :], re.M)
		end = start + 1 + (nxt.start() if nxt else len(tail) - start - 1)
		blocks.append({'name': name, '_raw': tail[start:end].rstrip()})
	return blocks


def _is_info_node(name: str) -> bool:
	"""占位/信息节点名。"""
	return any(keyword in name for keyword in _INFO_NAME_KEYWORDS)


def parse_subscription(content: str) -> list[dict]:
	"""从订阅内容提取节点列表（剔除占位节点、去重）。"""
	blocks = _clash_proxies_blocks(content)
	if blocks:
		proxies = blocks
	else:
		decoded = b64d(content)
		proxies = [p for p in (parse_line(line) for line in decoded.splitlines()) if p]

	cleaned: list[dict] = []
	seen: set[str] = set()
	for p in proxies:
		name = p['name']
		if name in seen or _is_info_node(name):
			continue
		seen.add(name)
		cleaned.append(p)
	return cleaned


def _proxy_lines(proxy: dict) -> list[str]:
	"""将单个节点转换为 config.yaml 的 proxies 条目行。"""
	if '_raw' in proxy:
		return proxy['_raw'].splitlines()
	lines = ['  - name: %s' % json.dumps(proxy['name'], ensure_ascii=False)]
	for k, v in proxy.items():
		if k == 'name':
			continue
		if k == 'type':
			lines.append('    type: %s' % v)
		else:
			lines.append('    %s: %s' % (k, json.dumps(v, ensure_ascii=False)))
	return lines


def build_config(proxies: list[dict], port: int, controller_port: int, secret: str) -> str:
	"""生成完整 mihomo 配置（proxies 直接写入主配置，按区域分组）。"""
	grouped: dict[str, list[str]] = {region: [] for region, _ in REGION_FILTERS}
	lines: list[str] = []
	for p in proxies:
		region = next((r for r, pattern in REGION_FILTERS if re.search(pattern, p['name'])), None)
		if region:
			grouped[region].append(p['name'])
		lines.extend(_proxy_lines(p))

	config = [
		f'mixed-port: {port}',
		'allow-lan: false',
		'ipv6: false',
		'mode: rule',
		'log-level: warning',
		'unified-delay: true',
		'',
		f'external-controller: 127.0.0.1:{controller_port}',
		f'secret: "{secret}"',
		'',
		'proxies:',
	]
	config.extend(lines)
	config.extend(['', 'proxy-groups:'])
	for region, node_names in grouped.items():
		config.append(f'  - name: "{region}"')
		config.append('    type: select')
		config.append('    proxies:')
		for name in node_names:
			config.append('      - %s' % json.dumps(name, ensure_ascii=False))
	config.append(f'  - name: "{AUTO_GROUP}"')
	config.append('    type: select')
	config.append('    proxies:')
	for region, _ in REGION_FILTERS:
		config.append('      - %s' % json.dumps(region, ensure_ascii=False))
	config.extend(['', 'rules:', '  - MATCH,AUTO'])
	return '\n'.join(config) + '\n'


def main(argv: list[str] | None = None) -> int:
	args = sys.argv[1:] if argv is None else argv
	if len(args) != 2:
		sys.stderr.write('用法: python3 convert_subscribe.py <订阅文件> <输出配置文件>\n')
		return 2
	src, out = args
	try:
		content = open(src, 'r', encoding='utf-8', errors='ignore').read()
	except OSError as e:
		sys.stderr.write(f'错误: 无法读取订阅文件: {e}\n')
		return 1

	proxies = parse_subscription(content)
	if not proxies:
		sys.stderr.write('错误: 订阅中未解析出任何可用节点\n')
		return 1

	config = build_config(
		proxies,
		port=_env_int('PROXY_PORT', 7890),
		controller_port=_env_int('MIHOMO_CONTROLLER_PORT', 9090),
		secret=os.getenv('PROXY_SECRET', ''),
	)
	try:
		with open(out, 'w', encoding='utf-8', newline='\n') as f:
			f.write(config)
	except OSError as e:
		sys.stderr.write(f'错误: 无法写入配置: {e}\n')
		return 1
	print(f'[信息] 已生成 mihomo 配置: {out}（{len(proxies)} 个有效节点）')
	return 0


if __name__ == '__main__':
	sys.exit(main())
