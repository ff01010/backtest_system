# 回测系统服务器部署与维护说明

本文记录量化回测系统在服务器上的部署位置、端口、目录、启动顺序和常用排查方法。文中的密码只使用变量名表示，不能写入文档或命令历史。

## 1. 当前服务器信息

| 项目 | 当前值 |
| --- | --- |
| 服务器系统 | Ubuntu Linux |
| 项目代码目录 | `/opt/projects/quant_backtest_platform_git` |
| 运行方式 | Docker Compose |
| 业务数据库 | PostgreSQL 容器 |
| 行情数据库 | 共享 ClickHouse：`raw_historical_data` |
| 对外访问端口 | `28601` |
| 预留端口 | `28681`，当前未使用 |

## 2. 端口说明

### 已确认的端口状态

| 端口 | 状态 | 用途 |
| --- | --- | --- |
| `28601` | 检查时未被占用 | 本系统前端对外访问端口 |
| `28681` | 检查时未被占用 | 预留，当前不需要配置 |
| `5432` | 仅 Docker 内网使用 | PostgreSQL，不对宿主机开放 |
| `8000` | 仅 Docker 内网使用 | FastAPI 后端，不对宿主机开放 |
| `8001` | 仅 Docker 内网使用 | 策略执行 worker，不对宿主机开放 |

系统只需要一个对外端口。浏览器访问 Nginx 前端后，Nginx 会在 Docker 网络中把 API 请求转发到后端。因此不要把 `8000`、`8001` 或 `5432` 映射到服务器公网端口。

### 查看所有正在监听的端口

```bash
sudo ss -ltnp
```

### 检查指定端口

```bash
sudo ss -ltnp '( sport = :28601 or sport = :28681 )'
```

没有输出表示端口当前未被监听。若需要确认 `8080` 是被哪个容器占用，可执行：

```bash
docker ps --format 'table {{.Names}}\t{{.Ports}}\t{{.Status}}'
```

## 3. 目录与文件位置

### 项目目录

```text
/opt/projects/quant_backtest_platform_git
```

日常执行 Git、迁移和 Docker Compose 命令前，先进入该目录：

```bash
cd /opt/projects/quant_backtest_platform_git
```

### 环境文件

```text
/opt/projects/quant_backtest_platform_git/deploy/.env
```

此文件保存数据库密码、应用数据库账号和对外端口，不提交到 Git。权限应保持为仅当前用户可读：

```bash
chmod 600 deploy/.env
```

当前需要确认的配置项：

```dotenv
APP_HTTP_PORT=28601
TREND_GRID_HOST_KLINE_ROOT=/srv/quant-backtest/data/kline
TREND_GRID_HOST_SIGNAL_ROOT=/srv/quant-backtest/data/signals
```

不要在 `.env` 中保存或恢复旧的 `TREND_GRID_HOST_CODE_ROOT`。当前版本不依赖 `/srv/quant-backtest/code`、`shared/` 或外部策略代码目录。

### 宿主机数据目录

```text
/srv/quant-backtest/data/kline
/srv/quant-backtest/data/signals
```

- `kline`：旧 CSV 兼容目录；当前回测不读取该目录中的 K 线。
- `signals`：信号可视化可选 CSV 目录；可以为空。
- 策略脚本、策略方案、账户和回测记录保存在 PostgreSQL；每位用户的回测展示编号从 `#01` 独立递增；QMT 原始行情保存在 ClickHouse。

### Docker 数据卷

```text
quant-backtest-postgres-data   PostgreSQL 持久化数据
quant-backtest-artifacts       回测输出文件
```

不要随意删除这两个 Docker 卷。删除 PostgreSQL 数据卷会清空账号、策略、方案、回测记录和已缓存行情。

## 4. 容器职责

| 容器名 | Compose 服务 | 作用 |
| --- | --- | --- |
| `quant-backtest-postgres` | `postgres` | 保存用户、策略、方案和回测记录 |
| `quant-backtest-backend` | `backend` | 登录、权限、QMT 覆盖检查、回测编排和 API |
| `quant-backtest-platform-bar-runner-*` | `bar-runner` | 领取并执行标准 Bar 回测任务 |
| `quant-backtest-platform-l2-runner-*` | `l2-runner` | 领取并执行 L2 盘口回测任务 |
| `quant-backtest-frontend` | `frontend` | Nginx 前端页面和 API 反向代理 |

