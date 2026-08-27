#!/usr/bin/env python3
"""
DUSD / USDT PancakeSwap V3 monitor on BNB Smart Chain.

Alerts:
1) Pool token-balance TVL estimate falls by LIQUIDITY_DROP_PCT (default 10%)
   from its persisted reference level.
2) A Pancake V3 Burn event removes position liquidity worth at least the same
   percentage of the reference TVL.
3) A swap has >= WHALE_USD_THRESHOLD (default $50,000) of USDT notional.
4) DUSD pool price falls below DEPEG_THRESHOLD (default 0.998 USDT).
5) Every completed DUSD withdrawal from the StandX Highway.
6) Every StandX Gateway redemption request and completed redemption payout.

The script is read-only. It never needs a wallet/private key.
"""

import json
import logging
import math
import os
import time
from decimal import Decimal, InvalidOperation, getcontext
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv
from web3 import Web3

getcontext().prec = 80
load_dotenv()

# ---------------------------- Configuration ----------------------------

def decimal_env(name: str, default: str) -> Decimal:
    raw = os.getenv(name, default).strip()
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise SystemExit(f"{name} must be a valid number, got {raw!r}") from exc


def int_env(name: str, default: str) -> int:
    raw = os.getenv(name, default).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from exc


def float_env(name: str, default: str) -> float:
    raw = os.getenv(name, default).strip()
    try:
        return float(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be a valid number, got {raw!r}") from exc

POOL_ADDRESS_RAW = "0xB67e5EaF770a384Ab28029d08B9bC5EBE32beb0F"
DUSD_ADDRESS_RAW = "0xaf44a1e76f56ee12adbb7ba8acd3cbd474888122"
USDT_ADDRESS_RAW = "0x55d398326f99059fF775485246999027B3197955"
STANDX_HIGHWAY_ADDRESS_RAW = "0x90bb5bdC6Acd166237640C8707a694f1Fc3AAB84"
STANDX_GATEWAY_ADDRESS_RAW = "0x00b4F9B510893505aceFB10eC91cBC972185088e"

RPC_URL = os.getenv("BSC_RPC_URL", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

POLL_SECONDS = float_env("POLL_SECONDS", "20")
CONFIRMATIONS = int_env("CONFIRMATIONS", "2")
LOG_BLOCK_CHUNK = int_env("LOG_BLOCK_CHUNK", "200")
MAX_BACKFILL_BLOCKS = int_env("MAX_BACKFILL_BLOCKS", "0")

LIQUIDITY_DROP_PCT = decimal_env("LIQUIDITY_DROP_PCT", "10") / Decimal("100")
WHALE_USD_THRESHOLD = decimal_env("WHALE_USD_THRESHOLD", "50000")
DEPEG_THRESHOLD = decimal_env("DEPEG_THRESHOLD", "0.998")
DEPEG_REARM = decimal_env("DEPEG_REARM", "0.999")
SEND_STARTUP_MESSAGE = os.getenv("SEND_STARTUP_MESSAGE", "true").lower() in {
    "1", "true", "yes", "y", "on"
}

STATE_FILE = Path(os.getenv("STATE_FILE", "dusd_pool_monitor_state.json"))

BSC_CHAIN_ID = 56
EXPECTED_FEE = 100  # 0.01%; V3 fee is in hundredths of a bip (1e-6 units).

# Minimal ABI: only methods/events used by this monitor.
POOL_ABI = [
    {
        "inputs": [],
        "name": "token0",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "token1",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "fee",
        "outputs": [{"internalType": "uint24", "name": "", "type": "uint24"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "slot0",
        "outputs": [
            {"internalType": "uint160", "name": "sqrtPriceX96", "type": "uint160"},
            {"internalType": "int24", "name": "tick", "type": "int24"},
            {"internalType": "uint16", "name": "observationIndex", "type": "uint16"},
            {"internalType": "uint16", "name": "observationCardinality", "type": "uint16"},
            {"internalType": "uint16", "name": "observationCardinalityNext", "type": "uint16"},
            {"internalType": "uint32", "name": "feeProtocol", "type": "uint32"},
            {"internalType": "bool", "name": "unlocked", "type": "bool"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "address", "name": "sender", "type": "address"},
            {"indexed": True, "internalType": "address", "name": "recipient", "type": "address"},
            {"indexed": False, "internalType": "int256", "name": "amount0", "type": "int256"},
            {"indexed": False, "internalType": "int256", "name": "amount1", "type": "int256"},
            {"indexed": False, "internalType": "uint160", "name": "sqrtPriceX96", "type": "uint160"},
            {"indexed": False, "internalType": "uint128", "name": "liquidity", "type": "uint128"},
            {"indexed": False, "internalType": "int24", "name": "tick", "type": "int24"},
            {"indexed": False, "internalType": "uint128", "name": "protocolFeesToken0", "type": "uint128"},
            {"indexed": False, "internalType": "uint128", "name": "protocolFeesToken1", "type": "uint128"},
        ],
        "name": "Swap",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "address", "name": "owner", "type": "address"},
            {"indexed": True, "internalType": "int24", "name": "tickLower", "type": "int24"},
            {"indexed": True, "internalType": "int24", "name": "tickUpper", "type": "int24"},
            {"indexed": False, "internalType": "uint128", "name": "amount", "type": "uint128"},
            {"indexed": False, "internalType": "uint256", "name": "amount0", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "amount1", "type": "uint256"},
        ],
        "name": "Burn",
        "type": "event",
    },
]

ERC20_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "from", "type": "address"},
            {"indexed": True, "name": "to", "type": "address"},
            {"indexed": False, "name": "value", "type": "uint256"},
        ],
        "name": "Transfer",
        "type": "event",
    },
    {
        "constant": True,
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function",
    },
]

