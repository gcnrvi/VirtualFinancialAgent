# 재시작·장애 복구 (설계 7절). 대화를 중간에 멈춘 상태를 만든 뒤 main.App을 새로 열어 복구한다.
# 원칙: 재시작 후에는 어떤 경우에도 새 승인 없이 변경을 실행하지 않는다.

import pytest

import functions as fn
import main
from conftest import USER, balance, load, set_balance
from test_graph_flow import APPROVE, Chat, P, S, transfer


@pytest.fixture
def chat(fake_llm):
    c = Chat(fake_llm, thread=main.USER_ID)          # main.App과 같은 thread_id
    yield c
    c.close()


def restart(capsys):
    """프로그램을 다시 켠 것처럼 App을 새로 열고 시작 복구를 실행한다."""
    app = main.App()
    app.recover(startup=True)
    return app, capsys.readouterr().out


def pending(app):
    return app._pending_interrupt()


def test_pending_question_is_shown_again(chat, capsys):
    chat.say("저축으로 5만원", transfer(50_000, src=None))
    chat.close()

    app, out = restart(capsys)
    assert "답변을 기다리던 질문" in out and "출금 계좌" in out
    assert pending(app)["type"] == "question"
    app.conn.close()


def test_pending_approval_is_revalidated_and_asked_again(chat, capsys):
    chat.say("생활비에서 저축으로 5만원", transfer(50_000))
    chat.close()

    app, out = restart(capsys)
    assert "승인이 완료되지 않은 요청" in out and "프로그램이 다시 시작되어" in out
    assert pending(app)["type"] == "approval"
    assert balance("acc-001") == 1_431_800
    app.conn.close()


def test_approved_but_not_executed_is_not_run_automatically(chat, capsys):
    chat.say("생활비에서 저축으로 2만원", transfer(20_000))
    chat.app.update_state(chat.config, {"decision": "approve"}, as_node="confirm")   # 승인 직후 종료
    assert chat.app.get_state(chat.config).next == ("execute",)
    chat.close()

    app, out = restart(capsys)
    assert pending(app)["type"] == "approval"          # 다시 승인받음
    assert balance("acc-001") == 1_431_800             # 자동 실행 없음
    app.conn.close()


def test_executed_in_json_but_not_in_checkpoint_is_not_duplicated(chat, capsys):
    chat.say("생활비에서 저축으로 3만원", transfer(30_000))
    chat.app.update_state(chat.config, {"decision": "approve"}, as_node="confirm")
    fn.execute_transfer(USER, "req-001")               # JSON 저장 후 체크포인트 전에 종료
    chat.close()

    app, out = restart(capsys)
    assert "이미 처리되었어요" in out and "30,000원을 이체했어요" in out
    assert pending(app) is None
    assert balance("acc-001") == 1_401_800             # 한 번만 반영
    app.conn.close()


def test_revalidation_failure_on_restart_marks_failed(chat, capsys):
    chat.say("생활비에서 저축으로 5만원", transfer(50_000))
    chat.close()
    set_balance("acc-001", 10_000)

    app, out = restart(capsys)
    assert "잔액(10,000원)" in out
    assert fn.get_request("req-001")["status"] == "failed"
    app.conn.close()


def test_batch_interrupted_midway_asks_only_remaining(chat, capsys, fail_save_at, today):
    today("2026-09-28")
    chat.say("미납 전부 납부", P(intent="pay_bills_batch", reason="", from_account="생활비", all_bills=True))
    chat.app.update_state(chat.config, {"decision": "approve"}, as_node="confirm")
    fail_save_at("2,3")                                # 가스만 저장된 채 중단
    fn.TASKS["pay_bills_batch"].execute(USER, "req-001")
    fail_save_at("")
    chat.close()

    app, out = restart(capsys)
    plan = pending(app)["message"]
    assert "가스요금" not in plan and "총액: 245,000원 (4건)" in plan
    app.conn.close()


def test_orphan_pending_request_is_closed_on_start(capsys):
    fn.create_request(USER, "transfer", {"from_account_id": "acc-001", "to_account_id": "acc-002", "amount": 1})

    app, out = restart(capsys)
    assert "처리가 끝나지 않은 요청" in out
    assert fn.get_request("req-001")["status"] == "failed"
    app.conn.close()


def test_reset_clears_data_and_conversation(chat, capsys):
    chat.say("생활비에서 저축으로 5만원", transfer(50_000))
    chat.close()
    app = main.App()
    app.reset()
    assert load()["requests"] == []
    assert not app.graph.get_state({"configurable": {"thread_id": main.USER_ID}}).next
    app.conn.close()
