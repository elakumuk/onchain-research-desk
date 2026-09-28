"""Central configuration: the token universe and every analysis parameter.

Everything a reviewer might want to question (window lengths, participation
rates, AUM scenarios) lives here, so it can be changed in one place and is
printed into the report.
"""
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"          # timestamped cache of every API pull (gitignored)
SNAPSHOT_DIR = ROOT / "data" / "snapshot"  # one committed pull, so a fresh clone reproduces offline
REPORTS_DIR = ROOT / "reports"


@dataclass(frozen=True)
class Token:
    symbol: str
    cg_id: str               # CoinGecko coin id (prices, volumes, market caps)
    llama_slug: str | None   # DefiLlama fees/revenue slug (None = not tracked)
    cb_pair: str | None      # Coinbase Exchange USD product id (None = not listed)
    kraken_pair: str | None  # Kraken USD pair name (None = not listed)
    kind: str                # "L1", "L2", "DeFi", "Infra" -- for reading the tables


# Candidate universe. Each candidate is checked at run time; any token missing
# price history, a live USD order book, or (for the fundamentals table) fee
# data is dropped *with the reason written to reports/universe.csv*.
UNIVERSE: list[Token] = [
    Token("BTC", "bitcoin", "bitcoin", "BTC-USD", "XXBTZUSD", "L1"),
    Token("ETH", "ethereum", "ethereum", "ETH-USD", "XETHZUSD", "L1"),
    Token("SOL", "solana", "solana", "SOL-USD", "SOLUSD", "L1"),
    Token("HYPE", "hyperliquid", "hyperliquid", "HYPE-USD", "HYPEUSD", "DeFi"),
    Token("UNI", "uniswap", "uniswap", "UNI-USD", "UNIUSD", "DeFi"),
    Token("AAVE", "aave", "aave", "AAVE-USD", "AAVEUSD", "DeFi"),
    Token("SKY", "sky", "sky", "SKY-USD", "SKYUSD", "DeFi"),
    Token("LDO", "lido-dao", "lido", "LDO-USD", "LDOUSD", "DeFi"),
    Token("LINK", "chainlink", "chainlink", "LINK-USD", "LINKUSD", "Infra"),
    Token("ARB", "arbitrum", "arbitrum", "ARB-USD", "ARBUSD", "L2"),
    Token("OP", "optimism", "op-mainnet", "OP-USD", "OPUSD", "L2"),
    Token("PENDLE", "pendle", "pendle", "PENDLE-USD", "PENDLEUSD", "DeFi"),
    # JUP: Coinbase delisted the USD pair; Kraken still lists it.
    Token("JUP", "jupiter-exchange-solana", "jupiter", None, "JUPUSD", "DeFi"),
    Token("CRV", "curve-dao-token", "curve-finance", "CRV-USD", "CRVUSD", "DeFi"),
    Token("ENA", "ethena", "ethena", "ENA-USD", "ENAUSD", "DeFi"),
    Token("COMP", "compound-governance-token", "compound-finance", "COMP-USD", "COMPUSD", "DeFi"),
    Token("AERO", "aerodrome-finance", "aerodrome", "AERO-USD", "AEROUSD", "DeFi"),
    Token("ONDO", "ondo-finance", "ondo-finance", "ONDO-USD", "ONDOUSD", "DeFi"),
    Token("MORPHO", "morpho", "morpho", "MORPHO-USD", "MORPHOUSD", "DeFi"),
    Token("JTO", "jito-governance-token", "jito", "JTO-USD", "JTOUSD", "DeFi"),
]


@dataclass(frozen=True)
class Params:
    # --- caching -----------------------------------------------------------
    cache_ttl_hours: float = 12.0          # reuse a cached pull younger than this
    # --- liquidity ---------------------------------------------------------
    amihud_window_days: int = 90           # trailing window for the Amihud average
    adv_window_days: int = 30              # trailing window for average daily volume (median)
    depth_bands: tuple = (0.005, 0.01, 0.02)          # +/- 0.5%, 1%, 2% from mid
    order_sizes_usd: tuple = (100_000, 1_000_000, 10_000_000)
    book_keep_band: float = 0.15           # cache only book levels within +/-15% of mid
    participation_rates: tuple = (0.10, 0.20)          # max share of ADV we trade per day
    liquidation_position_usd: float = 10_000_000       # position size for days-to-liquidate
    # --- fundamentals ------------------------------------------------------
    runrate_window_days: int = 30
    min_fee_usd_for_flag: float = 1_000_000            # only flag protocols with real fees
    holder_accrual_flag_ratio: float = 0.01            # holders revenue < 1% of fees -> flag
    # --- portfolio ---------------------------------------------------------
    est_window_days: int = 90              # covariance / vol estimation lookback
    rebalance_every_days: int = 30
    max_adv_fraction: float = 0.10         # position <= 10% of trailing ADV
    aum_scenarios: tuple = (10_000_000, 100_000_000, 1_000_000_000)
    periods_per_year: int = 365            # crypto trades every calendar day
    cost_bps_assumption: float = 10.0      # flat one-way cost used for the "net" line


PARAMS = Params()
