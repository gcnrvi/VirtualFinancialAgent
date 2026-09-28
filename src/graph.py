# 업무 흐름에 필요한 State와 노드 함수를 정의합니다.
# 요청 해석, 업무 함수 호출, 승인·거절과 결과 안내를 연결합니다.
# LangGraph의 중단·재개로 사용자 승인을 처리합니다.
#
# 흐름 (docs/design_draft.md 2절)
#   parse_request → lookup → respond        (대상이 불명확하면 ask_user → lookup)
#                 → resolve → ask_user → resolve
#                           → create_request → confirm → execute → next_task → respond
#                                                      → (거절) next_task
#                                                      → (수정) resolve
#                                                      → (판단 불가) confirm
#                           → (복합 업무에서 이미 완료된 단계) next_task
#   next_task: task_queue에 남은 단계가 있으면 resolve, 없으면 respond

import sqlite3
from functools import lru_cache
from typing import Annotated, Literal, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt
from pydantic import BaseModel, Field

import functions as fn
from data_store import CHECKPOINT_PATH, SaveError, load_data

load_dotenv()

MODEL_NAME = "gemini-3.6-flash"


@lru_cache
def get_llm() -> ChatGoogleGenerativeAI:
    return ChatGoogleGenerativeAI(model=MODEL_NAME)


# ---------------------------------------------------------------------------
# LLM 출력 스키마 (설계 4절)
# ---------------------------------------------------------------------------

Intent = Literal[
    "query_accounts", "transfer",
    "query_cards", "report_lost", "lock_card", "unlock_card",
    "reissue_card", "lost_and_reissue", "query_reissue", "modify_reissue", "cancel_reissue",
    "unknown",
]

INTENT_GUIDE = """\
- query_accounts: 계좌 목록·잔액·총액 질문
- transfer: 내 계좌 간 이체 요청
- query_cards: 카드 목록·상태 질문
- report_lost: 카드를 잃어버렸거나 도난당해 정지 요청 (되돌릴 수 없음)
- lock_card: 카드를 잠깐 못 쓰게 잠그는 요청 (집에 두고 옴, 잠시 보관 등)
- unlock_card: 잠근 카드를 다시 쓰도록 푸는 요청 (카드를 찾음 등)
- reissue_card: 카드 재발급 신청만 요청 (정지 요청은 없음)
- lost_and_reissue: 카드 분실 정지와 재발급을 함께 요청 (예: 잃어버렸어, 정지하고 재발급해줘)
- query_reissue: 재발급 신청의 상태·배송지 조회
- modify_reissue: 재발급 신청의 배송지 변경
- cancel_reissue: 재발급 신청 취소
- unknown: 그 외 또는 판단 불가"""


class Slots(BaseModel):
    """업무별 슬롯. 필드 이름은 functions.TASKS의 Task.slots와 같다."""
    from_account: str | None = Field(None, description="출금 계좌 별명. 언급이 없으면 null")
    to_account: str | None = Field(None, description="입금 계좌 별명. 언급이 없으면 null")
    amount: int | None = Field(None, description="원 단위 정수. '10만 원' → 100000, 음수도 그대로. 언급이 없으면 null")
    card: str | None = Field(None, description="카드 이름 또는 카드 ID. 언급이 없으면 null")
    address: Literal["집", "회사"] | None = Field(None, description="재발급 카드 배송지. 언급이 없으면 null")
    application: str | None = Field(None, description="재발급 신청 ID (예: rei-001). 언급이 없으면 null")


class ParsedRequest(Slots):
    intent: Intent = Field(description=INTENT_GUIDE)
    reason: str = Field(description="분류 근거 한 문장")


class SlotAnswer(Slots):
    cancel: bool = Field(description="사용자가 요청을 그만두겠다고 하면 true")
    selected_id: str | None = Field(None, description="후보 목록에서 고른 항목의 id")


class ApprovalReply(Slots):
    decision: Literal["approve", "reject", "modify", "unclear"] = Field(description=(
        "approve: 진행 동의 / reject: 취소·거절 / "
        "modify: 처리안 일부를 바꿔 달라는 요청 (바뀐 값만 채움) / unclear: 판단 불가"
    ))


# ---------------------------------------------------------------------------
# State (설계 5절)
# ---------------------------------------------------------------------------

