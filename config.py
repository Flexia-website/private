import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///private_chat.db")
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
