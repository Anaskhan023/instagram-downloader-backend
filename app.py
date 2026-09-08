from flask import Flask, request, jsonify, Response, send_file
import os
import tempfile
import shutil
import subprocess
import time
import hmac
import hashlib
import logging
import threading
from urllib.parse import urlparse

import requests


app = Flask(__name__)


# =========================================================
# CONFIG
# =========================================================

ALLOWED_HOSTS = {
    "instagram.com",
    "www.instagram.com",
    "m.instagram.com",
}

BACKEND_API_SECRET = os.environ.get(
    "BACKEND_API_SECRET",
    ""
)

FASTSAVER_API_KEY = os.environ.get(
    "FASTSAVER_API_KEY",
    ""
)

FASTSAVER_ENDPOINT = (
    "https://api.fastsaver.io/v1/fetch"
)

# WordPress/API protection
RATE_WINDOW = 60

MAX_RESOLVE_REQUESTS = 12
MAX_DOWNLOAD_REQUESTS = 8

# WordPress signed download token
DOWNLOAD_TOKEN_TTL = 180

# FastSaver media URLs are temporary.
# Keep them only briefly.
MEDIA_CACHE_TTL = 90

# Prevent too many simultaneous API/download operations.
MAX_CONCURRENT_RESOLVES = 2
MAX_CONCURRENT_DOWNLOADS = 2

# Maximum file size we allow.
MAX_FILE_SIZE = 100 * 1024 * 1024

# FastSaver/server retry behavior.
MAX_API_RETRIES = 1


# =========================================================
# STATE
# =========================================================

request_log = {}

resolve_cache = {}

resolve_cache_lock = threading.Lock()

resolve_semaphore = threading.Semaphore(
    MAX_CONCURRENT_RESOLVES
)

download_semaphore = threading.Semaphore(
    MAX_CONCURRENT_DOWNLOADS
)


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

logger = logging.getLogger(__name__)


# =========================================================
# CLIENT IP
# =========================================================

def get_client_ip():
    return request.remote_addr or "unknown"


# =========================================================
# RATE LIMIT
# =========================================================

def rate_limit_ok(
    action,
    max_requests,
    window=RATE_WINDOW
):

    ip = get_client_ip()

    key = f"{ip}|{action}"

    now = time.time()

    timestamps = request_log.setdefault(
        key,
        []
    )

    timestamps[:] = [
        ts
        for ts in timestamps
        if ts > now - window
    ]

    if len(timestamps) >= max_requests:
        return False

    timestamps.append(now)

    return True


# =========================================================
# BACKEND AUTH
# =========================================================

def is_authorized_server_request():

    if not BACKEND_API_SECRET:
        logger.error(
            "BACKEND_API_SECRET is missing."
        )
        return False

    supplied_secret = request.headers.get(
        "X-Backend-Secret",
        ""
    )

    return hmac.compare_digest(
        supplied_secret,
        BACKEND_API_SECRET
    )


def unauthorized():

    return jsonify({
        "success": False,
        "message": "Unauthorized request."
    }), 403


# =========================================================
# INSTAGRAM URL VALIDATION
# =========================================================

def is_valid_instagram_url(url):

    try:

        parsed = urlparse(url)

        host = (
            parsed.hostname
            or ""
        ).lower()

        path = (
            parsed.path
            or ""
        )

        return (
            parsed.scheme in {
                "http",
                "https"
            }

            and host in ALLOWED_HOSTS

            and (
                path.startswith("/p/")
                or path.startswith("/reel/")
                or path.startswith("/tv/")
            )
        )

    except Exception:

        return False


# =========================================================
# SIGNATURE
# =========================================================

def create_signature(
    url,
    file_format,
    expires
):

    message = (
        url
        + "|"
        + file_format
        + "|"
        + str(expires)
    )

    return hmac.new(
        BACKEND_API_SECRET.encode(
            "utf-8"
        ),
        message.encode(
            "utf-8"
        ),
        hashlib.sha256
    ).hexdigest()


