"""
Tunable parameters for the RITCx Volatility Trading Case.

Every number here is sourced from the official case PDF
('RITCx-Volatility Trading Case.pdf', in this folder) unless marked TUNABLE.
Values marked SPEC are case rules - do not change them without re-reading the PDF.
Values marked TUNABLE are our own trading choices.
"""

# ---------------------------------------------------------------- connection
API_HOST = 'http://localhost:9999'
API_KEY = {'X-API-Key': 'Rotman'}

# ------------------------------------------------------------------ case spec
# SPEC: one trading year is 240 days (20 days x 12 months).
# SPEC: the case represents one month = 20 days = 1/12 of a year.
DAYS_PER_YEAR = 240
CASE_DAYS = 20
CASE_YEARS = CASE_DAYS / DAYS_PER_YEAR          # 1/12

# SPEC: the case is divided into 4 weeks.
WEEKS_IN_CASE = 4

# SPEC: risk-free rate is 0%. The PDF notes an instructor may change this.
RISK_FREE_RATE = 0.0

# SPEC: options are European, cash settled, 100 shares per contract.
CONTRACT_MULTIPLIER = 100

# SPEC: underlying ticker.
UNDERLYING = 'RTM'

# ----------------------------------------------------------- fees and limits
# SPEC: $0.02 per share traded for RTM, $2.00 per contract for options.
FEE_PER_SHARE = 0.02
FEE_PER_CONTRACT = 2.00
# $2.00 per contract over 100 shares = $0.02 per underlying share equivalent,
# which makes option fees directly comparable to per-share option premium.
FEE_PER_OPTION_SHARE = FEE_PER_CONTRACT / CONTRACT_MULTIPLIER

# SPEC: delta must stay within +/- 7000 or a penalty accrues every second:
#   penalty(t) = (|portfolio delta| - DELTA_LIMIT) * DELTA_PENALTY_RATE
DELTA_LIMIT = 7000
DELTA_PENALTY_RATE = 0.10

# SPEC: gross/net position limits and max size per single trade.
LIMIT_RTM_GROSS = 50_000
LIMIT_RTM_NET = 50_000
LIMIT_OPT_GROSS = 2_500
LIMIT_OPT_NET = 1_000
MAX_TRADE_RTM = 10_000
MAX_TRADE_OPTIONS = 100

# --------------------------------------------------------- volatility model
# TUNABLE: how to fill weeks that have had no announcement yet.
#
#   'market'  - use the market's own average implied vol as the prior. Only
#               deviates from the market where we genuinely know more (the
#               current week, and next week's range). Never manufactures edge
#               out of an invented constant.
#   'fixed'   - use TAIL_VOL_FIXED for every unannounced week.
#   'persist' - carry the most recently announced sigma forward.
#
# Which of these is best is an empirical question, not a theoretical one, and
# it is settled with real data from the practice server, not by argument:
#     python record.py --name heat1     (run during a practice heat)
#     python replay.py heats/*.jsonl    (scores every mode on identical ticks)
TAIL_VOL_MODE = 'market'
TAIL_VOL_FIXED = 0.25

# TUNABLE: ignore implied vol solutions outside this range as numerically bad.
IV_MIN = 0.01
IV_MAX = 3.00

# --------------------------------------------------------------- signal gates
# TUNABLE: minimum edge, in dollars per underlying share, for a trade to be
# shown as actionable. This is measured AFTER crossing the spread and paying
# commission, so 0.00 is already break-even; require a real cushion.
MIN_EDGE_DOLLARS = 0.03

# TUNABLE: minimum implied-vol gap (in vol points, 0.02 = 2%) to flag a
# dispersion trade between two strikes.
MIN_DISPERSION_VOL = 0.015

# ------------------------------------------------------------------- hedging
# TUNABLE: hedge into the band with a buffer rather than flattening to zero.
# Only act when delta approaches the limit, then pull back to a safe interior
# level. This avoids burning $0.02/share repeatedly flattening a delta that
# drifts straight back.
HEDGE_TRIGGER_DELTA = 5_000     # start suggesting a hedge past this
HEDGE_TARGET_DELTA = 2_000      # hedge back to roughly this magnitude

# --------------------------------------------------------------- dashboard
REFRESH_SECONDS = 0.35
