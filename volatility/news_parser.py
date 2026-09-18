"""
Parse the volatility announcements out of the RIT news feed.

The case PDF gives this sample schedule:

    t=1    Week 1  "The realized volatility of RTM for this week will be 20%"
    t=75   Week 1  "The realized volatility of RTM for next week will be between 27-30%"
    t=150  Week 2  "The realized volatility of RTM for this week will be 29%"

So there are two kinds of release, and they differ in certainty:
  * "this week"  -> an exact point value for the week now in progress.
  * "next week"  -> a range for the following week, narrowed to a point later.

The wording is matched loosely on purpose. The exact phrasing on competition
day may differ from the sample, and a parser that silently matches nothing is
far more dangerous here than one that is slightly permissive.
"""

import re

import config

# "20%", "20.5 %", "between 27-30%", "between 27% and 30%", "27 to 30%"
_PCT = r'(\d+(?:\.\d+)?)\s*%?'
_RANGE_RE = re.compile(
    rf'between\s+{_PCT}\s*(?:-|to|and|–|—)\s*{_PCT}', re.IGNORECASE)
_BARE_RANGE_RE = re.compile(
    rf'{_PCT}\s*(?:-|to|–|—)\s*{_PCT}', re.IGNORECASE)
_POINT_RE = re.compile(rf'(?:will\s+be|is|of)\s+{_PCT}', re.IGNORECASE)

_THIS_WEEK_RE = re.compile(r'\bthis\s+week\b', re.IGNORECASE)
_NEXT_WEEK_RE = re.compile(r'\bnext\s+week\b', re.IGNORECASE)
_VOL_RE = re.compile(r'volatilit', re.IGNORECASE)

_DELTA_LIMIT_RE = re.compile(
    r'delta\s+limit[^0-9]{0,30}([0-9][0-9,]*)', re.IGNORECASE)


def week_of_tick(tick, ticks_total, weeks=config.WEEKS_IN_CASE):
    """
    Which week (1-based) a tick falls in.

    Derived from the live case length rather than hardcoded, because the
    official PDF is internally inconsistent: the prose says 300 seconds with
    75-tick weeks numbered from t=1, while the sample news table puts t=150 in
    week 2 and t=450 in week 4, which only holds for a 600-tick case indexed
    from t=0. Checked against all four rows of that table, only the 0-indexed
    reading fits, so that is what is used here.

    This is used for display and for the variance weighting, where a one-tick
    boundary disagreement is immaterial. News is deliberately NOT assigned to
    weeks this way - see parse_vol_news.
    """
    if ticks_total <= 0:
        return 1
    per_week = ticks_total / weeks
    idx = int(max(tick, 0) // per_week) + 1
    return max(1, min(weeks, idx))


def parse_delta_limit(news_items):
    """Return a delta limit announced in the news, or None if not mentioned."""
    for item in news_items:
        text = f"{item.get('headline', '')} {item.get('body', '')}"
        match = _DELTA_LIMIT_RE.search(text)
        if match:
            try:
                return int(match.group(1).replace(',', ''))
            except ValueError:
                continue
    return None


def parse_vol_news(news_items, ticks_total):
    """
    Turn the raw news feed into {week_number: sigma} plus the raw observations.

    Returns (week_vols, observations) where week_vols maps a 1-based week
    number to a decimal volatility (0.20 for 20%), and observations is a list
    of dicts describing what was parsed, for display and debugging.

    A "this week" release overwrites a "next week" estimate for the same week,
    because the point value is exact and supersedes the earlier range.

    Weeks are assigned by COUNTING announcements, not from the tick. The case
    issues exactly one "this week" release per week, so the Nth such release is
    week N by construction, and a "next week" release refers to the week after
    whichever week is currently running. That sidesteps the PDF's contradictory
    tick-to-week conventions completely: no boundary arithmetic can put an
    announcement in the wrong week.
    """
    week_vols = {}
    exact_weeks = set()
    observations = []
    current_week = 0        # how many "this week" releases we have seen

    # Oldest first, so later, better information overwrites earlier guesses.
    ordered = sorted(news_items, key=lambda n: n.get('tick', 0))

    for item in ordered:
        text = f"{item.get('headline', '')} {item.get('body', '')}".strip()
        if not _VOL_RE.search(text):
            continue

        tick = item.get('tick', 0)
        this_week = bool(_THIS_WEEK_RE.search(text))
        next_week = bool(_NEXT_WEEK_RE.search(text))
        if not (this_week or next_week):
            continue

        # The first release seen anchors the counter using tick arithmetic;
        # after that, counting takes over and boundary conventions stop
        # mattering. The anchor is safe because the two readings of the PDF
        # only disagree exactly AT a boundary tick, and they agree everywhere
        # else - so a coarse "which week are we roughly in" lookup is reliable
        # even though exact boundary arithmetic is not. This also keeps the
        # weeks correct if the news feed is ever read part-way into a heat.
        if this_week:
            current_week = (current_week + 1) if current_week else \
                week_of_tick(tick, ticks_total)
            target_week = current_week
        else:
            base = current_week or week_of_tick(tick, ticks_total)
            target_week = base + 1
        if target_week > config.WEEKS_IN_CASE:
            continue

        lo = hi = None
        range_match = _RANGE_RE.search(text) or _BARE_RANGE_RE.search(text)
        if range_match:
            lo, hi = float(range_match.group(1)) / 100.0, float(range_match.group(2)) / 100.0
            sigma, kind = (lo + hi) / 2.0, 'range'
        else:
            point_match = _POINT_RE.search(text)
            if not point_match:
                continue
            sigma, kind = float(point_match.group(1)) / 100.0, 'point'

        if not config.IV_MIN <= sigma <= config.IV_MAX:
            continue

        # An exact "this week" value must not be clobbered by a stale range.
        if target_week in exact_weeks and kind == 'range':
            continue
        if this_week and kind == 'point':
            exact_weeks.add(target_week)

        week_vols[target_week] = sigma
        observations.append({
            'tick': tick,
            'week': target_week,
            'tick_week': week_of_tick(tick, ticks_total),  # cross-check only
            'sigma': sigma,
            'kind': kind,
            'lo': lo,
            'hi': hi,
            'exact': this_week and kind == 'point',
            'text': text,
        })

    return week_vols, observations
