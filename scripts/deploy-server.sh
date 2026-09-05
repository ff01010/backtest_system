#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="$project_root/deploy/.env"
compose_file="$project_root/deploy/compose.infrastructure.yml"
clickhouse_compose_file="$project_root/deploy/compose.clickhouse.yml"

fail() {
  echo "发布终止：$1" >&2
  exit 1
}

wait_for_postgres() {
  local attempt health
  for attempt in $(seq 1 30); do
    health="$(docker inspect --format '{{.State.Health.Status}}' quant-backtest-postgres 2>/dev/null || true)"
    if [[ "$health" == "healthy" ]]; then
      return
    fi
    sleep 2
  done
  fail "PostgreSQL 未能在 60 秒内进入 healthy 状态"
}

read_http_port() {
  local port
  port="$(grep -E '^APP_HTTP_PORT=' "$env_file" | tail -n 1 | cut -d= -f2- || true)"
  port="${port//[[:space:]]/}"
  if [[ -z "$port" ]]; then
    echo "8080"
    return
  fi
  [[ "$port" =~ ^[0-9]{1,5}$ ]] || fail "deploy/.env 中的 APP_HTTP_PORT 必须是端口号"
  echo "$port"
}

read_runner_replicas() {
  local name="$1" replicas
  replicas="$(grep -E "^${name}=" "$env_file" | tail -n 1 | cut -d= -f2- || true)"
  replicas="${replicas//[[:space:]]/}"
  if [[ -z "$replicas" ]]; then
    echo "12"
    return
  fi
  [[ "$replicas" =~ ^([1-9]|1[0-2])$ ]] || fail "deploy/.env 中的 ${name} 必须是 1 至 12 的整数"
  echo "$replicas"
}

check_active_backtests() {
  local counts
  counts="$(docker exec quant-backtest-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$APP_DB_NAME" -Atc "SELECT count(*) FILTER (WHERE status='"'"'queued'"'"'), count(*) FILTER (WHERE status IN ('"'"'running'"'"','"'"'cancelling'"'"')) FROM backtest_run;"')"
  echo "发布前回测任务：queued=${counts%%|*}, active=${counts##*|}"
  [[ "${counts##*|}" == "0" ]] || fail "存在运行中或取消中的回测任务，请先等待完成或手动取消"
}

runner_containers_ready() {
  local service="$1" expected="$2" container_ids container_id count=0
  container_ids="$("${compose[@]}" ps -q "$service")"
  while IFS= read -r container_id; do
    [[ -n "$container_id" ]] || continue
    [[ "$(docker inspect --format '{{.State.Status}}' "$container_id" 2>/dev/null || true)" == "running" ]] \
      || return 1
    count=$((count + 1))
  done <<< "$container_ids"
  [[ "$count" == "$expected" ]]
}

validate_clickhouse_network() {
  local clickhouse_url
  clickhouse_url="$(grep -E '^TREND_GRID_CLICKHOUSE_HTTP_URL=' "$env_file" | tail -n 1 | cut -d= -f2- || true)"
  clickhouse_url="${clickhouse_url//[[:space:]]/}"
  [[ "$clickhouse_url" == "http://quant-clickhouse:8123" ]] || fail "deploy/.env 中的 TREND_GRID_CLICKHOUSE_HTTP_URL 必须为 http://quant-clickhouse:8123"
  docker network inspect quant-net >/dev/null 2>&1 || fail "缺少 ClickHouse Docker 网络：quant-net"
}

