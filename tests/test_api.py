"""API 层测试：建库、提交、查询、无解诊断。"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    # 重新加载 DB 路径后重建 app
    import importlib

    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c


GOOD_BODY = {
    "system_heights": [120.0, 130.0, 110.0, 125.0, 115.0],
    "boundary_rests": [1.0, 4.0, 0.5, 6.0],
    "page_height": 300.0,
    "min_safe_rest": 2.0,
}


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_create_and_get_plan(client):
    r = client.post("/plans", json=GOOD_BODY)
    assert r.status_code == 201, r.text
    plan = r.json()
    assert plan["id"] == 1
    assert plan["page_count"] >= 1
    total = sum(p["used_height"] for p in plan["pages"])
    assert total == pytest.approx(sum(GOOD_BODY["system_heights"]))
    for p in plan["pages"]:
        assert p["used_height"] <= GOOD_BODY["page_height"] + 1e-6
    for p in plan["pages"][:-1]:
        assert p["turn_rest_after"] >= GOOD_BODY["min_safe_rest"]
    assert plan["pages"][-1]["turn_rest_after"] is None

    got = client.get(f"/plans/{plan['id']}")
    assert got.status_code == 200
    assert got.json() == plan

    listed = client.get("/plans")
    assert listed.status_code == 200
    assert any(s["id"] == plan["id"] for s in listed.json())


def test_infeasible_returns_422_and_saves_nothing(client):
    bad = dict(GOOD_BODY, boundary_rests=[0.1, 0.1, 0.1, 0.1], page_height=150.0)
    r = client.post("/plans", json=bad)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "no_feasible_pagination"
    assert detail["reason"] in ("system_exceeds_page_height", "unsafe_boundary")
    assert client.get("/plans").json() == []  # 不保存部分方案


def test_oversized_system_diagnosis(client):
    body = dict(GOOD_BODY, system_heights=[100.0, 999.0, 100.0], boundary_rests=[3.0, 3.0])
    r = client.post("/plans", json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["reason"] == "system_exceeds_page_height"
    assert detail["system_index"] == 1


def test_get_missing_plan_404(client):
    assert client.get("/plans/42").status_code == 404


def test_validation_errors(client):
    # 边界数量不匹配
    r = client.post("/plans", json=dict(GOOD_BODY, boundary_rests=[1.0]))
    assert r.status_code == 422
    # 非正高度
    r = client.post("/plans", json=dict(GOOD_BODY, system_heights=[0.0, 1.0]))
    assert r.status_code == 422
    # 空系统列表
    r = client.post("/plans", json=dict(GOOD_BODY, system_heights=[], boundary_rests=[]))
    assert r.status_code == 422
    # 非法排版模式 / 起始侧
    r = client.post("/plans", json=dict(GOOD_BODY, layout_mode="book"))
    assert r.status_code == 422
    r = client.post("/plans", json=dict(GOOD_BODY, layout_mode="spread", spread_start_side="middle"))
    assert r.status_code == 422


SPREAD_BODY = {
    "system_heights": [6.0, 6.0, 6.0],
    "boundary_rests": [0.2, 8.0],
    "page_height": 10.0,
    "min_safe_rest": 1.0,
    "layout_mode": "spread",
    "spread_start_side": "left",
}


def test_layout_mode_defaults_to_single(client):
    r = client.post("/plans", json=GOOD_BODY)
    assert r.status_code == 201, r.text
    plan = r.json()
    assert plan["layout_mode"] == "single"
    # single 模式响应兼容：不分左右页，每次翻页都是实体翻页
    for p in plan["pages"]:
        assert p["side"] is None
        assert p["physical_turn_after"] == (p["turn_rest_after"] is not None)


def test_spread_plan_create_and_replay(client):
    r = client.post("/plans", json=SPREAD_BODY)
    assert r.status_code == 201, r.text
    plan = r.json()
    assert plan["layout_mode"] == "spread"
    assert plan["spread_start_side"] == "left"
    assert [p["side"] for p in plan["pages"]] == ["left", "right", "left"]
    assert [p["physical_turn_after"] for p in plan["pages"]] == [False, True, False]
    assert plan["min_turn_rest"] == 8.0
    # 组内边界（页 1→2）不检查休止，也不产生 turn_rest_after
    assert plan["pages"][0]["turn_rest_after"] is None
    assert plan["pages"][1]["turn_rest_after"] == 8.0

    # SQLite 回放：新增字段完整返回
    got = client.get(f"/plans/{plan['id']}")
    assert got.status_code == 200
    assert got.json() == plan

    listed = client.get("/plans")
    assert listed.status_code == 200
    summary = next(s for s in listed.json() if s["id"] == plan["id"])
    assert summary["layout_mode"] == "spread"


def test_spread_default_start_side_is_left(client):
    body = {k: v for k, v in SPREAD_BODY.items() if k != "spread_start_side"}
    r = client.post("/plans", json=body)
    assert r.status_code == 201, r.text
    plan = r.json()
    assert plan["spread_start_side"] == "left"
    assert [p["side"] for p in plan["pages"]] == ["left", "right", "left"]


def test_spread_infeasible_diagnosis(client):
    # 页 2 为右页，其后实体翻页边界 1 休止不足 → 422 且定位该实体翻页边界
    r = client.post("/plans", json=dict(SPREAD_BODY, boundary_rests=[8.0, 0.2]))
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "no_feasible_pagination"
    assert detail["reason"] == "unsafe_boundary"
    assert detail["boundary_index"] == 1
    assert detail["physical_turn"] is True
    assert client.get("/plans").json() == []  # 不保存部分方案


# ---- 人工断点约束 ----


CONSTRAINT_BODY = {
    "system_heights": [5.0, 5.0, 5.0],
    "boundary_rests": [5.0, 3.0],
    "page_height": 10.0,
    "min_safe_rest": 1.0,
}


def test_break_constraints_default_to_empty(client):
    r = client.post("/plans", json=CONSTRAINT_BODY)
    assert r.status_code == 201, r.text
    plan = r.json()
    assert plan["forced_break_after"] == []
    assert plan["forbidden_break_after"] == []
    # SQLite 回放同样保留两组约束
    assert client.get(f"/plans/{plan['id']}").json()["forced_break_after"] == []
    assert client.get(f"/plans/{plan['id']}").json()["forbidden_break_after"] == []


def test_forced_break_create_and_replay(client):
    body = dict(CONSTRAINT_BODY, forced_break_after=[1])
    r = client.post("/plans", json=body)
    assert r.status_code == 201, r.text
    plan = r.json()
    # 系统 1 后必须分页 → [0,1]|[2]，瓶颈降为 3.0
    assert [p["end_system"] for p in plan["pages"]] == [1, 2]
    assert plan["forced_break_after"] == [1]
    assert plan["forbidden_break_after"] == []
    # 回放保留约束
    got = client.get(f"/plans/{plan['id']}").json()
    assert got["forced_break_after"] == [1]
    assert got["forbidden_break_after"] == []
    assert got == plan


def test_forbidden_break_create_and_replay(client):
    body = dict(CONSTRAINT_BODY, forbidden_break_after=[0])
    r = client.post("/plans", json=body)
    assert r.status_code == 201, r.text
    plan = r.json()
    assert 0 not in [p["end_system"] for p in plan["pages"]]
    assert plan["forbidden_break_after"] == [0]
    assert client.get(f"/plans/{plan['id']}").json() == plan


def test_spread_forced_break_inside_group_no_rest_check(client):
    # 强制断在左页后（组内）：休止 0.1 不检查、不算实体翻页
    body = {
        "system_heights": [4.0, 4.0, 4.0],
        "boundary_rests": [0.1, 9.0],
        "page_height": 10.0,
        "min_safe_rest": 1.0,
        "layout_mode": "spread",
        "forced_break_after": [0],
    }
    r = client.post("/plans", json=body)
    assert r.status_code == 201, r.text
    plan = r.json()
    assert [p["physical_turn_after"] for p in plan["pages"]] == [False, False]
    assert plan["min_turn_rest"] is None
    assert client.get(f"/plans/{plan['id']}").json()["forced_break_after"] == [0]


def test_overlapping_constraints_422(client):
    body = dict(CONSTRAINT_BODY, forced_break_after=[1], forbidden_break_after=[1])
    r = client.post("/plans", json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "invalid_break_constraints"
    types = [c["type"] for c in detail["conflicts"]]
    assert "overlap" in types
    conflict = next(c for c in detail["conflicts"] if c["type"] == "overlap")
    assert conflict["system_index"] == 1
    assert client.get("/plans").json() == []  # 不保存


def test_constraint_pointing_to_last_system_422(client):
    body = dict(CONSTRAINT_BODY, forced_break_after=[2])
    r = client.post("/plans", json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "invalid_break_constraints"
    conflict = detail["conflicts"][0]
    assert conflict["type"] == "points_to_last_system"
    assert conflict["system_index"] == 2
    assert client.get("/plans").json() == []

    body = dict(CONSTRAINT_BODY, forbidden_break_after=[2])
    r = client.post("/plans", json=body)
    assert r.status_code == 422
    assert r.json()["detail"]["conflicts"][0]["type"] == "points_to_last_system"


def test_negative_constraint_index_422(client):
    body = dict(CONSTRAINT_BODY, forced_break_after=[-1])
    assert client.post("/plans", json=body).status_code == 422


def test_constraint_makes_plan_infeasible_422_and_saves_nothing(client):
    # 边界 1 禁止分页，但系统 1、2 同页 12 > 10 → 无解，沿用既有诊断格式
    body = {
        "system_heights": [6.0, 6.0, 6.0],
        "boundary_rests": [8.0, 0.2],
        "page_height": 10.0,
        "min_safe_rest": 1.0,
        "forbidden_break_after": [1],
    }
    r = client.post("/plans", json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "no_feasible_pagination"
    assert detail["reason"] == "unsafe_boundary"
    assert detail["boundary_index"] == 1
    assert detail["forbidden_break_after"] == 1
    assert client.get("/plans").json() == []


def test_constraint_indices_normalized(client):
    # 重复下标去重并排序后回放
    body = dict(CONSTRAINT_BODY, forced_break_after=[1, 0, 1])
    r = client.post("/plans", json=body)
    assert r.status_code == 201, r.text
    plan = r.json()
    assert plan["forced_break_after"] == [0, 1]
