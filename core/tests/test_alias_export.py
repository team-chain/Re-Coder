"""나눠 만든 파일끼리 이름이 살짝 어긋난 경우(useCart ↔ useCartStore)를 생성 직후 자동으로 맞춘다."""
import code_agent as ca
from build_readiness import add_missing_export, analyze

STORE = """import { create } from 'zustand';
const useCartStore = create((set) => ({ items: [], add: (p) => set((s) => ({ items: [...s.items, p] })) }));
export default useCartStore;
"""
PAGE = """import React from 'react';
import { useCart } from '../store/cartStore';
export default function CartPage() { const { items } = useCart(); return <div>{items.length}</div>; }
"""
PKG = '{"name":"c","type":"module","scripts":{"build":"vite build"},"dependencies":{"react":"18","zustand":"4"},"devDependencies":{"vite":"5"}}'


def test_짝이_하나면_별칭으로_내보낸다():
    out = add_missing_export(STORE, "useCart", "esm")
    assert out is not None and out.rstrip().endswith("export { useCartStore as useCart };")


def test_짝이_여럿이거나_다른_것이면_추측하지_않는다():
    assert add_missing_export("export const useCartStore = 1;\nexport const useCartState = 2;\n", "useCart", "esm") is None
    #: useCart 와 CartContext 는 다른 값이다
    assert add_missing_export("export const CartContext = 1;\n", "useCart", "esm") is None
    assert add_missing_export("export const cart = 1;\n", "useCartItems", "esm") is None


def test_생성_직후_자동_교정이_맞추고_남은_문제가_없다(tmp_path):
    ops = [
        {"action": "create", "file": "client/package.json", "content": PKG},
        {"action": "create", "file": "client/src/store/cartStore.js", "content": STORE},
        {"action": "create", "file": "client/src/pages/CartPage.jsx", "content": PAGE},
    ]
    fixed, notes = ca._autofix_ops(tmp_path, "", ops)
    store = next(o for o in fixed if o["file"].endswith("cartStore.js"))["content"]
    assert "export { useCartStore as useCart };" in store
    assert any("useCart" in n for n in notes)
    issues = ca._consistency_issues(tmp_path, "", fixed)
    assert not [i for i in issues if i["code"] == "NODE_IMPORT_NAME_MISSING"]


def test_확실한_것만이라도_고치고_나머지는_문제로_남긴다(tmp_path):
    overlay = {
        "package.json": PKG,
        "src/store/cartStore.js": STORE,
        "src/pages/CartPage.jsx": PAGE.replace("import { useCart }", "import { useCart, useWishlist }"),
    }
    r = analyze(tmp_path, overlay, dockerfile=None)
    issue = next(i for i in r.issues if i.code == "NODE_IMPORT_NAME_MISSING")
    assert not issue.auto_fix
    assert r.fix_data["missing_exports"] == [("src/store/cartStore.js", "useCart", "esm")]
