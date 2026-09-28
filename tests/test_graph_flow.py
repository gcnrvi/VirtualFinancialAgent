# 가짜 LLM으로 그래프 흐름을 확인한다. LLM이 어떻게 해석했는지를 고정하고,
# 그 뒤의 분기(추가 질문·승인·수정·거절·복합 업무·재승인·대화 대상)가 설계대로 이어지는지 본다.

import pytest
from langchain_core.messages import HumanMessage
from langgraph.types import Command

import functions as fn
import graph as g
from conftest import USER, balance, card_status, load, set_balance, set_card_status

P, A, S = g.ParsedRequest, g.ApprovalReply, g.SlotAnswer
APPROVE, REJECT = A(decision="approve"), A(decision="reject")


class Chat:
    """한 대화 세션. say()는 LLM 응답을 준비한 뒤 입력을 보내고, 에이전트의 출력 문장을 돌려준다."""

    def __init__(self, fake_llm, thread="t"):
        self.llm = fake_llm
        self.saver, self.conn = g.open_checkpointer()
        self.app = g.build_graph(self.saver)
        self.config = {"configurable": {"thread_id": thread}}

    def say(self, text, *replies):
        self.llm.add(*replies)
        state = self.app.get_state(self.config)
        if any(t.interrupts for t in state.tasks):
            out = self.app.invoke(Command(resume=text), self.config)
        else:
            out = self.app.invoke({"messages": [HumanMessage(text)], "user_id": USER}, self.config)
        if "__interrupt__" in out:
            value = out["__interrupt__"][0].value
            self.waiting = value["type"]
            return value.get("message") or value.get("question")
        self.waiting = None
        return out["messages"][-1].content

    def state(self):
        return self.app.get_state(self.config).values

    def close(self):
        self.conn.close()


@pytest.fixture
def chat(fake_llm):
    c = Chat(fake_llm)
    yield c
    c.close()


def transfer(amount=100_000, **kw):
    return P(intent="transfer", reason="", from_account=kw.get("src", "생활비"), to_account=kw.get("dst", "저축"), amount=amount)


# ---- 기본 흐름 ---------------------------------------------------------------------

def test_query_goes_straight_to_answer(chat):
    out = chat.say("내 계좌 잔액", P(intent="query_accounts", reason=""))
    assert "총 잔액: 4,101,800원" in out and chat.waiting is None


def test_unknown_and_llm_failure(chat):
    assert "도와드릴 수 있어요" in chat.say("날씨 어때?", P(intent="unknown", reason=""))
    assert "해석하지 못했어요" in chat.say("아무거나", RuntimeError("LLM 장애"))
    assert load()["requests"] == []


def test_transfer_modify_then_approve(chat):
    assert "금액: 100,000원" in chat.say("생활비에서 저축으로 10만 원", transfer())
    out = chat.say("아니, 5만 원만", A(decision="modify", amount=50_000))
    assert chat.waiting == "approval" and "금액: 50,000원" in out
    assert "이체했어요" in chat.say("응 진행해", APPROVE)
    assert balance("acc-001") == 1_381_800
    request = load()["requests"][0]
    assert request["params"]["amount"] == 50_000 and request["status"] == "completed"   # 같은 요청을 갱신


def test_missing_slot_question_then_reject(chat):
    out = chat.say("저축으로 10만 원", transfer(src=None))
    assert chat.waiting == "question" and "출금 계좌" in out
    chat.say("생활비에서", S(cancel=False, from_account="생활비"))
    assert chat.waiting == "approval"
    assert "취소했어요" in chat.say("취소할게", REJECT)
    assert load()["requests"][0]["status"] == "cancelled"


def test_cancel_during_question(chat):
    chat.say("이체해줘", P(intent="transfer", reason=""))
    out = chat.say("그냥 됐어", S(cancel=True))
    assert "취소했어요" in out and chat.waiting is None


def test_candidate_selection(chat):
    chat.say("여행에서 저축으로 1만원", transfer(10_000, src="여행"))
    assert chat.waiting == "question"
    out = chat.say("응 그 계좌", S(cancel=False, selected_id="acc-003"))
    assert "출금: 여행 자금 (acc-003)" in out


def test_validation_failure_ends_without_request(chat):
    out = chat.say("-1만 원 옮겨줘", transfer(-10_000))
    assert "1원 이상" in out and load()["requests"] == []


def test_invalid_modify_keeps_plan_and_unclear_reasks(chat):
    chat.say("저축에서 여행 자금으로 3만원", transfer(30_000, src="저축", dst="여행 자금"))
    out = chat.say("1억으로", A(decision="modify", amount=100_000_000))
    assert "기존 처리안을 유지할게요" in out and "금액: 30,000원" in out
    out = chat.say("음...", A(decision="unclear"))
    assert "답변을 이해하지 못했어요" in out and chat.waiting == "approval"
    out = chat.say("아무거나", RuntimeError("LLM 장애"))           # 승인 분류 실패 → unclear
    assert chat.waiting == "approval"
    chat.say("거절", REJECT)
    assert load()["requests"][0]["status"] == "cancelled"