STANDX_GATEWAY_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "user", "type": "address"},
            {"indexed": False, "name": "amount", "type": "uint256"},
            {"indexed": False, "name": "id", "type": "uint256"},
        ],
        "name": "WithdrawRequest",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "user", "type": "address"},
            {"indexed": False, "name": "amount", "type": "uint256"},
            {"indexed": False, "name": "id", "type": "uint256"},
        ],
        "name": "Withdraw",
        "type": "event",
    },
]

SWAP_TOPIC = Web3.to_hex(
    Web3.keccak(text="Swap(address,address,int256,int256,uint160,uint128,int24,uint128,uint128)")
).lower()
BURN_TOPIC = Web3.to_hex(
    Web3.keccak(text="Burn(address,int24,int24,uint128,uint256,uint256)")
).lower()
TRANSFER_TOPIC = Web3.to_hex(
    Web3.keccak(text="Transfer(address,address,uint256)")
).lower()
WITHDRAW_REQUEST_TOPIC = Web3.to_hex(
    Web3.keccak(text="WithdrawRequest(address,uint256,uint256)")
).lower()
WITHDRAW_TOPIC = Web3.to_hex(
    Web3.keccak(text="Withdraw(address,uint256,uint256)")
).lower()


def address_topic(address: str) -> str:
    """Encode an address for an indexed event-topic filter."""
    return "0x" + ("0" * 24) + address.removeprefix("0x").lower()


STANDX_HIGHWAY_TOPIC = address_topic(STANDX_HIGHWAY_ADDRESS_RAW)

# ------------------------------ Logging -------------------------------

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("dusd-monitor")


def die(message: str) -> None:
    log.error(message)
    raise SystemExit(1)


if not RPC_URL:
    die("Missing BSC_RPC_URL in environment/.env")
if not TELEGRAM_BOT_TOKEN:
    die("Missing TELEGRAM_BOT_TOKEN in environment/.env")
if not TELEGRAM_CHAT_ID:
    die("Missing TELEGRAM_CHAT_ID in environment/.env")

decimal_settings = (
    LIQUIDITY_DROP_PCT,
    WHALE_USD_THRESHOLD,
    DEPEG_THRESHOLD,
    DEPEG_REARM,
)
if not all(value.is_finite() for value in decimal_settings):
    die("Alert thresholds must be finite numbers")
if DEPEG_THRESHOLD <= 0:
    die("DEPEG_THRESHOLD must be positive")
if DEPEG_REARM <= DEPEG_THRESHOLD:
    die("DEPEG_REARM must be greater than DEPEG_THRESHOLD")
if not (Decimal("0") < LIQUIDITY_DROP_PCT < Decimal("1")):
    die("LIQUIDITY_DROP_PCT must be between 0 and 100")
if WHALE_USD_THRESHOLD <= 0:
    die("WHALE_USD_THRESHOLD must be positive")
