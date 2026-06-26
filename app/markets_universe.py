"""
markets_universe.py — the canonical list of tradable assets per exchange.

Previously this lived hard-coded inside the frontend JS. Centralizing it on
the backend makes it the single source of truth that drives the clickable
Markets dashboard and (optionally) bot creation.
"""

ALPACA_STOCKS = [
    ("AAPL", "Apple Inc."), ("MSFT", "Microsoft Corp."), ("GOOGL", "Alphabet Inc."),
    ("AMZN", "Amazon.com Inc."), ("NVDA", "NVIDIA Corp."), ("META", "Meta Platforms"),
    ("TSLA", "Tesla Inc."), ("BRK.B", "Berkshire Hathaway"), ("JPM", "JPMorgan Chase"),
    ("V", "Visa Inc."), ("UNH", "UnitedHealth Group"), ("XOM", "Exxon Mobil"),
    ("JNJ", "Johnson & Johnson"), ("PG", "Procter & Gamble"), ("MA", "Mastercard"),
    ("HD", "Home Depot"), ("AVGO", "Broadcom Inc."), ("LLY", "Eli Lilly"),
    ("ABBV", "AbbVie Inc."), ("MRK", "Merck & Co."), ("PEP", "PepsiCo"),
    ("KO", "Coca-Cola"), ("BAC", "Bank of America"), ("COST", "Costco Wholesale"),
    ("TMO", "Thermo Fisher"), ("NFLX", "Netflix Inc."), ("CRM", "Salesforce"),
    ("AMD", "Advanced Micro Devices"), ("ORCL", "Oracle Corp."), ("ACN", "Accenture"),
    ("DIS", "Walt Disney"), ("MCD", "McDonald's"), ("WMT", "Walmart Inc."),
    ("ADBE", "Adobe Inc."), ("TXN", "Texas Instruments"), ("INTC", "Intel Corp."),
    ("QCOM", "Qualcomm"), ("PFE", "Pfizer Inc."), ("INTU", "Intuit Inc."),
    ("IBM", "IBM Corp."), ("GS", "Goldman Sachs"), ("MS", "Morgan Stanley"),
    ("CSCO", "Cisco Systems"), ("NKE", "Nike Inc."), ("VZ", "Verizon"),
    ("T", "AT&T Inc."), ("CVX", "Chevron Corp."), ("UPS", "United Parcel Service"),
    ("RTX", "RTX Corp."), ("HON", "Honeywell"), ("CAT", "Caterpillar"),
    ("BA", "Boeing Co."), ("SPGI", "S&P Global"), ("NOW", "ServiceNow"),
    ("ISRG", "Intuitive Surgical"), ("DE", "Deere & Co."), ("GE", "GE Aerospace"),
    ("UNP", "Union Pacific"), ("PYPL", "PayPal Holdings"), ("SBUX", "Starbucks"),
    ("AXP", "American Express"), ("BKNG", "Booking Holdings"), ("ADP", "ADP Inc."),
    ("NEE", "NextEra Energy"), ("TMUS", "T-Mobile US"), ("ABT", "Abbott Labs"),
    ("BMY", "Bristol-Myers Squibb"), ("LIN", "Linde plc"), ("PM", "Philip Morris"),
    ("AMAT", "Applied Materials"), ("MU", "Micron Technology"), ("LRCX", "Lam Research"),
    ("REGN", "Regeneron"), ("GILD", "Gilead Sciences"), ("MDT", "Medtronic"),
    ("SYK", "Stryker Corp."), ("BLK", "BlackRock"), ("SCHW", "Charles Schwab"),
    ("TGT", "Target Corp."),
]

OKX_CRYPTO = [
    ("BTC", "Bitcoin"), ("ETH", "Ethereum"), ("SOL", "Solana"), ("BNB", "BNB"),
    ("XRP", "XRP"), ("ADA", "Cardano"), ("DOGE", "Dogecoin"), ("AVAX", "Avalanche"),
    ("DOT", "Polkadot"), ("MATIC", "Polygon"), ("LINK", "Chainlink"), ("LTC", "Litecoin"),
    ("ATOM", "Cosmos"), ("UNI", "Uniswap"), ("XLM", "Stellar"), ("TRX", "TRON"),
    ("ALGO", "Algorand"), ("FTM", "Fantom"), ("NEAR", "NEAR Protocol"), ("FIL", "Filecoin"),
    ("ICP", "Internet Computer"), ("SAND", "The Sandbox"), ("MANA", "Decentraland"),
    ("AXS", "Axie Infinity"), ("AAVE", "Aave"), ("COMP", "Compound"), ("MKR", "Maker"),
    ("SNX", "Synthetix"), ("YFI", "yearn.finance"), ("SUSHI", "SushiSwap"), ("CRV", "Curve DAO"),
    ("1INCH", "1inch"), ("ENJ", "Enjin Coin"), ("CHZ", "Chiliz"), ("ZEC", "Zcash"),
    ("DASH", "Dash"), ("XMR", "Monero"), ("THETA", "Theta Network"), ("VET", "VeChain"),
    ("EOS", "EOS"), ("RNDR", "Render"), ("APT", "Aptos"), ("ARB", "Arbitrum"),
    ("OP", "Optimism"), ("INJ", "Injective"), ("SUI", "Sui"), ("TIA", "Celestia"),
    ("WLD", "Worldcoin"), ("PYTH", "Pyth Network"), ("JUP", "Jupiter"), ("SEI", "Sei"),
    ("TON", "Toncoin"),
]

MARKET_UNIVERSE = {
    "alpaca": {
        "asset_class": "Equity",
        "quote": "USD",
        "items": [{"symbol": s, "name": n, "display": s} for s, n in ALPACA_STOCKS],
    },
    "okx": {
        "asset_class": "Crypto",
        "quote": "USDT",
        "items": [{"symbol": s, "name": n, "display": f"{s}/USDT"} for s, n in OKX_CRYPTO],
    },
}
