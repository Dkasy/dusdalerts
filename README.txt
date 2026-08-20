DUSD Pool Monitor
=================

Pool:
  PancakeSwap V3 USDT/DUSD 0.01%
  0xB67e5EaF770a384Ab28029d08B9bC5EBE32beb0F

Alerts:
  - >=10% pool token-balance TVL drop
  - V3 Burn event >=10% of reference TVL
  - >=$50,000 direct DUSD/USDT swap
  - DUSD <0.998 USDT

Install:
  Requires Python 3.9 or newer.

  python3 -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
  cp .env.example .env

Edit .env and set:
  BSC_RPC_URL
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID

Run:
  python dusd_pool_monitor.py

Test:
  python -m unittest -v

Notes:
  * The program is read-only and needs no wallet/private key.
  * Use a BSC RPC endpoint that supports eth_getLogs. The standard public BNB
    Chain endpoint rejects this method; the monitor checks capability at startup.
  * Prices and balances are read at the configured confirmed-block delay, so
    unconfirmed chain state does not trigger liquidity or depeg alerts.
  * State is persisted in dusd_pool_monitor_state.json so restarts do not
    intentionally reprocess old blocks.
