#!/usr/bin/env bash
# 通过 mihomo 拉取订阅、启动本地代理并探测可用节点。
# 环境变量:
#   PROXY_SUBSCRIPTION_URL  订阅链接（未设置则跳过代理初始化）
#   PROXY_TEST_URL          探测目标，默认 https://www.google.com/generate_204
#   PROXY_REQUIRED          true 时探测失败则退出 1
#   PROXY_PORT              本地 mixed-port，默认 7890

set -euo pipefail

if [[ -z "${PROXY_SUBSCRIPTION_URL:-}" ]]; then
	echo "[信息] 未设置 PROXY_SUBSCRIPTION_URL，跳过代理初始化"
	exit 0
fi

PROXY_DIR="${RUNNER_TEMP:-/tmp}/checkin-proxy"
PROXY_PORT="${PROXY_PORT:-7890}"
CONTROLLER_PORT="${MIHOMO_CONTROLLER_PORT:-9090}"
PROXY_TEST_URL="${PROXY_TEST_URL:-https://www.google.com/generate_204}"
MIHOMO_VERSION="${MIHOMO_VERSION:-v1.19.0}"
PROXY_REQUIRED="${PROXY_REQUIRED:-false}"

# 生成控制器访问密钥（仅 Python 节点选择器读取，绝不打印）
PROXY_SECRET="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
export PROXY_SECRET

mkdir -p "${PROXY_DIR}"
cd "${PROXY_DIR}"

# 提前收紧权限：后续创建的 config.yaml（含订阅 URL）、secret、日志等均不可被同机其他进程读取
umask 077

echo "[信息] 正在下载 mihomo ${MIHOMO_VERSION}..."
ARCHIVE="mihomo-linux-amd64-${MIHOMO_VERSION}.gz"
if ! curl --retry 3 --retry-delay 5 --retry-all-errors -fsSL -o "${ARCHIVE}" \
	"https://github.com/MetaCubeX/mihomo/releases/download/${MIHOMO_VERSION}/${ARCHIVE}"; then
	echo "[警告] 下载 mihomo ${MIHOMO_VERSION} 失败，跳过代理初始化"
	if [[ "${PROXY_REQUIRED}" == "true" ]]; then
		exit 1
	fi
	exit 0
fi

# 尽力而为的 SHA-256 校验：通过 GitHub Release API 获取官方 digest。
# 任何失败（网络 / 限流 / 字段变化）都只警告、不阻塞，避免破坏现有可用的下载流程。
EXPECTED_SHA="$(
	curl -fsS --max-time 15 "https://api.github.com/repos/MetaCubeX/mihomo/releases/tags/${MIHOMO_VERSION}" 2>/dev/null \
		| python3 -c '
import sys, json
try:
    tag = json.load(sys.stdin)
    digest = next((a.get("digest", "") for a in tag.get("assets", []) if a.get("name") == "'"${ARCHIVE}"'"), "")
    # GitHub asset digest 形如 "sha256:xxx"，解析出 64 位纯 hex；格式异常则跳过校验
    if isinstance(digest, str) and digest.startswith("sha256:"):
        hexdigest = digest[7:]
        print(hexdigest if len(hexdigest) == 64 else "")
    else:
        print("")
except Exception:
    print("")
' 2>/dev/null || echo ""
)"
if [[ -n "${EXPECTED_SHA}" ]]; then
	ACTUAL_SHA="$(sha256sum "${ARCHIVE}" | awk '{print $1}')"
	if [[ "${ACTUAL_SHA}" != "${EXPECTED_SHA}" ]]; then
		echo "[失败] mihomo 下载文件 SHA-256 校验失败（与官方 digest 不一致，或下载损坏）"
		rm -f "${ARCHIVE}"
		# 与"下载失败"降级逻辑一致：非必需代理时不阻塞签到，直连继续
		if [[ "${PROXY_REQUIRED}" == "true" ]]; then
			exit 1
		fi
		echo "[警告] 跳过代理初始化，将直连签到"
		exit 0
	fi
	echo "[信息] mihomo SHA-256 校验通过"
else
	echo "[警告] 无法获取 mihomo 官方 SHA-256，跳过校验（不影响代理启动）"
fi

# 解压失败（跳过校验时下载文件已损坏）同样按降级处理，避免阻塞签到
if ! gunzip -f "${ARCHIVE}"; then
	echo "[失败] mihomo 压缩包解压失败"
	rm -f "${ARCHIVE}"
	if [[ "${PROXY_REQUIRED}" == "true" ]]; then
		exit 1
	fi
	echo "[警告] 跳过代理初始化，将直连签到"
	exit 0
fi
chmod +x "mihomo-linux-amd64-${MIHOMO_VERSION}"
MIHOMO_BIN="${PROXY_DIR}/mihomo-linux-amd64-${MIHOMO_VERSION}"

cat > config.yaml <<EOF
mixed-port: ${PROXY_PORT}
allow-lan: false
ipv6: false
mode: rule
log-level: warning
unified-delay: true

# 开启 REST API，供 Python 节点选择器控制节点切换
external-controller: 127.0.0.1:${CONTROLLER_PORT}
secret: "${PROXY_SECRET}"

