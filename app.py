from flask import Flask, request, jsonify, Response, send_file
import yt_dlp
import os
import tempfile
import glob
import shutil
import subprocess
import time
import hmac
import hashlib
import logging
import threading
from urllib.parse import urlparse
from collections import OrderedDict

app = Flask(__name__)

# =========================================================
# CONFIG
# =========================================================

ALLOWED_HOSTS = {
    "instagram.com",
    "www.instagram.com",
    "m.instagram.com",
}

COMMON_HEADERS = {
    "Referer": "https://www.instagram.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/138.0.0.0 Safari/537.36"
    ),
}

BACKEND_API_SECRET = os.environ.get(
    "BACKEND_API_SECRET",
    ""
)

# WordPress / API request protection
RATE_WINDOW = 60
MAX_REQUESTS_PER_WINDOW = 12

# Secure download URL lifetime
DOWNLOAD_TOKEN_TTL = 180

# Cache successful Instagram metadata briefly.
# This is NOT used to bypass Instagram restrictions.
RESOLVE_CACHE_TTL = 120
RESOLVE_CACHE_MAX_ITEMS = 100

# Keep only a small number of Instagram resolves active
# at once. This reduces bursts against Instagram.
MAX_CONCURRENT_RESOLVES = 2

# Limited retries for temporary upstream failures.
MAX_RESOLVE_RETRIES = 2

# Small starting delay for 429/temporary errors.
RETRY_BASE_DELAY = 2


# =========================================================
# STATE
# =========================================================

request_log = {}

resolve_cache = OrderedDict()
resolve_cache_lock = threading.Lock()

