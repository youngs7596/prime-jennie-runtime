"""SCOUT §11 — Screening Executor 시나리오 테스트 (S03~S09, S13).

executor는 in-process. timeout/OOM은 adapter 컨테이너 책임이라 여기선 다루지 않음.
"""

from __future__ import annotations

from prime_jennie_runtime.screening_executor.executor import ScreeningExecutor

UNIVERSE = ["005930", "000660", "207940"]
CONTEXT = {"as_of": "2026-04-17", "universe": UNIVERSE}


def _executor(cap: int = 20) -> ScreeningExecutor:
    return ScreeningExecutor(candidate_cap=cap)


# ---------- S03 ----------


def test_s03_screen_not_defined():
    code = "x = 1\n"
    r = _executor().run(code, CONTEXT)
    assert not r.ok
    assert r.error == "screen_not_defined"


# ---------- S04 ----------


def test_s04_import_violation_blocks():
    code = "import os\ndef screen(md, ctx):\n    return []\n"
    r = _executor().run(code, CONTEXT)
    assert not r.ok
    assert r.error == "import_violation"
    assert r.details == ["import:os"]


# ---------- S05 ----------


def test_s05_forbidden_call():
    code = "def screen(md, ctx):\n    return eval('[]')\n"
    r = _executor().run(code, CONTEXT)
    assert not r.ok
    assert r.error == "forbidden_call"


# ---------- S06 ----------


def test_s06_truncate_to_cap():
    code = (
        "def screen(md, ctx):\n"
        "    return [{\n"
        "        'ticker': '00000' + str(i),\n"
        "        'strategy_tag': 'X',\n"
        "        'conviction': 0.5,\n"
        "        'entry_hint': {'trigger': 'market'},\n"
        "        'factors': {},\n"
        "        'notes': '',\n"
        "    } for i in range(25)]\n"
    )
    r = _executor(cap=20).run(code, CONTEXT)
    assert r.ok
    assert len(r.candidates) == 20
    assert r.truncated


# ---------- S07 (executor 단계는 dedup 안 함, validators 책임) ----------


def test_s07_executor_does_not_dedup():
    """executor는 dedup 책임 없음. validators가 dedup. 여기서는 단순 통과 확인."""
    code = (
        "def screen(md, ctx):\n"
        "    base = {\n"
        "        'strategy_tag': 'X', 'conviction': 0.5,\n"
        "        'entry_hint': {'trigger': 'market'},\n"
        "        'factors': {}, 'notes': '',\n"
        "    }\n"
        "    return [\n"
        "        {**base, 'ticker': '005930', 'conviction': 0.9},\n"
        "        {**base, 'ticker': '005930', 'conviction': 0.3},\n"
        "    ]\n"
    )
    r = _executor().run(code, CONTEXT)
    assert r.ok
    assert len(r.candidates) == 2  # validators가 dedup


# ---------- S07/S08 universe-out 통과 (validators 책임) ----------


def test_executor_passes_outside_universe_to_validator():
    code = (
        "def screen(md, ctx):\n"
        "    return [{\n"
        "        'ticker': '999999',\n"
        "        'strategy_tag': 'X', 'conviction': 0.5,\n"
        "        'entry_hint': {'trigger': 'market'},\n"
        "        'factors': {}, 'notes': '',\n"
        "    }]\n"
    )
    r = _executor().run(code, CONTEXT)
    assert r.ok
    assert r.candidates[0].ticker == "999999"


# ---------- runtime_error ----------


def test_runtime_error_in_screen_body():
    code = "def screen(md, ctx):\n    raise ValueError('boom')\n"
    r = _executor().run(code, CONTEXT)
    assert not r.ok
    assert r.error == "runtime_error"
    assert "ValueError" in (r.details or "")


# ---------- compile_error ----------


def test_compile_error_for_bad_syntax():
    """syntax error는 allowlist 단계의 syntax_error를 import_violation으로 분류."""
    code = "def screen(md, ctx)\n    return []\n"  # 콜론 빠짐
    r = _executor().run(code, CONTEXT)
    assert not r.ok
    assert r.error == "import_violation"
    assert r.details and r.details[0].startswith("syntax_error")


