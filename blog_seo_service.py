# -*- coding: utf-8 -*-
"""
업클릭 — 네이버 블로그 '글 진단' 엔진
======================================
엔드포인트 (main.py 에 /blog prefix 로 마운트됨)
  GET  /blog/health       → 헬스체크
  POST /blog/diagnose     → {url} 또는 {title, body} → 글진단 JSON
"""

import re
from collections import Counter
from typing import List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from fastapi import APIRouter
from pydantic import BaseModel
from kiwipiepy import Kiwi

router = APIRouter(prefix="/blog", tags=["blog-seo"])

# ==================================================================
# 1) 형태소 분석기 (전역 1회 초기화)
# ==================================================================
kiwi = Kiwi()
USER_WORDS = [
    ("비트코인", "NNP"), ("이더리움", "NNP"), ("반감기", "NNG"),
    ("엔비디아", "NNP"), ("팔란티어", "NNP"), ("테슬라", "NNP"),
    ("빅블록", "NNP"), ("빅테크", "NNG"), ("나스닥", "NNP"),
    ("배당주", "NNG"), ("성장주", "NNG"), ("점유율", "NNG"),
]
for _w, _t in USER_WORDS:
    kiwi.add_user_word(_w, _t)

NOUN_TAGS = {"NNG", "NNP"}
NUM_TAGS = {"SN"}


def korean_char_count(text: str) -> int:
    return len(re.findall(r"[가-힣]", text))


# ==================================================================
# 2) 블로그 본문 가져오기 (모바일 우선)
# ==================================================================
CRAWL_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                   "Version/17.0 Mobile/15E148 Safari/604.1"),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Referer": "https://m.blog.naver.com/",
}


def _to_mobile_url(url: str) -> str:
    url = url.strip()
    m = re.search(r"blog\.naver\.com/([^/?]+)/(\d+)", url)
    if m:
        return f"https://m.blog.naver.com/{m.group(1)}/{m.group(2)}"
    m = re.search(r"blogId=([^&]+).*logNo=(\d+)", url)
    if m:
        return f"https://m.blog.naver.com/{m.group(1)}/{m.group(2)}"
    if "blog.naver.com" in url and "m.blog.naver.com" not in url:
        return url.replace("blog.naver.com", "m.blog.naver.com")
    return url


def fetch_naver_blog(url: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        r = requests.get(_to_mobile_url(url), headers=CRAWL_HEADERS, timeout=20)
    except requests.RequestException as e:
        return None, f"요청 실패: {e}"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code} — 차단되었거나 로그인 전용 글일 수 있습니다."
    r.encoding = "utf-8"
    return r.text, None


def extract_blog_content(html: str) -> Tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = og["content"].strip()
    if not title:
        t = soup.select_one(".se-title-text, .pcol1, h3.tit_h3")
        if t:
            title = t.get_text(strip=True)
    body = ""
    for sel in [".se-main-container", "#postViewArea", ".se-content-area", "article", ".post_ct"]:
        node = soup.select_one(sel)
        if node:
            body = node.get_text("\n", strip=True)
            if len(body) > 200:
                break
    if len(body) < 200:
        chunks = [p.get_text(" ", strip=True) for p in soup.find_all(["p", "div"])]
        body = "\n".join(c for c in chunks if len(c) > 30)
    return title, body


