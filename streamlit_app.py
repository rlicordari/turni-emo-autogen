from __future__ import annotations

import calendar
import datetime as dt
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd
import streamlit as st

from app.auth import AuthConfig, admin_login, doctor_login
from app.config import ALL_DOCTORS, TEST_USER
from app.excel_io import create_month_workbook_from_style, write_assignments
from app.scheduler import solve_month
from app.storage_github import GitHubTarget, GitHubStoreError
from app.unavailability import (
    VALID_SHIFTS,
    UnavailabilityEntry,
    get_doctor_month,
    load_all,
    save_doctor_months,
)
from app.admin_support import (
    DEFAULT_SETTINGS,
    append_audit_csv,
    audit_path_for_month,
    load_app_settings,
    load_audit_csv,
    save_app_settings,
)

APP_TITLE = "Turni Emodinamica – M/N/O/P + J"


# ----------------------------
# Secrets + config helpers
# ----------------------------

def _load_auth_config() -> AuthConfig:
    doctor_pins = dict(st.secrets.get("DOCTOR_PINS", {}))

    admin_pin = (
        st.secrets.get("ADMIN_PIN")
        or st.secrets.get("admin_pin")
        or (st.secrets.get("ADMIN", {}) or {}).get("PIN")
        or (st.secrets.get("ADMIN", {}) or {}).get("pin")
        or ""
    )
    return AuthConfig(doctor_pins=doctor_pins, admin_pin=str(admin_pin))


def _github_cfg() -> tuple[str, str, str, str, str]:
    token = str(st.secrets.get("GITHUB_TOKEN", ""))
    repo = str(st.secrets.get("GITHUB_REPO", ""))
    branch = str(st.secrets.get("GITHUB_BRANCH", "main"))
    data_path = str(st.secrets.get("GITHUB_DATA_PATH", "data/unavailability.json"))

    # Optional extra paths
    settings_path = str(st.secrets.get("GITHUB_SETTINGS_PATH", "data/app_settings.json"))
    audit_dir = str(st.secrets.get("GITHUB_AUDIT_DIR", "data/audit"))

    if not token or not repo:
        raise GitHubStoreError("Missing GitHub secrets: set GITHUB_TOKEN and GITHUB_REPO in Streamlit secrets.")
    return token, repo, branch, data_path, settings_path, audit_dir


def _target(repo: str, branch: str, path: str) -> GitHubTarget:
    return GitHubTarget(repo=repo, branch=branch, path=path)


def _month_bounds(year: int, month: int) -> tuple[dt.date, dt.date]:
    first = dt.date(year, month, 1)
    last = dt.date(year, month, calendar.monthrange(year, month)[1])
    return first, last


def _next_month(today: dt.date) -> tuple[int, int]:
    first_this = today.replace(day=1)
    first_next = (first_this + dt.timedelta(days=32)).replace(day=1)
    return first_next.year, first_next.month


# ----------------------------
# Unavailability editor helpers
# ----------------------------

def _normalize_rows_to_entries(rows: List[dict], year: int, month: int) -> tuple[List[UnavailabilityEntry], Dict[str, int], Dict[str, int]]:
    """Normalizza righe UI -> entries. Deduplica per (date, shift)."""
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

    # dedup per (date, shift)
    dedup: Dict[Tuple[dt.date, str], UnavailabilityEntry] = {}
    for e in entries:
        dedup[(e.date, e.shift)] = e

    out = list(dedup.values())

    counts: Dict[str, int] = {}
    for e in out:
        counts[e.shift] = counts.get(e.shift, 0) + 1

    info = {"invalid_date": invalid_date, "out_of_month": out_of_month}
    return out, counts, info