策略 worker 没有数据库密码、没有宿主机目录挂载，并且不对外暴露端口；容器使用非 root 用户、只读文件系统、受限临时目录和内部网络。Bar/L2 的用户策略子进程也会清空环境并在 Linux 下主动降权，但目前仍与父 Runner 共享容器文件系统和网络命名空间，因此这是兼容与降权加固，不是完整安全沙箱。

## 5. 首次部署或升级步骤

### 推荐：使用一键发布脚本

日常升级应优先使用项目内置脚本。脚本会校验当前为 `main` 分支且工作区没有未提交改动，然后依次拉取最新代码、在脚本自身发生更新时于同一发布锁内重新执行、启动并等待 PostgreSQL 健康、执行数据库迁移和交易日历同步、重建并启动策略执行器、后端和前端，最后请求健康检查接口确认发布结果。

```bash
cd /opt/projects/quant_backtest_platform_git
bash scripts/deploy-server.sh
```

首次启用发布通知前，服务器私有 `deploy/.env` 必须增加独立发布角色密码。该密码不得与 `APP_DB_PASSWORD` 相同，长度不得少于 32 位，也不得提交到 Git：

```bash
openssl rand -hex 32
nano deploy/.env
```

在 `deploy/.env` 中保存为 `RELEASE_DB_PASSWORD=<生成值>`。生成值不得复制到工单、文档或聊天记录。

脚本会再次执行 `git pull --ff-only origin main`；即使已手动拉取，重复执行也是安全的。若工作区有本地未提交修改，脚本会终止发布，需先确认并处理这些修改，不能直接覆盖。正常发布会构建 `quant-backtest-release-admin:<完整 Git SHA>`，校验镜像内的构建标识、客户端兼容版本和公告清单，再执行激活。`quant-backtest-release-admin:recovery-v1` 只在首次不存在时建立，日常发布不得覆盖。

从不具备脚本自更新重启能力的旧版本首次升级时，旧进程仍可能按旧逻辑完成第一次运行。若输出中没有 `release-admin 镜像验证通过`，应在确认第一次命令正常结束后再执行一次相同命令；后续版本更新由脚本在同一发布锁内自动重启，不再需要重复执行。

### 发布前生成公告草稿

发布分支合并前，在开发工作区根据相对于 `origin/main` 的提交生成公告草稿：

```bash
python scripts/generate_release_manifest.py --base origin/main --target HEAD
```

生成器默认把 `feat` 和 `fix` 提交整理为用户可读条目，忽略 `docs`、`test`、`refactor`、`chore`、`build` 和 `ci`。需要改写或排除时，在提交正文使用：

```text
Release-Note: 用户可读的更新说明
Release-Level: important
```

或：

```text
Release-Note: skip
```

生成的 `deploy/release-manifest.json` 必须人工检查并随发布 MR 提交。没有用户可感知改动时生成器会设置 `notifyUsers=false`。`mandatory` 发布必须显式使用 `Release-Level: mandatory`，并通过 `--client-compatibility-version` 递增客户端兼容版本。

交易日历同步优先读取新浪财经的上交所交易日历文件；单个来源最多请求 3 次，并在新浪连续失败后回退到东方财富上证指数日线。两个来源都无法访问时发布会终止，以免写入不完整日历；此时应先检查服务器容器的 HTTPS 出网和 DNS，再重试发布。

### 手工发布步骤

### 第一步：更新主分支代码

```bash
cd /opt/projects/quant_backtest_platform_git
git status --short
git pull --ff-only origin main
```

`git status --short` 必须没有输出后再拉取。若有输出，先确认是否存在服务器本地修改，不能直接覆盖。

### 第二步：确认数据库健康

```bash
docker inspect --format '{{.State.Health.Status}}' quant-backtest-postgres
```

输出应为：

```text
healthy
```

### 第三步：执行数据库迁移

```bash
docker compose --env-file deploy/.env -f deploy/compose.infrastructure.yml --profile migration run --rm migrate
```

迁移可以重复执行。已有表、索引或列显示 `already exists, skipping` 是正常提示；命令最终必须正常结束，不能出现 `ERROR` 或事务回滚。

本次运行时兼容改造需要成功执行 `025_strategy_runtime_error_stats.sql`。该迁移只创建无用户、任务和源码关联的脱敏统计表，不改变回测记录及其错误日志的级联删除关系。

### 第四步：重建并启动应用