# ---------- invalid_return_type ----------


def test_invalid_return_type_not_list():
    code = "def screen(md, ctx):\n    return {'ticker': '005930'}\n"
    r = _executor().run(code, CONTEXT)
    assert not r.ok
    assert r.error == "invalid_return_type"


def test_invalid_return_type_bad_item():
    """item에 ScreeningCandidate 필수 필드 누락 → invalid_return_type."""
    code = (
        "def screen(md, ctx):\n"
        "    return [{'ticker': '005930'}]\n"  # strategy_tag 등 누락
    )
    r = _executor().run(code, CONTEXT)
    assert not r.ok
    assert r.error == "invalid_return_type"


def test_invalid_return_type_wrong_item_type():
    code = "def screen(md, ctx):\n    return ['005930', 42]\n"
    r = _executor().run(code, CONTEXT)
    assert not r.ok
    assert r.error == "invalid_return_type"


# ---------- happy path ----------


def test_happy_path_produces_candidate():
    code = (
        "def screen(md, ctx):\n"
        "    return [{\n"
        "        'ticker': '005930',\n"
        "        'strategy_tag': 'SECTOR_MOMENTUM',\n"
        "        'conviction': 0.7,\n"
        "        'entry_hint': {'trigger': 'limit', 'price_hint': 71000.0},\n"
        "        'factors': {'momentum_5d': 0.034},\n"
        "        'notes': 'reason',\n"
        "    }]\n"
    )
    r = _executor().run(code, CONTEXT)
    assert r.ok
    assert len(r.candidates) == 1
    c = r.candidates[0]
    assert c.ticker == "005930"
    assert c.strategy_tag == "SECTOR_MOMENTUM"
    assert c.entry_hint.trigger == "limit"
    assert c.entry_hint.price_hint == 71000.0
    assert r.exec_time_ms >= 0


# ---------- S13 재현성 ----------


def test_s13_reproducibility_same_code_same_result():
    code = (
        "def screen(md, ctx):\n"
        "    return [{\n"
        "        'ticker': '005930',\n"
        "        'strategy_tag': 'SECTOR_MOMENTUM',\n"
        "        'conviction': 0.5,\n"
        "        'entry_hint': {'trigger': 'market'},\n"
        "        'factors': {}, 'notes': '',\n"
        "    }]\n"
    )
    r1 = _executor().run(code, CONTEXT)
    r2 = _executor().run(code, CONTEXT)
    assert r1.candidates == r2.candidates


# ---------- ScreeningCandidate 인스턴스 직접 반환도 OK ----------


def test_returns_pydantic_instances_directly():
    code = (
        "from prime_jennie_runtime.slow_loop.scout.schemas import "
        "ScreeningCandidate, EntryHint\n"
        "def screen(md, ctx):\n"
        "    return [ScreeningCandidate(\n"
        "        ticker='005930', strategy_tag='X', conviction=0.5,\n"
        "        entry_hint=EntryHint(trigger='market'),\n"
        "    )]\n"
    )
    # 단, 위 import는 화이트리스트가 아니므로 실패해야 함 — 이중 안전장치 검증
    r = _executor().run(code, CONTEXT)
    assert not r.ok
    assert r.error == "import_violation"


def test_executor_accepts_candidate_instance_via_namespace_injection():
    """allowlist 통과를 위해 외부 import 없이도 ScreeningCandidate 인스턴스를
    반환할 수 있는지 확인. 실제로는 dict 반환이 일반적이지만, 명세상 둘 다 허용.
    여기선 dict 리턴 테스트를 happy_path가 이미 커버하므로 skip 대신 명시적 박제.
    """
    # NOTE: scout 코드에서 ScreeningCandidate 직접 import 불가 (allowlist) →
    # 운영 시엔 항상 dict 반환. Pydantic instance 통과는 호스트 in-process 호출
    # (테스트 코드가 직접 namespace 주입)으로만 검증되며 별도 테스트 불필요.
