import io
import os
import re
import sys
import secrets
import base64
import time
import queue
import threading
import calendar
import urllib.parse
from datetime import datetime, timedelta, date as date_cls
from xml.sax.saxutils import escape as xml_escape
from typing import Optional, List

from fastapi import FastAPI, Depends, HTTPException, Request, Form, status, Query, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, Response as FastAPIResponse
from fastapi.templating import Jinja2Templates
from jinja2 import TemplateNotFound
from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, Session

from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

import requests

# --- CONFIGURAZIONE WHATSAPP (WAHA) ---
# I valori si leggono dalle variabili d'ambiente, così niente segreti nel codice.
#   WAHA_API_KEY      chiave API di WAHA (dal file ~/waha/.env)
#   WHATSAPP_CHAT_ID  ID del gruppo, finisce con @g.us
#   WAHA_URL          (opzionale) default http://localhost:3000
#   WAHA_SESSION      (opzionale) default "default"
WAHA_URL = os.getenv("WAHA_URL", "http://localhost:3000").rstrip("/")
WAHA_API_KEY = os.getenv("WAHA_API_KEY", "")
WAHA_SESSION = os.getenv("WAHA_SESSION", "default")
WHATSAPP_CHAT_ID = os.getenv("WHATSAPP_CHAT_ID", "")

if not WAHA_API_KEY or not WHATSAPP_CHAT_ID:
    print("⚠️ WAHA_API_KEY o WHATSAPP_CHAT_ID non impostati: i messaggi WhatsApp NON verranno inviati.")

# Coda con un solo worker: i messaggi partono uno alla volta, nell'ordine in cui arrivano.
_wa_queue: "queue.Queue" = queue.Queue()
PAUSA_TRA_MESSAGGI = 2  # secondi tra un invio e il successivo
TENTATIVI = 3

def _wa_worker():
    while True:
        item = _wa_queue.get()
        try:
            if not WAHA_API_KEY or not WHATSAPP_CHAT_ID:
                print("❌ WhatsApp non configurato, messaggio scartato.")
                continue
            # item = testo semplice (str) oppure dict {"endpoint": ..., "json": ...} per i file
            if isinstance(item, str):
                endpoint = "sendText"
                payload = {"session": WAHA_SESSION, "chatId": WHATSAPP_CHAT_ID, "text": item}
            else:
                endpoint, payload = item["endpoint"], item["json"]
            for tentativo in range(1, TENTATIVI + 1):
                try:
                    r = requests.post(
                        f"{WAHA_URL}/api/{endpoint}",
                        json=payload,
                        headers={"X-Api-Key": WAHA_API_KEY},
                        timeout=60,
                    )
                    if not r.ok:
                        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
                    print(f"✅ WhatsApp: {endpoint} inviato al gruppo")
                    break
                except Exception as e:
                    print(f"❌ Invio WhatsApp fallito (tentativo {tentativo}/{TENTATIVI}): {e}")
                    if tentativo < TENTATIVI:
                        time.sleep(5)
        finally:
            time.sleep(PAUSA_TRA_MESSAGGI)
            _wa_queue.task_done()

threading.Thread(target=_wa_worker, daemon=True).start()

def invia_whatsapp_gruppo_asincrono(messaggio: str):
    """Mette il messaggio in coda; l'invio vero avviene nel worker."""
    _wa_queue.put(messaggio)

def costruisci_messaggio_whatsapp(azione, title, tipologia, dt_start, dt_end, operai_str, username, warning_msg=None):
    # In WhatsApp il grassetto si fa con UN asterisco (*testo*), non con due.
    operai_display = operai_str if operai_str else "Nessuno"
    testo = (
        f"📋 *{azione}*\n"
        f"🏷️ *Titolo:* {title}\n"
        f"📂 *Tipologia:* {tipologia}\n"
        f"📅 *Inizio:* {dt_start.strftime('%d/%m/%Y %H:%M')}\n"
    )
    if dt_end and dt_end != dt_start:
        testo += f"🏁 *Fine:* {dt_end.strftime('%d/%m/%Y %H:%M')}\n"
    testo += (
        f"👷 *Operai:* {operai_display}\n"
        f"👤 *Inserito da:* {username.upper()}"
    )
    if warning_msg:
        testo += f"\n\n⚠️ *ATTENZIONE SOVRAPPOSIZIONE:*\n" + warning_msg.replace(" | ", "\n")
    return testo

# --- CONFIGURAZIONE DATABASE ---
SQLALCHEMY_DATABASE_URL = "sqlite:///./falegnameria.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# --- MODELLI DATABASE ---
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    password = Column(String)
    role = Column(String, default="editor") # admin, editor
    color = Column(String, default="#3b82f6") # Colore hex identificativo utente
    events = relationship("Event", back_populates="owner", cascade="all, delete-orphan")
    notifications = relationship("Notification", back_populates="recipient", cascade="all, delete-orphan")

class Notification(Base):
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    message = Column(String)
    is_read = Column(Integer, default=0) # 0 = da leggere, 1 = letta
    created_at = Column(DateTime, default=datetime.now)
    recipient = relationship("User", back_populates="notifications")

class Operaio(Base):
    __tablename__ = "operai"
    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, unique=True, index=True)

class Tipologia(Base):
    __tablename__ = "tipologie"
    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, unique=True, index=True)

