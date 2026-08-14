"""可粘贴到自定义策略编辑器的 V6 分批建仓 L2 模板。

该文件不直接由服务端导入；保存为 `on_bar` 脚本后由隔离 worker 执行。
"""

from datetime import datetime, timedelta


TOTAL_POSITION_QTY = 18_000
OPENING_POSITION_QTY = 200
STAGE_QTYS = (400, 800, 1_500, 2_900)
REVERSE_QTY = 6_000
REVERSE_EXIT_QTY = 2_900
FORCE_CLOSE_HHMM = 1456


def _state(state):
  return state.setdefault('v6', {
    'day': '',
    'openingSubmitted': False,
    'stageSignals': [],
    'forwardQty': 0,
    'forwardAmount': 0.0,
    'shortQty': 0,
    'shortAmount': 0.0,
    'pending': {},
    'breachEnd': '',
    'reverseTrigger': 0.0,
    'reverseRequested': False,
    'reverseOffsetQty': 0,
    'reverseOpenedQty': 0,
    'reverseExitRequested': False,
    'manualTakeover': False,
    'forceCloseSubmitted': False,
    'closes': [],
  })


def _order(action, quantity, reason, key):
  return {
    'action': action,
    'quantity': quantity,
    'reason': reason,
    'clientOrderKey': key,
  }


def _minute_end(value):
  return (datetime.strptime(value, '%Y%m%d%H%M%S').replace(second=0) + timedelta(minutes=1)).strftime('%Y%m%d%H%M')


def _reset_day(item, trade_date):
  if item['day'] == trade_date:
    return
  item.update({
    'day': trade_date,
    'openingSubmitted': False,
    'stageSignals': [],
    'forwardQty': 0,
    'forwardAmount': 0.0,
    'shortQty': 0,
    'shortAmount': 0.0,
    'pending': {},
    'breachEnd': '',
    'reverseTrigger': 0.0,
    'reverseRequested': False,
    'reverseOffsetQty': 0,
    'reverseOpenedQty': 0,
    'reverseExitRequested': False,
    'manualTakeover': False,
    'forceCloseSubmitted': False,
    'closes': [],
  })


def _has_pending(item, prefix):
  return any(key.startswith(prefix) for key in item['pending'])


def _forward_average(item):
  return item['forwardAmount'] / item['forwardQty'] if item['forwardQty'] else 0.0


def on_quote(quote, state):
  """QMT on_tick 的 3 秒快照等价回调。"""
  price = float(quote.get('lastPrice') or 0)
  if price <= 0:
    return []
  item = _state(state)
  _reset_day(item, quote['tradeDate'])
  if item['manualTakeover']:
    return []
  actions = []
  if not item['openingSubmitted'] and quote['hhmm'] < FORCE_CLOSE_HHMM:
    item['openingSubmitted'] = True
    actions.append(_order('buy_open', OPENING_POSITION_QTY, 'V6 开盘 1% 建仓', 'opening'))
    return actions
  if item['forwardQty'] > 0:
    take_profit = _forward_average(item) * 1.01
    stop_price = item['stageSignals'][3] if len(item['stageSignals']) >= 4 else 0.0
    if price >= take_profit or (stop_price and price <= stop_price) or quote['hhmm'] >= FORCE_CLOSE_HHMM:
      if not item['forceCloseSubmitted'] and not _has_pending(item, 'forward-close'):
        item['forceCloseSubmitted'] = quote['hhmm'] >= FORCE_CLOSE_HHMM
        actions.append(_order('sell_close', item['forwardQty'], 'V6 前向仓止盈止损或尾盘平仓', 'forward-close'))
      return actions
  if item['reverseRequested'] and quote['hhmm'] >= FORCE_CLOSE_HHMM:
    return []
  if item['reverseTrigger'] and not item['reverseRequested'] and price <= item['reverseTrigger']:
    item['breachEnd'] = _minute_end(quote['time'])
  if item['reverseRequested']:
    remaining = REVERSE_QTY - item['reverseOffsetQty'] - item['reverseOpenedQty']
    if remaining > 0 and not _has_pending(item, 'reverse-'):
      if item['forwardQty'] > 0:
        actions.append(_order('sell_close', min(item['forwardQty'], remaining), 'V6 反手先平多', 'reverse-offset'))
      else:
        actions.append(_order('sell_open', remaining, 'V6 33% 反手开空', 'reverse-open'))
  if item['reverseExitRequested'] and item['shortQty'] > 0 and not _has_pending(item, 'reverse-exit'):
    actions.append(_order('buy_close', min(REVERSE_EXIT_QTY, item['shortQty']), 'V6 原第四阶段信号反手退出', 'reverse-exit'))
  return actions


