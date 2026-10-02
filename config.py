import os
from dotenv import load_dotenv

load_dotenv()

def _database_url():
    """DATABASE_URL, normalised for SQLAlchemy.
    Hosts like Neon, Render and Heroku hand out 'postgres://...' or 'postgresql://...'.
    SQLAlchemy rejects the first, and newer versions (2.1+) would pick the psycopg3
    driver for the second. We ship psycopg2, so pin that driver explicitly."""
    url = os.getenv("DATABASE_URL", "").strip() or "sqlite:///private_chat.db"
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg2://" + url[len(prefix):]
    return url


_DB_URL = _database_url()


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
    SQLALCHEMY_DATABASE_URI = _DB_URL
    # Serverless Postgres (e.g. Neon) closes idle connections; check before reuse.
    SQLALCHEMY_ENGINE_OPTIONS = (
        {} if _DB_URL.startswith("sqlite")
        else {"pool_pre_ping": True, "pool_recycle": 300}
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", "static/uploads")
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_CONTENT_LENGTH", 52428800))
    ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@privatechat.app")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "Admin@12345")
    ADMIN_NAME = os.getenv("ADMIN_NAME", "System Administrator")
    ALLOWED_IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp"}
    ALLOWED_VIDEO_EXT = {"mp4", "webm", "mov", "avi", "mkv"}
    ALLOWED_AUDIO_EXT = {"mp3", "wav", "m4a", "aac", "ogg", "flac", "webm", "opus"}

    # Cloudinary media storage. Set CLOUDINARY_URL (cloudinary://KEY:SECRET@CLOUD_NAME)
    # or the three separate values. If none are set, uploads stay on local disk.
    CLOUDINARY_URL = os.getenv("CLOUDINARY_URL", "")
    CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME", "")
    CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY", "")
    CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET", "")
    CLOUDINARY_FOLDER = os.getenv("CLOUDINARY_FOLDER", "private-chat")

    # Calls
    CALL_RING_TIMEOUT = int(os.getenv("CALL_RING_TIMEOUT", 45))  # seconds before an unanswered call is "missed"
    ICE_SERVERS_JSON = os.getenv("ICE_SERVERS_JSON", "")         # optional TURN relay, see app._ice_servers

