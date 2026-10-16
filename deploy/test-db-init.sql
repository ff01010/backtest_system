CREATE ROLE quant_backtest_owner
  NOLOGIN
  NOSUPERUSER
  NOCREATEDB
  NOCREATEROLE
  NOINHERIT;

CREATE ROLE quant_backtest_release_admin
  LOGIN
  PASSWORD 'test-release-password'
  NOSUPERUSER
  NOCREATEDB
  NOCREATEROLE
  NOINHERIT;

CREATE ROLE quant_backtest_test_app
  LOGIN
  PASSWORD 'test-app-password'
  NOSUPERUSER
  NOCREATEDB
  NOCREATEROLE
  NOINHERIT;

CREATE DATABASE quant_backtest_test
  OWNER quant_backtest_owner
  ENCODING 'UTF8'
  TEMPLATE template0;

GRANT CONNECT ON DATABASE quant_backtest_test TO quant_backtest_test_app;
GRANT CONNECT ON DATABASE quant_backtest_test TO quant_backtest_release_admin;
