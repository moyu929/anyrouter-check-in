#!/usr/bin/env bash
# 停止 mihomo 代理，并清理订阅缓存、secret、配置与日志等临时产物。
set -euo pipefail

PROXY_DIR="${RUNNER_TEMP:-/tmp}/checkin-proxy"
PID_FILE="${PROXY_DIR}/mihomo.pid"

if [[ -f "${PID_FILE}" ]]; then
	echo "[信息] 正在停止 mihomo 代理 (pid $(cat "${PID_FILE}"))"
	kill "$(cat "${PID_FILE}")" 2>/dev/null || true
	rm -f "${PID_FILE}"
fi

# 清理临时产物（订阅缓存、secret、配置、日志、本地 env 导出文件），避免残留敏感信息
if [[ -d "${PROXY_DIR}" ]]; then
	rm -f \
		"${PROXY_DIR}/subscription.raw" \
		"${PROXY_DIR}/subscription.yaml" \
		"${PROXY_DIR}/proxy-secret" \
		"${PROXY_DIR}/config.yaml" \
		"${PROXY_DIR}/mihomo.log" \
		"${PROXY_DIR}/proxy.env" \
		"${PROXY_DIR}/mihomo.pid"
	echo "[信息] 已清理代理临时产物: ${PROXY_DIR}"
fi