```bash
docker compose --env-file deploy/.env -f deploy/compose.infrastructure.yml -f deploy/compose.clickhouse.yml up -d --build --remove-orphans --scale bar-runner=12 --scale l2-runner=12 backend bar-runner l2-runner frontend
```

首次拉取前端 Node 镜像或执行 `npm ci` 时，构建可能需要几分钟。

### 第五步：检查容器状态

```bash
docker compose --env-file deploy/.env -f deploy/compose.infrastructure.yml ps
```

期望结果：

- PostgreSQL、策略 worker、后端、Bar Runner、L2 Runner、前端均为 `Up`。
- PostgreSQL、策略 worker 和后端应显示 `healthy`。
- 前端端口应显示 `0.0.0.0:28601->80/tcp` 或等效输出。

## 6. 启动失败：端口被占用

如果前端启动报错：

```text
Bind for :::8080 failed: port is already allocated
```

说明 `.env` 仍配置为 `8080`，而该端口已被其他服务使用。将端口改为 `28601`：

```bash
sed -i 's/^APP_HTTP_PORT=.*/APP_HTTP_PORT=28601/' deploy/.env
```

然后重新启动前端：

```bash
docker compose --env-file deploy/.env -f deploy/compose.infrastructure.yml up -d frontend
```

如果 `28601` 后续也被占用，先使用 `ss` 检查，再选择一个未占用的高位端口，修改同一配置项后重新启动前端。

## 7. 访问与首次验证

浏览器访问：

```text
http://服务器地址:28601
```

首次会进入登录页。默认管理员：

```text
用户名：admin
密码：123456
```

登录后应立即重置管理员密码。系统不开放自主注册，管理员通过“人员管理”创建其他账号。

建议按以下顺序验证：

1. 打开登录页面并使用管理员登录。
2. 确认可看到“人员管理”。
3. 确认管理员策略库中有“均线示例策略”。
4. 新建一个普通测试用户，确认该用户登录后看不到管理员的策略。
5. 保存策略并运行一段小范围回测。
6. 确认回测使用已导入的 QMT 数据；选择未覆盖区间时应明确提示缺失数据。

## 8. 常用维护命令

### 查看所有容器

```bash
docker compose --env-file deploy/.env -f deploy/compose.infrastructure.yml ps
```

### 查看后端日志

```bash
docker compose --env-file deploy/.env -f deploy/compose.infrastructure.yml logs --tail=100 backend
```

### 查看 Runner 日志

```bash
docker compose --env-file deploy/.env -f deploy/compose.infrastructure.yml logs --tail=100 bar-runner l2-runner
```

### 查看前端日志

```bash
docker compose --env-file deploy/.env -f deploy/compose.infrastructure.yml logs --tail=100 frontend
```

### 只重启前端

```bash
docker compose --env-file deploy/.env -f deploy/compose.infrastructure.yml up -d frontend
```

### 重建并重启全部应用容器

```bash
docker compose --env-file deploy/.env -f deploy/compose.infrastructure.yml -f deploy/compose.clickhouse.yml up -d --build --remove-orphans --scale bar-runner=12 --scale l2-runner=12 backend bar-runner l2-runner frontend
```

## 9. 注意事项

- 不要把 `deploy/.env` 提交到 Git，也不要把数据库密码贴到聊天记录或文档中。
- 不要删除 PostgreSQL 数据卷，除非确认要清空全部系统数据。
- 每次拉取包含新 `db/migrations/*.sql` 的代码后，都要执行一次迁移命令。
- `TREND_GRID_STRATEGY_RUNTIME_ERROR_RETENTION_DAYS` 控制平台运行环境错误聚合保留期，默认 180 天；统计只含错误码、运行时版本、标准内置名称和时间。
- `8080` 已被其他服务占用，本系统固定使用 `28601`。
- `28681` 当前未使用，不需要为后端或 worker 单独开放。
- 回测不会在运行时向东财、新浪或其他外部行情接口拉取数据；缺失数据必须先导入 QMT 原始文件。

## 10. QMT 原始历史数据（ClickHouse）

### 当前状态

服务器已创建 ClickHouse 数据库 `raw_historical_data`，用于保存本地 QMT 下载的原始历史 K 线。该数据库与回测系统的 PostgreSQL 业务库分离。

本版本后端通过 Docker 内网读取这些表。PostgreSQL 只保留用户、策略、账户和回测记录，不再承担分钟行情缓存或运行时外部拉取。

### 数据库与表

