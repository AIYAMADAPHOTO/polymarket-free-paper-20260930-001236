"""GET-only allowlisted public APIs; no credentials or signed requests."""
import json
import logging
import re
import time
from http.client import HTTPException
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

from config import GAMMA_URL, CLOB_URL, HTTP_TIMEOUT_SECONDS, HTTP_ATTEMPTS


class ApiError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PolymarketClient:
    def __init__(self, attempts=HTTP_ATTEMPTS, timeout=HTTP_TIMEOUT_SECONDS, sleep=time.sleep):
        self.attempts, self.timeout, self.sleep = attempts, timeout, sleep
        self.log = logging.getLogger('paperbot')
        self.opener = build_opener(NoRedirect)

    def _get(self, base, path, params=None):
        allowed = (base == GAMMA_URL and (path == '/markets/keyset' or
                   re.fullmatch(r'/markets/\d+', path))) or (
                   base == CLOB_URL and (path == '/book' or
                   re.fullmatch(r'/clob-markets/0x[0-9a-fA-F]{64}', path)))
        if not allowed:
            raise ValueError('Public read-only endpoint not allowlisted')
        url = base + path + ('?' + urlencode(params) if params else '')
        req = Request(url, method='GET', headers={
            'User-Agent': 'PolymarketPaperBot/1.0', 'Accept': 'application/json'})
        for attempt in range(self.attempts):
            retry_after = 0
            try:
                with self.opener.open(req, timeout=self.timeout) as response:
                    data = json.loads(response.read().decode('utf-8'), parse_float=Decimal)
                self.log.info('API success GET %s', url)
                return data
            except HTTPError as exc:
                self.log.warning('API error HTTP %s GET %s', exc.code, url)
                if exc.code not in (408, 429, 500, 502, 503, 504):
                    raise ApiError(f'HTTP {exc.code}: {url}') from exc
                try:
                    retry_after = min(30, max(0, int(exc.headers.get('Retry-After', '0'))))
                except (ValueError, AttributeError):
                    pass
                error = exc
            except (URLError, OSError, TimeoutError, HTTPException, ValueError) as exc:
                self.log.warning('API error %s GET %s', exc, url)
                error = exc
            if attempt + 1 < self.attempts:
                delay = max(retry_after, min(2 ** attempt, 30))
                self.log.info('API retry in %ss attempt=%s', delay, attempt + 2)
                self.sleep(delay)
        raise ApiError(f'API failed after {self.attempts} attempts: {url}') from error

    def market_page(self, limit, cursor=None, *, order=None, ascending=None, include_tag=False):
        params = {'closed': 'false', 'limit': limit}
        if cursor:
            params['after_cursor'] = cursor
        if order:
            params['order'] = order
        if ascending is not None:
            params['ascending'] = 'true' if ascending else 'false'
        if include_tag:
            params['include_tag'] = 'true'
        data = self._get(GAMMA_URL, '/markets/keyset', params)
        if not isinstance(data, dict) or not isinstance(data.get('markets'), list):
            raise ApiError('Invalid Gamma market page schema')
        return data

    def market(self, market_id):
        return self._get(GAMMA_URL, '/markets/' + str(market_id))

    def book(self, token_id):
        return self._get(CLOB_URL, '/book', {'token_id': token_id})

    def fee_details(self, condition_id):
        return self._get(CLOB_URL, '/clob-markets/' + condition_id)
