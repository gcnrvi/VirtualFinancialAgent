# 선택한 계좌·이체·카드·청구서 업무를 Python 함수로 구현합니다.
# 대상과 처리 조건을 검증하고 데이터를 조회하거나 변경합니다.
# 업무별 함수는 TASKS 레지스트리에 등록하고, 그래프의 공통 노드가 호출합니다.
#
# 함수 반환 형식
#   query(user_id, slots)       -> {"status": "ok", ...조회 결과} 또는 resolve와 같은 ask·fail
#   resolve(user_id, slots)     -> {"status": "ok",   "draft": {...ID로 해석된 처리안}}
#                                  {"status": "ask",  "question": str, "missing": [...], "candidates": {...}}
#                                  {"status": "fail", "error": str}
#   describe(user_id, draft)    -> 승인 질문에 보여줄 처리안 문장
#   execute(user_id, request_id) -> {"status": "completed" | "failed" | "skipped", "message": str, ...}

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from data_store import SaveError, load_data, save_data

KST = ZoneInfo("Asia/Seoul")


# ---------------------------------------------------------------------------
# 공통 도구
# ---------------------------------------------------------------------------

def now_kst() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def today_kst() -> str:
    return datetime.now(KST).date().isoformat()


def won(amount: int) -> str:
    return f"{amount:,}원"


def _final_consonant(word: str) -> int | None:
    """마지막 글자의 받침 번호 (0: 받침 없음, 8: ㄹ). 한글이 아니면 None."""
    code = ord(word[-1]) - 0xAC00
    return code % 28 if 0 <= code <= 11171 else None


def with_ro(word: str) -> str:
    """받침에 맞춰 '로/으로'를 붙인다. (ㄹ 받침은 '로')"""
    final = _final_consonant(word)
    if final is None:
        return f"{word}(으)로"
    return f"{word}{'로' if final in (0, 8) else '으로'}"


def with_eul(word: str) -> str:
    """받침에 맞춰 '을/를'을 붙인다."""
    final = _final_consonant(word)
    if final is None:
        return f"{word}을(를)"
    return f"{word}{'를' if final == 0 else '을'}"


def _next_id(items: list[dict], id_field: str, prefix: str) -> str:
    """'tx-020'까지 있으면 'tx-021'을 만든다."""
    numbers = [
        int(m.group(1))
        for item in items
        if (m := re.fullmatch(rf"{prefix}-(\d+)", item[id_field]))
    ]
    return f"{prefix}-{max(numbers, default=0) + 1:03d}"


def _user_accounts(data: dict, user_id: str) -> list[dict]:
    return [a for a in data["accounts"] if a["owner_id"] == user_id]


def _find_account(data: dict, user_id: str, account_id: str) -> dict | None:
    return next(
        (a for a in _user_accounts(data, user_id) if a["account_id"] == account_id),
        None,
    )


def _normalize_name(name: str) -> str:
    """'여행자금 계좌', '여행 자금' 을 같은 이름으로 비교하기 위해 정리한다."""
    name = re.sub(r"\s+", "", name)
    return re.sub(r"(계좌|통장)$", "", name)


def match_accounts(data: dict, user_id: str, name: str) -> tuple[list[dict], bool]:
    """별명으로 내 계좌를 찾는다.

    반환: (후보 목록, 정확히 일치했는지)
    정확히 일치하는 계좌가 없으면 이름을 포함하는 계좌를 후보로 돌려준다.
    이 경우 확신할 수 없으므로 호출한 쪽에서 사용자에게 확인받는다.
    """
    accounts = _user_accounts(data, user_id)
    key = _normalize_name(name)
    exact = [a for a in accounts if _normalize_name(a["nickname"]) == key]
    if exact:
        return exact, True
    partial = [a for a in accounts if key and key in _normalize_name(a["nickname"])]
    return partial, False


def _account_brief(account: dict) -> dict:
    return {
        "account_id": account["account_id"],
        "nickname": account["nickname"],
        "balance": account["balance"],
    }


