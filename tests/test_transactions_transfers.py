from datetime import date

import pytest

import functions as fn
from conftest import USER, balance, load, run_task, set_balance

T = fn.TASKS


# ---- 기간 계산 ---------------------------------------------------------------------

THURSDAY = date(2026, 9, 24)


@pytest.mark.parametrize("period, expected", [
    ("today", (THURSDAY, THURSDAY)),
    ("this_week", (date(2026, 9, 21), date(2026, 9, 27))),     # 월~일
    ("last_week", (date(2026, 9, 14), date(2026, 9, 20))),
    ("this_month", (date(2026, 9, 1), date(2026, 9, 30))),
    ("last_month", (date(2026, 8, 1), date(2026, 8, 31))),
    ("all", (None, None)),
    (None, (None, None)),
])
def test_period_range(period, expected):
    assert fn.period_range(period, THURSDAY) == expected


def test_period_edge_cases():
    assert fn.period_range("last_month", date(2026, 1, 15)) == (date(2025, 12, 1), date(2025, 12, 31))
    assert fn.period_range("this_week", date(2026, 9, 28)) == (date(2026, 9, 28), date(2026, 10, 4))
    assert fn.period_range("custom", THURSDAY, "2026-09-05", "2026-09-10") == (date(2026, 9, 5), date(2026, 9, 10))


# ---- 거래 내역 조회 -------------------------------------------------------------------

@pytest.fixture
def query(today):
    today("2026-09-24")
    return lambda **slots: fn.query_transactions(USER, slots)


def ids(result):
    return [t["transaction_id"] for t in result["transactions"]]


def test_all_transactions_recent_first(query):
    r = query()
    assert len(r["transactions"]) == 20
    assert ids(r)[0] == "tx-019" and ids(r)[-1] == "tx-001"


def test_this_month_living_withdrawals(query):
    r = query(account="생활비", period="this_month", tx_type="withdrawal")
    assert len(r["transactions"]) == 9
    assert all(t["account"] == "생활비" and t["sign"] < 0 for t in r["transactions"])


def test_card_payments_over_50k_show_card_name(query):
    r = query(min_amount=50_000, tx_type="card_payment")
    assert sorted(t["amount"] for t in r["transactions"]) == [50_000, 52_000, 55_000, 72_000, 80_000]
    assert all(t["type"] == "카드 결제" and t["card"] in ("생활비 카드", "여행 카드") for t in r["transactions"])


def test_withdrawal_includes_card_payments_and_transfers(query):
    r = query(tx_type="withdrawal", account="생활비")
    assert {"tx-011", "tx-002"} <= set(ids(r))


def test_period_includes_start_and_end(query):
    assert ids(query(period="custom", start_date="2026-09-20", end_date="2026-09-20")) == ["tx-019", "tx-020"]
    assert query(period="this_week")["transactions"] == []


@pytest.mark.parametrize("slots", [
    {"period": "custom", "start_date": "2026-09-10", "end_date": "2026-09-01"},
    {"min_amount": 10, "max_amount": 5},
    {"account": "비상금"},
])
def test_invalid_conditions_fail(query, slots):
    assert query(**slots)["status"] == "fail"


def test_totals(query):
    assert query(account="저축")["deposit_total"] == 280_000


# ---- 조건부 이체 ----------------------------------------------------------------------

def cond(**slots):
    return T["conditional_transfer"].resolve(USER, {"from_account": "생활비", "to_account": "저축", **slots})


def test_conditional_missing_keep_asks():
    r = cond(keep_amount=None)
    assert r["status"] == "ask" and r["missing"] == ["keep_amount"]


def test_conditional_amount_and_boundaries():
    assert cond(keep_amount=400_000)["draft"]["amount"] == 1_031_800
    assert cond(keep_amount=-1)["status"] == "fail"
    r = T["conditional_transfer"].resolve(USER, {"from_account": "여행 자금", "to_account": "저축", "keep_amount": 390_000})
    assert r["status"] == "fail" and "이체할 금액이 없어요" in r["error"]
    r = T["conditional_transfer"].resolve(USER, {"from_account": "여행 자금", "to_account": "저축", "keep_amount": 0})
    assert r["draft"]["amount"] == 390_000


def test_conditional_execute():
    result = run_task("conditional_transfer", {"from_account": "생활비", "to_account": "저축", "keep_amount": 400_000})
    assert result["status"] == "completed"
    assert balance("acc-001") == 400_000 and balance("acc-002") == 2_280_000 + 1_031_800