class Event(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, index=True)
    tipologia = Column(String)
    description = Column(String, nullable=True)
    operai = Column(String, nullable=True) # Operai separati da virgola
    start_time = Column(DateTime)
    end_time = Column(DateTime, nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    owner = relationship("User", back_populates="events")

Base.metadata.create_all(bind=engine)

# --- CREAZIONE AUTOMATICA ADMIN HARDCODED SE IL DB È NUOvo ---
def init_db():
    db = SessionLocal()
    try:
        admin_user = db.query(User).filter(User.username == "admin").first()
        if not admin_user:
            print("⚙️ Database vuoto: creazione utente admin predefinito...")
            default_admin = User(
                username="admin",
                password="admin123",
                role="admin",
                color="#13293d"
            )
            db.add(default_admin)
            db.commit()
    finally:
        db.close()

init_db()

app = FastAPI()
templates = Jinja2Templates(directory="templates")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- AUTENTICAZIONE COOKIE ---
def get_current_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    username = request.cookies.get("session_user")
    if not username:
        return None
    return db.query(User).filter(User.username == username).first()

# --- LOGICA CONTROLLO E GESTIONE AUTOMATICA SOVRAPPOSIZIONI E NOTIFICHE ---
def check_operaio_occupato_automatico(
    db: Session, 
    operai_selezionati: list[str], 
    dt_start: datetime, 
    dt_end: datetime, 
    is_nuova_assenza: bool, 
    current_user: User, 
    exclude_event_id: int = None
):
    all_events = db.query(Event).all()
    avvisi = []
    note_ritardi = []

    for ev in all_events:
        if exclude_event_id and ev.id == exclude_event_id:
            continue
        
        if ev.operai:
            operai_coinvolti = [op.strip() for op in ev.operai.split(",") if op.strip()]
            intersezione = set(operai_selezionati).intersection(set(operai_coinvolti))
            
            if intersezione:
                ev_end = ev.end_time if ev.end_time else ev.start_time
                
                if max(dt_start, ev.start_time) < min(dt_end, ev_end):
                    tip_exist_lower = ev.tipologia.lower() if ev.tipologia else ""
                    ev_is_assenza = any(kw in tip_exist_lower for kw in ["assenza", "ferie", "malattia", "permesso"])
                    
                    orario_liberazione = ev_end.strftime('%H:%M')
                    data_liberazione = ev_end.strftime('%d/%m/%Y')
                    op_str = ', '.join(intersezione)

                    msg_ritardo = f"⚠️ {op_str} arriverà più tardi (disponibile dalle {orario_liberazione} del {data_liberazione} dopo '{ev.title}')"
                    note_ritardi.append(msg_ritardo)
                    avvisi.append(msg_ritardo)

                    notif_creatore = f"📢 Sovrapposizione gestita: {msg_ritardo}"
                    db.add(Notification(user_id=current_user.id, message=notif_creatore))

                    if ev.user_id and ev.user_id != current_user.id:
                        notif_altro = f"📢 ATTENZIONE: {current_user.username.upper()} ha programmato un intervento con {op_str} in sovrapposizione col tuo cantiere '{ev.title}'."
                        db.add(Notification(user_id=ev.user_id, message=notif_altro))
                    
                    db.commit()

    return avvisi, note_ritardi

# --- ROTTE AUTENTICAZIONE ---
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    error = request.query_params.get("error")
    return templates.TemplateResponse("login.html", {"request": request, "error": error})

@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == username, User.password == password).first()
    if not user:
        return RedirectResponse(url="/login?error=1", status_code=status.HTTP_303_SEE_OTHER)
    
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(key="session_user", value=user.username)
    return response

@app.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(key="session_user")
    return response

# --- ROTTA PUBBLICA ---
@app.get("/public", response_class=HTMLResponse)
def public_view(request: Request, db: Session = Depends(get_db)):
    events = db.query(Event).order_by(Event.start_time.asc()).all()
    return templates.TemplateResponse("public.html", {
        "request": request,
        "events": events
    })

# --- ROTTA PRINCIPALE DASHBOARD ---
@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    
    events = db.query(Event).order_by(Event.start_time.asc()).all()
    operai = db.query(Operaio).all()
    tipologie = db.query(Tipologia).all()
    
    unread_notifications = db.query(Notification).filter(
        Notification.user_id == current_user.id,
        Notification.is_read == 0
    ).order_by(Notification.created_at.desc()).all()
    
    error_msg = request.query_params.get("error_msg")
    warning_msg = request.query_params.get("warning_msg")
    success_msg = request.query_params.get("success_msg")
    
    return templates.TemplateResponse("index.html", {
        "request": request,
        "current_user": current_user,
        "events": events,
        "operai": operai,
        "tipologie": tipologie,
        "error_msg": error_msg,
        "warning_msg": warning_msg,
        "success_msg": success_msg,
        "notifications": unread_notifications
    })

