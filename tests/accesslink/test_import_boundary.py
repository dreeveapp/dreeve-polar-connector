"""The one rule that keeps `accesslink` extractable, enforced rather than documented.

Decision 7 of the spec: this sub-package is a standalone library that happens to live here, so it can
be contributed to `polar-flow-api` later. A convention would rot; this test cannot.

Three ways an import can reach outside the package, and all three are checked here:
  - a plain `import dreeve_polar_connector.something` or `from dreeve_polar_connector.something import x`
  - a relative import deep enough to walk past the `accesslink` package root — `from .. import x` from
    a file directly inside `accesslink/` resolves to `dreeve_polar_connector`, not to `accesslink`
  - a dynamic `importlib.import_module("dreeve_polar_connector...")` or `__import__(...)` call whose
    argument is a literal string naming the connector
"""

import ast
from pathlib import Path

from dreeve_polar_connector import accesslink

CONNECTOR_PACKAGE = "dreeve_polar_connector"
OWN_PACKAGE = f"{CONNECTOR_PACKAGE}.accesslink"


def _reaches_upward(name: str) -> bool:
    if name == OWN_PACKAGE or name.startswith(f"{OWN_PACKAGE}."):
        return False

    return name == CONNECTOR_PACKAGE or name.startswith(f"{CONNECTOR_PACKAGE}.")


def _dynamic_import_target(node: ast.Call) -> str | None:
    """The literal string argument of an `importlib.import_module(...)` or `__import__(...)` call, or
    None if this call is neither, or its first argument isn't a string literal.

    Fully dynamic string construction (an f-string, a variable, string concatenation) can't be caught
    this way and isn't worth chasing — the realistic failure mode is a literal, copy-pasted call.
    """
    func = node.func
    is_import_module = (
        isinstance(func, ast.Attribute)
        and func.attr == "import_module"
        and isinstance(func.value, ast.Name)
        and func.value.id == "importlib"
    )
    is_dunder_import = isinstance(func, ast.Name) and func.id == "__import__"
    if not (is_import_module or is_dunder_import):
        return None

    if not node.args:
        return None

    first_arg = node.args[0]
    if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
        return first_arg.value

    return None


def _relative_import_spec(node: ast.ImportFrom) -> str:
    """A readable spec for a relative import, e.g. `..` or `..config`, for use in failure messages."""
    dots = "." * node.level
    return f"{dots}{node.module}" if node.module else dots


def _upward_reaching_imports(source: str, parts: int) -> set[str]:
    """Every import in `source` that reaches above the `accesslink` package: absolute imports naming
    the connector package, relative imports deep enough to walk past the package root, and dynamic
    `importlib.import_module`/`__import__` calls with a literal argument naming the connector package.

    `parts` is how many directories deep `source`'s file sits below the `accesslink` root (0 for a
    file directly inside `accesslink/`). A relative import's safe depth depends on it: a level-L
    relative import stays inside `accesslink` only while `L <= 1 + parts` — one level for the file's
    own package, plus one more for each subdirectory it sits under. `from .. import x` from a file
    directly inside `accesslink/` (parts=0) resolves to `dreeve_polar_connector`, not to `accesslink`.
    """
    safe_max_level = 1 + parts
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names if _reaches_upward(alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module and _reaches_upward(node.module):
                    names.add(node.module)
            elif node.level > safe_max_level:
                names.add(_relative_import_spec(node))
        elif isinstance(node, ast.Call):
            target = _dynamic_import_target(node)
            if target is not None and _reaches_upward(target):
                names.add(target)

    return names


def test_accesslink_never_imports_from_the_connector() -> None:
    package_root = Path(accesslink.__file__).parent
    offenders = {}
    for path in sorted(package_root.rglob("*.py")):
        relative = path.relative_to(package_root)
        parts = len(relative.parts) - 1
        upward = _upward_reaching_imports(path.read_text(encoding="utf-8"), parts)
        if upward:
            offenders[str(relative)] = sorted(upward)

    assert offenders == {}, (
        f"accesslink must stay extractable, but these files import from the connector: {offenders}. "
        f"Move whatever they need into accesslink, or invert the dependency."
    )


def test_absolute_import_of_the_connector_is_flagged() -> None:
    upward = _upward_reaching_imports("import dreeve_polar_connector\n", parts=0)
    assert upward == {"dreeve_polar_connector"}


def test_absolute_import_from_the_connector_is_flagged() -> None:
    upward = _upward_reaching_imports("from dreeve_polar_connector.config import Config\n", parts=0)
    assert upward == {"dreeve_polar_connector.config"}


def test_import_of_a_sibling_module_inside_accesslink_is_safe() -> None:
    upward = _upward_reaching_imports("from dreeve_polar_connector.accesslink.models import Session\n", parts=0)
    assert upward == set()


def test_level_1_relative_import_is_safe() -> None:
    # `from . import x` from a file directly inside accesslink/ (parts=0) resolves to accesslink itself.
    upward = _upward_reaching_imports("from . import x\n", parts=0)
    assert upward == set()


def test_level_2_relative_import_from_the_package_root_is_flagged() -> None:
    # `from .. import x` from a file directly inside accesslink/ (parts=0) resolves to
    # dreeve_polar_connector - exactly the boundary this test exists to catch.
    upward = _upward_reaching_imports("from .. import x\n", parts=0)
    assert upward == {".."}


def test_level_2_relative_import_naming_a_module_is_flagged() -> None:
    upward = _upward_reaching_imports("from ..config import Config\n", parts=0)
    assert upward == {"..config"}


def test_level_2_relative_import_is_safe_one_directory_deeper() -> None:
    # A file in accesslink/sub/ (parts=1) can reach up to level 2 and stay inside accesslink.
    upward = _upward_reaching_imports("from .. import x\n", parts=1)
    assert upward == set()


def test_level_3_relative_import_is_flagged_one_directory_deeper() -> None:
    upward = _upward_reaching_imports("from ... import x\n", parts=1)
    assert upward == {"..."}


def test_dynamic_import_module_reaching_upward_is_flagged() -> None:
    upward = _upward_reaching_imports('importlib.import_module("dreeve_polar_connector.config")\n', parts=0)
    assert upward == {"dreeve_polar_connector.config"}


def test_dunder_import_reaching_upward_is_flagged() -> None:
    upward = _upward_reaching_imports('__import__("dreeve_polar_connector")\n', parts=0)
    assert upward == {"dreeve_polar_connector"}


def test_dynamic_import_of_something_inside_accesslink_is_safe() -> None:
    upward = _upward_reaching_imports('importlib.import_module("dreeve_polar_connector.accesslink.models")\n', parts=0)
    assert upward == set()


def test_dynamic_import_with_a_non_literal_argument_is_not_flagged() -> None:
    # Fully dynamic string construction can't be caught this way, and isn't worth chasing.
    upward = _upward_reaching_imports("importlib.import_module(name)\n", parts=0)
    assert upward == set()
