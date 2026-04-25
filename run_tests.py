from __future__ import annotations

import importlib
import inspect
import shutil
import sys
import traceback
from pathlib import Path


def _project_root() -> Path:
    return Path(__file__).resolve().parent


def _build_tmp_path(base: Path, test_name: str) -> Path:
    path = base / test_name
    path.mkdir(parents=True, exist_ok=True)
    return path


def main() -> int:
    root = _project_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    module = importlib.import_module("tests.test_tradebot")
    base_tmp = root / ".codex_tmp_test_runner"
    shutil.rmtree(base_tmp, ignore_errors=True)
    base_tmp.mkdir(parents=True, exist_ok=True)

    results: list[tuple[str, str, str]] = []
    for name, fn in sorted(vars(module).items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        kwargs = {}
        signature = inspect.signature(fn)
        if "tmp_path" in signature.parameters:
            kwargs["tmp_path"] = _build_tmp_path(base_tmp, name)
        try:
            fn(**kwargs)
            results.append((name, "passed", ""))
        except Exception as exc:  # noqa: BLE001
            detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            results.append((name, "failed", detail))

    failures = [item for item in results if item[1] == "failed"]
    passed = len(results) - len(failures)
    print(f"Passed: {passed}")
    print(f"Failed: {len(failures)}")
    if failures:
        print()
        for name, _, detail in failures:
            print(f"FAILED {name}")
            print(detail.rstrip())
            print()
        return 1

    print()
    print("All tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
