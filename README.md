# Trend Grid Viewer

趋势网格策略工作台，包含两个页面：

- 信号可视化：查看 5m K 线、策略信号、实际触发点和理论触发点。
- 策略回测：管理回测账号和标的，提交参数运行回测，查看收益摘要、净值曲线和交易明细。

## 目录结构

```text
viewer/
  backend/      FastAPI 后端，负责数据读取、MySQL 状态管理和回测 API
  frontend/     Vite + React 前端，负责可视化页面和回测页面
```

## 数据来源

5m K 线数据读取自：

```text
C:\Users\zhongying\code\data\bars\backtest_data\zigzag\5m
```

标的 `688981.SH` 对应文件示例：

```text
C:\Users\zhongying\code\data\bars\backtest_data\zigzag\5m\688981_SH\688981_SH_5m.csv
```

信号数据读取自：

```text
C:\Users\zhongying\code\strategies\trend_grid\data
```

后端配置位置：

```text
backend/settings.py
```

## MySQL

viewer 使用 MySQL 保存页面状态和回测记录，不保存 K 线大数据。

默认数据库：

```text
trend_grid_viewer
```

默认连接配置：

```text
host: 127.0.0.1
port: 3306
user: root
password: 123456
```

可通过环境变量覆盖：

```text
TREND_GRID_MYSQL_HOST
TREND_GRID_MYSQL_PORT
TREND_GRID_MYSQL_USER
TREND_GRID_MYSQL_PASSWORD
TREND_GRID_MYSQL_DATABASE
```

后端会自动创建数据库和基础表：

- `viewer_instruments`：自定义标的
- `viewer_accounts`：回测账号参数
- `viewer_backtest_runs`：回测运行记录和摘要

## 后端启动

进入后端目录：

```powershell
cd C:\Users\zhongying\code\strategies\trend_grid\viewer\backend
```

如果当前虚拟环境缺少 MySQL 驱动，先安装：

```powershell
python -m pip install PyMySQL==1.1.2
```

启动：

```powershell
python app.py
```

后端地址：

```text
http://127.0.0.1:8000
```

健康检查：

```text
GET http://127.0.0.1:8000/api/health
```

## 前端启动

进入前端目录：

```powershell
cd C:\Users\zhongying\code\strategies\trend_grid\viewer\frontend
```

启动：

```powershell
npm run dev
```

默认地址：

```text
http://127.0.0.1:5174
```

如果端口被占用，可以指定端口：

```powershell
npm run dev -- --port 5175 --host 127.0.0.1
```

## API 概览

基础接口：

- `GET /api/health`
- `GET /api/strategies`
- `GET /api/instruments`
- `GET /api/dates`
- `GET /api/chart`
- `GET /api/signal-files`
- `POST /api/data/sync`

账号和标的管理：

- `GET /api/accounts`
- `POST /api/accounts`
- `PUT /api/accounts/{id}`
- `DELETE /api/accounts/{id}`
- `POST /api/instruments`
- `PUT /api/instruments/{symbol}`
- `DELETE /api/instruments/{symbol}`

回测：

- `POST /api/backtest/run`

回测请求示例：

```json
{
  "accountId": 1,
  "account": "default",
  "symbol": "688981.SH",
  "strategy": "trend_grid",
  "startDate": "20260623",
  "endDate": "20260624",
  "initialCapital": 1000000,
  "commissionRate": 0.0003,
  "slippage": 0,
  "positionMode": "fixed_cash",
  "orderCash": 100000,
  "orderPct": null,
  "allowShort": true
}
```

## 当前能力

信号可视化页面：

- 选择标的、日期、策略和信号文件。
- 显示 5m K 线、成交量、收盘线。
- 显示 A/B 模块买卖 marker。
- 支持实际触发和理论触发对比。
- 支持自动刷新和同步 K 线。

策略回测页面：

- 添加和选择标的。
- 添加和选择回测账号。
- 可视化编辑初始资金、手续费、滑点、仓位模式、单笔金额、单笔比例、是否允许做空。
- 调用现有 `BacktestEngine` 运行同步回测。
- 展示总收益、最大回撤、胜率、交易次数、净值曲线和交易明细。
- 回测记录写入 MySQL。

## 已知边界

- K 线和信号仍读取 CSV 文件，不写入 MySQL。
- 手续费参数会保存到回测记录；当前引擎仍使用 `shared.cost` 中的全局成本常量。
- `allowShort=false` 暂未真正改变引擎行为，当前引擎仍按策略默认双向逻辑执行。
- 交易明细中的 `time` 字段当前由引擎输出为空，页面会显示 `-`。
- 回测接口目前是同步执行；如果后续回测耗时明显增加，建议改成任务式接口。

## 常见问题

### ModuleNotFoundError: No module named 'pymysql'

原因是当前 `.venv` 中没有安装 `PyMySQL`。

解决：

```powershell
python -m pip install PyMySQL==1.1.2
```

### 前端请求接口失败

确认后端已经启动在：

```text
http://127.0.0.1:8000
```

确认前端 Vite 代理配置在：

```text
frontend/vite.config.ts
```

### 回测提示没有 K 线数据

确认对应标的文件存在。例如：

```text
C:\Users\zhongying\code\data\bars\backtest_data\zigzag\5m\688981_SH\688981_SH_5m.csv
```
