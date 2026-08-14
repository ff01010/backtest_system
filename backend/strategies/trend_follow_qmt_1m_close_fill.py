"""可粘贴到回测页面“新建策略”的 1m 双向趋势跟随脚本。

迁移来源：trend_follow_qmt_builtin_backtest.py。策略只产生多空信号，方向权限
由回测方案统一控制；开仓、止损和止盈均按满足条件的当前 Bar 收盘价成交。
"""

OPEN_FIXED_QTY = 1200
MIN_OPEN_QTY = 200
TAKE_PROFIT_FIRST_GAP = 2.4
TAKE_PROFIT_SECOND_GAP = 4.8
TAKE_PROFIT_MIN_QTY = 200
BREAK_EVEN_BAR_COUNT = 3
STOP_ANCHOR_MAX_GAP = 0.8
MORNING_FORCE_CLOSE_TIME = 1129
FORCE_CLOSE_TIME = 1455
FINAL_CLOSE_TIME = 1457
TAIL_CLOSE_BATCH_QTY = 200


def _new_side_state():
  return {
    'position': 0,
    'stop_price': 0.0,
    'profit_base': 0.0,
    'signal_bar_key': '',
    'first_target_qty': 0,
    'first_target_done': False,
    'entry_price_amount': 0.0,
    'entry_price_qty': 0,
    'break_even_applied': False,
  }


def _reset_day(state, trade_date, open_price):
  params = state.get('params', {})
  state.clear()
  state['params'] = params
  state['trade_date'] = trade_date
  state['prev_close'] = open_price
  state['pending_peak'] = 0.0
  state['pending_valley'] = 0.0
  state['peak_chain'] = 0.0
  state['valley_chain'] = 0.0
  state['processed_bar_keys'] = []
  state['short'] = _new_side_state()
  state['long'] = _new_side_state()
  state['morning_force_done'] = False
  state['afternoon_force_done'] = False
  state['final_close_done'] = False


def _is_trade_time(hhmm):
  return (930 <= hhmm < MORNING_FORCE_CLOSE_TIME) or (1300 <= hhmm < FORCE_CLOSE_TIME)


def _round_lot(quantity):
  return max(0, int(quantity / 100) * 100)


def _stop_anchor(side, extremum, confirm_close):
  if side == 'short' and extremum - confirm_close > STOP_ANCHOR_MAX_GAP:
    return confirm_close + STOP_ANCHOR_MAX_GAP
  if side == 'long' and confirm_close - extremum > STOP_ANCHOR_MAX_GAP:
    return confirm_close - STOP_ANCHOR_MAX_GAP
  return extremum


def _close_action(side):
  return 'buy_close' if side == 'short' else 'sell_close'


def _open_action(side):
  return 'sell_open' if side == 'short' else 'buy_open'


def _close_side(side_state, side, quantity, reason, orders):
  close_quantity = min(max(0, quantity), side_state['position'])
  if close_quantity <= 0:
    return
  side_state['position'] -= close_quantity
  orders.append({
    'action': _close_action(side),
    'quantity': close_quantity,
    'reason': reason,
  })
  if side_state['position'] == 0:
    side_state.clear()
    side_state.update(_new_side_state())


def _open_side(state, side, bar, anchor, orders):
  side_state = state[side]
  if side_state['position'] > 0 or OPEN_FIXED_QTY < MIN_OPEN_QTY:
    return
  side_state.update(_new_side_state())
  side_state['position'] = OPEN_FIXED_QTY
  side_state['stop_price'] = anchor
  side_state['profit_base'] = bar['close']
  side_state['signal_bar_key'] = bar['time']
  side_state['entry_price_amount'] = bar['close'] * OPEN_FIXED_QTY
  side_state['entry_price_qty'] = OPEN_FIXED_QTY
  orders.append({
    'action': _open_action(side),
    'quantity': OPEN_FIXED_QTY,
    'reason': 'lower_peak_open_short' if side == 'short' else 'higher_valley_open_long',
  })


def _bars_after_signal(processed_bar_keys, signal_bar_key):
  count = 0
  for bar_key in processed_bar_keys:
    if bar_key > signal_bar_key:
      count += 1
  return count


