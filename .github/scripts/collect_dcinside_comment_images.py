#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import html as html_lib
import json
import re
import shutil
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

GALLERY = "rescene1"
POST_NO = "352852"
POST_URL = f"https://m.dcinside.com/board/{GALLERY}/{POST_NO}"
COMMENT_URL = "https://m.dcinside.com/ajax/response-comment"
OUT = Path("out")
WORK = Path("work")
IMAGES = WORK / "images"
DEBUG = WORK / "debug"
ZIP_PATH = OUT / f"{GALLERY}_{POST_NO}_comment_images.zip"

UA = (
    "Mozilla/5.0 (Linux; Android 14; SM-S928N Build/UP1A.231005.007; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
    "Chrome/131.0.0.0 Mobile Safari/537.36"
)
ATTRS = ("data-original", "data-src", "data-url", "src", "href")
TAGS = ("img", "video", "source")
SKIP_HOSTS = {"nstatic.dcinside.com", "static.dcinside.com"}
SKIP_PARTS = (
    "/images/icon",
    "/w/images/",
    "nickicon",
    "member_icon",
    "gallog",
    "fixed_nik",
    "fix_nik",
    "sp_img",
    "noimage",
    "blank.gif",
    "loading.gif",
    "favicon",
    "captcha",
    "code.php",
)


@dataclass
class Occurrence:
    page: int
    comment_no: str
    parent_no: str
    author: str
    date: str
    url: str
    tag: str
    field: str
    media_order: int
    file_name: str = ""
    content_type: str = ""
    byte_size: int = 0
    sha256: str = ""
    status: str = "pending"
    error: str = ""


def reset_dirs() -> None:
    for path in (OUT, WORK):
        if path.exists():
            shutil.rmtree(path)
    OUT.mkdir(parents=True)
    IMAGES.mkdir(parents=True)
    DEBUG.mkdir(parents=True)


def clean_text(value: object) -> str:
    text = BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def safe_name(value: str, limit: int = 32) -> str:
    value = clean_text(value)
    value = re.sub(r"[^\w가-힣.-]+", "_", value, flags=re.UNICODE).strip("_.")
    return (value or "anonymous")[:limit]


def normalize_url(raw: str) -> str:
    raw = html_lib.unescape((raw or "").strip().strip("\"'"))
    raw = raw.replace("\\/", "/").rstrip("),]};")
    if not raw or raw.startswith(("data:", "javascript:", "#")):
        return ""
    if raw.startswith("//"):
        return "https:" + raw
    return urljoin(POST_URL, raw)


def is_media_url(url: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    lower = url.lower()
    if host in SKIP_HOSTS:
        return False
    if any(part in lower for part in SKIP_PARTS):
        return False
    if "dccon.php" in lower:
        return True
    if any(token in host for token in ("dcimg", "dccon")):
        return True
    return bool(
        re.search(
            r"\.(?:jpe?g|png|gif|webp|avif|bmp|svg|mp4|webm)(?:$|[?#])",
            lower,
        )
    )


def extract_media(fragment: str, field: str) -> list[tuple[str, str, str]]:
    result: list[tuple[str, str, str]] = []
    soup = BeautifulSoup(html_lib.unescape(fragment or ""), "html.parser")
    for tag_name in TAGS:
        for tag in soup.find_all(tag_name):
            for attr in ATTRS:
                raw = tag.get(attr)
                if not raw:
                    continue
                url = normalize_url(str(raw))
                if is_media_url(url):
                    result.append((url, tag_name, f"{field}.{attr}"))
                    break

    seen: set[str] = set()
    unique: list[tuple[str, str, str]] = []
    for item in result:
        if item[0] in seen:
            continue
        seen.add(item[0])
        unique.append(item)
    return unique


def pick_content(li: object) -> object:
    for selector in (
        "div.comment-txt",
        "div.comment_txt",
        "div.usertxt",
        "div.txt",
        "div.reply-content",
        "p",
    ):
        node = li.select_one(selector)
        if node and node.find(list(TAGS)):
            return node
    for selector in (
        "div.comment-txt",
        "div.comment_txt",
        "div.usertxt",
        "div.txt",
        "div.reply-content",
        "p",
    ):
        node = li.select_one(selector)
        if node:
            return node
    return li


def extract_from_comment_li(li: object, page: int) -> list[Occurrence]:
    comment_no = str(
        li.get("no")
        or li.get("data-no")
        or li.get("data-comment-no")
        or li.get("data-cmt-no")
        or hashlib.sha1(str(li).encode("utf-8", "ignore")).hexdigest()[:16]
    )
    parent_no = str(
        li.get("data-parent")
        or li.get("parent")
        or li.get("data-parent-no")
        or li.get("data-reply-no")
        or ""
    )
    author_node = li.select_one("a.nick, span.nick, .gall_writer, .name")
    date_node = li.select_one("span.date, .date_time, time")
    author = clean_text(author_node.get_text(" ", strip=True) if author_node else "")
    date = clean_text(date_node.get_text(" ", strip=True) if date_node else "")
    content = pick_content(li)
    media = extract_media(str(content), "comment_html")
    return [
        Occurrence(
            page=page,
            comment_no=comment_no,
            parent_no=parent_no,
            author=author,
            date=date,
            url=url,
            tag=tag,
            field=field,
            media_order=index,
        )
        for index, (url, tag, field) in enumerate(media, 1)
    ]


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": UA,
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.7,en;q=0.6",
        }
    )
    return session


