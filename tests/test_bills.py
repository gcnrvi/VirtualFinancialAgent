import pytest

import functions as fn
from conftest import USER, balance, load, run_task, set_balance

T = fn.TASKS
ALL_BILLS = 270_000   # 가스 25,000 / 통신 55,000 / 전기 42,000 / 수도 18,000 / 관리비 130,000


@pytest.fixture(autouse=True)
def fixed_today(today):
    today("2026-09-28")


def bill(bill_id):
    return next(b for b in load()["bills"] if b["bill_id"] == bill_id)


def statuses():
    return {b["name"]: b["status"] for b in load()["bills"]}


# ---- 조회 ------------------------------------------------------------------------

def test_query_unpaid_by_due_with_overdue_note():
    r = fn.query_bills(USER)
    assert [b["name"] for b in r["bills"]] == ["가스요금", "통신비", "전기요금", "수도요금", "관리비"]
    assert r["total"] == ALL_BILLS
    assert r["bills"][0]["due"] == "2026-09-18, 기한 지남·연체료 없음"
    assert r["bills"][4]["due"] == "2026-09-30"


# ---- 한 건 납부 ---------------------------------------------------------------------

def test_pay_bill_asks_account_with_balances():
    r = T["pay_bill"].resolve(USER, {"from_account": None, "bill": "전기요금"})
    assert r["status"] == "ask" and r["missing"] == ["from_account"]
    assert "생활비 (잔액 1,431,800원)" in r["question"]


def test_pay_bill_bill_selection():
    r = T["pay_bill"].resolve(USER, {"from_account": "생활비", "bill": None})
    assert r["status"] == "ask" and len(r["candidates"]["options"]) == 5

    r = T["pay_bill"].resolve(USER, {"from_account": "생활비", "bill": "요금"})
    assert [o["id"] for o in r["candidates"]["options"]] == ["bill-005", "bill-002", "bill-003"]

    assert T["pay_bill"].resolve(USER, {"from_account": "생활비", "bill": "전기"})["draft"]["bill_id"] == "bill-002"
    assert T["pay_bill"].resolve(USER, {"from_account": "생활비", "bill": "보험료"})["status"] == "fail"


def test_pay_bill_insufficient_balance():
    set_balance("acc-003", 10_000)
    r = T["pay_bill"].resolve(USER, {"from_account": "여행 자금", "bill": "전기요금"})
    assert r["status"] == "fail" and "잔액" in r["error"]


def test_pay_bill_saves_balance_transaction_and_bill_together():
    result = run_task("pay_bill", {"from_account": "생활비", "bill": "전기요금"})
    assert result["status"] == "completed"

    paid, tx = bill("bill-002"), load()["transactions"][-1]
    assert balance("acc-001") == 1_431_800 - 42_000
    assert paid["status"] == "paid" and paid["paid_account_id"] == "acc-001"
    assert paid["transaction_id"] == tx["transaction_id"]
    assert (tx["type"], tx["merchant"], tx["card_id"]) == ("withdrawal", "전기요금", None)

    r = T["pay_bill"].resolve(USER, {"from_account": "생활비", "bill": "전기요금"})
    assert r["status"] == "fail" and "이미 납부" in r["error"]
    assert len(fn.query_bills(USER)["bills"]) == 4


# ---- 일괄 납부 ---------------------------------------------------------------------

def test_batch_asks_which_bills():
    r = T["pay_bills_batch"].resolve(USER, {"from_account": "생활비"})
    assert r["status"] == "ask" and "전부" in r["question"]


def test_batch_all_in_due_order():
    result = run_task("pay_bills_batch", {"from_account": "생활비", "all_bills": True})
    assert result["status"] == "completed"
    assert set(statuses().values()) == {"paid"}
    assert balance("acc-001") == 1_431_800 - ALL_BILLS
    assert [t["merchant"] for t in load()["transactions"][-5:]] == ["가스요금", "통신비", "전기요금", "수도요금", "관리비"]
    assert fn.get_request(result["request_id"])["status"] == "completed"
    assert T["pay_bills_batch"].resolve(USER, {"from_account": "생활비", "all_bills": True})["status"] == "fail"


def test_batch_insufficient_items_stay_unpaid_and_continue():
    set_balance("acc-003", 100_000)
    draft = T["pay_bills_batch"].resolve(USER, {"from_account": "여행 자금", "all_bills": True})["draft"]
    assert "잔액이 총액보다 적어요" in T["pay_bills_batch"].describe(USER, draft)

    result = run_task("pay_bills_batch", {"from_account": "여행 자금", "all_bills": True})
    assert statuses() == {"통신비": "paid", "전기요금": "unpaid", "수도요금": "paid", "관리비": "unpaid", "가스요금": "paid"}
    assert balance("acc-003") == 2_000
    assert fn.get_request(result["request_id"])["status"] == "partial"
    assert "실패(미납 유지) 2건" in result["message"]


def test_batch_selected_excludes_already_paid():
    run_task("pay_bill", {"from_account": "생활비", "bill": "수도요금"})
    r = T["pay_bills_batch"].resolve(USER, {"from_account": "생활비", "bills": ["전기요금", "수도요금"]})
    assert r["draft"]["bill_ids"] == ["bill-002"]


def test_batch_save_failure_stops_and_keeps_saved(fail_save_at):
    draft = T["pay_bills_batch"].resolve(USER, {"from_account": "생활비", "all_bills": True})["draft"]
    request_id = fn.create_request(USER, "pay_bills_batch", draft)
    fail_save_at("2")                                    # 1: 가스 저장 OK, 2: 통신비 실패
    T["pay_bills_batch"].execute(USER, request_id)

    assert [name for name, s in statuses().items() if s == "paid"] == ["가스요금"]
    assert balance("acc-001") == 1_431_800 - 25_000
    items = {i["name"]: i["status"] for i in fn.get_request(request_id)["result"]["items"]}
    assert items == {"가스요금": "completed", "통신비": "failed", "전기요금": "unprocessed",
                     "수도요금": "unprocessed", "관리비": "unprocessed"}
    assert fn.get_request(request_id)["status"] == "partial"


def test_batch_resume_after_final_record_failure_pays_only_rest(fail_save_at):
    draft = T["pay_bills_batch"].resolve(USER, {"from_account": "생활비", "all_bills": True})["draft"]
    request_id = fn.create_request(USER, "pay_bills_batch", draft)
    fail_save_at("2,3")                                  # 통신비 저장 실패 + 최종 기록 저장 실패
    T["pay_bills_batch"].execute(USER, request_id)

    request = fn.get_request(request_id)
    assert request["status"] == "pending_approval"
    assert [i["name"] for i in request["result"]["items"]] == ["가스요금"]

    fail_save_at("")                                     # 재시작 후: 재검증하면 남은 4건만
    rest = T["pay_bills_batch"].resolve(USER, {"from_account": "생활비", "all_bills": True})["draft"]
    assert rest["bill_ids"] == ["bill-001", "bill-002", "bill-003", "bill-004"]
    fn.update_request_params(request_id, rest)
    T["pay_bills_batch"].execute(USER, request_id)

    request = fn.get_request(request_id)
    assert request["status"] == "completed" and len(request["result"]["items"]) == 5
    assert balance("acc-001") == 1_431_800 - ALL_BILLS
    assert len([t for t in load()["transactions"] if t["merchant"] == "가스요금"]) == 1
