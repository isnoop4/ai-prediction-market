# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *
import json


class AIPredictionMarket(gl.Contract):
    """
    Prediction market resolved by AI using web evidence.
    - Virtual balances (points), no real token transfers.
    - Anyone can create a market, bet YES/NO, then resolve it.
    - GenLayer validators fetch the evidence page, an LLM decides YES/NO/INVALID,
      and consensus is reached when the outcomes match.
    """

    markets: TreeMap[str, str]       # market_id -> JSON
    positions: TreeMap[str, str]     # "market_id:address" -> JSON
    balances: TreeMap[str, u256]     # address -> points
    faucet_used: TreeMap[str, u256]  # address -> 1 if already claimed
    market_count: u256

    def __init__(self):
        self.market_count = u256(0)

    # ---------- helpers ----------
    def _me(self) -> str:
        return gl.message.sender_address.as_hex

    def _bal(self, addr: str) -> int:
        return int(self.balances.get(addr, u256(0)))

    def _load(self, market_id: str) -> dict:
        if market_id not in self.markets:
            raise Exception("Market not found")
        return json.loads(self.markets[market_id])

    def _pos(self, market_id: str, addr: str) -> dict:
        key = f"{market_id}:{addr}"
        if key in self.positions:
            return json.loads(self.positions[key])
        return {"yes": 0, "no": 0, "claimed": 0}

    # ---------- write ----------
    @gl.public.write
    def claim_faucet(self) -> int:
        me = self._me()
        if int(self.faucet_used.get(me, u256(0))) == 1:
            raise Exception("Faucet already claimed")
        self.faucet_used[me] = u256(1)
        self.balances[me] = u256(self._bal(me) + 1000)
        return self._bal(me)

    @gl.public.write
    def create_market(self, question: str, resolution_url: str, criteria: str) -> str:
        if len(question.strip()) < 10:
            raise Exception("Question is too short")
        if not resolution_url.startswith("http"):
            raise Exception("Evidence URL must be http(s)")
        market_id = str(int(self.market_count))
        self.markets[market_id] = json.dumps({
            "id": market_id,
            "question": question.strip(),
            "url": resolution_url.strip(),
            "criteria": criteria.strip(),
            "creator": self._me(),
            "status": "OPEN",
            "yes_pool": 0,
            "no_pool": 0,
            "outcome": "",
            "confidence": 0,
            "reason": "",
            "evidence": "",
            "evidence_verified": False,
        })
        self.market_count = u256(int(self.market_count) + 1)
        return market_id

    @gl.public.write
    def bet(self, market_id: str, side: str, amount: int) -> None:
        side = side.upper()
        if side not in ("YES", "NO"):
            raise Exception("side must be YES or NO")
        if amount <= 0:
            raise Exception("amount must be > 0")
        m = self._load(market_id)
        if m["status"] != "OPEN":
            raise Exception("Market is closed")
        me = self._me()
        bal = self._bal(me)
        if bal < amount:
            raise Exception("Insufficient balance")
        self.balances[me] = u256(bal - amount)
        p = self._pos(market_id, me)
        k = "yes" if side == "YES" else "no"
        p[k] += amount
        m[k + "_pool"] += amount
        self.positions[f"{market_id}:{me}"] = json.dumps(p)
        self.markets[market_id] = json.dumps(m)

    @gl.public.write
    def resolve_market(self, market_id: str) -> str:
        m = self._load(market_id)
        if m["status"] != "OPEN":
            raise Exception("Market already resolved")

        question = m["question"]
        criteria = m["criteria"] or "Use reasonable judgment based on the evidence."
        url = m["url"]

        def leader_fn():
            page = gl.nondet.web.render(url, mode="text")
            page = page[:8000]
            prompt = f"""You are a neutral judge for a prediction market.
Question: {question}
Resolution criteria: {criteria}

Evidence from {url}:
\"\"\"
{page}
\"\"\"

Decide ONLY based on the evidence above.
- YES: the evidence clearly shows the answer to the question is yes.
- NO: the evidence clearly shows the answer is no.
- INVALID: the evidence is insufficient, irrelevant, or ambiguous.

Also copy ONE short verbatim excerpt (max 200 characters) from the evidence
that supports your decision. Use an empty string if the outcome is INVALID.

Reply in English with JSON only, no other text:
{{"outcome": "YES|NO|INVALID", "confidence": 0-100, "reason": "max 2 sentences", "evidence": "verbatim excerpt"}}"""
            raw = gl.nondet.exec_prompt(prompt)
            if isinstance(raw, dict):
                data = raw
            else:
                txt = str(raw).strip().replace("```json", "").replace("```", "")
                s, e = txt.find("{"), txt.rfind("}")
                try:
                    data = json.loads(txt[s:e + 1])
                except Exception:
                    data = {}
            outcome = str(data.get("outcome", "INVALID")).upper()
            if outcome not in ("YES", "NO", "INVALID"):
                outcome = "INVALID"
            try:
                conf = max(0, min(100, int(data.get("confidence", 0))))
            except Exception:
                conf = 0
            evidence = str(data.get("evidence", ""))[:200].strip()
            norm = lambda s: " ".join(s.split()).lower()
            verified = bool(evidence) and norm(evidence) in norm(page)
            return {
                "outcome": outcome,
                "confidence": conf,
                "reason": str(data.get("reason", ""))[:300],
                "evidence": evidence,
                "evidence_verified": verified,
            }

        def validator_fn(leaders_res) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return False
            mine = leader_fn()
            return mine["outcome"] == leaders_res.calldata["outcome"]

        result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

        m["status"] = "RESOLVED"
        m["outcome"] = result["outcome"]
        m["confidence"] = result["confidence"]
        m["reason"] = result["reason"]
        m["evidence"] = result["evidence"]
        m["evidence_verified"] = result["evidence_verified"]
        self.markets[market_id] = json.dumps(m)
        return result["outcome"]

    @gl.public.write
    def claim(self, market_id: str) -> int:
        m = self._load(market_id)
        if m["status"] != "RESOLVED":
            raise Exception("Market not resolved yet")
        me = self._me()
        p = self._pos(market_id, me)
        if p["claimed"]:
            raise Exception("Already claimed")

        total = m["yes_pool"] + m["no_pool"]
        outcome = m["outcome"]
        payout = 0
        if outcome == "INVALID":
            payout = p["yes"] + p["no"]  # full refund
        else:
            win_pool = m["yes_pool"] if outcome == "YES" else m["no_pool"]
            stake = p["yes"] if outcome == "YES" else p["no"]
            if win_pool == 0:
                payout = p["yes"] + p["no"]  # no winners -> refund
            elif stake > 0:
                payout = stake * total // win_pool
        if payout <= 0:
            raise Exception("Nothing to claim")

        p["claimed"] = 1
        self.positions[f"{market_id}:{me}"] = json.dumps(p)
        self.balances[me] = u256(self._bal(me) + payout)
        return payout

    # ---------- view ----------
    @gl.public.view
    def get_balance(self, address: str) -> int:
        return self._bal(address)

    @gl.public.view
    def get_market(self, market_id: str) -> str:
        return self.markets[market_id]

    @gl.public.view
    def get_markets(self) -> str:
        out = []
        for i in range(int(self.market_count)):
            out.append(json.loads(self.markets[str(i)]))
        return json.dumps(out)

    @gl.public.view
    def get_position(self, market_id: str, address: str) -> str:
        return json.dumps(self._pos(market_id, address))