resolve_semaphore = threading.Semaphore(
    MAX_CONCURRENT_RESOLVES
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
# SECURITY HELPERS
# =========================================================

def get_client_ip():
    """
    Return the direct client IP seen by Flask.

    Note:
    Behind a reverse proxy this may be the proxy IP unless
    the deployment is explicitly configured to trust forwarded
    headers. We intentionally do not blindly trust X-Forwarded-For.
    """
    return request.remote_addr or "unknown"


def cleanup_rate_log():
    now = time.time()

    expired = [
        ip
        for ip, timestamps in request_log.items()
        if not timestamps
        or timestamps[-1] < now - RATE_WINDOW
    ]

    for ip in expired:
        request_log.pop(ip, None)


def rate_limit_ok(
    action="default",
    max_requests=12,
    window=60
):
    """
    In-memory rate limiter.

    This limits requests seen by this Render instance.
    It is not a substitute for Instagram's own limits.
    """

    cleanup_rate_log()

    ip = get_client_ip()
    now = time.time()

    key = f"{ip}|{action}"

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


def too_many_requests():
    return jsonify({
        "success": False,
        "message": (
            "Too many requests. "
            "Please wait a moment."
        )
    }), 429


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
# CACHE HELPERS
# =========================================================

def cache_key(url):
    return url.strip()


def get_cached_resolve(url):

    key = cache_key(url)
    now = time.time()

    with resolve_cache_lock:

        item = resolve_cache.get(key)

        if not item:
            return None

        expires_at = item.get(
            "expires_at",
            0
        )

        if expires_at <= now:
            resolve_cache.pop(
                key,
                None
            )
            return None

        # Move recently used item to end.
        resolve_cache.move_to_end(
            key
        )

        # Return a copy so callers cannot mutate cache state.
        return dict(
            item["data"]
        )


def set_cached_resolve(
    url,
    data
):

    key = cache_key(url)

    with resolve_cache_lock:

        resolve_cache[key] = {
            "expires_at": (
                time.time()
                + RESOLVE_CACHE_TTL
            ),
            "data": dict(data)
        }

        resolve_cache.move_to_end(
            key
        )

        while len(resolve_cache) > RESOLVE_CACHE_MAX_ITEMS:
            resolve_cache.popitem(
                last=False
            )


# =========================================================
# YT-DLP
# =========================================================

def create_ydl_info_options():

    return {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,

        "http_headers": COMMON_HEADERS,

        "socket_timeout": 20,
    }


def get_ydl_info(url):

    ydl_opts = create_ydl_info_options()

    with yt_dlp.YoutubeDL(
        ydl_opts
    ) as ydl:

        return ydl.extract_info(
            url,
            download=False
        )


def is_probable_rate_limit_error(error):
    """
    Detect common 429/rate-limit messages without
    depending on one exact yt-dlp exception string.
    """

    text = str(error).lower()

    indicators = [
        "http error 429",
        "too many requests",
        "rate limit",
        "rate-limit",
        "temporarily blocked",
    ]

    return any(
        item in text
        for item in indicators
    )


def resolve_with_retry(url):

    last_error = None

    for attempt in range(
        MAX_RESOLVE_RETRIES + 1
    ):

        try:
            return get_ydl_info(
                url
            )

        except Exception as error:

            last_error = error

            if not is_probable_rate_limit_error(
                error
            ):
                raise

            if attempt >= MAX_RESOLVE_RETRIES:
                break

            delay = (
                RETRY_BASE_DELAY
                * (2 ** attempt)
            )

            logger.warning(
                "Temporary Instagram rate-limit "
                "response. Retry %s/%s after %ss.",
                attempt + 1,
                MAX_RESOLVE_RETRIES,
                delay
            )

            time.sleep(
                delay
            )

    if last_error:
        raise last_error

    raise RuntimeError(
        "Instagram resolve failed."
    )


# =========================================================
# AUDIO FORMAT SELECTION
# =========================================================

def choose_audio_format(info):

    formats = (
        info.get("formats")
        or []
    )

    candidates = []

    for fmt in formats:

        media_url = fmt.get("url")
        acodec = fmt.get("acodec")

        if (
            media_url
            and acodec
            and acodec != "none"
        ):
            candidates.append(
                fmt
            )

    if not candidates:
        return None

    def audio_score(fmt):

        try:
            abr = float(
                fmt.get("abr")
                or 0
            )
        except Exception:
            abr = 0

        try:
            tbr = float(
                fmt.get("tbr")
                or 0
            )
        except Exception:
            tbr = 0

        try:
            asr = int(
                fmt.get("asr")
                or 0
            )
        except Exception:
            asr = 0

        return (
            abr,
            tbr,
            asr
        )

    candidates.sort(
        key=audio_score,
        reverse=True
    )

    return candidates[0]


# =========================================================
# SIGNATURE VERIFICATION
# =========================================================

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

    if (
        expires_int
        > now + DOWNLOAD_TOKEN_TTL + 30
    ):
        return False

    message = (
        url
        + "|"
        + file_format
        + "|"
        + str(expires_int)
    )

    expected_signature = hmac.new(
        BACKEND_API_SECRET.encode(
            "utf-8"
        ),
        message.encode(
            "utf-8"
        ),
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(
        signature or "",
        expected_signature
    )


# =========================================================
# SECURITY HEADERS
# =========================================================

@app.after_request
def add_security_headers(response):

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

    # Updated for SaveVori
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
            "Instagram Downloader Backend is running."
        )
    })


# =========================================================
# RESOLVE ENDPOINT
# =========================================================

