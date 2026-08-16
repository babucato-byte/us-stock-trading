# Account-level risk controls
MAX_DAILY_LOSS_RATE = -0.02
MAX_TOTAL_DRAWDOWN = -0.10

# Position-level risk controls
MAX_POSITION_RATE = 0.10
MAX_SINGLE_TRADE_LOSS_RATE = -0.01

# Trading limits
MAX_TRADES_PER_DAY = 3
MAX_OPEN_POSITIONS = 5

# Account-wide exposure controls
# Total market value of all open positions, as a fraction of account equity.
MAX_TOTAL_EXPOSURE_RATE = 0.5

# Exit rules
TAKE_PROFIT_RATE = 0.15
STOP_LOSS_RATE = -0.08

# Trading mode guardrails.
# Defaults must remain paper-safe. Do not change live switches from the dashboard.
TRADING_MODE = "paper"
ENABLE_REAL_TRADING = False
ENABLE_PAPER_TRADING = True
LIVE_DRY_RUN = True