# --- GESTIONE PROFILO ---
@app.post("/profile/password")
def change_password(
    request: Request,
    old_password: str = Form(...),
    new_password: str = Form(...),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    
    if current_user.password != old_password:
        return RedirectResponse(url="/?error_msg=La+vecchia+password+non+è+corretta", status_code=status.HTTP_303_SEE_OTHER)
    
    if len(new_password.strip()) < 3:
        return RedirectResponse(url="/?error_msg=La+nuova+password+è+troppo+corta", status_code=status.HTTP_303_SEE_OTHER)
        
    current_user.password = new_password.strip()
    db.commit()
    return RedirectResponse(url="/?success_msg=Password+aggiornata+con+successo", status_code=status.HTTP_303_SEE_OTHER)

# --- PANNELLO ADMIN: UTENTI & OPERAI ---
@app.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user or (current_user.role != 'admin' and current_user.username != 'admin'):
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    
    users = db.query(User).all()
    operai = db.query(Operaio).all()
    error_msg = request.query_params.get("error_msg")
    success_msg = request.query_params.get("success_msg")
    
    return templates.TemplateResponse("admin_users.html", {
        "request": request,
        "current_user": current_user,
        "users": users,
        "operai": operai,
        "error_msg": error_msg,
        "success_msg": success_msg
    })

@app.post("/admin/users/add")
def admin_add_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
    color: str = Form("#3b82f6"),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(request, db)
    if not current_user or (current_user.role != 'admin' and current_user.username != 'admin'):
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    
    existing = db.query(User).filter(User.username == username.strip()).first()
    if existing:
        return RedirectResponse(url="/admin/users?error_msg=Nome+utente+già+esistente", status_code=status.HTTP_303_SEE_OTHER)
        
    new_user = User(
        username=username.strip(),
        password=password.strip(),
        role=role.strip(),
        color=color.strip()
    )
    db.add(new_user)
    db.commit()
    return RedirectResponse(url="/admin/users?success_msg=Utente+creato+con+successo", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/users/edit/{user_id}")
def admin_edit_user(
    request: Request,
    user_id: int,
    username: str = Form(...),
    password: str = Form(None),
    role: str = Form(...),
    color: str = Form("#3b82f6"),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(request, db)
    if not current_user or (current_user.role != 'admin' and current_user.username != 'admin'):
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        
    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        return RedirectResponse(url="/admin/users?error_msg=Utente+non+trovato", status_code=status.HTTP_303_SEE_OTHER)
        
    target_user.username = username.strip()
    target_user.role = role.strip()
    target_user.color = color.strip()
    if password and password.strip():
        target_user.password = password.strip()
        
    db.commit()
    return RedirectResponse(url="/admin/users?success_msg=Utente+aggiornato+con+successo", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/users/delete/{user_id}")
def admin_delete_user(request: Request, user_id: int, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user or (current_user.role != 'admin' and current_user.username != 'admin'):
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        
    target_user = db.query(User).filter(User.id == user_id).first()
    if target_user:
        if target_user.username == 'admin':
            return RedirectResponse(url="/admin/users?error_msg=Impossibile+eliminare+l'admin+principale", status_code=status.HTTP_303_SEE_OTHER)
        db.delete(target_user)
        db.commit()
        
    return RedirectResponse(url="/admin/users?success_msg=Utente+eliminato", status_code=status.HTTP_303_SEE_OTHER)

# --- GESTIONE OPERAI ---
@app.post("/operai/add")
def add_operaio(request: Request, nome: str = Form(...), redirect_to: str = Form("/"), db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user or (current_user.role != 'admin' and current_user.username != 'admin'):
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    
    if nome.strip():
        existing = db.query(Operaio).filter(Operaio.nome == nome.strip()).first()
        if not existing:
            db.add(Operaio(nome=nome.strip()))
            db.commit()
            
    return RedirectResponse(url=redirect_to, status_code=status.HTTP_303_SEE_OTHER)

@app.post("/operai/delete/{operaio_id}")
def delete_operaio(request: Request, operaio_id: int, redirect_to: str = Form("/"), db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user or (current_user.role != 'admin' and current_user.username != 'admin'):
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    
    operaio = db.query(Operaio).filter(Operaio.id == operaio_id).first()
    if operaio:
        db.delete(operaio)
        db.commit()
        
    return RedirectResponse(url=redirect_to, status_code=status.HTTP_303_SEE_OTHER)

# --- NOTIFICHE ---
@app.post("/notifications/read/{notif_id}")
def mark_notification_read(request: Request, notif_id: int, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if current_user:
        notif = db.query(Notification).filter(Notification.id == notif_id, Notification.user_id == current_user.id).first()
        if notif:
            notif.is_read = 1
            db.commit()
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

# --- GESTIONE EVENTI ---
@app.post("/events/add")
def add_event(
    request: Request,
    background_tasks: BackgroundTasks,
    title: str = Form(...),
    tipologia: str = Form(...),
    description: str = Form(None),
    operai: List[str] = Form([]),
    start_time: str = Form(...),
    end_time: str = Form(None),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    if current_user.username == 'lena' and "assenza" not in tipologia.lower() and "ferie" not in tipologia.lower() and "malattia" not in tipologia.lower():
        tipologia = "Assenza / Ferie"

    dt_start = datetime.strptime(start_time, "%Y-%m-%dT%H:%M")
    dt_end = datetime.strptime(end_time, "%Y-%m-%dT%H:%M") if end_time else dt_start

    is_assenza = "assenza" in tipologia.lower() or "ferie" in tipologia.lower() or "malattia" in tipologia.lower()
    warning_msg = None

    if operai:
        avvisi, note_ritardi = check_operaio_occupato_automatico(
            db, operai, dt_start, dt_end, 
            is_nuova_assenza=is_assenza, 
            current_user=current_user
        )
        
        if avvisi:
            warning_msg = " | ".join(avvisi)

        if note_ritardi:
            ritardi_str = "\n".join(note_ritardi)
            description = f"{description or ''}\n\n--- NOTE DISPONIBILITÀ AUTOMATICHE ---\n{ritardi_str}".strip()

    operai_str = ", ".join(operai)
    db_event = Event(
        title=title,
        tipologia=tipologia,
        description=description or "",
        operai=operai_str,
        start_time=dt_start,
        end_time=dt_end,
        user_id=current_user.id
    )
    db.add(db_event)
    db.commit()

    # 🚀 Messaggio WhatsApp per ogni nuovo inserimento (con avviso se c'è sovrapposizione)
    testo_messaggio = costruisci_messaggio_whatsapp(
        "Nuovo Intervento / Cantiere Aggiunto", title, tipologia,
        dt_start, dt_end, operai_str, current_user.username, warning_msg
    )

    # Accoda l'invio WhatsApp in background per qualsiasi nuovo inserimento
    background_tasks.add_task(invia_whatsapp_gruppo_asincrono, testo_messaggio)

    redirect_url = "/"
    if warning_msg:
        redirect_url += f"?warning_msg={urllib.parse.quote(warning_msg)}"

    return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)

@app.post("/events/edit/{event_id}")
def edit_event(
    request: Request,
    event_id: int,
    background_tasks: BackgroundTasks,
    title: str = Form(...),
    tipologia: str = Form(...),
    description: str = Form(None),
    operai: List[str] = Form([]),
    start_time: str = Form(...),
    end_time: str = Form(None),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Evento non trovato")

    if current_user.role != 'admin' and current_user.username != 'admin' and event.user_id != current_user.id:
        return RedirectResponse(url="/?error_msg=Non+autorizzato+alla+modifica", status_code=status.HTTP_303_SEE_OTHER)

    if current_user.username == 'lena':
        tipologia = "Assenza / Ferie"

    dt_start = datetime.strptime(start_time, "%Y-%m-%dT%H:%M")
    dt_end = datetime.strptime(end_time, "%Y-%m-%dT%H:%M") if end_time else dt_start

    is_assenza = "assenza" in tipologia.lower() or "ferie" in tipologia.lower() or "malattia" in tipologia.lower()
    warning_msg = None

    if operai:
        avvisi, note_ritardi = check_operaio_occupato_automatico(
            db, operai, dt_start, dt_end, 
            is_nuova_assenza=is_assenza, 
            current_user=current_user, 
            exclude_event_id=event_id
        )
        
        if avvisi:
            warning_msg = " | ".join(avvisi)

        if note_ritardi:
            ritardi_str = "\n".join(note_ritardi)
            description = f"{description or ''}\n\n--- NOTE DISPONIBILITÀ AUTOMATICHE ---\n{ritardi_str}".strip()

    event.title = title
    event.tipologia = tipologia
    event.description = description or ""
    event.operai = ", ".join(operai)
    event.start_time = dt_start
    event.end_time = dt_end
    
    db.commit()

    # 🚀 Messaggio WhatsApp anche per ogni modifica (con avviso se c'è sovrapposizione)
    testo_messaggio = costruisci_messaggio_whatsapp(
        "Intervento / Cantiere Modificato", title, tipologia,
        dt_start, dt_end, event.operai, current_user.username, warning_msg
    )
    background_tasks.add_task(invia_whatsapp_gruppo_asincrono, testo_messaggio)

    redirect_url = "/"
    if warning_msg:
        redirect_url += f"?warning_msg={urllib.parse.quote(warning_msg)}"

    return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)

@app.post("/events/delete/{event_id}")
def delete_event(request: Request, event_id: int, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    
    event = db.query(Event).filter(Event.id == event_id).first()
    if event:
        if current_user.role == 'admin' or current_user.username == 'admin' or event.user_id == current_user.id:
            db.delete(event)
            db.commit()
            
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

# --- TIPOLOGIE (AGGIUNTA, MODIFICA, CANCELLAZIONE) ---
@app.post("/tipologie/add")
def add_tipologia(request: Request, nome: str = Form(...), db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    
    if nome.strip():
        existing = db.query(Tipologia).filter(Tipologia.nome == nome.strip()).first()
        if not existing:
            db.add(Tipologia(nome=nome.strip()))
            db.commit()
            
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/tipologie/edit/{tipologia_id}")
def edit_tipologia(request: Request, tipologia_id: int, nome: str = Form(...), db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    
    tip = db.query(Tipologia).filter(Tipologia.id == tipologia_id).first()
    if tip and nome.strip():
        tip.nome = nome.strip()
        db.commit()
        
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/tipologie/delete/{tipologia_id}")
def delete_tipologia(request: Request, tipologia_id: int, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    
    tip = db.query(Tipologia).filter(Tipologia.id == tipologia_id).first()
    if tip:
        db.delete(tip)
        db.commit()
        
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

# --- ESPORTAZIONE PDF ---
@app.get("/export/pdf")
def export_pdf(
    request: Request, 
    year: int = Query(None), 
    month: int = Query(None), 
    db: Session = Depends(get_db)
):
    current_user = get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, 
        pagesize=landscape(letter), 
        rightMargin=20, 
        leftMargin=20, 
        topMargin=20, 
        bottomMargin=20
    )
    elements = []
    styles = getSampleStyleSheet()
    
    c_primary_dark = colors.HexColor("#13293d")
    c_header_days = colors.HexColor("#006466")
    c_header_weekend = colors.HexColor("#64748b")
    c_grid_border = colors.HexColor("#cbd5e1")
    c_bg_row = colors.HexColor("#f8fafc")
    
    title_style = ParagraphStyle('DocTitle', parent=styles['Heading1'], fontSize=13, leading=16, fontName="Helvetica-Bold", textColor=colors.white)
    subtitle_style = ParagraphStyle('DocSubTitle', parent=styles['Normal'], fontSize=16, leading=20, fontName="Helvetica-Bold", textColor=colors.white)
    section_heading = ParagraphStyle('SecHeading', parent=styles['Heading2'], fontSize=11, leading=14, fontName="Helvetica-Bold", textColor=colors.HexColor("#13293d"), spaceBefore=0, spaceAfter=8)
    cell_style = ParagraphStyle('GridCell', parent=styles['Normal'], fontSize=6.5, leading=8, textColor=colors.HexColor("#1e293b"))
    header_cell_work = ParagraphStyle('GridHeaderWork', parent=styles['Normal'], fontSize=8, leading=10, fontName="Helvetica-Bold", textColor=colors.white, alignment=1)
    header_cell_we = ParagraphStyle('GridHeaderWE', parent=styles['Normal'], fontSize=8, leading=10, fontName="Helvetica-Bold", textColor=colors.white, alignment=1)

    mesi_it = {
        1: "GENNAIO", 2: "FEBBRAIO", 3: "MARZO", 4: "APRILE",
        5: "MAGGIO", 6: "GIUGNO", 7: "LUGLIO", 8: "AGOSTO",
        9: "SETTEMBRE", 10: "OTTOBRE", 11: "NOVEMBRE", 12: "DICEMBRE"
    }
    
    target_year = year if year else datetime.now().year
    target_month = month if month else datetime.now().month
    nome_mese = mesi_it.get(target_month, "").upper()

    header_data = [
        [
            Paragraph(f"PROGRAMMAZIONE SQUADRE E ASSENZE | {nome_mese} {target_year}", title_style),
            Paragraph(datetime.now().strftime("%d/%m/%Y"), ParagraphStyle('RightDate', parent=subtitle_style, alignment=2))
        ],
        [
            Paragraph("Calendario interventi e presenze falegnameria", subtitle_style),
            Paragraph("", styles['Normal'])
        ]
    ]
    t_top = Table(header_data, colWidths=[552, 200])
    t_top.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), c_primary_dark),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING', (0,0), (-1,-1), 8),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
        ('LEFTPADDING', (0,0), (-1,-1), 10),
        ('RIGHTPADDING', (0,0), (-1,-1), 10),
    ]))
    elements.append(t_top)
    elements.append(Spacer(1, 6))

    events = db.query(Event).order_by(Event.start_time.asc()).all()
    
    cal_table_data = [[
        Paragraph("LUNEDI", header_cell_work),
        Paragraph("MARTEDI", header_cell_work),
        Paragraph("MERCOLEDI", header_cell_work),
        Paragraph("GIOVEDI", header_cell_work),
        Paragraph("VENERDI", header_cell_work),
        Paragraph("SABATO", header_cell_we),
        Paragraph("DOMENICA", header_cell_we)
    ]]
    
    cal = calendar.Calendar(firstweekday=0)
    month_days = list(cal.monthdayscalendar(target_year, target_month))
    
    events_by_day = {}
    for ev in events:
        user_color = ev.owner.color if ev.owner and ev.owner.color else "#3b82f6"
        resp_name = ev.owner.username if ev.owner else "-"
        
        curr_date = ev.start_time.date()
        end_date = ev.end_time.date() if ev.end_time else ev.start_time.date()
        
        while curr_date <= end_date:
            if curr_date.month == target_month and curr_date.year == target_year:
                day_key = curr_date.day
                if day_key not in events_by_day:
                    events_by_day[day_key] = []
                
                start_str = ev.start_time.strftime('%H:%M')
                end_str = ev.end_time.strftime('%H:%M') if ev.end_time else ''
                time_display = f"{start_str} - {end_str}" if end_str and end_str != start_str else start_str

                if "assenza" in ev.tipologia.lower() or "ferie" in ev.tipologia.lower() or "malattia" in ev.tipologia.lower():
                    txt = f"<font color='#dc2626'><b>🚫 {ev.title}</b></font> ({ev.tipologia})<br/><font color='{user_color}'>Resp: <b>{resp_name}</b></font><br/>{time_display} | {ev.operai or ''}"
                else:
                    txt = f"<font color='{user_color}'>■</font> <b>{ev.title}</b> ({ev.tipologia})<br/><font color='{user_color}'>Resp: <b>{resp_name}</b></font><br/>{time_display} | {ev.operai or ''}"
                
                events_by_day[day_key].append(txt)
            curr_date += timedelta(days=1)

    for week in month_days:
        week_row = []
        for day in week:
            if day == 0:
                week_row.append(Paragraph("", cell_style))
            else:
                cell_content = f"<b>{day}</b><br/>"
                if day in events_by_day:
                    cell_content += "<br/>".join(events_by_day[day])
                week_row.append(Paragraph(cell_content, cell_style))
        cal_table_data.append(week_row)

    col_w = 752 / 7
    t_cal = Table(cal_table_data, colWidths=[col_w]*7)
    t_cal.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (4,0), c_header_days),
        ('BACKGROUND', (5,0), (6,0), c_header_weekend),
        ('ALIGN', (0,0), (-1,-1), 'LEFT'),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('LEFTPADDING', (0,0), (-1,-1), 4),
        ('RIGHTPADDING', (0,0), (-1,-1), 4),
        ('GRID', (0,0), (-1,-1), 0.5, c_grid_border),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, c_bg_row]),
    ]))
    elements.append(t_cal)
    
    elements.append(PageBreak())

    elements.append(Paragraph("Dettaglio interventi e assenze registrate", section_heading))
    
    if events:
        detail_header = [
            Paragraph("PERIODO", header_cell_work),
            Paragraph("CANTIERE / EVENTO", header_cell_work),
            Paragraph("TIPOLOGIA", header_cell_work),
            Paragraph("ATTIVITÀ / MOTIVO", header_cell_work),
            Paragraph("PERSONALE / SQUADRA", header_cell_work),
            Paragraph("RESPONSABILE", header_cell_work)
        ]
        detail_data = [detail_header]
        
        for ev in events:
            if ev.start_time.month == target_month and ev.start_time.year == target_year:
                u_color = ev.owner.color if ev.owner and ev.owner.color else "#3b82f6"
                resp_html = f"<font color='{u_color}'><b>{ev.owner.username if ev.owner else '-'}</b></font>"
                period_str = f"{ev.start_time.strftime('%d/%m/%Y %H:%M')}<br/>al {ev.end_time.strftime('%d/%m/%Y %H:%M')}" if ev.end_time else ev.start_time.strftime('%d/%m/%Y %H:%M')
                detail_data.append([
                    Paragraph(period_str, cell_style),
                    Paragraph(f"<b>{ev.title}</b>", cell_style),
                    Paragraph(ev.tipologia or "-", cell_style),
                    Paragraph(ev.description or "-", cell_style),
                    Paragraph(ev.operai or "-", cell_style),
                    Paragraph(resp_html, cell_style)
                ])
            
        t_detail = Table(detail_data, colWidths=[90, 120, 75, 217, 100, 100])
        t_detail.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), c_primary_dark),
            ('ALIGN', (0,0), (-1,-1), 'LEFT'),
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('TOPPADDING', (0,0), (-1,-1), 5),
            ('BOTTOMPADDING', (0,0), (-1,-1), 5),
            ('LEFTPADDING', (0,0), (-1,-1), 5),
            ('RIGHTPADDING', (0,0), (-1,-1), 5),
            ('GRID', (0,0), (-1,-1), 0.5, c_grid_border),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, c_bg_row]),
        ]))
        elements.append(t_detail)

    doc.build(elements)
    buffer.seek(0)
    
    return FastAPIResponse(
        content=buffer.getvalue(),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=Programmazione_{nome_mese}_{target_year}.pdf"}
    )