if CONFIRMATIONS < 0:
    die("CONFIRMATIONS cannot be negative")
if not math.isfinite(POLL_SECONDS) or POLL_SECONDS <= 0:
    die("POLL_SECONDS must be positive")
if LOG_BLOCK_CHUNK <= 0:
    die("LOG_BLOCK_CHUNK must be positive")
if MAX_BACKFILL_BLOCKS < 0:
    die("MAX_BACKFILL_BLOCKS cannot be negative")


# -------------------------- Web3 / contracts --------------------------

w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 20}))

POOL_ADDRESS = Web3.to_checksum_address(POOL_ADDRESS_RAW)
DUSD_ADDRESS = Web3.to_checksum_address(DUSD_ADDRESS_RAW)
USDT_ADDRESS = Web3.to_checksum_address(USDT_ADDRESS_RAW)
STANDX_HIGHWAY_ADDRESS = Web3.to_checksum_address(STANDX_HIGHWAY_ADDRESS_RAW)
STANDX_GATEWAY_ADDRESS = Web3.to_checksum_address(STANDX_GATEWAY_ADDRESS_RAW)

pool = w3.eth.contract(address=POOL_ADDRESS, abi=POOL_ABI)
dusd = w3.eth.contract(address=DUSD_ADDRESS, abi=ERC20_ABI)
usdt = w3.eth.contract(address=USDT_ADDRESS, abi=ERC20_ABI)
standx_gateway = w3.eth.contract(
    address=STANDX_GATEWAY_ADDRESS, abi=STANDX_GATEWAY_ABI
)

token0: str
token1: str
decimals0: int
decimals1: int
dusd_decimals: int
usdt_decimals: int


# ------------------------------ State --------------------------------

DEFAULT_STATE: Dict[str, Any] = {
    "last_block": None,
    "reference_tvl_usd": None,
    "depeg_active": False,
}


def load_state() -> Dict[str, Any]:
    state = DEFAULT_STATE.copy()
    if STATE_FILE.exists():
        try:
            loaded = json.loads(STATE_FILE.read_text())
            if isinstance(loaded, dict):
                state.update(loaded)
        except Exception:
            log.exception("Could not read state file; starting with fresh state")

    # A hand-edited, truncated, or old state file must not trap the monitor in
    # a permanent retry loop. Invalid values are reset independently.
    try:
        if isinstance(state["last_block"], bool):
            raise ValueError("boolean is not a block number")
        if state["last_block"] is not None:
            state["last_block"] = int(state["last_block"])
            if state["last_block"] < 0:
                raise ValueError("negative block number")
    except (KeyError, TypeError, ValueError):
        log.warning("Invalid last_block in state file; resetting block cursor")
        state["last_block"] = None

    try:
        if state["reference_tvl_usd"] is not None:
            reference = Decimal(str(state["reference_tvl_usd"]))
            if not reference.is_finite() or reference <= 0:
                raise ValueError("invalid TVL reference")
            state["reference_tvl_usd"] = str(reference)
    except (InvalidOperation, KeyError, TypeError, ValueError):
        log.warning("Invalid reference_tvl_usd in state file; resetting reference")
        state["reference_tvl_usd"] = None

    if not isinstance(state.get("depeg_active"), bool):
        log.warning("Invalid depeg_active in state file; resetting depeg state")
        state["depeg_active"] = False

    return state


state = load_state()


def save_state() -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(STATE_FILE)


# ---------------------------- Formatting -----------------------------

def money(x: Decimal) -> str:
    return f"${x:,.2f}"


def pct(x: Decimal) -> str:
    return f"{x * Decimal('100'):.2f}%"


def tx_url(tx_hash: str) -> str:
    return f"https://bscscan.com/tx/{tx_hash}"


# ---------------------------- Telegram -------------------------------

