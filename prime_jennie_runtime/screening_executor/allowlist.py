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
        "sklearn.cluster",
        "sklearn.linear_model",
        "sklearn.preprocessing",
        "sklearn.metrics",
        "math",
        "statistics",
        "datetime",
    }
)

FORBIDDEN_CALLS: frozenset[str] = frozenset({"__import__", "eval", "exec", "compile"})


def _is_module_allowed(module: str) -> bool:
    """sklearn 같은 root 패키지 통째 import는 차단. 정확/하위 prefix만 허용."""
    if not module:
        return False
    return any(module == m or module.startswith(m + ".") for m in ALLOWED_MODULES)


def check_imports(source: str) -> list[str]:
    """Scout 코드를 AST 파싱해 위반 사항 목록을 반환. 빈 list면 통과.

    SCOUT §6.3 명세 + 다음 추가 검사:
    - `import x` / `from x import y` 모두 검사 (relative import는 module=None → 거부)
    - `__import__`, `eval`, `exec`, `compile` 직접 호출 검사 (id 기반)
    - getattr 우회는 화이트리스트 모듈 한정에서 막기 어려우므로 docs 명시 (Phase 1 한계)
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