# =====================================================================
# PROGRAMMA SETTIMANALE: ogni venerdì alle 13:00 -> PDF + messaggio WhatsApp
# =====================================================================
# Variabili d'ambiente opzionali:
#   ORA_INVIO_SETTIMANALE  ora di invio del venerdì (default 13)
#   WAHA_INVIA_PDF         "1" per inviare anche il PDF in chat (richiede WAHA Plus)
#   APP_BASE_URL           indirizzo con cui i colleghi raggiungono il sito, es. https://cantieri.miaditta.it
#                          -> il messaggio del venerdì contiene il link alla pagina della settimana
#   SETTIMANA_TOKEN        (opzionale) codice segreto nel link; se manca ne viene generato uno e
#                          salvato in .settimana_token
GIORNI_IT = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]
ORA_INVIO_SETTIMANALE = int(os.getenv("ORA_INVIO_SETTIMANALE", "13"))
FINESTRA_RECUPERO_ORE = 3   # se il programma era spento alle 13, recupera fino alle 16
WAHA_INVIA_PDF = os.getenv("WAHA_INVIA_PDF", "0") == "1"
APP_BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")
FILE_ULTIMO_INVIO = ".ultimo_invio_settimanale"

def _lunedi_di(d: date_cls) -> date_cls:
    return d - timedelta(days=d.weekday())

