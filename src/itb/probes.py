"""Probe builder (FR-04): memo-tagged txs with ComputeBudget price/limit and controlled accounts.

Design note: every probe has its OWN fee payer. A shared payer would be write-locked by every tx
and serialise the whole batch, confounding H1 vs H4. Payers are pre-funded (rent-exempt minimum,
recoverable by sweeping) and only fees count as spend. Each probe also write-locks `accounts_key`
via a 0-lamport transfer: unique per probe, or shared across a conflict set.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction

MEMO_PROGRAM = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
BASE_FEE_LAMPORTS = 5_000  # per signature


@dataclass
class ProbeSpec:
    probe_id: str          # probe:<run>:<batch>:<idx>
    run_id: str
    batch_id: str
    batch_type: str
    idx: int
    cu_price: int          # micro-lamports per CU
    cu_limit: int
    accounts_key: str      # label of the writable account set ("u<idx>" unique, "shared")
    send_order: int        # position in send sequence (0-based)
    payer_index: int
    send_offset_ms: int = 0


def fee_lamports(cu_price: int, cu_limit: int, signatures: int = 1) -> int:
    """Worst-case fee: base + ceil(price * limit / 1e6). Priority fee uses the REQUESTED limit."""
    return BASE_FEE_LAMPORTS * signatures + math.ceil(cu_price * cu_limit / 1_000_000)


def probe_account(run_id: str, batch_id: str, key: str) -> Pubkey:
    """Deterministic, keyless writable account for a probe/conflict set (owner = system program)."""
    return Pubkey.find_program_address([b"itb", run_id.encode(), batch_id.encode(), key.encode()],
                                       MEMO_PROGRAM)[0]


def build_tx(spec: ProbeSpec, payer: Keypair, blockhash: Hash) -> Transaction:
    ixs = [
        set_compute_unit_limit(spec.cu_limit),
        set_compute_unit_price(spec.cu_price),
        transfer(TransferParams(from_pubkey=payer.pubkey(),
                                to_pubkey=probe_account(spec.run_id, spec.batch_id, spec.accounts_key),
                                lamports=0)),
        Instruction(MEMO_PROGRAM, spec.probe_id.encode(), [AccountMeta(payer.pubkey(), True, False)]),
    ]
    msg = Message.new_with_blockhash(ixs, payer.pubkey(), blockhash)
    return Transaction([payer], msg, blockhash)


def signature_of(tx: Transaction) -> str:
    return str(tx.signatures[0])


def spec_row(spec: ProbeSpec, signature: str, send_ts_us: int | None = None,
             path: str | None = None) -> dict:
    """Row for probes.csv (section 7 of the spec)."""
    d = asdict(spec)
    d.update(signature=signature, send_ts_us=send_ts_us, path=path,
             lamports_fee=fee_lamports(spec.cu_price, spec.cu_limit))
    return d
