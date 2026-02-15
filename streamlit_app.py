from __future__ import annotations

import datetime as dt
from io import BytesIO
from pathlib import Path
from typing import Dict, Set

import streamlit as st

from app.config import ALL_DOCTORS, TEST_USER
from app.auth import AuthConfig, admin_login, doctor_login
from app.storage_github import GitHubTarget, GitHubStoreError
from app.unavailability import load_all, get_doctor_month, save_doctor_month
from app.scheduler import solve_month
from app.excel_io import create_month_workbook_from_style, write_assignments


APP_TITLE = "Turni Emodinamica – M/N/O/P + J"


def _load_auth_config() -> AuthConfig:
    doctor_pins = dict(st.secrets.get("DOCTOR_PINS", {}))
    admin_pin = str(st.secrets.get("ADMIN_PIN", ""))
    return AuthConfig(doctor_pins=doctor_pins, admin_pin=admin_pin)


def _load_github_target() -> GitHubTarget:
    token = str(st.secrets.get("GITHUB_TOKEN", ""))
    repo = str(st.secrets.get("GITHUB_REPO", ""))
    branch = str(st.secrets.get("GITHUB_BRANCH", "main"))
    path = str(st.secrets.get("GITHUB_DATA_PATH", "data/unavailability.json"))
    if not token or not repo:
        raise GitHubStoreError("Missing GitHub secrets: set GITHUB_TOKEN and GITHUB_REPO in Streamlit secrets.")
    return GitHubTarget(repo=repo, branch=branch, path=path)


def _dates_in_month(year: int, month: int):
    import calendar
    last = calendar.monthrange(year, month)[1]
    return [dt.date(year, month, d) for d in range(1, last + 1)]


def _fmt_date(d: dt.date) -> str:
    return d.strftime("%d/%m/%Y")


def page_doctor():
    st.subheader("Indisponibilità (Medico)")

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
        year = st.number_input("Anno", min_value=2020, max_value=2100, value=today.year, step=1)
    with col2:
        month = st.number_input("Mese", min_value=1, max_value=12, value=today.month, step=1)

    try:
        target = _load_github_target()
        token = str(st.secrets.get("GITHUB_TOKEN", ""))
        all_unavail, _sha = load_all(token, target)
    except Exception as e:
        st.error(f"Errore storage GitHub: {e}")
        return

    current = sorted(get_doctor_month(all_unavail, doctor, int(year), int(month)))
    st.write("Date attualmente impostate come indisponibili:", ", ".join(_fmt_date(d) for d in current) if current else "—")

    options = _dates_in_month(int(year), int(month))
    selected = st.multiselect(
        "Seleziona le date NON disponibili (giorno intero)",
        options=options,
        default=current,
        format_func=_fmt_date,
    )

    if st.button("Salva indisponibilità", type="primary"):
        try:
            save_doctor_month(
                token=str(st.secrets.get("GITHUB_TOKEN", "")),
                target=target,
                doctor=doctor,
                year=int(year),
                month=int(month),
                unavailable_dates_in_month=set(selected),
            )
            st.success("Salvato ✅")
        except Exception as e:
            st.error(f"Salvataggio fallito: {e}")


def page_admin():
    st.subheader("Genera turni (Admin)")

    auth = _load_auth_config()
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
        year = st.number_input("Anno", min_value=2020, max_value=2100, value=today.year, step=1, key="admin_year")
    with col2:
        month = st.number_input("Mese", min_value=1, max_value=12, value=today.month, step=1, key="admin_month")

    time_limit = st.slider("Tempo massimo solver (secondi)", min_value=2, max_value=60, value=10, step=1)

    if st.button("Genera", type="primary"):
        # Load unavailability
        try:
            target = _load_github_target()
            token = str(st.secrets.get("GITHUB_TOKEN", ""))
            all_unavail, _sha = load_all(token, target)
        except Exception as e:
            st.error(f"Errore storage GitHub: {e}")
            return

        # Solve
        res = solve_month(int(year), int(month), all_unavail, time_limit_seconds=int(time_limit))
        if not res.ok:
            st.error("Impossibile generare il mese con i vincoli attuali.")
            st.code(res.log)
            return

        # Build Excel
        style_path = Path(__file__).resolve().parent / "Style_Template.xlsx"
        wb = create_month_workbook_from_style(style_path, int(year), int(month))
        ws = wb.active
        write_assignments(ws, res.assignment)

        out = BytesIO()
        wb.save(out)
        out.seek(0)

        filename = f"TURNI_{int(year)}_{int(month):02d}.xlsx"
        st.download_button(
            "Scarica Excel",
            data=out.getvalue(),
            file_name=filename,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        st.text_area("Log / statistiche", value=res.log, height=260)


def main():
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    st.title(APP_TITLE)

    st.caption("Progetto riscritto da zero. Indisponibilità salvate su GitHub (JSON).")

    mode = st.sidebar.radio("Sezione", ["Indisponibilità (Medico)", "Genera turni (Admin)"])

    # Quick config check
    with st.sidebar.expander("Stato configurazione", expanded=False):
        missing = []
        for key in ["GITHUB_TOKEN", "GITHUB_REPO", "ADMIN_PIN"]:
            if not st.secrets.get(key, ""):
                missing.append(key)
        if missing:
            st.warning("Secrets mancanti: " + ", ".join(missing))
        else:
            st.success("Secrets OK")

    if mode == "Indisponibilità (Medico)":
        page_doctor()
    else:
        page_admin()


if __name__ == "__main__":
    main()
