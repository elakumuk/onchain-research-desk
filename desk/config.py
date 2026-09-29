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


# =========================================================================
# v2: long-history, point-in-time research layer (desk/longrun.py)
# =========================================================================
HISTORY_DIR = ROOT / "data" / "history" / "coinmetrics"   # committed, append-only store


@dataclass(frozen=True)
class HistAsset:
    symbol: str          # display symbol
    price_asset: str     # Coin Metrics id carrying PriceUSD, or the Binance symbol (price_source "binance")
    volume_asset: str    # Coin Metrics asset id that carries volume_reported_spot_usd_1d
    note: str = ""       # what happened to it, for the candidate table (documentation only)
    price_source: str = "coinmetrics"   # "coinmetrics" | "binance" (USDT close x Coin Metrics USDT/USD)


def _h(sym, price=None, volume=None, note=""):
    s = sym.lower()
    return HistAsset(sym, price or s, volume or s, note)


def _b(sym, volume=None, pair=None, note=""):
    """Priced from the Binance archive: <SYM>USDT close, converted to USD; volume from Coin Metrics."""
    return HistAsset(sym, pair or f"{sym}USDT", volume or sym.lower(), note, "binance")


# Candidate set for the point-in-time universe, frozen from the Coin Metrics
# Community catalog (GET /v4/catalog/assets, pulled 2026-09-29).
#
# THE RULE (not a hand-picked list): every asset for which the keyless
# Community API publishes BOTH a daily PriceUSD and a daily
# volume_reported_spot_usd_1d, minus stablecoins, wrapped/pegged assets and
# duplicate chain representations (HISTORY_EXCLUDED). Where the price is only
# published under a chain-specific id (avaxc, matic_eth, ...) and the volume
# under the base id, the two are paired; the pairing rule is applied to every
# such case, not only to the ones that did well.
#
# The list deliberately keeps assets that later collapsed, were delisted or
# were migrated. Assets whose trading stops are handled by the backtest's
# exit rule, never removed from history. The `note` column is documentation
# written after the fact; the code never reads it.
HISTORY_CANDIDATES: list[HistAsset] = [
    _h("BTC"), _h("ETH"), _h("XRP"), _h("LTC"), _h("BCH"), _h("BNB"), _h("ADA"), _h("DOGE"), _h("TRX"),
    _h("XLM"), _h("XMR"), _h("ETC"), _h("DASH"), _h("ZEC"), _h("XEM"), _h("DGB"), _h("DCR"), _h("NEO"),
    _h("GAS"), _h("XTZ"), _h("ALGO"), _h("DOT"), _h("ICP"), _h("FLOW"), _h("QNT"), _h("CRO"), _h("XVG"),
    _h("EOS", note="rebranded; price series ends 2026-07"),
    _h("BSV", note="Bitcoin SV fork; delisted by several venues in 2019"),
    _h("BTG", note="Bitcoin Gold; 51% attacks 2018/2020, series ends 2026-08"),
    _h("VTC", note="Vertcoin; price series ends 2023-12"),
    _h("MAID", note="MaidSafeCoin; migrated, series ends 2025-01"),
    _h("LINK"), _h("UNI"), _h("AAVE"), _h("CRV"), _h("COMP"), _h("LDO"), _h("SNX"), _h("YFI"), _h("SUSHI"),
    _h("BAL"), _h("UMA"), _h("1INCH"), _h("PERP"), _h("REN"), _h("KNC"), _h("ZRX"), _h("BAT"), _h("MANA"),
    _h("OMG"), _h("GNO"), _h("SNT"), _h("CVC"), _h("FUN"), _h("POWR"), _h("ELF"), _h("POLY"), _h("LPT"),
    _h("GRIN"), _h("DRGN"), _h("ALPHA"),
    _h("MKR", note="Maker; migrated to SKY, price series ends 2026-03"),
    _h("LEND", note="Aave's predecessor token; migrated to AAVE in 2020"),
    _h("GNT", note="Golem; migrated to GLM, trading stops 2022-11"),
    _h("REP", note="Augur; migrated to REPv2"),
    _h("ANT", note="Aragon; DAO dissolved and token redeemed, 2023-24"),
    _h("FTT", note="FTX exchange token; collapsed with FTX, Nov 2022"),
    _h("SRM", note="Serum; FTX/Alameda-linked, collapsed Nov 2022"),
    _h("HT", note="Huobi exchange token"),
    _h("LEO", price="leo_eth", volume="leo", note="Bitfinex exchange token"),
    _h("HEDG", note="HedgeTrade; collapsed 2020-21, series ends 2022-01"),
    _h("PPT", note="Populous; series ends 2022-09"),
    _h("WTC", note="Waltonchain; series ends 2024-06"),
    _h("PAY", note="TenX; wound down, series ends 2025-06"),
    _h("QASH", note="Liquid exchange token; Liquid hacked 2021, bankrupt with FTX 2022"),
    _h("SWRV", note="Swerve; abandoned DeFi fork, series ends 2025-03"),
    _h("REV", price="rev_eth", volume="rev_eth", note="Revain; series ends 2024-03"),
    # price under a chain-specific id, volume under the base id
    _h("AVAX", price="avaxc"), _h("MATIC", price="matic_eth", note="migrated to POL in 2024-25"),
    _h("POL", price="pol_eth"), _h("SHIB", price="shib_eth"), _h("LRC", price="lrc_eth"),
    _h("ICX", price="icx_eth"), _h("QTUM", price="qtum_eth"), _h("VET", price="vet_eth"),
    _h("ZIL", price="zil_eth"), _h("AE", price="ae_eth"),
    _h("AION", price="aion_eth", note="Aion; merged into OAN, series ends 2023-03"),
    _h("NAS", price="nas_eth", note="Nebulas; series ends 2024-12"),
    _h("BTM", price="btm_eth", note="Bytom; series ends 2025-06"),
]