# ==================================================================
# 3) SEO 진단
# ==================================================================
def analyze(title: str, body: str) -> dict:
    tokens = kiwi.tokenize(body)
    nouns = [t.form for t in tokens if t.tag in NOUN_TAGS and len(t.form) > 1]
    noun_freq = Counter(nouns)
    total_nouns = len(nouns) or 1
    top = noun_freq.most_common(20)
    main_kw = top[0][0] if top else ""
    main_density = (top[0][1] / total_nouns * 100) if top else 0.0

    num_tokens = [t.form for t in tokens if t.tag in NUM_TAGS]
    pct_count = len(re.findall(r"\d+(?:\.\d+)?\s*[%％]", body))
    money_count = len(re.findall(r"\d[\d,]*\s*만?\s*(?:원|달러|억|조)", body))

    first_para = body[:200]
    kw_in_title = bool(main_kw and main_kw in title)
    kw_in_first = bool(main_kw and main_kw in first_para)
    paras = [p for p in re.split(r"\n+", body) if p.strip()]
    avg_para_len = sum(korean_char_count(p) for p in paras) / (len(paras) or 1)
    han = korean_char_count(body)

    checks: List[Tuple[str, str]] = []
    if han < 1000:
        checks.append(("warn", f"글자수 {han}자 — 권장(1,000~1,500자)보다 짧습니다."))
    elif han > 1800:
        checks.append(("warn", f"글자수 {han}자 — 다소 깁니다. 1,000~1,500자로 압축하세요."))
    else:
        checks.append(("ok", f"글자수 {han}자 — 권장 범위에 적절합니다."))

    if main_density > 5:
        checks.append(("bad", f"핵심 키워드 '{main_kw}' 밀도 {main_density:.1f}% — 과도(>5%). 도배 위험."))
    elif main_density < 1.5:
        checks.append(("warn", f"핵심 키워드 '{main_kw}' 밀도 {main_density:.1f}% — 다소 낮습니다."))
    else:
        checks.append(("ok", f"핵심 키워드 '{main_kw}' 밀도 {main_density:.1f}% — 자연스럽습니다."))

    checks.append(("ok", f"제목에 핵심 키워드 '{main_kw}' 포함됨.") if kw_in_title
                  else ("warn", f"제목에 핵심 키워드 '{main_kw}'가 없습니다."))
    checks.append(("ok", f"첫 문단에 핵심 키워드 '{main_kw}' 포함됨 — 역피라미드 적합.") if kw_in_first
                  else ("warn", f"첫 문단에 핵심 키워드 '{main_kw}'가 없습니다."))

    num_total = len(num_tokens) + pct_count + money_count
    checks.append(("ok", f"수치 표현 {num_total}개 — AI 인용에 유리합니다.") if num_total >= 5
                  else ("warn", f"수치 표현 {num_total}개 — 부족. 구체적 수치를 더하세요."))
    checks.append(("warn", f"문단 평균 {avg_para_len:.0f}자 — 모바일엔 깁니다. 2~4줄로 끊으세요.") if avg_para_len > 120
                  else ("ok", f"문단 평균 {avg_para_len:.0f}자 — 모바일 가독성 적절."))

    weight = {"ok": 1.0, "warn": 0.5, "bad": 0.0}
    score = round(sum(weight[c[0]] for c in checks) / len(checks) * 100)

    return {
        "title": title,
        "han": han,
        "total_morph": len(tokens),
        "total_nouns": len(nouns),
        "unique_nouns": len(noun_freq),
        "main_kw": main_kw,
        "main_density": round(main_density, 1),
        "score": score,
        "checks": checks,
        "top": top,
        "hashtags": [w for w, _ in top[1:11]],
    }


# ==================================================================
# 4) 엔드포인트
# ==================================================================
class DiagnoseReq(BaseModel):
    url: Optional[str] = None
    title: Optional[str] = None
    body: Optional[str] = None


@router.get("/health")
def blog_health():
    return {"ok": True, "service": "blog-seo", "version": "1.0"}


@router.post("/diagnose")
def diagnose(req: DiagnoseReq):
    if req.body and korean_char_count(req.body) >= 150:
        title = (req.title or "").strip()
        body = req.body
    elif req.url:
        html, err = fetch_naver_blog(req.url)
        if err:
            return {"ok": False, "error": err}
        title, body = extract_blog_content(html)
        if korean_char_count(body) < 150:
            return {"ok": False,
                    "error": "본문을 충분히 추출하지 못했습니다. 로그인 전용 글이거나 구조가 다를 수 있습니다."}
    else:
        return {"ok": False, "error": "url 또는 body 중 하나는 필요합니다."}

    data = analyze(title, body)
    data["ok"] = True
    return data
