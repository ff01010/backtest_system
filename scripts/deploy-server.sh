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

wait_for_application() {
  local port="$1" attempt health_url
  health_url="http://127.0.0.1:${port}/api/health"
  for attempt in $(seq 1 30); do
    if curl --fail --silent --show-error "$health_url"; then
      echo
      echo "发布完成，健康检查通过：$health_url"
      return
    fi
    sleep 2
  done
  fail "应用未能在 60 秒内通过健康检查：$health_url"
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
"${compose[@]}" up -d postgres
wait_for_postgres
"${compose[@]}" --profile migration run --rm migrate
"${compose[@]}" up -d --build strategy-worker backend l2-runner frontend
"${compose[@]}" ps
wait_for_application "$(read_http_port)"
