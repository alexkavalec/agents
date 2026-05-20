# core polymarket api - updated for py-clob-client-v2

import os
import ast
import time

from dotenv import load_dotenv

from web3 import Web3
from web3.constants import MAX_INT
from web3.middleware import geth_poa_middleware

import httpx
from py_clob_client_v2 import (
    ApiCreds,
    ClobClient,
    MarketOrderArgs,
    OrderArgs,
    OrderType,
    Side,
)

from agents.utils.objects import SimpleMarket, SimpleEvent

load_dotenv()


class Polymarket:
    def __init__(self) -> None:
        self.gamma_url = "https://gamma-api.polymarket.com"
        self.gamma_markets_endpoint = self.gamma_url + "/markets"
        self.gamma_events_endpoint = self.gamma_url + "/events"

        self.clob_url = "https://clob.polymarket.com"
        self.chain_id = 137  # POLYGON
        self.private_key = os.getenv("POLYGON_WALLET_PRIVATE_KEY")
        self.polygon_rpc = os.getenv("POLYGON_RPC_URL", "https://polygon-mainnet.g.alchemy.com/v2/demo")
        self.w3 = Web3(Web3.HTTPProvider(self.polygon_rpc))

        self.usdc_address = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        self.ctf_address = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

        self.erc20_approve = """[{"inputs":[{"internalType":"address","name":"spender","type":"address"},{"internalType":"uint256","name":"amount","type":"uint256"}],"name":"approve","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"address","name":"account","type":"address"}],"name":"balanceOf","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]"""
        self.erc1155_set_approval = """[{"inputs":[{"internalType":"address","name":"operator","type":"address"},{"internalType":"bool","name":"approved","type":"bool"}],"name":"setApprovalForAll","outputs":[],"stateMutability":"nonpayable","type":"function"}]"""

        self.web3 = Web3(Web3.HTTPProvider(self.polygon_rpc))
        self.web3.middleware_onion.inject(geth_poa_middleware, layer=0)

        self.usdc = self.web3.eth.contract(
            address=self.usdc_address, abi=self.erc20_approve
        )
        self.ctf = self.web3.eth.contract(
            address=self.ctf_address, abi=self.erc1155_set_approval
        )

        self._init_api_keys()

    def _init_api_keys(self) -> None:
        # Deposit wallet flow - signature_type=3 (POLY_1271), funder = deposit wallet address
        funder = os.getenv("POLYGON_FUNDER_ADDRESS")
        l1_client = ClobClient(
            host=self.clob_url,
            chain_id=self.chain_id,
            key=self.private_key,
            signature_type=3,
            funder=funder,
        )
        creds = l1_client.create_or_derive_api_key()
        self.client = ClobClient(
            host=self.clob_url,
            chain_id=self.chain_id,
            key=self.private_key,
            creds=creds,
            signature_type=3,
            funder=funder,
        )

    def get_all_markets(self) -> "list[SimpleMarket]":
        markets = []
        res = httpx.get(self.gamma_markets_endpoint)
        if res.status_code == 200:
            for market in res.json():
                try:
                    market_data = self.map_api_to_market(market)
                    markets.append(SimpleMarket(**market_data))
                except Exception as e:
                    pass
        return markets

    def filter_markets_for_trading(self, markets: "list[SimpleMarket]"):
        return [m for m in markets if m.active]

    def get_market(self, token_id: str) -> SimpleMarket:
        params = {"clob_token_ids": token_id}
        res = httpx.get(self.gamma_markets_endpoint, params=params)
        if res.status_code == 200:
            data = res.json()
            if data:
                return self.map_api_to_market(data[0], token_id)

    def map_api_to_market(self, market, token_id: str = "") -> dict:
        result = {
            "id": int(market.get("id", 0)),
            "question": market.get("question", ""),
            "end": market.get("endDate", ""),
            "description": market.get("description", ""),
            "active": market.get("active", False),
            "funded": market.get("funded", False),
            "rewardsMinSize": float(market.get("rewardsMinSize", 0) or 0),
            "rewardsMaxSpread": float(market.get("rewardsMaxSpread", 0) or 0),
            "spread": float(market.get("spread", 0) or 0),
            "outcomes": str(market.get("outcomes", "[]")),
            "outcome_prices": str(market.get("outcomePrices", "[]")),
            "clob_token_ids": str(market.get("clobTokenIds", "[]")),
        }
        if token_id:
            result["clob_token_ids"] = token_id
        return result

    def get_all_events(self) -> "list[SimpleEvent]":
        events = []
        res = httpx.get(
            self.gamma_events_endpoint,
            params={"active": "true", "closed": "false", "limit": 50, "order": "volume", "ascending": "false"}
        )
        if res.status_code == 200:
            print(f"RAW API returned {len(res.json())} events")
            for event in res.json():
                try:
                    event_data = self.map_api_to_event(event)
                    events.append(SimpleEvent(**event_data))
                except Exception as e:
                    print(e)
        else:
            print(f"API error: {res.status_code}")
        return events

    def map_api_to_event(self, event) -> dict:
        description = event.get("description", "")
        return {
            "id": int(event["id"]),
            "ticker": event["ticker"],
            "slug": event["slug"],
            "title": event["title"],
            "description": description,
            "active": event["active"],
            "closed": event["closed"],
            "archived": event["archived"],
            "new": event["new"],
            "featured": event["featured"],
            "restricted": event["restricted"],
            "end": event["endDate"],
            "markets": ",".join([x["id"] for x in event["markets"]]),
        }

    def filter_events_for_trading(self, events: "list[SimpleEvent]") -> "list[SimpleEvent]":
        tradeable = []
        for event in events:
            print(f"DEBUG event={event.title[:30]} active={event.active} closed={event.closed} archived={event.archived} restricted={event.restricted}")
            if event.active and not event.archived and not event.closed:
                tradeable.append(event)
        return tradeable

    def get_all_tradeable_events(self) -> "list[SimpleEvent]":
        all_events = self.get_all_events()
        print(f"Total raw events fetched: {len(all_events)}")
        tradeable = self.filter_events_for_trading(all_events)
        print(f"Total tradeable events after filter: {len(tradeable)}")
        return tradeable

    def get_sampling_simplified_markets(self) -> list:
        try:
            raw = self.client.get_sampling_simplified_markets()
            data = raw.get("data", raw) if isinstance(raw, dict) else raw
            markets = []
            for raw_market in data:
                try:
                    if isinstance(raw_market, dict):
                        tokens = raw_market.get("tokens", [])
                        if tokens:
                            token_one_id = tokens[0].get("token_id", "")
                            if token_one_id:
                                market = self.get_market(token_one_id)
                                if market:
                                    markets.append(market)
                    else:
                        markets.append(raw_market)
                except Exception:
                    continue
            return markets
        except Exception as e:
            print(f"Sampling markets error: {e}")
            return []

    def get_orderbook(self, token_id: str):
        return self.client.get_order_book(token_id)

    def get_orderbook_price(self, token_id: str) -> float:
        return float(self.client.get_price(token_id, side="BUY"))

    def get_address_for_private_key(self):
        account = self.w3.eth.account.from_key(str(self.private_key))
        return account.address

    def execute_market_order(self, market, amount) -> str:
        try:
            token_id = ast.literal_eval(market[0].dict()["metadata"]["clob_token_ids"])[1]
        except Exception:
            try:
                token_id = ast.literal_eval(market[0].dict()["metadata"]["clob_token_ids"])[0]
            except Exception as e:
                print(f"Token ID error: {e}")
                raise

        from py_clob_client_v2 import MarketOrderArgs, OrderType, Side, PartialCreateOrderOptions
        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=amount,
            side=Side.BUY,
            order_type=OrderType.FOK,
        )
        resp = self.client.create_and_post_market_order(
            order_args=order_args,
            options=PartialCreateOrderOptions(tick_size="0.01"),
            order_type=OrderType.FOK,
        )
        print(f"Order response: {resp}")
        print("Done!")
        return resp

    def get_usdc_balance(self) -> float:
        # Polymarket holds tradeable USDC in the FUNDER (deposit/proxy) wallet,
        # not the raw signing wallet. Check the funder first, fall back to wallet.
        addresses = []
        funder = os.getenv("POLYGON_FUNDER_ADDRESS")
        if funder:
            addresses.append(funder)
        try:
            addresses.append(self.get_address_for_private_key())
        except Exception:
            pass
        for addr in addresses:
            try:
                bal = self.usdc.functions.balanceOf(addr).call()
                val = float(bal / 1e6)  # USDC has 6 decimals
                if val > 0:
                    return val
            except Exception as e:
                print(f"Balance error for {addr}: {e}")
        return 0.0


if __name__ == "__main__":
    load_dotenv()
    p = Polymarket()
    print(p.get_all_events())