def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    last_error: Optional[Exception] = None
    for attempt in range(1, 4):
        try:
            r = requests.post(url, data=payload, timeout=12)
            try:
                body = r.json()
            except ValueError as exc:
                raise RuntimeError(
                    f"Telegram returned HTTP {r.status_code} with invalid JSON"
                ) from exc

            if not r.ok:
                description = body.get("description", "request rejected")
                raise RuntimeError(
                    f"Telegram returned HTTP {r.status_code}: {description}"
                )
            if not body.get("ok"):
                raise RuntimeError(f"Telegram API returned: {body}")
            return
        except Exception as exc:
            last_error = exc
            # requests exceptions may include the request URL, which embeds the
            # bot token. Never copy that credential into service logs.
            safe_error = str(exc).replace(TELEGRAM_BOT_TOKEN, "<redacted>")
            log.warning("Telegram send failed (%d/3): %s", attempt, safe_error)
            if attempt < 3:
                time.sleep(attempt * 2)

    safe_error = str(last_error).replace(TELEGRAM_BOT_TOKEN, "<redacted>")
    raise RuntimeError(f"Telegram alert failed after retries: {safe_error}")


# -------------------------- Price / balances --------------------------

def token1_per_token0_from_sqrt(sqrt_price_x96: int) -> Decimal:
    """
    Convert Pancake V3 sqrtPriceX96 into human token1/token0.

    slot0 sqrtPriceX96 is sqrt(raw token1 / raw token0) * 2^96.
    Decimal scaling converts raw base units into whole-token units.
    """
    raw_ratio = (
        Decimal(sqrt_price_x96) * Decimal(sqrt_price_x96)
        / Decimal(2 ** 192)
    )
    decimal_scale = Decimal(10) ** Decimal(decimals0 - decimals1)
    return raw_ratio * decimal_scale


def dusd_price_from_sqrt(sqrt_price_x96: int) -> Decimal:
    """Return DUSD price denominated in USDT."""
    t1_per_t0 = token1_per_token0_from_sqrt(sqrt_price_x96)
    if t1_per_t0 <= 0:
        raise ValueError("Invalid zero/negative pool price")

    if token0.lower() == DUSD_ADDRESS.lower() and token1.lower() == USDT_ADDRESS.lower():
        # token1/token0 = USDT/DUSD
        return t1_per_t0

    if token0.lower() == USDT_ADDRESS.lower() and token1.lower() == DUSD_ADDRESS.lower():
        # token1/token0 = DUSD/USDT, so invert to obtain USDT/DUSD.
        return Decimal(1) / t1_per_t0

    raise RuntimeError("Configured pool is not DUSD/USDT")


def raw_to_human(raw: int, decimals: int) -> Decimal:
    return Decimal(raw) / (Decimal(10) ** decimals)


def split_amounts(amount0: int, amount1: int) -> tuple[Decimal, Decimal]:
    """Return (DUSD amount, USDT amount), retaining event signs."""
    a0 = raw_to_human(amount0, decimals0)
    a1 = raw_to_human(amount1, decimals1)

    if token0.lower() == DUSD_ADDRESS.lower():
        return a0, a1
    return a1, a0


def current_snapshot(block_number: Optional[int] = None) -> Dict[str, Decimal]:
    """Return a pool snapshot, optionally pinned to a specific block."""
    call_kwargs = (
        {"block_identifier": block_number} if block_number is not None else {}
    )
    sqrt_price_x96 = int(pool.functions.slot0().call(**call_kwargs)[0])
    price = dusd_price_from_sqrt(sqrt_price_x96)

    dusd_balance = raw_to_human(
        int(dusd.functions.balanceOf(POOL_ADDRESS).call(**call_kwargs)), dusd_decimals
    )
    usdt_balance = raw_to_human(
        int(usdt.functions.balanceOf(POOL_ADDRESS).call(**call_kwargs)), usdt_decimals
    )

    # For a stable/stable pool, nominal token-balance sum is a useful withdrawal
    # detector and is less sensitive to DUSD's own small depeg than marked TVL.
    nominal_tvl = dusd_balance + usdt_balance
    marked_tvl = usdt_balance + (dusd_balance * price)

    return {
        "price": price,
        "dusd_balance": dusd_balance,
        "usdt_balance": usdt_balance,
        "nominal_tvl": nominal_tvl,
        "marked_tvl": marked_tvl,
    }


# ---------------------------- Alert logic ----------------------------

