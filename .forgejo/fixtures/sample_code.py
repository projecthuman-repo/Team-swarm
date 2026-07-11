"""Code fixture for the Headroom CI gate (v4.1 T1.1).

Code is NOT a JSON tool-output: Headroom's 15-20% coding-agent savings
come from tool traffic around it, so the gate asserts code content is
protected (never lossily crushed), not that it shrinks.
"""


def fibonacci(n: int) -> int:
    if n < 2:
        return n
    a, b = 0, 1
    for _ in range(n - 1):
        a, b = b, a + b
    return b


class LedgerEntry:
    def __init__(self, account: str, amount_cents: int) -> None:
        self.account = account
        self.amount_cents = amount_cents

    def formatted(self) -> str:
        sign = "-" if self.amount_cents < 0 else ""
        cents = abs(self.amount_cents)
        return f"{sign}${cents // 100}.{cents % 100:02d} ({self.account})"


MAGIC_SENTINEL = "swarm-gate-fixture-7f3a9c"
