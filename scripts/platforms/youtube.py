"""YouTube publisher — Data API v3 resumable upload over OAuth2.

Unlike the other platforms in this package this is a pure HTTP integration:
no Chrome, no CDP, no Playwright. An API key cannot upload (Google answers
401 "API keys are not supported by this API"); the access token comes from
the loopback OAuth flow in scripts/youtube_auth.py with the upload-only
scope https://www.googleapis.com/auth/youtube.upload.

Quanta: one videos.insert costs ~1,600 quota units against the default
10,000/day project budget — i.e. at most ~6 uploads per day. The queue
integration is therefore opt-in (ONLY_PLATFORMS=youtube), never a default.

Public surface, matching the other platform modules:
- publish_via_api(**kwargs) -> PublishResult
- a CLI for one-off uploads: python3 scripts/platforms/youtube.py FILE ...
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from publisher import PublishResult
from youtube_auth import (DEFAULT_OPENER, OAuthError, get_access_token,
                          guess_mime_type)

UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
# snippet + status are the only parts this client writes.
UPLOAD_PARTS = "snippet,status"

TITLE_MAX = 100
DESC_MAX = 5000
TAG_MAX_COUNT = 15
TAG_MAX_LEN = 30
TAGS_TOTAL_BUDGET = 400

CHUNK_BYTES = 4 * 1024 * 1024
REQUEST_TIMEOUT_S = 60
MAX_ATTEMPTS = 5
# Uploads default to private: a first API run must never push a public video
# by surprise. Flip per upload with --privacy unlisted|public.
DEFAULT_PRIVACY = "private"
VALID_PRIVACY = ("private", "unlisted", "public")

RETRIABLE_STATUS = {429, 500, 502, 503, 504}


class UploadError(RuntimeError):
    """The resumable upload failed permanently (or exhausted retries)."""


def build_metadata(title: str, *, description: str = "", topics=None,
                   privacy: str = DEFAULT_PRIVACY, category_id: str | None = None,
                   publish_at: str | datetime | None = None,
                   default_language: str | None = None,
                   audio_language: str | None = None,
                   license_: str = "youtube",
                   public_stats: bool = True) -> dict:
    """Build the videos.insert snippet+status body with YouTube's limits."""
    title = (title or "").strip()[:TITLE_MAX]
    if not title:
        raise ValueError("title is required (YouTube rejects an empty title)")
    if privacy not in VALID_PRIVACY:
        raise ValueError(f"privacy must be one of {VALID_PRIVACY}, got {privacy!r}")
    if license_ not in ("youtube", "creativeCommon"):
        raise ValueError("license must be 'youtube' or 'creativeCommon'")

    tags, budget = [], TAGS_TOTAL_BUDGET
    for raw in topics or []:
        tag = str(raw).lstrip("#").strip()[:TAG_MAX_LEN]
        if not tag or tag in tags:
            continue
        if len(tags) >= TAG_MAX_COUNT or len(tag) > budget:
            break
        tags.append(tag)
        budget -= len(tag) + 1

    snippet = {
        "title": title,
        "description": (description or "").strip()[:DESC_MAX],
        "tags": tags,
    }
    if category_id:
        snippet["categoryId"] = str(category_id)
    if default_language:
        # BCP-47, e.g. zh-CN / en.
        snippet["defaultLanguage"] = default_language
    if audio_language:
        snippet["defaultAudioLanguage"] = audio_language

    status = {
        "privacyStatus": privacy,
        # Explicit audience selection; without it Studio keeps nagging and
        # API-uploaded videos can sit in an "audience unset" limbo.
        "selfDeclaredMadeForKids": False,
        "embeddable": True,
        # Standard YouTube license; public view counter visible to everyone.
        "license": license_,
        "publicStatsViewable": bool(public_stats),
    }
    if publish_at is not None:
        status["privacyStatus"] = "private"
        status["publishAt"] = _rfc3339(publish_at)

    return {"snippet": snippet, "status": status}


