from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from jd_product import _best_product_result, parse_product_html


URL = "https://item.jd.com/123456.html"
TARGET_IMAGE = "https://img10.360buyimg.com/img/target.jpg"
RECOMMENDATION = "https://img10.360buyimg.com/n1/s800x800/recommended.jpg"


def page(*, title: str = "目标草莓饼干100g", image: str = TARGET_IMAGE, body: str = "") -> str:
    return f'<meta property="og:title" content="{title}"><meta property="og:image" content="{image}">{body}'


def test_high_resolution_recommendation_cannot_replace_or_supplement_target() -> None:
    result = parse_product_html(page(body=f'<div>为你推荐<img src="{RECOMMENDATION}"></div>'), URL)
    assert result["main_image_url"] == TARGET_IMAGE
    assert result["image_urls"] == [TARGET_IMAGE]
    assert result["image_evidence"][0]["item_id"] == "123456"


def test_unscoped_image_on_page_is_not_a_target_image() -> None:
    result = parse_product_html(page(image="", body=f'<img src="{RECOMMENDATION}">'), URL)
    assert not result["main_image_url"]
    assert result["image_urls_scope"] == "unconfirmed"


def test_scoped_gallery_survives_placeholder_and_excludes_recommendation() -> None:
    body = f'<div id="spec-n1"><img src="{TARGET_IMAGE}"><div class="recommend"><img src="{RECOMMENDATION}"></div></div>'
    result = parse_product_html(page(image="https://img10.360buyimg.com/imagetools/placeholder.png", body=body), URL)
    assert result["image_urls"] == [TARGET_IMAGE]
    assert result["image_urls_scope"] == "product_page"


@pytest.mark.parametrize("invalid_image", ["https://[broken/image.jpg", "https://not-jd.example/360buyimg.com/a.jpg", "https://img10.360buyimg.com/logo.jpg"])
def test_bad_metadata_image_does_not_prevent_scoped_gallery_fallback(invalid_image: str) -> None:
    result = parse_product_html(page(image=invalid_image, body=f'<div id="spec-n1"><img src="{TARGET_IMAGE}"></div>'), URL)
    assert result["image_urls"] == [TARGET_IMAGE]


@pytest.mark.parametrize("container", ["array", "graph"])
def test_jsonld_images_are_bound_to_exact_sku(container: str) -> None:
    nodes = [
        {"@type": "Product", "sku": "654321", "image": RECOMMENDATION},
        {"@type": "Product", "sku": "123456", "image": [TARGET_IMAGE, {"contentUrl": TARGET_IMAGE + "?back=1"}]},
    ]
    data = nodes if container == "array" else {"@graph": nodes}
    result = parse_product_html(page(image=RECOMMENDATION, body=f'<script type="application/ld+json">{json.dumps(data)}</script>'), URL)
    assert result["main_image_url"] == TARGET_IMAGE
    assert result["image_urls"] == [TARGET_IMAGE, TARGET_IMAGE + "?back=1"]
    assert result["image_urls_scope"] == "selected_sku"


def test_unidentified_jsonld_recommendation_is_not_used() -> None:
    data = {"@type": "Product", "name": "推荐商品", "image": RECOMMENDATION}
    result = parse_product_html(page(image="", body=f'<script type="application/ld+json">{json.dumps(data)}</script>'), URL)
    assert not result["image_urls"]


@pytest.mark.parametrize("binding", ['<meta property="og:url" content="https://item.jd.com/654321.html">', '<link rel="canonical" href="https://item.jd.com/654321.html">'])
def test_conflicting_page_identity_blocks_its_image(binding: str) -> None:
    result = parse_product_html(page(body=binding), URL)
    assert not result["image_urls"]
    assert result["target_errors"]


def test_fallback_chooses_one_coherent_page_without_mixing_images() -> None:
    generic = parse_product_html(page(title="京东", image=RECOMMENDATION), URL)
    target = parse_product_html(page(), URL)
    wrong = parse_product_html(page(title="不属于请求SKU的草莓饼干100g×6袋", image=RECOMMENDATION), "https://item.jd.com/654321.html")
    result = _best_product_result([generic, target, wrong])
    assert result == target


def test_mobile_product_identity_is_preserved() -> None:
    result = parse_product_html(page(), "https://item.m.jd.com/product/123456.html")
    assert result["item_id"] == "123456"
    assert result["main_image_url"] == TARGET_IMAGE


@pytest.mark.parametrize("state_sku", ["123456", "654321", "conflicting", "canonical", "og_url", "no_state"])
def test_browser_images_keep_sku_binding_and_exclude_recommendations(state_sku: str) -> None:
    from agent.builtin_tools import _JD_BROWSER_EXTRACT_JS

    node = shutil.which("node")
    if not node:
        pytest.skip("browser extraction JavaScript regression requires Node.js")
    state = {"sku": state_sku, "image": [TARGET_IMAGE + "?state=1", {"contentUrl": TARGET_IMAGE + "?back=1"}]}
    if state_sku == "conflicting":
        state.update(sku="123456", skuId="654321")
    if state_sku in {"canonical", "og_url"}:
        state["sku"] = "123456"
    if state_sku == "no_state":
        state = {}
    setup = """
global.location = {href: 'https://b2b.jd.com/goods/goods-detail/123456', protocol: 'https:'};
global.window = {pageConfig: {product: STATE}};
const images = [
  {currentSrc: 'RECOMMENDATION', closest: () => ({})},
  {currentSrc: 'TARGET_IMAGE', closest: () => null},
];
global.document = {
  body: {innerText: ''}, documentElement: {outerHTML: ''}, title: '目标商品',
  querySelector: selector => selector === 'h1' ? {innerText: '目标草莓饼干100g'} :
    selector === 'link[rel="canonical"]' && CASE === 'canonical' ? {href: 'https://item.jd.com/654321.html'} :
    selector === 'meta[property="og:url"]' && CASE === 'og_url' ? {content: 'https://item.jd.com/654321.html'} : null,
  querySelectorAll: selector => selector.startsWith('#spec-n1') ? images : [],
};
""".replace("STATE", json.dumps(state)).replace("CASE", json.dumps(state_sku)).replace("RECOMMENDATION", RECOMMENDATION).replace("TARGET_IMAGE", TARGET_IMAGE)
    completed = subprocess.run([node, "-e", setup + "\nprocess.stdout.write(" + _JD_BROWSER_EXTRACT_JS + ");"], capture_output=True, text=True, encoding="utf-8", timeout=10, check=True)
    product = json.loads(completed.stdout)
    if state_sku == "123456":
        assert product["image_urls"] == state["image"][:1] + [state["image"][1]["contentUrl"]]
        assert product["image_urls_scope"] == "selected_sku"
    elif state_sku == "no_state":
        assert product["image_urls"] == [TARGET_IMAGE]
        assert product["image_urls_scope"] == "product_page"
    else:
        assert product["image_urls"] == []
        assert product["image_urls_scope"] == "unconfirmed"
        assert product["target_errors"]
    assert RECOMMENDATION not in product["image_urls"]
