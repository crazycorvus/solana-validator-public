"""In-memory chain: accepts raw txs, seals them into blocks by a chosen ordering rule, and answers the
JSON-RPC calls the collector uses. Lets the whole send->collect->map pipeline (and later the
inference engine) be tested offline with a KNOWN ground-truth rule."""
from __future__ import annotations

import base64

from solders.hash import Hash
from solders.transaction import Transaction

from itb.collect import compute_budget
from itb.rpc import RpcError

from test_collect import _json_tx


class FakeChain:
    path, live = "fake", True

    def __init__(self, leader="LEADER", rule="fee"):
        self.leader, self.rule = leader, rule
        self.pending: list[Transaction] = []
        self.blocks: dict[int, list[dict]] = {}

    # transport side
    async def send(self, raw: bytes) -> dict:
        self.pending.append(Transaction.from_bytes(raw))
        return {"result": str(self.pending[-1].signatures[0])}

    async def close(self) -> None:
        pass

    def seal(self, slot: int) -> None:
        txs = [_json_tx(t) for t in self.pending]
        if self.rule == "fee":     # highest requested price first, FIFO on ties
            txs = sorted(txs, key=lambda t: -(compute_budget(t)[0] or 0))
        elif self.rule == "reverse":
            txs = txs[::-1]
        self.blocks[slot] = txs
        self.pending = []

    # rpc side
    def call(self, method, params=None):
        if method == "getBlock":
            slot = params[0]
            if slot not in self.blocks:
                raise RpcError("skipped", -32007)
            if len(params) > 1 and params[1].get("transactionDetails") == "signatures":
                return {"signatures": [t["transaction"]["signatures"][0] for t in self.blocks[slot]]}
            return {"blockhash": "h", "parentSlot": slot - 1, "blockTime": 0, "blockHeight": slot,
                    "transactions": self.blocks[slot]}
        if method == "getSignatureStatuses":
            where = {t["transaction"]["signatures"][0]: sl for sl, ts in self.blocks.items() for t in ts}
            return {"value": [{"slot": where[x]} if x in where else None for x in params[0]]}
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": str(Hash.default())}}
        if method == "getSlotLeaders":
            fn = getattr(self, "leader_fn", None)
            return [fn(params[0] + i) if fn else self.leader for i in range(params[1])]
        raise RpcError(f"unsupported {method}")
