# core polymarket api - updated for py-clob-client-v2

import os
import datetime

from dotenv import load_dotenv

from web3 import Web3
from web3.middleware import geth_poa_middleware

import httpx
from py_clob_client_v2 import ApiCreds, ClobClient

load_dotenv()


class Polymarket:
    def __init__(self) -> None:
        self.clob_url = "https://clob.polymarket.com"
        self.chain_id = 137  # POLYGON
        self.private_key = os.getenv("POLYGON_WALLET_PRIVATE_KEY")
        self.polygon_rpc = os.getenv("POLYGON_RPC_URL", "https://polygon-mainnet.g.alchemy.com/v2/demo")
        self.w3 = Web3(Web3.HTTPProvider(self.polygon_rpc))

        self.usdc_address = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        self.erc20_approve = """[{"inputs":[{"internalType":"address","name":"spender","type":"address"},{"internalType":"uint256","name":"amount","type":"uint256"}],"name":"approve","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"address","name":"account","type":"address"}],"name":"balanceOf","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]"""

        self.web3 = Web3(Web3.HTTPProvider(self.polygon_rpc))
        self.web3.middleware_onion.inject(geth_poa_middleware, layer=0)

        self.usdc = self.web3.eth.contract(
            address=self.usdc_address, abi=self.erc20_approve
        )

        self._init_api_keys()

    def _init_api_keys(self) -> None:
        funder = os.getenv("POLYGON_FUNDER_ADDRESS")
        api_key = os.getenv("CLOB_API_KEY")
        api_secret = os.getenv("CLOB_API_SECRET")
        api_passphrase = os.getenv("CLOB_API_PASSPHRASE")

        if api_key and api_secret and api_passphrase:
            # Use pre-provisioned API creds directly — no create call needed
            creds = ApiCreds(
                api_key=api_key,
                api_secret=api_secret,
                api_passphrase=api_passphrase,
            )
        else:
            # Derive creds from the existing key — skips the create attempt that logs a 400
            l1_client = ClobClient(
                host=self.clob_url,
                chain_id=self.chain_id,
                key=self.private_key,
                signature_type=3,
                funder=funder,
            )
            creds = l1_client.derive_api_key()

        self.client = ClobClient(
            host=self.clob_url,
            chain_id=self.chain_id,
            key=self.private_key,
            creds=creds,
            signature_type=3,
            funder=funder,
        )

    def get_midpoint_price(self, token_id: str) -> float:
        """Return the bid/ask midpoint for a CLOB token (best neutral price estimate)."""
        result = self.client.get_midpoint(token_id)
        if isinstance(result, dict):
            return float(result.get("mid", result.get("price", 0)))
        return float(result)

    def get_open_positions(self) -> list:
        """Return current open positions via the Polymarket data API."""
        address = os.getenv("POLYGON_FUNDER_ADDRESS") or os.getenv("POLYGON_ADDRESS")
        if not address:
            return []
        try:
            resp = httpx.get(
                "https://data-api.polymarket.com/positions",
                params={"user": address, "limit": 500, "sizeThreshold": "0.01"},
                timeout=10,
            )
            if resp.status_code == 200:
                return [
                    p for p in resp.json()
                    if float(p.get("size", 0)) > 0.01
                    and not p.get("redeemable", False)
                    and float(p.get("curPrice", p.get("currentValue", 1))) > 0
                ]
            print(f"Positions API HTTP {resp.status_code}")
            return []
        except Exception as e:
            print(f"get_open_positions error: {e}")
            return []

    def get_last_trade_minutes_ago(self):
        """Return minutes since most recent trade via the Polymarket activity API.
        Returns None if unavailable. Survives redeploys — reads live API state."""
        address = os.getenv("POLYGON_FUNDER_ADDRESS") or os.getenv("POLYGON_ADDRESS")
        if not address:
            return None
        try:
            resp = httpx.get(
                "https://data-api.polymarket.com/activity",
                params={"user": address, "limit": 5},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data:
                    ts_raw = data[0].get("timestamp")
                    if ts_raw is not None:
                        ts = float(ts_raw)
                        if ts > 1e10:
                            ts /= 1000  # milliseconds → seconds
                        last_dt = datetime.datetime.utcfromtimestamp(ts)
                        return (datetime.datetime.utcnow() - last_dt).total_seconds() / 60
        except Exception as e:
            print(f"Activity API check failed: {e}")
        return None

    def get_held_token_ids(self) -> set:
        """Return set of CLOB token IDs currently held as open positions.
        Survives redeploys — reads live Polymarket positions."""
        held = set()
        try:
            for p in self.get_open_positions():
                asset = p.get("asset")
                if asset:
                    held.add(str(asset))
        except Exception as e:
            print(f"get_held_token_ids error: {e}")
        return held

    def get_address_for_private_key(self):
        account = self.w3.eth.account.from_key(str(self.private_key))
        return account.address

    def get_usdc_balance(self) -> float:
        # PRIMARY: ask the CLOB client for the COLLATERAL (USDC) balance it sees.
        # This reflects your Polymarket tradeable balance, which a raw on-chain
        # balanceOf cannot see for deposit/proxy wallets.
        try:
            from py_clob_client_v2 import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            ba = self.client.get_balance_allowance(params)
            raw = ba.get("balance") if isinstance(ba, dict) else getattr(ba, "balance", None)
            if raw is not None:
                val = float(raw) / 1e6  # USDC has 6 decimals
                if val > 0:
                    return val
        except Exception as e:
            print(f"CLOB balance read failed ({e}); falling back to on-chain.")

        # FALLBACK: on-chain balanceOf, funder first then signing wallet.
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
                val = float(bal / 1e6)
                if val > 0:
                    return val
            except Exception as e:
                print(f"Balance error for {addr}: {e}")
        return 0.0
