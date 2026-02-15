# Turni Emodinamica – M/N/O/P + J (progetto nuovo)

Questo progetto **non dipende** dal progetto “turni_autogen” originale: è stato riscritto da zero.
L’unico file mantenuto identico è `Style_Template.xlsx` (usato solo per lo stile Excel).

## Cosa genera
- Colonne **M, N, O**: tutti i giorni
- Colonna **P**: solo **Mercoledì**
- Colonna **J**: solo **Giovedì**

## Pool
Vizzari, Virga, Carciotto, Giusti, De Luca, Saporito  
+ **Utente Test** (solo per pannello indisponibilità, mai usato nei turni)

## Storage indisponibilità (GitHub)
Le indisponibilità sono salvate in un file JSON nella repo GitHub configurata nei `secrets`:
- `GITHUB_REPO`, `GITHUB_BRANCH`, `GITHUB_DATA_PATH`, `GITHUB_TOKEN`

Ogni medico vede e modifica **solo** le proprie indisponibilità.

## Deploy su Streamlit Community Cloud
1) Crea una nuova repo GitHub e carica questo progetto.
2) Su Streamlit Cloud crea una nuova app puntando a `streamlit_app.py`.
3) Imposta i secrets (vedi `.streamlit/secrets.toml.example`).

## Note operative
Se i vincoli rendono il mese **infeasible** (es. indisponibilità che impediscono “M martedì = Vizzari/Virga”), l’app mostra un errore con una diagnosi.

