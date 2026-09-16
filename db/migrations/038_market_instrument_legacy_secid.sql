-- eastmoney_secid 仅保留为历史兼容字段，证券身份由 (exchange, symbol) 唯一确定。
-- 北交所和深交所均可能使用 0.<symbol>，因此该字段不能作为全局唯一键。
ALTER TABLE market_instrument
  DROP CONSTRAINT IF EXISTS market_instrument_eastmoney_secid_key;
