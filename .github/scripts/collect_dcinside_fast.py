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
DEBUG_DIR = Path("debug_work")
OUT = Path("out")
ZIP_PATH = OUT / f"{GALLERY}_{POST_NO}_comment_images.zip"

STATIC_HOSTS = {"nstatic.dcinside.com", "static.dcinside.com"}
SKIP_PARTS = (
    "nickicon",
    "member_icon",
    "favicon",
    "captcha",
    "dccon_loading",
    "loading.gif",
    "blank.gif",
    "noimage",
)


def clean() -> None:
    for path in (ROOT, DEBUG_DIR, OUT):
        if path.exists():
            shutil.rmtree(path)
    IMG_DIR.mkdir(parents=True)
    DEBUG_DIR.mkdir(parents=True)
    OUT.mkdir(parents=True)


def normalize(raw: str) -> str:
    raw = html.unescape((raw or "").strip().strip("\"'")).replace("\\/", "/")
    if not raw or raw.startswith(("data:", "javascript:", "#")):
        return ""
    if raw.startswith("//"):
        return "https:" + raw
    if not raw.startswith(("http://", "https://")):
        return urljoin(POST_URL, raw)
    return raw


def is_comment_media(url: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    low = url.lower()
    if host in STATIC_HOSTS:
        return False
    if any(part in low for part in SKIP_PARTS):
        return False
    return bool(
        "dccon.php" in low
        or "dcimg" in host
        or "dccon" in host
        or re.search(
            r"\.(?:jpe?g|png|gif|webp|avif|bmp|svg)(?:$|[?#])",
            low,
        )
    )


def pick_image_url(tag) -> tuple[str, str]:
    # Animated DCCon exposes the original animation in data-gif while
    # data-original/src point to a loading placeholder. Prefer data-gif.
    if tag.name == "img":
        attrs = ("data-gif", "data-original", "data-src", "data-url", "src")
    else:
        attrs = ("src", "data-src", "data-original", "data-url", "href")
    for attr in attrs:
        value = tag.get(attr)
        if not value:
            continue
        url = normalize(str(value))
        if is_comment_media(url):
            return url, attr
    return "", ""


def comment_items(soup: BeautifulSoup):
    for selector in ("body > ul > li", "ul > li", "li[no]", "li[data-no]"):
        items = soup.select(selector)
        if items:
            return items
    return []


def get_last_page(soup: BeautifulSoup, current: int) -> int | None:
    candidates: list[int] = []
    for node in soup.select("span.pgnum, .paging, .pagination, .btn-paging"):
        candidates.extend(
            int(value)
            for value in re.findall(r"\d+", node.get_text(" ", strip=True))
        )
        for anchor in node.select("a[href]"):
            candidates.extend(
                int(value)
                for value in re.findall(
                    r"(?:cpage|page)=(\d+)",
                    anchor.get("href", ""),
                )
            )
    candidates = [value for value in candidates if current <= value < 10000]
    return max(candidates) if candidates else None


def collect() -> tuple[list[dict], list[dict], dict]:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": UA,
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.7,en;q=0.6",
        }
    )

    view = session.get(POST_URL, timeout=(8, 20))
    view.raise_for_status()
    (DEBUG_DIR / "post_sample.html").write_text(
        view.text[:300000],
        encoding="utf-8",
        errors="replace",
    )

    occurrences: list[dict] = []
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
            timeout=(8, 20),
        )
        response.raise_for_status()
        if page <= 3:
            (DEBUG_DIR / f"comments_page_{page}.html").write_text(
                response.text[:1000000],
                encoding="utf-8",
                errors="replace",
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
        for li in items:
            comment_no = str(li.get("no") or li.get("data-no") or "")
            if not comment_no:
                comment_no = hashlib.sha1(
                    str(li).encode("utf-8", errors="ignore")
                ).hexdigest()[:16]
            if comment_no in seen_comments:
                continue
            seen_comments.add(comment_no)
            new_count += 1

            author_node = li.select_one(
                "button.nick, a.nick, span.nick, .gall_writer, .name"
            )
            author = author_node.get_text(" ", strip=True) if author_node else ""
            date_node = li.select_one("span.date, .date_time, time")
            date = date_node.get_text(" ", strip=True) if date_node else ""
            parent_no = str(
                li.get("data-parent")
                or li.get("parent")
                or li.get("data-parent-no")
                or ""
            )

            media_order = 0
            for tag in li.find_all(["img", "source", "video"]):
                url, source_attr = pick_image_url(tag)
                if not url:
                    continue
                media_order += 1
                occurrences.append(
                    {
                        "occurrence_no": len(occurrences) + 1,
                        "page": page,
                        "comment_no": comment_no,
                        "comment_order": len(seen_comments),
                        "image_order": media_order,
                        "parent_no": parent_no,
                        "author": author,
                        "date": date,
                        "title": str(tag.get("title") or tag.get("alt") or ""),
                        "source_attr": source_attr,
                        "url": url,
                    }
                )

        page_stats.append(
            {"page": page, "items": len(items), "new_comments": new_count}
        )
        print(
            f"page={page} items={len(items)} new={new_count} "
            f"occurrences={len(occurrences)} last={last_page}",
            flush=True,
        )
        if new_count == 0:
            break
        if last_page is not None and page >= last_page:
            break
        time.sleep(0.05)

    unique: list[dict] = []
    seen_urls: set[str] = set()
    for row in occurrences:
        if row["url"] in seen_urls:
            continue
        seen_urls.add(row["url"])
        unique_row = dict(row)
        unique_row["unique_no"] = len(unique) + 1
        unique.append(unique_row)

    stats = {
        "comment_count": len(seen_comments),
        "image_occurrences": len(occurrences),
        "unique_image_urls": len(unique),
        "last_page_detected": last_page,
        "pages": page_stats,
    }
    return unique, occurrences, stats


def sniff_ext(data: bytes, content_type: str, url: str) -> str:
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

    guessed = mimetypes.guess_extension(
        (content_type or "").split(";", 1)[0].strip()
    )
    if guessed in {".jpe", ".jpeg"}:
        guessed = ".jpg"
    if guessed:
        return guessed

    suffix = Path(urlparse(url).path).suffix.lower()
    allowed = {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".avif",
        ".bmp",
        ".svg",
    }
    return suffix if suffix in allowed else ".bin"


def safe_name(value: str, limit: int = 30) -> str:
    value = re.sub(r"[^\w가-힣.-]+", "_", str(value or ""), flags=re.UNICODE)
    value = value.strip("_.")
    return (value or "untitled")[:limit]


def fetch_one(row: dict) -> dict:
    result = dict(row)
    try:
        response = requests.get(
            row["url"],
            headers={
                "User-Agent": UA,
                "Referer": POST_URL,
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            },
            timeout=(8, 30),
        )
        response.raise_for_status()
        data = response.content
        if not data:
            raise RuntimeError("empty response")

        content_type = response.headers.get("Content-Type", "")
        extension = sniff_ext(data, content_type, row["url"])
        if extension == ".bin" and "html" in content_type.lower():
            raise RuntimeError("server returned HTML")

        file_name = (
            f"{row['unique_no']:03d}_comment_{safe_name(row['comment_no'], 24)}_"
            f"{safe_name(row['title'])}{extension}"
        )
        (IMG_DIR / file_name).write_bytes(data)
        result.update(
            {
                "file": f"images/{file_name}",
                "content_type": content_type,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "status": "saved",
                "error": "",
            }
        )
    except Exception as exc:
        result.update(
            {
                "file": "",
                "content_type": "",
                "bytes": 0,
                "sha256": "",
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
    return result


def download(rows: list[dict]) -> list[dict]:
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(12, max(1, len(rows)))) as pool:
        futures = {pool.submit(fetch_one, row): row for row in rows}
        for done, future in enumerate(as_completed(futures), 1):
            results.append(future.result())
            if done % 10 == 0 or done == len(rows):
                print(f"downloads={done}/{len(rows)}", flush=True)
    results.sort(key=lambda row: row["unique_no"])
    return results


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def package(
    results: list[dict],
    occurrences: list[dict],
    stats: dict,
) -> dict:
    unique_fields = [
        "unique_no",
        "page",
        "comment_no",
        "comment_order",
        "image_order",
        "parent_no",
        "author",
        "date",
        "title",
        "source_attr",
        "url",
        "file",
        "content_type",
        "bytes",
        "sha256",
        "status",
        "error",
    ]
    write_csv(ROOT / "manifest_unique_images.csv", results, unique_fields)

    by_url = {row["url"]: row for row in results}
    occurrence_rows: list[dict] = []
    for row in occurrences:
        mapped = by_url.get(row["url"], {})
        occurrence_row = dict(row)
        occurrence_row.update(
            {
                "file": mapped.get("file", ""),
                "content_type": mapped.get("content_type", ""),
                "bytes": mapped.get("bytes", 0),
                "sha256": mapped.get("sha256", ""),
                "status": mapped.get("status", "failed"),
                "error": mapped.get("error", ""),
            }
        )
        occurrence_rows.append(occurrence_row)

    occurrence_fields = [
        "occurrence_no",
        "page",
        "comment_no",
        "comment_order",
        "image_order",
        "parent_no",
        "author",
        "date",
        "title",
        "source_attr",
        "url",
        "file",
        "content_type",
        "bytes",
        "sha256",
        "status",
        "error",
    ]
    write_csv(
        ROOT / "manifest_all_occurrences.csv",
        occurrence_rows,
        occurrence_fields,
    )

    saved = sum(row["status"] == "saved" for row in results)
    failed = sum(row["status"] == "failed" for row in results)
    formats: dict[str, int] = {}
    for row in results:
        if row["status"] != "saved":
            continue
        suffix = Path(row["file"]).suffix.lower() or ".unknown"
        formats[suffix] = formats.get(suffix, 0) + 1

    summary = dict(stats)
    summary.update(
        {
            "saved_unique_images": saved,
            "failed_unique_images": failed,
            "formats": formats,
            "post_url": POST_URL,
        }
    )
    (ROOT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (ROOT / "README.txt").write_text(
        f"게시글: {POST_URL}\n"
        f"확인한 댓글: {stats['comment_count']}개\n"
        f"댓글 내 이미지 등장 횟수: {stats['image_occurrences']}회\n"
        f"중복 URL을 제외한 실제 이미지: {stats['unique_image_urls']}개\n"
        f"저장 성공: {saved}개\n"
        f"저장 실패: {failed}개\n\n"
        "images 폴더에는 중복 URL을 제외한 원본 이미지가 들어 있습니다.\n"
        "애니메이션 DCCon은 data-gif 원본을 우선 저장했습니다.\n"
        "manifest_all_occurrences.csv에는 중복을 포함한 댓글별 등장 내역이 있습니다.\n"
        "게시글 본문 이미지는 제외했습니다.\n",
        encoding="utf-8",
    )

    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(ROOT.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(ROOT))

    summary["zip_bytes"] = ZIP_PATH.stat().st_size
    summary["zip_sha256"] = hashlib.sha256(ZIP_PATH.read_bytes()).hexdigest()
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return summary


def main() -> int:
    clean()
    unique, occurrences, stats = collect()
    results = download(unique)
    summary = package(results, occurrences, stats)
    return 0 if summary["saved_unique_images"] > 0 else 2


if __name__ == "__main__":
    sys.exit(main())
