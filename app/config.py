from __future__ import annotations

# Pool turni (NON include "Utente Test")
CORE_DOCTORS = ["Vizzari", "Virga", "Carciotto", "Giusti"]
ALL_DOCTORS = CORE_DOCTORS + ["De Luca", "Saporito"]

TEST_USER = "Utente Test"

# Colonne Excel (lettere)
COL_J = "J"  # solo Giovedì
COL_M = "M"
COL_N = "N"
COL_O = "O"
COL_P = "P"  # solo Mercoledì

# Stringa fissa per M il Martedì
TUE_M_PAIR = "Vizzari / Virga"

# Giorni della settimana (datetime.weekday): Lun=0 ... Dom=6
MON, TUE, WED, THU, FRI, SAT, SUN = range(7)

ITALIAN_DOW = ["lunedi", "martedi", "mercoledi", "giovedi", "venerdi", "sabato", "domenica"]
