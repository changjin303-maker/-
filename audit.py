import asyncio
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

SHARD = int(os.environ.get("SHARD", "0"))
TOTAL_SHARDS = int(os.environ.get("TOTAL_SHARDS", "10"))

POPULAR_PATTERNS = [
    re.compile(r"인기\s*주제"),
    re.compile(r"인기주제"),
]
SMART_PATTERNS = [
    re.compile(r"인기\s*글"),
    re.compile(r"인기글"),
]
BLOCK_PATTERNS = [
    re.compile(r"스마트\s*블록", re.I),
]
BLOCKED_PATTERNS = [
    "비정상적인 검색", "자동입력 방지", "서비스 이용이 제한", "접근이 제한", "captcha"
]


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def evidence_lines(text: str, patterns, limit=5):
    lines = []
    seen = set()
    for raw in (text or "").splitlines():
        line = norm(raw)
        if not line or len(line) > 160:
            continue
        if any(p.search(line) for p in patterns) and line not in seen:
            lines.append(line)
            seen.add(line)
            if len(lines) >= limit:
                break
    return lines


async def inspect_keyword(page, keyword: str):
    url = "https://search.naver.com/search.naver?where=nexearch&sm=top_hty&fbm=0&ie=utf8&query=" + quote(keyword)
    last_error = ""
    for attempt in range(1, 4):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=35000)
            await page.wait_for_timeout(1200 + attempt * 300)
            # Dynamic blocks often load after a moderate scroll.
            for y in (500, 1200, 2200, 3500):
                await page.evaluate(f"window.scrollTo(0,{y})")
                await page.wait_for_timeout(250)
            await page.evaluate("window.scrollTo(0,0)")
            text = await page.locator("body").inner_text(timeout=10000)
            html = await page.content()
            text_norm = norm(text)

            if len(text_norm) < 100:
                raise RuntimeError("page content too short")
            blocked = any(token.lower() in text_norm.lower() for token in BLOCKED_PATTERNS)
            if blocked:
                last_error = "네이버 접속 제한/자동입력 방지 화면"
                await page.wait_for_timeout(2500 * attempt)
                continue

            popular_evidence = evidence_lines(text, POPULAR_PATTERNS)
            smart_evidence = evidence_lines(text, SMART_PATTERNS + BLOCK_PATTERNS)

            # DOM-level supplemental detection. Naver frequently changes class names,
            # so visible text is the primary signal and semantic snippets are secondary.
            popular_present = bool(popular_evidence) or bool(re.search(r"인기\s*주제", html))
            smart_present = bool(smart_evidence) or bool(re.search(r"인기\s*글|스마트\s*블록", html, re.I))

            return {
                "keyword": keyword,
                "popular_status": "있음" if popular_present else "없음",
                "popular_evidence": " | ".join(popular_evidence[:3]),
                "smart_status": "있음" if smart_present else "없음",
                "smart_evidence": " | ".join(smart_evidence[:3]),
                "check_status": "확인완료",
                "error": "",
                "url": url,
                "checked_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "attempts": attempt,
            }
        except (PlaywrightTimeoutError, Exception) as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:180]}"
            await page.wait_for_timeout(1800 * attempt)
    return {
        "keyword": keyword,
        "popular_status": "확인실패",
        "popular_evidence": "",
        "smart_status": "확인실패",
        "smart_evidence": "",
        "check_status": "확인실패",
        "error": last_error,
        "url": url,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "attempts": 3,
    }


async def main():
    keywords = json.loads(Path("keywords.json").read_text(encoding="utf-8"))
    mine = [kw for i, kw in enumerate(keywords) if i % TOTAL_SHARDS == SHARD]
    out_dir = Path("results")
    out_dir.mkdir(exist_ok=True)
    out_json = out_dir / f"results-{SHARD:02d}.json"
    out_csv = out_dir / f"results-{SHARD:02d}.csv"

    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            viewport={"width": 1440, "height": 1600},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
            extra_http_headers={"Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.7,en;q=0.5"},
        )
        page = await context.new_page()
        for idx, keyword in enumerate(mine, 1):
            result = await inspect_keyword(page, keyword)
            results.append(result)
            print(f"[{SHARD}] {idx}/{len(mine)} {keyword}: popular={result['popular_status']} smart={result['smart_status']} check={result['check_status']}", flush=True)
            out_json.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            await page.wait_for_timeout(650)
        await browser.close()

    fields = ["keyword", "popular_status", "popular_evidence", "smart_status", "smart_evidence", "check_status", "error", "url", "checked_at_utc", "attempts"]
    with out_csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(results)


if __name__ == "__main__":
    asyncio.run(main())