@app.route(
    "/api/resolve",
    methods=[
        "GET",
        "POST",
        "OPTIONS"
    ]
)
def resolve_instagram():

    if request.method == "OPTIONS":
        return Response(
            status=204
        )

    # -----------------------------------------------------
    # SECRET
    # -----------------------------------------------------

    if not is_authorized_server_request():

        logger.warning(
            "Unauthorized resolve request from %s",
            get_client_ip()
        )

        return unauthorized()


    # -----------------------------------------------------
    # API RATE LIMIT
    # -----------------------------------------------------

    if not rate_limit_ok(
        action="resolve",
        max_requests=12,
        window=60
    ):

        logger.warning(
            "Resolve rate limit exceeded for %s",
            get_client_ip()
        )

        return too_many_requests()


    # -----------------------------------------------------
    # GET URL
    # -----------------------------------------------------

    if request.method == "POST":

        data = (
            request.get_json(
                silent=True
            )
            or {}
        )

        url = (
            data.get("url")
            or request.form.get("url")
            or ""
        ).strip()

    else:

        url = (
            request.args.get("url")
            or ""
        ).strip()


    # -----------------------------------------------------
    # BASIC VALIDATION
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
            "message": (
                "URL is too long."
            )
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
    # CACHE
    # -----------------------------------------------------

    cached = get_cached_resolve(
        url
    )

    if cached:

        logger.info(
            "Serving cached resolve result."
        )

        return jsonify(
            cached
        ), 200


    # -----------------------------------------------------
    # CONCURRENCY CONTROL
    # -----------------------------------------------------

    acquired = resolve_semaphore.acquire(
        timeout=30
    )

    if not acquired:

        logger.warning(
            "Resolve concurrency limit reached."
        )

        return jsonify({
            "success": False,
            "message": (
                "The service is busy right now. "
                "Please try again in a moment."
            )
        }), 503


    try:

        logger.info(
            "Resolving Instagram URL."
        )


        # -------------------------------------------------
        # RESOLVE WITH LIMITED RETRIES
        # -------------------------------------------------

        try:

            info = resolve_with_retry(
                url
            )

        except Exception as error:

            logger.exception(
                "Instagram resolve failed."
            )

            if is_probable_rate_limit_error(
                error
            ):

                return jsonify({
                    "success": False,
                    "message": (
                        "Instagram is temporarily "
                        "limiting requests. "
                        "Please try again later."
                    )
                }), 429

            return jsonify({
                "success": False,
                "message": (
                    "This public media "
                    "could not be processed."
                )
            }), 400


        # -------------------------------------------------
        # MEDIA URL
        # -------------------------------------------------

        media_url = info.get(
            "url"
        )


        if not media_url:

            formats = (
                info.get("formats")
                or []
            )

            usable = [
                f
                for f in formats
                if (
                    f.get("url")
                    and (
                        f.get("vcodec") != "none"
                        or f.get("acodec") != "none"
                    )
                )
            ]

            usable.sort(
                key=lambda f: float(
                    f.get("tbr")
                    or 0
                ),
                reverse=True
            )

            if usable:
                media_url = usable[0].get(
                    "url"
                )


        if not media_url:

            logger.warning(
                "No downloadable media found."
            )

            return jsonify({
                "success": False,
                "message": (
                    "No downloadable media was found."
                )
            }), 404


        # -------------------------------------------------
        # BUILD RESPONSE
        # -------------------------------------------------

        result = {
            "success": True,
            "title": (
                info.get("title")
                or "Instagram Media"
            ),
            "thumbnail": info.get(
                "thumbnail"
            ),
            "duration": info.get(
                "duration"
            ),
            "media_url": media_url,
            "message": (
                "Instagram media resolved successfully."
            )
        }


        # -------------------------------------------------
        # CACHE
        # -------------------------------------------------

        set_cached_resolve(
            url,
            result
        )


        return jsonify(
            result
        ), 200


    finally:

        resolve_semaphore.release()


# =========================================================
# DOWNLOAD ENDPOINT
# =========================================================