```text
raw_historical_data
├─ qmt_kline
├─ qmt_import_batch
└─ qmt_kline_coverage
```

| 表名 | 用途 |
| --- | --- |
| `qmt_kline` | 保存 QMT 返回的原始 K 线，可同时保存 `1m`、`5m`、`15m`、`30m`、`60m`、`1d` 等周期。 |
| `qmt_import_batch` | 记录每个 QMT 文件的校验值、导入范围、导入行数、QMT 客户端版本和执行结果。 |
| `qmt_kline_coverage` | 按标的、周期、交易日记录数据完整度，供回测提交前快速判断数据是否覆盖。 |

### `qmt_kline` 原始字段约定

QMT 原始返回为 DataFrame。分钟线与日线均含 11 个字段，导入时保留原始字段名与数据类型，不转换为另一套统一行情表。

| 字段 | 含义 |
| --- | --- |
| `symbol` | QMT 标的代码，例如 `688153.SH`。 |
| `period` | QMT 周期，例如 `1m`、`5m`、`1d`。 |
| `qmt_index` | QMT DataFrame 原始索引；分钟线示例为 `20260730093000`，日线示例为 `20260730`。 |
| `time` | QMT 原始毫秒时间戳。 |
| `open`、`high`、`low`、`close` | 开、高、低、收价格，保留 QMT 的 `Float64` 类型。 |
| `volume`、`amount` | 成交量和成交额，保留 QMT 原始数值及单位。 |
| `settelementPrice`、`openInterest`、`preClose`、`suspendFlag` | QMT 返回的原始扩展字段，不得在导入时丢弃或改名。 |
| `import_batch_id` | 关联到 `qmt_import_batch` 的导入批次。 |
| `ingested_at`、`ingest_version` | 写入时间和覆盖版本；重复导入同一根 K 线时保留最新版本。 |

表按月分区，排序键为：

```text
(symbol, period, time)
```

### 数据完整度与读取规则

`qmt_kline_coverage` 以 `symbol + period + trade_date` 为一条记录，保存首末时间、期望行数、实际行数、有效行数和状态。状态仅使用：

```text
complete
partial
empty
failed
```

回测只读取 `qmt_kline`；提交回测前先查 `qmt_kline_coverage`。数据不完整时必须提示缺失标的、周期和日期区间，不允许在回测过程中临时联网拉取行情。

QMT 直接原始周期为 `1m`、`5m`、`60m`、`1d`；`15m`、`30m` 由 `1m` 原始数据按 QMT 的区间结束时间规则聚合生成，不写回原始表。

### 导入 QMT 原始文件

本地导出目录约定为：

```text
qmt_raw/{period}/{代码_SH或SZ}/{代码_SH或SZ}_{period}_raw.csv
```

服务器接收目录为：

```text
/data/projects/quant-backtest/imports/qmt
```

先将本地 `qmt_raw` 目录上传到上述目录。然后在服务器项目目录执行导入脚本；密码仅通过当前 Shell 环境变量传入：

```bash
read -rs -p 'ClickHouse default 密码: ' QMT_CLICKHOUSE_PASSWORD; echo
export QMT_CLICKHOUSE_PASSWORD
python3 scripts/import_qmt_history.py \
  --input-root /data/projects/quant-backtest/imports/qmt \
  --symbols 688525.SH,688449.SH,688008.SH,688981.SH,688126.SH,600584.SH
unset QMT_CLICKHOUSE_PASSWORD
```

首次正式导入前，先追加 `--dry-run` 验证 CSV 字段、行数和目录结构。导入完成后，通过 `qmt_import_batch` 和 `qmt_kline_coverage` 复核批次和覆盖状态。

### 安全与运维要求

- 后端必须使用 `deploy/compose.clickhouse.yml` 接入服务器现有 `quant-net`，通过 `http://quant-clickhouse:8123` 访问 ClickHouse；不新增行情数据对外端口。
- 当前阶段复用 ClickHouse `default` 管理员账号连接 `raw_historical_data`；该账号权限覆盖共享 ClickHouse 的其他数据库，凭据必须仅保存在服务器 `deploy/.env`，不得提交、外传或写入文档。后续具备条件时应改为仅授予 `raw_historical_data.*` 访问权限的专用账号。
- QMT 原始文件先上传至热盘导入目录，成功导入并校验后再归档；不得直接修改 ClickHouse Docker 数据卷中的文件。
- 任何导入脚本、环境文件和文档都不得记录 ClickHouse 明文密码。