class BankState(TypedDict, total=False):
    messages: Annotated[list, add_messages]  # 대화 기록
    user_id: str                  # 현재 사용자
    today: str                    # 기준일 YYYY-MM-DD
    intent: str                   # 현재 업무
    slots: dict                   # LLM이 추출한 값 (별명·금액, 후보 선택 시 *_id)
    slots_backup: dict | None     # 승인 전 수정 직전의 slots (수정 실패 시 복구)
    draft: dict | None            # ID로 해석된 처리안
    step: str | None              # 직전 노드의 결과 (ok | ask | fail | cancel | revert)
    question: str | None          # 추가 질문 문장
    candidates: dict | None       # 선택이 필요한 후보 {"slot": ..., "options": [...]}
    plan: str | None              # 승인 질문에 보여줄 처리안
    notice: str | None            # 승인 질문 앞에 덧붙일 안내
    error: str | None             # 검증·실행 실패 사유
    request_id: str | None        # 현재 처리 중인 requests 항목
    decision: str | None          # approve | reject | modify | unclear
    result: dict | None           # 조회·실행 결과
    task_queue: list[dict]        # 이어서 처리할 업무 [{"intent", "slots"}] (정지 후 재발급)
    multi_step: bool              # 복합 업무 진행 중 여부
    parent_request_id: str | None # 복합 업무에서 앞 단계의 요청 ID
    log: list[str]                # 복합 업무의 단계별 결과
    log_shown: int                # log 중 사용자에게 이미 보여준 개수
    last_targets: dict            # 최근 다룬 대상 (6단계에서 사용)


# 업무가 끝나면 비우는 필드
TASK_FIELDS = {
    "slots": {}, "slots_backup": None, "draft": None, "step": None,
    "question": None, "candidates": None, "plan": None, "notice": None,
    "request_id": None, "decision": None, "parent_request_id": None,
}


def _user_context(user_id: str) -> str:
    """LLM에 전달할 사용자의 계좌·카드 이름 목록."""
    data = load_data()
    accounts = "\n".join(
        f"- {a['nickname']} ({a['account_id']})"
        for a in data["accounts"] if a["owner_id"] == user_id
    )
    cards = "\n".join(
        f"- {c['name']} ({c['card_id']})"
        for c in data["cards"] if c["owner_id"] == user_id
    )
    applications = "\n".join(
        f"- {a['application_id']} ({a['card_id']}, {fn.APPLICATION_STATUS_LABELS[a['status']]})"
        for a in data["reissue_applications"] if a["owner_id"] == user_id
    ) or "- 없음"
    return (
        f"사용자의 계좌 목록:\n{accounts}\n사용자의 카드 목록:\n{cards}\n"
        f"사용자의 재발급 신청 목록:\n{applications}"
    )


def _pick_slots(model: BaseModel, intent: str) -> dict:
    """LLM 출력에서 현재 업무가 쓰는 슬롯만 꺼낸다."""
    return {name: getattr(model, name) for name in fn.TASKS[intent].slots}


def _merge_slots(slots: dict, changes: dict) -> dict:
    """새로 받은 값만 덮어쓴다. 이름이 바뀌면 이전에 후보에서 고른 ID는 버린다."""
    merged = dict(slots)
    for key, value in changes.items():
        if value is None:
            continue
        merged[key] = value
        merged.pop(f"{key}_id", None)
    return merged


# ---------------------------------------------------------------------------
# 노드
# ---------------------------------------------------------------------------

def parse_request(state: BankState) -> dict:
    """새 요청을 해석한다. 이전 업무의 필드를 초기화하고 오늘 날짜를 갱신한다."""
    user_id = state["user_id"]
    text = state["messages"][-1].content
    today = fn.today_kst()
    update = {
        **TASK_FIELDS, "today": today, "error": None, "result": None,
        "task_queue": [], "multi_step": False, "log": [], "log_shown": 0,
    }

    prompt = (
        "당신은 은행 앱의 요청 해석기입니다. 사용자 요청을 분류하고 필요한 값을 추출하세요.\n"
        f"오늘 날짜: {today}\n"
        f"{_user_context(user_id)}\n"
        "계좌 별명과 카드 이름은 사용자가 말한 표현 그대로 추출하세요.\n"
        "현재 업무에 필요 없는 값은 null로 두세요.\n\n"
        f"사용자 요청: {text}"
    )
    try:
        parsed = get_llm().with_structured_output(ParsedRequest).invoke(prompt)
    except Exception as e:
        return {**update, "intent": "unknown", "error": f"요청을 해석하지 못했어요. 다시 말씀해 주세요. ({type(e).__name__})"}

    if parsed.intent in fn.COMPOSITES:
        # 복합 업무: 첫 단계를 바로 진행하고 나머지는 task_queue에 넣는다.
        first, *rest = fn.COMPOSITES[parsed.intent]
        return {
            **update,
            "intent": first,
            "slots": _pick_slots(parsed, first),
            "task_queue": [{"intent": i, "slots": _pick_slots(parsed, i)} for i in rest],
            "multi_step": True,
        }

    slots = _pick_slots(parsed, parsed.intent) if parsed.intent in fn.TASKS else {}
    return {**update, "intent": parsed.intent, "slots": slots}