def verify_download_signature(
    url,
    file_format,
    expires,
    signature
):

    if not BACKEND_API_SECRET:
        return False

    try:
        expires_int = int(
            expires
        )
    except (
        TypeError,
        ValueError
    ):
        return False

    now = int(
        time.time()
    )

    if expires_int < now:
        return False

    if expires_int > (
        now
        + DOWNLOAD_TOKEN_TTL
        + 30
    ):
        return False

    expected = create_signature(
        url,
        file_format,
        expires_int
    )

    return hmac.compare_digest(
        signature or "",
        expected
    )


# =========================================================
# CACHE
# =========================================================

def get_cached_media(url):

    now = time.time()

    with resolve_cache_lock:

        item = resolve_cache.get(
            url
        )

        if not item:
            return None

        if item["expires_at"] <= now:

            resolve_cache.pop(
                url,
                None
            )

            return None

        return dict(
            item["data"]
        )


def set_cached_media(
    url,
    data
):

    with resolve_cache_lock:

        resolve_cache[url] = {
            "expires_at": (
                time.time()
                + MEDIA_CACHE_TTL
            ),
            "data": dict(data)
        }


def clear_expired_cache():

    now = time.time()

    with resolve_cache_lock:

        expired = [
            key
            for key, item
            in resolve_cache.items()
            if item["expires_at"] <= now
        ]

        for key in expired:

            resolve_cache.pop(
                key,
                None
            )


# =========================================================
# FASTSAVER API
# =========================================================

def fastsaver_fetch(
    instagram_url
):

    if not FASTSAVER_API_KEY:

        raise RuntimeError(
            "FASTSAVER_API_KEY is missing."
        )

    payload = {
        "url": instagram_url
    }

    headers = {
        "X-Api-Key":
            FASTSAVER_API_KEY,

        "Content-Type":
            "application/json",

        "Accept":
            "application/json",
    }

    last_error = None

    for attempt in range(
        MAX_API_RETRIES + 1
    ):

        try:

            response = requests.post(
                FASTSAVER_ENDPOINT,
                headers=headers,
                json=payload,
                timeout=30
            )

            logger.info(
                "FastSaver response: %s",
                response.status_code
            )

            # ---------------------------------------------
            # SUCCESS
            # ---------------------------------------------

            if response.ok:

                data = response.json()

                return data


            # ---------------------------------------------
            # RATE LIMITED
            # ---------------------------------------------

            if response.status_code == 429:

                retry_after = (
                    response.headers.get(
                        "Retry-After"
                    )
                    or "5"
                )

                try:
                    wait_seconds = min(
                        int(retry_after),
                        30
                    )
                except (
                    TypeError,
                    ValueError
                ):
                    wait_seconds = 5

                logger.warning(
                    "FastSaver rate limited. "
                    "Retry-After=%s",
                    wait_seconds
                )

                if attempt < MAX_API_RETRIES:

                    time.sleep(
                        wait_seconds
                    )

                    continue

                raise RuntimeError(
                    "FastSaver rate limit reached."
                )


            # ---------------------------------------------
            # OTHER API ERRORS
            # ---------------------------------------------

            try:

                error_data = (
                    response.json()
                )

                error_message = (
                    error_data.get(
                        "message"
                    )
                    or "FastSaver request failed."
                )

                error_code = (
                    error_data.get(
                        "code"
                    )
                    or ""
                )

            except Exception:

                error_message = (
                    "FastSaver request failed."
                )

                error_code = ""


            # One retry for temporary server failure
            if (
                response.status_code >= 500
                and attempt < MAX_API_RETRIES
            ):

                time.sleep(2)

                continue


            raise RuntimeError(
                f"{error_code}: {error_message}"
                if error_code
                else error_message
            )


        except requests.RequestException as error:

            last_error = error

            logger.warning(
                "FastSaver network error: %s",
                error
            )

            if attempt < MAX_API_RETRIES:

                time.sleep(2)

                continue

            raise RuntimeError(
                "FastSaver service is temporarily unavailable."
            )


        except ValueError as error:

            logger.exception(
                "FastSaver returned invalid JSON."
            )

            raise RuntimeError(
                "FastSaver returned an invalid response."
            )


    if last_error:

        raise RuntimeError(
            "FastSaver request failed."
        )

    raise RuntimeError(
        "FastSaver request failed."
    )


# =========================================================
# PARSE FASTSAVER RESPONSE
# =========================================================