def _resolve_account_slot(
    data: dict, user_id: str, slots: dict, role: str, label: str
) -> dict:
    """slots의 '<role>_id'(선택 완료) 또는 '<role>'(별명)을 계좌 하나로 확정한다.

    반환: {"status": "ok", "account": {...}} / ask / fail
    """
    account_id = slots.get(f"{role}_id")
    if account_id:
        account = _find_account(data, user_id, account_id)
        if account is None:
            return {"status": "fail", "error": f"{label}({account_id})를 찾을 수 없어요."}
        return {"status": "ok", "account": account}

    name = slots.get(role)
    matches, exact = match_accounts(data, user_id, name)
    if not matches:
        return {"status": "fail", "error": f"'{name}' 계좌를 찾을 수 없어요."}
    if exact and len(matches) == 1:
        return {"status": "ok", "account": matches[0]}

    options = [{"id": a["account_id"], "label": a["nickname"]} for a in matches]
    return _choice_question(label, name, "계좌", exact, f"{role}_id", options)


def _choice_question(
    label: str, name: str, noun: str, exact: bool, slot: str, options: list[dict]
) -> dict:
    """후보 중 하나를 고르게 하는 ask 결과를 만든다. options: [{"id", "label"}]"""
    lines = [f"{i}) {o['label']} ({o['id']})" for i, o in enumerate(options, 1)]
    reason = "여러 개 있어요" if exact else "정확히 일치하지 않아요"
    return {
        "status": "ask",
        "question": f"{label} '{name}'에 해당하는 {noun}가 {reason}. 어느 {noun}인가요?\n"
        + "\n".join(lines),
        "missing": [],
        "candidates": {"slot": slot, "options": options},
    }


# ---------------------------------------------------------------------------
# requests (업무 요청 처리 기록)
# ---------------------------------------------------------------------------

def create_request(user_id: str, task_type: str, params: dict) -> str:
    """승인 대기 요청을 기록하고 request_id를 돌려준다. 저장 실패 시 SaveError."""
    data = load_data()
    now = now_kst()
    request_id = _next_id(data["requests"], "request_id", "req")
    data["requests"].append({
        "request_id": request_id,
        "owner_id": user_id,
        "type": task_type,
        "params": params,
        "status": "pending_approval",
        "parent_request_id": None,
        "created_at": now,
        "updated_at": now,
        "result": None,
    })
    save_data(data)
    return request_id


def get_request(request_id: str) -> dict | None:
    data = load_data()
    return next((r for r in data["requests"] if r["request_id"] == request_id), None)


def update_request_params(request_id: str, params: dict) -> None:
    """승인 전 수정: 같은 요청의 처리안을 갱신한다. 저장 실패 시 SaveError."""
    data = load_data()
    request = _find_request(data, request_id)
    if request["status"] != "pending_approval":
        raise ValueError(f"{request_id}는 승인 대기 상태가 아니에요.")
    request["params"] = params
    request["updated_at"] = now_kst()
    save_data(data)


def cancel_request(request_id: str) -> dict:
    """사용자가 거절한 요청을 취소로 기록한다."""
    data = load_data()
    request = _find_request(data, request_id)
    if request["status"] != "pending_approval":
        return {"status": "skipped", "message": "이미 처리가 끝난 요청이에요."}
    _set_request_result(request, "cancelled", "사용자가 요청을 취소했어요.")
    try:
        save_data(data)
    except SaveError as e:
        return {"status": "failed", "message": f"취소 기록을 저장하지 못했어요. ({e})"}
    return {"status": "cancelled", "message": "요청을 취소했어요. 변경된 내용은 없어요."}


def fail_request(request_id: str, message: str) -> None:
    """승인 대기 중 재검증에 실패한 요청을 실패로 기록한다. (재시작 복구 등)"""
    data = load_data()
    request = _find_request(data, request_id)
    if request["status"] != "pending_approval":
        return
    _set_request_result(request, "failed", message)
    try:
        save_data(data)
    except SaveError:
        pass  # 데이터 변경은 없으므로 안내만 한다.


