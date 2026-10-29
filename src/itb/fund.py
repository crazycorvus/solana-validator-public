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

# Measured 2026-10-29 with getMinimumBalanceForRentExemption(0). My earlier constant (890 880) was wrong for
# this network and made every payer's fee budget ~50 000 instead of a few hundred thousand: the first live
# study stopped landing after ~57 txs per payer. Live code queries the RPC instead of trusting this constant.
RENT_EXEMPT_ZERO_DATA = 650_240
SWEEP_FEE = 5_000


def _send(rpc: Rpc, tx: Transaction) -> str:
    return rpc.call("sendTransaction", [base64.b64encode(bytes(tx)).decode(),
                                        {"encoding": "base64", "skipPreflight": False,
                                         "preflightCommitment": "confirmed"}])


def _blockhash(rpc: Rpc) -> Hash:
    return Hash.from_string(rpc.call("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]["blockhash"])


def rent_min(rpc: Rpc) -> int:
    """Rent-exempt minimum of a 0-byte account on THIS network, from the RPC."""
    return rpc.call("getMinimumBalanceForRentExemption", [0])


def fund_payers(rpc: Rpc, master: Keypair, payers: list[Keypair], each: int | list[int] = RENT_EXEMPT_ZERO_DATA + 50_000,
                chunk: int = 10) -> list[str]:
    """Top up every payer to its target balance from `master` (idempotent: skips funded ones). A payer's
    target must be rent_min + the fees it will pay: a fee payer can never spend below the rent minimum."""
    targets = each if isinstance(each, list) else [each] * len(payers)
    sigs = []
    todo = []
    for p, tgt in zip(payers, targets):
        bal = rpc.call("getBalance", [str(p.pubkey())])["value"]
        if bal < tgt:
            todo.append((p, tgt - bal))
    for i in range(0, len(todo), chunk):
        ixs = [transfer(TransferParams(from_pubkey=master.pubkey(), to_pubkey=p.pubkey(), lamports=n))
               for p, n in todo[i:i + chunk]]
        bh = _blockhash(rpc)
        tx = Transaction([master], Message.new_with_blockhash(ixs, master.pubkey(), bh), bh)
        sigs.append(_send(rpc, tx))
    return sigs


def sweep(rpc: Rpc, master: Keypair, payers: list[Keypair], attempts: int = 4) -> int:
    """Return everything left in each payer to master. The MASTER pays the fee and the payer only signs as the
    source: a fee payer cannot be drained to zero by its own fee (it must stay rent-exempt), but a plain source
    account can. Returns lamports recovered. One payer failing never stops the others; failures are retried."""
    import time
    got = 0
    pending = list(payers)
    for attempt in range(attempts):
        failed = []
        for p in pending:
            try:
                bal = rpc.call("getBalance", [str(p.pubkey())])["value"]
                if bal == 0:
                    continue
                bh = _blockhash(rpc)
                msg = Message.new_with_blockhash(
                    [transfer(TransferParams(from_pubkey=p.pubkey(), to_pubkey=master.pubkey(), lamports=bal))],
                    master.pubkey(), bh)
                _send(rpc, Transaction([master, p], msg, bh))
                got += bal
            except Exception:
                failed.append(p)
        # a sent sweep is not a landed sweep: confirm every payer really reached zero, retry the rest
        t_end = time.time() + 30
        still = [p for p in payers if p not in failed]
        while still and time.time() < t_end:
            still = [p for p in still if rpc.call("getBalance", [str(p.pubkey())])["value"] > 0]
            if still:
                time.sleep(2)
        pending = failed + still
        if not pending:
            return got
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{len(pending)} payer(s) not swept after {attempts} attempts")