def parse_fastsaver_video(
    data
):

    if not isinstance(
        data,
        dict
    ):
        raise RuntimeError(
            "Invalid FastSaver response."
        )

    if data.get(
        "error"
    ):

        raise RuntimeError(
            data.get(
                "message"
            )
            or "FastSaver could not process this URL."
        )

    medias = (
        data.get("medias")
        or []
    )

    if not medias:

        raise RuntimeError(
            "No downloadable media was found."
        )

    video_media = None

    for media in medias:

        if not isinstance(
            media,
            dict
        ):
            continue

        media_type = (
            media.get("type")
            or ""
        ).lower()

        extension = (
            media.get("ext")
            or ""
        ).lower()

        media_url = (
            media.get("url")
            or ""
        )

        if not media_url:
            continue

        if (
            media_type == "video"
            or extension == "mp4"
        ):

            video_media = media
            break

    if not video_media:

        raise RuntimeError(
            "This Instagram post does not contain "
            "a downloadable video."
        )

    media_url = (
        video_media.get("url")
        or ""
    )

    if not media_url:

        raise RuntimeError(
            "FastSaver did not return a media URL."
        )

    return {
        "media_url": media_url,

        "title": (
            data.get("title")
            or "Instagram Media"
        ),

        "author": (
            data.get("author")
            or ""
        ),

        "thumbnail": (
            data.get("thumbnail")
            or ""
        ),

        "duration": (
            data.get("duration")
        ),

        "platform": (
            data.get("platform")
            or "instagram"
        ),

        "kind": (
            data.get("kind")
            or "single"
        ),
    }


# =========================================================
# FETCH MEDIA FROM FASTSAVER
# =========================================================

def resolve_media(
    instagram_url
):

    clear_expired_cache()

    cached = get_cached_media(
        instagram_url
    )

    if cached:

        logger.info(
            "Using cached FastSaver media."
        )

        return cached


    data = fastsaver_fetch(
        instagram_url
    )

    parsed = parse_fastsaver_video(
        data
    )

    set_cached_media(
        instagram_url,
        parsed
    )

    return parsed


# =========================================================
# DOWNLOAD MEDIA URL
# =========================================================

def download_media_to_file(
    media_url,
    destination
):

    response = requests.get(
        media_url,
        headers={
            "User-Agent":
                "Mozilla/5.0"
        },
        stream=True,
        timeout=45
    )

    response.raise_for_status()

    content_length = (
        response.headers.get(
            "Content-Length"
        )
    )

    if content_length:

        try:

            if int(
                content_length
            ) > MAX_FILE_SIZE:

                raise RuntimeError(
                    "Media file is too large."
                )

        except ValueError:
            pass


    total = 0

    with open(
        destination,
        "wb"
    ) as file:

        for chunk in response.iter_content(
            chunk_size=1024 * 256
        ):

            if not chunk:
                continue

            total += len(
                chunk
            )

            if total > MAX_FILE_SIZE:

                file.close()

                try:
                    os.remove(
                        destination
                    )
                except OSError:
                    pass

                raise RuntimeError(
                    "Media file is too large."
                )

            file.write(
                chunk
            )

    if total <= 0:

        raise RuntimeError(
            "Downloaded media file is empty."
        )


# =========================================================
# SECURITY HEADERS
# =========================================================

@app.after_request
def add_security_headers(
    response
):

    response.headers[
        "X-Content-Type-Options"
    ] = "nosniff"

    response.headers[
        "X-Frame-Options"
    ] = "DENY"

    response.headers[
        "Referrer-Policy"
    ] = "strict-origin-when-cross-origin"

    response.headers[
        "Cache-Control"
    ] = "no-store"

    response.headers[
        "Access-Control-Allow-Origin"
    ] = "https://savevori.com"

    response.headers[
        "Access-Control-Allow-Methods"
    ] = "GET, POST, OPTIONS"

    response.headers[
        "Access-Control-Allow-Headers"
    ] = (
        "Content-Type, X-Backend-Secret"
    )

    return response


# =========================================================
# HEALTH CHECK
# =========================================================

@app.route(
    "/",
    methods=["GET"]
)
def home():

    return jsonify({
        "status": "success",
        "message": (
            "SaveVori FastSaver backend is running."
        )
    })


