# Toss Open API read-only validation

This is an isolated validation runner. It does not import or alter KIS, discovery,
orders, risk, sizing, cron, systemd, databases, or release pointers.

It permits only OAuth token issuance and these GET endpoints: accounts, buying
power, commissions, prices, and candles. All other REST requests and all
WebSocket topics except `trade:us`, `orderbook:us`, and `personal:order` fail
closed with `BLOCKED_MUTATING_REQUEST`.

On Oracle, run from the repository checkout using its existing Python runtime:

```bash
cd ~/trading
set -a
source .env.toss
set +a
python3 -m validation.toss_openapi.validate
```

No `TOSS_ACCOUNT_ID` is required. The runner calls `/api/v1/accounts` after
authentication and uses its discovered `accountSeq`; account identifiers, access
tokens, secrets, authorization headers, and response payloads are never printed.

The controlled quote checks use fixed batches of 1, 10, 50, 100, and 200 liquid
US tickers. WebSocket checks use only AAPL, NVDA, and QQQ. No full-universe scan
is performed, and the runner makes one request per check (no rate-limit probing).
