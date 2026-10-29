"""Fund / sweep the per-probe payer pool (needed for T15 devnet/localnet and the T16 live gate).

Payers need a rent-exempt balance to exist. That money is NOT spend: `sweep` returns it, and only
fees count against the ledger cap.
"""
from __future__ import annotations

import base64

from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import Message
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction

from .rpc import Rpc

RENT_EXEMPT_ZERO_DATA = 890_880  # lamports, 0-byte system account
SWEEP_FEE = 5_000


def _send(rpc: Rpc, tx: Transaction) -> str:
    return rpc.call("sendTransaction", [base64.b64encode(bytes(tx)).decode(),
                                        {"encoding": "base64", "skipPreflight": False,
                                         "preflightCommitment": "confirmed"}])


def _blockhash(rpc: Rpc) -> Hash:
    return Hash.from_string(rpc.call("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]["blockhash"])


def fund_payers(rpc: Rpc, master: Keypair, payers: list[Keypair], each: int = RENT_EXEMPT_ZERO_DATA + 50_000,
                chunk: int = 10) -> list[str]:
    """Top up every payer to `each` lamports from `master` (idempotent: skips funded ones)."""
    sigs = []
    todo = []
    for p in payers:
        bal = rpc.call("getBalance", [str(p.pubkey())])["value"]
        if bal < each:
            todo.append((p, each - bal))
    for i in range(0, len(todo), chunk):
        ixs = [transfer(TransferParams(from_pubkey=master.pubkey(), to_pubkey=p.pubkey(), lamports=n))
               for p, n in todo[i:i + chunk]]
        bh = _blockhash(rpc)
        tx = Transaction([master], Message.new_with_blockhash(ixs, master.pubkey(), bh), bh)
        sigs.append(_send(rpc, tx))
    return sigs


def sweep(rpc: Rpc, master: Keypair, payers: list[Keypair], attempts: int = 4) -> int:
    """Return whatever is left in each payer to master. Returns lamports recovered. One payer failing
    (transient RPC error) never stops the others, and failures are retried: a deposit is real money."""
    import time
    got = 0
    pending = list(payers)
    for attempt in range(attempts):
        failed = []
        for p in pending:
            try:
                bal = rpc.call("getBalance", [str(p.pubkey())])["value"]
                if bal <= SWEEP_FEE:
                    continue
                amt = bal - SWEEP_FEE
                bh = _blockhash(rpc)
                msg = Message.new_with_blockhash(
                    [transfer(TransferParams(from_pubkey=p.pubkey(), to_pubkey=master.pubkey(), lamports=amt))],
                    p.pubkey(), bh)
                _send(rpc, Transaction([p], msg, bh))
                got += amt
            except Exception:
                failed.append(p)
        pending = failed
        if not pending:
            return got
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{len(pending)} payer(s) not swept after {attempts} attempts")
