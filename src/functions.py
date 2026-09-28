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
from datetime import date, datetime, timedelta
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


def with_eun(word: str) -> str:
    """받침에 맞춰 '은/는'을 붙인다."""
    final = _final_consonant(word)
    if final is None:
        return f"{word}은(는)"
    return f"{word}{'는' if final == 0 else '은'}"


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

def create_request(
    user_id: str, task_type: str, params: dict, parent_request_id: str | None = None
) -> str:
    """승인 대기 요청을 기록하고 request_id를 돌려준다. 저장 실패 시 SaveError.

    parent_request_id: 정지 후 재발급처럼 이어진 업무의 앞 단계 요청
    """
    data = load_data()
    now = now_kst()
    request_id = _next_id(data["requests"], "request_id", "req")
    data["requests"].append({
        "request_id": request_id,
        "owner_id": user_id,
        "type": task_type,
        "params": params,
        "status": "pending_approval",
        "parent_request_id": parent_request_id,
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
    transaction_ids = _apply_transfer(data, user_id, source, target, amount, now_kst())

    message = (
        f"{source['nickname']}에서 {with_ro(target['nickname'])} {won(amount)}을 이체했어요. "
        f"{source['nickname']} 잔액은 {won(source['balance'])}이에요."
    )
    _set_request_result(request, "completed", message, transaction_ids=transaction_ids)

    try:
        save_data(data)
    except SaveError as e:
        return {
            "status": "failed",
            "message": f"이체 내용을 저장하지 못해 이체가 반영되지 않았어요. ({e})",
        }
    return {"status": "completed", "message": message}


def _apply_transfer(
    data: dict, user_id: str, source: dict, target: dict, amount: int, now: str
) -> list[str]:
    """메모리의 data에 이체 한 건(잔액 2건, 거래 2건)을 반영하고 거래 ID를 돌려준다. 저장은 호출한 쪽에서."""
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
    return [withdrawal_id, deposit_id]


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
            # already: 이미 원하는 상태라 할 일이 없음 (복합 업무에서 다음 단계로 넘어가는 데 사용)
            return {
                "status": "fail",
                "error": f"{card['name']}: {error}",
                "already": card["status"] == rule["to"],
            }
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
# 카드 재발급 (설계 6-3)
# ---------------------------------------------------------------------------

APPLICATION_STATUS_LABELS = {
    "received": "접수",
    "in_production": "제작 중",
    "shipping": "배송 중",
    "delivered": "배송 완료",
    "cancelled": "취소",
}

# 수정·취소가 불가능한 신청 상태와 사유
APPLICATION_LOCKED_REASONS = {
    "in_production": "카드 제작이 이미 시작되어",
    "shipping": "카드가 이미 배송 중이라",
    "delivered": "카드 배송이 이미 끝나",
    "cancelled": "이미 취소된 신청이라",
}


def _user_addresses(data: dict, user_id: str) -> list[dict]:
    return [a for a in data["addresses"] if a["owner_id"] == user_id]


def _find_address(data: dict, user_id: str, address_id: str) -> dict | None:
    return next((a for a in _user_addresses(data, user_id) if a["address_id"] == address_id), None)


def _resolve_address(data: dict, user_id: str, slots: dict) -> dict:
    """slots의 address_id(선택 완료) 또는 address(집·회사)를 배송지 하나로 확정한다."""
    addresses = _user_addresses(data, user_id)
    if slots.get("address_id"):
        address = _find_address(data, user_id, slots["address_id"])
        if address:
            return {"status": "ok", "address": address}
    label = slots.get("address")
    if label:
        match = next((a for a in addresses if a["label"] == label.strip()), None)
        if match:
            return {"status": "ok", "address": match}

    options = [{"id": a["address_id"], "label": f"{a['label']} - {a['address']}"} for a in addresses]
    lines = [f"{i}) {o['label']}" for i, o in enumerate(options, 1)]
    prefix = f"'{label}'은(는) 등록된 배송지가 아니에요. " if label else ""
    return {
        "status": "ask",
        "question": f"{prefix}재발급 카드를 받을 배송지를 선택해 주세요.\n" + "\n".join(lines),
        "missing": ["address"],
        "candidates": {"slot": "address_id", "options": options},
    }


def _user_applications(data: dict, user_id: str) -> list[dict]:
    return [a for a in data["reissue_applications"] if a["owner_id"] == user_id]


def _open_application(data: dict, card_id: str) -> dict | None:
    """같은 카드의 취소되지 않은 신청."""
    return next(
        (a for a in data["reissue_applications"]
         if a["card_id"] == card_id and a["status"] != "cancelled"),
        None,
    )


def _application_brief(data: dict, application: dict) -> dict:
    card = next((c for c in data["cards"] if c["card_id"] == application["card_id"]), None)
    address = next((a for a in data["addresses"] if a["address_id"] == application["address_id"]), None)
    return {
        "application_id": application["application_id"],
        "card": card["name"] if card else application["card_id"],
        "address": f"{address['label']} ({address['address']})" if address else application["address_id"],
        "status": APPLICATION_STATUS_LABELS.get(application["status"], application["status"]),
        "created_at": application["created_at"][:10],
    }


def _application_option(data: dict, application: dict) -> dict:
    b = _application_brief(data, application)
    return {"id": b["application_id"], "label": f"{b['card']} · {b['address']} · {b['status']} · {b['created_at']}"}


def _resolve_application(data: dict, user_id: str, slots: dict, include_cancelled: bool) -> dict:
    """slots의 application_id(선택 완료), application(신청 ID), card(카드 이름)로 신청 하나를 확정한다.

    반환: {"status": "ok", "application": {...}} / ask(여러 건이면 선택) / fail(기록 없음)
    """
    applications = _user_applications(data, user_id)
    if not include_cancelled:
        applications = [a for a in applications if a["status"] != "cancelled"]

    chosen_id = slots.get("application_id") or slots.get("application")
    if chosen_id:
        match = next((a for a in applications if a["application_id"] == chosen_id.strip()), None)
        if match is None:
            return {"status": "fail", "error": f"재발급 신청({chosen_id}) 기록이 없어요."}
        return {"status": "ok", "application": match}

    card_name = slots.get("card")
    if card_name:
        cards, _ = match_cards(data, user_id, card_name)
        card_ids = {c["card_id"] for c in cards}
        applications = [a for a in applications if a["card_id"] in card_ids]

    if not applications:
        target = f"'{card_name}'의 " if card_name else ""
        return {"status": "fail", "error": f"{target}재발급 신청 기록이 없어요."}
    if len(applications) == 1:
        return {"status": "ok", "application": applications[0]}

    recent_first = sorted(applications, key=lambda a: a["created_at"], reverse=True)
    options = [_application_option(data, a) for a in recent_first]
    lines = [f"{i}) {o['label']} ({o['id']})" for i, o in enumerate(options, 1)]
    return {
        "status": "ask",
        "question": "재발급 신청이 여러 건 있어요. 어느 신청인가요?\n" + "\n".join(lines),
        "missing": [],
        "candidates": {"slot": "application_id", "options": options},
    }


# ---- 재발급 신청 ------------------------------------------------------------

def _validate_reissue(data: dict, card: dict) -> str | None:
    if card["status"] != "lost":
        return (
            f"{with_eun(card['name'])} 분실 정지 상태가 아니라 재발급을 신청할 수 없어요. "
            "먼저 분실 정지를 해 주세요."
        )
    existing = _open_application(data, card["card_id"])
    if existing:
        b = _application_brief(data, existing)
        return (
            f"{with_eun(card['name'])} 이미 재발급 신청이 있어요. "
            f"(신청 {b['application_id']}, 배송지 {b['address']}, 상태 {b['status']})"
        )
    return None


def resolve_reissue(user_id: str, slots: dict) -> dict:
    """대상 카드 → 분실 정지 여부·기존 신청 → 배송지 순서로 검증한다."""
    data = load_data()
    result = _resolve_card(data, user_id, slots)
    if result["status"] != "ok":
        return result
    card = result["card"]

    error = _validate_reissue(data, card)
    if error:
        return {"status": "fail", "error": error}

    result = _resolve_address(data, user_id, slots)
    if result["status"] != "ok":
        return result
    return {
        "status": "ok",
        "draft": {"card_id": card["card_id"], "address_id": result["address"]["address_id"]},
    }


def describe_reissue(user_id: str, draft: dict) -> str:
    data = load_data()
    card = _find_card(data, user_id, draft["card_id"])
    address = _find_address(data, user_id, draft["address_id"])
    return (
        "[카드 재발급 신청]\n"
        f"- 카드: {card['name']} ({card['card_id']})\n"
        f"- 배송지: {address['label']} ({address['address']})\n"
        "- 안내: 신청해도 기존 카드의 분실 정지는 그대로 유지돼요."
    )


def execute_reissue(user_id: str, request_id: str) -> dict:
    data = load_data()
    request = _find_request(data, request_id)
    if request["status"] != "pending_approval":
        return {"status": "skipped", "message": f"이미 처리된 요청이에요. (상태: {request['status']})"}

    draft = request["params"]
    card = _find_card(data, user_id, draft["card_id"])
    address = _find_address(data, user_id, draft["address_id"])
    error = "카드나 배송지를 찾을 수 없어요." if card is None or address is None else _validate_reissue(data, card)
    if error:
        return _fail_and_save(data, request, error)

    now = now_kst()
    application_id = _next_id(data["reissue_applications"], "application_id", "rei")
    data["reissue_applications"].append({
        "application_id": application_id,
        "owner_id": user_id,
        "card_id": card["card_id"],
        "address_id": address["address_id"],
        "status": "received",
        "request_id": request_id,
        "created_at": now,
        "updated_at": now,
    })
    message = (
        f"{card['name']} 재발급을 신청했어요. (신청 {application_id}, 배송지 {address['label']}) "
        "기존 카드는 분실 정지 상태로 유지돼요."
    )
    _set_request_result(request, "completed", message, application_id=application_id)
    return _save_result(data, message)


# ---- 재발급 조회 ------------------------------------------------------------

def query_reissue(user_id: str, slots: dict | None = None) -> dict:
    """신청 하나를 골라 배송지와 처리 상태를 보여준다. 불명확하면 목록에서 선택받는다."""
    data = load_data()
    result = _resolve_application(data, user_id, slots or {}, include_cancelled=True)
    if result["status"] != "ok":
        return result
    return {"status": "ok", "application": _application_brief(data, result["application"])}


# ---- 재발급 수정·취소 -------------------------------------------------------

def _validate_editable(application: dict) -> str | None:
    """접수 상태의 신청만 수정·취소할 수 있다."""
    reason = APPLICATION_LOCKED_REASONS.get(application["status"])
    if reason:
        status = APPLICATION_STATUS_LABELS[application["status"]]
        return f"{reason} 변경하거나 취소할 수 없어요. (현재 상태: {status})"
    return None


def resolve_modify_reissue(user_id: str, slots: dict) -> dict:
    """대상 신청 → 수정 가능 상태 → 새 배송지 → 기존 배송지와 다른지 순서로 검증한다."""
    data = load_data()
    result = _resolve_application(data, user_id, slots, include_cancelled=False)
    if result["status"] != "ok":
        return result
    application = result["application"]

    error = _validate_editable(application)
    if error:
        return {"status": "fail", "error": error}

    result = _resolve_address(data, user_id, slots)
    if result["status"] != "ok":
        return result
    address = result["address"]
    if address["address_id"] == application["address_id"]:
        return {"status": "fail", "error": f"이미 배송지가 {with_ro(address['label'])} 되어 있어요."}
    return {
        "status": "ok",
        "draft": {"application_id": application["application_id"], "address_id": address["address_id"]},
    }


def describe_modify_reissue(user_id: str, draft: dict) -> str:
    data = load_data()
    application = _find_application(data, draft["application_id"])
    b = _application_brief(data, application)
    new = _find_address(data, user_id, draft["address_id"])
    return (
        "[재발급 배송지 변경]\n"
        f"- 신청: {b['application_id']} ({b['card']}, {b['status']})\n"
        f"- 배송지: {b['address']} → {new['label']} ({new['address']})"
    )


def execute_modify_reissue(user_id: str, request_id: str) -> dict:
    data = load_data()
    request = _find_request(data, request_id)
    if request["status"] != "pending_approval":
        return {"status": "skipped", "message": f"이미 처리된 요청이에요. (상태: {request['status']})"}

    draft = request["params"]
    application = _find_application(data, draft["application_id"])
    address = _find_address(data, user_id, draft["address_id"])
    error = "신청이나 배송지를 찾을 수 없어요." if application is None or address is None else _validate_editable(application)
    if error:
        return _fail_and_save(data, request, error)

    before = application["address_id"]
    application["address_id"] = address["address_id"]
    application["updated_at"] = now_kst()
    message = f"재발급 신청 {application['application_id']}의 배송지를 {with_ro(address['label'])} 바꿨어요."
    _set_request_result(request, "completed", message, before_address_id=before)
    return _save_result(data, message)


def resolve_cancel_reissue(user_id: str, slots: dict) -> dict:
    data = load_data()
    result = _resolve_application(data, user_id, slots, include_cancelled=False)
    if result["status"] != "ok":
        return result
    application = result["application"]
    error = _validate_editable(application)
    if error:
        return {"status": "fail", "error": error}
    return {"status": "ok", "draft": {"application_id": application["application_id"]}}


def describe_cancel_reissue(user_id: str, draft: dict) -> str:
    data = load_data()
    b = _application_brief(data, _find_application(data, draft["application_id"]))
    return (
        "[재발급 신청 취소]\n"
        f"- 신청: {b['application_id']} ({b['card']}, 배송지 {b['address']}, {b['status']})\n"
        "- 안내: 취소해도 기존 카드의 분실 정지는 유지돼요."
    )


def execute_cancel_reissue(user_id: str, request_id: str) -> dict:
    data = load_data()
    request = _find_request(data, request_id)
    if request["status"] != "pending_approval":
        return {"status": "skipped", "message": f"이미 처리된 요청이에요. (상태: {request['status']})"}

    application = _find_application(data, request["params"]["application_id"])
    error = "신청을 찾을 수 없어요." if application is None else _validate_editable(application)
    if error:
        return _fail_and_save(data, request, error)

    application["status"] = "cancelled"
    application["updated_at"] = now_kst()
    message = f"재발급 신청 {with_eul(application['application_id'])} 취소했어요. 기존 카드의 분실 정지는 유지돼요."
    _set_request_result(request, "completed", message)
    return _save_result(data, message)


def _find_application(data: dict, application_id: str) -> dict | None:
    return next(
        (a for a in data["reissue_applications"] if a["application_id"] == application_id), None
    )


# ---- 실행 공통 ---------------------------------------------------------------

def _fail_and_save(data: dict, request: dict, error: str) -> dict:
    """실행 직전 재검증 실패: 요청만 failed로 기록한다. 데이터는 바꾸지 않는다."""
    _set_request_result(request, "failed", error)
    try:
        save_data(data)
    except SaveError:
        pass
    return {"status": "failed", "message": f"처리하지 못했어요. {error}"}


def _save_result(data: dict, message: str) -> dict:
    """변경 내용과 요청 기록을 한 번에 저장한다. 실패하면 파일은 실행 전 상태로 남는다."""
    try:
        save_data(data)
    except SaveError as e:
        return {"status": "failed", "message": f"변경 내용을 저장하지 못해 반영되지 않았어요. ({e})"}
    return {"status": "completed", "message": message}


# ---------------------------------------------------------------------------
# 청구서·납부 (설계 6-4)
# ---------------------------------------------------------------------------

BILL_ITEM_LABELS = {"completed": "완료", "failed": "실패(미납 유지)", "unprocessed": "미처리(미납 유지)"}


def _user_bills(data: dict, user_id: str) -> list[dict]:
    return [b for b in data["bills"] if b["owner_id"] == user_id]


def _find_bill(data: dict, user_id: str, bill_id: str) -> dict | None:
    return next((b for b in _user_bills(data, user_id) if b["bill_id"] == bill_id), None)


def _by_due(bills: list[dict]) -> list[dict]:
    return sorted(bills, key=lambda b: (b["due_date"], b["bill_id"]))


def _due_note(bill: dict) -> str:
    """납기일 표시. 기한이 지났으면 연체료 없이 납부할 수 있다고 덧붙인다."""
    if bill["due_date"] < today_kst():
        return f"{bill['due_date']}, 기한 지남·연체료 없음"
    return bill["due_date"]


def match_bills(data: dict, user_id: str, name: str) -> list[dict]:
    """청구서 이름(또는 ID)으로 내 청구서를 찾는다. 정확히 일치하면 그것만, 아니면 부분 일치."""
    bills = _user_bills(data, user_id)
    by_id = [b for b in bills if b["bill_id"] == name.strip()]
    if by_id:
        return by_id
    key = re.sub(r"\s+", "", name)
    exact = [b for b in bills if re.sub(r"\s+", "", b["name"]) == key]
    return exact or [b for b in bills if key and key in re.sub(r"\s+", "", b["name"])]


def _bill_brief(bill: dict) -> dict:
    return {
        "bill_id": bill["bill_id"],
        "name": bill["name"],
        "amount": bill["amount"],
        "due": _due_note(bill),
    }


def query_bills(user_id: str, slots: dict | None = None) -> dict:
    """미납 청구서를 납기일순으로 돌려준다."""
    data = load_data()
    unpaid = _by_due([b for b in _user_bills(data, user_id) if b["status"] == "unpaid"])
    return {
        "status": "ok",
        "bills": [_bill_brief(b) for b in unpaid],
        "total": sum(b["amount"] for b in unpaid),
    }


def _bill_payment(data: dict, user_id: str, account: dict, bill: dict, now: str) -> str:
    """청구서 한 건을 메모리의 data에 반영하고 거래 ID를 돌려준다. (저장은 호출한 쪽에서)"""
    account["balance"] -= bill["amount"]
    transaction_id = _next_id(data["transactions"], "transaction_id", "tx")
    data["transactions"].append({
        "transaction_id": transaction_id,
        "owner_id": user_id,
        "account_id": account["account_id"],
        "type": "withdrawal",
        "amount": bill["amount"],
        "occurred_at": now,
        "card_id": None,
        "merchant": bill["name"],
    })
    bill.update({
        "status": "paid",
        "paid_at": now,
        "paid_account_id": account["account_id"],
        "transaction_id": transaction_id,
    })
    return transaction_id


def _bill_unpayable_reason(bill: dict | None, account: dict | None) -> str | None:
    if bill is None:
        return "청구서를 찾을 수 없어요."
    if bill["status"] != "unpaid":
        return f"{with_eun(bill['name'])} 이미 납부한 청구서예요."
    if account is None:
        return "출금 계좌를 찾을 수 없어요."
    if account["balance"] < bill["amount"]:
        return (
            f"{account['nickname']} 잔액({won(account['balance'])})이 "
            f"{bill['name']} 금액({won(bill['amount'])})보다 적어요."
        )
    return None


# ---- 청구서 한 건 납부 --------------------------------------------------------

def resolve_pay_bill(user_id: str, slots: dict) -> dict:
    """청구서 → 출금 계좌 → 미납 여부 → 잔액 순서로 검증한다."""
    data = load_data()
    unpaid = _by_due([b for b in _user_bills(data, user_id) if b["status"] == "unpaid"])

    # 청구서 확정
    bill = _find_bill(data, user_id, slots["bill_id"]) if slots.get("bill_id") else None
    if bill is None:
        name = slots.get("bill")
        matches = match_bills(data, user_id, name) if name else []
        if name and not matches:
            return {"status": "fail", "error": f"'{name}' 청구서를 찾을 수 없어요."}
        if len(matches) == 1:
            bill = matches[0]
        else:
            options = [
                {"id": b["bill_id"], "label": f"{b['name']} {won(b['amount'])} (납기 {b['due_date']})"}
                for b in (_by_due(matches) if matches else unpaid)
            ]
            if not options:
                return {"status": "fail", "error": "납부할 미납 청구서가 없어요."}
            lines = [f"{i}) {o['label']}" for i, o in enumerate(options, 1)]
            return {
                "status": "ask",
                "question": "어떤 청구서를 납부할까요?\n" + "\n".join(lines),
                "missing": [] if name else ["bill"],
                "candidates": {"slot": "bill_id", "options": options},
            }

    if bill["status"] != "unpaid":
        return {"status": "fail", "error": f"{with_eun(bill['name'])} 이미 납부한 청구서예요."}

    # 출금 계좌 확정
    if not slots.get("from_account") and not slots.get("from_account_id"):
        return {
            "status": "ask",
            "question": f"{bill['name']} {won(bill['amount'])}을 어느 계좌에서 낼까요?\n"
            + "\n".join(f"- {a['nickname']} (잔액 {won(a['balance'])})" for a in _user_accounts(data, user_id)),
            "missing": ["from_account"],
            "candidates": None,
        }
    result = _resolve_account_slot(data, user_id, slots, "from_account", "출금 계좌")
    if result["status"] != "ok":
        return result

    error = _bill_unpayable_reason(bill, result["account"])
    if error:
        return {"status": "fail", "error": error}
    return {
        "status": "ok",
        "draft": {"from_account_id": result["account"]["account_id"], "bill_id": bill["bill_id"]},
    }


def describe_pay_bill(user_id: str, draft: dict) -> str:
    data = load_data()
    account = _find_account(data, user_id, draft["from_account_id"])
    bill = _find_bill(data, user_id, draft["bill_id"])
    return (
        "[청구서 납부]\n"
        f"- 청구서: {bill['name']} ({bill['bill_id']}, 납기 {_due_note(bill)})\n"
        f"- 금액: {won(bill['amount'])} (전액 납부)\n"
        f"- 출금: {account['nickname']} ({account['account_id']})\n"
        f"- 납부 후 {account['nickname']} 잔액: {won(account['balance'] - bill['amount'])}"
    )


def execute_pay_bill(user_id: str, request_id: str) -> dict:
    """잔액·출금 거래·청구서 상태·요청 기록을 한 번에 저장한다."""
    data = load_data()
    request = _find_request(data, request_id)
    if request["status"] != "pending_approval":
        return {"status": "skipped", "message": f"이미 처리된 요청이에요. (상태: {request['status']})"}

    draft = request["params"]
    account = _find_account(data, user_id, draft["from_account_id"])
    bill = _find_bill(data, user_id, draft["bill_id"])
    error = _bill_unpayable_reason(bill, account)
    if error:
        return _fail_and_save(data, request, error)

    transaction_id = _bill_payment(data, user_id, account, bill, now_kst())
    message = (
        f"{bill['name']} {won(bill['amount'])}을 {account['nickname']}에서 납부했어요. "
        f"{account['nickname']} 잔액은 {won(account['balance'])}이에요."
    )
    _set_request_result(request, "completed", message, transaction_ids=[transaction_id])
    return _save_result(data, message)


# ---- 일괄 납부 ----------------------------------------------------------------

def resolve_pay_bills_batch(user_id: str, slots: dict) -> dict:
    """출금 계좌 → 대상 청구서(이름 목록 또는 전부) → 미납 1건 이상 순서로 검증한다.

    총액이 잔액보다 커도 승인은 받는다. 잔액이 부족한 건은 실행 중에 미납으로 남긴다.
    """
    data = load_data()
    if not slots.get("from_account") and not slots.get("from_account_id"):
        return {"status": "ask", "question": "어느 계좌에서 납부할까요?", "missing": ["from_account"], "candidates": None}
    result = _resolve_account_slot(data, user_id, slots, "from_account", "출금 계좌")
    if result["status"] != "ok":
        return result
    account = result["account"]

    unpaid_all = [b for b in _user_bills(data, user_id) if b["status"] == "unpaid"]
    names = slots.get("bills") or []
    if slots.get("all_bills"):
        selected = unpaid_all
    elif names:
        selected = []
        for name in names:
            matches = match_bills(data, user_id, name)
            if not matches:
                return {"status": "fail", "error": f"'{name}' 청구서를 찾을 수 없어요."}
            if len(matches) > 1:
                labels = ", ".join(b["name"] for b in matches)
                return {"status": "fail", "error": f"'{name}'에 해당하는 청구서가 여러 개예요. ({labels}) 이름을 정확히 알려주세요."}
            selected.append(matches[0])
    else:
        lines = [f"- {b['name']} {won(b['amount'])} (납기 {b['due_date']})" for b in _by_due(unpaid_all)]
        return {
            "status": "ask",
            "question": "어떤 청구서를 납부할까요? '전부' 또는 청구서 이름을 알려주세요.\n" + "\n".join(lines),
            "missing": ["bills"],
            "candidates": None,
        }

    # 이미 납부한 청구서는 다시 처리하지 않는다.
    selected = _by_due({b["bill_id"]: b for b in selected if b["status"] == "unpaid"}.values())
    if not selected:
        return {"status": "fail", "error": "선택한 청구서는 모두 이미 납부했어요. 납부할 미납 청구서가 없어요."}
    return {
        "status": "ok",
        "draft": {"from_account_id": account["account_id"], "bill_ids": [b["bill_id"] for b in selected]},
    }


def describe_pay_bills_batch(user_id: str, draft: dict) -> str:
    data = load_data()
    account = _find_account(data, user_id, draft["from_account_id"])
    bills = [_find_bill(data, user_id, bid) for bid in draft["bill_ids"]]
    total = sum(b["amount"] for b in bills)
    lines = [f"  {i}. {b['name']} {won(b['amount'])} (납기 {_due_note(b)})" for i, b in enumerate(bills, 1)]
    text = (
        "[청구서 일괄 납부]\n"
        f"- 출금: {account['nickname']} ({account['account_id']}, 잔액 {won(account['balance'])})\n"
        "- 납부 순서 (납기일 빠른 순):\n" + "\n".join(lines) + "\n"
        f"- 총액: {won(total)} ({len(bills)}건)"
    )
    if total > account["balance"]:
        text += "\n- 안내: 잔액이 총액보다 적어요. 순서대로 납부하다 잔액이 부족한 건은 미납으로 남겨요."
    return text


def execute_pay_bills_batch(user_id: str, request_id: str) -> dict:
    """납기일이 빠른 청구서부터 건별로 처리·저장한다.

    - 잔액 부족·이미 납부: 실패로 기록하고 다음 건으로 진행
    - 저장 실패: 해당 건은 미반영(실패), 남은 건은 미처리로 두고 중단. 이미 저장된 납부는 유지
    - 건별 저장에 요청의 진행 기록(result.items)을 함께 저장한다. 요청 상태는 끝날 때 갱신한다.
    """
    data = load_data()
    request = _find_request(data, request_id)
    if request["status"] != "pending_approval":
        return {"status": "skipped", "message": f"이미 처리된 요청이에요. (상태: {request['status']})"}

    draft = request["params"]
    # 재시작 후 다시 승인받아 실행하는 경우, 이전에 저장된 완료 건은 결과에 그대로 남긴다.
    items = [i for i in ((request.get("result") or {}).get("items") or []) if i["status"] == "completed"]
    bill_ids = _by_due_ids(data, user_id, draft["bill_ids"])

    stopped = False
    for index, bill_id in enumerate(bill_ids):
        bill = _find_bill(data, user_id, bill_id)
        account = _find_account(data, user_id, draft["from_account_id"])
        reason = _bill_unpayable_reason(bill, account)
        if reason:
            items.append(_bill_item(bill, bill_id, "failed", reason))
            continue

        transaction_id = _bill_payment(data, user_id, account, bill, now_kst())
        item = _bill_item(bill, bill_id, "completed", None, transaction_id)
        request["result"] = {"message": "일괄 납부 진행 중", "items": [*items, item]}
        request["updated_at"] = now_kst()
        try:
            save_data(data)
        except SaveError as e:
            # 저장하지 못한 건은 반영하지 않는다. 파일 기준으로 다시 읽어 메모리 변경을 버린다.
            data = load_data()
            request = _find_request(data, request_id)
            items.append(_bill_item(bill, bill_id, "failed", f"저장 실패로 반영되지 않음 ({e})"))
            for rest_id in bill_ids[index + 1:]:
                items.append(_bill_item(_find_bill(data, user_id, rest_id), rest_id, "unprocessed", "저장 실패로 처리 중단"))
            stopped = True
            break
        items.append(item)

    completed = [i for i in items if i["status"] == "completed"]
    status = "completed" if len(completed) == len(items) else ("partial" if completed else "failed")
    account = _find_account(data, user_id, draft["from_account_id"])
    message = _batch_message(items, account, stopped)
    _set_request_result(request, status, message, items=items)
    try:
        save_data(data)
    except SaveError:
        # 최종 상태를 못 남겨도 건별 납부는 이미 저장되어 있다. 요청은 pending_approval로 남아
        # 재시작 시 남은 청구서만 다시 승인받는다.
        message += "\n(처리 결과 기록을 저장하지 못했어요.)"
    return {"status": status if status != "partial" else "completed", "message": message, "items": items}


def _by_due_ids(data: dict, user_id: str, bill_ids: list[str]) -> list[str]:
    bills = [b for b in (_find_bill(data, user_id, bid) for bid in bill_ids) if b]
    ordered = [b["bill_id"] for b in _by_due(bills)]
    return ordered + [bid for bid in bill_ids if bid not in ordered]


def _bill_item(bill: dict | None, bill_id: str, status: str, reason: str | None, transaction_id: str | None = None) -> dict:
    return {
        "bill_id": bill_id,
        "name": bill["name"] if bill else bill_id,
        "amount": bill["amount"] if bill else 0,
        "status": status,
        "reason": reason,
        "transaction_id": transaction_id,
    }


def _batch_message(items: list[dict], account: dict, stopped: bool) -> str:
    lines = ["청구서 일괄 납부 결과예요."]
    for status, label in BILL_ITEM_LABELS.items():
        group = [i for i in items if i["status"] == status]
        if not group:
            continue
        total = sum(i["amount"] for i in group)
        lines.append(f"- {label} {len(group)}건 ({won(total)})")
        for i in group:
            lines.append(f"  · {i['name']} {won(i['amount'])}" + (f": {i['reason']}" if i["reason"] and status != "completed" else ""))
    if stopped:
        lines.append("저장 오류로 처리를 중단했어요. 이미 납부한 건은 그대로 유지돼요.")
    lines.append(f"{account['nickname']} 잔액: {won(account['balance'])}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 거래 내역 조회 (설계 6-2)
# ---------------------------------------------------------------------------

PERIOD_LABELS = {
    "today": "오늘", "this_week": "이번 주", "last_week": "지난주",
    "this_month": "이번 달", "last_month": "지난달", "custom": "지정 기간", "all": "전체 기간",
}
TX_TYPE_LABELS = {"deposit": "입금", "withdrawal": "출금", "card_payment": "카드 결제"}


def period_range(period: str | None, today: date, start: str | None = None, end: str | None = None) -> tuple[date | None, date | None]:
    """기간 이름을 (시작일, 종료일)로 바꾼다. 둘 다 포함하며 None이면 제한 없음.

    이번 주는 월요일~일요일, 이번 달은 기준일이 속한 달이다.
    """
    if period == "today":
        return today, today
    if period in ("this_week", "last_week"):
        monday = today - timedelta(days=today.weekday())
        if period == "last_week":
            monday -= timedelta(days=7)
        return monday, monday + timedelta(days=6)
    if period in ("this_month", "last_month"):
        first = today.replace(day=1)
        if period == "last_month":
            first = (first - timedelta(days=1)).replace(day=1)
        next_first = (first + timedelta(days=32)).replace(day=1)
        return first, next_first - timedelta(days=1)
    if period == "custom":
        return (date.fromisoformat(start) if start else None, date.fromisoformat(end) if end else None)
    return None, None


def query_transactions(user_id: str, slots: dict | None = None) -> dict:
    """기간·금액·유형·계좌로 거래를 걸러 최근순으로 돌려준다. 카드 결제에는 카드 이름을 붙인다."""
    slots = slots or {}
    data = load_data()

    # 계좌 (선택)
    account = None
    if slots.get("account") or slots.get("account_id"):
        result = _resolve_account_slot(data, user_id, slots, "account", "조회할 계좌")
        if result["status"] != "ok":
            return result
        account = result["account"]

    # 기간
    period = slots.get("period") or "all"
    try:
        start, end = period_range(period, date.fromisoformat(today_kst()), slots.get("start_date"), slots.get("end_date"))
    except ValueError:
        return {"status": "fail", "error": "날짜는 YYYY-MM-DD 형식이어야 해요."}
    if start and end and start > end:
        return {"status": "fail", "error": f"시작일({start})이 종료일({end})보다 늦어요."}

    # 금액
    min_amount, max_amount = slots.get("min_amount"), slots.get("max_amount")
    for value in (min_amount, max_amount):
        if value is not None and value < 0:
            return {"status": "fail", "error": "금액 조건은 0원 이상이어야 해요."}
    if min_amount is not None and max_amount is not None and min_amount > max_amount:
        return {"status": "fail", "error": "최소 금액이 최대 금액보다 커요."}

    tx_type = slots.get("tx_type")
    cards = {c["card_id"]: c["name"] for c in data["cards"]}
    accounts = {a["account_id"]: a["nickname"] for a in data["accounts"]}

    rows = []
    for t in data["transactions"]:
        if t["owner_id"] != user_id:
            continue
        if account and t["account_id"] != account["account_id"]:
            continue
        day = date.fromisoformat(t["occurred_at"][:10])
        if (start and day < start) or (end and day > end):
            continue
        if (min_amount is not None and t["amount"] < min_amount) or (max_amount is not None and t["amount"] > max_amount):
            continue
        is_card_payment = t["type"] == "withdrawal" and t["card_id"] is not None
        if tx_type == "card_payment" and not is_card_payment:
            continue
        if tx_type in ("deposit", "withdrawal") and t["type"] != tx_type:
            continue
        rows.append({
            "transaction_id": t["transaction_id"],
            "occurred_at": t["occurred_at"][:16].replace("T", " "),
            "account": accounts.get(t["account_id"], t["account_id"]),
            "type": "카드 결제" if is_card_payment else TX_TYPE_LABELS[t["type"]],
            "amount": t["amount"],
            "merchant": t["merchant"],
            "card": cards.get(t["card_id"]) if t["card_id"] else None,
            "sign": 1 if t["type"] == "deposit" else -1,
        })
    rows.sort(key=lambda r: (r["occurred_at"], r["transaction_id"]), reverse=True)

    filters = [PERIOD_LABELS.get(period, period)]
    if start or end:
        filters[0] += f" ({start or '처음'} ~ {end or '현재'})"
    if account:
        filters.append(f"{account['nickname']} 계좌")
    if tx_type:
        filters.append(TX_TYPE_LABELS[tx_type])
    if min_amount is not None:
        filters.append(f"{won(min_amount)} 이상")
    if max_amount is not None:
        filters.append(f"{won(max_amount)} 이하")

    return {
        "status": "ok",
        "filters": ", ".join(filters),
        "transactions": rows,
        "deposit_total": sum(r["amount"] for r in rows if r["sign"] > 0),
        "withdrawal_total": sum(r["amount"] for r in rows if r["sign"] < 0),
    }


# ---------------------------------------------------------------------------
# 조건부 이체 (설계 6-2)
# ---------------------------------------------------------------------------

CONDITIONAL_SLOTS = {"from_account": "출금 계좌", "to_account": "입금 계좌", "keep_amount": "출금 계좌에 남길 금액"}


def _resolve_two_accounts(data: dict, user_id: str, slots: dict, labels: dict) -> dict:
    """누락 확인 후 출금·입금 계좌를 확정한다. 즉시이체·조건부 이체 공통."""
    missing = [s for s in labels if slots.get(s) is None and slots.get(f"{s}_id") is None]
    if missing:
        return {
            "status": "ask",
            "question": "다음 정보를 알려주세요: " + ", ".join(labels[s] for s in missing),
            "missing": missing,
            "candidates": None,
        }
    resolved = {}
    for role in ("from_account", "to_account"):
        result = _resolve_account_slot(data, user_id, slots, role, labels[role])
        if result["status"] != "ok":
            return result
        resolved[role] = result["account"]
    return {"status": "ok", **resolved}


def _conditional_amount(source: dict, keep_amount: int) -> int:
    return source["balance"] - keep_amount


def _validate_conditional(data: dict, user_id: str, draft: dict) -> str | None:
    keep = draft["keep_amount"]
    if not isinstance(keep, int) or isinstance(keep, bool) or keep < 0:
        return "남길 금액은 0원 이상의 정수여야 해요."
    source = _find_account(data, user_id, draft["from_account_id"])
    target = _find_account(data, user_id, draft["to_account_id"])
    if source is None or target is None:
        return "이체할 계좌를 찾을 수 없어요."
    if source["account_id"] == target["account_id"]:
        return "같은 계좌로는 이체할 수 없어요."
    amount = _conditional_amount(source, keep)
    if amount <= 0:
        return (
            f"{source['nickname']} 잔액({won(source['balance'])})이 남길 금액({won(keep)}) 이하라 "
            "이체할 금액이 없어요."
        )
    return None


def resolve_conditional_transfer(user_id: str, slots: dict) -> dict:
    """누락 → 계좌 매칭 → 남길 금액 ≥ 0 → 같은 계좌 → 이체액 > 0 순서로 검증한다."""
    data = load_data()
    result = _resolve_two_accounts(data, user_id, slots, CONDITIONAL_SLOTS)
    if result["status"] != "ok":
        return result
    draft = {
        "from_account_id": result["from_account"]["account_id"],
        "to_account_id": result["to_account"]["account_id"],
        "keep_amount": slots["keep_amount"],
    }
    error = _validate_conditional(data, user_id, draft)
    if error:
        return {"status": "fail", "error": error}
    draft["amount"] = _conditional_amount(result["from_account"], draft["keep_amount"])
    return {"status": "ok", "draft": draft}


def describe_conditional_transfer(user_id: str, draft: dict) -> str:
    data = load_data()
    source = _find_account(data, user_id, draft["from_account_id"])
    target = _find_account(data, user_id, draft["to_account_id"])
    return (
        "[조건부 이체]\n"
        f"- 출금: {source['nickname']} ({source['account_id']}, 현재 잔액 {won(source['balance'])})\n"
        f"- 입금: {target['nickname']} ({target['account_id']})\n"
        f"- 남길 금액: {won(draft['keep_amount'])}\n"
        f"- 이체할 금액: {won(draft['amount'])} (현재 잔액 − 남길 금액)\n"
        "- 안내: 실행 직전 잔액이 바뀌어 이체할 금액이 달라지면 다시 확인받아요."
    )


def execute_conditional_transfer(user_id: str, request_id: str) -> dict:
    """실행 직전 이체액을 다시 계산한다. 승인한 금액과 다르면 실행하지 않고 재승인을 요청한다."""
    data = load_data()
    request = _find_request(data, request_id)
    if request["status"] != "pending_approval":
        return {"status": "skipped", "message": f"이미 처리된 요청이에요. (상태: {request['status']})"}

    draft = request["params"]
    error = _validate_conditional(data, user_id, draft)
    if error:
        return _fail_and_save(data, request, error)

    source = _find_account(data, user_id, draft["from_account_id"])
    target = _find_account(data, user_id, draft["to_account_id"])
    amount = _conditional_amount(source, draft["keep_amount"])
    if amount != draft["amount"]:
        return {
            "status": "changed",
            "draft": {**draft, "amount": amount},
            "message": (
                f"승인 후 {source['nickname']} 잔액이 바뀌어 이체할 금액이 "
                f"{won(draft['amount'])}에서 {won(amount)}으로 달라졌어요. 다시 확인해 주세요."
            ),
        }

    transaction_ids = _apply_transfer(data, user_id, source, target, amount, now_kst())
    message = (
        f"{source['nickname']}에 {won(draft['keep_amount'])}을 남기고 "
        f"{with_ro(target['nickname'])} {won(amount)}을 이체했어요."
    )
    _set_request_result(request, "completed", message, transaction_ids=transaction_ids)
    return _save_result(data, message)


# ---------------------------------------------------------------------------
# 여러 계좌로 나눠 이체 (설계 6-2)
# ---------------------------------------------------------------------------

def _validate_multi_transfer(data: dict, user_id: str, draft: dict) -> str | None:
    source = _find_account(data, user_id, draft["from_account_id"])
    if source is None:
        return "출금 계좌를 찾을 수 없어요."
    seen = set()
    for item in draft["items"]:
        target = _find_account(data, user_id, item["to_account_id"])
        if target is None:
            return "입금 계좌를 찾을 수 없어요."
        amount = item["amount"]
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            return f"{with_ro(target['nickname'])} 보낼 금액은 1원 이상의 정수여야 해요."
        if target["account_id"] == source["account_id"]:
            return "출금 계좌로는 이체할 수 없어요."
        if target["account_id"] in seen:
            return f"{target['nickname']} 계좌가 두 번 들어 있어요. 입금 계좌는 한 번씩만 지정해 주세요."
        seen.add(target["account_id"])
    total = sum(i["amount"] for i in draft["items"])
    if source["balance"] < total:
        return f"{source['nickname']} 잔액({won(source['balance'])})이 이체 총액({won(total)})보다 적어요."
    return None


def resolve_multi_transfer(user_id: str, slots: dict) -> dict:
    """출금 계좌 → 입금 목록 → 각 입금 계좌 매칭 → 금액·중복·잔액 순서로 검증한다."""
    data = load_data()
    if not slots.get("from_account") and not slots.get("from_account_id"):
        return {"status": "ask", "question": "어느 계좌에서 보낼까요?", "missing": ["from_account"], "candidates": None}
    result = _resolve_account_slot(data, user_id, slots, "from_account", "출금 계좌")
    if result["status"] != "ok":
        return result
    source = result["account"]

    transfers = slots.get("transfers") or []
    if not transfers:
        return {
            "status": "ask",
            "question": "어느 계좌로 얼마씩 보낼까요? (예: 저축 20만 원, 여행 자금 10만 원)",
            "missing": ["transfers"],
            "candidates": None,
        }

    items = []
    for t in transfers:
        name, amount = t.get("to_account"), t.get("amount")
        if not name or amount is None:
            return {"status": "ask", "question": "입금 계좌와 금액을 모두 알려주세요.", "missing": ["transfers"], "candidates": None}
        matches, _ = match_accounts(data, user_id, name)
        if not matches:
            return {"status": "fail", "error": f"'{name}' 계좌를 찾을 수 없어요."}
        if len(matches) > 1:
            labels = ", ".join(a["nickname"] for a in matches)
            return {"status": "fail", "error": f"'{name}'에 해당하는 계좌가 여러 개예요. ({labels}) 이름을 정확히 알려주세요."}
        items.append({"to_account_id": matches[0]["account_id"], "amount": amount})

    draft = {"from_account_id": source["account_id"], "items": items}
    error = _validate_multi_transfer(data, user_id, draft)
    if error:
        return {"status": "fail", "error": error}
    return {"status": "ok", "draft": draft}


def describe_multi_transfer(user_id: str, draft: dict) -> str:
    data = load_data()
    source = _find_account(data, user_id, draft["from_account_id"])
    total = sum(i["amount"] for i in draft["items"])
    lines = [
        f"  {n}. {_find_account(data, user_id, i['to_account_id'])['nickname']}: {won(i['amount'])}"
        for n, i in enumerate(draft["items"], 1)
    ]
    return (
        "[여러 계좌로 나눠 이체]\n"
        f"- 출금: {source['nickname']} ({source['account_id']})\n"
        "- 입금:\n" + "\n".join(lines) + "\n"
        f"- 총액: {won(total)}\n"
        f"- 이체 후 {source['nickname']} 잔액: {won(source['balance'] - total)}\n"
        "- 안내: 모든 이체를 한 번에 처리해요. 하나라도 실패하면 전체를 반영하지 않아요."
    )


def execute_multi_transfer(user_id: str, request_id: str) -> dict:
    """모든 이체의 잔액·거래·요청 상태를 한 번에 저장한다. 일부만 반영하지 않는다."""
    data = load_data()
    request = _find_request(data, request_id)
    if request["status"] != "pending_approval":
        return {"status": "skipped", "message": f"이미 처리된 요청이에요. (상태: {request['status']})"}

    draft = request["params"]
    error = _validate_multi_transfer(data, user_id, draft)
    if error:
        return _fail_and_save(data, request, error)

    source = _find_account(data, user_id, draft["from_account_id"])
    now = now_kst()
    transaction_ids, parts = [], []
    for item in draft["items"]:
        target = _find_account(data, user_id, item["to_account_id"])
        transaction_ids += _apply_transfer(data, user_id, source, target, item["amount"], now)
        parts.append(f"{target['nickname']} {won(item['amount'])}")
    total = sum(i["amount"] for i in draft["items"])
    message = (
        f"{source['nickname']}에서 {', '.join(parts)}을 이체했어요. (총 {won(total)}) "
        f"{source['nickname']} 잔액은 {won(source['balance'])}이에요."
    )
    _set_request_result(request, "completed", message, transaction_ids=transaction_ids)
    try:
        save_data(data)
    except SaveError as e:
        return {"status": "failed", "message": f"이체 내용을 저장하지 못해 모든 이체가 반영되지 않았어요. ({e})"}
    return {"status": "completed", "message": message}


# ---------------------------------------------------------------------------
# 계좌 별명 변경 (설계 6-2)
# ---------------------------------------------------------------------------

NICKNAME_MAX = 20


def _validate_nickname(data: dict, user_id: str, account: dict, nickname: str) -> str | None:
    if not 1 <= len(nickname) <= NICKNAME_MAX:
        return f"별명은 앞뒤 공백을 빼고 1~{NICKNAME_MAX}자여야 해요. (입력: {len(nickname)}자)"
    if nickname == account["nickname"]:
        return f"이미 별명이 '{nickname}'이에요."
    key = _normalize_name(nickname)
    for other in _user_accounts(data, user_id):
        if other["account_id"] != account["account_id"] and _normalize_name(other["nickname"]) == key:
            return f"'{other['nickname']}' 계좌와 별명이 겹쳐요. 다른 이름을 정해 주세요."
    return None


def resolve_rename_account(user_id: str, slots: dict) -> dict:
    """대상 계좌 → 새 별명 누락 → 길이 → 현재와 같음 → 다른 계좌와 중복 순서로 검증한다."""
    data = load_data()
    if not slots.get("account") and not slots.get("account_id"):
        options = [{"id": a["account_id"], "label": a["nickname"]} for a in _user_accounts(data, user_id)]
        lines = [f"{i}) {o['label']} ({o['id']})" for i, o in enumerate(options, 1)]
        return {
            "status": "ask",
            "question": "어느 계좌의 별명을 바꿀까요?\n" + "\n".join(lines),
            "missing": ["account"],
            "candidates": {"slot": "account_id", "options": options},
        }
    result = _resolve_account_slot(data, user_id, slots, "account", "대상 계좌")
    if result["status"] != "ok":
        return result
    account = result["account"]

    nickname = slots.get("new_nickname")
    if nickname is None:
        return {
            "status": "ask",
            "question": f"'{account['nickname']}' 계좌의 새 별명을 알려주세요. (1~{NICKNAME_MAX}자)",
            "missing": ["new_nickname"],
            "candidates": None,
        }
    nickname = nickname.strip()
    error = _validate_nickname(data, user_id, account, nickname)
    if error:
        return {"status": "fail", "error": error}
    return {"status": "ok", "draft": {"account_id": account["account_id"], "new_nickname": nickname}}


def describe_rename_account(user_id: str, draft: dict) -> str:
    account = _find_account(load_data(), user_id, draft["account_id"])
    return (
        "[계좌 별명 변경]\n"
        f"- 계좌: {account['account_id']}\n"
        f"- 별명: {account['nickname']} → {draft['new_nickname']}\n"
        "- 안내: 이미 기록된 이체 거래의 상대 계좌 이름은 바뀌지 않아요."
    )


def execute_rename_account(user_id: str, request_id: str) -> dict:
    data = load_data()
    request = _find_request(data, request_id)
    if request["status"] != "pending_approval":
        return {"status": "skipped", "message": f"이미 처리된 요청이에요. (상태: {request['status']})"}

    draft = request["params"]
    account = _find_account(data, user_id, draft["account_id"])
    error = "계좌를 찾을 수 없어요." if account is None else _validate_nickname(data, user_id, account, draft["new_nickname"])
    if error:
        return _fail_and_save(data, request, error)

    before = account["nickname"]
    account["nickname"] = draft["new_nickname"]
    message = f"'{before}' 계좌의 별명을 {with_ro(draft['new_nickname'])} 바꿨어요."
    _set_request_result(request, "completed", message, before=before)
    return _save_result(data, message)


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
    "reissue_card": Task(
        kind="change", label="카드 재발급 신청", slots=("card", "address"),
        resolve=resolve_reissue, describe=describe_reissue, execute=execute_reissue,
    ),
    "query_reissue": Task(
        kind="query", label="재발급 신청 조회", slots=("card", "application"), query=query_reissue,
    ),
    "modify_reissue": Task(
        kind="change", label="재발급 배송지 변경", slots=("card", "application", "address"),
        resolve=resolve_modify_reissue, describe=describe_modify_reissue, execute=execute_modify_reissue,
    ),
    "cancel_reissue": Task(
        kind="change", label="재발급 신청 취소", slots=("card", "application"),
        resolve=resolve_cancel_reissue, describe=describe_cancel_reissue, execute=execute_cancel_reissue,
    ),
    "query_bills": Task(kind="query", label="미납 청구서 조회", query=query_bills),
    "query_transactions": Task(
        kind="query", label="거래 내역 조회",
        slots=("account", "period", "start_date", "end_date", "min_amount", "max_amount", "tx_type"),
        query=query_transactions,
    ),
    "conditional_transfer": Task(
        kind="change", label="조건부 이체", slots=("from_account", "to_account", "keep_amount"),
        resolve=resolve_conditional_transfer, describe=describe_conditional_transfer,
        execute=execute_conditional_transfer,
    ),
    "multi_transfer": Task(
        kind="change", label="여러 계좌 이체", slots=("from_account", "transfers"),
        resolve=resolve_multi_transfer, describe=describe_multi_transfer, execute=execute_multi_transfer,
    ),
    "rename_account": Task(
        kind="change", label="계좌 별명 변경", slots=("account", "new_nickname"),
        resolve=resolve_rename_account, describe=describe_rename_account, execute=execute_rename_account,
    ),
    "pay_bill": Task(
        kind="change", label="청구서 납부", slots=("from_account", "bill"),
        resolve=resolve_pay_bill, describe=describe_pay_bill, execute=execute_pay_bill,
    ),
    "pay_bills_batch": Task(
        kind="change", label="청구서 일괄 납부", slots=("from_account", "bills", "all_bills"),
        resolve=resolve_pay_bills_batch, describe=describe_pay_bills_batch, execute=execute_pay_bills_batch,
    ),
}

# 여러 업무를 이어서 처리하는 복합 업무. 각 단계는 따로 승인받는다. (설계 6-3)
#   - 앞 단계를 거절하거나 실행에 실패하면 뒤 단계는 진행하지 않는다.
#   - 앞 단계가 이미 완료된 상태(already)이면 건너뛰고 다음 단계로 간다.
#   - 뒤 단계가 실패·취소돼도 앞 단계 결과는 되돌리지 않는다.
COMPOSITES: dict[str, list[str]] = {
    "lost_and_reissue": ["report_lost", "reissue_card"],
}