def _rfc3339(when: str | datetime) -> str:
    """Normalize a datetime (or passthrough an RFC3339 string) for publishAt."""
    if isinstance(when, str):
        return when
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_api_error(err: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(err.read().decode("utf-8", "replace"))
        detail = body["error"]["errors"][0]
        return f"{detail.get('reason') or 'http_' + str(err.code)}: {detail.get('message','')[:160]}"
    except Exception:
        return f"http_{err.code}"


def _backoff_sleep(attempt: int, retry_after: str | None) -> None:
    if retry_after:
        try:
            time.sleep(min(float(retry_after), 30.0))
            return
        except ValueError:
            pass
    time.sleep(min(2 ** attempt, 30))


def initiate_session(token: str, metadata: dict, file_size: int,
                     mime_type: str, *, upload_url: str = UPLOAD_URL,
                     notify_subscribers: bool = False, opener=None) -> str:
    """Create the resumable session; return its upload URL (Location header)."""
    url = (f"{upload_url}?uploadType=resumable&part={UPLOAD_PARTS}"
           f"&notifySubscribers={'true' if notify_subscribers else 'false'}")
    req = urllib.request.Request(url, data=json.dumps(metadata).encode("utf-8"),
                                 method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json; charset=UTF-8")
    req.add_header("X-Upload-Content-Length", str(file_size))
    req.add_header("X-Upload-Content-Type", mime_type)
    open_ = (opener or DEFAULT_OPENER).open
    try:
        with open_(req, timeout=REQUEST_TIMEOUT_S) as resp:
            location = resp.headers.get("Location")
    except urllib.error.HTTPError as e:
        detail = _parse_api_error(e)
        if detail.startswith("youtubeSignupRequired"):
            detail += (" — the authorized Google account has no YouTube "
                       "channel yet; create one at https://www.youtube.com/"
                       "create_channel (or re-authorize the correct brand "
                       "account), then retry")
        raise UploadError(f"session rejected: {detail}") from None
    except urllib.error.URLError as e:
        raise UploadError(f"session endpoint unreachable: {e.reason}") from None
    if not location:
        raise UploadError("session response carried no Location header")
    return location


def _received_offset(resp_headers) -> int | None:
    """Parse a 308 Range header ('bytes=0-1048575') into the next byte offset."""
    rng = resp_headers.get("Range") or resp_headers.get("range")
    if not rng or "-" not in rng:
        return None
    try:
        return int(rng.split("-", 1)[1]) + 1
    except ValueError:
        return None


def _put(session_url: str, data: bytes, content_range: str,
         content_type: str, *, opener=None):
    """PUT one chunk. Returns (status, body, headers); urllib surfaces a 308
    as HTTPError, so headers come from the exception object in that case."""
    req = urllib.request.Request(session_url, data=data, method="PUT")
    req.add_header("Content-Range", content_range)
    req.add_header("Content-Type", content_type)
    try:
        with (opener or DEFAULT_OPENER).open(req,
                                                timeout=REQUEST_TIMEOUT_S) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def upload_file(token: str, video_path: str | Path, metadata: dict,
                *, upload_url: str = UPLOAD_URL,
                notify_subscribers: bool = False, opener=None,
                progress=None) -> dict:
    """Run a resumable, chunked upload; return the created video resource.

    progress(bytes_sent, total_bytes) is called after every accepted chunk.
    """
    path = Path(video_path)
    if not path.is_file():
        raise UploadError(f"file_missing:{path}")
    size = path.stat().st_size
    if size <= 0:
        raise UploadError(f"empty_file:{path}")
    mime_type = guess_mime_type(path)
    session_url = initiate_session(token, metadata, size, mime_type,
                                   upload_url=upload_url,
                                   notify_subscribers=notify_subscribers,
                                   opener=opener)

    offset = 0
    with path.open("rb") as fh:
        while offset < size:
            fh.seek(offset)
            chunk = fh.read(CHUNK_BYTES)
            end = offset + len(chunk) - 1
            content_range = f"bytes {offset}-{end}/{size}"
            status, body, headers = None, b"", None
            for attempt in range(MAX_ATTEMPTS):
                status, body, headers = _put(session_url, chunk, content_range,
                                             mime_type, opener=opener)
                if status in (200, 201, 308):
                    break
                if status == 408 or status in RETRIABLE_STATUS:
                    retry_after = headers.get("Retry-After") if headers else None
                    _backoff_sleep(attempt, retry_after)
                    continue
                raise UploadError(f"upload rejected (HTTP {status}): "
                                  f"{body.decode('utf-8','replace')[:200]}")
            else:
                raise UploadError(f"chunk upload exhausted retries at {content_range}")

            if status == 308:
                new_offset = _received_offset(headers)
                if new_offset is None:
                    # No Range on this 308 (observed after a retried timeout):
                    # re-query the server-side position explicitly.
                    new_offset = _query_server_position(session_url, size, opener)
                if new_offset is None or new_offset <= offset:
                    raise UploadError("server reported no upload progress")
                offset = new_offset
                if progress is not None:
                    progress(offset, size)
                continue

            try:
                resource = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                raise UploadError("upload finished without a video resource") from None
            if not resource.get("id"):
                raise UploadError(f"upload response missing video id: {body[:200]!r}")
            return resource
    raise UploadError("unexpected end of upload loop")


def _query_server_position(session_url: str, size: int, opener):
    """Ask Google how many bytes it already holds (empty status PUT)."""
    req = urllib.request.Request(session_url, data=b"", method="PUT")
    req.add_header("Content-Range", f"bytes */{size}")
    try:
        with (opener or DEFAULT_OPENER).open(req,
                                                timeout=REQUEST_TIMEOUT_S) as resp:
            if resp.status == 308:
                return _received_offset(resp.headers)
            # 2xx here means the upload actually completed during the retry gap.
            return size if resp.status in (200, 201) else None
    except urllib.error.HTTPError as e:
        if e.code == 308:
            return _received_offset(e.headers)
        return None
    except urllib.error.URLError:
        return None


def publish_via_api(*, title, description="", video, topics=None,
                    privacy: str = DEFAULT_PRIVACY, category_id=None,
                    publish_at=None, default_language=None,
                    audio_language=None, license_: str = "youtube",
                    public_stats: bool = True, notify_subscribers: bool = False,
                    upload_url: str = UPLOAD_URL, opener=None,
                    progress=None, **kwargs) -> PublishResult:
    """Upload one video. The contract every platform module shares."""
    t0 = time.time()
    try:
        metadata = build_metadata(
            title, description=description, topics=topics,
            privacy=privacy, category_id=category_id, publish_at=publish_at,
            default_language=default_language, audio_language=audio_language,
            license_=license_, public_stats=public_stats)
        token = get_access_token()
        resource = upload_file(token, video, metadata,
                               upload_url=upload_url,
                               notify_subscribers=notify_subscribers,
                               opener=opener, progress=progress)
    except OAuthError as e:
        return PublishResult("youtube", "fail", method="api",
                             duration_s=time.time() - t0,
                             error=f"oauth:{str(e)[:180]}")
    except (UploadError, ValueError) as e:
        return PublishResult("youtube", "fail", method="api",
                             duration_s=time.time() - t0,
                             error=str(e)[:200])

    vid = resource["id"]
    return PublishResult("youtube", "ok", method="api",
                         duration_s=time.time() - t0,
                         video_id=vid,
                         url=f"https://youtu.be/{vid}",
                         privacy=resource.get("status", {}).get("privacyStatus",
                                                                privacy))


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Upload a video to YouTube (Data API v3)")
    p.add_argument("video", type=Path, help="path to the video file")
    p.add_argument("--title", help=f"defaults to the file stem (max {TITLE_MAX})")
    p.add_argument("--description", default="")
    p.add_argument("--tags", nargs="*", default=[], help="plain tags, '#' optional")
    p.add_argument("--privacy", choices=VALID_PRIVACY, default=DEFAULT_PRIVACY)
    p.add_argument("--category", dest="category_id", default="22",
                   help="YouTube categoryId, e.g. 22 (People & Blogs, default)")
    p.add_argument("--language", default="zh-CN",
                   help="snippet.defaultLanguage (BCP-47); '' disables it")
    p.add_argument("--audio-language", default="",
                   help="snippet.defaultAudioLanguage (BCP-47), e.g. zh")
    p.add_argument("--license", dest="license_",
                   choices=("youtube", "creativeCommon"), default="youtube")
    p.add_argument("--notify-subscribers", action="store_true",
                   help="push a subscriber notification on insert")
    p.add_argument("--no-public-stats", dest="public_stats",
                   action="store_false", help="hide the public view counter")
    p.add_argument("--publish-at", dest="publish_at",
                   help="RFC3339 timestamp; forces private until that time")
    p.add_argument("--dry-run", action="store_true",
                   help="print the metadata body and exit")
    return p


def main(argv=None) -> int:
    args = _build_arg_parser().parse_args(argv)
    metadata = build_metadata(
        args.title or args.video.stem,
        description=args.description,
        topics=args.tags,
        privacy=args.privacy,
        category_id=args.category_id,
        publish_at=args.publish_at,
        default_language=args.language or None,
        audio_language=args.audio_language or None,
        license_=args.license_,
        public_stats=args.public_stats,
    )
    if args.dry_run:
        print(json.dumps(metadata, ensure_ascii=False, indent=2))
        print(f"\nnotifySubscribers={args.notify_subscribers}")
        print(f"source: {args.video} ({args.video.stat().st_size if args.video.exists() else 'MISSING'} bytes)")
        return 0

    def _progress(sent: int, total: int) -> None:
        pct = 100.0 * sent / total if total else 0
        print(f"\r  upload {sent / 1e6:7.1f}/{total / 1e6:.1f} MB ({pct:4.1f}%)",
              end="", file=sys.stderr, flush=True)

    result = publish_via_api(title=metadata["snippet"]["title"],
                             description=metadata["snippet"]["description"],
                             video=str(args.video),
                             topics=metadata["snippet"].get("tags"),
                             privacy=args.privacy,
                             category_id=args.category_id,
                             publish_at=args.publish_at,
                             default_language=args.language or None,
                             audio_language=args.audio_language or None,
                             license_=args.license_,
                             public_stats=args.public_stats,
                             notify_subscribers=args.notify_subscribers,
                             progress=_progress)
    if result.status == "ok":
        print("", file=sys.stderr)  # close the \r progress line
        print(f"[youtube] uploaded {result.details['video_id']} "
              f"({result.details['privacy']}) {result.details['url']} "
              f"in {result.duration_s:.1f}s")
        return 0
    print(f"[youtube] failed: {result.error}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
