"""
Thin wrapper over the RIT REST API.

Every call fails loudly with a named error rather than returning a silent
default, so a dead connection can never be mistaken for a flat market.
"""

import requests

import config


class ApiException(Exception):
    """Raised when the RIT API is unreachable or rejects a request."""


class RitClient:
    def __init__(self, host=config.API_HOST, api_key=None):
        self.host = host.rstrip('/')
        self.session = requests.Session()
        self.session.headers.update(api_key or config.API_KEY)

    # ------------------------------------------------------------- internals
    def _get(self, path, **params):
        url = f'{self.host}/v1/{path}'
        try:
            resp = self.session.get(url, params=params or None, timeout=5)
        except requests.RequestException as exc:
            raise ApiException(
                f'cannot reach RIT at {url} - is the client running and the '
                f'API enabled on port 9999? ({exc})'
            ) from exc
        if resp.status_code == 401:
            raise ApiException(
                'RIT rejected the API key. Check the X-API-Key value in '
                'config.py matches the key set in the RIT client.'
            )
        if not resp.ok:
            raise ApiException(f'GET {path} failed: {resp.status_code} {resp.text[:200]}')
        return resp.json()

    def _post(self, path, **params):
        url = f'{self.host}/v1/{path}'
        try:
            resp = self.session.post(url, params=params, timeout=5)
        except requests.RequestException as exc:
            raise ApiException(f'cannot reach RIT at {url} ({exc})') from exc
        if not resp.ok:
            raise ApiException(f'POST {path} failed: {resp.status_code} {resp.text[:200]}')
        return resp.json()

    # ------------------------------------------------------------- endpoints
    def case(self):
        """Case status: tick, ticks_per_period, period, status."""
        return self._get('case')

    def trader(self):
        """Trader info, including net liquidation value."""
        return self._get('trader')

    def limits(self):
        """Gross/net position limits and current usage."""
        return self._get('limits')

    def securities(self, ticker=None):
        """All tradeable securities with bid/ask/last/position."""
        return self._get('securities', ticker=ticker) if ticker else self._get('securities')

    def news(self, since=0, limit=50):
        """News releases, newest first."""
        return self._get('news', since=since, limit=limit)

    # ---------------------------------------------------------------- orders
    def market_order(self, ticker, action, quantity):
        """
        Send a market order. The case guarantees unlimited depth and no price
        impact, so market orders always fill at the touch.

        action: 'BUY' or 'SELL'
        """
        action = action.upper()
        if action not in ('BUY', 'SELL'):
            raise ValueError(f"action must be BUY or SELL, got {action!r}")
        quantity = int(quantity)
        if quantity <= 0:
            raise ValueError(f'quantity must be positive, got {quantity}')
        return self._post(
            'orders', ticker=ticker, type='MARKET',
            quantity=quantity, action=action,
        )

    def send_in_chunks(self, ticker, action, quantity, max_chunk):
        """
        Split an order that exceeds the per-trade size cap into legal chunks.

        The case caps a single trade at 10,000 RTM shares or 100 option
        contracts; exceeding it is rejected outright, so anything larger has to
        be sliced. Returns the list of order acknowledgements.
        """
        quantity = int(quantity)
        acks = []
        remaining = quantity
        while remaining > 0:
            chunk = min(remaining, max_chunk)
            acks.append(self.market_order(ticker, action, chunk))
            remaining -= chunk
        return acks

    def cancel_all(self):
        return self._post('commands/cancel', all=1)
