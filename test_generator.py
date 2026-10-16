from datetime import datetime, timedelta
import random
from sqlalchemy.orm import Session
from main import SessionLocal, User, Event, Operaio, Tipologia, Base, engine

# Ricrea il DB per garantire l'allineamento dei campi
Base.metadata.create_all(bind=engine)

def seed():
    db: Session = SessionLocal()
    
    print("🧹 Pulizia vecchi dati...")
    db.query(Event).delete()
    db.query(User).delete()
    db.query(Operaio).delete()
    db.query(Tipologia).delete()
    db.commit()

    print("👤 Creazione Utenti con Colori Personalizzati...")
    users_data = [
        {"username": "admin", "password": "password123", "role": "admin", "color": "#1e293b"},
        {"username": "mirko", "password": "mirko", "role": "editor", "color": "#2563eb"}, # Blu
        {"username": "jury", "password": "jury", "role": "editor", "color": "#059669"},   # Verde
        {"username": "lena", "password": "lena", "role": "editor", "color": "#d97706"},   # Arancione
    ]
    
    created_users = {}
    for u in users_data:
        user = User(username=u["username"], password=u["password"], role=u["role"], color=u["color"])
        db.add(user)
        db.commit()
        db.refresh(user)
        created_users[u["username"]] = user

    print("👷 Creazione Operai...")
    operai_list = ["Massimo Bevilacqua", "Giovanni Petti", "Gaetano Pepe", "Emilio Giordano", "Jury Novarelli"]
    for op_name in operai_list:
        db.add(Operaio(nome=op_name))
    db.commit()

    print("🏷️ Creazione Tipologie...")
    tipologie_cantieri = ["Montaggio", "Smontaggio", "Sopralluogo", "Rifiniture"]
    tipologie_assenze = ["Assenza / Ferie", "Malattia"]
    
    for tip_name in tipologie_cantieri + tipologie_assenze:
        db.add(Tipologia(nome=tip_name))
    db.commit()

    print("📅 Generazione Appuntamenti e Assenze su 2 Mesi...")
    cantieri = ["Cantiere Via Roma", "Showroom Milano", "Villa Bracigliano", "Appartamento Salerno", "Ufficio Nocera"]
    attivita_cantieri = [
        "Installazione armadi e boiserie", 
        "Sopralluogo misure e rilievo", 
        "Montaggio cucina su misura", 
        "Smontaggio e smaltimento vecchi arredi"
    ]
    
    motivi_assenze = [
        "Ferie programmate", 
        "Permesso personale", 
        "Assenza per visita medica", 
        "Certificato medico / Malattia"
    ]
    
    editor_keys = ["mirko", "jury", "lena"]
    now = datetime.now()

    # Calcoliamo il primo giorno del mese corrente e del mese successivo
    current_month_date = now.replace(day=1, hour=8, minute=0, second=0, microsecond=0)
    
    # Calcolo sicuro del mese successivo
    if now.month == 12:
        next_month_date = now.replace(year=now.year + 1, month=1, day=1, hour=8, minute=0, second=0, microsecond=0)
    else:
        next_month_date = now.replace(month=now.month + 1, day=1, hour=8, minute=0, second=0, microsecond=0)

    # Generiamo 10 eventi nel mese corrente e 10 nel mese successivo
    target_periods = [
        (current_month_date, 10), # (Data di riferimento, numero eventi)
        (next_month_date, 10)
    ]

    event_counter = 1
    for start_month_base, count in target_periods:
        for _ in range(count):
            u_key = random.choice(editor_keys)
            owner_user = created_users[u_key]
            
            # Scegliamo un giorno casuale all'interno del mese (da 1 a 25)
            random_days = random.randint(0, 24)
            random_hour = random.choice([8, 10, 14, 16])
            
            event_start = start_month_base + timedelta(days=random_days)
            event_start = event_start.replace(hour=random_hour)
            
            # Durata di 4 ore
            event_end = event_start + timedelta(hours=4)

            selected_operai = random.sample(operai_list, k=random.randint(1, 2))
            
            # Se l'utente è Lena, registra ESCLUSIVAMENTE Assenze / Ferie o Malattie
            if u_key == "lena":
                tipologia_scelta = random.choice(tipologie_assenze)
                titolo_evento = f"Assenza {selected_operai[0]}"
                descrizione_evento = random.choice(motivi_assenze)
            else:
                tipologia_scelta = random.choice(tipologie_cantieri)
                titolo_evento = f"{random.choice(cantieri)} #{event_counter}"
                descrizione_evento = random.choice(attivita_cantieri)

            event = Event(
                title=titolo_evento,
                tipologia=tipologia_scelta,
                description=descrizione_evento,
                operai=", ".join(selected_operai),
                start_time=event_start,
                end_time=event_end,
                user_id=owner_user.id
            )
            db.add(event)
            event_counter += 1

    db.commit()
    db.close()
    print("✅ Popolamento su 2 mesi completato con successo!")

if __name__ == "__main__":
    seed()