proxy-providers:
  subscription:
    type: http
    url: "${PROXY_SUBSCRIPTION_URL}"
    interval: 3600
    path: ./subscription.yaml
    health-check:
      enable: true
      interval: 300
      url: https://www.gstatic.com/generate_204

# 按区域分组，节点由 Python 节点选择器手动选择（selector），不再自动测速切换
proxy-groups:
  - name: 🇯🇵 日本
    type: selector
    use:
      - subscription
    filter: "日本|JP|Japan|东京|Tokyo|大阪|Osaka"

  - name: 🇸🇬 新加坡
    type: selector
    use:
      - subscription
    filter: "新加坡|SG|Singapore"

  - name: 🇭🇰 香港
    type: selector
    use:
      - subscription
    filter: "香港|HK|Hong Kong|HongKong|HGC"

# 回退链：日本 → 新加坡 → 香港（由 Python 节点选择器按顺序选择）
  - name: AUTO
    type: selector
    proxies:
      - 🇯🇵 日本
      - 🇸🇬 新加坡
      - 🇭🇰 香港

rules:
  # 订阅拉取直连，避免走代理自身形成循环依赖
  - DOMAIN-KEYWORD,mjurl.com,DIRECT
  - MATCH,AUTO
EOF

echo "[信息] 正在保存控制器密钥到 ${PROXY_DIR}/proxy-secret"
echo "${PROXY_SECRET}" > "${PROXY_DIR}/proxy-secret"

echo "[信息] 正在启动 mihomo 于 127.0.0.1:${PROXY_PORT}..."
nohup "${MIHOMO_BIN}" -d "${PROXY_DIR}" -f config.yaml > mihomo.log 2>&1 &
echo $! > mihomo.pid

PROXY_URL="http://127.0.0.1:${PROXY_PORT}"

# 订阅是异步加载的，启动瞬间代理还没有节点，直接健康检查必然失败；先等待节点加载完成
echo "[信息] 等待订阅节点加载..."
NODE_READY=false
for wait in $(seq 1 60); do
	NODE_COUNT="$(
		curl -fsS -H "Authorization: Bearer ${PROXY_SECRET}" \
			"http://127.0.0.1:${CONTROLLER_PORT}/providers/proxies/subscription" \
			--max-time 5 2>/dev/null \
			| python3 -c 'import sys,json;d=json.load(sys.stdin);print(len(d.get("all") or d.get("proxies") or []))' 2>/dev/null \
			|| echo 0
	)"
	if [[ "${NODE_COUNT}" -gt 0 ]]; then
		echo "[信息] 已加载 ${NODE_COUNT} 个订阅节点"
		NODE_READY=true
		break
	fi
	echo "[信息] 等待订阅节点加载中 (${wait}/60)..."
	sleep 2
done

if [[ "${NODE_READY}" != "true" ]]; then
	echo "[失败] 订阅节点加载超时，代理不可用"
	tail -n 30 mihomo.log || true
	if [[ -f mihomo.pid ]]; then
		kill "$(cat mihomo.pid)" 2>/dev/null || true
	fi
	if [[ "${PROXY_REQUIRED}" == "true" ]]; then
		exit 1
	fi
	exit 0
fi

# 发起健康检查：通过代理访问探测目标
READY=false
for attempt in $(seq 1 45); do
	if curl -fsS -x "${PROXY_URL}" --max-time 20 "${PROXY_TEST_URL}" -o /dev/null 2>/dev/null; then
		READY=true
		break
	fi
	echo "[信息] 正在等待代理健康检查 (${attempt}/45)..."
	sleep 2
done

if [[ "${READY}" != "true" ]]; then
	echo "[失败] 代理健康检查失败：${PROXY_TEST_URL}"
	tail -n 30 mihomo.log || true
	if [[ -f mihomo.pid ]]; then
		kill "$(cat mihomo.pid)" 2>/dev/null || true
	fi
	if [[ "${PROXY_REQUIRED}" == "true" ]]; then
		exit 1
	fi
	exit 0
fi

echo "[成功] 代理已就绪：${PROXY_URL}"
echo "[信息] 代理仅作用于 CHECKIN_PROXY_URL（浏览器/Python，不设全局 HTTP_PROXY）"
if [[ -n "${GITHUB_ENV:-}" ]]; then
	echo "CHECKIN_PROXY_URL=${PROXY_URL}" >> "${GITHUB_ENV}"
	echo "MIHOMO_CONTROLLER=http://127.0.0.1:${CONTROLLER_PORT}" >> "${GITHUB_ENV}"
	echo "MIHOMO_SECRET_FILE=${PROXY_DIR}/proxy-secret" >> "${GITHUB_ENV}"
else
	# 本地运行（无 GITHUB_ENV）：把代理变量写入可 source 的文件，并提示手动导出
	ENV_FILE="${PROXY_DIR}/proxy.env"
	cat > "${ENV_FILE}" <<EONV
export CHECKIN_PROXY_URL="${PROXY_URL}"
export MIHOMO_CONTROLLER="http://127.0.0.1:${CONTROLLER_PORT}"
export MIHOMO_SECRET_FILE="${PROXY_DIR}/proxy-secret"
EONV
	echo "[信息] 本地运行：代理变量已写入 ${ENV_FILE}"
	echo "[信息] 如需传递到当前 shell，请执行: source ${ENV_FILE}"
fi