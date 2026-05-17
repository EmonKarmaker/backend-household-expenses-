"""Settlement minimization.

Converts a map of {user_id: balance} into the minimum number of cash transfers
needed to bring every user to zero using the greedy largest-match algorithm.

For N users the result contains at most N-1 transfers.
"""
from dataclasses import dataclass
from decimal import Decimal

ZERO      = Decimal("0")
TOLERANCE = Decimal("0.01")


@dataclass
class Transfer:
    from_user_id: int
    to_user_id: int
    amount: Decimal


def minimize_transfers(balances: dict[int, Decimal]) -> list[Transfer]:
    """Greedy minimization: match biggest creditor with biggest debtor, repeat.

    Positive balance  → user is owed money (creditor).
    Negative balance  → user owes money (debtor).

    Raises ValueError if the balances do not sum to zero within TOLERANCE,
    which would indicate a bug in the upstream calculation engine.
    """
    total = sum(balances.values(), ZERO)
    if abs(total) > TOLERANCE:
        raise ValueError(
            f"balances do not sum to zero (got {total}); "
            "this indicates a calculation engine bug"
        )

    working = {uid: bal for uid, bal in balances.items() if bal != ZERO}
    transfers: list[Transfer] = []

    while True:
        creditors = sorted(
            ((uid, bal) for uid, bal in working.items() if bal > ZERO),
            key=lambda x: x[1],
            reverse=True,
        )
        debtors = sorted(
            ((uid, bal) for uid, bal in working.items() if bal < ZERO),
            key=lambda x: x[1],
        )

        if not creditors or not debtors:
            break

        cred_id, cred_bal = creditors[0]
        debt_id, debt_bal = debtors[0]

        amount = min(cred_bal, -debt_bal)
        transfers.append(Transfer(from_user_id=debt_id, to_user_id=cred_id, amount=amount))

        working[cred_id] -= amount
        working[debt_id] += amount

        if working[cred_id] == ZERO:
            del working[cred_id]
        if working[debt_id] == ZERO:
            del working[debt_id]

    return transfers