def _lunedi_successivo(oggi: date_cls) -> date_cls:
    return oggi + timedelta(days=7 - oggi.weekday())

def _is_assenza(ev) -> bool:
    tip = (ev.tipologia or "").lower()
    return any(kw in tip for kw in ["assenza", "ferie", "malattia", "permesso"])

def _orario_nel_giorno(ev, giorno: date_cls) -> str:
    inizio = ev.start_time
    fine = ev.end_time or ev.start_time
    primo, ultimo = inizio.date() == giorno, fine.date() == giorno
    # 00:00 come inizio e 23:59/00:00 come fine = evento "a giornata intera" (tipico di ferie/assenze)
    inizia_a_mezzanotte = inizio.hour == 0 and inizio.minute == 0
    finisce_a_fine_giornata = (fine.hour == 23 and fine.minute >= 59) or (fine.hour == 0 and fine.minute == 0)
    if primo and ultimo:
        if inizia_a_mezzanotte and finisce_a_fine_giornata:
            return "tutto il giorno"
        return f"{inizio:%H:%M}-{fine:%H:%M}" if fine != inizio else f"{inizio:%H:%M}"
    if primo:
        return "tutto il giorno" if inizia_a_mezzanotte else f"dalle {inizio:%H:%M}"
    if ultimo:
        return "tutto il giorno" if finisce_a_fine_giornata else f"fino alle {fine:%H:%M}"
    return "tutto il giorno"

def _eventi_settimana(db: Session, lunedi: date_cls):
    """Ritorna (lista eventi della settimana, dict giorno 0-6 -> [(evento, orario)])."""
    inizio = datetime.combine(lunedi, datetime.min.time())
    fine = inizio + timedelta(days=7)
    per_giorno = {i: [] for i in range(7)}
    eventi = []
    for ev in db.query(Event).filter(Event.start_time < fine).order_by(Event.start_time.asc()).all():
        ev_fine = ev.end_time or ev.start_time
        if ev_fine < inizio:
            continue
        eventi.append(ev)
        g = max(ev.start_time.date(), lunedi)
        ultimo = min(ev_fine.date(), lunedi + timedelta(days=6))
        while g <= ultimo:
            per_giorno[(g - lunedi).days].append((ev, _orario_nel_giorno(ev, g)))
            g += timedelta(days=1)
    return eventi, per_giorno

