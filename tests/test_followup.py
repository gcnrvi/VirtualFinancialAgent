import functions as fn
from conftest import USER, run_task


def query(**slots):
    return fn.query_request_status(USER, slots)


def transfer(amount, approve=True, source="생활비", target="저축"):
    return run_task("transfer", {"from_account": source, "to_account": target, "amount": amount}, approve)["request_id"]


# ---- 후속 결과 조회 ------------------------------------------------------------------

def test_no_requests():
    assert query()["status"] == "fail"
    assert query(request_kind="transfer")["error"] == "이체 요청 기록이 없어요."


def test_single_match_answers_directly():
    request_id = transfer(10_000)
    r = query(request_kind="transfer")
    assert r["status"] == "ok"
    assert r["request"]["request_id"] == request_id and r["request"]["status"] == "완료"
    assert r["request"]["summary"] == "출금 계좌 생활비 / 입금 계좌 저축 / 금액 10,000원"


def test_multiple_matches_use_context_or_ask():
    first = transfer(10_000)
    card = run_task("lock_card", {"card": "생활비 카드"}, approve=False)["request_id"]
    last = run_task("multi_transfer", {"from_account": "저축", "transfers": [
        {"to_account": "생활비", "amount": 1_000}, {"to_account": "여행 자금", "amount": 2_000}]})["request_id"]

    r = query(request_kind="transfer")
    assert r["status"] == "ask" and [o["id"] for o in r["candidates"]["options"]] == [last, first]
    assert query(request_kind="transfer", context_request_id=last)["request"]["request_id"] == last
    assert query(request_kind="transfer", context_request_id=card)["status"] == "ask"   # 맥락이 다른 종류

    r = query(request_kind="card")
    assert r["request"]["status"] == "취소" and "생활비 카드" in r["request"]["summary"]
    assert query(context_request_id=card)["request"]["request_id"] == card


def test_request_id():
    transfer(10_000)
    assert query(request="req-001")["request"]["request_id"] == "req-001"
    assert query(request="req-999")["status"] == "fail"


def test_earlier_request_excludes_last():
    a = transfer(1_000)
    run_task("lock_card", {"card": "생활비 카드"})
    c = transfer(1_000, approve=False, source="저축", target="생활비")

    assert query(request_kind="transfer", context_request_id=c, earlier_request=True)["request"]["request_id"] == a
    assert query(context_request_id=c, earlier_request=True)["status"] == "ask"                    # a, b 중 선택
    assert query(request_kind="transfer", context_request_id=a, earlier_request=True)["status"] == "fail"


# ---- 대화 대상 ---------------------------------------------------------------------

def test_targets_from_drafts_and_results():
    assert fn.targets_from({"from_account_id": "acc-001", "to_account_id": "acc-002", "amount": 1}, None) == {
        "accounts": ["acc-001", "acc-002"]}
    assert fn.targets_from({"card_id": "card-001", "to_status": "locked"}, {"status": "completed"}) == {"cards": ["card-001"]}
    assert fn.targets_from(None, fn.query_cards(USER, {})) == {"cards": ["card-001", "card-002"]}
    assert fn.targets_from(None, fn.query_cards(USER, {"card": "여행"})) == {"cards": ["card-002"]}
    multi = {"from_account_id": "acc-002", "items": [{"to_account_id": "acc-001", "amount": 1}, {"to_account_id": "acc-003", "amount": 1}]}
    assert fn.targets_from(multi, None) == {"accounts": ["acc-002", "acc-001", "acc-003"]}


def test_target_options():
    assert [o["id"] for o in fn.target_options(USER, "cards", ["card-002"])] == ["card-002"]
    assert [o["id"] for o in fn.target_options(USER, "cards", [])] == ["card-001", "card-002"]   # 내 카드만


# ---- 재시작 시 대기 요청 정리 ---------------------------------------------------------------

def test_close_orphan_requests_keeps_current():
    keep = fn.create_request(USER, "transfer", {"from_account_id": "acc-001", "to_account_id": "acc-002", "amount": 1})
    orphan = fn.create_request(USER, "transfer", {"from_account_id": "acc-001", "to_account_id": "acc-002", "amount": 2})

    notes = fn.close_orphan_requests(USER, keep)
    assert len(notes) == 1 and orphan in notes[0]
    assert fn.get_request(keep)["status"] == "pending_approval"
    assert fn.get_request(orphan)["status"] == "failed"
