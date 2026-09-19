"""
Tunable parameters for the RITCx Volatility Trading Case.

Every number here is sourced from the official case PDF
('RITCx-Volatility Trading Case.pdf', in this folder) unless marked TUNABLE.
Values marked SPEC are case rules - do not change them without re-reading the PDF.
Values marked TUNABLE are our own trading choices.
"""

import os

# ---------------------------------------------------------------- connection
API_HOST = 'http://localhost:9999'


def _load_api_key():
    """
    Resolve the RIT API key without committing it.

    Order: the RIT_API_KEY environment variable, then a local `api_key.txt`
    next to this file (gitignored), then Rotman's documented default. The key
    is per-machine and belongs in neither the repo nor the history.
    """
    from_env = os.environ.get('RIT_API_KEY')
    if from_env:
        return from_env.strip()
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'api_key.txt')
    try:
        with open(local, encoding='utf-8') as handle:
            key = handle.read().strip()
            if key:
                return key
    except OSError:
        pass
    return 'Rotman'


API_KEY = {'X-API-Key': _load_api_key()}

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

# TUNABLE: reject an option whose implied vol is not meaningfully determined by
# its own quote. The case guarantees a 2-cent spread on every option, but two
# cents means very different things depending on vega: measured live on the
# practice server at tick ~190, two cents was worth 0.63 vol points on RTM48C
# (vega 0.032) and 4.08 vol points on RTM52C (vega 0.0006).
#
# Without this filter the low-vega wings generate large fake dispersion and drag
# the market-IV average around - and in 'market' mode that average is the prior
# for unannounced weeks, so the noise would leak straight into the forecast.
#
# Low-vega options are bad volatility vehicles anyway: at a $2.00-per-contract
# fee, a $0.02 option pays 100% of its premium in commission.
MAX_IV_SPREAD = 0.015          # vol points of noise implied by the quote width

# ------------------------------------------------------------------- hedging
# TUNABLE: hedge into the band with a buffer rather than flattening to zero.
# Only act when delta approaches the limit, then pull back to a safe interior
# level. This avoids burning $0.02/share repeatedly flattening a delta that
# drifts straight back.
#
# The TARGET was 2,000 and is 3,000. Delta inside +/-7,000 is free, so the only
# thing the last 1,000 of flattening buys is a slightly later next hedge - and
# it is paid for in shares every single time. Over 207 recordings, paired:
# +$1,342 a heat (t 2.8), friction $48,170 -> $47,576, and the fine essentially
# unmoved at $7,631 -> $7,795. Confirmed order-accurate over 80 heats at +$657
# (t 2.7) with no illegal book, which matters here because this changes how
# much of the 50,000-share limit the hedge is using.
#
# Broad interior plateau: 2,500 is +$1,032 (t 2.3), 3,500 +$1,152 (t 2.2), and
# it falls away either side - 1,500 is -$976, 4,500 is +$611 at t 0.9 with the
# fine climbing to $9,338. Past about 3,500 the extra room stops being free.
HEDGE_TRIGGER_DELTA = 5_000     # start suggesting a hedge past this
HEDGE_TARGET_DELTA = 3_000      # hedge back to roughly this magnitude

# --------------------------------------------------------------- dashboard
REFRESH_SECONDS = 0.35