def on_bar(bar, state):
  """只使用已完成 1 分钟 Bar 确认顶底和反手收盘条件。"""
  item = _state(state)
  _reset_day(item, bar['tradeDate'])
  closes = item['closes']
  closes.append(float(bar['close']))
  if len(closes) > 3:
    closes.pop(0)
  if len(closes) < 3 or item['manualTakeover']:
    return []
  a_close, b_close, c_close = closes
  is_peak = a_close <= b_close and b_close > c_close
  is_valley = a_close >= b_close and b_close < c_close
  if item['reverseTrigger'] and not item['reverseRequested']:
    if item['breachEnd'] == bar['time'][:12] and c_close <= item['reverseTrigger']:
      item['reverseRequested'] = True
      return []
  if item['reverseRequested']:
    if is_valley and item['shortQty'] > 0 and not item['reverseExitRequested']:
      item['reverseExitRequested'] = True
    return []
  if not is_valley or item['forwardQty'] <= 0 or len(item['stageSignals']) >= len(STAGE_QTYS):
    return []
  if item['stageSignals'] and b_close >= item['stageSignals'][-1]:
    return []
  stage_index = len(item['stageSignals'])
  key = f'stage-{stage_index + 1}'
  if _has_pending(item, key):
    return []
  item['pendingSignal'] = b_close
  return [_order('buy_open', STAGE_QTYS[stage_index], f'V6 第 {stage_index + 1} 阶段低点建仓', key)]


def on_order_event(event, state):
  """仅以真实成交回报推进阶段与反手状态，拒单/撤单会释放对应目标。"""
  item = _state(state)
  key = event.get('clientOrderKey')
  if not key:
    return
  if event['type'] == 'accepted':
    item['pending'][key] = {'filled': 0, 'quantity': int(event['quantity'])}
    return
  pending = item['pending'].get(key)
  if event['type'] in ('rejected', 'cancelled'):
    item['pending'].pop(key, None)
    return
  if event['type'] not in ('partial_fill', 'filled') or pending is None:
    return
  quantity = int(event['quantity'])
  price = float(event['price'])
  pending['filled'] += quantity
  if key == 'opening' or key.startswith('stage-'):
    item['forwardQty'] += quantity
    item['forwardAmount'] += quantity * price
  elif key == 'forward-close' or key == 'reverse-offset':
    item['forwardQty'] = max(0, item['forwardQty'] - quantity)
    if item['forwardQty'] == 0:
      item['forwardAmount'] = 0.0
    if key == 'reverse-offset':
      item['reverseOffsetQty'] += quantity
  elif key == 'reverse-open':
    item['shortQty'] += quantity
    item['shortAmount'] += quantity * price
    item['reverseOpenedQty'] += quantity
  elif key == 'reverse-exit':
    item['shortQty'] = max(0, item['shortQty'] - quantity)
    if item['shortQty'] == 0:
      item['shortAmount'] = 0.0
  if event['type'] != 'filled':
    return
  item['pending'].pop(key, None)
  if key.startswith('stage-'):
    item['stageSignals'].append(float(item.pop('pendingSignal', price)))
    if len(item['stageSignals']) == 3:
      item['reverseTrigger'] = item['stageSignals'][2]
  if key == 'reverse-exit':
    item['manualTakeover'] = True