def _render_month_editor(
    *,
    rows_key: str,
    first_day: dt.date,
    last_day: dt.date,
    editable: bool,
) -> List[dict]:
    """UI a righe: Data + Fascia + Note + rimuovi."""
    if rows_key not in st.session_state:
        st.session_state[rows_key] = [{"id": "0", "Data": first_day, "Fascia": "Mattina", "Note": ""}]

    rows = list(st.session_state.get(rows_key) or [])

    if editable:
        cA, cB, _cC = st.columns([1, 1.2, 6])
        with cA:
            add_row = st.button("➕ Aggiungi riga", key=f"{rows_key}__add", use_container_width=True)
        with cB:
            clean_rows = st.button("🧹 Pulisci righe vuote", key=f"{rows_key}__clean", use_container_width=True)

        if add_row:
            rows.append({"id": str(dt.datetime.utcnow().timestamp()), "Data": first_day, "Fascia": "Mattina", "Note": ""})
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
                rows = [{"id": "0", "Data": first_day, "Fascia": "Mattina", "Note": ""}]
            st.session_state[rows_key] = rows
            st.rerun()

    # Header
    h1, h2, h3, h4 = st.columns([2, 2, 6, 1])
    h1.markdown("**Data**")
    h2.markdown("**Fascia**")
    h3.markdown("**Note**")
    h4.markdown("**Rimuovi**")

    remove_ids: List[str] = []
    new_rows: List[dict] = []

    for r in rows:
        rid = str(r.get("id") or str(dt.datetime.utcnow().timestamp()))
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
            d_val = st.date_input(
                "Data",
                key=d_key,
                min_value=first_day,
                max_value=last_day,
                label_visibility="collapsed",
                disabled=not editable,
            )
        with c2:
            sh_val = st.selectbox(
                "Fascia",
                options=VALID_SHIFTS,
                key=s_key,
                label_visibility="collapsed",
                disabled=not editable,
            )
        with c3:
            note_val = st.text_input(
                "Note",
                key=n_key,
                label_visibility="collapsed",
                disabled=not editable,
            )
        with c4:
            if editable:
                if st.button("🗑️", key=rm_key, help="Rimuovi questa riga"):
                    remove_ids.append(rid)
            else:
                st.write("")

        # safety bounds
        if isinstance(d_val, dt.date):
            if d_val < first_day:
                d_val = first_day
                st.session_state[d_key] = d_val
            if d_val > last_day:
                d_val = last_day
                st.session_state[d_key] = d_val

        new_rows.append({"id": rid, "Data": d_val, "Fascia": sh_val, "Note": note_val})

    if editable and remove_ids:
        new_rows = [r for r in new_rows if str(r.get("id")) not in set(remove_ids)]
        if not new_rows:
            new_rows = [{"id": "0", "Data": first_day, "Fascia": "Mattina", "Note": ""}]
        st.session_state[rows_key] = new_rows
        st.rerun()

    st.session_state[rows_key] = new_rows
    return new_rows


# ----------------------------
# Manual unavailability upload (Admin)
# ----------------------------

def _parse_unavailability_upload(upload) -> Dict[str, List[UnavailabilityEntry]]:
    """Parsa un file caricato (xlsx/csv/tsv) con colonne: doctor|medico, date|data, shift|fascia, note."""
    if upload is None:
        return {}

    name = (upload.name or "").lower()
    if name.endswith(".xlsx") or name.endswith(".xls"):
        df = pd.read_excel(upload)
    else:
        # tenta separatori comuni
        raw = upload.getvalue().decode("utf-8", errors="replace")
        if "\t" in raw and "," not in raw:
            df = pd.read_csv(BytesIO(upload.getvalue()), sep="\t")
        else:
            df = pd.read_csv(BytesIO(upload.getvalue()))

    if df is None or df.empty:
        return {}

    cols = {c.lower().strip(): c for c in df.columns}

    def pick(*cands: str) -> str | None:
        for c in cands:
            if c in cols:
                return cols[c]
        return None

    c_doc = pick("doctor", "medico", "nome")
    c_date = pick("date", "data")
    c_shift = pick("shift", "fascia")
    c_note = pick("note", "nota", "commento")

    if not (c_doc and c_date and c_shift):
        raise ValueError("File indisponibilità: mancano colonne obbligatorie. Servono: doctor/medico, date/data, shift/fascia.")

    out: Dict[str, List[UnavailabilityEntry]] = {}
    for _, row in df.iterrows():
        doc = str(row.get(c_doc) or "").strip()
        if not doc:
            continue
        ds = row.get(c_date)
        d: dt.date | None = None
        if isinstance(ds, dt.datetime):
            d = ds.date()
        elif isinstance(ds, dt.date):
            d = ds
        else:
            try:
                d = dt.date.fromisoformat(str(ds)[:10])
            except Exception:
                continue
        sh = str(row.get(c_shift) or "").strip()
        if sh not in set(VALID_SHIFTS):
            # accettiamo alias semplici
            low = sh.lower()
            alias = {
                "mattina": "Mattina",
                "pomeriggio": "Pomeriggio",
                "notte": "Notte",
                "diurno": "Diurno",
                "tutto il giorno": "Tutto il giorno",
            }
            sh = alias.get(low, sh)
        if sh not in set(VALID_SHIFTS):
            continue
        note = str(row.get(c_note) or "") if c_note else ""
        out.setdefault(doc, []).append(UnavailabilityEntry(date=d, shift=sh, note=note, updated_at=""))

    return out


