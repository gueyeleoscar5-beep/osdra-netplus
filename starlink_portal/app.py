import os
import secrets
from datetime import date, datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from twilio.rest import Client as TwilioClient
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATA_DIR = os.getenv("DATA_DIR", os.path.join(BASE_DIR, "data"))
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.getenv("DATABASE_PATH", os.path.join(DATA_DIR, "starlink_subscriptions.db"))
APP_NAME = os.getenv("APP_NAME", "osdra_NetPlus")
APP_TAGLINE = os.getenv("APP_TAGLINE", "Suivi des kits Starlink, paiements, expirations et alertes WhatsApp")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", secrets.token_hex(16))
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
scheduler = BackgroundScheduler()


class Client(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(150), nullable=False)
    phone = db.Column(db.String(30), nullable=False)
    email = db.Column(db.String(120), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    whatsapp_opt_in = db.Column(db.Boolean, default=True, nullable=False)
    portal_token = db.Column(db.String(64), unique=True, nullable=False, default=lambda: secrets.token_urlsafe(24))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    kits = db.relationship("Kit", backref="client", cascade="all, delete-orphan", lazy=True)


class Kit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("client.id"), nullable=False)
    label = db.Column(db.String(120), nullable=False)
    serial_number = db.Column(db.String(120), nullable=False, unique=True)
    monthly_amount = db.Column(db.Float, nullable=False, default=0)
    expiry_date = db.Column(db.Date, nullable=False)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    payments = db.relationship("Payment", backref="kit", cascade="all, delete-orphan", lazy=True)
    reminders = db.relationship("ReminderLog", backref="kit", cascade="all, delete-orphan", lazy=True)


class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    kit_id = db.Column(db.Integer, db.ForeignKey("kit.id"), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    paid_on = db.Column(db.Date, nullable=False)
    period_start = db.Column(db.Date, nullable=False)
    period_end = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="paid")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class ReminderLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    kit_id = db.Column(db.Integer, db.ForeignKey("kit.id"), nullable=False)
    reminder_type = db.Column(db.String(30), nullable=False)  # paid, unpaid
    overdue_day = db.Column(db.Integer, nullable=True)
    sent_on = db.Column(db.Date, nullable=False)
    message_sid = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    __table_args__ = (
        db.UniqueConstraint("kit_id", "reminder_type", "overdue_day", "sent_on", name="uq_reminder_once"),
    )


with app.app_context():
    db.create_all()


def app_tz() -> ZoneInfo:
    return ZoneInfo(os.getenv("APP_TIMEZONE", "Africa/Bangui"))


def today_local() -> date:
    return datetime.now(app_tz()).date()


def reminder_days() -> list[int]:
    raw = os.getenv("REMINDER_DAYS", "10,20,24,25,26,27,28,29,30")
    return sorted({int(part.strip()) for part in raw.split(",") if part.strip().isdigit()})


def validity_days() -> int:
    return int(os.getenv("PAYMENT_VALIDITY_DAYS", "30"))


def base_url() -> str:
    return os.getenv("BASE_URL", "http://127.0.0.1:5000").rstrip("/")