def lookup(state: BankState) -> dict:
    task = fn.TASKS[state["intent"]]
    result = task.query(state["user_id"], state.get("slots", {}))
    if result["status"] == "ask":
        return {"step": "ask", "question": result["question"], "candidates": result.get("candidates")}
    if result["status"] == "fail":
        return {"step": "fail", "error": result["error"]}
    return {"step": "ok", "result": result}


def resolve(state: BankState) -> dict:
    task = fn.TASKS[state["intent"]]
    r = task.resolve(state["user_id"], state["slots"])

    if r["status"] == "ok":
        return {"step": "ok", "draft": r["draft"], "question": None, "candidates": None}
    if r["status"] == "ask":
        return {"step": "ask", "question": r["question"], "candidates": r.get("candidates")}

    # 승인 전 수정이 검증에 실패하면 기존 처리안으로 되돌려 다시 묻는다.
    if state.get("request_id") and state.get("slots_backup") is not None:
        return {
            "step": "revert",
            "slots": state["slots_backup"],
            "slots_backup": None,
            "notice": f"그렇게 바꿀 수 없어요. {r['error']} 기존 처리안을 유지할게요.",
        }
    # 복합 업무에서 이미 원하는 상태면(예: 이미 분실 정지) 이 단계를 건너뛰고 다음 단계로 간다.
    if r.get("already") and state.get("task_queue"):
        return {"step": "skip", "log": _log(state, f"{r['error']} 이 단계는 건너뛸게요.")}
    # 이미 기록된 요청을 다시 검증하다 실패하면(재시작 복구 등) 요청도 실패로 남긴다.
    if state.get("request_id"):
        fn.fail_request(state["request_id"], r["error"])
    return {"step": "fail", "error": r["error"]}


def ask_user(state: BankState) -> dict:
    """빠진 정보나 후보 선택을 묻고, 답변을 slots에 반영한다."""
    question = state["question"]
    candidates = state.get("candidates")
    shown = "\n\n".join(filter(None, [_unshown_log(state), question]))
    answer = interrupt({"type": "question", "question": shown})

    messages = [AIMessage(shown), HumanMessage(answer)]
    prompt = (
        "은행 앱이 사용자에게 추가 정보를 물었고, 사용자가 답했습니다. 답변에서 값을 추출하세요.\n"
        f"진행 중인 업무: {fn.TASKS[state['intent']].label}\n"
        f"지금까지 받은 값: {state['slots']}\n"
        f"질문: {question}\n"
    )
    if candidates:
        prompt += f"후보 목록(이 중에서 고르면 selected_id에 해당 id를 넣으세요): {candidates['options']}\n"
    prompt += f"사용자 답변: {answer}"

    try:
        reply = get_llm().with_structured_output(SlotAnswer).invoke(prompt)
    except Exception:
        # 해석 실패 시 slots를 바꾸지 않고 resolve로 돌아가 같은 질문을 다시 한다.
        return {"messages": messages, "log_shown": len(state.get("log") or [])}

    if reply.cancel:
        if state.get("request_id"):
            fn.cancel_request(state["request_id"])
        message = (
            "이 단계부터 요청을 취소했어요. 앞 단계에서 처리된 내용은 그대로 유지돼요."
            if state.get("log") else "요청을 취소했어요. 변경된 내용은 없어요."
        )
        return {"messages": messages, "step": "cancel", "error": message, "log_shown": len(state.get("log") or [])}

    slots = _merge_slots(state["slots"], _pick_slots(reply, state["intent"]))

    if candidates and reply.selected_id:
        valid_ids = {o["id"] for o in candidates["options"]}
        if reply.selected_id in valid_ids:
            slots[candidates["slot"]] = reply.selected_id

    return {"messages": messages, "slots": slots, "step": None, "log_shown": len(state.get("log") or [])}


def create_request(state: BankState) -> dict:
    """처리안을 requests에 승인 대기로 기록한다. 수정 후 재진입이면 같은 요청을 갱신한다."""
    task = fn.TASKS[state["intent"]]
    request_id = state.get("request_id")
    try:
        if request_id:
            fn.update_request_params(request_id, state["draft"])
        else:
            request_id = fn.create_request(
                state["user_id"], state["intent"], state["draft"], state.get("parent_request_id")
            )
    except SaveError as e:
        return {"step": "fail", "error": f"요청을 기록하지 못해 진행할 수 없어요. ({e})"}

    return {
        "step": "ok",
        "request_id": request_id,
        "plan": task.describe(state["user_id"], state["draft"]),
        "slots_backup": None,
    }


