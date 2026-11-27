"""Authenticated read access for reconciliation during engineering acceptance."""
import json
from types import SimpleNamespace
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from config import PAPER_ORIGIN
from paper_store import amt, stamp


class PaperObservationOnly(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class AlpacaPaper:
    # The account-specific fractional protection protocol has no acceptance evidence yet.
    # An environment variable or database entry must never enable this transport to trade.
    orders_enabled = False

    def __init__(self, settings):
        if not settings.alpaca.is_paper:
            raise ValueError("paper broker origin required")
        self.s = settings

    def _get(self, origin, path, params=None):
        if not self.s.alpaca.is_paper or origin not in (PAPER_ORIGIN, "https://data.alpaca.markets"):
            raise ValueError("unrecognized paper gateway origin")
        url = origin + path
        if params:
            url += "?" + urlencode(params)
        req = Request(url, method="GET", headers={
            "APCA-API-KEY-ID": self.s.alpaca.api_key,
            "APCA-API-SECRET-KEY": self.s.alpaca.api_secret,
        })
        with build_opener(NoRedirect()).open(req, timeout=10) as response:
            return json.load(response)

    def snapshot(self):
        a = self._get(PAPER_ORIGIN, "/v2/account")
        positions = self._get(PAPER_ORIGIN, "/v2/positions")
        orders = self.get_active_orders()
        if (not a.get("id") or not isinstance(a["trading_blocked"], bool)
                or not isinstance(a["account_blocked"], bool)):
            raise ValueError("incomplete account snapshot")
        holdings = {p["symbol"].replace("/", "").upper(): amt(p["qty"]) for p in positions}
        if len(holdings) != len(positions):
            raise ValueError("duplicate position in snapshot")
        return (a["id"], amt(a["equity"], True), min(amt(a["buying_power"]), amt(a["cash"])),
                holdings, [o.client_order_id for o in orders], a["trading_blocked"] or a["account_blocked"])

    def get_active_orders(self):
        orders = self._get(PAPER_ORIGIN, "/v2/orders", {"status": "open", "limit": 500})
        if not isinstance(orders, list) or len(orders) >= 500:
            raise ValueError("open order snapshot incomplete")
        return [SimpleNamespace(**o) for o in orders]

    def fills(self, since):
        options = {"after": since, "direction": "asc", "page_size": 100}
        seen = set()
        rows = []
        for _ in range(1000):
            page = self._get(PAPER_ORIGIN, "/v2/account/activities/FILL", options)
            if not isinstance(page, list) or len(page) > 100:
                raise ValueError("invalid activity page")
            rows.extend(page)
            if len(page) < 100:
                return rows
            token = page[-1]["id"]
            if not token or token in seen:
                raise ValueError("invalid activity cursor")
            seen.add(token)
            options["page_token"] = token
        raise ValueError("activity limit reached")

    def lookup(self, cid):
        order = self._get(PAPER_ORIGIN, "/v2/orders:by_client_order_id", {"client_order_id": cid})
        return SimpleNamespace(**order)

    def quote(self, symbol):
        if not (symbol.isascii() and symbol.isalpha() and symbol.isupper()):
            return None
        if self._get(PAPER_ORIGIN, "/v2/clock").get("is_open") is not True:
            return None
        data = self._get("https://data.alpaca.markets", "/v2/stocks/quotes/latest",
                         {"symbols": symbol, "feed": "iex"})
        q = data["quotes"][symbol]
        return amt(q["bp"], True), amt(q["ap"], True), stamp(q["t"])

    def submit(self, cid, symbol, side, qty):
        raise PaperObservationOnly("broker order transport awaits controlled acceptance")
