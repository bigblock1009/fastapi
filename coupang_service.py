# -*- coding: utf-8 -*-
"""
업클릭 — 쿠팡 상품 정보 추출기
================================
상품 URL(또는 productId/itemId) → 상품명 / 가격 / 썸네일 이미지 URL

엔드포인트 (main.py 에 /coupang prefix 로 마운트됨)
  GET  /coupang/health    → 헬스체크
  POST /coupang/product   → {url} 또는 {product_id} → 상품 정보 JSON

추출 전략 (위에서부터 시도, 먼저 성공한 값을 채택)
  1) JSON-LD (<script type="application/ld+json">) 의 Product 스키마
  2) OG/메타 태그 (og:title, og:image, product:price:amount)
  3) 본문 DOM 셀렉터 (.prod-buy-header__title, .total-price ...)
  4) 인라인 JS 객체 정규식 ("salePrice": 12900 등)
  쿠팡은 DOM 클래스명을 자주 바꾸므로 1~2번이 안정적이고, 3~4번은 폴백이다.
  debug=true 로 요청하면 어느 전략이 값을 냈는지 sources 에 담아 돌려준다.

주의
  - 쿠팡은 봇 트래픽을 차단한다. 헤더 위장 + 쿠키 워밍업으로 대부분 통과하지만
    실패할 수 있고, 그때는 render=true (Playwright) 로 재시도한다.
    Playwright 는 배포 이미지를 수백 MB 키우므로 requirements.txt 에 넣지 않았다.
    필요한 곳에서만:  pip install playwright && playwright install chromium
  - 쿠팡 robots.txt 는 /vp/products/ 크롤링을 제한한다. 지속적·상업적 수집은
    쿠팡 파트너스 Open API 를 사용할 것.
"""

import json
import re
from typing import Any, Dict, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter(prefix="/coupang", tags=["coupang"])


# ==================================================================
# 1) URL 파싱
# ==================================================================
PRODUCT_ID_RE = re.compile(r"/vp/products/(\d+)")
QS_ITEM_ID_RE = re.compile(r"[?&]itemId=(\d+)")
QS_VENDOR_ITEM_ID_RE = re.compile(r"[?&]vendorItemId=(\d+)")


def parse_product_url(url: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """상품 URL 에서 (productId, itemId, vendorItemId) 를 뽑는다. 없으면 None."""
    url = (url or "").strip()
    pid = PRODUCT_ID_RE.search(url)
    iid = QS_ITEM_ID_RE.search(url)
    vid = QS_VENDOR_ITEM_ID_RE.search(url)
    return (pid.group(1) if pid else None,
            iid.group(1) if iid else None,
            vid.group(1) if vid else None)


def build_product_url(product_id: str,
                      item_id: Optional[str] = None,
                      vendor_item_id: Optional[str] = None) -> str:
    """추적 파라미터를 걷어낸 정규 상품 URL 을 만든다."""
    qs = []
    if item_id:
        qs.append(f"itemId={item_id}")
    if vendor_item_id:
        qs.append(f"vendorItemId={vendor_item_id}")
    url = f"https://www.coupang.com/vp/products/{product_id}"
    return url + ("?" + "&".join(qs) if qs else "")


# ==================================================================
# 2) 페이지 가져오기
# ==================================================================
# 쿠팡은 User-Agent 만 바꾼 요청을 잘 걸러낸다. 실제 크롬이 보내는 헤더 세트를
# 최대한 맞춰야 통과율이 올라간다. (br 은 brotli 미설치 환경에서 깨지므로 제외)
BASE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/131.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
}

BLOCK_MARKERS = (
    "Access Denied", "Request Rejected", "비정상적인 접근",
    "잠시 후 다시 시도", "Service Unavailable",
)


def _looks_blocked(html: str) -> bool:
    """차단/에러 페이지인지 대략 판별. 상품 페이지는 항상 수십 KB 이상이다."""
    if len(html) < 5000:
        return True
    return any(m in html[:4000] for m in BLOCK_MARKERS)


