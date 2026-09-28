# 테스트 공통 설정
#
# - 모든 테스트는 임시 폴더의 데이터 복사본을 사용한다. data/의 작업 파일과 체크포인트는 건드리지 않는다.
# - fake_llm: 미리 정한 LLM 응답을 순서대로 돌려준다. 그래프 흐름을 API 호출 없이 매번 같게 확인한다.
# - llm 표시 테스트: RUN_LLM_TESTS=1일 때만 실제 Gemini로 실행한다.

import os
import shutil
from pathlib import Path

import pytest

import data_store as ds
import functions as fn

USER = "user-001"
REAL_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """임시 폴더에 원본과 테스트 시드를 복사하고 data_store 경로를 그쪽으로 바꾼다."""
    for name in ("initial_data.json", "test_seed.json"):
        shutil.copyfile(REAL_DATA_DIR / name, tmp_path / name)
    monkeypatch.setattr(ds, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ds, "INITIAL_PATH", tmp_path / "initial_data.json")
    monkeypatch.setattr(ds, "WORK_PATH", tmp_path / "bank_data.json")
    monkeypatch.setattr(ds, "SEED_PATH", tmp_path / "test_seed.json")
    monkeypatch.setattr(ds, "CHECKPOINT_PATH", tmp_path / "checkpoints.sqlite")
    monkeypatch.delenv("FAIL_SAVE_AT", raising=False)
    ds.reset_data()
    yield tmp_path


@pytest.fixture
def today(monkeypatch):
    """기준일을 고정한다. 사용: today("2026-09-28")"""
    def set_today(value: str):
        monkeypatch.setattr(fn, "today_kst", lambda: value)
    return set_today


@pytest.fixture
def fail_save_at(monkeypatch):
    """n번째 save_data를 실패시킨다. 카운터는 지금부터 다시 센다. 사용: fail_save_at("2")"""
    def set_fail(value: str):
        monkeypatch.setattr(ds, "_save_count", 0)
        monkeypatch.setenv("FAIL_SAVE_AT", value)
    return set_fail


class FakeLLM:
    """with_structured_output(...).invoke(...)에 미리 넣어둔 응답을 순서대로 돌려준다."""

    def __init__(self):
        self.replies = []
        self.prompts = []

    def add(self, *replies):
        self.replies.extend(replies)

    def with_structured_output(self, schema):
        return self

    def invoke(self, prompt):
        self.prompts.append(prompt)
        if not self.replies:
            raise AssertionError("FakeLLM에 준비된 응답이 없어요.")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def fake_llm(monkeypatch):
    import graph
    llm = FakeLLM()
    monkeypatch.setattr(graph, "get_llm", lambda: llm)
    return llm


def pytest_collection_modifyitems(config, items):
    if os.getenv("RUN_LLM_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="실제 LLM 테스트는 RUN_LLM_TESTS=1일 때만 실행")
    for item in items:
        if "llm" in item.keywords:
            item.add_marker(skip)


# ---- 도우미 -------------------------------------------------------------------

def load():
    return ds.load_data()


def balance(account_id: str) -> int:
    return next(a for a in load()["accounts"] if a["account_id"] == account_id)["balance"]


def set_balance(account_id: str, value: int) -> None:
    data = load()
    next(a for a in data["accounts"] if a["account_id"] == account_id)["balance"] = value
    ds.save_data(data)


def card_status(card_id: str) -> str:
    return next(c for c in load()["cards"] if c["card_id"] == card_id)["status"]


def set_card_status(card_id: str, status: str) -> None:
    data = load()
    next(c for c in data["cards"] if c["card_id"] == card_id)["status"] = status
    ds.save_data(data)


def run_task(task: str, slots: dict, approve: bool = True) -> dict:
    """resolve → 요청 기록 → 실행(또는 거절)까지 한 번에. 결과에 request_id를 붙여 돌려준다."""
    resolved = fn.TASKS[task].resolve(USER, slots)
    if resolved["status"] != "ok":
        return resolved
    request_id = fn.create_request(USER, task, resolved["draft"])
    result = fn.TASKS[task].execute(USER, request_id) if approve else fn.cancel_request(request_id)
    return {**result, "request_id": request_id}
