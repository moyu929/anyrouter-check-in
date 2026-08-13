#!/usr/bin/env bash
# 通过 mihomo 拉取订阅、启动本地代理并探测可用节点。
# 环境变量:
#   PROXY_SUBSCRIPTION_URL  订阅链接（必填才启用）
#   PROXY_TEST_URL          探测目标，默认 https://www.google.com/generate_204
#   PROXY_REQUIRED          true 时探测失败则退出 1
#   PROXY_PORT              本地 mixed-port，默认 7890

set -euo pipefail

if [[ -z "${PROXY_SUBSCRIPTION_URL:-}" ]]; then
	echo "[INFO] PROXY_SUBSCRIPTION_URL not set, skip proxy setup"
	exit 0
fi

PROXY_DIR="${RUNNER_TEMP:-/tmp}/checkin-proxy"
PROXY_PORT="${PROXY_PORT:-7890}"
CONTROLLER_PORT="${MIHOMO_CONTROLLER_PORT:-9090}"
PROXY_TEST_URL="${PROXY_TEST_URL:-https://www.google.com/generate_204}"
MIHOMO_VERSION="${MIHOMO_VERSION:-v1.19.0}"
PROXY_REQUIRED="${PROXY_REQUIRED:-false}"

# 生成 controller 访问密钥（仅 Python 节点选择器读取，绝不打印）
PROXY_SECRET="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
export PROXY_SECRET

mkdir -p "${PROXY_DIR}"
cd "${PROXY_DIR}"

echo "[INFO] Downloading mihomo ${MIHOMO_VERSION}..."
ARCHIVE="mihomo-linux-amd64-${MIHOMO_VERSION}.gz"
if ! curl --retry 3 --retry-delay 5 --retry-all-errors -fsSL -o "${ARCHIVE}" \
	"https://github.com/MetaCubeX/mihomo/releases/download/${MIHOMO_VERSION}/${ARCHIVE}"; then
	echo "[WARN] Failed to download mihomo ${MIHOMO_VERSION}, skip proxy setup"
	if [[ "${PROXY_REQUIRED}" == "true" ]]; then
		exit 1
	fi
	exit 0
fi
gunzip -f "${ARCHIVE}"
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

echo "[INFO] Saving controller secret to ${PROXY_DIR}/proxy-secret"
umask 077  # 防止 secret 文件被同机其他进程读取
echo "${PROXY_SECRET}" > "${PROXY_DIR}/proxy-secret"

echo "[INFO] Starting mihomo on 127.0.0.1:${PROXY_PORT}..."
nohup "${MIHOMO_BIN}" -d "${PROXY_DIR}" -f config.yaml > mihomo.log 2>&1 &
echo $! > mihomo.pid

PROXY_URL="http://127.0.0.1:${PROXY_PORT}"
READY=false
for attempt in $(seq 1 45); do
	if curl -fsS -x "${PROXY_URL}" --max-time 20 "${PROXY_TEST_URL}" -o /dev/null 2>/dev/null; then
		READY=true
		break
	fi
	echo "[INFO] Waiting for proxy health check (${attempt}/45)..."
	sleep 2
done

if [[ "${READY}" != "true" ]]; then
	echo "[FAILED] Proxy health check failed for ${PROXY_TEST_URL}"
	tail -n 30 mihomo.log || true
	if [[ -f mihomo.pid ]]; then
		kill "$(cat mihomo.pid)" 2>/dev/null || true
	fi
	if [[ "${PROXY_REQUIRED}" == "true" ]]; then
		exit 1
	fi
	exit 0
fi

echo "[SUCCESS] Proxy is ready: ${PROXY_URL}"
echo "[INFO] Proxy is scoped to CHECKIN_PROXY_URL (browser/python only, not global HTTP_PROXY)"
if [[ -n "${GITHUB_ENV:-}" ]]; then
	echo "CHECKIN_PROXY_URL=${PROXY_URL}" >> "${GITHUB_ENV}"
	echo "MIHOMO_CONTROLLER=http://127.0.0.1:${CONTROLLER_PORT}" >> "${GITHUB_ENV}"
	echo "MIHOMO_SECRET_FILE=${PROXY_DIR}/proxy-secret" >> "${GITHUB_ENV}"
fi