def fetch_product_html(url: str, timeout: int = 20) -> Tuple[Optional[str], Optional[str]]:
    """requests 로 상품 페이지 HTML 을 받아온다. (html, error) 반환."""
    session = requests.Session()
    session.headers.update(BASE_HEADERS)

    # 메인 페이지를 먼저 찍어 PCID 등 세션 쿠키를 확보한다. 쿠키 없이 상품
    # 페이지를 바로 때리면 차단 페이지가 돌아오는 경우가 많다.
    try:
        session.get("https://www.coupang.com/", timeout=timeout)
    except requests.RequestException:
        pass  # 워밍업 실패는 치명적이지 않다. 본 요청은 그대로 진행.

    try:
        r = session.get(url, headers={"Referer": "https://www.coupang.com/"},
                        timeout=timeout)
    except requests.RequestException as e:
        return None, f"요청 실패: {e}"

    if r.status_code != 200:
        return None, (f"HTTP {r.status_code} — 쿠팡 봇 차단일 수 있습니다. "
                      f"render=true 로 재시도해 보세요.")

    r.encoding = "utf-8"
    html = r.text
    if _looks_blocked(html):
        return None, "차단 페이지가 반환되었습니다. render=true 로 재시도해 보세요."
    return html, None


def render_product_html(url: str, timeout: int = 30) -> Tuple[Optional[str], Optional[str]]:
    """Playwright 로 실제 브라우저 렌더링. 헤더 위장이 막혔을 때의 폴백."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, ("playwright 가 설치되어 있지 않습니다. "
                      "pip install playwright && playwright install chromium")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            ctx = browser.new_context(
                user_agent=BASE_HEADERS["User-Agent"],
                locale="ko-KR",
                viewport={"width": 1440, "height": 900},
            )
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            # 메타 태그가 붙을 때까지 잠깐 기다린다. 없어도 그냥 진행.
            try:
                page.wait_for_selector("meta[property='og:title']", timeout=5000)
            except Exception:
                pass
            html = page.content()
            browser.close()
        return html, None
    except Exception as e:
        return None, f"렌더링 실패: {e}"


# ==================================================================
# 3) 값 정규화
# ==================================================================
TITLE_SUFFIX_RE = re.compile(r"\s*[-|]\s*쿠팡!?\s*$")

# coupangcdn 은 경로에 썸네일 크기가 박혀 있다:
#   //thumbnail10.coupangcdn.com/thumbnails/remote/230x230ex/image/retail/images/....jpg
# 이 세그먼트를 정사각형 큰 사이즈로 갈아끼워 대표 썸네일 1장을 만든다.
SIZE_SEG_RE = re.compile(r"/\d{2,4}x\d{2,4}(?:ex)?/")
THUMB_SIZE = 492

# 로고/아이콘/배너 따위를 상품 이미지로 오인하지 않기 위한 필터
PRODUCT_PATH_HINTS = ("/thumbnails/", "/image/retail/", "/image/vendor")
NON_PRODUCT_HINTS = ("logo", "icon", "sprite", "banner", "badge", "btn_",
                     "bg_", "placeholder", "blank", "dummy")


def clean_name(name: Optional[str]) -> Optional[str]:
    """공백 정리 + 뒤에 붙는 ' - 쿠팡!' 꼬리표 제거."""
    if not name:
        return None
    name = re.sub(r"\s+", " ", str(name)).strip()
    name = TITLE_SUFFIX_RE.sub("", name).strip()
    return name or None


def parse_price(value: Any) -> Optional[int]:
    """'12,900원' / '12900' / 12900.0 → 12900. 못 읽으면 None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) or None
    m = re.search(r"\d[\d,]*", str(value))
    if not m:
        return None
    try:
        n = int(m.group(0).replace(",", ""))
    except ValueError:
        return None
    return n or None


def normalize_image_url(url: Optional[str], square: bool = True) -> Optional[str]:
    """프로토콜 상대경로(//...) 보정 + 크기 세그먼트를 정사각형 대표 썸네일로 교체."""
    if not url:
        return None
    url = str(url).strip()
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = "https://www.coupang.com" + url
    if square:
        # 원본이 230x230ex 든 320x180 이든 항상 정사각형 한 장으로 통일한다.
        # 크기 세그먼트가 없는 URL(벤더 원본 등)은 그대로 둔다.
        url = SIZE_SEG_RE.sub(f"/{THUMB_SIZE}x{THUMB_SIZE}ex/", url, count=1)
    return url


def is_product_image(url: Optional[str]) -> bool:
    """상품 이미지로 보이는 URL 인지. 정규식 폴백이 로고를 집는 걸 막는다."""
    if not url:
        return False
    low = url.lower()
    if any(bad in low for bad in NON_PRODUCT_HINTS):
        return False
    return any(hint in low for hint in PRODUCT_PATH_HINTS)