def collect_occurrences() -> tuple[list[Occurrence], dict]:
    session = build_session()
    view = session.get(POST_URL, timeout=(8, 20))
    (DEBUG / "view_status.txt").write_text(
        f"status={view.status_code}\nurl={view.url}\nheaders={dict(view.headers)}\n",
        encoding="utf-8",
    )
    (DEBUG / "view_sample.html").write_text(
        view.text[:500000], encoding="utf-8", errors="replace"
    )
    view.raise_for_status()

    all_items: list[Occurrence] = []
    seen_occurrences: set[tuple[str, str]] = set()
    seen_pages: set[str] = set()
    seen_comment_pages: set[tuple[str, ...]] = set()
    pages_with_comments = 0
    comment_ids: set[str] = set()

    for page in range(1, 101):
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
        body = response.text

        if page <= 5:
            (DEBUG / f"comments_page_{page}.html").write_text(
                body[:1000000], encoding="utf-8", errors="replace"
            )
        fingerprint = hashlib.sha256(body.encode("utf-8", "ignore")).hexdigest()
        if fingerprint in seen_pages:
            print(f"Repeated comment page at {page}; pagination finished.", flush=True)
            break
        seen_pages.add(fingerprint)

        soup = BeautifulSoup(body, "lxml")
        lis = soup.select("body > ul > li")
        if not lis:
            lis = soup.select("li[no], li[data-no], li.comment, li.comment-add")
        if not lis:
            print(f"No comment items at page {page}; pagination finished.", flush=True)
            break

        current_ids = tuple(
            sorted(
                {
                    str(
                        li.get("no")
                        or li.get("data-no")
                        or li.get("data-comment-no")
                        or li.get("data-cmt-no")
                        or ""
                    )
                    for li in lis
                }
                - {""}
            )
        )
        if current_ids and current_ids in seen_comment_pages:
            print(
                f"Repeated comment IDs at page {page}; pagination finished.",
                flush=True,
            )
            break
        if current_ids:
            seen_comment_pages.add(current_ids)
            comment_ids.update(current_ids)

        page_added = 0
        for li in lis:
            for item in extract_from_comment_li(li, page):
                key = (item.comment_no, item.url)
                if key in seen_occurrences:
                    continue
                seen_occurrences.add(key)
                all_items.append(item)
                page_added += 1

        pages_with_comments += 1
        print(
            f"page={page} comment_items={len(lis)} new_images={page_added} "
            f"total_images={len(all_items)}",
            flush=True,
        )

    meta = {
        "pages_with_comments": pages_with_comments,
        "unique_comment_ids_seen": len(comment_ids),
        "occurrences_found": len(all_items),
        "cookies": session.cookies.get_dict(),
    }
    return all_items, meta


def extension(data: bytes, content_type: str, url: str) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith(b"BM"):
        return "bmp"
    if b"ftypavif" in data[:32] or b"ftypavis" in data[:32]:
        return "avif"
    sample = data.lstrip()[:300].lower()
    if sample.startswith(b"<svg") or b"<svg" in sample:
        return "svg"

    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    mapping = {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/gif": "gif",
        "image/webp": "webp",
        "image/avif": "avif",
        "image/bmp": "bmp",
        "image/svg+xml": "svg",
        "video/mp4": "mp4",
        "video/webm": "webm",
    }
    if ctype in mapping:
        return mapping[ctype]
    suffix = Path(urlparse(url).path).suffix.lower().lstrip(".")
    allowed = {"jpg", "jpeg", "png", "gif", "webp", "avif", "bmp", "svg", "mp4", "webm"}
    if suffix in allowed:
        return "jpg" if suffix == "jpeg" else suffix
    return "bin"


