from models import db, User, Connection, VoiceEffect, FanCardDesign
from sqlalchemy import text, inspect

def init_db(app):
    with app.app_context():
        db.create_all()
        migrate_columns()
        sync_connections()
        seed_admin(app)
        seed_voice_effects()
        seed_fan_card_designs()

def sync_connections():
    """Older admin assignments set users.assigned_public_figure_id without a Connection row,
    so the public figure could not open that chat. Create the missing rows."""
    try:
        made = 0
        for u in User.query.filter(User.assigned_public_figure_id.isnot(None)).all():
            has = Connection.query.filter_by(user_id=u.id, public_figure_id=u.assigned_public_figure_id, active=True).first()
            if not has:
                db.session.add(Connection(user_id=u.id, public_figure_id=u.assigned_public_figure_id,
                                          assigned_by=None, active=True))
                made += 1
        if made:
            db.session.commit()
            print("[BOOT] Synced %d missing connection(s)" % made)
    except Exception as e:
        db.session.rollback()
        print("[BOOT] sync_connections skipped:", e)


def migrate_columns():
    """Add any new columns to existing tables (additive only; works on SQLite and Postgres)."""
    try:
        inspector = inspect(db.engine)
        existing_cols = {c["name"] for c in inspector.get_columns("users")}
        if "call_video_url" not in existing_cols:
            db.session.execute(text("ALTER TABLE users ADD COLUMN call_video_url VARCHAR(255) DEFAULT ''"))
            db.session.commit()
            print("[BOOT] Migrated: added users.call_video_url")

        for col in ("mouth_x", "mouth_y"):
            if col not in existing_cols:
                db.session.execute(text("ALTER TABLE users ADD COLUMN %s FLOAT" % col))
                db.session.commit()
                print("[BOOT] Migrated: added users.%s" % col)

        call_cols = {c["name"] for c in inspector.get_columns("calls")}
        if "status" not in call_cols:
            db.session.execute(text("ALTER TABLE calls ADD COLUMN status VARCHAR(20) DEFAULT 'ringing'"))
            db.session.commit()
            print("[BOOT] Migrated: added calls.status")
        if "ended_at" not in call_cols:
            db.session.execute(text("ALTER TABLE calls ADD COLUMN ended_at TIMESTAMP"))
            db.session.commit()
            print("[BOOT] Migrated: added calls.ended_at")

        fc_cols = {c["name"] for c in inspector.get_columns("fan_cards")}
        if "expiry_date" not in fc_cols:
            db.session.execute(text("ALTER TABLE fan_cards ADD COLUMN expiry_date VARCHAR(20) DEFAULT ''"))
            db.session.commit()
            print("[BOOT] Migrated: added fan_cards.expiry_date")
        if "special_code" not in fc_cols:
            db.session.execute(text("ALTER TABLE fan_cards ADD COLUMN special_code VARCHAR(40) DEFAULT ''"))
            db.session.commit()
            print("[BOOT] Migrated: added fan_cards.special_code")
    except Exception as e:
        db.session.rollback()
        print("[BOOT] Migration check skipped/failed:", e)

def seed_admin(app):
    email = app.config["ADMIN_EMAIL"].lower().strip()
    password = app.config["ADMIN_PASSWORD"]
    name = app.config["ADMIN_NAME"]
    admin = User.query.filter_by(email=email).first()
    if not admin:
        admin = User(full_name=name, email=email, phone="+0000000000",
                     role="admin", status="active", verified=True)
        admin.set_password(password)
        db.session.add(admin)
        db.session.commit()
        print("[BOOT] Admin created:", email)
    else:
        if not admin.check_password(password):
            admin.set_password(password)
        admin.role = "admin"
        admin.status = "active"
        db.session.commit()
        print("[BOOT] Admin synced:", email)

def seed_voice_effects():
    if VoiceEffect.query.count() > 0:
        return
    names = ["Deep", "Studio", "Radio", "Robot", "Bass", "Echo", "Character",
             "Cinematic", "Whisper", "Bright", "Warm", "Vintage", "Modern",
             "Podcast", "Hall", "Soft", "Bold", "Anime", "Narrator", "Chill"]
    for n in names:
        db.session.add(VoiceEffect(name=n, config="{}", active=True))
    db.session.commit()

def seed_fan_card_designs():
    if FanCardDesign.query.count() > 0:
        return
    designs = ["Classic", "Gold Luxury", "Royal", "Diamond", "Neon",
               "Purple Dream", "Black Edition", "VIP", "Platinum"]
    for d in designs:
        db.session.add(FanCardDesign(name=d, preview="", design_data="{}", active=True))
    db.session.commit()
