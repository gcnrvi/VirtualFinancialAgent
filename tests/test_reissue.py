import data_store as ds
import functions as fn
from conftest import USER, card_status, load, run_task, set_card_status

T = fn.TASKS


def application(application_id):
    return next(a for a in load()["reissue_applications"] if a["application_id"] == application_id)


def set_application_status(application_id, status):
    data = load()
    next(a for a in data["reissue_applications"] if a["application_id"] == application_id)["status"] = status
    ds.save_data(data)


# ---- 재발급 신청 -----------------------------------------------------------------

def test_reissue_requires_lost_card():
    r = T["reissue_card"].resolve(USER, {"card": "생활비 카드", "address": "집"})
    assert r["status"] == "fail" and "분실 정지 상태가 아니" in r["error"]


def test_missing_address_asks_home_or_work():
    set_card_status("card-001", "lost")
    r = T["reissue_card"].resolve(USER, {"card": "생활비 카드", "address": None})
    assert r["status"] == "ask"
    assert [o["id"] for o in r["candidates"]["options"]] == ["addr-home", "addr-work"]

    r = T["reissue_card"].resolve(USER, {"card": "생활비 카드", "address_id": "addr-work"})
    assert r["draft"] == {"card_id": "card-001", "address_id": "addr-work"}


def test_reissue_creates_received_application_and_keeps_card_lost():
    set_card_status("card-001", "lost")
    result = run_task("reissue_card", {"card": "생활비 카드", "address": "집"})

    assert result["status"] == "completed"
    app = application("rei-001")
    assert app["status"] == "received" and app["request_id"] == result["request_id"]
    assert card_status("card-001") == "lost"


def test_existing_open_application_is_reported():
    set_card_status("card-001", "lost")
    run_task("reissue_card", {"card": "생활비 카드", "address": "집"})
    r = T["reissue_card"].resolve(USER, {"card": "생활비 카드", "address": "회사"})
    assert r["status"] == "fail" and "rei-001" in r["error"]


# ---- 조회·변경·취소 ----------------------------------------------------------------

def test_query_modify_cancel_and_reapply():
    set_card_status("card-001", "lost")
    run_task("reissue_card", {"card": "생활비 카드", "address": "집"})

    r = fn.query_reissue(USER, {})
    assert r["status"] == "ok" and r["application"]["status"] == "접수"
    assert fn.query_reissue(USER, {"card": "여행 카드"})["status"] == "fail"       # 기록 없음

    assert T["modify_reissue"].resolve(USER, {"address": "집"})["status"] == "fail"  # 같은 배송지
    assert T["modify_reissue"].resolve(USER, {})["status"] == "ask"                  # 새 배송지 질문
    assert run_task("modify_reissue", {"address": "회사"})["status"] == "completed"
    assert application("rei-001")["address_id"] == "addr-work"

    assert run_task("cancel_reissue", {"card": "생활비"})["status"] == "completed"
    assert application("rei-001")["status"] == "cancelled"
    assert card_status("card-001") == "lost"
    assert T["cancel_reissue"].resolve(USER, {})["status"] == "fail"                 # 취소된 신청만 남음

    assert run_task("reissue_card", {"card": "생활비 카드", "address": "회사"})["status"] == "completed"
    assert application("rei-002")["status"] == "received"


# ---- 테스트 시드: 제작 중·배송 중 ------------------------------------------------------

def test_seed_query_lists_recent_first():
    ds.reset_data(with_seed=True)
    r = fn.query_reissue(USER, {})
    assert r["status"] == "ask"
    assert [o["id"] for o in r["candidates"]["options"]] == ["rei-001", "rei-003", "rei-002"]
    assert fn.query_reissue(USER, {"application_id": "rei-003"})["application"]["status"] == "배송 중"
    assert len(fn.query_reissue(USER, {"card": "교통 카드"})["candidates"]["options"]) == 2   # 취소 포함


def test_seed_in_production_and_shipping_cannot_change():
    ds.reset_data(with_seed=True)
    r = T["modify_reissue"].resolve(USER, {"card": "여행 카드", "address": "회사"})
    assert r["status"] == "fail" and "제작" in r["error"]
    r = T["cancel_reissue"].resolve(USER, {"card": "교통 카드"})        # 취소된 rei-002는 후보 제외
    assert r["status"] == "fail" and "배송 중" in r["error"]
    r = T["reissue_card"].resolve(USER, {"card": "여행 카드", "address": "집"})
    assert r["status"] == "fail" and "rei-001" in r["error"]


def test_revalidation_when_production_starts_before_execute():
    set_card_status("card-001", "lost")
    run_task("reissue_card", {"card": "생활비 카드", "address": "집"})
    draft = T["cancel_reissue"].resolve(USER, {})["draft"]
    request_id = fn.create_request(USER, "cancel_reissue", draft)
    set_application_status("rei-001", "in_production")

    assert T["cancel_reissue"].execute(USER, request_id)["status"] == "failed"
    assert application("rei-001")["status"] == "in_production"


def test_save_failure_creates_no_application(fail_save_at):
    set_card_status("card-001", "lost")
    draft = T["reissue_card"].resolve(USER, {"card": "생활비 카드", "address": "집"})["draft"]
    request_id = fn.create_request(USER, "reissue_card", draft)
    fail_save_at("1")
    assert T["reissue_card"].execute(USER, request_id)["status"] == "failed"
    assert load()["reissue_applications"] == []
    assert fn.get_request(request_id)["status"] == "pending_approval"


def test_composite_helpers():
    assert fn.COMPOSITES["lost_and_reissue"] == ["report_lost", "reissue_card"]
    request_id = fn.create_request(USER, "reissue_card", {}, "req-001")
    assert fn.get_request(request_id)["parent_request_id"] == "req-001"