def check_depeg(price: Decimal, tx_hash: Optional[str] = None) -> None:
    active = bool(state.get("depeg_active", False))

    if price < DEPEG_THRESHOLD and not active:
        link = f'\n<a href="{tx_url(tx_hash)}">BscScan transaction</a>' if tx_hash else ""
        send_telegram(
            "🔴 <b>DUSD DEPEG ALERT</b>\n"
            f"DUSD price: <b>{price:.6f} USDT</b>\n"
            f"Threshold: {DEPEG_THRESHOLD} USDT"
            f"{link}"
        )
        state["depeg_active"] = True
        save_state()
        log.warning("DUSD depeg alert: %s", price)

    elif price >= DEPEG_REARM and active:
        # Rearm silently so a future fresh depeg can alert again.
        state["depeg_active"] = False
        save_state()
        log.info("DUSD depeg alert re-armed at price %s", price)


def handle_swap(event: Any) -> None:
    args = event["args"]
    tx_hash = Web3.to_hex(event["transactionHash"])

    dusd_delta, usdt_delta = split_amounts(int(args["amount0"]), int(args["amount1"]))
    usdt_notional = abs(usdt_delta)
    price_after = dusd_price_from_sqrt(int(args["sqrtPriceX96"]))

    if dusd_delta > 0:
        direction = "SELL DUSD → USDT"
    elif dusd_delta < 0:
        direction = "BUY DUSD ← USDT"
    else:
        direction = "UNKNOWN"

    # Save depeg state before attempting the independent whale alert. If the
    # whale notification fails, retrying this event will not resend a depeg
    # notification that Telegram already accepted.
    check_depeg(price_after, tx_hash)

    if usdt_notional >= WHALE_USD_THRESHOLD:
        send_telegram(
            "🐋 <b>LARGE DUSD POOL SWAP</b>\n"
            f"Direction: <b>{direction}</b>\n"
            f"USDT notional: <b>{money(usdt_notional)}</b>\n"
            f"DUSD amount: {abs(dusd_delta):,.2f}\n"
            f"Price after: <b>{price_after:.6f} USDT</b>\n"
            f'Sender: <code>{args["sender"]}</code>\n'
            f'<a href="{tx_url(tx_hash)}">View transaction on BscScan</a>'
        )
        log.warning(
            "Large swap %s | notional=%s | price=%s | tx=%s",
            direction, usdt_notional, price_after, tx_hash,
        )


def handle_burn(event: Any) -> None:
    args = event["args"]
    tx_hash = Web3.to_hex(event["transactionHash"])

    dusd_amount, usdt_amount = split_amounts(int(args["amount0"]), int(args["amount1"]))
    dusd_amount = abs(dusd_amount)
    usdt_amount = abs(usdt_amount)

    # The reference TVL is nominal (one dollar per stablecoin), so value a Burn
    # on the same basis. This is consistent and avoids requiring archive-node
    # eth_call support while backfilling old Burn events.
    burn_value = usdt_amount + dusd_amount

    ref_raw = state.get("reference_tvl_usd")
    if ref_raw is None:
        return

    reference = Decimal(str(ref_raw))
    if reference <= 0:
        return

    burn_share = burn_value / reference
    if burn_share >= LIQUIDITY_DROP_PCT:
        send_telegram(
            "🚨 <b>LARGE LIQUIDITY BURN</b>\n"
            f"Estimated position liquidity removed: <b>{money(burn_value)}</b>\n"
            f"Share of reference pool TVL: <b>{pct(burn_share)}</b>\n"
            f"DUSD removed from position: {dusd_amount:,.2f}\n"
            f"USDT removed from position: {usdt_amount:,.2f}\n"
            f"Tick range: {args['tickLower']} → {args['tickUpper']}\n"
            "Note: a V3 Burn reduces position liquidity; token transfer out of "
            "the pool can occur via Collect.\n"
            f'<a href="{tx_url(tx_hash)}">View transaction on BscScan</a>'
        )
        log.warning(
            "Large Burn | value=%s | share=%s | tx=%s",
            burn_value, burn_share, tx_hash,
        )


def handle_standx_withdraw(event: Any) -> None:
    """Alert on a completed DUSD withdrawal from the StandX Highway."""
    args = event["args"]
    tx_hash = Web3.to_hex(event["transactionHash"])
    amount = raw_to_human(int(args["value"]), dusd_decimals)

    send_telegram(
        "🏦 <b>STANDX WITHDRAWAL COMPLETED</b>\n"
        f"DUSD withdrawn: <b>{amount:,.6f} DUSD</b>\n"
        f"Nominal value: <b>{money(amount)}</b>\n"
        f'Recipient: <code>{args["to"]}</code>\n'
        f'<a href="{tx_url(tx_hash)}">View transaction on BscScan</a>'
    )
    log.warning(
        "StandX withdrawal | amount=%s | recipient=%s | tx=%s",
        amount, args["to"], tx_hash,
    )


