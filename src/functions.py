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


def with_ro(word: str) -> str:
    """받침에 맞춰 '로/으로'를 붙인다. (ㄹ 받침은 '로')"""
    code = ord(word[-1]) - 0xAC00
    if not 0 <= code <= 11171:
        return f"{word}(으)로"
    final = code % 28
    return f"{word}{'으로' if final not in (0, 8) else '로'}"


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

    options = [_account_brief(a) for a in matches]
    lines = [f"{i}) {o['nickname']} ({o['account_id']})" for i, o in enumerate(options, 1)]
    reason = "여러 개 있어요" if exact else "정확히 일치하지 않아요"
    return {
        "status": "ask",
        "question": f"{label} '{name}'에 해당하는 계좌가 {reason}. 어느 계좌인가요?\n"
        + "\n".join(lines),
        "missing": [],
        "candidates": {"slot": f"{role}_id", "options": options},
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
# 업무 레지스트리
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Task:
    kind: str                           # "query" | "change"
    label: str                          # 사용자에게 보여줄 업무 이름
    query: Callable | None = None
    resolve: Callable | None = None
    describe: Callable | None = None
    execute: Callable | None = None


TASKS: dict[str, Task] = {
    "query_accounts": Task(kind="query", label="계좌 조회", query=query_accounts),
    "transfer": Task(
        kind="change",
        label="즉시이체",
        resolve=resolve_transfer,
        describe=describe_transfer,
        execute=execute_transfer,
    ),
}