# ==================================================================
# 4) 추출
# ==================================================================
NAME_SELECTORS = [
    "h1.prod-buy-header__title",
    "h2.prod-buy-header__title",
    ".prod-buy-header__title",
    "h1.product-title",
    ".prod-title",
]
PRICE_SELECTORS = [
    ".prod-coupon-price .total-price strong",
    ".prod-sale-price .total-price strong",
    ".prod-price .total-price strong",
    ".total-price strong",
    ".total-price",
    ".price-value",
    ".prod-price-value",
]
IMAGE_SELECTORS = [
    "img.prod-image__detail",
    ".prod-image__detail img",
    ".prod-image__item img",
    ".prod-image img",
]
PRICE_JSON_RES = [
    re.compile(r'"couponPrice"\s*:\s*"?([0-9,]+)"?'),
    re.compile(r'"salePrice"\s*:\s*"?([0-9,]+)"?'),
    re.compile(r'"finalPrice"\s*:\s*"?([0-9,]+)"?'),
    re.compile(r'"price"\s*:\s*"?([0-9,]+)"?'),
]
CDN_IMAGE_RE = re.compile(
    r'(?:https?:)?//[a-z0-9.\-]*coupangcdn\.com/[^\s"\'<>]+?\.(?:jpg|jpeg|png|webp)',
    re.I,
)