@app.route(
    "/api/download",
    methods=["GET"]
)
def download_instagram():

    logger.info(
        "Download request received."
    )


    # -----------------------------------------------------
    # SECRET
    # -----------------------------------------------------

    if not is_authorized_server_request():

        logger.warning(
            "Unauthorized download request."
        )

        return unauthorized()


    # -----------------------------------------------------
    # DOWNLOAD RATE LIMIT
    # -----------------------------------------------------

    if not rate_limit_ok(
        action="download",
        max_requests=8,
        window=60
    ):

        logger.warning(
            "Download rate limit exceeded."
        )

        return too_many_requests()


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
    # BASIC VALIDATION
    # -----------------------------------------------------

    if not url:

        return jsonify({
            "success": False,
            "message": "Missing URL."
        }), 400


    if not is_valid_instagram_url(
        url
    ):

        logger.warning(
            "Invalid Instagram URL in download."
        )

        return jsonify({
            "success": False,
            "message": "Invalid Instagram URL."
        }), 400


    if file_format not in {
        "mp3",
        "mp4"
    }:

        return jsonify({
            "success": False,
            "message": "Unsupported format."
        }), 400


    # -----------------------------------------------------
    # SIGNATURE
    # -----------------------------------------------------

    if not verify_download_signature(
        url,
        file_format,
        expires,
        signature
    ):

        logger.warning(
            "Invalid or expired download signature."
        )

        return jsonify({
            "success": False,
            "message": (
                "Download link is invalid or expired."
            )
        }), 403


    # -----------------------------------------------------
    # FFMPEG CHECK
    # -----------------------------------------------------

    ffmpeg_path = shutil.which(
        "ffmpeg"
    )

    if not ffmpeg_path:

        logger.error(
            "FFmpeg was not found."
        )

        return jsonify({
            "success": False,
            "message": (
                "Media conversion is temporarily unavailable."
            )
        }), 500


    logger.info(
        "FFmpeg found at %s",
        ffmpeg_path
    )


    # -----------------------------------------------------
    # TEMP DIRECTORY
    # -----------------------------------------------------

    temp_dir = tempfile.mkdtemp(
        prefix="instagram_"
    )


    try:

        # =================================================
        # MP4
        # =================================================

        if file_format == "mp4":

            logger.info(
                "Starting MP4 download."
            )


            output_template = os.path.join(
                temp_dir,
                "instagram-video.%(ext)s"
            )


            ydl_opts = {

                "quiet": False,

                "no_warnings": False,

                "noplaylist": True,

                "format": "bv*+ba/b",

                "merge_output_format": "mp4",

                "outtmpl": output_template,

                "http_headers":
                    COMMON_HEADERS,

                "socket_timeout": 20,

                "max_filesize":
                    100 * 1024 * 1024,
            }


            try:

                with yt_dlp.YoutubeDL(
                    ydl_opts
                ) as ydl:

                    ydl.download([
                        url
                    ])

            except Exception as error:

                logger.exception(
                    "MP4 download failed."
                )

                if is_probable_rate_limit_error(
                    error
                ):

                    return jsonify({
                        "success": False,
                        "message": (
                            "Instagram is temporarily "
                            "limiting requests. "
                            "Please try again later."
                        )
                    }), 429

                return jsonify({
                    "success": False,
                    "message": (
                        "Video download failed. "
                        "Please try again later."
                    )
                }), 400


            mp4_files = glob.glob(
                os.path.join(
                    temp_dir,
                    "*.mp4"
                )
            )


            logger.info(
                "MP4 files created: %s",
                len(mp4_files)
            )


            if not mp4_files:

                raise RuntimeError(
                    "MP4 file was not created."
                )


            response = send_file(
                mp4_files[0],
                mimetype="video/mp4",
                as_attachment=True,
                download_name=(
                    "instagram-video.mp4"
                )
            )


        # =================================================
        # MP3
        # =================================================

        else:

            logger.info(
                "Starting MP3 processing."
            )


            # First resolve metadata.
            try:

                info = resolve_with_retry(
                    url
                )

            except Exception as error:

                logger.exception(
                    "MP3 resolve failed."
                )

                if is_probable_rate_limit_error(
                    error
                ):

                    return jsonify({
                        "success": False,
                        "message": (
                            "Instagram is temporarily "
                            "limiting requests. "
                            "Please try again later."
                        )
                    }), 429

                return jsonify({
                    "success": False,
                    "message": (
                        "Audio could not be processed."
                    )
                }), 400


            audio_format = (
                choose_audio_format(
                    info
                )
            )


            if not audio_format:

                raise RuntimeError(
                    "No audio-capable format was found."
                )


            format_id = (
                audio_format.get(
                    "format_id"
                )
            )


            audio_codec = (
                audio_format.get(
                    "acodec"
                )
            )


            logger.info(
                "Selected audio format: %s | codec: %s",
                format_id,
                audio_codec
            )


            if not format_id:

                raise RuntimeError(
                    "Audio format ID was unavailable."
                )


            source_template = os.path.join(
                temp_dir,
                "instagram-source.%(ext)s"
            )


            ydl_opts = {

                "quiet": False,

                "no_warnings": False,

                "noplaylist": True,

                "format": str(
                    format_id
                ),

                "outtmpl":
                    source_template,

                "http_headers":
                    COMMON_HEADERS,

                "socket_timeout": 20,

                "max_filesize":
                    100 * 1024 * 1024,
            }


            try:

                with yt_dlp.YoutubeDL(
                    ydl_opts
                ) as ydl:

                    ydl.download([
                        url
                    ])

            except Exception as error:

                logger.exception(
                    "Audio source download failed."
                )

                if is_probable_rate_limit_error(
                    error
                ):

                    return jsonify({
                        "success": False,
                        "message": (
                            "Instagram is temporarily "
                            "limiting requests. "
                            "Please try again later."
                        )
                    }), 429

                return jsonify({
                    "success": False,
                    "message": (
                        "Audio download failed. "
                        "Please try again later."
                    )
                }), 400


            source_files = [

                f
                for f in glob.glob(
                    os.path.join(
                        temp_dir,
                        "*"
                    )
                )

                if (
                    os.path.isfile(f)
                    and not f.endswith(".part")
                    and not f.endswith(".mp3")
                )

            ]


            logger.info(
                "Source files created: %s",
                len(source_files)
            )


            if not source_files:

                raise RuntimeError(
                    "Audio source was not downloaded."
                )


            source_file = (
                source_files[0]
            )


            mp3_file = os.path.join(
                temp_dir,
                "instagram-audio.mp3"
            )


            logger.info(
                "Running FFmpeg conversion."
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


            process = subprocess.run(

                ffmpeg_command,

                stdout=subprocess.PIPE,

                stderr=subprocess.PIPE,

                text=True,

                timeout=120
            )


            if process.returncode != 0:

                logger.error(
                    "FFmpeg failed:\n%s",
                    process.stderr[-4000:]
                )

                raise RuntimeError(
                    "FFmpeg audio conversion failed."
                )


            if not os.path.exists(
                mp3_file
            ):

                raise RuntimeError(
                    "MP3 file was not created."
                )


            logger.info(
                "MP3 created successfully."
            )


            response = send_file(
                mp3_file,
                mimetype="audio/mpeg",
                as_attachment=True,
                download_name=(
                    "instagram-audio.mp3"
                )
            )


        # -------------------------------------------------
        # CLEANUP
        # -------------------------------------------------

        @response.call_on_close
        def cleanup():

            shutil.rmtree(
                temp_dir,
                ignore_errors=True
            )


        return response


    # -----------------------------------------------------
    # TIMEOUT
    # -----------------------------------------------------

    except subprocess.TimeoutExpired:

        logger.exception(
            "Download process timed out."
        )

        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )

        return jsonify({
            "success": False,
            "message": (
                "Media processing took too long."
            )
        }), 500


    # -----------------------------------------------------
    # ALL OTHER ERRORS
    # -----------------------------------------------------

    except Exception as error:

        logger.exception(
            "Instagram download failed."
        )

        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )

        if is_probable_rate_limit_error(
            error
        ):

            return jsonify({
                "success": False,
                "message": (
                    "Instagram is temporarily "
                    "limiting requests. "
                    "Please try again later."
                )
            }), 429

        return jsonify({
            "success": False,
            "message": (
                "Download failed. "
                "The media may be unavailable "
                "or temporarily unsupported."
            )
        }), 400


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
