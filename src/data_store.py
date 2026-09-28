# 원본 JSON의 복사본을 작업용 데이터로 준비합니다.
# 작업용 JSON을 읽고 변경된 데이터를 저장하는 함수를 구현합니다.
# 초기화가 필요할 때만 원본을 다시 복사합니다.

import json
import os
import shutil
import tempfile
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
INITIAL_PATH = DATA_DIR / "initial_data.json"   # 원본 (수정하지 않음)
WORK_PATH = DATA_DIR / "bank_data.json"         # 작업용 복사본
SEED_PATH = DATA_DIR / "test_seed.json"         # 테스트용 추가 데이터 (선택)
CHECKPOINT_PATH = DATA_DIR / "checkpoints.sqlite"

# 배열별 ID 필드. 테스트 시드를 합칠 때 같은 항목을 찾는 데 사용한다.
ID_FIELDS = {
    "accounts": "account_id",
    "cards": "card_id",
    "transactions": "transaction_id",
    "addresses": "address_id",
    "requests": "request_id",
    "reissue_applications": "application_id",
    "bills": "bill_id",
}

# 저장 실패 주입용 카운터. FAIL_SAVE_AT=2 이면 두 번째 save_data 호출이 실패한다.
_save_count = 0


class SaveError(Exception):
    """작업용 JSON 저장에 실패했을 때 발생한다. 기존 파일은 그대로 남는다."""


def ensure_data() -> None:
    """작업용 복사본이 없으면 원본을 복사한다."""
    if not WORK_PATH.exists():
        shutil.copyfile(INITIAL_PATH, WORK_PATH)


def load_data() -> dict:
    """작업용 JSON을 읽는다. 조회할 때마다 호출해 저장된 최신 값을 사용한다."""
    ensure_data()
    with WORK_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def save_data(data: dict) -> None:
    """작업용 JSON 전체를 저장한다.

    임시 파일에 먼저 쓴 뒤 os.replace로 교체하므로, 저장 도중 실패해도
    기존 파일은 저장 전 내용 그대로 유지된다.
    """
    global _save_count
    _save_count += 1
    if _should_fail(_save_count):
        raise SaveError(f"저장 실패 주입 (FAIL_SAVE_AT, {_save_count}번째 저장)")

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=DATA_DIR, suffix=".tmp", delete=False
        ) as f:
            tmp_path = f.name
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, WORK_PATH)
    except OSError as e:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise SaveError(f"데이터 파일 저장 실패: {e}") from e


def reset_data(with_seed: bool = False) -> None:
    """원본을 다시 복사하고 대화 체크포인트도 함께 삭제한다.

    둘 중 하나만 지우면 대화 기록과 업무 데이터가 어긋나므로 항상 같이 초기화한다.
    with_seed=True 이면 test_seed.json의 항목을 원본 위에 합친다.
    """
    global _save_count
    _save_count = 0

    for path in DATA_DIR.glob(CHECKPOINT_PATH.name + "*"):  # -wal, -shm 포함
        path.unlink()

    shutil.copyfile(INITIAL_PATH, WORK_PATH)

    if with_seed:
        data = load_data()
        with SEED_PATH.open(encoding="utf-8") as f:
            seed = json.load(f)
        _merge_seed(data, seed)
        save_data(data)


def _merge_seed(data: dict, seed: dict) -> None:
    """시드 항목을 ID 기준으로 합친다. 같은 ID가 있으면 교체하고, 없으면 추가한다."""
    for key, items in seed.items():
        id_field = ID_FIELDS[key]
        index = {item[id_field]: i for i, item in enumerate(data[key])}
        for item in items:
            if item[id_field] in index:
                data[key][index[item[id_field]]] = item
            else:
                data[key].append(item)


def _should_fail(count: int) -> bool:
    value = os.getenv("FAIL_SAVE_AT", "").strip()
    if not value:
        return False
    return str(count) in {v.strip() for v in value.split(",")}