def fetch_one(item: Occurrence) -> tuple[Occurrence, bytes | None]:
    try:
        response = requests.get(
            item.url,
            headers={
                "User-Agent": UA,
                "Referer": POST_URL,
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            },
            timeout=(8, 25),
        )
        response.raise_for_status()
        data = response.content
        if not data:
            raise RuntimeError("empty response")
        content_type = response.headers.get("Content-Type", "")
        ext = extension(data, content_type, item.url)
        if ext == "bin" and "html" in content_type.lower():
            raise RuntimeError("received HTML instead of media")
        item.content_type = content_type
        item.byte_size = len(data)
        item.sha256 = hashlib.sha256(data).hexdigest()
        item.status = "downloaded"
        item.file_name = ext
        return item, data
    except Exception as exc:
        item.status = "failed"
        item.error = f"{type(exc).__name__}: {exc}"[:1000]
        return item, None


def download_all(items: list[Occurrence]) -> None:
    if not items:
        return
    results: dict[int, tuple[Occurrence, bytes | None]] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(items))) as executor:
        futures = {executor.submit(fetch_one, item): index for index, item in enumerate(items)}
        for done, future in enumerate(as_completed(futures), 1):
            index = futures[future]
            results[index] = future.result()
            print(f"downloaded {done}/{len(items)}", flush=True)

    for index in range(len(items)):
        item, data = results[index]
        if data is None:
            continue
        ext = item.file_name
        name = (
            f"{index + 1:03d}_comment_{safe_name(item.comment_no, 24)}_"
            f"{safe_name(item.author)}_{item.media_order:02d}.{ext}"
        )
        (IMAGES / name).write_bytes(data)
        item.file_name = f"images/{name}"
        item.status = "saved"


def write_outputs(items: list[Occurrence], meta: dict, errors: list[str]) -> dict:
    fields = list(asdict(items[0]).keys()) if items else [
        "page", "comment_no", "parent_no", "author", "date", "url", "tag",
        "field", "media_order", "file_name", "content_type", "byte_size",
        "sha256", "status", "error",
    ]
    with (WORK / "manifest.csv").open("w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.DictWriter(fp, fieldnames=fields)
        writer.writeheader()
        for item in items:
            writer.writerow(asdict(item))
    (WORK / "manifest.json").write_text(
        json.dumps([asdict(item) for item in items], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    saved = sum(item.status == "saved" for item in items)
    failed = sum(item.status == "failed" for item in items)
    image_files = len(list(IMAGES.glob("*")))
    readme = f"""디시인사이드 댓글 이미지 저장 결과

게시글: {POST_URL}
수집 시각(UTC): {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}
댓글 페이지 수: {meta.get('pages_with_comments', 0)}
확인한 고유 댓글 수: {meta.get('unique_comment_ids_seen', 0)}
발견한 댓글 이미지 수: {len(items)}
저장 성공: {saved}
저장 실패: {failed}
실제 이미지 파일 수: {image_files}

게시글 본문 이미지는 제외하고 댓글 안에 첨부된 이미지/DCCon만 저장했습니다.
GIF/WebP/AVIF 등은 가능한 한 원본 형식을 유지했습니다.
manifest.csv에는 댓글 번호, 작성자, 원본 주소, 저장 파일명이 들어 있습니다.
"""
    if errors:
        readme += "\n경고:\n- " + "\n- ".join(errors) + "\n"
    (WORK / "README.txt").write_text(readme, encoding="utf-8")

    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(WORK.rglob("*")):
            if path.is_file() and DEBUG not in path.parents:
                zf.write(path, path.relative_to(WORK))

    summary = {
        **meta,
        "occurrences": len(items),
        "saved": saved,
        "failed": failed,
        "image_files": image_files,
        "zip_path": str(ZIP_PATH),
        "zip_bytes": ZIP_PATH.stat().st_size,
        "zip_sha256": hashlib.sha256(ZIP_PATH.read_bytes()).hexdigest(),
        "errors": errors,
    }
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return summary


def main() -> int:
    reset_dirs()
    errors: list[str] = []
    try:
        items, meta = collect_occurrences()
    except Exception as exc:
        errors.append(f"collection: {type(exc).__name__}: {exc}")
        items, meta = [], {
            "pages_with_comments": 0,
            "unique_comment_ids_seen": 0,
            "occurrences_found": 0,
        }
    download_all(items)
    summary = write_outputs(items, meta, errors)
    return 0 if summary["saved"] > 0 else 2


if __name__ == "__main__":
    sys.exit(main())
