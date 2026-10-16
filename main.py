import io
import os
import calendar
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional, List

from fastapi import FastAPI, Depends, HTTPException, Request, Form, status, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response as FastAPIResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, Session

from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

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

# --- CREAZIONE AUTOMATICA ADMIN HARDCODED SE IL DB È NUOVO ---
def init_db():
    db = SessionLocal()
    try:
        admin_user = db.query(User).filter(User.username == "admin").first()
        if not admin_user:
            print("⚙️ Database vuoto: creazione utente admin predefinito...")
            default_admin = User(
                username="admin",
                password="admin123",  # <-- Password iniziale
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

# --- LOGICA CONTROLLO SOVRAPPOSIZIONI ED INVIO NOTIFICHE ---
def check_operaio_occupato(db: Session, operai_selezionati: list[str], dt_start: datetime, dt_end: datetime, is_nuova_assenza: bool, current_user: User, exclude_event_id: int = None):
    all_events = db.query(Event).all()
    blocchi = []
    avvisi = []

    for ev in all_events:
        if exclude_event_id and ev.id == exclude_event_id:
            continue
        
        if ev.operai:
            operai_coinvolti = [op.strip() for op in ev.operai.split(",") if op.strip()]
            intersezione = set(operai_selezionati).intersection(set(operai_coinvolti))
            
            if intersezione:
                ev_end = ev.end_time if ev.end_time else ev.start_time
                if max(dt_start, ev.start_time) <= min(dt_end, ev_end):
                    tip_exist_lower = ev.tipologia.lower()
                    ev_is_assenza = "assenza" in tip_exist_lower or "ferie" in tip_exist_lower or "malattia" in tip_exist_lower
                    
                    if is_nuova_assenza:
                        if ev_is_assenza:
                            blocchi.append(f"I seguenti operai hanno già un'assenza registrata in questo orario: {', '.join(intersezione)}")
                        else:
                            resp = ev.owner.username if ev.owner else 'sconosciuto'
                            msg_avviso = f"⚠️ Assenza registrata da {current_user.username.upper()}! NOTA: {', '.join(intersezione)} era/erano assegnati al cantiere '{ev.title}' (creato da {resp}). La squadra del cantiere va riorganizzata."
                            avvisi.append(msg_avviso)
                            
                            if ev.user_id and ev.user_id != current_user.id:
                                notif_msg = f"📢 ATTENZIONE: {current_user.username.upper()} ha registrato un'assenza/malattia per {', '.join(intersezione)} in data {dt_start.strftime('%d/%m/%Y %H:%M')}. Era assegnato al tuo cantiere '{ev.title}'!"
                                db.add(Notification(user_id=ev.user_id, message=notif_msg))
                                db.commit()
                    else:
                        if ev_is_assenza:
                            blocchi.append(f"Impossibile assegnare: {', '.join(intersezione)} risulta in ASSENZA / FERIE / MALATTIA ('{ev.title}')")
                        else:
                            msg_blocco = f"Sovrapposizione cantieri: {', '.join(intersezione)} è già sul cantiere '{ev.title}' nello stesso orario"
                            blocchi.append(msg_blocco)
                            
                            if ev.user_id and ev.user_id != current_user.id:
                                notif_msg = f"⚠️ SOVRAPPOSIZIONE CANTIERE: {current_user.username.upper()} ha programmato un cantiere con {', '.join(intersezione)} in data {dt_start.strftime('%d/%m/%Y %H:%M')}, operai già presenti sul tuo cantiere '{ev.title}'!"
                                db.add(Notification(user_id=ev.user_id, message=notif_msg))
                                db.commit()

    return blocchi, avvisi

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
    
    return templates.TemplateResponse("index.html", {
        "request": request,
        "current_user": current_user,
        "events": events,
        "operai": operai,
        "tipologie": tipologie,
        "error_msg": error_msg,
        "warning_msg": warning_msg,
        "notifications": unread_notifications
    })

# --- GESTIONE PROFILO (CAMBIO PASSWORD PERSONALE) ---
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

# --- PANNELLO ADMIN: GESTIONE UTENTI & OPERAI ---
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

# --- NOTIFICHE (LETTE) ---
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
        blocchi, avvisi = check_operaio_occupato(db, operai, dt_start, dt_end, is_nuova_assenza=is_assenza, current_user=current_user)
        
        if blocchi:
            msg = " | ".join(blocchi)
            return RedirectResponse(url=f"/?error_msg={urllib.parse.quote(msg)}", status_code=status.HTTP_303_SEE_OTHER)
        
        if avvisi:
            warning_msg = " | ".join(avvisi)

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

    redirect_url = "/"
    if warning_msg:
        redirect_url += f"?warning_msg={urllib.parse.quote(warning_msg)}"

    return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)

@app.post("/events/edit/{event_id}")
def edit_event(
    request: Request,
    event_id: int,
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
        blocchi, avvisi = check_operaio_occupato(db, operai, dt_start, dt_end, is_nuova_assenza=is_assenza, current_user=current_user, exclude_event_id=event_id)
        
        if blocchi:
            msg = " | ".join(blocchi)
            return RedirectResponse(url=f"/?error_msg={urllib.parse.quote(msg)}", status_code=status.HTTP_303_SEE_OTHER)
        
        if avvisi:
            warning_msg = " | ".join(avvisi)

    event.title = title
    event.tipologia = tipologia
    event.description = description or ""
    event.operai = ", ".join(operai)
    event.start_time = dt_start
    event.end_time = dt_end
    
    db.commit()

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

# --- TIPOLOGIE ---
@app.post("/tipologie/add")
def add_tipologia(request: Request, nome: str = Form(...), db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user or current_user.username == 'lena':
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    
    if nome.strip():
        existing = db.query(Tipologia).filter(Tipologia.nome == nome.strip()).first()
        if not existing:
            db.add(Tipologia(nome=nome.strip()))
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)