def genera_pdf_settimana(db: Session, lunedi: date_cls) -> bytes:
    eventi, per_giorno = _eventi_settimana(db, lunedi)
    domenica = lunedi + timedelta(days=6)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(letter), rightMargin=20, leftMargin=20, topMargin=20, bottomMargin=20)
    styles = getSampleStyleSheet()

    c_dark = colors.HexColor("#13293d")
    c_days = colors.HexColor("#006466")
    c_weekend = colors.HexColor("#64748b")
    c_border = colors.HexColor("#cbd5e1")
    c_row = colors.HexColor("#f8fafc")

    title_style = ParagraphStyle('WTitle', parent=styles['Heading1'], fontSize=13, leading=16, fontName="Helvetica-Bold", textColor=colors.white)
    sub_style = ParagraphStyle('WSub', parent=styles['Normal'], fontSize=16, leading=20, fontName="Helvetica-Bold", textColor=colors.white)
    right_style = ParagraphStyle('WRight', parent=sub_style, alignment=2)
    head_style = ParagraphStyle('WHead', parent=styles['Normal'], fontSize=8, leading=10, fontName="Helvetica-Bold", textColor=colors.white, alignment=1)
    cell_style = ParagraphStyle('WCell', parent=styles['Normal'], fontSize=7.5, leading=9.5, textColor=colors.HexColor("#1e293b"))
    small_style = ParagraphStyle('WSmall', parent=styles['Normal'], fontSize=6.5, leading=8, textColor=colors.HexColor("#1e293b"))
    section_style = ParagraphStyle('WSection', parent=styles['Heading2'], fontSize=11, leading=14, fontName="Helvetica-Bold", textColor=c_dark, spaceBefore=0, spaceAfter=8)

    elements = []

    # --- intestazione ---
    t_top = Table([
        [Paragraph(f"PROGRAMMAZIONE SETTIMANALE | {lunedi:%d/%m/%Y} - {domenica:%d/%m/%Y}", title_style),
         Paragraph(datetime.now().strftime("%d/%m/%Y"), right_style)],
        [Paragraph("Calendario interventi e presenze falegnameria", sub_style), Paragraph("", styles['Normal'])],
    ], colWidths=[552, 200])
    t_top.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), c_dark), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 8), ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING', (0, 0), (-1, -1), 10), ('RIGHTPADDING', (0, 0), (-1, -1), 10),
    ]))
    elements += [t_top, Spacer(1, 6)]

    # --- griglia dei 7 giorni ---
    header_row, body_row = [], []
    for i in range(7):
        giorno = lunedi + timedelta(days=i)
        header_row.append(Paragraph(f"{GIORNI_IT[i].upper()}<br/>{giorno:%d/%m}", head_style))
        if not per_giorno[i]:
            body_row.append(Paragraph("<font color='#94a3b8'>-</font>", cell_style))
            continue
        blocchi = []
        for ev, orario in per_giorno[i]:
            titolo = xml_escape(ev.title or "")
            colore = _colore_sicuro(ev.owner.color if ev.owner else None)
            operai = xml_escape(ev.operai or "")
            if _is_assenza(ev):
                riga = f"<font color='#dc2626'><b>{titolo}</b></font><br/>ASSENZA - {xml_escape(ev.tipologia or '')}<br/>{orario}"
            else:
                riga = f"<font color='{colore}'><b>{titolo}</b></font><br/>{xml_escape(ev.tipologia or '')}<br/>{orario}"
            if operai:
                riga += f"<br/>Operai: {operai}"
            blocchi.append(riga)
        body_row.append(Paragraph("<br/><br/>".join(blocchi), cell_style))

    col_w = 752 / 7
    try:
        t_cal = Table([header_row, body_row], colWidths=[col_w] * 7, repeatRows=1, splitInRow=1)
    except TypeError:  # versioni vecchie di reportlab senza splitInRow
        t_cal = Table([header_row, body_row], colWidths=[col_w] * 7, repeatRows=1)
    t_cal.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (4, 0), c_days), ('BACKGROUND', (5, 0), (6, 0), c_weekend),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 5), ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 4), ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('GRID', (0, 0), (-1, -1), 0.5, c_border),
        ('BACKGROUND', (5, 1), (6, 1), c_row),
    ]))
    elements.append(t_cal)

    # --- dettaglio ---
    elements += [PageBreak(), Paragraph("Dettaglio interventi e assenze della settimana", section_style)]
    if eventi:
        dati = [[Paragraph(h, head_style) for h in
                 ["PERIODO", "CANTIERE / EVENTO", "TIPOLOGIA", "ATTIVITÀ / MOTIVO", "PERSONALE / SQUADRA", "RESPONSABILE"]]]
        for ev in eventi:
            colore = _colore_sicuro(ev.owner.color if ev.owner else None)
            resp = f"<font color='{colore}'><b>{xml_escape(ev.owner.username) if ev.owner else '-'}</b></font>"
            periodo = (f"{ev.start_time:%d/%m/%Y %H:%M}<br/>al {ev.end_time:%d/%m/%Y %H:%M}"
                       if ev.end_time else f"{ev.start_time:%d/%m/%Y %H:%M}")
            dati.append([
                Paragraph(periodo, small_style),
                Paragraph(f"<b>{xml_escape(ev.title or '')}</b>", small_style),
                Paragraph(xml_escape(ev.tipologia or "-"), small_style),
                Paragraph(xml_escape(ev.description or "-"), small_style),
                Paragraph(xml_escape(ev.operai or "-"), small_style),
                Paragraph(resp, small_style),
            ])
        t_det = Table(dati, colWidths=[90, 120, 75, 217, 100, 100], repeatRows=1)
        t_det.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), c_dark), ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('TOPPADDING', (0, 0), (-1, -1), 5), ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('LEFTPADDING', (0, 0), (-1, -1), 5), ('RIGHTPADDING', (0, 0), (-1, -1), 5),
            ('GRID', (0, 0), (-1, -1), 0.5, c_border),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, c_row]),
        ]))
        elements.append(t_det)
    else:
        elements.append(Paragraph("Nessun intervento o assenza registrati per questa settimana.", styles['Normal']))

    doc.build(elements)
    return buffer.getvalue()