def test_conditional_amount_change_requires_reapproval():
    request_id = fn.create_request(USER, "conditional_transfer", cond(keep_amount=400_000)["draft"])
    set_balance("acc-001", 1_000_000)

    result = T["conditional_transfer"].execute(USER, request_id)
    assert result["status"] == "changed" and result["draft"]["amount"] == 600_000
    assert balance("acc-001") == 1_000_000                          # 아직 이체 안 됨

    fn.update_request_params(request_id, result["draft"])            # 재승인
    assert T["conditional_transfer"].execute(USER, request_id)["status"] == "completed"
    assert balance("acc-001") == 400_000


def test_conditional_nothing_left_after_change_fails():
    request_id = fn.create_request(USER, "conditional_transfer", cond(keep_amount=400_000)["draft"])
    set_balance("acc-001", 300_000)
    assert T["conditional_transfer"].execute(USER, request_id)["status"] == "failed"
    assert fn.get_request(request_id)["status"] == "failed"


# ---- 여러 계좌 이체 --------------------------------------------------------------------

def multi(*items):
    return {"from_account": "생활비", "transfers": [{"to_account": n, "amount": a} for n, a in items]}


def test_multi_valid():
    r = T["multi_transfer"].resolve(USER, multi(("저축", 200_000), ("여행 자금", 100_000)))
    assert r["draft"]["items"] == [{"to_account_id": "acc-002", "amount": 200_000},
                                   {"to_account_id": "acc-003", "amount": 100_000}]


@pytest.mark.parametrize("items", [
    [("저축", 0), ("여행 자금", 100_000)],          # 0원
    [("저축", 1_000), ("저축", 2_000)],             # 입금 계좌 중복
    [("생활비", 1_000), ("저축", 2_000)],           # 출금 계좌로 입금
    [("저축", 1_000_000), ("여행 자금", 500_000)],  # 총액 > 잔액
    [("비상금", 1_000)],                            # 없는 계좌
])
def test_multi_invalid(items):
    assert T["multi_transfer"].resolve(USER, multi(*items))["status"] == "fail"


def test_multi_missing_list_asks():
    assert T["multi_transfer"].resolve(USER, {"from_account": "생활비", "transfers": None})["status"] == "ask"


def test_multi_execute_all_at_once():
    result = run_task("multi_transfer", multi(("저축", 200_000), ("여행 자금", 100_000)))
    assert result["status"] == "completed"
    assert (balance("acc-001"), balance("acc-002"), balance("acc-003")) == (1_131_800, 2_480_000, 490_000)
    assert len(load()["transactions"]) == 24


def test_multi_save_failure_applies_nothing(fail_save_at):
    draft = T["multi_transfer"].resolve(USER, multi(("저축", 200_000), ("여행 자금", 100_000)))["draft"]
    request_id = fn.create_request(USER, "multi_transfer", draft)
    before = load()
    fail_save_at("1")
    result = T["multi_transfer"].execute(USER, request_id)
    assert result["status"] == "failed" and "모든 이체가 반영되지 않았어요" in result["message"]
    assert load() == before


# ---- 계좌 별명 변경 --------------------------------------------------------------------

def rename(account="여행 자금", new=None):
    return T["rename_account"].resolve(USER, {"account": account, "new_nickname": new})


def test_rename_strips_and_validates_length():
    assert rename(new="  휴가비  ")["draft"] == {"account_id": "acc-003", "new_nickname": "휴가비"}
    assert rename(new="가" * 20)["status"] == "ok"
    assert rename(new="가" * 21)["status"] == "fail"
    assert rename(new="   ")["status"] == "fail"


def test_rename_duplicates_and_same_name():
    r = rename(new="저 축")
    assert r["status"] == "fail" and "겹쳐요" in r["error"]       # 내 계좌와 중복(공백 무시)
    assert rename(account="저축", new="생활비2")["status"] == "ok"
    assert rename(new="여행 자금")["status"] == "fail"              # 현재와 같음


def test_rename_asks_missing_values():
    assert rename(new=None)["status"] == "ask"
    assert T["rename_account"].resolve(USER, {"account": None})["candidates"]["slot"] == "account_id"


def test_rename_keeps_past_transfer_names():
    run_task("transfer", {"from_account": "생활비", "to_account": "여행 자금", "amount": 1_000})
    assert run_task("rename_account", {"account": "여행 자금", "new_nickname": "휴가비"})["status"] == "completed"

    assert next(a for a in load()["accounts"] if a["account_id"] == "acc-003")["nickname"] == "휴가비"
    assert load()["transactions"][-2]["merchant"] == "여행 자금"
    assert T["transfer"].resolve(USER, {"from_account": "생활비", "to_account": "휴가비", "amount": 1_000})["status"] == "ok"
