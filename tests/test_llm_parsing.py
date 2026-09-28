# 실제 Gemini로 요청 해석을 확인한다. API 키가 필요하고 결과가 매번 조금씩 다를 수 있다.
# 실행: RUN_LLM_TESTS=1 uv run pytest -m llm

import pytest

import graph as g

pytestmark = pytest.mark.llm

CASES = [
    ("내 계좌에 총 얼마 있어?", "query_accounts", {}),
    ("생활비에서 저축으로 10만 원 옮겨줘", "transfer", {"from_account": "생활비", "to_account": "저축", "amount": 100_000}),
    ("생활비에 40만 원 남기고 나머지를 저축해줘", "conditional_transfer", {"from_account": "생활비", "keep_amount": 400_000}),
    ("여행 자금 계좌 이름을 휴가비로 바꿔줘", "rename_account", {"account": "여행 자금", "new_nickname": "휴가비"}),
    ("이번 달 생활비 출금 내역 보여줘", "query_transactions", {"period": "this_month", "tx_type": "withdrawal"}),
    ("5만 원 이상 결제한 내역 찾아줘", "query_transactions", {"min_amount": 50_000, "tx_type": "card_payment"}),
    ("생활비 카드를 집에 둔 것 같아. 잠깐 잠가줘", "lock_card", {}),
    ("생활비 카드를 잃어버렸어. 정지하고 재발급해줘", "lost_and_reissue", {}),
    ("재발급 카드 배송지를 회사로 바꿔줘", "modify_reissue", {"address": "회사"}),
    ("생활비 계좌에서 미납 청구서 전부 납부해줘", "pay_bills_batch", {"all_bills": True}),
    ("아까 이체가 됐어?", "query_request_status", {"request_kind": "transfer"}),
    ("오늘 날씨 어때?", "unknown", {}),
]


@pytest.mark.parametrize("text, intent, expected", CASES)
def test_parse_request(text, intent, expected):
    prompt = f"{g._user_context('user-001')}\n\n사용자 요청: {text}"
    parsed = g.get_llm().with_structured_output(g.ParsedRequest).invoke(prompt)
    assert parsed.intent == intent
    for key, value in expected.items():
        assert getattr(parsed, key) == value, key


def test_reference_is_marked_without_guessing_name():
    parsed = g.get_llm().with_structured_output(g.ParsedRequest).invoke(f"{g._user_context('user-001')}\n\n사용자 요청: 그 카드 잠가줘")
    assert parsed.intent == "lock_card"
    assert parsed.referenced_slot == "card" and parsed.card is None
