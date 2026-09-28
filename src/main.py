# 사용자 입력을 반복해서 받고 그래프의 응답을 출력합니다.
# 같은 대화 세션을 유지하고 승인·거절 입력과 종료 명령을 처리합니다.
# 시작할 때 중단된 업무가 있으면 확인하고 이어서 처리합니다. (설계 7절)
#
# 실행: uv run python src/main.py

import logging
import warnings

from langchain_core.messages import HumanMessage
from langgraph.types import Command

import data_store
import functions as fn
import graph

USER_ID = "user-001"
CONFIG = {"configurable": {"thread_id": USER_ID}}  # 사용자 단위로 대화 세션 고정

EXIT_COMMANDS = {"종료", "exit", "quit", "q"}
RESET_COMMANDS = {"초기화", "/reset"}
SEED_RESET_COMMANDS = {"초기화 테스트", "/reset seed"}
HELP_COMMANDS = {"도움말", "/help"}

HELP_TEXT = """\
사용할 수 있는 요청 예시
  - 내 계좌 목록과 잔액을 보여줘
  - 내 계좌에 총 얼마 있어?
  - 생활비에서 저축으로 10만 원 옮겨줘
  - 내 카드 목록과 상태를 보여줘
  - 생활비 카드 잃어버렸어. 정지해줘
  - 생활비 카드를 집에 둔 것 같아. 잠깐 잠가줘
  - 생활비 카드 찾았어. 잠금 풀어줘
  - 생활비 카드를 잃어버렸어. 정지하고 재발급해줘
  - 카드 재발급 신청 상태 알려줘
  - 재발급 카드 배송지를 회사로 바꿔줘 / 재발급 신청 취소해줘
승인 질문에는 '승인', '거절' 또는 바꿀 내용('아니, 5만 원만')으로 답하세요.
명령: 도움말 | 초기화 | 초기화 테스트(제작 중·배송 중 재발급 신청 포함) | 종료"""

RESTART_NOTICE = "프로그램이 다시 시작되어 승인이 완료되지 않은 요청을 다시 확인할게요."


class App:
    def __init__(self):
        self._open()

    def _open(self):
        self.saver, self.conn = graph.open_checkpointer()
        self.graph = graph.build_graph(self.saver)

    def reset(self, with_seed: bool = False):
        """체크포인트 연결을 닫은 뒤 업무 데이터와 대화 기록을 함께 초기화한다."""
        self.conn.close()
        data_store.reset_data(with_seed=with_seed)
        self._open()

    # ---- 그래프 실행 ----------------------------------------------------

    def send(self, text: str) -> None:
        """승인·추가 질문을 기다리는 중이면 답변으로 재개하고, 아니면 새 요청으로 시작한다."""
        if self._pending_interrupt():
            self._run(Command(resume=text))
        else:
            self._run({"messages": [HumanMessage(text)], "user_id": USER_ID})

    def _run(self, graph_input) -> None:
        try:
            output = self.graph.invoke(graph_input, CONFIG)
        except Exception as e:
            # 노드 실행 중 예상하지 못한 오류. 체크포인트는 마지막으로 완료된 노드에 남는다.
            print(f"\n오류가 발생해 처리를 멈췄어요. ({type(e).__name__}: {e})")
            print("다음 입력 전에 진행 중이던 업무를 다시 확인할게요.")
            return
        self._print_output(output)

    def _print_output(self, output: dict) -> None:
        if "__interrupt__" in output:
            value = output["__interrupt__"][0].value
            print(f"\n에이전트> {value.get('question') or value.get('message')}")
        else:
            print(f"\n에이전트> {output['messages'][-1].content}")

    def _pending_interrupt(self) -> dict | None:
        state = self.graph.get_state(CONFIG)
        for task in state.tasks:
            if task.interrupts:
                return task.interrupts[0].value
        return None

    # ---- 재시작·장애 복구 (설계 7절) -----------------------------------

    def recover(self, startup: bool = False) -> None:
        """중단된 업무가 있으면 상태에 맞게 이어서 처리한다.

        이전 승인만으로 변경을 실행하지 않는다. 승인 대기·실행 중 중단은
        처리안을 다시 검증하고 새로 승인받는다.
        startup=False(대화 중)이면 정상적인 질문·승인 대기는 건드리지 않고,
        오류로 멈춘 경우만 처리한다.
        """
        state = self.graph.get_state(CONFIG)
        if not state.next:
            return
        if not startup and self._pending_interrupt():
            return

        values = state.values
        node = state.next[0]
        request = fn.get_request(values["request_id"]) if values.get("request_id") else None

        # 추가 질문 대기: 같은 질문을 다시 보여준다.
        if node == "ask_user":
            print("\n[이어서 진행] 답변을 기다리던 질문이 있어요.")
            print(f"\n에이전트> {self._pending_interrupt()['question']}")
            return

        # 실행이 이미 JSON에 반영됐다면(저장 후 체크포인트 전에 종료) 결과만 안내한다.
        if request and request["status"] != "pending_approval":
            print("\n[이어서 진행] 이전 요청은 이미 처리되었어요.")
            message = (request.get("result") or {}).get("message", request["status"])
            self.graph.update_state(CONFIG, {"result": {"message": message}}, as_node="execute")
            self._run(None)
            return

        # 승인 대기·실행 전 중단: 처리안을 다시 검증하고 새로 승인받는다.
        if request:
            print("\n[이어서 진행] 승인이 완료되지 않은 요청이 있어요.")
            self.graph.update_state(
                CONFIG,
                {"notice": RESTART_NOTICE, "decision": None, "slots_backup": None},
                as_node="parse_request",
            )
            self._run(None)
            return

        # 요청 기록 전 단계(조회·검증·안내)에서 멈춘 경우: 데이터 변경이 없으므로 그대로 이어서 실행한다.
        print("\n[이어서 진행] 처리 중이던 요청을 이어서 진행할게요.")
        self._run(None)


def main() -> None:
    logging.getLogger("google_genai").setLevel(logging.ERROR)
    warnings.filterwarnings("ignore", category=UserWarning)

    data_store.ensure_data()
    app = App()

    print("가상 금융 업무 에이전트입니다. ('도움말'로 사용법 확인, '종료'로 끝내기)")
    app.recover(startup=True)

    while True:
        try:
            text = input("\n나> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n종료합니다.")
            break

        if not text:
            continue
        if text in EXIT_COMMANDS:
            print("종료합니다. 진행 중이던 업무는 다음 실행 때 이어서 확인할 수 있어요.")
            break
        if text in HELP_COMMANDS:
            print(HELP_TEXT)
            continue
        if text in RESET_COMMANDS:
            app.reset()
            print("데이터와 대화 기록을 초기 상태로 되돌렸어요.")
            continue
        if text in SEED_RESET_COMMANDS:
            app.reset(with_seed=True)
            print("테스트 데이터로 초기화했어요. (여행 카드: 분실 정지·재발급 제작 중, 교통 카드: 분실 정지·재발급 배송 중)")
            continue

        app.recover()  # 직전 실행이 오류로 멈췄다면 먼저 정리
        app.send(text)

    app.conn.close()


if __name__ == "__main__":
    main()