# Coin Metrics ids with a community PriceUSD that are NOT candidates, and why.
HISTORY_EXCLUDED: dict[str, str] = {
    **{a: "stablecoin (designed not to move; not a risk asset)" for a in (
        "usdt", "usdt_omni", "usdt_eth", "usdt_trx", "usdt_avaxc", "usdc", "usdc_eth", "usdc_trx", "usdc_avaxc",
        "tusd", "tusd_eth", "tusd_trx", "gusd", "pax", "husd", "busd", "dai", "sai", "usdk", "frax_eth",
        "lusd_eth", "usdd_eth", "fdusd_eth", "pyusd_eth", "crvusd_eth", "usde_eth", "susde_eth", "usdm_eth",
        "sdai_eth", "eurc_eth", "buidl_eth")},
    **{a: "wrapped or pegged to another asset (double-counts the underlying)" for a in (
        "weth", "wbtc", "renbtc", "hbtc", "wnxm", "xaut", "paxg")},
    **{a: "duplicate chain representation of a candidate" for a in (
        "bnb_eth", "trx_eth", "eos_eth", "leo_eos", "flow_native", "flow_evm", "avaxp", "avaxx")},
    "nxm": "no reported volume in the Community API",
}

# Scan-derived candidates (desk/data/scan.py -> data/history/universe_scan.csv).
# Every risk asset that was ever in the top 50 by reported volume at a month
# start and has NO Coin Metrics price is priced from the Binance archive
# (<ID>USDT). The ones Binance never listed, or whose Binance ticker fails the
# volume check in longrun.load_panels, stay in the candidate set with no price:
# they cannot be held, and they are what the coverage diagnostic counts.
SCAN_FILE = ROOT / "data" / "history" / "universe_scan.csv"


def _scan_candidates() -> list[HistAsset]:
    import csv
    if not SCAN_FILE.exists():
        return []
    have = {h.volume_asset for h in HISTORY_CANDIDATES}
    used = {h.symbol for h in HISTORY_CANDIDATES}
    out = []
    with SCAN_FILE.open() as f:
        for row in csv.DictReader(f):
            a = row["asset"]
            if row["risk_asset"] != "True" or row["has_cm_price"] == "True" or a in have:
                continue
            base = a.split("_")[0]
            sym = base.upper() if base.upper() not in used else a.upper()    # ids are unique, tickers are not
            used.add(sym)
            out.append(HistAsset(sym, f"{base.upper()}USDT", a, row["name"], "binance"))
    return out