# ---- 복합 업무: 정지 후 재발급 ----------------------------------------------------------

def lost_and_reissue(address=None):
    return P(intent="lost_and_reissue", reason="", card="생활비 카드", address=address)


def test_lost_and_reissue_each_approved_and_linked(chat):
    chat.say("잃어버렸어 정지하고 재발급", lost_and_reissue())
    out = chat.say("승인", APPROVE)
    assert "[카드 분실 정지] 생활비 카드를 분실 정지" in out and "배송지를 선택" in out   # 앞 단계 결과를 먼저 안내
    out = chat.say("회사로", S(cancel=False, address="회사"))
    assert "[카드 재발급 신청]" in out and "회사" in out
    out = chat.say("승인", APPROVE)
    assert "재발급을 신청했어요" in out

    lost, reissue = load()["requests"]
    assert reissue["parent_request_id"] == lost["request_id"]
    assert card_status("card-001") == "lost" and load()["reissue_applications"][0]["address_id"] == "addr-work"


def test_reject_first_step_skips_reissue(chat):
    chat.say("정지하고 재발급", lost_and_reissue("집"))
    out = chat.say("거절", REJECT)
    assert "진행하지 않았어요" in out
    assert card_status("card-001") == "active" and len(load()["requests"]) == 1


def test_cancel_second_step_keeps_lost(chat):
    chat.say("정지하고 재발급", lost_and_reissue())
    chat.say("승인", APPROVE)
    out = chat.say("재발급은 나중에", S(cancel=True))
    assert "앞 단계에서 처리된 내용은 그대로 유지" in out
    assert card_status("card-001") == "lost" and load()["reissue_applications"] == []


def test_already_lost_skips_first_step(chat):
    set_card_status("card-001", "lost")
    out = chat.say("정지하고 재발급", lost_and_reissue("집"))
    assert chat.waiting == "approval"
    assert "이미 분실 정지된 카드예요. 이 단계는 건너뛸게요" in out     # 건너뛴 사실을 재발급 승인 전에 안내
    assert "[카드 재발급 신청]" in out
    assert "재발급을 신청했어요" in chat.say("승인", APPROVE)
    assert [r["type"] for r in load()["requests"]] == ["reissue_card"]


# ---- 조건부 이체 금액 변동 재승인 ---------------------------------------------------------

def test_conditional_transfer_reapproval_when_amount_changes(chat):
    chat.say("40만 원 남기고 저축", P(intent="conditional_transfer", reason="", from_account="생활비", to_account="저축", keep_amount=400_000))
    set_balance("acc-001", 1_000_000)
    out = chat.say("승인", APPROVE)
    assert chat.waiting == "approval" and "600,000원으로 달라졌어요" in out
    assert balance("acc-001") == 1_000_000
    chat.say("승인", APPROVE)
    assert balance("acc-001") == 400_000
    assert load()["requests"][0]["params"]["amount"] == 600_000


# ---- 대화 대상·후속 결과 ------------------------------------------------------------------

def test_reference_single_target_used_directly(chat):
    chat.say("생활비 카드 상태", P(intent="query_cards", reason="", card="생활비 카드"))
    out = chat.say("그 카드 잠가줘", P(intent="lock_card", reason="", referenced_slot="card"))
    assert chat.waiting == "approval" and "생활비 카드 (card-001" in out


def test_reference_multiple_targets_asks(chat):
    chat.say("카드 목록", P(intent="query_cards", reason=""))
    out = chat.say("그 카드 잠가줘", P(intent="lock_card", reason="", referenced_slot="card"))
    assert chat.waiting == "question" and "여러 개" in out
    out = chat.say("여행 카드", S(cancel=False, selected_id="card-002"))
    assert "여행 카드 (card-002" in out


def test_reference_without_context_asks_all(chat):
    out = chat.say("그 카드 잠가줘", P(intent="lock_card", reason="", referenced_slot="card"))
    assert "확실히 알 수 없어요" in out and "생활비 카드" in out and "여행 카드" in out


def test_followup_uses_last_request(chat):
    chat.say("이체", transfer(10_000))
    chat.say("승인", APPROVE)
    chat.say("이체", transfer(20_000, src="저축", dst="여행 자금"))
    chat.say("안 할래", REJECT)
    chat.say("잔액", P(intent="query_accounts", reason=""))           # 조회는 마지막 요청을 바꾸지 않음
    out = chat.say("아까 이체 됐어?", P(intent="query_request_status", reason="", request_kind="transfer"))
    assert "'취소'" in out and "20,000원" in out
    out = chat.say("그 전에 한 이체는?", P(intent="query_request_status", reason="", request_kind="transfer", earlier_request=True))
    assert "'완료'" in out and "10,000원" in out
    assert chat.state()["last_targets"]["request"] == "req-001"
