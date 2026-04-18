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


class TestAttributeBypass:
    """M13~M17: getattr / dunder / pickle / globals 우회 차단.

    LLM 환각으로 생성될 수 있는 전형적 sandbox-escape 패턴을 AST 레벨에서 블록.
    """

    def test_m13_getattr_builtins_import(self):
        """M13: `getattr(__builtins__, '__import__', None)` 체인.

        `'__import__'` 는 str literal 이라 ast.Attribute 로 잡히지는 않지만,
        `getattr` name + `__builtins__` name 둘이 이미 차단되어 체인 자체가 reject.
        """
        code = (
            "def screen(market_data, context):\n"
            "    _imp = getattr(__builtins__, '__import__', None)\n"
            "    return []\n"
        )
        v = check_imports(code)
        assert "call:getattr" in v
        assert "call:__builtins__" in v

    def test_m14_pandas_read_pickle(self):
        """M14: `pd.read_pickle(...)` — pickle 경로 차단."""
        code = (
            "import pandas as pd\n"
            "def screen(market_data, context):\n"
            "    return pd.read_pickle('/tmp/payload.pkl').to_dict('records')\n"
        )
        v = check_imports(code)
        assert "call:read_pickle" in v

    def test_m15_numpy_load_allow_pickle(self):
        """M15: `np.load(..., allow_pickle=True)` — pickle kwarg 차단."""
        code = (
            "import numpy as np\n"
            "def screen(market_data, context):\n"
            "    arr = np.load('/tmp/x.npy', allow_pickle=True)\n"
            "    return []\n"
        )
        v = check_imports(code)
        assert "call:load(allow_pickle=True)" in v

    def test_m15_numpy_load_without_allow_pickle_ok(self):
        """M15 네거티브: allow_pickle 생략/False면 통과."""
        code_default = (
            "import numpy as np\n"
            "def screen(market_data, context):\n"
            "    arr = np.load('/tmp/x.npy')\n"
            "    return []\n"
        )
        code_false = (
            "import numpy as np\n"
            "def screen(market_data, context):\n"
            "    arr = np.load('/tmp/x.npy', allow_pickle=False)\n"
            "    return []\n"
        )
        assert check_imports(code_default) == []
        assert check_imports(code_false) == []

    def test_m16_globals_access(self):
        """M16: `globals()` 참조 차단."""
        code = "def screen(market_data, context):\n    g = globals()\n    return []\n"
        v = check_imports(code)
        assert "call:globals" in v

    def test_dunder_class_mro(self):
        """`object.__class__.__mro__` 체인 — introspection escape."""
        code = "def screen(market_data, context):\n    return list(object.__class__.__mro__)\n"
        v = check_imports(code)
        assert "attr:__class__" in v
        assert "attr:__mro__" in v

    def test_dunder_subclasses_escape(self):
        """`type.__subclasses__()` 클래식 escape."""
        code = (
            "def screen(market_data, context):\n"
            "    for cls in type.__subclasses__(object):\n"
            "        pass\n"
            "    return []\n"
        )
        v = check_imports(code)
        assert "attr:__subclasses__" in v

    def test_dict_globals_chain(self):
        """func.__globals__ 접근."""
        code = "def screen(market_data, context):\n    return screen.__globals__\n"
        v = check_imports(code)
        assert "attr:__globals__" in v

    def test_user_defined_getattr_allowed(self):
        """`def getattr(x): ...` user 정의는 Store 컨텍스트라 통과.

        (FORBIDDEN_NAMES 검사는 Load 컨텍스트 한정)
        """
        code = (
            "def screen(market_data, context):\n"
            "    def getattr_safe(x):\n"
            "        return x\n"
            "    return []\n"
        )
        # getattr_safe 이름 자체는 다른 식별자. FORBIDDEN_NAMES 검사는 id 정확 매치.
        assert check_imports(code) == []

    def test_setattr_blocked(self):
        code = (
            "def screen(market_data, context):\n"
            "    setattr(context, 'hacked', True)\n"
            "    return []\n"
        )
        v = check_imports(code)
        assert "call:setattr" in v


class TestAllowedSamples:
    """화이트리스트 모듈은 통과해야 함."""

    @pytest.mark.parametrize(
        "src",
        [
            "import pandas as pd\n",
            "import numpy as np\n",
            "from scipy.stats import norm\n",
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