def _iter_jsonld(soup: BeautifulSoup):
    """페이지 안의 모든 JSON-LD 노드를 평평하게 훑는다(@graph 포함)."""
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = (tag.string or tag.get_text() or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            graph = node.get("@graph")
            if isinstance(graph, list):
                for sub in graph:
                    if isinstance(sub, dict):
                        yield sub
            yield node


def _from_jsonld(soup: BeautifulSoup):
    """JSON-LD 의 Product 스키마에서 (name, price, image) 를 뽑는다."""
    for node in _iter_jsonld(soup):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if "Product" not in types:
            continue

        image = node.get("image")
        if isinstance(image, list):
            image = image[0] if image else None
        if isinstance(image, dict):
            image = image.get("url")

        price = None
        offers = node.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else None
        if isinstance(offers, dict):
            price = offers.get("price") or offers.get("lowPrice")

        return node.get("name"), price, image
    return None, None, None


def _meta(soup: BeautifulSoup, prop: str) -> Optional[str]:
    tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
    if tag and tag.get("content"):
        return tag["content"].strip()
    return None


def extract_product(html: str) -> Dict[str, Any]:
    """HTML → {name, price, thumbnail, sources}. 못 찾은 값은 None."""
    soup = BeautifulSoup(html, "html.parser")
    found: Dict[str, Any] = {"name": None, "price": None, "image": None}
    sources: Dict[str, str] = {}

    def take(field: str, value: Any, origin: str) -> None:
        """이미 채워진 필드는 덮어쓰지 않는다 — 먼저 성공한 전략이 이긴다."""
        if value not in (None, "") and found[field] is None:
            found[field] = value
            sources[field] = origin

    # 1) JSON-LD
    ld_name, ld_price, ld_image = _from_jsonld(soup)
    take("name", clean_name(ld_name), "json-ld")
    take("price", parse_price(ld_price), "json-ld")
    take("image", normalize_image_url(ld_image), "json-ld")

    # 2) OG / 메타 태그
    take("name", clean_name(_meta(soup, "og:title")), "og:title")
    take("price", parse_price(_meta(soup, "product:price:amount")
                              or _meta(soup, "og:price:amount")), "og:price")
    take("image", normalize_image_url(_meta(soup, "og:image")), "og:image")

    # 3) 본문 DOM 셀렉터 — 쿠팡이 자주 바꾸는 부분이라 폴백으로만 쓴다
    if found["name"] is None:
        for sel in NAME_SELECTORS:
            node = soup.select_one(sel)
            if node:
                take("name", clean_name(node.get_text(" ", strip=True)), f"dom:{sel}")
                break
    if found["price"] is None:
        for sel in PRICE_SELECTORS:
            node = soup.select_one(sel)
            if node and parse_price(node.get_text(" ", strip=True)):
                take("price", parse_price(node.get_text(" ", strip=True)), f"dom:{sel}")
                break
    if found["image"] is None:
        for sel in IMAGE_SELECTORS:
            node = soup.select_one(sel)
            if not node:
                continue
            src = node.get("src") or node.get("data-src") or ""
            if not src and node.get("srcset"):
                src = node["srcset"].split()[0]
            if src:
                take("image", normalize_image_url(src), f"dom:{sel}")
                break

    # 4) 인라인 JS 객체 정규식 — DOM 까지 실패했을 때의 마지막 수단
    if found["price"] is None:
        for rx in PRICE_JSON_RES:
            m = rx.search(html)
            if m and parse_price(m.group(1)):
                take("price", parse_price(m.group(1)), "regex:inline-json")
                break
    if found["image"] is None:
        for m in CDN_IMAGE_RE.finditer(html):
            candidate = normalize_image_url(m.group(0))
            if is_product_image(candidate):
                take("image", candidate, "regex:coupangcdn")
                break
    if found["name"] is None and soup.title:
        take("name", clean_name(soup.title.get_text(strip=True)), "title")

    return {
        "name": found["name"],
        "price": found["price"],
        "thumbnail": found["image"],
        "sources": sources,
    }


# ==================================================================
# 5) 엔드포인트
# ==================================================================
class ProductReq(BaseModel):
    url: Optional[str] = Field(None, description="쿠팡 상품 페이지 URL")
    product_id: Optional[str] = Field(None, description="url 대신 productId 직접 지정")
    item_id: Optional[str] = Field(None, description="옵션 상품 itemId")
    vendor_item_id: Optional[str] = Field(None, description="vendorItemId")
    render: bool = Field(False, description="True면 Playwright 브라우저 렌더링으로 가져온다")
    debug: bool = Field(False, description="True면 어느 전략에서 값을 얻었는지 함께 반환")


class ProductRes(BaseModel):
    ok: bool
    product_id: Optional[str] = None
    item_id: Optional[str] = None
    url: Optional[str] = None
    name: Optional[str] = None
    price: Optional[int] = None
    price_text: Optional[str] = None   # '12,900원' 형태의 표시용 문자열
    thumbnail: Optional[str] = None
    sources: Optional[Dict[str, str]] = None
    error: Optional[str] = None


@router.get("/health")
def coupang_health():
    return {"ok": True, "service": "coupang-product", "version": "1.0"}


@router.post("/product", response_model=ProductRes)
def get_product(req: ProductReq):
    if req.url:
        product_id, item_id, vendor_item_id = parse_product_url(req.url)
        if not product_id:
            return ProductRes(ok=False,
                              error="쿠팡 상품 URL 이 아닙니다. /vp/products/{id} 형식이어야 합니다.")
        item_id = item_id or req.item_id
        vendor_item_id = vendor_item_id or req.vendor_item_id
    elif req.product_id:
        product_id = req.product_id
        item_id, vendor_item_id = req.item_id, req.vendor_item_id
    else:
        return ProductRes(ok=False, error="url 또는 product_id 중 하나는 필요합니다.")

    url = build_product_url(product_id, item_id, vendor_item_id)
    html, err = (render_product_html(url) if req.render else fetch_product_html(url))
    if err:
        return ProductRes(ok=False, product_id=product_id, item_id=item_id,
                          url=url, error=err)

    data = extract_product(html)
    if data["name"] is None and data["price"] is None:
        return ProductRes(
            ok=False, product_id=product_id, item_id=item_id, url=url,
            error=("페이지는 받았지만 상품 정보를 찾지 못했습니다. "
                   "셀렉터가 바뀌었거나 차단 페이지일 수 있습니다."),
            sources=data["sources"] if req.debug else None,
        )

    return ProductRes(
        ok=True, product_id=product_id, item_id=item_id, url=url,
        name=data["name"],
        price=data["price"],
        price_text=f"{data['price']:,}원" if data["price"] else None,
        thumbnail=data["thumbnail"],
        sources=data["sources"] if req.debug else None,
    )


# ==================================================================
# 6) 로컬 검증용 CLI
#    쿠팡이 셀렉터를 바꿨는지 확인할 때 서버 없이 바로 돌려본다.
#      python coupang_service.py "<상품 URL>" [--render] [--dump out.html]
# ==================================================================
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print('사용법: python coupang_service.py "<쿠팡 상품 URL>" [--render] [--dump out.html]')
        raise SystemExit(1)

    target = sys.argv[1]
    pid, iid, vid = parse_product_url(target)
    full_url = build_product_url(pid, iid, vid) if pid else target

    page_html, fetch_err = (render_product_html(full_url) if "--render" in sys.argv
                            else fetch_product_html(full_url))
    if fetch_err:
        print(f"[실패] {fetch_err}")
        raise SystemExit(2)

    if "--dump" in sys.argv:
        dump_path = sys.argv[sys.argv.index("--dump") + 1]
        with open(dump_path, "w", encoding="utf-8") as fp:
            fp.write(page_html)
        print(f"[dump] {dump_path} ({len(page_html):,} bytes)")

    print(json.dumps(extract_product(page_html), ensure_ascii=False, indent=2))
