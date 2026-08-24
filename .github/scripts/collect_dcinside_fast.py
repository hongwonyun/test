#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import html
import json
import mimetypes
import re
import shutil
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

GALLERY = "rescene1"
POST_NO = "352852"
POST_URL = f"https://m.dcinside.com/board/{GALLERY}/{POST_NO}"
COMMENT_URL = "https://m.dcinside.com/ajax/response-comment"
UA = (
    "Mozilla/5.0 (Linux; Android 14; SM-S928N Build/UP1A.231005.007; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
    "Chrome/131.0.0.0 Mobile Safari/537.36"
)
ROOT = Path("fast_work")
IMG_DIR = ROOT / "images"
OUT = Path("out")
ZIP_PATH = OUT / f"{GALLERY}_{POST_NO}_comment_images.zip"


def clean() -> None:
    for p in (ROOT, OUT):
        if p.exists():
            shutil.rmtree(p)
    IMG_DIR.mkdir(parents=True)
    OUT.mkdir(parents=True)


def normalize(raw: str) -> str:
    raw = html.unescape((raw or "").strip().strip("\"'"))
    raw = raw.replace("\\/", "/")
    if not raw or raw.startswith(("data:", "javascript:", "#")):
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    elif not raw.startswith(("http://", "https://")):
        raw = urljoin(POST_URL, raw)
    return raw


def image_urls_from_li(li) -> list[str]:
    urls: list[str] = []
    # 댓글 본문은 보통 두 번째 자식이지만 구조 변경에도 대응해 li 내부 이미지를 모두 검사합니다.
    for tag in li.find_all(["img", "source", "video"]):
        for attr in ("data-original", "data-src", "data-url", "src", "href"):
            value = tag.get(attr)
            if not value:
                continue
            url = normalize(str(value))
            low = url.lower()
            host = (urlparse(url).hostname or "").lower()
            if not url:
                continue
            if host in {"nstatic.dcinside.com", "static.dcinside.com"}:
                continue
            if any(x in low for x in ("nickicon", "member_icon", "favicon", "captcha", "loading.gif", "blank.gif")):
                continue
            if (
                "dccon.php" in low
                or "dcimg" in host
                or "dccon" in host
                or re.search(r"\.(?:jpe?g|png|gif|webp|avif|bmp|svg)(?:$|[?#])", low)
            ):
                urls.append(url)
            break
    return list(dict.fromkeys(urls))


def comment_items(soup: BeautifulSoup):
    for selector in ("body > ul > li", "ul > li", "li[no]", "li[data-no]"):
        items = soup.select(selector)
        if items:
            return items
    return []


def get_last_page(soup: BeautifulSoup, current: int) -> int | None:
    candidates: list[int] = []
    for node in soup.select("span.pgnum, .paging, .pagination, .btn-paging"):
        candidates.extend(int(x) for x in re.findall(r"\d+", node.get_text(" ", strip=True)))
        for a in node.select("a[href]"):
            candidates.extend(int(x) for x in re.findall(r"(?:cpage|page)=(\d+)", a.get("href", "")))
    candidates = [x for x in candidates if x >= current and x < 10000]
    return max(candidates) if candidates else None


def collect() -> tuple[list[dict], dict]:
    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.7,en;q=0.6",
    })
    view = session.get(POST_URL, timeout=20)
    view.raise_for_status()
    (ROOT / "post_sample.html").write_text(view.text[:300000], encoding="utf-8", errors="replace")

    rows: list[dict] = []
    seen_comments: set[str] = set()
    page_stats: list[dict] = []
    last_page: int | None = None

    for page in range(1, 501):
        response = session.post(
            COMMENT_URL,
            data={
                "id": GALLERY,
                "no": POST_NO,
                "cpage": str(page),
                "managerskill": "",
                "del_scope": "1",
                "csort": "",
            },
            headers={
                "User-Agent": UA,
                "Accept": "*/*",
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": "https://m.dcinside.com",
                "Referer": POST_URL,
            },
            timeout=20,
        )
        response.raise_for_status()
        if page <= 3:
            (ROOT / f"comments_page_{page}.html").write_text(
                response.text[:1000000], encoding="utf-8", errors="replace"
            )
        soup = BeautifulSoup(response.text, "lxml")
        items = comment_items(soup)
        if not items:
            page_stats.append({"page": page, "items": 0, "new_comments": 0})
            break
        detected = get_last_page(soup, page)
        if detected is not None:
            last_page = detected if last_page is None else max(last_page, detected)

        new_count = 0
        for order, li in enumerate(items, 1):
            cno = str(li.get("no") or li.get("data-no") or "")
            if not cno:
                cno = hashlib.sha1(str(li).encode("utf-8", errors="ignore")).hexdigest()[:16]
            if cno in seen_comments:
                continue
            seen_comments.add(cno)
            new_count += 1
            author_node = li.select_one("a.nick, span.nick, .gall_writer, .name")
            author = author_node.get_text(" ", strip=True) if author_node else ""
            date_node = li.select_one("span.date, .date_time, time")
            date = date_node.get_text(" ", strip=True) if date_node else ""
            for image_order, url in enumerate(image_urls_from_li(li), 1):
                rows.append({
                    "page": page,
                    "comment_no": cno,
                    "comment_order": len(seen_comments),
                    "image_order": image_order,
                    "author": author,
                    "date": date,
                    "url": url,
                })
        page_stats.append({"page": page, "items": len(items), "new_comments": new_count})
        print(f"page={page} items={len(items)} new={new_count} images={len(rows)} last={last_page}", flush=True)
        if new_count == 0:
            break
        if last_page is not None and page >= last_page:
            break
        time.sleep(0.05)

    # URL 단위로 중복 제거하되 첫 댓글의 메타데이터를 유지합니다.
    unique: list[dict] = []
    seen_urls: set[str] = set()
    for row in rows:
        if row["url"] in seen_urls:
            continue
        seen_urls.add(row["url"])
        unique.append(row)
    stats = {
        "comment_count": len(seen_comments),
        "image_occurrences": len(rows),
        "unique_image_urls": len(unique),
        "last_page_detected": last_page,
        "pages": page_stats,
    }
    return unique, stats


