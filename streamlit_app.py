
from __future__ import annotations

import datetime as dt
from io import BytesIO
from pathlib import Path
from typing import Dict, List

import streamlit as st

from app.config import ALL_DOCTORS, TEST_USER
from app.auth import AuthConfig, admin_login, doctor_login
from app.storage_github import GitHubTarget, GitHubStoreError
from app.unavailability import (
    VALID_SHIFTS,
    UnavailabilityEntry,
    load_all,
    get_doctor_month,
    save_doctor_month,
)
from app.scheduler import solve_month
from app.excel_io import create_month_workbook_from_style, write_assignments


APP_TITLE = "Turni Emodinamica – M/N/O/P + J"


def _load_auth_config() -> AuthConfig:
    doctor_pins = dict(st.secrets.get("DOCTOR_PINS", {}))

    # Fallbacks per evitare problemi di formattazione secrets
    admin_pin = (
        st.secrets.get("ADMIN_PIN")
        or st.secrets.get("admin_pin")
        or (st.secrets.get("ADMIN", {}) or {}).get("PIN")
        or (st.secrets.get("ADMIN", {}) or {}).get("pin")
        or ""
    )
    admin_pin = str(admin_pin)

    return AuthConfig(doctor_pins=doctor_pins, admin_pin=admin_pin)


def _load_github_target() -> GitHubTarget:
    token = str(st.secrets.get("GITHUB_TOKEN", ""))
    repo = str(st.secrets.get("GITHUB_REPO", ""))
    branch = str(st.secrets.get("GITHUB_BRANCH", "main"))
    path = str(st.secrets.get("GITHUB_DATA_PATH", "data/unavailability.json"))
    if not token or not repo:
        raise GitHubStoreError("Missing GitHub secrets: set GITHUB_TOKEN and GITHUB_REPO in Streamlit secrets.")
    return GitHubTarget(repo=repo, branch=branch, path=path)


def _fmt_date(d: dt.date) -> str:
    return d.strftime("%d/%m/%Y")


def _month_bounds(year: int, month: int) -> tuple[dt.date, dt.date]:
    import calendar
    first = dt.date(year, month, 1)
    last = dt.date(year, month, calendar.monthrange(year, month)[1])
    return first, last


def _normalize_rows_to_entries(rows: List[dict], year: int, month: int) -> tuple[List[UnavailabilityEntry], Dict[str, int]]:
    """
    Normalizza righe UI -> entries, deduplicate by (date, shift).
    Ignora righe fuori mese o con shift vuoto/non valido.
    """
    entries: List[UnavailabilityEntry] = []
    invalid_date = 0
    out_of_month = 0

    for r in rows or []:
        d = r.get("Data")
        if isinstance(d, dt.datetime):
            d = d.date()
        if not isinstance(d, dt.date):
            invalid_date += 1
            continue
        if d.year != int(year) or d.month != int(month):
            out_of_month += 1
            continue

        sh = str(r.get("Fascia") or "").strip()
        if sh not in set(VALID_SHIFTS):
            continue

        note = str(r.get("Note") or "")
        entries.append(UnavailabilityEntry(date=d, shift=sh, note=note, updated_at=""))

    # de-duplicate by (date, shift): keep last note
    dedup: dict[tuple[dt.date, str], UnavailabilityEntry] = {}
    for e in entries:
        dedup[(e.date, e.shift)] = e
    entries2 = list(dedup.values())

    counts: Dict[str, int] = {}
    for e in entries2:
        counts[e.shift] = counts.get(e.shift, 0) + 1

    return entries2, {"invalid_date": invalid_date, "out_of_month": out_of_month, "counts": counts}