def _find_request(data: dict, request_id: str) -> dict:
    request = next((r for r in data["requests"] if r["request_id"] == request_id), None)
    if request is None:
        raise KeyError(f"요청 {request_id}를 찾을 수 없어요.")
    return request


def _set_request_result(request: dict, status: str, message: str, **extra) -> None:
    request["status"] = status
    request["updated_at"] = now_kst()
    request["result"] = {"message": message, **extra}


# ---------------------------------------------------------------------------
# 계좌 조회
# ---------------------------------------------------------------------------

def query_accounts(user_id: str, slots: dict | None = None) -> dict:
    """계좌 목록, 계좌별 잔액, 합계를 돌려준다."""
    data = load_data()
    accounts = [_account_brief(a) for a in _user_accounts(data, user_id)]
    return {
        "status": "ok",
        "accounts": accounts,
        "total": sum(a["balance"] for a in accounts),
    }


# ---------------------------------------------------------------------------
# 즉시이체
# ---------------------------------------------------------------------------

TRANSFER_SLOTS = {"from_account": "출금 계좌", "to_account": "입금 계좌", "amount": "이체 금액"}


def resolve_transfer(user_id: str, slots: dict) -> dict:
    """이체 슬롯을 검증해 처리안(draft)을 만든다. 설계 5-2의 순서를 따른다."""
    data = load_data()

    # 1. 필수 정보 누락
    missing = [
        slot for slot in TRANSFER_SLOTS
        if slots.get(slot) is None and slots.get(f"{slot}_id") is None
    ]
    if missing:
        labels = ", ".join(TRANSFER_SLOTS[s] for s in missing)
        return {
            "status": "ask",
            "question": f"다음 정보를 알려주세요: {labels}",
            "missing": missing,
            "candidates": None,
        }

    # 2~3. 계좌 매칭 (없음 → 실패, 여러 개·불확실 → 선택)
    resolved = {}
    for role in ("from_account", "to_account"):
        result = _resolve_account_slot(data, user_id, slots, role, TRANSFER_SLOTS[role])
        if result["status"] != "ok":
            return result
        resolved[role] = result["account"]

    draft = {
        "from_account_id": resolved["from_account"]["account_id"],
        "to_account_id": resolved["to_account"]["account_id"],
        "amount": slots["amount"],
    }

    # 4~6. 금액·계좌·잔액 검증
    error = _validate_transfer(data, user_id, draft)
    if error:
        return {"status": "fail", "error": error}
    return {"status": "ok", "draft": draft}


def _validate_transfer(data: dict, user_id: str, draft: dict) -> str | None:
    """승인 전 검증과 실행 직전 재검증에 함께 쓴다. 문제가 없으면 None."""
    amount = draft["amount"]
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        return "이체 금액은 1원 이상의 정수여야 해요."

    source = _find_account(data, user_id, draft["from_account_id"])
    target = _find_account(data, user_id, draft["to_account_id"])
    if source is None or target is None:
        return "이체할 계좌를 찾을 수 없어요."
    if source["account_id"] == target["account_id"]:
        return "같은 계좌로는 이체할 수 없어요."
    if source["balance"] < amount:
        return (
            f"{source['nickname']} 잔액({won(source['balance'])})이 "
            f"이체 금액({won(amount)})보다 적어요."
        )
    return None


def describe_transfer(user_id: str, draft: dict) -> str:
    data = load_data()
    source = _find_account(data, user_id, draft["from_account_id"])
    target = _find_account(data, user_id, draft["to_account_id"])
    amount = draft["amount"]
    return (
        "[즉시이체]\n"
        f"- 출금: {source['nickname']} ({source['account_id']})\n"
        f"- 입금: {target['nickname']} ({target['account_id']})\n"
        f"- 금액: {won(amount)}\n"
        f"- 이체 후 {source['nickname']} 잔액: {won(source['balance'] - amount)}"
    )


