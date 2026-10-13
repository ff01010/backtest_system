#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="$project_root/deploy/.env"
compose_file="$project_root/deploy/compose.infrastructure.yml"
clickhouse_compose_file="$project_root/deploy/compose.clickhouse.yml"
submission_gate_dir="$project_root/deploy/submission-gate"
deploy_lock_file="/tmp/quant-backtest-deploy.lock"
script_args=("$@")

# 让 flock 父进程持有整段发布锁，脚本更新后可在同一把锁内安全重启。
if [[ "${QUANT_DEPLOY_LOCK_HELD:-}" != "1" ]]; then
  command -v flock >/dev/null || {
    echo "发布终止：未安装 flock，无法保证发布互斥" >&2
    exit 1
  }
  if flock -n -E 75 "$deploy_lock_file" \
    env QUANT_DEPLOY_LOCK_HELD=1 bash "$project_root/scripts/deploy-server.sh" \
      "${script_args[@]}"; then
    exit 0
  else
    status=$?
    if [[ "$status" == "75" ]]; then
      echo "发布终止：已有部署或回滚流程正在执行" >&2
    fi
    exit "$status"
  fi
fi

fail() {
  echo "发布终止：$1" >&2
  exit 1
}

close_submission_gate() {
  mkdir -p "$submission_gate_dir"
  : > "$submission_gate_dir/closed"
  echo "新回测提交门禁已关闭"
}

open_submission_gate() {
  rm -f "$submission_gate_dir/closed"
  echo "新回测提交门禁已开放"
}

report_incomplete_release() {
  if [[ "${release_activated:-false}" != "true" ]]; then
    echo "发布未完成：新回测提交门禁保持关闭，需完成激活或完整回退后手动开放" >&2
  fi
}

read_client_compatibility_version() {
  local version
  version="$(sed -n 's/.*"clientCompatibilityVersion"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$project_root/deploy/release-manifest.json" | head -n 1)"
  [[ "$version" =~ ^[1-9][0-9]*$ ]] || fail "发布清单缺少有效 clientCompatibilityVersion"
  echo "$version"
}

