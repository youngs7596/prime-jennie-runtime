"""SCOUT §6.6 — 악의 코드 12건 (M01~M12) 모두 거부 검증.

allowlist는 정적 AST 분석만 한다. 런타임 효과(M08 timeout, M09 OOM, M10 fork bomb)는
adapter/컨테이너 책임이므로 별도 테스트 클래스로 분리.
"""

from __future__ import annotations

import pytest

from prime_jennie_runtime.screening_executor.allowlist import check_imports


class TestStaticAstViolations:
    """AST만으로 거부할 수 있는 케이스 — M01~M07, M11, M12, 그리고 추가."""

    def test_m01_import_os(self):
        """M01: `import os; os.system(...)` → import_violation."""
        v = check_imports("import os\nos.system('whoami')\n")
        assert any(s.startswith("import:os") for s in v)

    def test_m02_dunder_import_subprocess(self):
        """M02: `__import__('subprocess').call(...)` → forbidden_call."""
        v = check_imports("__import__('subprocess').call(['ls'])\n")
        assert any(s == "call:__import__" for s in v)

    def test_m03_eval(self):
        """M03: `eval(...)` → forbidden_call."""
        v = check_imports("eval('1+1')\n")
        assert v == ["call:eval"]

    def test_m04_exec_decoded(self):
        """M04: `exec(b'...'.decode())` → forbidden_call."""
        v = check_imports("exec(b'pass'.decode())\n")
        assert "call:exec" in v

    def test_m05_import_socket(self):
        """M05: `import socket` → import_violation."""
        v = check_imports("import socket\n")
        assert v == ["import:socket"]

    def test_m07_import_sklearn_root(self):
        """M07: `import sklearn` (root) → 거부. sklearn.cluster 같은 sub만 OK."""
        v = check_imports("import sklearn\n")
        assert v == ["import:sklearn"]

    def test_m11_import_ctypes(self):
        """M11: `import ctypes` → import_violation."""
        v = check_imports("import ctypes\n")
        assert v == ["import:ctypes"]

    def test_m12_importlib_import_module(self):
        """M12: `importlib.import_module('os')` → importlib 자체가 화이트리스트 밖이라 거부."""
        v = check_imports("import importlib\nimportlib.import_module('os')\n")
        assert "import:importlib" in v

    def test_compile_call_blocked(self):
        """추가: `compile('1+1', '<x>', 'eval')` 단독 호출 거부."""
        v = check_imports("compile('1+1', '<x>', 'eval')\n")
        assert v == ["call:compile"]

    def test_from_import_subprocess(self):
        """추가: `from subprocess import run` 거부."""
        v = check_imports("from subprocess import run\n")
        assert v == ["import:subprocess"]

    def test_relative_import_blocked(self):
        """추가: `from . import x` 같은 relative import 거부."""
        v = check_imports("from . import sibling\n")
        assert v == ["import:<relative>"]

    def test_method_call_compile_not_blocked(self):
        """경계 케이스: `obj.compile(...)` (예: re.compile) 같은 메서드 호출도 단순 이름이
        FORBIDDEN_CALLS와 일치하면 차단된다 — false positive 가능. Phase 1 한계로 받아들이고
        명시적 테스트로 박제 (사용자가 re를 못 쓰는 건 어차피 import 단계에서 막힘).
        """
        v = check_imports("import pandas as pd\npd.compile  # 호출 아님\n")
        assert v == []
        # 실제 호출이면 차단
        v2 = check_imports("import pandas as pd\npd.compile(1)\n")
        assert v2 == ["call:compile"]


class TestAllowedSamples:
    """화이트리스트 모듈은 통과해야 함."""

    @pytest.mark.parametrize(
        "src",
        [
            "import pandas as pd\n",
            "import numpy as np\n",
            "from scipy.stats import norm\n",
            "from sklearn.cluster import KMeans\n",
            "from sklearn.linear_model import LinearRegression\n",
            "from sklearn.preprocessing import StandardScaler\n",
            "from sklearn.metrics import accuracy_score\n",
            "import math\n",
            "import statistics\n",
            "from datetime import date, datetime\n",
            # 실제 screen 패턴
            "import pandas as pd\nimport numpy as np\n"
            "def screen(market_data, context):\n"
            "    return []\n",
        ],
    )
    def test_allowed_passes(self, src: str):
        assert check_imports(src) == []


class TestSyntaxError:
    def test_syntax_error_returned(self):
        v = check_imports("def screen(x: : pass\n")  # 의도적 깨짐
        assert v and v[0].startswith("syntax_error")
