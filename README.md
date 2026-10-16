# 🪵 WoodCraft Planner — Gestione Cantieri, Squadre & Assenze

**WoodCraft Planner** è un'applicazione web completa per la gestione dei cantieri, la pianificazione degli interventi sul campo, la coordinazione delle squadre di operai e il tracciamento delle assenze/ferie.

Progettato specificamente per la gestione operativa di falegnamerie, studi di progettazione e imprese artigiane, il sistema elimina i conflitti di sovrapposizione tra squadre e genera automaticamente la reportistica mensile in formato PDF.

---

## 🚀 Caratteristiche Principali

* 📅 **Calendario Interattivo**: Integrazione con *FullCalendar* per la visualizzazione mensile, settimanale e ad elenco degli interventi programmati.
* 👥 **Gestione Squadre ed Operai**: Assegnazione rapida degli operai disponibili a ciascun cantiere o lavorazione.
* 🚫 **Rilevamento Intelligente Sovrapposizioni**: Controllo automatico in tempo reale delle disponibilità degli operai. Impedisce l'assegnazione contemporanea di un operaio a più cantieri o in giornate di assenza/ferie/malattia.
* 🔔 **Sistema Notifiche Integrato**: Alert automatici per i responsabili di cantiere in caso di modifiche della disponibilità del personale o sovrapposizioni di orario.
* 📄 **Esportazione PDF Mensile**: Generazione dinamica (tramite *ReportLab*) della pianificazione mensile stampabile, completa di griglia di riepilogo e dettaglio interventi.
* 🔑 **Gestione Ruoli e Permessi**:
  * **Admin**: Gestione completa di cantieri, utenze aziendali, operai attivi e opzioni avanzate.
  * **Editor / Personale**: Registrazione interventi di propria competenza e gestione autonoma delle assenze/ferie.
* 🏷️ **Tipologie Lavorazioni Personalizzabili**: Possibilità di aggiungere al volo nuove tipologie di intervento (es. Sopralluogo, Montaggio, Manutenzione).

---

## 🛠️ Stack Tecnologico

* **Backend**: Python 3, [FastAPI](https://fastapi.tiangolo.com/)
* **Database**: SQLite, [SQLAlchemy](https://www.sqlalchemy.org/)
* **Frontend**: HTML5, Jinja2 Templates, [Tailwind CSS](https://tailwindcss.com/)
* **Calendario**: [FullCalendar v6](https://fullcalendar.io/)
* **Generazione PDF**: [ReportLab](https://www.reportlab.com/)

---

## 📦 Installazione e Avvio Locale

### 1. Requisiti Prerequisiti
* Python 3.10 o superiore
* Git

### 2. Clonare il Repository
```bash
git clone [https://github.com/tuo-username/woodcraft-planner.git](https://github.com/tuo-username/woodcraft-planner.git)
cd woodcraft-planner
```

### 3. Creare ed Attivare un Ambiente Virtuale
•	Su Linux / macOS:
```bash
Bash
python3 -m venv .venv
source .venv/bin/activate
```

•	Su Windows (PowerShell):
```bash
PowerShell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

4. Installare le Dipendenze
```bash
Bash
pip install -r requirements.txt
```
6. Avviare l'Applicazione
```bash
Bash
uvicorn main:app --reload
```
L'applicazione sarà raggiungibile nel browser all'indirizzo: http://localhost:8000

## 🔑 Credenziali di Primo Accesso
Al primo avvio, il database crea automaticamente un utente amministratore di default:
•	Username: admin
•	Password: admin123
(Si raccomanda di modificare la password dell'amministratore subito dopo il primo accesso).

## 📜 Licenza
Questo progetto è distribuito sotto licenza Creative Commons Attribution-NonCommercial 4.0 International (CC BY-NC 4.0).
È possibile consultare, modificare, forkarlo e riutilizzarlo per scopi personali o interni, ma ne è espressamente vietata la rivendita o lo sfruttamento commerciale diretto. Per ulteriori dettagli, consultare il file LICENSE.