verify_clickhouse_connectivity() {
  local runner_id runner_ids
  runner_ids="$("${compose[@]}" ps -q l2-runner)"
  [[ -n "$runner_ids" ]] || fail "l2-runner 未启动，无法检查 ClickHouse 连通性"
  while IFS= read -r runner_id; do
    [[ -n "$runner_id" ]] || continue
    docker exec "$runner_id" python -c "import urllib.request; response = urllib.request.urlopen('http://quant-clickhouse:8123/ping', timeout=5); assert response.read().strip() == b'Ok.'" \
      || fail "l2-runner 无法通过 quant-clickhouse:8123 连接 ClickHouse"
  done <<< "$runner_ids"
  runner_ids="$("${compose[@]}" ps -q bar-runner)"
  [[ -n "$runner_ids" ]] || fail "bar-runner 未启动，无法检查 ClickHouse 连通性"
  while IFS= read -r runner_id; do
    [[ -n "$runner_id" ]] || continue
    docker exec "$runner_id" python -c "import urllib.request; response = urllib.request.urlopen('http://quant-clickhouse:8123/ping', timeout=5); assert response.read().strip() == b'Ok.'" \
      || fail "bar-runner 无法通过 quant-clickhouse:8123 连接 ClickHouse"
  done <<< "$runner_ids"
}

verify_trade_calendar() {
  local check_code
  check_code="$(cat <<'PY'
from datetime import datetime
from zoneinfo import ZoneInfo

from backend.services import state_store

today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
latest = state_store.latest_trade_calendar_date()
covered = state_store.is_trade_calendar_range_covered(today, today)
if latest != today or not covered:
  raise SystemExit(f"交易日历校验失败：latest={latest}, expected={today}, covered={covered}")
print(f"交易日历校验通过：覆盖至 {today}")
PY
)"
  "${compose[@]}" --profile calendar-sync run --rm --no-deps calendar-sync python -c "$check_code"
}

wait_for_application() {
  local port="$1" bar_replicas="$2" l2_replicas="$3" attempt health_url response
  health_url="http://127.0.0.1:${port}/api/health"
  for attempt in $(seq 1 45); do
    response="$(curl --fail --silent --show-error "$health_url" 2>/dev/null || true)"
    if runner_containers_ready "bar-runner" "$bar_replicas" \
      && runner_containers_ready "l2-runner" "$l2_replicas" \
      && [[ "$response" == *'"status":"ok"'* ]]; then
      echo "发布完成，应用与 Runner 健康检查通过：$health_url"
      return
    fi
    sleep 2
  done
  "${compose[@]}" ps
  fail "应用或 Runner 未能在 90 秒内通过健康检查：$health_url"
}

[[ -f "$env_file" ]] || fail "缺少服务器环境文件：$env_file"
[[ -f "$clickhouse_compose_file" ]] || fail "缺少 ClickHouse 网络配置：$clickhouse_compose_file"
command -v docker >/dev/null || fail "未安装 Docker"
command -v curl >/dev/null || fail "未安装 curl"

cd "$project_root"
[[ "$(git branch --show-current)" == "main" ]] || fail "当前分支不是 main"
[[ -z "$(git status --porcelain)" ]] || fail "工作区有未提交改动，请先处理后再发布"

git fetch origin main
git pull --ff-only origin main

compose=(docker compose --env-file "$env_file" -f "$compose_file" -f "$clickhouse_compose_file")
bar_runner_replicas="$(read_runner_replicas TREND_GRID_BAR_RUNNER_REPLICAS)"
l2_runner_replicas="$(read_runner_replicas TREND_GRID_L2_RUNNER_REPLICAS)"
validate_clickhouse_network
"${compose[@]}" config >/dev/null || fail "Docker Compose 配置校验失败"
"${compose[@]}" up -d postgres
wait_for_postgres
check_active_backtests
"${compose[@]}" --profile migration run --rm migrate
"${compose[@]}" --profile calendar-sync run --rm --build calendar-sync
verify_trade_calendar || fail "交易日历同步后校验失败"
"${compose[@]}" up -d --build \
  --scale "bar-runner=$bar_runner_replicas" \
  --scale "l2-runner=$l2_runner_replicas" \
  strategy-worker backend bar-runner l2-runner
# Nginx 在启动时解析 backend 容器地址；后端重建后必须刷新 frontend，避免保留旧地址返回 502。
"${compose[@]}" up -d --build --force-recreate frontend
verify_clickhouse_connectivity
"${compose[@]}" ps
wait_for_application "$(read_http_port)" "$bar_runner_replicas" "$l2_runner_replicas"