def page_doctor():
    st.subheader("Indisponibilità (Medico)")

    st.markdown(
        """
**Fasce disponibili (come nel progetto originale):**
- **Mattina**
- **Pomeriggio**
- **Notte**
- **Diurno** (intera giornata diurna)
- **Tutto il giorno**
        """.strip()
    )

    auth = _load_auth_config()
    doctor_list = list(ALL_DOCTORS) + [TEST_USER]

    doctor = st.selectbox("Seleziona medico", doctor_list)
    pin = st.text_input("PIN", type="password")

    if not pin:
        st.info("Inserisci il PIN per accedere.")
        return

    if not doctor_login(auth, doctor, pin):
        st.error("PIN errato.")
        return

    today = dt.date.today()
    col1, col2 = st.columns(2)
    with col1:
        year = int(st.number_input("Anno", min_value=2020, max_value=2100, value=today.year, step=1))
    with col2:
        month = int(st.number_input("Mese", min_value=1, max_value=12, value=today.month, step=1))

    first_day, last_day = _month_bounds(year, month)

    try:
        target = _load_github_target()
        token = str(st.secrets.get("GITHUB_TOKEN", ""))
        all_unavail, _sha = load_all(token, target)
    except Exception as e:
        st.error(f"Errore storage GitHub: {e}")
        return

    existing = get_doctor_month(all_unavail, doctor, year, month)
    init_rows = [{"Data": e.date, "Fascia": e.shift, "Note": e.note} for e in existing]
    if not init_rows:
        init_rows = [{"Data": first_day, "Fascia": "Mattina", "Note": ""}]

    rows_key = f"unav_rows__{doctor}__{year}__{month}"
    if rows_key not in st.session_state:
        # persist UI rows with stable ids (per editing session)
        st.session_state[rows_key] = [
            {"id": f"{doctor}-{year}-{month}-{i}", **r} for i, r in enumerate(init_rows)
        ]

    st.divider()
    st.caption("Inserisci righe con **Data + Fascia**. Righe vuote vengono ignorate. Duplicati (Data+Fascia) vengono unificati.")

    cA, cB, _cC = st.columns([1, 1, 6])
    with cA:
        add_row = st.button("➕ Aggiungi riga", use_container_width=True)
    with cB:
        clean_rows = st.button("🧹 Pulisci righe vuote", use_container_width=True)

    rows = list(st.session_state.get(rows_key) or [])

    if add_row:
        rows.append({"id": f"{doctor}-{year}-{month}-{len(rows)}-{dt.datetime.utcnow().timestamp()}", "Data": first_day, "Fascia": "Mattina", "Note": ""})
        st.session_state[rows_key] = rows
        st.rerun()

    if clean_rows:
        def _is_empty(x: dict) -> bool:
            d = x.get("Data")
            sh = str(x.get("Fascia") or "").strip()
            note = str(x.get("Note") or "").strip()
            return (not d) and (not sh) and (not note)

        rows = [r for r in rows if not _is_empty(r)]
        if not rows:
            rows = [{"id": f"{doctor}-{year}-{month}-0", "Data": first_day, "Fascia": "Mattina", "Note": ""}]
        st.session_state[rows_key] = rows
        st.rerun()

    # Header
    h1, h2, h3, h4 = st.columns([2, 2, 6, 1])
    h1.markdown("**Data**")
    h2.markdown("**Fascia**")
    h3.markdown("**Note**")
    h4.markdown("**Rimuovi**")

    remove_ids = []
    new_rows = []

    for r in rows:
        rid = str(r.get("id") or f"{dt.datetime.utcnow().timestamp()}")
        d_key = f"{rows_key}__d__{rid}"
        s_key = f"{rows_key}__s__{rid}"
        n_key = f"{rows_key}__n__{rid}"
        rm_key = f"{rows_key}__rm__{rid}"

        if d_key not in st.session_state:
            st.session_state[d_key] = r.get("Data") or first_day
        if s_key not in st.session_state:
            st.session_state[s_key] = r.get("Fascia") or "Mattina"
        if n_key not in st.session_state:
            st.session_state[n_key] = r.get("Note", "")

        c1, c2, c3, c4 = st.columns([2, 2, 6, 1])
        with c1:
            d_val = st.date_input("Data", key=d_key, min_value=first_day, max_value=last_day, label_visibility="collapsed")
        with c2:
            sh_val = st.selectbox("Fascia", options=VALID_SHIFTS, key=s_key, label_visibility="collapsed")
        with c3:
            note_val = st.text_input("Note", key=n_key, label_visibility="collapsed")
        with c4:
            if st.button("🗑️", key=rm_key, help="Rimuovi questa riga"):
                remove_ids.append(rid)

        new_rows.append({"id": rid, "Data": d_val, "Fascia": sh_val, "Note": note_val})

    if remove_ids:
        new_rows = [r for r in new_rows if str(r.get("id")) not in set(remove_ids)]
        if not new_rows:
            new_rows = [{"id": f"{doctor}-{year}-{month}-0", "Data": first_day, "Fascia": "Mattina", "Note": ""}]
        st.session_state[rows_key] = new_rows
        st.rerun()

    st.session_state[rows_key] = new_rows

    # Normalize + counts
    entries_norm, info = _normalize_rows_to_entries(new_rows, year, month)
    counts = info.get("counts", {}) or {}
    if info.get("out_of_month"):
        st.warning(f"⚠️ {info['out_of_month']} righe con data fuori mese sono state ignorate (devono essere in {year}-{month:02d}).")
    if info.get("invalid_date"):
        st.warning(f"⚠️ {info['invalid_date']} righe con data non valida sono state ignorate.")

    st.caption(
        "Conteggi mese (per fascia): "
        + ", ".join([f"{sh}: {counts.get(sh, 0)}" for sh in VALID_SHIFTS])
    )

    st.divider()
    if st.button("Salva indisponibilità", type="primary"):
        try:
            save_doctor_month(
                token=str(st.secrets.get("GITHUB_TOKEN", "")),
                target=target,
                doctor=doctor,
                year=year,
                month=month,
                entries_in_month=entries_norm,
            )
            st.success("Salvato ✅")
        except Exception as e:
            st.error(f"Salvataggio fallito: {e}")


