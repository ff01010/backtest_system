# 受控策略脚本编写规则

本系统的自定义策略在单次回测内以 `on_bar(bar, state)` 执行。每根**已完成** K 线调用一次；当前成交规则固定为“本 Bar 收盘产生信号，并按该 Bar 收盘价虚拟成交”。这不同于部分 QMT 策略的“下一根 Bar 开盘成交”，两者不能直接对比结果。

## 最小可运行示例

```python
def on_bar(bar, state):
  # state 会在本次回测期间持续存在，用于保存跨 Bar 信息。
  state['bars_seen'] = state.get('bars_seen', 0) + 1

  if bar['position'] == 0 and bar['close'] > bar['open']:
    return [{
      'action': 'buy_open',
      'quantity': 100,
      'reason': '阳线开多',
    }]
  if bar['position'] > 0 and bar['close'] < bar['open']:
    return [{
      'action': 'sell_close',
      'quantity': bar['position'],
      'reason': '阴线平多',
    }]
  return []
```

## 回调与可读数据

脚本必须且只能定义 `on_bar(bar, state)`。返回值必须是订单字典列表或 `None`。

- `bar['time']`：当前已完成 K 线的时间。
- `bar['open']`、`high`、`low`、`close`、`volume`：当前 K 线字段。
- `bar['cash']`、`equity`：当前账户现金和权益。
- `bar['position']`：当前脚本自己的净持仓；多头为正，空头为负。
- `bar['params']`：回测页面提交的策略参数。
- `state`：可读写字典，只在本次回测持续保存。跨 Bar 的计数、价格或信号状态必须写入这里。

回调只会得到当前 Bar 和已累积账户状态，不提供未来 K 线。不得通过预先写入常量、外部文件或网络数据变相读取未来信息。

## 订单动作

每个订单均为字典，必须含有 `action` 与正整数 `quantity`；`reason` 可选。

| action | 含义 |
| --- | --- |
| `buy_open` | 买入开多 |
| `sell_open` | 卖出开空 |
| `sell_close` | 卖出平多 |
| `buy_close` | 买入平空 |

`quantity` 必须是 Python 整数且大于零；布尔值、浮点数和字符串都不接受。平仓数量不能超过当前可平仓持仓，否则由回测内核拒绝该订单。

## 方向权限

策略应根据自己的市场逻辑返回 `buy_open` 或 `sell_open`，不需要在代码中读取或判断 `allowLong（允许做多）`、`allowShort（允许做空）`。策略方案默认允许做多和做空；页面关闭某一方向后，系统会跳过该方向的**开仓**信号，并在信号明细标记“已跳过”。

方向权限不会把空头信号自动变成多头信号。若策略没有产生 `buy_open`，即使允许做多也不会凭空开多。

跨 Bar 状态应以 `bar['position']` 的实际持仓为准；不要在未确认成交前把 `state` 中的自定义仓位标记为已开仓。

## 运行周期和数据

策略方案保存 `timeframe（运行周期）`。当前回测引擎支持 `1m` 与 `5m`；只有本地存在该标的文件且所选日期范围被覆盖时才可运行。新建模板会标明当前周期。

不要把依赖 1 分钟频率的 QMT 逻辑直接放到 5 分钟方案中，也不要把“下一根开盘成交”的假设写入当前模型。成交时点改造属于独立执行模型工作。

## 模块导入与受限能力

内部使用的策略脚本可以使用标准 Python 导入语法，例如：

```python
import math
from statistics import mean
```

模块必须已安装在回测后端当前 Python 环境中；系统不会在保存或运行策略时安装依赖。导入模块后，策略仍须遵守 `on_bar(bar, state)` 回调、订单动作和数量校验。

为避免动态执行代码，脚本仍禁止直接调用：

- `open`、`eval`、`exec`、`compile`、`input`；
- 双下划线名称或属性访问；

保存时会校验回调签名、可静态识别的订单动作和数量；运行时会再次校验每一笔订单。