# =========================================================
# RESOLVE
# =========================================================

@app.route(
    "/api/resolve",
    methods=[
        "POST",
        "GET",
        "OPTIONS"
    ]
)
def resolve_instagram():

    if request.method == "OPTIONS":

        return Response(
            status=204
        )


    # -----------------------------------------------------
    # SERVER AUTH
    # -----------------------------------------------------

    if not is_authorized_server_request():

        logger.warning(
            "Unauthorized resolve request."
        )

        return unauthorized()


    # -----------------------------------------------------
    # RATE LIMIT
    # -----------------------------------------------------

    if not rate_limit_ok(
        "resolve",
        MAX_RESOLVE_REQUESTS
    ):

        return jsonify({
            "success": False,
            "message": (
                "Too many requests. "
                "Please wait a moment."
            )
        }), 429


    # -----------------------------------------------------
    # GET URL
    # -----------------------------------------------------

    if request.method == "POST":

        body = (
            request.get_json(
                silent=True
            )
            or {}
        )

        url = (
            body.get("url")
            or ""
        ).strip()

    else:

        url = (
            request.args.get("url")
            or ""
        ).strip()


    # -----------------------------------------------------
    # VALIDATION
    # -----------------------------------------------------

    if not url:

        return jsonify({
            "success": False,
            "message": (
                "Please enter an Instagram URL."
            )
        }), 400


    if len(url) > 1000:

        return jsonify({
            "success": False,
            "message": "URL is too long."
        }), 400


    if not is_valid_instagram_url(
        url
    ):

        return jsonify({
            "success": False,
            "message": (
                "Please enter a valid public "
                "Instagram post or Reel URL."
            )
        }), 400


    # -----------------------------------------------------
    # CONCURRENCY
    # -----------------------------------------------------

    acquired = resolve_semaphore.acquire(
        timeout=30
    )

    if not acquired:

        return jsonify({
            "success": False,
            "message": (
                "The service is busy. "
                "Please try again shortly."
            )
        }), 503


    try:

        logger.info(
            "Resolving Instagram URL through FastSaver."
        )

        try:

            media = resolve_media(
                url
            )

        except RuntimeError as error:

            logger.warning(
                "FastSaver resolve failed: %s",
                error
            )

            return jsonify({
                "success": False,
                "message": str(error)
            }), 400

        return jsonify({

            "success": True,

            "title":
                media["title"],

            "thumbnail":
                media["thumbnail"],

            "duration":
                media["duration"],

            "media_url":
                media["media_url"],

            "message":
                "Instagram media resolved successfully."

        }), 200


    finally:

        resolve_semaphore.release()


# =========================================================
# DOWNLOAD
# =========================================================

