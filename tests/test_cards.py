import json

import pytest

import data_store as ds
import functions as fn
from conftest import USER, card_status, load, run_task, set_card_status

T = fn.TASKS


def card_run(action, card):
    return run_task(action, {"card": card})


# ---- 조회·대상 확인 -------------------------------------------------------------

def test_query_cards_only_mine_and_by_name_or_id():
    result = fn.query_cards(USER, {})
    assert [c["card_id"] for c in result["cards"]] == ["card-001", "card-002"]
    assert result["cards"][0] == {"card_id": "card-001", "name": "생활비 카드", "card_type": "체크카드", "status": "사용 가능"}
    assert [c["card_id"] for c in fn.query_cards(USER, {"card": "여행카드"})["cards"]] == ["card-002"]
    assert [c["card_id"] for c in fn.query_cards(USER, {"card_id": "card-002"})["cards"]] == ["card-002"]
    assert fn.query_cards(USER, {"card": "회사 카드"})["status"] == "fail"


def test_card_matching():
    assert T["lock_card"].resolve(USER, {"card": "생활비"})["draft"]["card_id"] == "card-001"   # 다른 사용자 생활비 카드 제외
    assert T["lock_card"].resolve(USER, {"card": "card-002"})["draft"]["card_id"] == "card-002"
    assert T["lock_card"].resolve(USER, {"card": "card-003"})["status"] == "fail"             # 다른 사용자 카드 ID


def test_missing_card_asks_with_my_cards():
    r = T["lock_card"].resolve(USER, {"card": None})
    assert r["status"] == "ask"
    assert [o["id"] for o in r["candidates"]["options"]] == ["card-001", "card-002"]
    assert "1) 생활비 카드" in r["question"]


def test_partial_card_name_asks():
    r = T["lock_card"].resolve(USER, {"card": "여"})
    assert r["status"] == "ask" and r["candidates"]["options"][0]["id"] == "card-002"


# ---- 상태 전이 (설계 6-3) ----------------------------------------------------------

def test_lock_unlock_cycle():
    assert card_run("unlock_card", "생활비 카드")["status"] == "fail"       # active는 해제 불가
    assert card_run("lock_card", "생활비 카드")["status"] == "completed"
    assert card_status("card-001") == "locked"
    assert card_run("lock_card", "생활비 카드")["status"] == "fail"         # 이미 잠김
    assert card_run("unlock_card", "생활비 카드")["status"] == "completed"
    assert card_status("card-001") == "active"


def test_lost_from_active_and_locked():
    card_run("lock_card", "생활비 카드")
    assert card_run("report_lost", "생활비 카드")["status"] == "completed"
    assert card_status("card-001") == "lost"
    assert card_run("report_lost", "여행 카드")["status"] == "completed"
    assert card_status("card-002") == "lost"


@pytest.mark.parametrize("action, reason", [
    ("report_lost", "이미 분실 정지"),
    ("lock_card", "잠글 수 없어요"),
    ("unlock_card", "재발급을 신청"),
])
def test_lost_card_rejects_every_action(action, reason):
    set_card_status("card-001", "lost")
    r = T[action].resolve(USER, {"card": "생활비 카드"})
    assert r["status"] == "fail" and reason in r["error"]


def test_already_flag_only_when_already_in_target_state():
    set_card_status("card-001", "lost")
    assert T["report_lost"].resolve(USER, {"card": "생활비 카드"})["already"] is True
    assert T["lock_card"].resolve(USER, {"card": "생활비 카드"})["already"] is False


def test_describe_lost_warns_irreversible():
    text = T["report_lost"].describe(USER, {"card_id": "card-001", "to_status": "lost"})
    assert "사용 가능 → 분실 정지" in text and "재발급" in text


def test_card_change_keeps_balances_and_transactions():
    card_run("lock_card", "생활비 카드")
    card_run("report_lost", "여행 카드")
    initial = json.loads(ds.INITIAL_PATH.read_text(encoding="utf-8"))
    assert load()["accounts"] == initial["accounts"]
    assert load()["transactions"] == initial["transactions"]


def test_revalidation_and_duplicate_guard():
    draft = T["lock_card"].resolve(USER, {"card": "생활비 카드"})["draft"]
    request_id = fn.create_request(USER, "lock_card", draft)
    set_card_status("card-001", "lost")                     # 승인 사이 분실 처리됨

    assert T["lock_card"].execute(USER, request_id)["status"] == "failed"
    assert card_status("card-001") == "lost"
    assert fn.get_request(request_id)["status"] == "failed"
    assert T["lock_card"].execute(USER, request_id)["status"] == "skipped"


def test_save_failure_keeps_status(fail_save_at):
    draft = T["report_lost"].resolve(USER, {"card": "생활비 카드"})["draft"]
    request_id = fn.create_request(USER, "report_lost", draft)
    fail_save_at("1")
    assert T["report_lost"].execute(USER, request_id)["status"] == "failed"
    assert card_status("card-001") == "active"
    assert fn.get_request(request_id)["status"] == "pending_approval"