def sniff_ext(data: bytes, ctype: str, url: str) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if b"ftypavif" in data[:32] or b"ftypavis" in data[:32]:
        return ".avif"
    if data.lstrip().lower().startswith(b"<svg"):
        return ".svg"
    guessed = mimetypes.guess_extension((ctype or "").split(";")[0].strip())
    if guessed in {".jpe", ".jpeg"}:
        guessed = ".jpg"
    if guessed:
        return guessed
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".svg"} else ".bin"


def fetch_one(index: int, row: dict) -> dict:
    result = dict(row)
    try:
        response = requests.get(
            row["url"],
            headers={"User-Agent": UA, "Referer": POST_URL, "Accept": "image/*,*/*;q=0.8"},
            timeout=25,
        )
        response.raise_for_status()
        data = response.content
        if not data:
            raise RuntimeError("empty response")
        ctype = response.headers.get("Content-Type", "")
        ext = sniff_ext(data, ctype, row["url"])
        if ext == ".bin" and "html" in ctype.lower():
            raise RuntimeError("server returned HTML")
        filename = f"{index:03d}_comment_{re.sub(r'[^0-9A-Za-z_-]+', '_', row['comment_no'])[:24]}{ext}"
        (IMG_DIR / filename).write_bytes(data)
        result.update({
            "file": f"images/{filename}",
            "content_type": ctype,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "status": "saved",
            "error": "",
        })
    except Exception as exc:
        result.update({
            "file": "",
            "content_type": "",
            "bytes": 0,
            "sha256": "",
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        })
    return result


def download(rows: list[dict]) -> list[dict]:
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(fetch_one, i, row): i for i, row in enumerate(rows, 1)}
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda x: x.get("file") or x["url"])
    return results


def package(results: list[dict], stats: dict) -> dict:
    fields = [
        "page", "comment_no", "comment_order", "image_order", "author", "date", "url",
        "file", "content_type", "bytes", "sha256", "status", "error",
    ]
    with (ROOT / "manifest.csv").open("w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.DictWriter(fp, fieldnames=fields)
        writer.writeheader()
        writer.writerows(results)
    saved = sum(r["status"] == "saved" for r in results)
    failed = sum(r["status"] == "failed" for r in results)
    summary = dict(stats)
    summary.update({"saved": saved, "failed": failed, "post_url": POST_URL})
    (ROOT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (ROOT / "README.txt").write_text(
        f"게시글: {POST_URL}\n댓글 수: {stats['comment_count']}\n"
        f"고유 댓글 이미지 URL: {stats['unique_image_urls']}\n저장 성공: {saved}\n저장 실패: {failed}\n"
        "게시글 본문 이미지는 제외했고, 댓글 안 이미지와 DCCon만 저장했습니다.\n",
        encoding="utf-8",
    )
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(ROOT.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(ROOT))
    summary["zip_bytes"] = ZIP_PATH.stat().st_size
    summary["zip_sha256"] = hashlib.sha256(ZIP_PATH.read_bytes()).hexdigest()
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return summary


def main() -> int:
    clean()
    rows, stats = collect()
    results = download(rows)
    summary = package(results, stats)
    return 0 if summary["saved"] > 0 else 2


if __name__ == "__main__":
    sys.exit(main())
