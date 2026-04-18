"""AST 기반 import + forbidden call 검사 (SCOUT §6.3).

Scout 코드 안의 모든 import 문을 화이트리스트와 대조. 동시에 eval/exec/compile/
__import__ 호출도 거부. 통과한 코드만 executor가 exec한다.

원칙: 거부는 무조건 영속적 실패. 재시도 안 함 (SCOUT §8.3 import_violation).
"""

from __future__ import annotations

import ast

# SCOUT §6.3 — 정확히 이 모듈만 허용. 하위 모듈은 prefix match.
# scipy 전체를 허용 — LLM 이 `import scipy` 또는 `from scipy import stats` 자유롭게
# 쓸 수 있도록. 실 격리는 subprocess/docker 샌드박스가 담당.
ALLOWED_MODULES: frozenset[str] = frozenset(
    {
        "pandas",
        "numpy",
        "scipy",
        "math",
        "statistics",
        "datetime",
    }
)

FORBIDDEN_CALLS: frozenset[str] = frozenset({"__import__", "eval", "exec", "compile"})

# 속성 접근 우회 경로 차단 (builtins/introspection/pickle).
# getattr(obj, "__import__")(...) / obj.__class__.__mro__ / ({}).__globals__ 등 방어.
FORBIDDEN_ATTRS: frozenset[str] = frozenset(
    {
        "__import__",
        "__builtins__",
        "__globals__",
        "__dict__",
        "__subclasses__",
        "__class__",
        "__mro__",
        "__bases__",
    }
)

# 이름 참조로 builtins 우회 (getattr/globals/vars 등)도 금지.
# ast.Name(ctx=Load) 에서만 검사 — `def getattr(...)` user-define 은 Store 이므로 통과.
FORBIDDEN_NAMES: frozenset[str] = frozenset(
    {"getattr", "setattr", "globals", "vars", "locals", "__builtins__"}
)


def _is_module_allowed(module: str) -> bool:
    """sklearn 같은 root 패키지 통째 import는 차단. 정확/하위 prefix만 허용."""
    if not module:
        return False
    return any(module == m or module.startswith(m + ".") for m in ALLOWED_MODULES)


def _has_allow_pickle_true(call: ast.Call) -> bool:
    """numpy.load(allow_pickle=True) 같은 pickle 경로 탐지."""
    for kw in call.keywords:
        if (
            kw.arg == "allow_pickle"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is True
        ):
            return True
    return False


def check_imports(source: str) -> list[str]:
    """Scout 코드를 AST 파싱해 위반 사항 목록을 반환. 빈 list면 통과.

    SCOUT §6.3 명세 + 다음 추가 검사:
    - `import x` / `from x import y` 모두 검사 (relative import는 module=None → 거부)
    - `__import__`, `eval`, `exec`, `compile` 직접 호출 검사 (id 기반)
    - `ast.Attribute` 로 FORBIDDEN_ATTRS 접근 차단 — getattr/__class__/__mro__ 우회 방어
    - `ast.Name(ctx=Load)` 로 FORBIDDEN_NAMES 참조 차단 — getattr/globals 우회 방어
    - pandas `read_pickle` / numpy `load(allow_pickle=True)` 차단
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [f"syntax_error: {e.msg}"]

    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _is_module_allowed(alias.name):
                    violations.append(f"import:{alias.name}")
        elif isinstance(node, ast.ImportFrom):
            # `from . import x` 같은 relative import는 module이 None이거나 level>0
            if node.level > 0:
                violations.append("import:<relative>")
                continue
            module = node.module or ""
            if not _is_module_allowed(module):
                violations.append(f"import:{module}")
        elif isinstance(node, ast.Call):
            fn_name = _call_name(node.func)
            if fn_name and fn_name in FORBIDDEN_CALLS:
                violations.append(f"call:{fn_name}")
            # pickle 경로: pd.read_pickle / np.load(allow_pickle=True)
            if fn_name == "read_pickle":
                violations.append("call:read_pickle")
            elif fn_name == "load" and _has_allow_pickle_true(node):
                violations.append("call:load(allow_pickle=True)")
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_ATTRS:
                violations.append(f"attr:{node.attr}")
        elif (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in FORBIDDEN_NAMES
        ):
            violations.append(f"call:{node.id}")

    return violations


def _call_name(func_node: ast.AST) -> str | None:
    """ast.Call.func에서 호출 대상 식별자 이름 추출.

    - ast.Name → 'eval'
    - ast.Attribute → 'os.system' (가장 바깥 attr.attr만 단순 이름으로)
    - 그 외 (Call, Subscript 등) → None
    """
    if isinstance(func_node, ast.Name):
        return func_node.id
    if isinstance(func_node, ast.Attribute):
        return func_node.attr
    return None