@app.route(
    "/api/download",
    methods=["GET"]
)
def download_instagram():

    # -----------------------------------------------------
    # AUTH
    # -----------------------------------------------------

    if not is_authorized_server_request():

        return unauthorized()


    # -----------------------------------------------------
    # RATE LIMIT
    # -----------------------------------------------------

    if not rate_limit_ok(
        "download",
        MAX_DOWNLOAD_REQUESTS
    ):

        return jsonify({
            "success": False,
            "message": (
                "Too many download requests. "
                "Please wait a moment."
            )
        }), 429


    # -----------------------------------------------------
    # PARAMETERS
    # -----------------------------------------------------

    url = (
        request.args.get("url")
        or ""
    ).strip()

    file_format = (
        request.args.get("format")
        or "mp3"
    ).lower()

    expires = (
        request.args.get("expires")
        or ""
    )

    signature = (
        request.args.get("signature")
        or ""
    )


    # -----------------------------------------------------
    # VALIDATION
    # -----------------------------------------------------

    if not is_valid_instagram_url(
        url
    ):

        return jsonify({
            "success": False,
            "message": (
                "Invalid Instagram URL."
            )
        }), 400


    if file_format not in {
        "mp3",
        "mp4"
    }:

        return jsonify({
            "success": False,
            "message": (
                "Unsupported format."
            )
        }), 400


    if not verify_download_signature(
        url,
        file_format,
        expires,
        signature
    ):

        return jsonify({
            "success": False,
            "message": (
                "Download link is invalid or expired."
            )
        }), 403


    # -----------------------------------------------------
    # DOWNLOAD CONCURRENCY
    # -----------------------------------------------------

    acquired = download_semaphore.acquire(
        timeout=60
    )

    if not acquired:

        return jsonify({
            "success": False,
            "message": (
                "The download service is busy. "
                "Please try again shortly."
            )
        }), 503


    temp_dir = tempfile.mkdtemp(
        prefix="savevori_"
    )

    try:

        # -------------------------------------------------
        # FFMPEG
        # -------------------------------------------------

        ffmpeg_path = shutil.which(
            "ffmpeg"
        )

        if not ffmpeg_path:

            return jsonify({
                "success": False,
                "message": (
                    "Media conversion is temporarily unavailable."
                )
            }), 500


        # -------------------------------------------------
        # GET RESOLVED FASTSAVER MEDIA
        # -------------------------------------------------

        try:

            media = resolve_media(
                url
            )

        except RuntimeError as error:

            logger.warning(
                "Media resolve during download failed: %s",
                error
            )

            return jsonify({
                "success": False,
                "message": str(error)
            }), 400


        media_url = (
            media.get("media_url")
            or ""
        )

        if not media_url:

            return jsonify({
                "success": False,
                "message": (
                    "Download media URL was not available."
                )
            }), 400


        # -------------------------------------------------
        # DOWNLOAD ORIGINAL VIDEO
        # -------------------------------------------------

        source_file = os.path.join(
            temp_dir,
            "instagram-source.mp4"
        )

        logger.info(
            "Downloading FastSaver media."
        )

        try:

            download_media_to_file(
                media_url,
                source_file
            )

        except Exception as error:

            logger.exception(
                "Failed to download FastSaver media."
            )

            return jsonify({
                "success": False,
                "message": (
                    "The media file could not be downloaded."
                )
            }), 502


        # =================================================
        # MP4
        # =================================================

        if file_format == "mp4":

            response = send_file(
                source_file,
                mimetype="video/mp4",
                as_attachment=True,
                download_name=(
                    "instagram-video.mp4"
                )
            )

            @response.call_on_close
            def cleanup_mp4():

                shutil.rmtree(
                    temp_dir,
                    ignore_errors=True
                )

            return response


        # =================================================
        # MP3
        # =================================================

        mp3_file = os.path.join(
            temp_dir,
            "instagram-audio.mp3"
        )


        ffmpeg_command = [

            ffmpeg_path,

            "-y",

            "-i",
            source_file,

            "-map",
            "0:a:0",

            "-vn",

            "-codec:a",
            "libmp3lame",

            "-b:a",
            "192k",

            mp3_file
        ]


        logger.info(
            "Extracting MP3 with FFmpeg."
        )


        try:

            process = subprocess.run(

                ffmpeg_command,

                stdout=subprocess.PIPE,

                stderr=subprocess.PIPE,

                text=True,

                timeout=120
            )

        except subprocess.TimeoutExpired:

            shutil.rmtree(
                temp_dir,
                ignore_errors=True
            )

            return jsonify({
                "success": False,
                "message": (
                    "Audio conversion took too long."
                )
            }), 500


        if process.returncode != 0:

            logger.error(
                "FFmpeg failed:\n%s",
                process.stderr[-4000:]
            )

            return jsonify({
                "success": False,
                "message": (
                    "Audio conversion failed."
                )
            }), 500


        if not os.path.exists(
            mp3_file
        ):

            return jsonify({
                "success": False,
                "message": (
                    "MP3 file was not created."
                )
            }), 500


        response = send_file(
            mp3_file,
            mimetype="audio/mpeg",
            as_attachment=True,
            download_name=(
                "instagram-audio.mp3"
            )
        )


        @response.call_on_close
        def cleanup_mp3():

            shutil.rmtree(
                temp_dir,
                ignore_errors=True
            )


        return response


    except Exception:

        logger.exception(
            "Instagram download failed."
        )

        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )

        return jsonify({
            "success": False,
            "message": (
                "Download failed. "
                "Please try again."
            )
        }), 500


    finally:

        download_semaphore.release()


# =========================================================
# START SERVER
# =========================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            10000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