def confirm(state: BankState) -> dict:
    """처리안을 보여주고 승인·거절·수정을 받는다."""
    notice = state.get("notice")
    question = "이대로 진행할까요? (승인 / 거절 / 바꿀 내용)"
    shown = "\n\n".join(filter(None, [_unshown_log(state), notice, state["plan"], question]))
    answer = interrupt({"type": "approval", "request_id": state["request_id"], "message": shown})

    messages = [AIMessage(shown), HumanMessage(answer)]
    prompt = (
        "은행 앱이 아래 처리안의 승인 여부를 물었고, 사용자가 답했습니다. 답변을 분류하세요.\n"
        "modify라면 바뀐 값만 채우고, 나머지는 null로 두세요.\n\n"
        f"처리안:\n{state['plan']}\n\n사용자 답변: {answer}"
    )
    try:
        reply = get_llm().with_structured_output(ApprovalReply).invoke(prompt)
    except Exception:
        reply = ApprovalReply(decision="unclear")

    base = {
        "messages": messages, "notice": None, "decision": reply.decision,
        "log_shown": len(state.get("log") or []),
    }

    if reply.decision == "approve":
        return base
    if reply.decision == "reject":
        result = fn.cancel_request(state["request_id"])
        return {**base, "result": result, **_stop_queue(state, result["message"])}
    if reply.decision == "modify":
        changes = _pick_slots(reply, state["intent"])
        if any(v is not None for v in changes.values()):
            return {
                **base,
                "slots_backup": state["slots"],
                "slots": _merge_slots(state["slots"], changes),
            }
    # unclear, 또는 바꿀 값을 찾지 못한 modify
    return {
        **base,
        "decision": "unclear",
        "notice": "답변을 이해하지 못했어요. '승인', '거절' 또는 바꿀 내용을 말씀해 주세요.",
    }


def execute(state: BankState) -> dict:
    task = fn.TASKS[state["intent"]]
    result = task.execute(state["user_id"], state["request_id"])
    if result["status"] == "completed":
        return {"result": result, "log": _log(state, result["message"])}
    # 실행에 실패하면 뒤 단계는 진행하지 않는다.
    return {"result": result, **_stop_queue(state, result["message"])}


def next_task(state: BankState) -> dict:
    """task_queue에 이어서 처리할 업무가 있으면 꺼낸다. (정지 후 재발급)"""
    queue = list(state.get("task_queue") or [])
    if not queue:
        return {"step": "done"}
    task = queue.pop(0)

    # 앞 단계에서 확정한 대상(예: 후보에서 고른 카드)은 다음 단계에서도 그대로 쓴다.
    slots = dict(task["slots"])
    for key, value in (state.get("draft") or {}).items():
        if key.endswith("_id") and key[:-3] in slots:
            slots[key] = value

    return {
        **TASK_FIELDS,
        "step": "next",
        "intent": task["intent"],
        "slots": slots,
        "task_queue": queue,
        "parent_request_id": state.get("request_id") or state.get("parent_request_id"),
    }


def respond(state: BankState) -> dict:
    """결과를 안내 문장으로 만들고 업무 필드를 정리한다."""
    parts = list((state.get("log") or [])[state.get("log_shown", 0):])  # 아직 보여주지 않은 결과만
    if state.get("error"):
        parts.append(_labeled(state, state["error"]))
    if not parts:
        parts.append(_format_response(state))
    return {
        "messages": [AIMessage("\n".join(parts))],
        **TASK_FIELDS, "log": [], "log_shown": 0, "multi_step": False,
    }


def _labeled(state: BankState, message: str) -> str:
    """복합 업무에서는 어느 단계의 결과인지 앞에 붙인다."""
    if state.get("multi_step") and state.get("intent") in fn.TASKS:
        return f"[{fn.TASKS[state['intent']].label}] {message}"
    return message


def _unshown_log(state: BankState) -> str | None:
    """복합 업무에서 앞 단계 결과를 다음 질문 앞에 먼저 보여준다."""
    new = (state.get("log") or [])[state.get("log_shown", 0):]
    return "\n".join(new) if new else None


def _log(state: BankState, message: str) -> list[str]:
    return [*(state.get("log") or []), _labeled(state, message)]