def handle_standx_redeem_request(event: Any) -> None:
    """Alert when DUSD is burned to start the StandX redemption delay."""
    args = event["args"]
    tx_hash = Web3.to_hex(event["transactionHash"])
    amount = raw_to_human(int(args["amount"]), dusd_decimals)

    send_telegram(
        "⏳ <b>STANDX REDEMPTION REQUESTED</b>\n"
        f"DUSD burned: <b>{amount:,.6f} DUSD</b>\n"
        f"Nominal value: <b>{money(amount)}</b>\n"
        f'Redeemer: <code>{args["user"]}</code>\n'
        f'Redemption ID: <code>{args["id"]}</code>\n'
        f'<a href="{tx_url(tx_hash)}">View transaction on BscScan</a>'
    )
    log.warning(
        "StandX redemption requested | amount=%s | user=%s | id=%s | tx=%s",
        amount, args["user"], args["id"], tx_hash,
    )


def handle_standx_redeem(event: Any) -> None:
    """Alert when StandX completes a redemption with a base-asset payout."""
    args = event["args"]
    tx_hash = Web3.to_hex(event["transactionHash"])
    # BSC USDT and USDC use 18 decimals. The Gateway event intentionally does
    # not identify which supported base asset was paid.
    amount = raw_to_human(int(args["amount"]), 18)

    send_telegram(
        "💸 <b>STANDX REDEMPTION COMPLETED</b>\n"
        f"Base asset paid: <b>{amount:,.6f} USDT/USDC</b>\n"
        f"Nominal value: <b>{money(amount)}</b>\n"
        f'Redeemer: <code>{args["user"]}</code>\n'
        f'Redemption ID: <code>{args["id"]}</code>\n'
        f'<a href="{tx_url(tx_hash)}">View transaction on BscScan</a>'
    )
    log.warning(
        "StandX redemption completed | amount=%s | user=%s | id=%s | tx=%s",
        amount, args["user"], args["id"], tx_hash,
    )


def check_tvl(snapshot: Dict[str, Decimal]) -> None:
    current = snapshot["nominal_tvl"]
    ref_raw = state.get("reference_tvl_usd")

    if ref_raw is None:
        state["reference_tvl_usd"] = str(current)
        save_state()
        return

    reference = Decimal(str(ref_raw))
    if reference <= 0:
        state["reference_tvl_usd"] = str(current)
        save_state()
        return

    # Raise the reference when liquidity grows. This means the threshold is
    # measured from the highest observed level since the last drop alert.
    if current > reference:
        state["reference_tvl_usd"] = str(current)
        save_state()
        return

    drop = (reference - current) / reference
    if drop >= LIQUIDITY_DROP_PCT:
        send_telegram(
            "🚨 <b>POOL LIQUIDITY DROP</b>\n"
            f"Reference TVL estimate: <b>{money(reference)}</b>\n"
            f"Current TVL estimate: <b>{money(current)}</b>\n"
            f"Drop: <b>{pct(drop)}</b>\n"
            f"DUSD in pool: {snapshot['dusd_balance']:,.2f}\n"
            f"USDT in pool: {snapshot['usdt_balance']:,.2f}\n"
            f"DUSD price: {snapshot['price']:.6f} USDT\n"
            f'<a href="https://bscscan.com/address/{POOL_ADDRESS}">View pool on BscScan</a>'
        )
        log.warning(
            "TVL drop alert | reference=%s current=%s drop=%s",
            reference, current, drop,
        )

        # Reset after an alert so another additional 10% fall can trigger again.
        state["reference_tvl_usd"] = str(current)
        save_state()


# ------------------------------ Logs ---------------------------------

def fetch_pool_and_gateway_logs(from_block: int, to_block: int) -> list:
    return list(w3.eth.get_logs(
        {
            "fromBlock": from_block,
            "toBlock": to_block,
            "address": [POOL_ADDRESS, STANDX_GATEWAY_ADDRESS],
            # OR filter across the pool and Gateway. Address scoping prevents
            # identically named events from unrelated contracts matching.
            "topics": [[
                SWAP_TOPIC,
                BURN_TOPIC,
                WITHDRAW_REQUEST_TOPIC,
                WITHDRAW_TOPIC,
            ]],
        }
    ))


