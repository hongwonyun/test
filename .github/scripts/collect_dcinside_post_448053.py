#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import json
import mimetypes
import re
import shutil
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

GALLERY = "rescene1"
POST_NO = "448053"
POST_URL = f"https://m.dcinside.com/board/{GALLERY}/{POST_NO}"
UA = (
    "Mozilla/5.0 (Linux; Android 14; SM-S928N Build/UP1A.231005.007; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
    "Chrome/131.0.0.0 Mobile Safari/537.36"
)
ROOT = Path("collection")
IMAGE_DIR = ROOT / "post_images"
OUT = Path("out")
ZIP_PATH = OUT / f"{GALLERY}_{POST_NO}_post_images.zip"


def reset() -> None:
    for path in (ROOT, OUT):
        if path.exists():
            shutil.rmtree(path)
    IMAGE_DIR.mkdir(parents=True)
    OUT.mkdir(parents=True)


def find_body(soup: BeautifulSoup):
    for selector in (
        "div.thum-txtin",
        "div.thum-txt",
        "div.write_div",
        ".gallview-contents",
        ".writing_view_box",
    ):
        node = soup.select_one(selector)
        if node:
            return node
    return None


def collect_urls(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    body = find_body(soup)
    if body is None:
        raise RuntimeError("post body container not found")

    rows: list[dict] = []
    seen: set[str] = set()
    for tag in body.find_all(["img", "video", "source"]):
        url = ""
        source_attr = ""
        for attr in ("data-gif", "data-original", "data-src", "data-url", "src"):
            value = tag.get(attr)
            if not value:
                continue
            candidate = str(value).strip()
            if candidate.startswith("//"):
                candidate = "https:" + candidate
            host = (urlparse(candidate).hostname or "").lower()
            if host in {"nstatic.dcinside.com", "static.dcinside.com"}:
                continue
            if "dcimg" in host or "viewimage.php" in candidate.lower():
                url = candidate
                source_attr = attr
                break
        if not url or url in seen:
            continue
        if tag.name == "img" and not (
            tag.get("data-nummark") or tag.get("data-fileno") or "dcimg" in (urlparse(url).hostname or "").lower()
        ):
            continue
        seen.add(url)
        rows.append(
            {
                "index": len(rows) + 1,
                "data_nummark": str(tag.get("data-nummark") or ""),
                "data_fileno": str(tag.get("data-fileno") or ""),
                "alt": str(tag.get("alt") or ""),
                "source_attr": source_attr,
                "url": url,
            }
        )
    return rows


def sniff_extension(data: bytes, content_type: str, url: str) -> str:
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
    guessed = mimetypes.guess_extension((content_type or "").split(";", 1)[0].strip())
    if guessed in {".jpe", ".jpeg"}:
        guessed = ".jpg"
    if guessed:
        return guessed
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".svg"} else ".bin"


def download_one(row: dict, cookies: dict[str, str]) -> dict:
    result = dict(row)
    try:
        response = requests.get(
            row["url"],
            headers={
                "User-Agent": UA,
                "Referer": POST_URL,
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            },
            cookies=cookies,
            timeout=(8, 45),
        )
        response.raise_for_status()
        data = response.content
        if not data:
            raise RuntimeError("empty response")
        content_type = response.headers.get("Content-Type", "")
        extension = sniff_extension(data, content_type, row["url"])
        if extension == ".bin" and "html" in content_type.lower():
            raise RuntimeError("server returned HTML instead of an image")
        filename = f"{int(row['index']):03d}{extension}"
        (IMAGE_DIR / filename).write_bytes(data)
        result.update(
            {
                "file": f"post_images/{filename}",
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


def download_all(rows: list[dict], cookies: dict[str, str]) -> list[dict]:
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(12, max(1, len(rows)))) as pool:
        futures = {pool.submit(download_one, row, cookies): row for row in rows}
        for done, future in enumerate(as_completed(futures), 1):
            results.append(future.result())
            if done % 10 == 0 or done == len(rows):
                print(f"downloads={done}/{len(rows)}", flush=True)
    results.sort(key=lambda row: int(row["index"]))
    return results


def write_outputs(results: list[dict]) -> dict:
    fields = [
        "index", "data_nummark", "data_fileno", "alt", "source_attr", "url",
        "file", "content_type", "bytes", "sha256", "status", "error",
    ]
    with (ROOT / "manifest.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)

    formats: dict[str, int] = {}
    for row in results:
        if row["status"] == "saved":
            suffix = Path(row["file"]).suffix.lower() or ".unknown"
            formats[suffix] = formats.get(suffix, 0) + 1
    summary = {
        "post_url": POST_URL,
        "image_count": len(results),
        "saved": sum(row["status"] == "saved" for row in results),
        "failed": sum(row["status"] == "failed" for row in results),
        "formats": formats,
    }
    (ROOT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (ROOT / "README.txt").write_text(
        f"게시글: {POST_URL}\n"
        f"본문 원본 이미지: {summary['image_count']}개\n"
        f"저장 성공: {summary['saved']}개\n"
        f"저장 실패: {summary['failed']}개\n\n"
        "post_images 폴더에 게시글 순서대로 저장했습니다.\n"
        "manifest.csv에서 원본 URL, 파일 형식, 크기, SHA-256을 확인할 수 있습니다.\n"
        "확인 당시 댓글 13개에는 별도의 댓글 이미지가 없었습니다.\n",
        encoding="utf-8",
    )

    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(ROOT.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(ROOT))
    summary["zip_bytes"] = ZIP_PATH.stat().st_size
    summary["zip_sha256"] = hashlib.sha256(ZIP_PATH.read_bytes()).hexdigest()
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return summary


def main() -> int:
    reset()
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": UA,
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.7,en;q=0.6",
        }
    )
    response = session.get(POST_URL, timeout=(8, 30))
    response.raise_for_status()
    (ROOT / "source_post.html").write_text(
        response.text, encoding="utf-8", errors="replace"
    )
    rows = collect_urls(response.text)
    print(f"post_images_found={len(rows)}", flush=True)
    results = download_all(rows, session.cookies.get_dict())
    summary = write_outputs(results)
    return 0 if summary["saved"] > 0 and summary["failed"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
