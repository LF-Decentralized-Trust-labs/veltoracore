# failed_tx_extractor.py

import os
import logging
import pandas as pd
from web3 import Web3
from alchemy_client import AlchemyClient

from config import *

# -----------------------------
# Router Addresses
# -----------------------------
V2_ROUTER = Web3.to_checksum_address("0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D")
V3_ROUTER = Web3.to_checksum_address("0xE592427A0AEce92De3Edee1F18E0157C05861564")
UNIVERSAL_ROUTER = Web3.to_checksum_address("0xEf1c6E67703c7BD7107eed8303Fbe6EC2554BF6B")

ROUTERS = {V2_ROUTER, V3_ROUTER, UNIVERSAL_ROUTER}

# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


def extract_failed_transactions(w3, start_block, end_block):

    rows = []

    for block in range(start_block, end_block + 1):

        block_data = w3.eth.get_block(block, full_transactions=True)

        total_router_txs = 0
        failed_router_txs = 0

        for tx in block_data.transactions:

            if tx.to is None:
                continue

            to_addr = Web3.to_checksum_address(tx.to)

            if to_addr not in ROUTERS:
                continue

            total_router_txs += 1

            try:
                receipt = w3.eth.get_transaction_receipt(tx.hash)
            except Exception:
                continue

            if receipt.status == 0:
                failed_router_txs += 1

        rejection_rate = (
            failed_router_txs / total_router_txs
            if total_router_txs > 0
            else 0
        )

        rows.append({
            "block": block,
            "router_txs": total_router_txs,
            "failed_txs": failed_router_txs,
            "rejection_rate": rejection_rate
        })

        if block % 50 == 0:
            logger.info(f"Processed block {block}")

    return pd.DataFrame(rows)


if __name__ == "__main__":

    logger.info("Starting failed tx extraction")

    client = AlchemyClient(os.getenv("ALCHEMY_KEY"))
    w3 = client.w3

    df = extract_failed_transactions(w3, START_BLOCK, END_BLOCK)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    df.to_parquet(f"{OUTPUT_DIR}/failed_transactions.parquet", index=False)
    df.to_csv(f"{OUTPUT_DIR}/failed_transactions.csv", index=False)

    logger.info("Failed transaction extraction completed")
