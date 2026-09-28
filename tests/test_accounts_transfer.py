import pytest

import data_store as ds
import functions as fn
from conftest import USER, balance, load, run_task, set_balance


def resolve(**slots):
    return fn.resolve_transfer(USER, slots)


# ---- 공통 도구 -----------------------------------------------------------------

def test_particles():
    assert [fn.with_ro(w) for w in ["저축", "생활비", "여행 자금", "휴가비", "달"]] == [
        "저축으로", "생활비로", "여행 자금으로", "휴가비로", "달로"]
    assert [fn.with_eul(w) for w in ["생활비 카드", "계좌", "잔액"]] == ["생활비 카드를", "계좌를", "잔액을"]
    assert [fn.with_eun(w) for w in ["여행 카드", "신청", "rei-001"]] == ["여행 카드는", "신청은", "rei-001은(는)"]


# ---- 계좌 조회 -----------------------------------------------------------------

def test_query_accounts_only_mine_with_total():
    result = fn.query_accounts(USER)
    assert [a["account_id"] for a in result["accounts"]] == ["acc-001", "acc-002", "acc-003"]
    assert result["total"] == 1_431_800 + 2_280_000 + 390_000


# ---- 즉시이체 검증 (설계 5-2 순서) ------------------------------------------------

def test_missing_slot_asks():
    r = resolve(from_account=None, to_account="저축", amount=100_000)
    assert r["status"] == "ask" and r["missing"] == ["from_account"]


def test_unknown_account_fails():
    assert resolve(from_account="비상금", to_account="저축", amount=100_000)["status"] == "fail"


def test_partial_name_asks_with_candidates_then_selection_ok():
    r = resolve(from_account="여행", to_account="저축", amount=100_000)
    assert r["status"] == "ask"
    assert r["candidates"]["options"][0]["id"] == "acc-003"

    r = resolve(from_account_id="acc-003", to_account="저축", amount=100_000)
    assert r["status"] == "ok" and r["draft"]["from_account_id"] == "acc-003"


@pytest.mark.parametrize("slots, reason", [
    ({"from_account": "생활비", "to_account": "저축", "amount": -10_000}, "1원 이상"),
    ({"from_account": "생활비", "to_account": "생활비 통장", "amount": 10_000}, "같은 계좌"),
    ({"from_account": "여행자금", "to_account": "저축", "amount": 500_000}, "잔액"),
    ({"from_account_id": "acc-004", "to_account": "저축", "amount": 1_000}, "찾을 수 없어요"),
])
def test_validation_failures(slots, reason):
    r = fn.resolve_transfer(USER, slots)
    assert r["status"] == "fail" and reason in r["error"]


def test_valid_transfer_matches_my_account_not_other_users():
    r = resolve(from_account="생활비", to_account="저축", amount=100_000)
    assert r == {"status": "ok", "draft": {"from_account_id": "acc-001", "to_account_id": "acc-002", "amount": 100_000}}
    assert "이체 후 생활비 잔액: 1,331,800원" in fn.describe_transfer(USER, r["draft"])


# ---- 요청 기록·실행 ---------------------------------------------------------------

def test_modify_then_execute_saves_balances_transactions_and_request():
    draft = resolve(from_account="생활비", to_account="저축", amount=100_000)["draft"]
    request_id = fn.create_request(USER, "transfer", draft)
    assert request_id == "req-001" and fn.get_request(request_id)["status"] == "pending_approval"

    fn.update_request_params(request_id, {**draft, "amount": 50_000})
    result = fn.execute_transfer(USER, request_id)

    assert result["status"] == "completed"
    assert balance("acc-001") == 1_381_800 and balance("acc-002") == 2_330_000
    withdrawal, deposit = load()["transactions"][-2:]
    assert (withdrawal["transaction_id"], deposit["transaction_id"]) == ("tx-021", "tx-022")
    assert withdrawal["type"] == "withdrawal" and withdrawal["merchant"] == "저축"
    assert deposit["merchant"] == "생활비"
    request = fn.get_request(request_id)
    assert request["status"] == "completed" and request["result"]["transaction_ids"] == ["tx-021", "tx-022"]

    assert fn.execute_transfer(USER, request_id)["status"] == "skipped"   # 중복 실행 방지


def test_rejected_request_cannot_execute():
    result = run_task("transfer", {"from_account": "생활비", "to_account": "저축", "amount": 1_000}, approve=False)
    assert result["status"] == "cancelled"
    assert fn.get_request(result["request_id"])["status"] == "cancelled"
    assert fn.execute_transfer(USER, result["request_id"])["status"] == "skipped"


def test_revalidation_before_execute_fails_without_change():
    request_id = fn.create_request(USER, "transfer", {"from_account_id": "acc-003", "to_account_id": "acc-002", "amount": 300_000})
    set_balance("acc-003", 100_000)          # 승인 사이 잔액 감소

    result = fn.execute_transfer(USER, request_id)
    assert result["status"] == "failed"
    assert balance("acc-003") == 100_000
    assert fn.get_request(request_id)["status"] == "failed"


def test_save_failure_keeps_file_and_request_pending(fail_save_at):
    draft = resolve(from_account="생활비", to_account="저축", amount=100_000)["draft"]
    request_id = fn.create_request(USER, "transfer", draft)
    before = load()

    fail_save_at("1")
    result = fn.execute_transfer(USER, request_id)

    assert result["status"] == "failed"
    assert load() == before
    assert fn.get_request(request_id)["status"] == "pending_approval"


def test_fail_request_only_changes_pending():
    request_id = fn.create_request(USER, "transfer", {"from_account_id": "acc-001", "to_account_id": "acc-002", "amount": 1})
    fn.fail_request(request_id, "재검증 실패")
    assert fn.get_request(request_id)["status"] == "failed"
    fn.fail_request(request_id, "다시")
    assert fn.get_request(request_id)["result"]["message"] == "재검증 실패"


def test_registry():
    assert {"query_accounts", "transfer"} <= set(fn.TASKS)
    assert fn.TASKS["transfer"].kind == "change"
    assert ds.WORK_PATH.exists()