require_release_database_password() {
  local password app_password
  password="$(grep -E '^RELEASE_DB_PASSWORD=' "$env_file" | tail -n 1 | cut -d= -f2- || true)"
  app_password="$(grep -E '^APP_DB_PASSWORD=' "$env_file" | tail -n 1 | cut -d= -f2- || true)"
  [[ ${#password} -ge 32 ]] \
    || fail "deploy/.env 中的 RELEASE_DB_PASSWORD 必须设置且长度不少于 32 位"
  [[ "$password" != "$app_password" ]] \
    || fail "RELEASE_DB_PASSWORD 必须与 APP_DB_PASSWORD 不同"
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
    echo "16"
    return
  fi
  [[ "$replicas" =~ ^([1-9]|[12][0-9]|3[0-2])$ ]] \
    || fail "deploy/.env 中的 ${name} 必须是 1 至 32 的整数"
  echo "$replicas"
}

check_active_backtests() {
  local counts queued active
  counts="$(docker exec quant-backtest-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$APP_DB_NAME" -Atc "SELECT count(*) FILTER (WHERE status='"'"'queued'"'"'), count(*) FILTER (WHERE status IN ('"'"'running'"'"','"'"'cancelling'"'"')) FROM backtest_run;"')"
  queued="${counts%%|*}"
  active="${counts##*|}"
  echo "发布前回测任务：queued=${queued}, active=${active}"
  [[ "$queued" == "0" && "$active" == "0" ]]
}

stop_submission_and_wait_for_backtests() {
  local attempt
  echo "停止 backend，阻断新的页面和开放 API 回测提交"
  "${compose[@]}" stop frontend backend experiment-coordinator evaluation-worker evaluation-export-worker experiment-purge-worker signal-export-runner storage-probe
  for attempt in $(seq 1 360); do
    if check_active_backtests; then
      echo "回测队列已排空，可以开始迁移和同批发布"
      return
    fi
    sleep 5
  done
  fail "30 分钟内未能排空回测任务；backend 保持停止"
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

wait_for_application() {
  local port="$1" bar_replicas="$2" l2_replicas="$3" attempt health_url response
  health_url="http://127.0.0.1:${port}/api/health"
  for attempt in $(seq 1 45); do
    response="$(curl --fail --silent --show-error "$health_url" 2>/dev/null || true)"
    if runner_containers_ready "bar-runner" "$bar_replicas" \
      && runner_containers_ready "l2-runner" "$l2_replicas" \
      && runner_containers_ready "experiment-coordinator" "1" \
      && runner_containers_ready "storage-probe" "1" \
      && runner_containers_ready "evaluation-worker" "1" \
      && runner_containers_ready "evaluation-export-worker" "1" \
      && runner_containers_ready "experiment-purge-worker" "1" \
      && runner_containers_ready "signal-export-runner" "1" \
      && [[ "$response" == *'"status":"ok"'* ]] \
      && [[ "$response" == *'"signalExportRunner":{"healthy":true}'* ]] \
      && [[ "$response" == *'"evaluationExportWorker":{"healthy":true}'* ]] \
      && [[ "$response" == *'"observability":{"status":'* ]] \
      && [[ "$response" == *"\"applicationBuildId\":\"${APPLICATION_BUILD_ID}\""* ]] \
      && [[ "$response" == *"\"clientCompatibilityVersion\":${CLIENT_COMPATIBILITY_VERSION}"* ]]; then
      echo "发布完成，应用与 Runner 健康检查通过：$health_url"
      return
    fi
    sleep 2
  done
  "${compose[@]}" ps
  fail "应用或 Runner 未能在 90 秒内通过健康检查：$health_url"
}

wait_for_observability() {
  local attempt
  local check_code="import urllib.request; urls=('http://loki:3100/ready','http://alloy:12345/-/ready','http://docker-socket-proxy:2375/_ping'); [urllib.request.urlopen(url, timeout=3).read(64) for url in urls]"
  for attempt in $(seq 1 45); do
    if docker exec quant-backtest-container-event-observer \
      python -c "$check_code" >/dev/null 2>&1; then
      echo "Loki、Alloy 与 Docker socket proxy 内部健康检查通过"
      return
    fi
    sleep 2
  done
  "${compose[@]}" ps docker-socket-proxy loki alloy container-event-observer
  echo "警告：日志基础设施未能在 90 秒内通过内部健康检查" >&2
  return 1
}

wait_for_observability_services() {
  local attempt service health all_healthy
  for attempt in $(seq 1 45); do
    all_healthy=true
    for service in quant-backtest-docker-socket-proxy quant-backtest-loki quant-backtest-alloy; do
      health="$(docker inspect --format '{{.State.Health.Status}}' "$service" 2>/dev/null || true)"
      if [[ "$health" != "healthy" ]]; then
        all_healthy=false
        break
      fi
    done
    if [[ "$all_healthy" == "true" ]]; then
      echo "日志基础设施预检通过"
      return 0
    fi
    sleep 2
  done
  echo "警告：日志基础设施预检未通过" >&2
  return 1
}

verify_frontend_build() {
  local port="$1" temp_dir asset_path asset_digest
  temp_dir="$(mktemp -d)"
  curl --fail --silent --show-error \
    "http://127.0.0.1:${port}/build-info.json" -o "$temp_dir/build-info.json"
  grep -Fq "\"buildId\":\"${APPLICATION_BUILD_ID}\"" "$temp_dir/build-info.json" \
    || fail "对外 build-info.json 的 buildId 与目标构建不一致"
  grep -Fq "\"clientCompatibilityVersion\":${CLIENT_COMPATIBILITY_VERSION}" \
    "$temp_dir/build-info.json" \
    || fail "对外 build-info.json 的客户端兼容版本不一致"
  curl --fail --silent --show-error \
    "http://127.0.0.1:${port}/index.html" -o "$temp_dir/index.html"
  asset_path="$(grep -oE '/assets/[^\"]+\.js' "$temp_dir/index.html" | head -n 1)"
  [[ -n "$asset_path" ]] || fail "对外 index.html 未引用入口 JavaScript"
  curl --fail --silent --show-error \
    "http://127.0.0.1:${port}${asset_path}" -o "$temp_dir/entry.js"
  grep -Fq "$APPLICATION_BUILD_ID" "$temp_dir/entry.js" \
    || fail "实际入口 bundle 未包含目标 buildId"
  asset_digest="$(sha256sum "$temp_dir/entry.js" | cut -d' ' -f1)"
  rm -rf "$temp_dir"
  echo "frontend 实际入口验证通过：asset=${asset_path}, sha256=${asset_digest}"
}

verify_market_instrument_directory() {
  local counts active_count beijing_count code_920_count
  counts="$(docker exec quant-backtest-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$APP_DB_NAME" -Atc "SELECT count(*) FILTER (WHERE is_active=true AND instrument_type='"'"'stock'"'"'), count(*) FILTER (WHERE is_active=true AND instrument_type='"'"'stock'"'"' AND exchange='"'"'BJ'"'"'), count(*) FILTER (WHERE is_active=true AND instrument_type='"'"'stock'"'"' AND exchange='"'"'BJ'"'"' AND symbol LIKE '"'"'920%'"'"') FROM market_instrument;"')"
  IFS='|' read -r active_count beijing_count code_920_count <<< "$counts"
  [[ "$active_count" =~ ^[1-9][0-9]*$ ]] \
    || fail "证券目录验收失败：没有活跃股票"
  [[ "$beijing_count" =~ ^[1-9][0-9]*$ ]] \
    || fail "证券目录验收失败：没有活跃北交所股票"
  [[ "$code_920_count" =~ ^[1-9][0-9]*$ ]] \
    || fail "证券目录验收失败：没有 920 开头的活跃北交所股票"
  echo "证券目录验收通过：active=${active_count}, BJ=${beijing_count}, 920=${code_920_count}"
}

verify_release_admin_image() {
  local image_build_id image_client_version host_digest image_digest
  image_build_id="$(docker image inspect --format \
    '{{ index .Config.Labels "io.trend-grid.application-build-id" }}' \
    "$RELEASE_ADMIN_IMAGE")"
  image_client_version="$(docker image inspect --format \
    '{{ index .Config.Labels "io.trend-grid.client-compatibility-version" }}' \
    "$RELEASE_ADMIN_IMAGE")"
  [[ "$image_build_id" == "$APPLICATION_BUILD_ID" ]] \
    || fail "release-admin 镜像 buildId 与目标提交不一致"
  [[ "$image_client_version" == "$CLIENT_COMPATIBILITY_VERSION" ]] \
    || fail "release-admin 镜像客户端兼容版本不一致"
  host_digest="$(sha256sum "$project_root/deploy/release-manifest.json" | cut -d' ' -f1)"
  image_digest="$(docker run --rm --entrypoint sha256sum \
    "$RELEASE_ADMIN_IMAGE" /app/deploy/release-manifest.json | cut -d' ' -f1)"
  [[ "$image_digest" == "$host_digest" ]] \
    || fail "release-admin 镜像公告清单与目标提交不一致"
  docker run --rm --entrypoint python "$RELEASE_ADMIN_IMAGE" \
    -m backend.release_publish --help >/dev/null \
    || fail "release-admin 镜像命令入口不可用"
  echo "release-admin 镜像验证通过：image=${RELEASE_ADMIN_IMAGE}, manifest=${host_digest}"
}

verify_experiment_service_builds() {
  local service container_id build_id health
  for service in backend experiment-coordinator evaluation-worker \
    evaluation-export-worker storage-probe frontend; do
    container_id="$("${compose[@]}" ps -q "$service")"
    [[ -n "$container_id" ]] || fail "语义激活前缺少服务容器：${service}"
    build_id="$(docker inspect --format \
      '{{ index .Config.Labels "io.trend-grid.application-build-id" }}' \
      "$container_id")"
    [[ "$build_id" == "$APPLICATION_BUILD_ID" ]] \
      || fail "服务 ${service} 的 buildId 不一致：${build_id}"
    health="$(docker inspect --format \
      '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
      "$container_id")"
    [[ "$health" == "healthy" || "$health" == "running" ]] \
      || fail "服务 ${service} 当前不健康：${health}"
  done
}

validate_epoch_step() {
  local expected="$1" target="$2"
  case "${expected}:${target}" in
    legacy:lifecycle_v2|lifecycle_v2:evaluation_v2|evaluation_v2:rolling_window_v1) ;;
    *) fail "语义纪元只能单步推进：${expected} -> ${target}" ;;
  esac
}

activate_experiment_epoch() {
  local expected="$1" target="$2" actor="$3"
  local port health_url health_response active_build actual_epoch takeover_args
  validate_epoch_step "$expected" "$target"
  [[ -n "$actor" ]] || fail "语义激活必须提供操作者标识"
  active_build="$(docker exec quant-backtest-postgres sh -c \
    'psql -U "$POSTGRES_USER" -d "$APP_DB_NAME" -Atc "SELECT trim(current_build_id) FROM app_deployment_state WHERE singleton_key=true;"')"
  [[ "$active_build" == "$APPLICATION_BUILD_ID" ]] \
    || fail "当前部署 buildId=${active_build}，与工作区 ${APPLICATION_BUILD_ID} 不一致"
  actual_epoch="$(docker exec quant-backtest-postgres sh -c \
    'psql -U "$POSTGRES_USER" -d "$APP_DB_NAME" -Atc "SELECT contract_epoch FROM backtest_experiment_rollout_state WHERE singleton;"')"
  [[ "$actual_epoch" == "$expected" ]] \
    || fail "语义纪元冲突：预期 ${expected}，实际 ${actual_epoch}"
  verify_experiment_service_builds
  port="$(read_http_port)"
  health_url="http://127.0.0.1:${port}/api/health"
  health_response="$(curl --fail --silent --show-error "$health_url")"
  [[ "$health_response" == *"\"applicationBuildId\":\"${APPLICATION_BUILD_ID}\""* ]] \
    || fail "后端健康响应 buildId 不一致"
  [[ "$health_response" == *'"maximum":"rolling_window_v1"'* ]] \
    || fail "后端未声明支持 rolling_window_v1"
  [[ "$health_response" == *'"storagePressure":{"status":"open"'* ]] \
    || fail "存储压力信号未全部新鲜且开放"
  verify_frontend_build "$port"
  if [[ "$target" == "rolling_window_v1" ]]; then
    takeover_args=(python -m backend.experiments.takeover
      --build-id "$APPLICATION_BUILD_ID" --actor "$actor")
    "${compose[@]}" run --rm --no-deps experiment-coordinator \
      "${takeover_args[@]}" \
      || fail "活动 Revision 接管预检失败；保持 evaluation_v2"
  fi

  close_submission_gate
  release_activated=false
  trap report_incomplete_release EXIT
  "${compose[@]}" stop frontend backend experiment-coordinator \
    evaluation-worker evaluation-export-worker
  if [[ "$target" == "rolling_window_v1" ]]; then
    "${compose[@]}" run --rm --no-deps experiment-coordinator \
      python -m backend.experiments.takeover --apply \
      --build-id "$APPLICATION_BUILD_ID" --actor "$actor" \
      || fail "活动 Revision 接管失败；语义纪元未推进，保持提交门禁关闭"
  fi
  "${compose[@]}" --profile release-admin run --rm --no-deps release-admin \
    activate-experiment-epoch \
    --expected-epoch "$expected" --target-epoch "$target" \
    --build-id "$APPLICATION_BUILD_ID" --actor "$actor" \
    --detail-json '{"activationPath":"scripts/deploy-server.sh"}'
  if [[ "$target" == "rolling_window_v1" ]]; then
    "${compose[@]}" run --rm --no-deps experiment-coordinator \
      python -m backend.experiments.legacy_quota_cleanup --apply \
      --build-id "$APPLICATION_BUILD_ID" --actor "$actor" \
      || fail "旧计划额度清理失败；保持提交门禁关闭并执行前向修复"
  fi
  "${compose[@]}" start backend experiment-coordinator evaluation-worker \
    evaluation-export-worker frontend
  bar_runner_replicas="$(read_runner_replicas TREND_GRID_BAR_RUNNER_REPLICAS)"
  l2_runner_replicas="$(read_runner_replicas TREND_GRID_L2_RUNNER_REPLICAS)"
  wait_for_application "$port" "$bar_runner_replicas" "$l2_runner_replicas"
  health_response="$(curl --fail --silent --show-error "$health_url")"
  [[ "$health_response" == *"\"contractEpoch\":\"${target}\""* ]] \
    || fail "服务恢复后未读取到目标语义纪元 ${target}"
  release_activated=true
  open_submission_gate
  trap - EXIT
  echo "实验语义纪元已激活：${expected} -> ${target}，buildId=${APPLICATION_BUILD_ID}"
}

preserve_recovery_image() {
  local recovery_image="quant-backtest-release-admin:recovery-v1"
  if docker image inspect "$recovery_image" >/dev/null 2>&1; then
    echo "保留既有恢复镜像：${recovery_image}"
    return
  fi
  docker image tag "$RELEASE_ADMIN_IMAGE" "$recovery_image"
  echo "已建立恢复镜像基线：${recovery_image}"
}

[[ -f "$env_file" ]] || fail "缺少服务器环境文件：$env_file"
[[ -f "$clickhouse_compose_file" ]] || fail "缺少 ClickHouse 网络配置：$clickhouse_compose_file"
command -v docker >/dev/null || fail "未安装 Docker"
command -v curl >/dev/null || fail "未安装 curl"
command -v sha256sum >/dev/null || fail "未安装 sha256sum，无法校验发布镜像"

cd "$project_root"
[[ "$(git branch --show-current)" == "main" ]] || fail "当前分支不是 main"
[[ -z "$(git status --porcelain)" ]] || fail "工作区有未提交改动，请先处理后再发布"

if [[ "${1:-deploy}" == "activate-experiment-epoch" ]]; then
  [[ "$#" == "4" ]] \
    || fail "用法：deploy-server.sh activate-experiment-epoch <expected> <target> <actor>"
  require_release_database_password
  APPLICATION_BUILD_ID="$(git rev-parse HEAD)"
  CLIENT_COMPATIBILITY_VERSION="$(read_client_compatibility_version)"
  RELEASE_ADMIN_IMAGE="quant-backtest-release-admin:${APPLICATION_BUILD_ID}"
  export APPLICATION_BUILD_ID CLIENT_COMPATIBILITY_VERSION RELEASE_ADMIN_IMAGE
  compose=(docker compose --env-file "$env_file" -f "$compose_file" -f "$clickhouse_compose_file" --profile observability)
  "${compose[@]}" config >/dev/null || fail "Docker Compose 配置校验失败"
  verify_release_admin_image
  activate_experiment_epoch "$2" "$3" "$4"
  exit 0
fi
[[ "$#" == "0" || "${1:-}" == "deploy" ]] \
  || fail "未知命令：${1:-}"

script_digest_before="$(sha256sum "$project_root/scripts/deploy-server.sh" | cut -d' ' -f1)"
git fetch origin main
git pull --ff-only origin main
script_digest_after="$(sha256sum "$project_root/scripts/deploy-server.sh" | cut -d' ' -f1)"
if [[ "$script_digest_before" != "$script_digest_after" \
  && "${QUANT_DEPLOY_REEXECUTED:-}" != "1" ]]; then
  echo "部署脚本已更新，在同一发布锁内重新执行新版本"
  exec env QUANT_DEPLOY_LOCK_HELD=1 QUANT_DEPLOY_REEXECUTED=1 \
    bash "$project_root/scripts/deploy-server.sh" "${script_args[@]}"
fi

require_release_database_password
APPLICATION_BUILD_ID="$(git rev-parse HEAD)"
CLIENT_COMPATIBILITY_VERSION="$(read_client_compatibility_version)"
RELEASE_ADMIN_IMAGE="quant-backtest-release-admin:${APPLICATION_BUILD_ID}"
export APPLICATION_BUILD_ID CLIENT_COMPATIBILITY_VERSION RELEASE_ADMIN_IMAGE
deployment_id="$(cat /proc/sys/kernel/random/uuid)"
echo "目标构建：${APPLICATION_BUILD_ID}，客户端兼容版本：${CLIENT_COMPATIBILITY_VERSION}"

compose=(docker compose --env-file "$env_file" -f "$compose_file" -f "$clickhouse_compose_file" --profile observability)
bar_runner_replicas="$(read_runner_replicas TREND_GRID_BAR_RUNNER_REPLICAS)"
l2_runner_replicas="$(read_runner_replicas TREND_GRID_L2_RUNNER_REPLICAS)"
validate_clickhouse_network
"${compose[@]}" config >/dev/null || fail "Docker Compose 配置校验失败"
# 不依赖业务迁移的日志服务先预检，避免其故障发生在停服之后。
observability_preflight_ready=false
if "${compose[@]}" up -d --build docker-socket-proxy loki alloy \
  && wait_for_observability_services; then
  observability_preflight_ready=true
else
  echo "警告：可观测性未通过预检，将继续核心回测发布" >&2
fi

release_activated=false
trap report_incomplete_release EXIT
close_submission_gate
"${compose[@]}" up -d postgres
wait_for_postgres
stop_submission_and_wait_for_backtests
check_active_backtests || fail "回测任务未完全排空；backend 保持停止"
"${compose[@]}" --profile migration run --rm migrate
# 事件观察器需要迁移后的表结构；启动失败仅降级故障归因。
if [[ "$observability_preflight_ready" == "true" ]]; then
  if "${compose[@]}" up -d --build container-event-observer; then
    echo "容器事件观察器已启动"
  else
    observability_preflight_ready=false
    echo "警告：容器事件观察器启动失败，将继续核心回测发布" >&2
  fi
fi
"${compose[@]}" up -d --build --remove-orphans \
  --scale "bar-runner=$bar_runner_replicas" \
  --scale "l2-runner=$l2_runner_replicas" \
  backend bar-runner l2-runner experiment-coordinator evaluation-worker evaluation-export-worker experiment-purge-worker signal-export-runner storage-probe
"${compose[@]}" --profile release-admin build release-admin
verify_release_admin_image
# Nginx 在启动时解析 backend 容器地址；后端重建后必须刷新 frontend，避免保留旧地址返回 502。
"${compose[@]}" up -d --build --force-recreate frontend
verify_clickhouse_connectivity
"${compose[@]}" ps
http_port="$(read_http_port)"
wait_for_application "$http_port" "$bar_runner_replicas" "$l2_runner_replicas"
verify_frontend_build "$http_port"
verify_market_instrument_directory

generation="$(docker exec quant-backtest-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$APP_DB_NAME" -Atc "SELECT generation FROM app_deployment_state WHERE singleton_key=true;"')"
if [[ -z "$generation" ]]; then
  "${compose[@]}" --profile release-admin run --rm --no-deps release-admin \
    bootstrap --build-id "$APPLICATION_BUILD_ID" \
    --client-version "$CLIENT_COMPATIBILITY_VERSION" --deployment-id "$deployment_id"
  generation="0"
fi
"${compose[@]}" --profile release-admin run --rm --no-deps release-admin \
  activate --manifest /app/deploy/release-manifest.json \
  --build-id "$APPLICATION_BUILD_ID" --deployment-id "$deployment_id" \
  --expected-generation "$generation"
active_build="$(docker exec quant-backtest-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$APP_DB_NAME" -Atc "SELECT trim(current_build_id) FROM app_deployment_state WHERE singleton_key=true;"')"
[[ "$active_build" == "$APPLICATION_BUILD_ID" ]] || fail "发布激活后数据库构建标识不一致"
preserve_recovery_image
release_activated=true
open_submission_gate
echo "版本通知已激活：buildId=${APPLICATION_BUILD_ID}"
# 完整链路探针放在恢复业务之后，避免辅助服务延长停机窗口。
if [[ "$observability_preflight_ready" == "true" ]]; then
  if ! wait_for_observability; then
    observability_preflight_ready=false
  fi
fi
if [[ "$observability_preflight_ready" != "true" ]]; then
  echo "警告：可观测性未通过，核心回测服务已完成发布，日志与容器事件需单独修复" >&2
fi
