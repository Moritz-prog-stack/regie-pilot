import json
import os
import re
import time
from datetime import date

import streamlit as st
from fpdf import FPDF
from google import genai
from google.genai import errors, types

# ---------------------------------------------------------
# Einstellungen: Modell-Reihenfolge & automatische Wiederholung
# ---------------------------------------------------------
MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
]
RETRIES_PER_MODEL = 3
RETRY_CODES = {429, 500, 502, 503, 504}

GEWERKE = [
    "SHK (Sanitär, Heizung, Klima)",
    "Elektro",
    "Maler & Lackierer",
    "Dachdecker",
    "Tischler / Schreiner",
    "Trockenbau",
    "Fliesenleger",
    "Garten- & Landschaftsbau",
    "Allgemeines Handwerk / Bau",
]

# ---------------------------------------------------------
# Seiten-Konfiguration (MUSS der erste st.-Befehl sein!)
# ---------------------------------------------------------
st.set_page_config(
    page_title="Regie-Pilot",
    page_icon="🛠️",
    layout="centered",
)

# ---------------------------------------------------------
# Zugangsschutz (Firmen-Code)
# ---------------------------------------------------------
def check_access():
    # Master-Code: RP-7482-X (weitere Kunden-Codes über Streamlit Secrets)
    allowed_codes = st.secrets.get("ACCESS_CODES", {"RP-7482-X": "Regie-Pilot Admin"})

    if "authenticated_company" not in st.session_state:
        st.session_state.authenticated_company = None

    if st.session_state.authenticated_company is None:
        st.title("🔒 Regie-Pilot – Zugang")
        st.write("Bitte geben Sie Ihren persönlichen Firmen-Code ein, um fortzufahren.")

        code_input = st.text_input("Firmen-Code", type="password", placeholder="Firmen-Code eingeben")

        if st.button("Anmelden", use_container_width=True):
            cleaned_code = code_input.strip().upper()
            if cleaned_code in allowed_codes:
                st.session_state.authenticated_company = allowed_codes[cleaned_code]
                st.rerun()
            else:
                st.error("❌ Ungültiger Firmen-Code. Bitte prüfen Sie Ihre Eingabe.")

        st.stop()


check_access()

# ---------------------------------------------------------
# Hilfsfunktionen: Gemini API & PDF-Generierung
# ---------------------------------------------------------
def get_gemini_client():
    api_key = st.secrets.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        st.error("❌ Kein GEMINI_API_KEY gefunden. Bitte in den Streamlit Secrets hinterlegen.")
        st.stop()
    return genai.Client(api_key=api_key)