def execute_transfer(user_id: str, request_id: str) -> dict:
    """승인된 이체를 실행한다.

    잔액 2건, 거래 2건, 요청 상태를 한 번의 저장으로 반영한다.
    저장에 실패하면 파일은 실행 전 상태(요청은 pending_approval)로 남는다.
    """
    data = load_data()
    request = _find_request(data, request_id)

    # 중복 실행 방지: 승인 대기 상태일 때만 실행
    if request["status"] != "pending_approval":
        return {
            "status": "skipped",
            "message": f"이미 처리된 요청이에요. (상태: {request['status']})",
        }

    draft = request["params"]

    # 실행 직전 재검증
    error = _validate_transfer(data, user_id, draft)
    if error:
        _set_request_result(request, "failed", error)
        try:
            save_data(data)
        except SaveError:
            pass  # 실패 기록을 못 남겨도 데이터는 바뀌지 않았으므로 안내만 한다.
        return {"status": "failed", "message": f"이체하지 못했어요. {error}"}

    source = _find_account(data, user_id, draft["from_account_id"])
    target = _find_account(data, user_id, draft["to_account_id"])
    amount = draft["amount"]
    now = now_kst()

    source["balance"] -= amount
    target["balance"] += amount

    withdrawal_id = _next_id(data["transactions"], "transaction_id", "tx")
    data["transactions"].append(
        _transfer_transaction(withdrawal_id, user_id, source, "withdrawal", amount, now, target)
    )
    deposit_id = _next_id(data["transactions"], "transaction_id", "tx")
    data["transactions"].append(
        _transfer_transaction(deposit_id, user_id, target, "deposit", amount, now, source)
    )

    message = (
        f"{source['nickname']}에서 {with_ro(target['nickname'])} {won(amount)}을 이체했어요. "
        f"{source['nickname']} 잔액은 {won(source['balance'])}이에요."
    )
    _set_request_result(
        request, "completed", message, transaction_ids=[withdrawal_id, deposit_id]
    )

    try:
        save_data(data)
    except SaveError as e:
        return {
            "status": "failed",
            "message": f"이체 내용을 저장하지 못해 이체가 반영되지 않았어요. ({e})",
        }
    return {"status": "completed", "message": message}


def _transfer_transaction(
    transaction_id: str,
    user_id: str,
    account: dict,
    tx_type: str,
    amount: int,
    occurred_at: str,
    counterpart: dict,
) -> dict:
    return {
        "transaction_id": transaction_id,
        "owner_id": user_id,
        "account_id": account["account_id"],
        "type": tx_type,
        "amount": amount,
        "occurred_at": occurred_at,
        "card_id": None,
        "merchant": counterpart["nickname"],  # 이체 당시 상대 계좌 별명
    }


# ---------------------------------------------------------------------------
# 카드
# ---------------------------------------------------------------------------

CARD_STATUS_LABELS = {"active": "사용 가능", "locked": "일시 잠금", "lost": "분실 정지"}
CARD_TYPE_LABELS = {"debit": "체크카드", "credit": "신용카드"}

# 카드 상태 변경 업무: 허용되는 현재 상태 → 바뀔 상태, 거부 사유 (설계 6-3)
CARD_ACTIONS = {
    "report_lost": {
        "label": "카드 분실 정지",
        "allowed": {"active", "locked"},
        "to": "lost",
        "reject": {"lost": "이미 분실 정지된 카드예요."},
        "note": "분실 정지 후에는 잠금 해제로 되돌릴 수 없고, 재발급을 신청해야 해요.",
    },
    "lock_card": {
        "label": "카드 일시 잠금",
        "allowed": {"active"},
        "to": "locked",
        "reject": {
            "locked": "이미 일시 잠금된 카드예요.",
            "lost": "분실 정지된 카드는 이미 사용이 막혀 있어 잠글 수 없어요.",
        },
        "note": "카드를 찾으면 잠금 해제할 수 있어요. 계좌 잔액과 기존 거래는 바뀌지 않아요.",
    },
    "unlock_card": {
        "label": "카드 잠금 해제",
        "allowed": {"locked"},
        "to": "active",
        "reject": {
            "active": "잠겨 있지 않은 카드예요. 이미 사용 가능한 상태예요.",
            "lost": "분실 정지된 카드는 잠금 해제로 다시 사용할 수 없어요. 재발급을 신청해 주세요.",
        },
        "note": None,
    },
}