def costruisci_testo_settimana(db: Session, lunedi: date_cls) -> str:
    eventi, per_giorno = _eventi_settimana(db, lunedi)
    fine = _ultimo_giorno_mostrato(lunedi, per_giorno)
    righe = [f"📅 *Programma settimana dal {lunedi:%d/%m} al {fine:%d/%m/%Y}*", ""]
    if not eventi:
        righe.append("Nessun intervento in programma.")
    else:
        for i in range(7):
            voci = per_giorno[i]
            if not voci and i == 6:   # la domenica la mostro solo se ha eventi
                continue
            righe.append(f"*{GIORNI_IT[i]} {lunedi + timedelta(days=i):%d/%m}*")
            if not voci:
                righe.append("— nessun impegno")
            for ev, orario in voci:
                simbolo = "🚫" if _is_assenza(ev) else "•"
                riga = f"{simbolo} *{ev.title}* ({ev.tipologia}) {orario}"
                if ev.operai:
                    riga += f" — {ev.operai}"
                righe.append(riga)
            righe.append("")
    return "\n".join(righe).rstrip()

# ---------------------------------------------------------------------
# PAGINA WEB DELLA SETTIMANA (si apre dal link mandato su WhatsApp)
# ---------------------------------------------------------------------
MESI_IT = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
           "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]
FILE_TOKEN_SETTIMANA = ".settimana_token"

def _carica_token_settimana() -> str:
    """Codice segreto presente nel link: chi non lo conosce non può aprire la pagina."""
    token = os.getenv("SETTIMANA_TOKEN", "").strip()
    if token:
        return token
    try:
        with open(FILE_TOKEN_SETTIMANA, "r") as f:
            token = f.read().strip()
        if token:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(16)
    try:
        with open(FILE_TOKEN_SETTIMANA, "w") as f:
            f.write(token)
    except OSError as e:
        print(f"⚠️ Impossibile salvare {FILE_TOKEN_SETTIMANA}: {e} (il link cambierà ad ogni riavvio)")
    return token

SETTIMANA_TOKEN = _carica_token_settimana()

def _plurale(n: int, singolare: str, plurale: str) -> str:
    return f"{n} {singolare if n == 1 else plurale}"

def _intervallo_it(inizio: date_cls, fine: date_cls) -> str:
    if inizio.year != fine.year:
        return f"{inizio.day} {MESI_IT[inizio.month - 1]} {inizio.year} – {fine.day} {MESI_IT[fine.month - 1]} {fine.year}"
    if inizio.month != fine.month:
        return f"{inizio.day} {MESI_IT[inizio.month - 1]} – {fine.day} {MESI_IT[fine.month - 1]} {fine.year}"
    return f"{inizio.day} – {fine.day} {MESI_IT[inizio.month - 1]} {fine.year}"

def _ultimo_giorno_mostrato(lunedi: date_cls, per_giorno) -> date_cls:
    """La settimana va da lunedì a sabato; si estende alla domenica solo se ha eventi."""
    return lunedi + timedelta(days=6 if per_giorno[6] else 5)

def _colore_sicuro(colore: Optional[str]) -> str:
    # Il colore finisce in un attributo style: accetto solo un esadecimale valido.
    if colore and re.fullmatch(r"#[0-9a-fA-F]{3,8}", colore):
        return colore
    return "#3b82f6"

def _conteggi_settimana(eventi):
    n_ass = sum(1 for e in eventi if _is_assenza(e))
    n_int = len(eventi) - n_ass
    return n_int, n_ass

def _contesto_pagina_settimana(db: Session, lunedi: date_cls) -> dict:
    eventi, per_giorno = _eventi_settimana(db, lunedi)
    oggi = datetime.now().date()
    giorni = []
    for i in range(7):
        d = lunedi + timedelta(days=i)
        voci = per_giorno[i]
        if i == 6 and not voci:     # la domenica la mostro solo se ha eventi
            continue
        card = []
        for ev, orario in voci:
            fine = ev.end_time or ev.start_time
            multi = fine.date() != ev.start_time.date()
            card.append({
                "titolo": ev.title or "",
                "tipologia": ev.tipologia or "-",
                "orario": orario,
                "periodo": f"{ev.start_time:%d/%m} – {fine:%d/%m}" if multi else "",
                "operai": ev.operai or "",
                "descrizione": ev.description or "",
                "resp": ev.owner.username if ev.owner else "-",
                "colore": _colore_sicuro(ev.owner.color if ev.owner else None),
                "assenza": _is_assenza(ev),
            })
        giorni.append({"nome": GIORNI_IT[i], "numero": d.day, "mese": MESI_IT[d.month - 1],
                       "oggi": d == oggi, "weekend": i >= 5, "eventi": card})
    n_int, n_ass = _conteggi_settimana(eventi)
    base = f"/settimana/{SETTIMANA_TOKEN}"
    return {
        "intervallo": _intervallo_it(lunedi, _ultimo_giorno_mostrato(lunedi, per_giorno)),
        "giorni": giorni,
        "n_assenze": n_ass,
        "txt_interventi": _plurale(n_int, "intervento", "interventi"),
        "txt_assenze": _plurale(n_ass, "assenza", "assenze"),
        "riepilogo": f"{_plurale(n_int, 'intervento', 'interventi')}, {_plurale(n_ass, 'assenza', 'assenze')}",
        "prev_url": f"{base}?data={(lunedi - timedelta(days=7)).isoformat()}",
        "next_url": f"{base}?data={(lunedi + timedelta(days=7)).isoformat()}",
        "oggi_url": f"{base}?data={_lunedi_di(oggi).isoformat()}",
        "pdf_url": f"{base}/pdf?data={lunedi.isoformat()}",
        "aggiornato_il": datetime.now().strftime("%d/%m/%Y alle %H:%M"),
    }

def _controlla_token(token: str):
    if not secrets.compare_digest(token.encode("utf-8"), SETTIMANA_TOKEN.encode("utf-8")):
        raise HTTPException(status_code=404, detail="Pagina non trovata")

def _lunedi_da_parametro(data: Optional[str]) -> date_cls:
    try:
        giorno = datetime.strptime(data, "%Y-%m-%d").date() if data else datetime.now().date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Data non valida, usa il formato AAAA-MM-GG")
    return _lunedi_di(giorno)