def fetch_standx_withdraw_logs(from_block: int, to_block: int) -> list:
    return list(w3.eth.get_logs(
        {
            "fromBlock": from_block,
            "toBlock": to_block,
            "address": DUSD_ADDRESS,
            # A completed StandX withdrawal releases DUSD from the Highway.
            "topics": [TRANSFER_TOPIC, STANDX_HIGHWAY_TOPIC],
        }
    ))


def fetch_relevant_logs(from_block: int, to_block: int) -> list:
    return (
        fetch_pool_and_gateway_logs(from_block, to_block)
        + fetch_standx_withdraw_logs(from_block, to_block)
    )


def process_block_range(from_block: int, to_block: int) -> None:
    logs = fetch_relevant_logs(from_block, to_block)
    logs = sorted(logs, key=lambda x: (int(x["blockNumber"]), int(x["logIndex"])))

    logs_by_block: Dict[int, list] = {}
    for entry in logs:
        logs_by_block.setdefault(int(entry["blockNumber"]), []).append(entry)

    for block_number in sorted(logs_by_block):
        for entry in logs_by_block[block_number]:
            # Web3 6 and 7 differ in whether HexBytes.hex() includes "0x".
            # Web3.to_hex is stable across both versions.
            topic0 = Web3.to_hex(entry["topics"][0]).lower()

            if topic0 == SWAP_TOPIC:
                event = pool.events.Swap().process_log(entry)
                handle_swap(event)

            elif topic0 == BURN_TOPIC:
                event = pool.events.Burn().process_log(entry)
                handle_burn(event)

            elif topic0 == TRANSFER_TOPIC:
                event = dusd.events.Transfer().process_log(entry)
                handle_standx_withdraw(event)

            elif topic0 == WITHDRAW_REQUEST_TOPIC:
                event = standx_gateway.events.WithdrawRequest().process_log(entry)
                handle_standx_redeem_request(event)

            elif topic0 == WITHDRAW_TOPIC:
                event = standx_gateway.events.Withdraw().process_log(entry)
                handle_standx_redeem(event)

        # Commit after every complete event-bearing block. A later failure in a
        # large RPC chunk then cannot replay alerts from earlier blocks.
        state["last_block"] = block_number
        save_state()

    # Empty blocks and the tail after the final event are also complete.
    state["last_block"] = to_block
    save_state()


# --------------------------- Initialization --------------------------

def validate_chain_and_pool() -> None:
    global token0, token1, decimals0, decimals1, dusd_decimals, usdt_decimals

    if not w3.is_connected():
        die("Could not connect to BSC RPC")

    chain_id = int(w3.eth.chain_id)
    if chain_id != BSC_CHAIN_ID:
        die(f"Wrong chain ID: got {chain_id}, expected BSC mainnet {BSC_CHAIN_ID}")

    if not w3.eth.get_code(POOL_ADDRESS):
        die("No contract code found at configured pool address")
    if not w3.eth.get_code(STANDX_HIGHWAY_ADDRESS):
        die("No contract code found at configured StandX Highway address")
    if not w3.eth.get_code(STANDX_GATEWAY_ADDRESS):
        die("No contract code found at configured StandX Gateway address")

    token0 = Web3.to_checksum_address(pool.functions.token0().call())
    token1 = Web3.to_checksum_address(pool.functions.token1().call())

    expected = {DUSD_ADDRESS.lower(), USDT_ADDRESS.lower()}
    actual = {token0.lower(), token1.lower()}
    if actual != expected:
        die(
            "Pool token mismatch. "
            f"Pool has {token0}/{token1}, expected DUSD/USDT."
        )

    fee = int(pool.functions.fee().call())
    if fee != EXPECTED_FEE:
        die(f"Pool fee mismatch: got {fee}, expected {EXPECTED_FEE} (0.01%)")

    c0 = w3.eth.contract(address=token0, abi=ERC20_ABI)
    c1 = w3.eth.contract(address=token1, abi=ERC20_ABI)
    decimals0 = int(c0.functions.decimals().call())
    decimals1 = int(c1.functions.decimals().call())
    dusd_decimals = int(dusd.functions.decimals().call())
    usdt_decimals = int(usdt.functions.decimals().call())

    if dusd_decimals != 6:
        die(f"Unexpected DUSD decimals: {dusd_decimals}, expected 6")
    if usdt_decimals != 18:
        die(f"Unexpected BSC USDT decimals: {usdt_decimals}, expected 18")

    # Some otherwise healthy BNB Chain endpoints disable eth_getLogs. Catch
    # that before announcing startup; without it, pool events and StandX
    # withdrawals cannot be monitored at all.
    probe_block = max(0, int(w3.eth.block_number) - CONFIRMATIONS)
    try:
        fetch_relevant_logs(probe_block, probe_block)
    except Exception:
        die(
            "BSC RPC does not support the required one-block eth_getLogs query. "
            "Use an endpoint with eth_getLogs enabled."
        )

    log.info(
        "Validated pool %s | token0=%s (%d) token1=%s (%d) fee=%d",
        POOL_ADDRESS, token0, decimals0, token1, decimals1, fee,
    )