# ----------------------------
# Pages
# ----------------------------

def page_doctor():
    st.subheader("Indisponibilità (Medico)")

    auth = _load_auth_config()
    doctor_list = list(ALL_DOCTORS) + [TEST_USER]

    # Persist auth per sessione (come nello spunto)
    st.session_state.setdefault("doctor_auth_ok", False)
    st.session_state.setdefault("doctor_name", "")

    if not st.session_state.get("doctor_auth_ok"):
        with st.form("doctor_login"):
            doctor = st.selectbox("Seleziona medico", doctor_list)
            pin = st.text_input("PIN", type="password")
            ok = st.form_submit_button("Accedi", type="primary")
        if not ok:
            st.info("Inserisci nome e PIN per accedere.")
            return
        if not doctor_login(auth, doctor, pin):
            st.error("PIN errato.")
            return
        st.session_state.doctor_auth_ok = True
        st.session_state.doctor_name = doctor
        st.rerun()

    doctor = str(st.session_state.get("doctor_name") or "")

    c_logout, c_state = st.columns([1, 3])
    with c_logout:
        if st.button("Esci (Medico)"):
            st.session_state.doctor_auth_ok = False
            st.session_state.doctor_name = ""
            st.rerun()
    with c_state:
        st.success(f"Accesso OK ✅ ({doctor})")

    token, repo, branch, data_path, settings_path, audit_dir = _github_cfg()

    # settings (open/closed + max per shift)
    try:
        app_settings, _settings_sha = load_app_settings(token, repo, branch, settings_path)
    except Exception as e:
        app_settings, _settings_sha = dict(DEFAULT_SETTINGS), None
        st.warning(f"Impostazioni indisponibilità non leggibili (uso default): {e}")

    unav_open = bool(app_settings.get("unavailability_open", True))
    max_per_shift = int(app_settings.get("max_unavailability_per_shift", 31) or 31)
    if max_per_shift < 0:
        max_per_shift = 0

    if not unav_open:
        st.warning("🔒 Inserimento indisponibilità temporaneamente **chiuso** dall'amministratore. Puoi solo visualizzare.")

    st.caption(
        f"Limite per medico: **max {max_per_shift}** inserimenti per fascia (per mese). "
        "Fasce: Mattina / Pomeriggio / Notte / Diurno / Tutto il giorno."
    )

    # Selezione mesi (come nello spunto)
    today = dt.date.today()
    def_y, def_m = _next_month(today)

    st.session_state.setdefault("doctor_year_sel", def_y)
    st.session_state.setdefault("doctor_month_sel", def_m)
    st.session_state.setdefault("doctor_selected_months", [(def_y, def_m)])

    years = list(range(today.year, today.year + 21))

    st.markdown("### 1) Seleziona mese/i da compilare")
    c1, c2, c3, c4 = st.columns([1, 1.4, 1, 1])
    with c1:
        yy_sel = st.selectbox("Anno", years, key="doctor_year_sel")
    with c2:
        mm_sel = st.selectbox(
            "Mese",
            list(range(1, 13)),
            format_func=lambda m: f"{m:02d} - {calendar.month_name[m]}",
            key="doctor_month_sel",
        )
    with c3:
        add_month = st.button("Aggiungi", use_container_width=True)
    with c4:
        remove_month = st.button("Rimuovi", use_container_width=True)

    cur = (int(yy_sel), int(mm_sel))
    sel_set = set(st.session_state.get("doctor_selected_months") or [])
    if add_month:
        sel_set.add(cur)
    if remove_month:
        sel_set.discard(cur)

    selected = sorted(sel_set)
    st.session_state.doctor_selected_months = selected
    if not selected:
        st.info("Aggiungi almeno un mese per iniziare.")
        return

    st.caption("Mesi selezionati: " + ", ".join([f"{yy}-{mm:02d}" for (yy, mm) in selected]))

    # Refresh baseline
    cR1, _cR2 = st.columns([1, 3])
    with cR1:
        refresh = st.button("🔄 Ricarica dati", help="Ricarica l'archivio dal server.")

    # load all unavailability
    try:
        tgt = _target(repo, branch, data_path)
        all_unavail, _sha = load_all(token, tgt)
    except Exception as e:
        st.error(f"Errore accesso archivio indisponibilità: {e}")
        return

    if refresh:
        # Clear cached month editors for selected months
        for (yy, mm) in selected:
            rows_key = f"unav_rows_{doctor}_{yy}_{mm}"
            for k in list(st.session_state.keys()):
                if str(k).startswith(rows_key):
                    st.session_state.pop(k, None)
        st.rerun()

    st.divider()

    # Tabs for months
    tabs = st.tabs([f"{yy}-{mm:02d}" for (yy, mm) in selected])

    updates: Dict[Tuple[int, int], List[UnavailabilityEntry]] = {}
    violations: Dict[Tuple[int, int], Dict[str, int]] = {}

    for (yy, mm), tab in zip(selected, tabs):
        with tab:
            first_day, last_day = _month_bounds(yy, mm)
            existing = get_doctor_month(all_unavail, doctor, yy, mm)
            init = [{"id": f"{i}", "Data": e.date, "Fascia": e.shift, "Note": e.note} for i, e in enumerate(existing)]
            if not init:
                init = [{"id": "0", "Data": first_day, "Fascia": "Mattina", "Note": ""}]

            rows_key = f"unav_rows_{doctor}_{yy}_{mm}"
            if rows_key not in st.session_state:
                st.session_state[rows_key] = init

            st.caption("Inserisci righe con Data + Fascia. Duplicati (Data+Fascia) vengono unificati.")

            rows = _render_month_editor(rows_key=rows_key, first_day=first_day, last_day=last_day, editable=bool(unav_open))

            entries_norm, counts, info = _normalize_rows_to_entries(rows, yy, mm)
            updates[(yy, mm)] = entries_norm

            if info.get("out_of_month"):
                st.warning(f"⚠️ {info['out_of_month']} righe fuori mese ignorate (devono essere in {yy}-{mm:02d}).")
            if info.get("invalid_date"):
                st.warning(f"⚠️ {info['invalid_date']} righe con data non valida ignorate.")

            st.caption("Conteggi mese (per fascia): " + ", ".join([f"{sh} {counts.get(sh, 0)}/{max_per_shift}" for sh in VALID_SHIFTS]))

            over = {sh: n for sh, n in (counts or {}).items() if n > max_per_shift}
            violations[(yy, mm)] = over
            if over:
                pretty = ", ".join([f"{sh}: {n}/{max_per_shift}" for sh, n in over.items()])
                st.error(f"Limite superato → {pretty}. Rimuovi righe prima di salvare.")

    any_over = any(bool(v) for v in (violations or {}).values())
    can_save = bool(unav_open) and (not any_over)

    st.divider()
    cS1, cS2 = st.columns([1, 3])
    with cS1:
        save = st.button("Salva indisponibilità", type="primary", disabled=not can_save)
    with cS2:
        if any_over:
            st.caption("Salvataggio disabilitato: limite superato in almeno un mese.")
        if not unav_open:
            st.caption("Salvataggio disabilitato: inserimento chiuso dall'amministratore.")

    if save:
        try:
            save_doctor_months(token=token, target=_target(repo, branch, data_path), doctor=doctor, updates=updates)

            # Audit log per ogni mese salvato
            for (yy, mm), entries_norm in updates.items():
                counts: Dict[str, int] = {}
                for e in entries_norm:
                    counts[e.shift] = counts.get(e.shift, 0) + 1
                mk = f"{yy}-{mm:02d}"
                line = f"{dt.datetime.utcnow().replace(microsecond=0).isoformat()}Z,{doctor},{mk},\"{counts}\""
                ap = audit_path_for_month(audit_dir, yy, mm)
                append_audit_csv(token, repo, branch, ap, line)

            st.success("Salvato ✅")
        except Exception as e:
            st.error(f"Salvataggio fallito: {e}")


