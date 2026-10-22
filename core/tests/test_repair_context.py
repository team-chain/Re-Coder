"""Error context must retain relevant tests without mixing independent packages."""

from __future__ import annotations

from pathlib import Path

import pytest
from grounded_repair.context import context_order
from grounded_repair.workspace import manifest, select_context


def workspace(root: Path, contents: dict[str, str]) -> dict[str, str]:
    for name, text in contents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return manifest(root)


@pytest.mark.parametrize(
    "source,test",
    [
        ("src/calc.py", "tests/test_calc.py"),
        ("src/calc.py", "tests/calc_test.py"),
        ("src/Cart.tsx", "tests/Cart.test.tsx"),
        ("src/Cart.jsx", "tests/Cart.spec.jsx"),
    ],
)
def test_related_tests_survive_a_crowded_context_budget(tmp_path, source, test):
    contents = {f"a_unrelated_{i}.py": "unrelated = True" for i in range(25)}
    contents.update({source: "broken", test: "expected behavior", "package.json": "{}"})
    files = workspace(tmp_path, contents)
    chosen = select_context(tmp_path, files, f"Failure at {source}:17:2")
    assert list(chosen)[:2] == [source, test]
    assert len(chosen) == 12
    assert "package.json" in chosen


def test_failed_test_brings_its_source_before_other_files(tmp_path):
    contents = {f"a_{i}.py": "unrelated" for i in range(20)}
    contents.update({"src/calc.py": "source", "tests/test_calc.py": "test"})
    files = workspace(tmp_path, contents)
    chosen = select_context(tmp_path, files, "FAILED tests/test_calc.py::test_add")
    assert list(chosen)[:2] == ["tests/test_calc.py", "src/calc.py"]


def test_monorepo_does_not_mix_another_apps_matching_test(tmp_path):
    files = workspace(
        tmp_path,
        {
            "package.json": "{}",
            "Dockerfile": "FROM scratch",
            "api/package.json": "{}",
            "api/src/cart.ts": "api source",
            "api/tests/cart.test.ts": "api test",
            "shop/package.json": "{}",
            "shop/src/cart.ts": "shop source",
            "shop/tests/cart.test.ts": "shop test",
        },
    )
    chosen = select_context(tmp_path, files, "at /app/api/src/cart.ts:7:1")
    assert list(chosen)[:2] == ["api/src/cart.ts", "api/tests/cart.test.ts"]
    assert "api/package.json" in chosen and "package.json" in chosen
    assert not any(name.startswith("shop/") for name in chosen)


def test_windows_log_paths_and_longest_match():
    files = dict.fromkeys(["app.ts", "api/src/app.ts", "api/package.json"], "hash")
    order = context_order(files, r"C:\project\api\src\app.ts:2:1")
    assert order[0] == "api/src/app.ts"
    assert "app.ts" not in order


def test_ambiguous_bare_name_does_not_choose_a_package():
    files = dict.fromkeys(["a/package.json", "a/app.ts", "b/package.json", "b/app.ts"], "hash")
    order = context_order(files, "Failure in app.ts:1")
    assert order[:2] == ["a/package.json", "b/package.json"]
    assert "a/app.ts" in order and "b/app.ts" in order


def test_filename_substrings_do_not_count_as_references():
    files = dict.fromkeys(["package.json", "x.ts", "src/xx.ts", "tests/x.test.ts"], "hash")
    order = context_order(files, "unknown/xxx.ts:1")
    assert order[0] == "package.json"


@pytest.mark.parametrize(
    "log",
    [
        "at /app/node_modules/lib/src/x.ts:1",
        "at /env/site-packages/src/x.ts:1",
        "at /unknown/other/x.ts:1",
    ],
)
def test_dependency_or_unknown_paths_do_not_select_a_same_named_file(log):
    files = dict.fromkeys(["package.json", "src/x.ts", "tests/x.test.ts"], "hash")
    assert context_order(files, log)[0] == "package.json"


def test_unique_bare_filename_can_identify_a_nested_source():
    files = dict.fromkeys(["package.json", "src/calc.py", "tests/test_calc.py"], "hash")
    assert context_order(files, 'File "calc.py", line 7')[:2] == [
        "src/calc.py",
        "tests/test_calc.py",
    ]


def test_context_still_bounds_size_and_excludes_secrets(tmp_path):
    files = workspace(
        tmp_path,
        {
            "src/app.py": "broken",
            "tests/test_app.py": "x" * 12001,
            "config.json": "password=superprivate",
            "a.py": "a" * 11900,
            "b.py": "b" * 11900,
            "c.py": "c" * 11900,
        },
    )
    chosen = select_context(tmp_path, files, "src/app.py:1")
    assert "src/app.py" in chosen
    assert "tests/test_app.py" not in chosen and "config.json" not in chosen
    assert sum(map(len, chosen.values())) <= 24000


def test_no_reference_order_is_stable_across_manifest_order():
    names = ["z.py", "a.tsx", "Dockerfile", "package.json", "photo.png"]
    forward = context_order(dict.fromkeys(names, "hash"), "Unknown error")
    backward = context_order(dict.fromkeys(reversed(names), "hash"), "Unknown error")
    assert forward == backward == ["Dockerfile", "package.json", "a.tsx", "z.py"]