def _user_cards(data: dict, user_id: str) -> list[dict]:
    return [c for c in data["cards"] if c["owner_id"] == user_id]


def _find_card(data: dict, user_id: str, card_id: str) -> dict | None:
    return next((c for c in _user_cards(data, user_id) if c["card_id"] == card_id), None)


def _normalize_card_name(name: str) -> str:
    """'생활비 카드', '생활비카드', '생활비' 를 같은 이름으로 비교한다."""
    name = re.sub(r"\s+", "", name)
    return re.sub(r"카드$", "", name)


def match_cards(data: dict, user_id: str, name: str) -> tuple[list[dict], bool]:
    """카드 이름(또는 카드 ID)으로 내 카드를 찾는다. 반환 형식은 match_accounts와 같다."""
    cards = _user_cards(data, user_id)
    by_id = [c for c in cards if c["card_id"] == name.strip()]
    if by_id:
        return by_id, True
    key = _normalize_card_name(name)
    exact = [c for c in cards if _normalize_card_name(c["name"]) == key]
    if exact:
        return exact, True
    partial = [c for c in cards if key and key in _normalize_card_name(c["name"])]
    return partial, False


def _card_brief(card: dict) -> dict:
    return {
        "card_id": card["card_id"],
        "name": card["name"],
        "card_type": CARD_TYPE_LABELS.get(card["card_type"], card["card_type"]),
        "status": CARD_STATUS_LABELS.get(card["status"], card["status"]),
    }


def query_cards(user_id: str, slots: dict | None = None) -> dict:
    """카드 이름·ID·종류·상태를 돌려준다. 카드 이름이 있으면 해당 카드만 보여준다."""
    data = load_data()
    cards = _user_cards(data, user_id)
    name = (slots or {}).get("card")
    if name:
        matches, _ = match_cards(data, user_id, name)
        if not matches:
            return {"status": "fail", "error": f"'{name}' 카드를 찾을 수 없어요."}
        cards = matches
    return {"status": "ok", "cards": [_card_brief(c) for c in cards]}


def _resolve_card(data: dict, user_id: str, slots: dict) -> dict:
    """slots의 card_id(선택 완료) 또는 card(이름)를 카드 하나로 확정한다."""
    card_id = slots.get("card_id")
    if card_id:
        card = _find_card(data, user_id, card_id)
        if card is None:
            return {"status": "fail", "error": f"카드({card_id})를 찾을 수 없어요."}
        return {"status": "ok", "card": card}

    name = slots.get("card")
    if not name:
        options = [{"id": c["card_id"], "label": c["name"]} for c in _user_cards(data, user_id)]
        lines = [f"{i}) {o['label']} ({o['id']})" for i, o in enumerate(options, 1)]
        return {
            "status": "ask",
            "question": "어느 카드인가요?\n" + "\n".join(lines),
            "missing": ["card"],
            "candidates": {"slot": "card_id", "options": options},
        }

    matches, exact = match_cards(data, user_id, name)
    if not matches:
        return {"status": "fail", "error": f"'{name}' 카드를 찾을 수 없어요."}
    if exact and len(matches) == 1:
        return {"status": "ok", "card": matches[0]}
    options = [{"id": c["card_id"], "label": c["name"]} for c in matches]
    return _choice_question("대상 카드", name, "카드", exact, "card_id", options)