def page_admin():
    st.subheader("Generazione turni (Admin)")

    auth = _load_auth_config()
    if not auth.admin_pin:
        st.error("ADMIN_PIN non configurato nei secrets di Streamlit. Imposta ADMIN_PIN = \"....\".")
        st.stop()

    # Persist admin auth
    st.session_state.setdefault("admin_auth_ok", False)

    if not st.session_state.admin_auth_ok:
        with st.form("admin_login"):
            pin = st.text_input("PIN Admin", type="password")
            ok = st.form_submit_button("Sblocca area Admin", type="primary")
        if not ok:
            st.stop()
        if not admin_login(auth, pin):
            st.error("PIN Admin errato.")
            st.stop()
        st.session_state.admin_auth_ok = True
        st.rerun()

    col_logout, col_status = st.columns([1, 3])
    with col_logout:
        if st.button("Esci (Admin)"):
            st.session_state.admin_auth_ok = False
            st.rerun()
    with col_status:
        st.success("Area Admin sbloccata ✅")

    token, repo, branch, data_path, settings_path, audit_dir = _github_cfg()

    # --- Settings (open/close + limits) ---
    with st.expander("⚙️ Impostazioni indisponibilità (Admin)", expanded=True):
        try:
            app_settings, settings_sha = load_app_settings(token, repo, branch, settings_path)
        except Exception as e:
            app_settings, settings_sha = dict(DEFAULT_SETTINGS), None
            st.warning(f"Impossibile leggere impostazioni da GitHub (uso default): {e}")

        cur_open = bool(app_settings.get("unavailability_open", True))
        cur_max = int(app_settings.get("max_unavailability_per_shift", 31) or 31)

        cS1, cS2, cS3 = st.columns([1.4, 1, 2])
        with cS1:
            new_open = st.toggle(
                "Consenti ai medici di inserire/modificare indisponibilità",
                value=cur_open,
                help="Se disattivato, i medici possono solo visualizzare le proprie indisponibilità.",
            )
        with cS2:
            new_max = st.number_input(
                "Max per fascia (per mese)",
                min_value=0,
                max_value=31,
                value=int(cur_max),
                step=1,
            )
        with cS3:
            meta = ""
            if app_settings.get("updated_at"):
                meta += f"Ultimo aggiornamento: {app_settings.get('updated_at')}"
            if app_settings.get("updated_by"):
                meta += f" | da: {app_settings.get('updated_by')}"
            if meta:
                st.caption(meta)

        if st.button("Salva impostazioni indisponibilità", type="primary"):
            try:
                save_app_settings(
                    token=token,
                    repo=repo,
                    branch=branch,
                    settings_path=settings_path,
                    settings={"unavailability_open": bool(new_open), "max_unavailability_per_shift": int(new_max)},
                    sha=settings_sha,
                    updated_by="admin",
                )
                st.success("Impostazioni salvate ✅")
                st.rerun()
            except Exception as e:
                st.error(f"Errore salvataggio impostazioni: {e}")

    st.divider()

    # Step 1: Periodo
    st.markdown("### 1) Periodo")
    today = dt.date.today()
    cA, cB, _cC = st.columns([1, 1, 2])
    with cA:
        year = int(st.number_input("Anno", min_value=2025, max_value=2035, value=today.year, step=1, key="admin_year"))
    with cB:
        month = int(st.number_input("Mese", min_value=1, max_value=12, value=today.month, step=1, key="admin_month"))
    mk = f"{year}-{month:02d}"
    st.caption(f"Stai generando: **{mk}**")

    # Step 2: Indisponibilità
    st.markdown("### 2) Indisponibilità")
    unav_mode = st.radio(
        "Fonte indisponibilità",
        ["Nessuna", "Carica file manuale", "Usa archivio (privacy)"],
        horizontal=True,
        help="Puoi caricare un file manuale, oppure usare l’archivio compilato dai medici.",
    )

    upload = None
    if unav_mode == "Carica file manuale":
        upload = st.file_uploader("Carica indisponibilità (xlsx/csv/tsv)", type=["xlsx", "csv", "tsv"])

    # Audit expander
    with st.expander("📜 Log inserimenti/modifiche indisponibilità (Audit)", expanded=False):
        st.caption("Mostra e scarica il log (per mese) dei salvataggi delle indisponibilità.")
        cL1, cL2 = st.columns([1, 1])
        with cL1:
            ay = int(st.number_input("Anno log", min_value=2025, max_value=2035, value=year, step=1, key="audit_year"))
        with cL2:
            am = int(st.number_input("Mese log", min_value=1, max_value=12, value=month, step=1, key="audit_month"))
        ap = audit_path_for_month(audit_dir, ay, am)
        try:
            txt = load_audit_csv(token, repo, branch, ap)
        except Exception as e:
            txt = ""
            st.error(f"Errore lettura audit log: {e}")

        if not txt.strip():
            st.info("Nessun audit log trovato per questo mese.")
        else:
            st.download_button(
                "⬇️ Scarica audit log (CSV)",
                data=txt.encode("utf-8"),
                file_name=f"unavailability_audit_{ay}-{am:02d}.csv",
                mime="text/csv",
            )
            try:
                df = pd.read_csv(BytesIO(txt.encode("utf-8")))
                st.dataframe(df.tail(200), use_container_width=True, hide_index=True)
                st.caption("Mostro al massimo 200 righe (le più recenti).")
            except Exception:
                st.text_area("Audit (raw)", value=txt, height=220)

    # Step 3: Avanzate
    with st.expander("⚙️ Avanzate (Template/Style)", expanded=False):
        st.caption("Di default uso lo Style_Template.xlsx incluso nella repo. Puoi caricarne uno diverso solo se serve.")
        style_upload = st.file_uploader("Carica Style_Template.xlsx (opzionale)", type=["xlsx"], key="style_up")
        sheet_name = st.text_input("Nome foglio (opzionale)", value="")

    st.divider()

    # Generate button
    generate = st.button("🚀 Genera turni", type="primary")

    if generate:
        status = st.status("Preparazione…", expanded=True)
        try:
            status.update(label="Carico indisponibilità…", state="running")

            all_unavail: Dict[str, List[UnavailabilityEntry]] = {}
            if unav_mode == "Nessuna":
                all_unavail = {}
            elif unav_mode == "Usa archivio (privacy)":
                tgt = _target(repo, branch, data_path)
                all_unavail, _sha = load_all(token, tgt)
            else:
                if upload is None:
                    raise ValueError("Hai selezionato 'Carica file manuale' ma non hai caricato alcun file.")
                all_unavail = _parse_unavailability_upload(upload)

            status.update(label="Generazione turni…", state="running")
            res = solve_month(year, month, all_unavail)
            if not res.ok:
                status.update(label="Impossibile generare ❌", state="error")
                st.error("Impossibile generare il mese con i vincoli attuali.")
                st.code(res.log)
                st.stop()

            status.update(label="Creo Excel…", state="running")

            if style_upload is not None:
                style_path = Path("/tmp/Style_Template.xlsx")
                style_path.write_bytes(style_upload.getvalue())
            else:
                style_path = Path(__file__).resolve().parent / "Style_Template.xlsx"

            wb = create_month_workbook_from_style(style_path, year, month)
            ws = wb.active
            # (sheet_name opzionale: create_month_workbook_from_style usa wb.active; qui manteniamo semplicità)
            write_assignments(ws, res.assignment)

            out = BytesIO()
            wb.save(out)
            out.seek(0)

            st.session_state["last_generated"] = {
                "mk": mk,
                "excel_bytes": out.getvalue(),
                "log": res.log,
                "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            }

            status.update(label="Completato ✅", state="complete")

        except Exception as e:
            status.update(label="Errore ❌", state="error")
            st.error(str(e))

    last = st.session_state.get("last_generated")
    if isinstance(last, dict) and last.get("mk") == mk and last.get("excel_bytes"):
        st.success(f"Creato ✅ | {last.get('generated_at','')}")
        st.download_button(
            "⬇️ Scarica Excel",
            data=bytes(last["excel_bytes"]),
            file_name=f"TURNI_{mk}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        with st.expander("📄 Solver log", expanded=False):
            st.text_area("Log", value=str(last.get("log") or ""), height=260)


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