def _check_exit_rules(state, bar, orders):
  for side in ('short', 'long'):
    side_state = state[side]
    if side_state['position'] <= 0:
      continue
    stop_hit = bar['high'] >= side_state['stop_price'] if side == 'short' else bar['low'] <= side_state['stop_price']
    if side_state['stop_price'] > 0 and stop_hit:
      _close_side(side_state, side, side_state['position'], 'stop', orders)
      continue
    direction = -1 if side == 'short' else 1
    profit_base = side_state['profit_base']
    close_price = bar['close']
    second_hit = close_price <= profit_base - TAKE_PROFIT_SECOND_GAP if side == 'short' else close_price >= profit_base + TAKE_PROFIT_SECOND_GAP
    first_hit = close_price <= profit_base - TAKE_PROFIT_FIRST_GAP if side == 'short' else close_price >= profit_base + TAKE_PROFIT_FIRST_GAP
    if second_hit:
      _close_side(side_state, side, side_state['position'], 'take_profit_2', orders)
    elif first_hit and not side_state['first_target_done']:
      side_state['first_target_qty'] = side_state['first_target_qty'] or _round_lot(side_state['position'] // 2)
      quantity = min(side_state['position'], side_state['first_target_qty'])
      if quantity >= TAKE_PROFIT_MIN_QTY:
        _close_side(side_state, side, quantity, 'take_profit_1', orders)
        side_state['first_target_done'] = True


def _apply_break_even_stop(state):
  for side in ('short', 'long'):
    side_state = state[side]
    if side_state['position'] <= 0 or side_state['break_even_applied']:
      continue
    if _bars_after_signal(state['processed_bar_keys'], side_state['signal_bar_key']) < BREAK_EVEN_BAR_COUNT:
      continue
    if side_state['entry_price_qty'] <= 0:
      continue
    side_state['stop_price'] = side_state['entry_price_amount'] / side_state['entry_price_qty']
    side_state['break_even_applied'] = True


def _confirm_peak(state, bar, orders):
  peak_price = state['pending_peak']
  if peak_price > 0 and bar['close'] < peak_price:
    if state['peak_chain'] > 0 and peak_price < state['peak_chain']:
      _open_side(state, 'short', bar, _stop_anchor('short', peak_price, bar['close']), orders)
    state['peak_chain'] = peak_price
    state['pending_peak'] = 0.0


def _confirm_valley(state, bar, orders):
  valley_price = state['pending_valley']
  if valley_price > 0 and bar['close'] > valley_price:
    if state['valley_chain'] > 0 and valley_price > state['valley_chain']:
      _open_side(state, 'long', bar, _stop_anchor('long', valley_price, bar['close']), orders)
    state['valley_chain'] = valley_price
    state['pending_valley'] = 0.0


def _update_candidates(state, close_price):
  state['pending_peak'] = close_price if state['prev_close'] < close_price else 0.0
  state['pending_valley'] = close_price if state['prev_close'] > close_price else 0.0
  state['prev_close'] = close_price


def _force_close(state, hhmm, orders):
  if hhmm == MORNING_FORCE_CLOSE_TIME and not state['morning_force_done']:
    state['morning_force_done'] = True
    for side in ('short', 'long'):
      _close_side(state[side], side, TAIL_CLOSE_BATCH_QTY, 'morning_force', orders)
  if hhmm >= FORCE_CLOSE_TIME and not state['afternoon_force_done']:
    for side in ('short', 'long'):
      _close_side(state[side], side, TAIL_CLOSE_BATCH_QTY, 'afternoon_force', orders)
    if hhmm >= FINAL_CLOSE_TIME:
      state['afternoon_force_done'] = True
  if hhmm >= FINAL_CLOSE_TIME and not state['final_close_done']:
    state['final_close_done'] = True
    for side in ('short', 'long'):
      _close_side(state[side], side, state[side]['position'], 'final_force', orders)


def _sync_with_portfolio(state, position):
  """方向权限跳过开仓时，下一根 Bar 按真实持仓修正脚本内部状态。"""
  if state['short']['position'] > 0 and position >= 0:
    state['short'] = _new_side_state()
  if state['long']['position'] > 0 and position <= 0:
    state['long'] = _new_side_state()


def on_bar(bar, state):
  """每根已完成 1m K 线执行一次，方向由回测方案统一控制。"""
  bar_time = bar['time']
  trade_date = bar['tradeDate']
  hhmm = bar['hhmm']
  if not trade_date or hhmm is None or state.get('last_processed_bar') == bar_time:
    return []
  if state.get('trade_date') != trade_date:
    _reset_day(state, trade_date, bar['open'])
  _sync_with_portfolio(state, bar['position'])
  state['last_processed_bar'] = bar_time
  state['processed_bar_keys'].append(bar_time)
  orders = []
  if _is_trade_time(hhmm):
    _check_exit_rules(state, bar, orders)
    _apply_break_even_stop(state)
    _confirm_peak(state, bar, orders)
    _confirm_valley(state, bar, orders)
    _update_candidates(state, bar['close'])
  _force_close(state, hhmm, orders)
  return orders