HISTORY_CANDIDATES = HISTORY_CANDIDATES + _scan_candidates()

# Current-universe tokens -> long-history candidate symbol, for the memos' long-run context
# (used only where the candidate exists and has >= LongRunParams.min_memo_history_days of prices).
# SKY is NOT mapped to MKR: 1 MKR converts to 24,000 SKY, and splicing two tokens' price series
# would invent a history neither token had. (Coin Metrics' "sky" id is Skycoin, a different token.)
MEMO_HISTORY_MAP: dict[str, str] = {s: s for s in (
    "BTC", "ETH", "SOL", "HYPE", "UNI", "AAVE", "LDO", "LINK", "ARB", "OP", "PENDLE", "JUP", "CRV", "ENA",
    "COMP", "AERO", "ONDO", "MORPHO", "JTO")}


@dataclass(frozen=True)
class LongRunParams:
    history_start: str = "2015-01-01"      # first day stored and used
    first_rebalance: str = "2016-01-01"    # first monthly rebalance of the long backtest
    # --- point-in-time universe (all evaluated with data strictly before the rebalance date)
    top_k: int = 20                        # hold at most this many assets
    min_history_days: int = 90             # >= this many days of prices before the date
    rank_window_days: int = 90             # rank by median reported volume over this window
    liquidity_floor_usd: float = 1_000_000  # ... and require that median to be at least this
    k_sensitivity: tuple = (10, 30)        # re-run the headline scheme with these K
    # --- death / delisting
    death_grace_days: int = 30             # data ending closer than this to the sample end is not a death
    exit_haircut: float = 0.0              # held asset stops trading: sold at last price x (1 - haircut)
    exit_haircut_stress: float = 1.0       # sensitivity: a total loss on every such exit
    # --- liquidity trend and memos
    amihud_roll_days: int = 365            # rolling window for the long-run Amihud series
    amihud_min_obs: int = 300              # need at least this many valid days in the window
    min_memo_history_days: int = 1095      # long-run context in a memo needs >= 3 years of prices
    # --- data store
    refetch_overlap_days: int = 7          # re-pull this many recent days each run (late revisions)
    binance_gap_days: int = 5              # a Binance series is used only up to its first gap this long
    binance_volume_ratio: tuple = (0.002, 1.25)  # median Binance / all-venue volume must lie here,
                                           # or the ticker is taken to be a different token
    # --- capacity over time: a month counts as "deep enough" for a fund size when the liquidity
    # cap moves at most this share of the designed (inverse-vol) portfolio
    deep_enough_moved: float = 0.10
    # --- statistics
    bootstrap_reps: int = 2000             # circular block bootstrap of the daily returns
    bootstrap_block_days: int = 20         # block length (keeps about a month of serial dependence)
    bootstrap_seed: int = 7
    # --- regimes: (slug, label, start, end). Boundaries are Bitcoin cycle turning points,
    # chosen with hindsight. They label periods for description; no portfolio decision uses them.
    regimes: tuple = (
        ("r2016_17", "2016-17 run-up to the ICO peak", "2016-01-01", "2017-12-17"),
        ("r2018", "2018 bust", "2017-12-18", "2018-12-15"),
        ("r2019_20", "2019 to Mar 2020, incl. COVID crash", "2018-12-16", "2020-03-31"),
        ("r2020_21", "2020-21 bull (DeFi, peak Nov 2021)", "2020-04-01", "2021-11-10"),
        ("r2022", "2022 deleveraging: LUNA, 3AC, FTX", "2021-11-11", "2022-11-21"),
        ("r2023_now", "Nov 2022 to now, incl. spot ETFs", "2022-11-22", "2099-12-31"),
    )


LONGRUN = LongRunParams()