def page_admin():
    st.subheader("Genera turni (Admin)")

    auth = _load_auth_config()

    # Se ADMIN_PIN non è configurato, è più chiaro dirlo esplicitamente
    if not auth.admin_pin:
        st.error("ADMIN_PIN non configurato nei secrets di Streamlit. Imposta `ADMIN_PIN = \"....\"`.")
        st.stop()

    st.caption(f"Admin PIN configurato: {'✅ Sì' if auth.admin_pin else '❌ No'}")

    pin = st.text_input("Admin PIN", type="password")
    if not pin:
        st.info("Inserisci l'Admin PIN per generare.")
        return
    if not admin_login(auth, pin):
        st.error("Admin PIN errato.")
        return

    today = dt.date.today()
    col1, col2 = st.columns(2)
    with col1:
        year = int(st.number_input("Anno", min_value=2020, max_value=2100, value=today.year, step=1, key="admin_year"))
    with col2:
        month = int(st.number_input("Mese", min_value=1, max_value=12, value=today.month, step=1, key="admin_month"))

    time_limit = int(st.slider("Tempo massimo solver (secondi)", min_value=2, max_value=60, value=10, step=1))

    st.markdown(
        """
### Interpretazione fasce → colonne
- **M = Emodinamica Mattina** → blocca indisponibilità: **Mattina, Diurno, Tutto il giorno**
- **N = Emodinamica Pomeriggio** → blocca indisponibilità: **Pomeriggio, Diurno, Tutto il giorno**
- **O = Emodinamica Notte (Reperibilità)** → blocca indisponibilità: **Notte, Tutto il giorno**
  - (Indisponibilità *mattina/pomeriggio/diurno* NON bloccano la notte)
- **P = Ambulatorio (Mattina)** → come **M**
        """.strip()
    )

    if st.button("Genera", type="primary"):
        try:
            target = _load_github_target()
            token = str(st.secrets.get("GITHUB_TOKEN", ""))
            all_unavail, _sha = load_all(token, target)
        except Exception as e:
            st.error(f"Errore storage GitHub: {e}")
            return

        res = solve_month(year, month, all_unavail, time_limit_seconds=time_limit)
        if not res.ok:
            st.error("Impossibile generare il mese con i vincoli attuali.")
            st.code(res.log)
            return

        style_path = Path(__file__).resolve().parent / "Style_Template.xlsx"
        wb = create_month_workbook_from_style(style_path, year, month)
        ws = wb.active
        write_assignments(ws, res.assignment)

        out = BytesIO()
        wb.save(out)
        out.seek(0)

        filename = f"TURNI_{year}_{month:02d}.xlsx"
        st.download_button("Scarica Excel", data=out.getvalue(), file_name=filename)


def main():
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    st.title(APP_TITLE)
    mode = st.sidebar.radio("Sezione", ["Indisponibilità (Medico)", "Genera turni (Admin)"], index=0)

    try:
        if mode == "Indisponibilità (Medico)":
            page_doctor()
        else:
            page_admin()
    except GitHubStoreError as e:
        st.error(str(e))


if __name__ == "__main__":
    main()