def _stop_queue(state: BankState, message: str) -> dict:
    """현재 단계 결과를 기록하고, 남은 단계가 있으면 진행하지 않았다고 안내한다."""
    log = _log(state, message)
    queue = state.get("task_queue") or []
    if queue:
        labels = ", ".join(fn.TASKS[t["intent"]].label for t in queue)
        log.append(f"앞 단계가 완료되지 않아 {fn.with_eun(labels)} 진행하지 않았어요.")
    return {"log": log, "task_queue": []}


def _format_response(state: BankState) -> str:
    if state.get("error"):
        return state["error"]

    intent = state.get("intent")
    result = state.get("result") or {}

    if intent == "query_accounts":
        lines = [f"- {a['nickname']} ({a['account_id']}): {fn.won(a['balance'])}" for a in result["accounts"]]
        return "계좌 목록이에요.\n" + "\n".join(lines) + f"\n총 잔액: {fn.won(result['total'])}"
    if intent == "query_cards":
        lines = [f"- {c['name']} ({c['card_id']}, {c['card_type']}): {c['status']}" for c in result["cards"]]
        return "카드 목록이에요.\n" + "\n".join(lines)
    if intent == "query_reissue":
        a = result["application"]
        return (
            f"재발급 신청 {a['application_id']}\n- 카드: {a['card']}\n- 배송지: {a['address']}\n"
            f"- 상태: {a['status']}\n- 신청일: {a['created_at']}"
        )
    if intent in fn.TASKS and result.get("message"):
        return result["message"]
    return (
        "지금은 계좌 조회·이체, 카드 조회·분실 정지·잠금·해제, 카드 재발급 신청·조회·변경·취소를 도와드릴 수 있어요.\n"
        "예: '내 계좌 잔액 보여줘', '생활비에서 저축으로 10만 원 옮겨줘', '생활비 카드 잃어버렸어. 정지하고 재발급해줘'"
    )


# ---------------------------------------------------------------------------
# 분기
# ---------------------------------------------------------------------------

def route_after_parse(state: BankState) -> str:
    task = fn.TASKS.get(state["intent"])
    if task is None or state.get("error"):
        return "respond"
    return "lookup" if task.kind == "query" else "resolve"


def route_after_lookup(state: BankState) -> str:
    return "ask_user" if state.get("step") == "ask" else "respond"


def route_after_resolve(state: BankState) -> str:
    return {
        "ok": "create_request", "ask": "ask_user", "revert": "confirm", "skip": "next_task",
    }.get(state["step"], "respond")


def route_after_ask(state: BankState) -> str:
    if state.get("step") == "cancel":
        return "respond"
    return "lookup" if fn.TASKS[state["intent"]].kind == "query" else "resolve"


def route_after_create(state: BankState) -> str:
    return "confirm" if state["step"] == "ok" else "respond"


def route_after_confirm(state: BankState) -> str:
    return {
        "approve": "execute",
        "reject": "next_task",
        "modify": "resolve",
    }.get(state["decision"], "confirm")


def route_after_next(state: BankState) -> str:
    return "resolve" if state["step"] == "next" else "respond"


# ---------------------------------------------------------------------------
# 그래프 조립
# ---------------------------------------------------------------------------

def build_graph(checkpointer=None):
    builder = StateGraph(BankState)
    for name, node in [
        ("parse_request", parse_request), ("lookup", lookup), ("resolve", resolve),
        ("ask_user", ask_user), ("create_request", create_request), ("confirm", confirm),
        ("execute", execute), ("next_task", next_task), ("respond", respond),
    ]:
        builder.add_node(name, node)

    builder.add_edge(START, "parse_request")
    builder.add_conditional_edges("parse_request", route_after_parse, ["lookup", "resolve", "respond"])
    builder.add_conditional_edges("lookup", route_after_lookup, ["ask_user", "respond"])
    builder.add_conditional_edges("resolve", route_after_resolve, ["create_request", "ask_user", "confirm", "next_task", "respond"])
    builder.add_conditional_edges("ask_user", route_after_ask, ["lookup", "resolve", "respond"])
    builder.add_conditional_edges("create_request", route_after_create, ["confirm", "respond"])
    builder.add_conditional_edges("confirm", route_after_confirm, ["execute", "next_task", "resolve", "confirm"])
    builder.add_edge("execute", "next_task")
    builder.add_conditional_edges("next_task", route_after_next, ["resolve", "respond"])
    builder.add_edge("respond", END)
    return builder.compile(checkpointer=checkpointer)


def open_checkpointer() -> tuple[SqliteSaver, sqlite3.Connection]:
    """SQLite 체크포인터를 연다. reset_data 전에는 반환한 연결을 닫아야 한다."""
    conn = sqlite3.connect(CHECKPOINT_PATH, check_same_thread=False)
    return SqliteSaver(conn), conn
