import json
import os

import pytest

import data_store as ds


def initial():
    return json.loads(ds.INITIAL_PATH.read_text(encoding="utf-8"))


def test_reset_creates_work_copy_same_as_initial():
    assert ds.WORK_PATH.exists()
    assert ds.load_data() == initial()


def test_save_and_load_roundtrip_keeps_korean_and_leaves_no_temp_file():
    data = ds.load_data()
    data["accounts"][0]["balance"] = 1
    ds.save_data(data)

    assert ds.load_data()["accounts"][0]["balance"] == 1
    assert not list(ds.DATA_DIR.glob("*.tmp"))
    assert "생활비" in ds.WORK_PATH.read_text(encoding="utf-8")


def test_injected_failure_keeps_previous_file(fail_save_at):
    fail_save_at("2")
    data = ds.load_data()
    data["accounts"][0]["balance"] = 111
    ds.save_data(data)                      # 1번째: 성공

    data["accounts"][0]["balance"] = 222
    with pytest.raises(ds.SaveError):
        ds.save_data(data)                  # 2번째: 실패
    assert ds.load_data()["accounts"][0]["balance"] == 111

    data["accounts"][0]["balance"] = 333
    ds.save_data(data)                      # 3번째: 성공
    assert ds.load_data()["accounts"][0]["balance"] == 333


def test_os_error_becomes_save_error_and_keeps_file():
    os.chmod(ds.DATA_DIR, 0o555)
    try:
        with pytest.raises(ds.SaveError):
            ds.save_data({"x": 1})
    finally:
        os.chmod(ds.DATA_DIR, 0o755)
    assert ds.load_data() == initial()


def test_reset_removes_checkpoint_files():
    for suffix in ("", "-wal", "-shm"):
        (ds.DATA_DIR / f"checkpoints.sqlite{suffix}").write_text("x")
    ds.reset_data()
    assert not list(ds.DATA_DIR.glob("checkpoints.sqlite*"))


def test_reset_with_seed_replaces_same_id_and_appends_new():
    ds.reset_data(with_seed=True)
    data = ds.load_data()

    cards = {c["card_id"]: c for c in data["cards"]}
    assert len(data["cards"]) == 5                     # card-002 교체, card-005 추가
    assert cards["card-002"]["status"] == "lost"
    assert [a["application_id"] for a in data["reissue_applications"]] == ["rei-001", "rei-002", "rei-003"]