@app.get("/settimana/{token}", response_class=HTMLResponse)
def pagina_settimana(token: str, data: Optional[str] = Query(None), db: Session = Depends(get_db)):
    """Pagina di sola lettura con gli eventi della settimana. ?data=AAAA-MM-GG = un giorno qualsiasi di quella settimana."""
    _controlla_token(token)
    lunedi = _lunedi_da_parametro(data)
    try:
        html = templates.get_template("settimana.html").render(**_contesto_pagina_settimana(db, lunedi))
    except TemplateNotFound:
        raise HTTPException(status_code=500, detail="Manca il file templates/settimana.html")
    return HTMLResponse(html)

@app.get("/settimana/{token}/pdf")
def pdf_settimana_con_token(token: str, data: Optional[str] = Query(None), db: Session = Depends(get_db)):
    _controlla_token(token)
    lunedi = _lunedi_da_parametro(data)
    return FastAPIResponse(
        content=genera_pdf_settimana(db, lunedi),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=Programma_settimana_{lunedi:%Y-%m-%d}.pdf"},
    )

def link_pagina_settimana(lunedi: date_cls) -> str:
    return f"{APP_BASE_URL}/settimana/{SETTIMANA_TOKEN}?data={lunedi.isoformat()}"

def costruisci_messaggio_link_settimana(db: Session, lunedi: date_cls) -> str:
    eventi, per_giorno = _eventi_settimana(db, lunedi)
    fine = _ultimo_giorno_mostrato(lunedi, per_giorno)
    n_int, n_ass = _conteggi_settimana(eventi)
    righe = [f"📅 *Programma settimana dal {lunedi:%d/%m} al {fine:%d/%m/%Y}*"]
    if eventi:
        riepilogo = _plurale(n_int, "intervento", "interventi")
        if n_ass:
            riepilogo += f" · {_plurale(n_ass, 'assenza', 'assenze')}"
        righe.append(riepilogo)
    else:
        righe.append("Nessun intervento in programma.")
    righe += ["", "👉 Apri il programma:", link_pagina_settimana(lunedi)]
    return "\n".join(righe)

def invia_whatsapp_file_asincrono(pdf_bytes: bytes, nome_file: str, didascalia: str = ""):
    _wa_queue.put({"endpoint": "sendFile", "json": {
        "session": WAHA_SESSION,
        "chatId": WHATSAPP_CHAT_ID,
        "file": {"mimetype": "application/pdf", "filename": nome_file, "data": base64.b64encode(pdf_bytes).decode()},
        "caption": didascalia,
    }})

def _leggi_ultimo_invio() -> str:
    try:
        with open(FILE_ULTIMO_INVIO, "r") as f:
            return f.read().strip()
    except OSError:
        return ""

def _scrivi_ultimo_invio(valore: str):
    try:
        with open(FILE_ULTIMO_INVIO, "w") as f:
            f.write(valore)
    except OSError as e:
        print(f"⚠️ Impossibile scrivere {FILE_ULTIMO_INVIO}: {e}")

def invia_programma_settimanale(lunedi: Optional[date_cls] = None, segna_inviato: bool = True):
    """Prepara e invia il programma della settimana (default: la prossima)."""
    lunedi = lunedi or _lunedi_successivo(datetime.now().date())
    db = SessionLocal()
    try:
        if APP_BASE_URL:
            testo = costruisci_messaggio_link_settimana(db, lunedi)
        else:
            testo = costruisci_testo_settimana(db, lunedi)   # senza link raggiungibile mando il programma scritto
        pdf = genera_pdf_settimana(db, lunedi) if WAHA_INVIA_PDF else None
    finally:
        db.close()

    if segna_inviato:
        _scrivi_ultimo_invio(datetime.now().date().isoformat())

    invia_whatsapp_gruppo_asincrono(testo)
    if pdf:
        invia_whatsapp_file_asincrono(pdf, f"Programma_settimana_{lunedi:%Y-%m-%d}.pdf", f"Programma settimana dal {lunedi:%d/%m/%Y}")
    print(f"📅 Programma settimanale preparato (settimana dal {lunedi:%d/%m/%Y})")

def _scheduler_settimanale():
    """Ogni 30 secondi controlla se è venerdì dopo le 13 e se l'invio di oggi non è ancora stato fatto."""
    while True:
        try:
            ora = datetime.now()
            in_finestra = ORA_INVIO_SETTIMANALE <= ora.hour < ORA_INVIO_SETTIMANALE + FINESTRA_RECUPERO_ORE
            if ora.weekday() == 4 and in_finestra and _leggi_ultimo_invio() != ora.date().isoformat():
                invia_programma_settimanale()
        except Exception as e:
            print(f"❌ Errore programma settimanale: {e}")
        time.sleep(30)

@app.on_event("startup")
def avvia_scheduler_settimanale():
    # Parte solo nel processo che serve davvero l'app (non nel supervisore di uvicorn --reload).
    threading.Thread(target=_scheduler_settimanale, daemon=True).start()
    print(f"⏰ Invio programma settimanale attivo: ogni venerdì alle {ORA_INVIO_SETTIMANALE}:00")
    print(f"🔗 Pagina della settimana: {APP_BASE_URL or 'http://localhost:8000'}/settimana/{SETTIMANA_TOKEN}")
    if not APP_BASE_URL:
        print("⚠️ APP_BASE_URL non impostato: il messaggio del venerdì conterrà il programma scritto, senza link.")

@app.get("/export/pdf/settimana")
def export_pdf_settimana(request: Request, data: Optional[str] = Query(None), db: Session = Depends(get_db)):
    """PDF di una settimana. ?data=YYYY-MM-DD = un giorno qualsiasi di quella settimana (default: settimana corrente)."""
    if not get_current_user(request, db):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    try:
        giorno = datetime.strptime(data, "%Y-%m-%d").date() if data else datetime.now().date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Data non valida, usa il formato AAAA-MM-GG")
    lunedi = _lunedi_di(giorno)
    pdf = genera_pdf_settimana(db, lunedi)
    return FastAPIResponse(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=Programma_settimana_{lunedi:%Y-%m-%d}.pdf"},
    )

if __name__ == "__main__":
    if "--invia-settimana" in sys.argv:
        # Prova manuale:  python main.py --invia-settimana [AAAA-MM-GG]
        # Invia subito il programma della settimana successiva (o di quella che contiene la data indicata).
        idx = sys.argv.index("--invia-settimana")
        lunedi_manuale = None
        if len(sys.argv) > idx + 1:
            lunedi_manuale = _lunedi_di(datetime.strptime(sys.argv[idx + 1], "%Y-%m-%d").date())
        invia_programma_settimanale(lunedi_manuale, segna_inviato=False)
        _wa_queue.join()
    else:
        import uvicorn
        uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)