def initialize_state() -> None:
    latest = int(w3.eth.block_number)
    target = max(0, latest - CONFIRMATIONS)
    snap = current_snapshot(target)

    if state.get("reference_tvl_usd") is None:
        state["reference_tvl_usd"] = str(snap["nominal_tvl"])

    if state.get("last_block") is None:
        # Start at the current confirmed head. We monitor new events from here.
        state["last_block"] = target

    last_block = int(state["last_block"])
    if MAX_BACKFILL_BLOCKS > 0 and target - last_block > MAX_BACKFILL_BLOCKS:
        old = last_block
        state["last_block"] = target - MAX_BACKFILL_BLOCKS
        log.warning(
            "State is %d blocks behind; limiting backfill from %d to %d blocks",
            target - old, old, MAX_BACKFILL_BLOCKS,
        )

    save_state()

    log.info(
        "Initial snapshot | DUSD=%s USDT | nominal TVL=%s | marked TVL=%s | "
        "DUSD balance=%s | USDT balance=%s",
        snap["price"], snap["nominal_tvl"], snap["marked_tvl"],
        snap["dusd_balance"], snap["usdt_balance"],
    )

    if SEND_STARTUP_MESSAGE:
        send_telegram(
            "✅ <b>DUSD POOL MONITOR STARTED</b>\n"
            f"DUSD price: <b>{snap['price']:.6f} USDT</b>\n"
            f"Pool TVL estimate: <b>{money(snap['nominal_tvl'])}</b>\n"
            f"Large swap threshold: {money(WHALE_USD_THRESHOLD)}\n"
            "StandX withdrawal/redeem alerts: every event\n"
            f"Liquidity-drop threshold: {pct(LIQUIDITY_DROP_PCT)}\n"
            f"Depeg threshold: {DEPEG_THRESHOLD} USDT\n"
            f"Confirmed-block delay: {CONFIRMATIONS} blocks"
        )

    # Alert immediately if monitor starts while already below threshold.
    check_depeg(snap["price"])
    check_tvl(snap)


# ------------------------------- Main --------------------------------

def main() -> None:
    validate_chain_and_pool()
    initialize_state()

    log.info("Monitoring started. Press Ctrl+C to stop.")

    while True:
        try:
            latest = int(w3.eth.block_number)
            target = max(0, latest - CONFIRMATIONS)
            last_block = int(state["last_block"])

            if target > last_block:
                start = last_block + 1

                while start <= target:
                    end = min(start + LOG_BLOCK_CHUNK - 1, target)
                    process_block_range(start, end)
                    start = end + 1

                # Snapshot after all newly confirmed logs are processed.
                snap = current_snapshot(target)
                check_depeg(snap["price"])
                check_tvl(snap)

                log.info(
                    "block=%d | DUSD=%s | TVL=%s | DUSD=%s | USDT=%s",
                    target,
                    f"{snap['price']:.6f}",
                    money(snap["nominal_tvl"]),
                    f"{snap['dusd_balance']:,.2f}",
                    f"{snap['usdt_balance']:,.2f}",
                )

            time.sleep(POLL_SECONDS)

        except KeyboardInterrupt:
            log.info("Stopped by user")
            return

        except Exception:
            # Do not advance state on an error. This lets event processing retry.
            log.exception("Monitor loop error; retrying")
            time.sleep(max(POLL_SECONDS, 5))


if __name__ == "__main__":
    main()
