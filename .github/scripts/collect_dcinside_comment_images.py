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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

GALLERY_ID = "rescene1"
POST_NO = "352852"
MOBILE_POST_URL = f"https://m.dcinside.com/board/{GALLERY_ID}/{POST_NO}"
PC_POST_URL = (
    f"https://gall.dcinside.com/mgallery/board/view/"
    f"?id={GALLERY_ID}&no={POST_NO}"
)
MOBILE_COMMENT_URL = "https://m.dcinside.com/ajax/response-comment"
PC_COMMENT_URL = "https://gall.dcinside.com/board/comment/"

OUT_DIR = Path("out")
WORK_DIR = Path("work")
MEDIA_DIR = WORK_DIR / "images"
DEBUG_DIR = WORK_DIR / "debug"
ZIP_PATH = OUT_DIR / f"{GALLERY_ID}_{POST_NO}_comment_images.zip"

MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 14; SM-S928N Build/UP1A.231005.007; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
    "Chrome/131.0.0.0 Mobile Safari/537.36"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

MEDIA_ATTRS = ("data-original", "data-src", "data-url", "src", "href")
MEDIA_TAGS = ("img", "video", "source")
SKIP_HOSTS = {
    "nstatic.dcinside.com",
    "nstatic.dcinside.com.",
    "static.dcinside.com",
}
SKIP_URL_PARTS = (
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
class MediaOccurrence:
    page: int
    comment_no: str
    parent_no: str
    author: str
    date: str
    source_url: str
    source_api: str
    tag: str
    source_field: str
    comment_order: int
    media_order: int
    file_name: str = ""
    content_type: str = ""
    byte_size: int = 0
    sha256: str = ""
    status: str = "pending"
    error: str = ""


def reset_dirs() -> None:
    for path in (OUT_DIR, WORK_DIR):
        if path.exists():
            shutil.rmtree(path)
    OUT_DIR.mkdir(parents=True)
    MEDIA_DIR.mkdir(parents=True)
    DEBUG_DIR.mkdir(parents=True)


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    text = BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def sanitize_filename(value: str, limit: int = 35) -> str:
    value = safe_text(value)
    value = re.sub(r"[^\w가-힣.-]+", "_", value, flags=re.UNICODE).strip("_.")
    return value[:limit] or "anonymous"


def normalize_url(raw: str, base_url: str) -> str:
    raw = html_lib.unescape((raw or "").strip().strip("\"'"))
    raw = raw.replace("\\/", "/").rstrip("),]};")
    if not raw or raw.startswith(("data:", "javascript:", "#")):
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    elif raw.startswith("/"):
        raw = urljoin(base_url, raw)
    elif not re.match(r"^https?://", raw, flags=re.I):
        raw = urljoin(base_url, raw)
    return raw


def looks_like_comment_media(url: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    host = parsed.hostname.lower() if parsed.hostname else ""
    lower = url.lower()
    if host in SKIP_HOSTS:
        return False
    if any(part in lower for part in SKIP_URL_PARTS):
        return False
    if "dccon.php" in lower:
        return True
    if any(token in host for token in ("dcimg", "dcinside", "dccon")):
        return True
    if re.search(r"\.(?:jpe?g|png|gif|webp|avif|bmp|svg|mp4|webm)(?:$|[?#])", lower):
        return True
    return False


def extract_urls_from_html(
    fragment: str,
    base_url: str,
    source_field: str,
) -> list[tuple[str, str, str]]:
    results: list[tuple[str, str, str]] = []
    if not fragment:
        return results
    decoded = html_lib.unescape(fragment)
    soup = BeautifulSoup(decoded, "html.parser")
    for tag_name in MEDIA_TAGS:
        for tag in soup.find_all(tag_name):
            for attr in MEDIA_ATTRS:
                value = tag.get(attr)
                if not value:
                    continue
                url = normalize_url(str(value), base_url)
                if looks_like_comment_media(url):
                    results.append((url, tag_name, f"{source_field}.{attr}"))
                    break
    for match in re.findall(r"(?:(?:https?:)?//|/)[^\s\"'<>\\]+", decoded, flags=re.I):
        url = normalize_url(match, base_url)
        if looks_like_comment_media(url):
            results.append((url, "url", source_field))
    seen: set[tuple[str, str]] = set()
    unique: list[tuple[str, str, str]] = []
    for item in results:
        key = (item[0], item[1])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def choose_content_container(li: Any) -> Any:
    selectors = (
        "div.comment-txt",
        "div.comment_txt",
        "div.txt",
        "div.usertxt",
        "div.reply-content",
        "p",
    )
    for selector in selectors:
        node = li.select_one(selector)
        if node and node.find(list(MEDIA_TAGS)):
            return node
    for selector in selectors:
        node = li.select_one(selector)
        if node:
            return node
    return li


def get_attr_first(node: Any, names: Iterable[str]) -> str:
    for name in names:
        value = node.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def fetch_mobile_comments(session: requests.Session) -> tuple[list[MediaOccurrence], int]:
    occurrences: list[MediaOccurrence] = []
    seen_comments: set[str] = set()
    total_comments = 0

    session.headers.update({
        "User-Agent": MOBILE_UA,
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.7,en;q=0.6",
    })
    view = session.get(MOBILE_POST_URL, timeout=30)
    (DEBUG_DIR / "mobile_view_status.txt").write_text(
        f"status={view.status_code}\nurl={view.url}\nheaders={dict(view.headers)}\n",
        encoding="utf-8",
    )
    (DEBUG_DIR / "mobile_view_sample.html").write_text(
        view.text[:200000], encoding="utf-8", errors="replace"
    )
    view.raise_for_status()

    for page in range(1, 101):
        payload = {
            "id": GALLERY_ID,
            "no": POST_NO,
            "cpage": str(page),
            "managerskill": "",
            "del_scope": "1",
            "csort": "",
        }
        headers = {
            "User-Agent": MOBILE_UA,
            "Accept": "*/*",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://m.dcinside.com",
            "Referer": MOBILE_POST_URL,
        }
        response = session.post(
            MOBILE_COMMENT_URL, data=payload, headers=headers, timeout=30
        )
        if page <= 5:
            (DEBUG_DIR / f"mobile_comments_page_{page}.html").write_text(
                response.text[:500000], encoding="utf-8", errors="replace"
            )
            (DEBUG_DIR / f"mobile_comments_page_{page}_status.txt").write_text(
                f"status={response.status_code}\nheaders={dict(response.headers)}\n",
                encoding="utf-8",
            )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")
        items = soup.select("body > ul > li")
        if not items:
            items = soup.select("ul > li")
        if not items:
            items = soup.select("li[no], li[data-no], li.comment, li.comment-add")
        if not items:
            break

        new_on_page = 0
        for index, li in enumerate(items, 1):
            comment_no = get_attr_first(
                li, ("no", "data-no", "data-comment-no", "data-cmt-no")
            )
            if not comment_no:
                comment_no = hashlib.sha1(
                    str(li).encode("utf-8", errors="ignore")
                ).hexdigest()[:16]
            unique_comment_key = f"mobile:{comment_no}"
            if unique_comment_key in seen_comments:
                continue
            seen_comments.add(unique_comment_key)
            new_on_page += 1
            total_comments += 1

            parent_no = get_attr_first(
                li, ("data-parent", "parent", "data-parent-no", "data-reply-no")
            )
            author_node = li.select_one("a.nick, span.nick, .gall_writer, .name")
            author = safe_text(author_node.get_text(" ", strip=True) if author_node else "")
            date_node = li.select_one("span.date, .date_time, time")
            date = safe_text(date_node.get_text(" ", strip=True) if date_node else "")
            content = choose_content_container(li)
            media = extract_urls_from_html(str(content), MOBILE_POST_URL, "comment_html")
            for media_index, (url, tag, field) in enumerate(media, 1):
                occurrences.append(
                    MediaOccurrence(
                        page=page,
                        comment_no=comment_no,
                        parent_no=parent_no,
                        author=author,
                        date=date,
                        source_url=url,
                        source_api="mobile",
                        tag=tag,
                        source_field=field,
                        comment_order=total_comments,
                        media_order=media_index,
                    )
                )
        if new_on_page == 0:
            break
        time.sleep(0.2)

    return occurrences, total_comments


def extract_esno(text: str) -> str:
    soup = BeautifulSoup(text, "lxml")
    node = soup.select_one('input[name="e_s_n_o"]')
    if node and node.get("value"):
        return str(node.get("value"))
    patterns = (
        r'name=["\']e_s_n_o["\'][^>]*value=["\']([^"\']+)',
        r'e_s_n_o\s*[:=]\s*["\']([^"\']+)',
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            return html_lib.unescape(match.group(1))
    return ""


def recursively_find_media(
    value: Any,
    base_url: str,
    path: str = "root",
) -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            key_lower = str(key).lower()
            if isinstance(child, str) and (
                key_lower in {"memo", "comment_memo", "content", "dccon"}
                or "dccon" in key_lower
                or "image" in key_lower
                or key_lower.startswith("img")
            ):
                found.extend(extract_urls_from_html(child, base_url, child_path))
            elif isinstance(child, (dict, list)):
                found.extend(recursively_find_media(child, base_url, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(recursively_find_media(child, base_url, f"{path}[{index}]"))
    return found


def fetch_pc_comments(session: requests.Session) -> tuple[list[MediaOccurrence], int]:
    occurrences: list[MediaOccurrence] = []
    total_comments = 0
    seen_comments: set[str] = set()

    headers = {
        "User-Agent": DESKTOP_UA,
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.7,en;q=0.6",
        "Referer": "https://gall.dcinside.com/",
    }
    view = session.get(PC_POST_URL, headers=headers, timeout=30)
    (DEBUG_DIR / "pc_view_status.txt").write_text(
        f"status={view.status_code}\nurl={view.url}\nheaders={dict(view.headers)}\n",
        encoding="utf-8",
    )
    (DEBUG_DIR / "pc_view_sample.html").write_text(
        view.text[:200000], encoding="utf-8", errors="replace"
    )
    view.raise_for_status()
    esno = extract_esno(view.text)
    (DEBUG_DIR / "pc_esno.txt").write_text(esno, encoding="utf-8")

    for page in range(1, 101):
        payload = {
            "id": GALLERY_ID,
            "no": POST_NO,
            "cmt_id": GALLERY_ID,
            "cmt_no": POST_NO,
            "e_s_n_o": esno,
            "comment_page": str(page),
            "_GALLTYPE_": "M",
            "sort": "D",
            "prevCnt": "0",
            "board_type": "",
            "focus_cno": "",
            "focus_pno": "",
        }
        ajax_headers = {
            "User-Agent": DESKTOP_UA,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://gall.dcinside.com",
            "Referer": PC_POST_URL,
        }
        response = session.post(
            PC_COMMENT_URL, data=payload, headers=ajax_headers, timeout=30
        )
        if page <= 5:
            (DEBUG_DIR / f"pc_comments_page_{page}.txt").write_text(
                response.text[:1000000], encoding="utf-8", errors="replace"
            )
            (DEBUG_DIR / f"pc_comments_page_{page}_status.txt").write_text(
                f"status={response.status_code}\nheaders={dict(response.headers)}\n",
                encoding="utf-8",
            )
        response.raise_for_status()
        try:
            data = response.json()
        except Exception:
            break
        comments = data.get("comments", []) if isinstance(data, dict) else []
        if not isinstance(comments, list) or not comments:
            break

        new_on_page = 0
        for index, comment in enumerate(comments, 1):
            if not isinstance(comment, dict):
                continue
            comment_no = str(
                comment.get("no")
                or comment.get("comment_no")
                or comment.get("id")
                or f"p{page}_{index}"
            )
            unique_comment_key = f"pc:{comment_no}"
            if unique_comment_key in seen_comments:
                continue
            seen_comments.add(unique_comment_key)
            new_on_page += 1
            total_comments += 1
            parent_no = str(
                comment.get("parent")
                or comment.get("parent_no")
                or comment.get("reple_id")
                or ""
            )
            author = safe_text(comment.get("name") or comment.get("nick") or "")
            date = safe_text(comment.get("reg_date") or comment.get("date") or "")
            media = recursively_find_media(comment, PC_POST_URL, "comment")
            memo = str(comment.get("memo") or "")
            media.extend(extract_urls_from_html(memo, PC_POST_URL, "comment.memo"))
            dedup: set[str] = set()
            media_unique: list[tuple[str, str, str]] = []
            for item in media:
                if item[0] in dedup:
                    continue
                dedup.add(item[0])
                media_unique.append(item)
            for media_index, (url, tag, field) in enumerate(media_unique, 1):
                occurrences.append(
                    MediaOccurrence(
                        page=page,
                        comment_no=comment_no,
                        parent_no=parent_no,
                        author=author,
                        date=date,
                        source_url=url,
                        source_api="pc",
                        tag=tag,
                        source_field=field,
                        comment_order=total_comments,
                        media_order=media_index,
                    )
                )
        if new_on_page == 0:
            break
        total_page = 0
        if isinstance(data, dict):
            for key in ("total_page", "totalPage", "page_count"):
                try:
                    total_page = int(data.get(key) or 0)
                except Exception:
                    total_page = 0
                if total_page:
                    break
        if total_page and page >= total_page:
            break
        time.sleep(0.2)

    return occurrences, total_comments


def merge_occurrences(
    mobile: list[MediaOccurrence], pc: list[MediaOccurrence]
) -> list[MediaOccurrence]:
    merged: list[MediaOccurrence] = []
    seen: set[tuple[str, str]] = set()
    for item in mobile + pc:
        key = (item.comment_no, item.source_url)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    merged.sort(key=lambda x: (x.comment_order, x.media_order, x.comment_no))
    return merged


def detect_extension(data: bytes, content_type: str, url: str) -> str:
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
    if data[:4] in (b"\x00\x00\x00\x18", b"\x00\x00\x00\x1c", b"\x00\x00\x00 "):
        if b"ftypavif" in data[:32] or b"ftypavis" in data[:32]:
            return "avif"
        if b"ftyp" in data[:32]:
            return "mp4"
    if data.startswith(b"\x1aE\xdf\xa3"):
        return "webm"
    stripped = data.lstrip()[:200].lower()
    if stripped.startswith(b"<svg") or b"<svg" in stripped:
        return "svg"
    ctype = (content_type or "").split(";")[0].strip().lower()
    mapping = {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
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
    path_ext = Path(urlparse(url).path).suffix.lower().lstrip(".")
    allowed = {"jpg", "jpeg", "png", "gif", "webp", "avif", "bmp", "svg", "mp4", "webm"}
    if path_ext in allowed:
        return "jpg" if path_ext == "jpeg" else path_ext
    return "bin"


def download_occurrences(
    occurrences: list[MediaOccurrence],
    session: requests.Session,
) -> None:
    cache: dict[str, tuple[bytes, str, str]] = {}
    for sequence, item in enumerate(occurrences, 1):
        try:
            if item.source_url in cache:
                data, content_type, ext = cache[item.source_url]
            else:
                response = session.get(
                    item.source_url,
                    headers={
                        "User-Agent": MOBILE_UA,
                        "Referer": MOBILE_POST_URL,
                        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                    },
                    timeout=40,
                )
                response.raise_for_status()
                data = response.content
                content_type = response.headers.get("Content-Type", "")
                if not data:
                    raise RuntimeError("empty response")
                ext = detect_extension(data, content_type, item.source_url)
                if ext == "bin" and "html" in content_type.lower():
                    raise RuntimeError(
                        f"received HTML instead of media ({len(data)} bytes)"
                    )
                cache[item.source_url] = (data, content_type, ext)

            author_part = sanitize_filename(item.author)
            comment_part = sanitize_filename(item.comment_no, 24)
            file_name = (
                f"{sequence:03d}_comment_{comment_part}_{author_part}"
                f"_{item.media_order:02d}.{ext}"
            )
            destination = MEDIA_DIR / file_name
            destination.write_bytes(data)
            item.file_name = f"images/{file_name}"
            item.content_type = content_type
            item.byte_size = len(data)
            item.sha256 = hashlib.sha256(data).hexdigest()
            item.status = "saved"
        except Exception as exc:
            item.status = "failed"
            item.error = f"{type(exc).__name__}: {exc}"[:1000]


def write_outputs(
    occurrences: list[MediaOccurrence],
    mobile_count: int,
    pc_count: int,
    errors: list[str],
) -> None:
    manifest_path = WORK_DIR / "manifest.csv"
    fields = list(asdict(occurrences[0]).keys()) if occurrences else [
        "page", "comment_no", "parent_no", "author", "date", "source_url",
        "source_api", "tag", "source_field", "comment_order", "media_order",
        "file_name", "content_type", "byte_size", "sha256", "status", "error",
    ]
    with manifest_path.open("w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.DictWriter(fp, fieldnames=fields)
        writer.writeheader()
        for item in occurrences:
            writer.writerow(asdict(item))

    (WORK_DIR / "occurrences.json").write_text(
        json.dumps([asdict(item) for item in occurrences], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    saved = sum(1 for item in occurrences if item.status == "saved")
    failed = sum(1 for item in occurrences if item.status == "failed")
    unique_files = len(list(MEDIA_DIR.glob("*")))
    readme = f"""디시인사이드 댓글 이미지 수집 결과

게시글: {MOBILE_POST_URL}
갤러리/게시글 번호: {GALLERY_ID} / {POST_NO}
수집 시각(UTC): {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}

수집된 댓글 수:
- 모바일 댓글 응답: {mobile_count}
- PC 댓글 응답: {pc_count}

댓글 이미지/DCCon:
- 발견한 댓글별 이미지 발생 건수: {len(occurrences)}
- 저장 성공: {saved}
- 저장 실패: {failed}
- ZIP 안의 실제 이미지 파일 수: {unique_files}

안내:
- 게시글 본문 이미지는 제외하고 댓글 안의 이미지/DCCon만 수집했습니다.
- 같은 이미지가 여러 댓글에 반복된 경우 댓글별 파일로 각각 보존했습니다.
- GIF/WebP/AVIF 등 애니메이션 형식은 원본 형식을 유지했습니다.
- manifest.csv에서 댓글 번호, 작성자, 원본 URL, 저장 파일명을 확인할 수 있습니다.
"""
    if errors:
        readme += "\n수집 경고:\n- " + "\n- ".join(errors) + "\n"
    (WORK_DIR / "README.txt").write_text(readme, encoding="utf-8")

    if saved > 0 and failed == 0:
        shutil.rmtree(DEBUG_DIR, ignore_errors=True)

    with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(WORK_DIR.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(WORK_DIR))

    summary = {
        "mobile_comment_count": mobile_count,
        "pc_comment_count": pc_count,
        "occurrences": len(occurrences),
        "saved": saved,
        "failed": failed,
        "zip": str(ZIP_PATH),
        "zip_bytes": ZIP_PATH.stat().st_size,
        "zip_sha256": hashlib.sha256(ZIP_PATH.read_bytes()).hexdigest(),
        "errors": errors,
    }
    (OUT_DIR / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main() -> int:
    reset_dirs()
    errors: list[str] = []
    mobile_occurrences: list[MediaOccurrence] = []
    pc_occurrences: list[MediaOccurrence] = []
    mobile_count = 0
    pc_count = 0

    try:
        mobile_session = requests.Session()
        mobile_occurrences, mobile_count = fetch_mobile_comments(mobile_session)
    except Exception as exc:
        errors.append(f"mobile API: {type(exc).__name__}: {exc}")

    try:
        pc_session = requests.Session()
        pc_occurrences, pc_count = fetch_pc_comments(pc_session)
    except Exception as exc:
        errors.append(f"PC API: {type(exc).__name__}: {exc}")

    occurrences = merge_occurrences(mobile_occurrences, pc_occurrences)
    download_session = requests.Session()
    download_occurrences(occurrences, download_session)
    write_outputs(occurrences, mobile_count, pc_count, errors)

    saved = sum(1 for item in occurrences if item.status == "saved")
    return 0 if saved > 0 else 2


if __name__ == "__main__":
    sys.exit(main())
