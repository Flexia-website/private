from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(160), unique=True, nullable=False, index=True)
    phone = db.Column(db.String(40), unique=True, nullable=False, index=True)
    phone_verified = db.Column(db.Boolean, default=False)
    phone_verification_code = db.Column(db.String(6), default="")
    country_code = db.Column(db.String(2), default="")  # e.g., "US", "GB", "IN"
    country_name = db.Column(db.String(80), default="")  # e.g., "United States"
    device_info = db.Column(db.Text, default="")  # JSON: {brand, model, os, os_version}
    ip_address = db.Column(db.String(45), default="")  # IPv4 or IPv6
    last_login = db.Column(db.DateTime, nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)
    profile_photo = db.Column(db.String(255), default="")
    role = db.Column(db.String(20), default="user")
    status = db.Column(db.String(20), default="active")
    assigned_public_figure_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    bio = db.Column(db.Text, default="")
    username = db.Column(db.String(80), default="")
    verified = db.Column(db.Boolean, default=False)
    followers_count = db.Column(db.BigInteger, default=0)
    likes_count = db.Column(db.BigInteger, default=0)
    assigned_fan_card_design_id = db.Column(db.Integer, db.ForeignKey("fan_card_designs.id"), nullable=True)
    call_video_url = db.Column(db.String(255), default="")  # premade looping video for public figures to use when answering calls
    mouth_x = db.Column(db.Float, nullable=True)  # relative X position of mouth in call video (0–1)
    mouth_y = db.Column(db.Float, nullable=True)  # relative Y position of mouth in call video (0–1)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

    @property
    def is_admin(self):
        return self.role == "admin"

    @property
    def is_public_figure(self):
        return self.role == "public_figure"

    @property
    def is_user(self):
        return self.role == "user"

class Connection(db.Model):
    __tablename__ = "connections"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    public_figure_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    assigned_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Message(db.Model):
    __tablename__ = "messages"
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    receiver_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    message = db.Column(db.Text, default="")
    message_type = db.Column(db.String(20), default="text")
    media_url = db.Column(db.String(255), default="")
    read_status = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Call(db.Model):
    __tablename__ = "calls"
    id = db.Column(db.Integer, primary_key=True)
    caller_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    receiver_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    call_type = db.Column(db.String(10), default="voice")
    status = db.Column(db.String(20), default="ringing")  # ringing, completed, declined, missed, failed
    duration = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    ended_at = db.Column(db.DateTime, nullable=True)

class FanCardDesign(db.Model):
    __tablename__ = "fan_card_designs"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    preview = db.Column(db.String(255), default="")
    design_data = db.Column(db.Text, default="")
    assigned_public_figure_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    active = db.Column(db.Boolean, default=True)

class FanCard(db.Model):
    __tablename__ = "fan_cards"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    public_figure_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    design_id = db.Column(db.Integer, db.ForeignKey("fan_card_designs.id"), nullable=True)
    name = db.Column(db.String(120))
    photo = db.Column(db.String(255), default="")
    expiry_date = db.Column(db.String(20), default="")
    special_code = db.Column(db.String(40), default="")
    generated_card = db.Column(db.String(255), default="")
    status = db.Column(db.String(20), default="pending")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class VoiceEffect(db.Model):
    __tablename__ = "voice_effects"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    config = db.Column(db.Text, default="")
    active = db.Column(db.Boolean, default=True)

class VideoLibrary(db.Model):
    __tablename__ = "video_library"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(120), nullable=False)
    video_url = db.Column(db.String(255), nullable=False)
    thumbnail = db.Column(db.String(255), default="")
    active = db.Column(db.Boolean, default=True)

class Notification(db.Model):
    __tablename__ = "notifications"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    title = db.Column(db.String(120))
    message = db.Column(db.Text)
    read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class LipSyncVideo(db.Model):
    __tablename__ = "lip_sync_videos"
    id = db.Column(db.Integer, primary_key=True)
    public_figure_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    title = db.Column(db.String(120), nullable=False)
    video_url = db.Column(db.String(255), nullable=False)
    thumbnail = db.Column(db.String(255), default="")
    status = db.Column(db.String(20), default="active")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class LipSyncSession(db.Model):
    __tablename__ = "lip_sync_sessions"
    id = db.Column(db.Integer, primary_key=True)
    video_id = db.Column(db.Integer, db.ForeignKey("lip_sync_videos.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    session_status = db.Column(db.String(20), default="active")
    recording_data = db.Column(db.Text, default="")
    output_video_url = db.Column(db.String(255), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
