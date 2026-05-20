# 자동매매 리스크 설정 파일

# 총 계좌 기준
MAX_DAILY_LOSS_RATE = -0.02        # 하루 최대 손실 -2%
MAX_TOTAL_DRAWDOWN = -0.10         # 전체 계좌 최대 손실 -10%

# 종목 기준
MAX_POSITION_RATE = 0.10           # 한 종목 최대 비중 10%
MAX_SINGLE_TRADE_LOSS_RATE = -0.01 # 1회 거래 최대 손실 -1%

# 매매 제한
MAX_TRADES_PER_DAY = 3             # 하루 최대 매매 횟수
MAX_OPEN_POSITIONS = 5             # 동시 보유 최대 종목 수

# 전략 기준
TAKE_PROFIT_RATE = 0.15            # 익절 +15%
STOP_LOSS_RATE = -0.08             # 손절 -8%

# 안전 모드
ENABLE_REAL_TRADING = False        # 실거래 금지
ENABLE_PAPER_TRADING = False       # 아직 모의투자도 비활성
