"""
업클릭 네이버 블로그 분석 - 형태소 분석 워커 (stateless)
=========================================================
역할: PHP(고도호스팅)가 블로그 본문을 보내면, 한국어 형태소 분석으로
      명사/키워드를 추출해 JSON으로 돌려주기만 한다. DB도, mb_id도 모른다.
      모든 영속 데이터는 PHP/MySQL이 단독으로 보관한다(SSOT).

엔드포인트:
  GET  /health            : 살아있는지 핑 (인증 불필요)
  POST /analyze/keywords  : 본문 → 명사/키워드 추출 (X-Worker-Secret 필요)

인증: 요청 헤더 X-Worker-Secret 값이 환경변수 WORKER_SECRET 과 일치해야 함.
"""

import os
import time
import hmac
from collections import Counter
from typing import List, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
# blog_seo_service 가 Kiwi를 초기화하므로 그 인스턴스를 재사용 (이중 초기화 방지)
from blog_seo_service import router as blog_router, kiwi

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
WORKER_SECRET = os.environ.get("WORKER_SECRET", "")

KEYWORD_TAGS = {"NNG", "NNP", "SL"}

app = FastAPI(title="Upclick Naver Blog Worker", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

app.include_router(blog_router)


# ---------------------------------------------------------------------------
# 요청/응답 스키마
# ---------------------------------------------------------------------------
class KeywordRequest(BaseModel):
    text: str = Field(..., description="분석할 블로그 본문")
    top_n: int = Field(20, ge=1, le=200, description="상위 N개 키워드")
    min_len: int = Field(2, ge=1, le=10, description="이 글자 수 미만 명사는 제외")


class KeywordItem(BaseModel):
    word: str
    pos: str
    count: int


class KeywordResponse(BaseModel):
    keywords: List[KeywordItem]   # 빈도순 상위 N개
    total_nouns: int              # 추출된 명사 토큰 총수(중복 포함)
    unique_nouns: int             # 고유 명사 수
    char_count: int               # 본문 글자 수
    took_ms: int                  # 처리 시간(ms)


# ---------------------------------------------------------------------------
# 인증
# ---------------------------------------------------------------------------
def verify_secret(x_worker_secret: Optional[str]):
    # 시크릿이 서버에 설정 안 됐으면 잠금(설정 누락 사고 방지)
    if not WORKER_SECRET:
        raise HTTPException(status_code=503, detail="worker secret not configured")
    # 타이밍 공격 방지를 위해 compare_digest 사용
    if not x_worker_secret or not hmac.compare_digest(x_worker_secret, WORKER_SECRET):
        raise HTTPException(status_code=401, detail="invalid worker secret")


# ---------------------------------------------------------------------------
# 엔드포인트
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    """클라우드타입 슬립 깨우기 + 헬스체크용. 인증 불필요."""
    return {"status": "ok"}


@app.post("/analyze/keywords", response_model=KeywordResponse)
def analyze_keywords(
    req: KeywordRequest,
    x_worker_secret: Optional[str] = Header(default=None),
):
    verify_secret(x_worker_secret)

    started = time.perf_counter()
    text = (req.text or "").strip()

    if not text:
        return KeywordResponse(
            keywords=[], total_nouns=0, unique_nouns=0,
            char_count=0, took_ms=0,
        )

    # 형태소 분석 → 키워드 품사만 추리기
    pos_by_word = {}   # word -> pos (대표 품사)
    counter = Counter()
    total_nouns = 0

    for token in kiwi.tokenize(text):
        if token.tag not in KEYWORD_TAGS:
            continue
        word = token.form
        # 한 글자 명사(예: '것','수','때')는 노이즈가 많아 기본 제외
        if len(word) < req.min_len:
            continue
        total_nouns += 1
        counter[word] += 1
        # 같은 단어가 NNG/NNP로 갈릴 때 첫 태그를 대표로 사용
        if word not in pos_by_word:
            pos_by_word[word] = token.tag

    top = counter.most_common(req.top_n)
    keywords = [
        KeywordItem(word=w, pos=pos_by_word.get(w, "NNG"), count=c)
        for w, c in top
    ]

    took_ms = int((time.perf_counter() - started) * 1000)

    return KeywordResponse(
        keywords=keywords,
        total_nouns=total_nouns,
        unique_nouns=len(counter),
        char_count=len(text),
        took_ms=took_ms,
    )