def require_login(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


@app.context_processor
def inject_globals():
    return {
        "base_url": base_url(),
        "today": today_local(),
        "app_name": APP_NAME,
        "app_tagline": APP_TAGLINE,
    }


def compute_status(kit: Kit) -> dict:
    today = today_local()
    if kit.expiry_date >= today:
        remaining = (kit.expiry_date - today).days
        return {
            "label": "Actif",
            "badge": "success",
            "days": remaining,
            "message": f"Actif jusqu’au {kit.expiry_date.strftime('%d/%m/%Y')}",
        }
    overdue = (today - kit.expiry_date).days
    return {
        "label": "En retard",
        "badge": "danger",
        "days": overdue,
        "message": f"Retard de {overdue} jour(s)",
    }


def portal_url_for_client(client: Client) -> str:
    return f"{base_url()}/suivi/{client.portal_token}"


def normalize_whatsapp(phone: str) -> str:
    cleaned = phone.strip().replace(" ", "")
    if cleaned.startswith("whatsapp:"):
        return cleaned
    if not cleaned.startswith("+"):
        raise ValueError("Le numéro doit être au format international, par ex. +23670000000")
    return f"whatsapp:{cleaned}"


def twilio_enabled() -> bool:
    return os.getenv("TWILIO_ENABLED", "false").lower() == "true"


def send_whatsapp(phone: str, body: str) -> str | None:
    if not twilio_enabled():
        app.logger.info("Twilio désactivé. Message simulé vers %s: %s", phone, body)
        return None

    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_WHATSAPP_FROM")

    if not account_sid or not auth_token or not from_number:
        raise RuntimeError("Variables Twilio manquantes.")

    client = TwilioClient(account_sid, auth_token)
    msg = client.messages.create(
        from_=normalize_whatsapp(from_number.replace("whatsapp:", "")) if not from_number.startswith("whatsapp:") else from_number,
        to=normalize_whatsapp(phone),
        body=body,
    )
    return msg.sid


def paid_message_for(kit: Kit) -> str:
    client = kit.client
    return (
        f"Cher client {client.full_name}, votre abonnement est actif jusqu'au "
        f"{kit.expiry_date.strftime('%d/%m/%Y')}.\n\n"
        f"Plateforme de suivi : {portal_url_for_client(client)}"
    )


def unpaid_message_for(kit: Kit, overdue_day: int) -> str:
    client = kit.client
    if overdue_day >= 25:
        return (
            f"Cher client {client.full_name}, nous vous signalons le non paiement de votre abonnement. "
            f"Votre kit sera bloqué et nous nous désengageons de toutes les conséquences.\n\n"
            f"Plateforme de suivi : {portal_url_for_client(client)}"
        )
    return (
        f"Cher client {client.full_name}, nous vous rappelons que votre abonnement est impayé. "
        f"Merci de régulariser avant le blocage du kit.\n\n"
        f"Échéance dépassée depuis {overdue_day} jour(s).\n"
        f"Plateforme de suivi : {portal_url_for_client(client)}"
    )


def send_paid_notification(kit: Kit):
    client = kit.client
    if not client.whatsapp_opt_in:
        return None
    sid = send_whatsapp(client.phone, paid_message_for(kit))
    log = ReminderLog(kit_id=kit.id, reminder_type="paid", overdue_day=None, sent_on=today_local(), message_sid=sid)
    db.session.add(log)
    db.session.commit()
    return sid



def maybe_send_unpaid_notifications():
    today = today_local()
    days_to_alert = reminder_days()
    overdue_kits = Kit.query.all()

    for kit in overdue_kits:
        status = compute_status(kit)
        if status["label"] != "En retard":
            continue
        overdue_day = status["days"]
        if overdue_day not in days_to_alert:
            continue
        client = kit.client
        if not client.whatsapp_opt_in:
            continue
        exists = ReminderLog.query.filter_by(
            kit_id=kit.id,
            reminder_type="unpaid",
            overdue_day=overdue_day,
            sent_on=today,
        ).first()
        if exists:
            continue
        sid = send_whatsapp(client.phone, unpaid_message_for(kit, overdue_day))
        db.session.add(ReminderLog(
            kit_id=kit.id,
            reminder_type="unpaid",
            overdue_day=overdue_day,
            sent_on=today,
            message_sid=sid,
        ))
    db.session.commit()


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == os.getenv("ADMIN_USERNAME", "admin") and password == os.getenv("ADMIN_PASSWORD", "admin123"):
            session["admin_logged_in"] = True
            return redirect(url_for("dashboard"))
        flash("Identifiants invalides.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@require_login
def dashboard():
    clients = Client.query.order_by(Client.created_at.desc()).all()
    data = []
    for client in clients:
        kits = []
        for kit in client.kits:
            kits.append({
                "model": kit,
                "status": compute_status(kit),
            })
        data.append({
            "client": client,
            "portal_url": portal_url_for_client(client),
            "kits": kits,
        })
    return render_template("dashboard.html", rows=data, reminder_days=reminder_days())


@app.route("/clients/new", methods=["GET", "POST"])
@require_login
def create_client():
    if request.method == "POST":
        client = Client(
            full_name=request.form["full_name"].strip(),
            phone=request.form["phone"].strip(),
            email=request.form.get("email", "").strip() or None,
            notes=request.form.get("notes", "").strip() or None,
            whatsapp_opt_in=bool(request.form.get("whatsapp_opt_in")),
        )
        db.session.add(client)
        db.session.commit()
        flash("Client ajouté.", "success")
        return redirect(url_for("dashboard"))
    return render_template("client_form.html", client=None)


@app.route("/clients/<int:client_id>/edit", methods=["GET", "POST"])
@require_login
def edit_client(client_id: int):
    client = Client.query.get_or_404(client_id)
    if request.method == "POST":
        client.full_name = request.form["full_name"].strip()
        client.phone = request.form["phone"].strip()
        client.email = request.form.get("email", "").strip() or None
        client.notes = request.form.get("notes", "").strip() or None
        client.whatsapp_opt_in = bool(request.form.get("whatsapp_opt_in"))
        db.session.commit()
        flash("Client mis à jour.", "success")
        return redirect(url_for("dashboard"))
    return render_template("client_form.html", client=client)


@app.route("/clients/<int:client_id>/delete", methods=["POST"])
@require_login
def delete_client(client_id: int):
    client = Client.query.get_or_404(client_id)
    db.session.delete(client)
    db.session.commit()
    flash("Client supprimé.", "success")
    return redirect(url_for("dashboard"))


@app.route("/kits/new/<int:client_id>", methods=["GET", "POST"])
@require_login
def create_kit(client_id: int):
    client = Client.query.get_or_404(client_id)
    if request.method == "POST":
        kit = Kit(
            client_id=client.id,
            label=request.form["label"].strip(),
            serial_number=request.form["serial_number"].strip(),
            monthly_amount=float(request.form["monthly_amount"]),
            expiry_date=datetime.strptime(request.form["expiry_date"], "%Y-%m-%d").date(),
            notes=request.form.get("notes", "").strip() or None,
        )
        db.session.add(kit)
        db.session.commit()
        flash("Kit ajouté.", "success")
        return redirect(url_for("dashboard"))
    return render_template("kit_form.html", client=client, kit=None)


@app.route("/kits/<int:kit_id>/edit", methods=["GET", "POST"])
@require_login
def edit_kit(kit_id: int):
    kit = Kit.query.get_or_404(kit_id)
    if request.method == "POST":
        kit.label = request.form["label"].strip()
        kit.serial_number = request.form["serial_number"].strip()
        kit.monthly_amount = float(request.form["monthly_amount"])
        kit.expiry_date = datetime.strptime(request.form["expiry_date"], "%Y-%m-%d").date()
        kit.notes = request.form.get("notes", "").strip() or None
        db.session.commit()
        flash("Kit mis à jour.", "success")
        return redirect(url_for("dashboard"))
    return render_template("kit_form.html", client=kit.client, kit=kit)


@app.route("/kits/<int:kit_id>/delete", methods=["POST"])
@require_login
def delete_kit(kit_id: int):
    kit = Kit.query.get_or_404(kit_id)
    db.session.delete(kit)
    db.session.commit()
    flash("Kit supprimé.", "success")
    return redirect(url_for("dashboard"))


@app.route("/kits/<int:kit_id>/mark-paid", methods=["POST"])
@require_login
def mark_paid(kit_id: int):
    kit = Kit.query.get_or_404(kit_id)
    today = today_local()
    current_expiry = kit.expiry_date
    if current_expiry >= today:
        period_start = current_expiry + timedelta(days=1)
    else:
        period_start = today
    period_end = period_start + timedelta(days=validity_days() - 1)

    payment = Payment(
        kit_id=kit.id,
        amount=float(request.form.get("amount") or kit.monthly_amount),
        paid_on=today,
        period_start=period_start,
        period_end=period_end,
        status="paid",
    )
    kit.expiry_date = period_end
    db.session.add(payment)
    db.session.commit()

    try:
        send_paid_notification(kit)
        flash("Paiement enregistré et notification WhatsApp envoyée (ou simulée si Twilio est désactivé).", "success")
    except Exception as exc:
        flash(f"Paiement enregistré, mais échec de l’envoi WhatsApp: {exc}", "warning")
    return redirect(url_for("dashboard"))


@app.route("/kits/<int:kit_id>/mark-unpaid-now", methods=["POST"])
@require_login
def mark_unpaid_now(kit_id: int):
    kit = Kit.query.get_or_404(kit_id)
    status = compute_status(kit)
    if status["label"] != "En retard":
        flash("Ce kit n’est pas encore en retard.", "warning")
        return redirect(url_for("dashboard"))

    client = kit.client
    overdue_day = status["days"]
    existing = ReminderLog.query.filter_by(
        kit_id=kit.id,
        reminder_type="unpaid",
        overdue_day=overdue_day,
        sent_on=today_local(),
    ).first()
    if existing:
        flash("Un rappel pour ce jour de retard a déjà été envoyé aujourd’hui.", "warning")
        return redirect(url_for("dashboard"))

    try:
        sid = send_whatsapp(client.phone, unpaid_message_for(kit, overdue_day))
        db.session.add(ReminderLog(
            kit_id=kit.id,
            reminder_type="unpaid",
            overdue_day=overdue_day,
            sent_on=today_local(),
            message_sid=sid,
        ))
        db.session.commit()
        flash("Rappel non payé envoyé (ou simulé).", "success")
    except Exception as exc:
        flash(f"Échec de l’envoi: {exc}", "danger")
    return redirect(url_for("dashboard"))


@app.route("/run-reminders", methods=["POST"])
@require_login
def run_reminders():
    try:
        maybe_send_unpaid_notifications()
        flash("Vérification des rappels terminée.", "success")
    except Exception as exc:
        flash(f"Erreur de rappel: {exc}", "danger")
    return redirect(url_for("dashboard"))


@app.route("/suivi/<token>")
def public_status(token: str):
    client = Client.query.filter_by(portal_token=token).first_or_404()
    kits = []
    for kit in client.kits:
        payments = Payment.query.filter_by(kit_id=kit.id).order_by(Payment.paid_on.desc()).limit(6).all()
        kits.append({
            "model": kit,
            "status": compute_status(kit),
            "payments": payments,
        })
    return render_template("public_status.html", client=client, kits=kits)



def start_scheduler():
    if scheduler.running:
        return
    trigger = CronTrigger(hour=int(os.getenv("REMINDER_HOUR", "8")), minute=int(os.getenv("REMINDER_MINUTE", "0")), timezone=app_tz())
    scheduler.add_job(maybe_send_unpaid_notifications, trigger=trigger, id="daily-reminders", replace_existing=True)
    scheduler.start()


DEBUG_MODE = os.getenv("DEBUG", "false").lower() == "true"

if not DEBUG_MODE or os.getenv("WERKZEUG_RUN_MAIN") == "true":
    start_scheduler()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=DEBUG_MODE)