def clean_pdf_text(text: str) -> str:
    """Säubert Sonderzeichen, damit Standard-PDF-Schriften fehlerfrei rendern."""
    if not text:
        return "-"
    replacements = {
        "€": "EUR",
        "–": "-",
        "—": "-",
        "„": '"',
        "“": '"',
        "‚": "'",
        "’": "'",
        "•": "-",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text.encode("latin-1", "replace").decode("latin-1")


def analyze_audio_with_gemini(audio_bytes: bytes, mime_type: str, gewerk: str, extra_notes: str) -> dict:
    client = get_gemini_client()

    prompt = f"""
Du bist ein erfahrener deutscher Bauleiter und Handwerksmeister im Gewerk: {gewerk}.
Analysiere die folgende Sprachnachricht von der Baustelle (und eventuelle Zusatznotizen: "{extra_notes}").
Erstelle daraus einen professionellen, präzisen Regiebericht für den Kunden.

Antworte AUSSCHLIESSLICH mit einem validen JSON-Objekt (ohne Markdown-Codeblöcke) in exakt dieser Struktur:
{{
  "kunde": "Name des Kunden / Auftraggebers (falls genannt, sonst 'Nicht angegeben')",
  "baustelle": "Adresse oder Ort der Baustelle (falls genannt, sonst 'Nicht angegeben')",
  "ausgefuehrte_arbeiten": [
    "Sauber formulierter, fachgerechter Stichpunkt 1",
    "Sauber formulierter, fachgerechter Stichpunkt 2"
  ],
  "material": [
    "Menge + genaue Materialbezeichnung (falls genannt)"
  ],
  "arbeitszeit": "z. B. 2 Monteure à 3,5 Std. (falls genannt, sonst 'Nicht angegeben')",
  "besonderheiten": "Hinweise, Mängel, Absprachen mit dem Kunden oder offene Punkte (falls keine, 'Keine besonderen Vorkommnisse')"
}}
"""

    last_error = None
    for model_name in MODELS:
        for attempt in range(RETRIES_PER_MODEL):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=[
                        types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
                        prompt,
                    ],
                    config=types.GenerateContentConfig(
                        temperature=0.2,
                        response_mime_type="application/json",
                    ),
                )
                raw = response.text.strip()
                raw = re.sub(r"^```json\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
                return json.loads(raw)
            except errors.APIError as e:
                last_error = e
                if getattr(e, "code", None) in RETRY_CODES and attempt < RETRIES_PER_MODEL - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
            except Exception as e:
                last_error = e
                break

    raise RuntimeError(f"KI-Auswertung fehlgeschlagen: {last_error}")


def build_regiebericht_pdf(
    firma: str,
    gewerk: str,
    datum_str: str,
    daten: dict,
    unterzeichner_kunde: str = "",
    logo_bytes: bytes | None = None,
) -> bytes:
    pdf = FPDF()
    pdf.set_margins(15, 15, 15)
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()

    # Optionales Firmenlogo oben rechts
    if logo_bytes:
        tmp_logo = "/tmp/company_logo.png"
        with open(tmp_logo, "wb") as f:
            f.write(logo_bytes)
        try:
            pdf.image(tmp_logo, x=155, y=12, w=38)
        except Exception:
            pass

    # Kopfbereich
    pdf.set_x(pdf.l_margin)
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(pdf.epw, 10, clean_pdf_text(firma), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(pdf.epw, 6, clean_pdf_text(f"Gewerk: {gewerk}  |  Datum: {datum_str}"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(6)

    pdf.set_draw_color(40, 40, 40)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(6)

    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(pdf.epw, 9, "REGIEBERICHT / TAGESNACHWEIS", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    # Stammdaten
    label_w = 45
    val_w = pdf.epw - label_w

    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(label_w, 7, "Auftraggeber / Kunde:")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(val_w, 7, clean_pdf_text(daten.get("kunde", "-")), new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(label_w, 7, "Baustelle / Einsatzort:")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(val_w, 7, clean_pdf_text(daten.get("baustelle", "-")), new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(label_w, 7, "Arbeitszeit / Personal:")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(val_w, 7, clean_pdf_text(daten.get("arbeitszeit", "-")), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    # Ausgeführte Arbeiten
    pdf.set_x(pdf.l_margin)
    pdf.set_fill_color(240, 242, 246)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(pdf.epw, 8, "  Ausgefuehrte Arbeiten", new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 10)
    arbeiten = daten.get("ausgefuehrte_arbeiten") or ["Keine Angaben"]
    for item in arbeiten:
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(pdf.epw, 6, clean_pdf_text(f"- {item}"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Verwendetes Material
    pdf.set_x(pdf.l_margin)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(pdf.epw, 8, "  Verwendetes Material", new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 10)
    materialien = daten.get("material") or ["Kein Material angegeben"]
    for mat in materialien:
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(pdf.epw, 6, clean_pdf_text(f"- {mat}"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Besonderheiten / Hinweise
    pdf.set_x(pdf.l_margin)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(pdf.epw, 8, "  Besonderheiten / Absprachen", new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(pdf.epw, 6, clean_pdf_text(daten.get("besonderheiten", "-")), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(14)

    # Unterschriftsfelder
    pdf.set_x(pdf.l_margin)
    y_sig = pdf.get_y()
    pdf.line(15, y_sig, 90, y_sig)
    pdf.line(115, y_sig, 195, y_sig)
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(100, 5, clean_pdf_text(f"Datum / Unterschrift {firma}"))
    kunde_label = f"Unterschrift Auftraggeber ({unterzeichner_kunde})" if unterzeichner_kunde else "Unterschrift Auftraggeber / Bauleitung"
    pdf.cell(pdf.epw - 100, 5, clean_pdf_text(kunde_label), new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())


# ---------------------------------------------------------
# Hauptoberfläche der App
# ---------------------------------------------------------
col_head, col_logout = st.columns([4, 1])
with col_head:
    st.title("🛠️ Regie-Pilot")
    st.caption(f"Angemeldet als: **{st.session_state.authenticated_company}**")
with col_logout:
    st.write("")
    if st.button("Abmelden"):
        st.session_state.authenticated_company = None
        st.rerun()

st.divider()

with st.expander("⚙️ Firmendaten & Logo anpassen", expanded=False):
    firma_name = st.text_input("Firmenname im PDF", value=st.session_state.authenticated_company)
    gewerk = st.selectbox("Gewerk auswählen", GEWERKE, index=0)
    einsatz_datum = st.date_input("Einsatzdatum", value=date.today())
    logo_file = st.file_uploader("Firmenlogo hochladen (PNG / JPG, optional)", type=["png", "jpg", "jpeg"])

st.subheader("1. Baustellen-Bericht einsprechen")
st.write("Sprechen Sie einfach frei ein: **Kunde, Baustelle, erledigte Arbeiten, Material und Stunden.**")

audio_data = st.audio_input("Mikrofon antippen zum Aufnehmen")
extra_notes = st.text_area(
    "Optionale Zusatznotizen (falls etwas ergänzt werden soll)",
    placeholder="z. B. Nachtrag mit Herrn Müller vor Ort besprochen...",
    height=75,
)

if st.button("📄 Regiebericht erstellen", type="primary", use_container_width=True):
    if audio_data is None:
        st.warning("⚠️ Bitte nehmen Sie zuerst eine Sprachnachricht auf.")
    else:
        with st.spinner("KI erstellt Ihren Regiebericht..."):
            try:
                audio_bytes = audio_data.read()
                mime_type = audio_data.type or "audio/wav"
                result_data = analyze_audio_with_gemini(audio_bytes, mime_type, gewerk, extra_notes)
                st.session_state.last_report = result_data
            except Exception as err:
                st.error(f"Fehler bei der Erstellung: {err}")

# ---------------------------------------------------------
# Ergebnis prüfen, anpassen & PDF herunterladen
# ---------------------------------------------------------
if "last_report" in st.session_state and st.session_state.last_report:
    st.divider()
    st.subheader("2. Bericht prüfen & PDF herunterladen")

    rep = st.session_state.last_report

    kunde_edit = st.text_input("Kunde / Auftraggeber", value=rep.get("kunde", ""))
    baustelle_edit = st.text_input("Baustelle / Ort", value=rep.get("baustelle", ""))
    zeit_edit = st.text_input("Arbeitszeit / Personal", value=rep.get("arbeitszeit", ""))

    arbeiten_text = st.text_area(
        "Ausgeführte Arbeiten (eine Zeile pro Punkt)",
        value="\n".join(rep.get("ausgefuehrte_arbeiten", [])),
        height=120,
    )
    material_text = st.text_area(
        "Verwendetes Material (eine Zeile pro Position)",
        value="\n".join(rep.get("material", [])),
        height=90,
    )
    besonderheiten_edit = st.text_area(
        "Besonderheiten / Absprachen",
        value=rep.get("besonderheiten", ""),
        height=70,
    )
    unterzeichner_name = st.text_input(
        "Name des Unterzeichners beim Kunden (optional für PDF)",
        placeholder="z. B. Herr Weber",
    )

    updated_data = {
        "kunde": kunde_edit,
        "baustelle": baustelle_edit,
        "arbeitszeit": zeit_edit,
        "ausgefuehrte_arbeiten": [line.strip() for line in arbeiten_text.splitlines() if line.strip()],
        "material": [line.strip() for line in material_text.splitlines() if line.strip()],
        "besonderheiten": besonderheiten_edit,
    }

    logo_bytes = logo_file.read() if logo_file is not None else None
    datum_formatted = einsatz_datum.strftime("%d.%m.%Y")

    pdf_bytes = build_regiebericht_pdf(
        firma=firma_name,
        gewerk=gewerk,
        datum_str=datum_formatted,
        daten=updated_data,
        unterzeichner_kunde=unterzeichner_name,
        logo_bytes=logo_bytes,
    )

    safe_kunde = re.sub(r"[^a-zA-Z0-9_-]", "_", kunde_edit or "Kunde")
    file_name = f"Regiebericht_{safe_kunde}_{einsatz_datum.strftime('%Y-%m-%d')}.pdf"

    st.download_button(
        label="⬇️ Fertiges PDF herunterladen",
        data=pdf_bytes,
        file_name=file_name,
        mime="application/pdf",
        type="primary",
        use_container_width=True,
    )