def _validate_card_action(action: str, card: dict) -> str | None:
    rule = CARD_ACTIONS[action]
    if card["status"] in rule["allowed"]:
        return None
    return rule["reject"].get(card["status"], f"현재 상태({card['status']})에서는 할 수 없어요.")


def _make_card_task(action: str):
    """분실 정지·일시 잠금·잠금 해제는 상태 전이만 다르므로 같은 함수 틀로 만든다."""
    rule = CARD_ACTIONS[action]

    def resolve(user_id: str, slots: dict) -> dict:
        data = load_data()
        result = _resolve_card(data, user_id, slots)
        if result["status"] != "ok":
            return result
        card = result["card"]
        error = _validate_card_action(action, card)
        if error:
            return {"status": "fail", "error": f"{card['name']}: {error}"}
        return {"status": "ok", "draft": {"card_id": card["card_id"], "to_status": rule["to"]}}

    def describe(user_id: str, draft: dict) -> str:
        card = _find_card(load_data(), user_id, draft["card_id"])
        lines = [
            f"[{rule['label']}]",
            f"- 카드: {card['name']} ({card['card_id']}, {CARD_TYPE_LABELS[card['card_type']]})",
            f"- 상태: {CARD_STATUS_LABELS[card['status']]} → {CARD_STATUS_LABELS[rule['to']]}",
        ]
        if rule["note"]:
            lines.append(f"- 안내: {rule['note']}")
        return "\n".join(lines)

    def execute(user_id: str, request_id: str) -> dict:
        data = load_data()
        request = _find_request(data, request_id)
        if request["status"] != "pending_approval":
            return {"status": "skipped", "message": f"이미 처리된 요청이에요. (상태: {request['status']})"}

        card = _find_card(data, user_id, request["params"]["card_id"])
        error = "카드를 찾을 수 없어요." if card is None else _validate_card_action(action, card)
        if error:
            _set_request_result(request, "failed", error)
            try:
                save_data(data)
            except SaveError:
                pass
            return {"status": "failed", "message": f"처리하지 못했어요. {error}"}

        before = card["status"]
        card["status"] = rule["to"]
        message = (
            f"{with_eul(card['name'])} {CARD_STATUS_LABELS[rule['to']]} 상태로 바꿨어요. "
            f"(이전: {CARD_STATUS_LABELS[before]})"
        )
        _set_request_result(request, "completed", message, card_id=card["card_id"], before=before)
        try:
            save_data(data)
        except SaveError as e:
            return {"status": "failed", "message": f"변경 내용을 저장하지 못해 반영되지 않았어요. ({e})"}
        return {"status": "completed", "message": message}

    return resolve, describe, execute


# ---------------------------------------------------------------------------
# 업무 레지스트리
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Task:
    kind: str                           # "query" | "change"
    label: str                          # 사용자에게 보여줄 업무 이름
    slots: tuple[str, ...] = ()         # LLM이 채울 슬롯 이름 (graph의 스키마 필드와 같음)
    query: Callable | None = None
    resolve: Callable | None = None
    describe: Callable | None = None
    execute: Callable | None = None


def _card_task(action: str) -> Task:
    resolve, describe, execute = _make_card_task(action)
    return Task(
        kind="change", label=CARD_ACTIONS[action]["label"], slots=("card",),
        resolve=resolve, describe=describe, execute=execute,
    )


TASKS: dict[str, Task] = {
    "query_accounts": Task(kind="query", label="계좌 조회", query=query_accounts),
    "transfer": Task(
        kind="change",
        label="즉시이체",
        slots=("from_account", "to_account", "amount"),
        resolve=resolve_transfer,
        describe=describe_transfer,
        execute=execute_transfer,
    ),
    "query_cards": Task(kind="query", label="카드 조회", slots=("card",), query=query_cards),
    "report_lost": _card_task("report_lost"),
    "lock_card": _card_task("lock_card"),
    "unlock_card": _card_task("unlock_card"),
}
