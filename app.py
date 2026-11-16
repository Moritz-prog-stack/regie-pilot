import json
import os
import re
from datetime import date

import streamlit as st
from fpdf import FPDF
from google import genai
from google.genai import errors, types

# ---------------------------------------------------------------------------
# Einstellungen: Modellnamen nur HIER ändern
# ---------------------------------------------------------------------------
MODELS = ["gemini-3.5-flash-lite", "gemini-3.8-flash"]  # Erstes = Hauptmodell, Rest = Fallback
RETRIES_PER_MODEL = 2
RETRY_CODES = {429, 500, 503, 504}
TIMEOUT_MS = 25000

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

st.set_page_config(
    page_title="Regie-Pilot | Regiebericht per KI",
    page_icon="🛠️",
    layout="centered",
)


def bereinige_text(text: str) -> str:
    """Macht deutsche Umlaute und Sonderzeichen für FPDF2 Standard-Schriften sicher."""
    if not text:
        return ""
    ersetzungen = {
        "€": "EUR",
        "–": "-",
        "—": "-",
        "„": '"',
        "“": '"',
        "‚": "'",
        "‘": "'",
        "…": "...",
        "•": "-",
    }
    for alt, neu in ersetzungen.items():
        text = text.replace(alt, neu)
    return text.encode("latin-1", errors="replace").decode("latin-1")


def hole_api_key() -> str:
    """Lädt den API-Key bevorzugt aus Streamlit Secrets, sonst aus Umgebung oder Eingabe."""
    if "GEMINI_API_KEY" in st.secrets:
        return st.secrets["GEMINI_API_KEY"]
    env_key = os.environ.get("GEMINI_API_KEY", "")
    if env_key:
        return env_key
    return st.text_input("Gemini API-Key (nur nötig, falls noch nicht in Secrets hinterlegt)", type="password")


def erstelle_pdf(daten: dict, gewerk: str, mit_unterschrift: bool, firma_name: str) -> bytes:
    """Erzeugt ein sauber formatiertes Regiebericht-PDF."""
    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    # Kopfbereich
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 10, bereinige_text("REGIEBERICHT / STUNDENLOHNZETTEL"), ln=True)

    pdf.set_font("Helvetica", "", 10)
    if firma_name.strip():
        pdf.cell(0, 6, bereinige_text(f"Ausführender Betrieb: {firma_name.strip()}"), ln=True)
    pdf.cell(0, 6, bereinige_text(f"Gewerk: {gewerk}   |   Datum: {daten.get('datum', date.today().strftime('%d.%m.%Y'))}"), ln=True)
    pdf.ln(4)

    # Trennlinie
    pdf.set_draw_color(180, 180, 180)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(5)

    # Kunde & Baustelle
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, bereinige_text("1. Kunde & Baustelle"), ln=True)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, bereinige_text(f"Kunde / Auftraggeber: {daten.get('kunde', '-')}"), ln=True)
    pdf.cell(0, 6, bereinige_text(f"Baustelle / Einsatzort: {daten.get('baustelle', '-')}"), ln=True)
    pdf.ln(4)

    # Ausgeführte Arbeiten
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, bereinige_text("2. Ausgeführte Arbeiten"), ln=True)
    pdf.set_font("Helvetica", "", 10)
    arbeiten = daten.get("arbeiten", [])
    if arbeiten:
        for punkt in arbeiten:
            pdf.multi_cell(0, 6, bereinige_text(f"- {punkt}"))
    else:
        pdf.multi_cell(0, 6, bereinige_text("- Keine Tätigkeiten angegeben."))
    pdf.ln(4)

    # Arbeitszeiten & Personal
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, bereinige_text("3. Arbeitszeiten & Mitarbeiter"), ln=True)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_fill_color(240, 240, 240)
    pdf.cell(75, 7, bereinige_text("Mitarbeiter / Qualifikation"), border=1, fill=True)
    pdf.cell(45, 7, bereinige_text("Zeitraum"), border=1, fill=True)
    pdf.cell(30, 7, bereinige_text("Pause (Min)"), border=1, fill=True)
    pdf.cell(40, 7, bereinige_text("Netto-Stunden"), border=1, fill=True, ln=True)

    pdf.set_font("Helvetica", "", 10)
    mitarbeiter = daten.get("mitarbeiter", [])
    gesamt_stunden = 0.0
    if mitarbeiter:
        for m in mitarbeiter:
            std = float(m.get("netto_stunden", 0))
            gesamt_stunden += std
            pdf.cell(75, 7, bereinige_text(str(m.get("name", "Monteur"))), border=1)
            pdf.cell(45, 7, bereinige_text(str(m.get("zeitraum", "-"))), border=1)
            pdf.cell(30, 7, bereinige_text(str(m.get("pause_min", 0))), border=1)
            pdf.cell(40, 7, bereinige_text(f"{std:.2f} Std."), border=1, ln=True)
    else:
        pdf.cell(190, 7, bereinige_text("Keine Arbeitszeiten erfasst"), border=1, ln=True)

    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(150, 7, bereinige_text("Gesamtstunden:"), border=1, align="R")
    pdf.cell(40, 7, bereinige_text(f"{gesamt_stunden:.2f} Std."), border=1, ln=True)
    pdf.ln(5)

    # Verbrauchtes Material
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, bereinige_text("4. Verbrauchtes Material"), ln=True)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(35, 7, bereinige_text("Menge"), border=1, fill=True)
    pdf.cell(155, 7, bereinige_text("Materialbezeichnung"), border=1, fill=True, ln=True)

    pdf.set_font("Helvetica", "", 10)
    material = daten.get("material", [])
    if material:
        for item in material:
            pdf.cell(35, 7, bereinige_text(str(item.get("menge", "-"))), border=1)
            pdf.cell(155, 7, bereinige_text(str(item.get("bezeichnung", "-"))), border=1, ln=True)
    else:
        pdf.cell(190, 7, bereinige_text("Kein Material verbraucht / angegeben"), border=1, ln=True)
    pdf.ln(5)

    # Hinweise / Mängel / Nachträge
    hinweise = daten.get("hinweise", "").strip()
    if hinweise:
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 8, bereinige_text("5. Besondere Hinweise / Nachträge"), ln=True)
        pdf.set_font("Helvetica", "", 10)
        pdf.multi_cell(0, 6, bereinige_text(hinweise))
        pdf.ln(6)

    # Unterschriftsfeld
    if mit_unterschrift:
        pdf.ln(8)
        y_pos = pdf.get_y()
        pdf.line(10, y_pos, 90, y_pos)
        pdf.line(115, y_pos, 195, y_pos)
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(105, 5, bereinige_text("Datum, Unterschrift Monteur / Bauleiter"))
        pdf.cell(85, 5, bereinige_text("Datum, Unterschrift Auftraggeber / Kunde"), ln=True)

    return bytes(pdf.output())


def analysiere_eingabe(client: genai.Client, gewerk: str, text_notiz: str, audio_bytes: bytes | None, audio_mime: str | None) -> tuple[dict, str]:
    """Sendet Text und/oder Sprachaufnahme an Gemini und gibt strukturierte JSON-Daten zurück."""
    heute = date.today().strftime("%d.%m.%Y")
    system_prompt = f"""
Du bist ein erfahrener deutscher Bauleiter und Abrechnungs-Experte für das Gewerk "{gewerk}".
Analysiere die Baustellen-Notiz (Text und/oder Sprachaufnahme) und wandle sie in einen abrechnungsfertigen Regiebericht um.
Heutiges Datum (falls kein anderes genannt wird): {heute}.

Wichtige Regeln:
- Formuliere die ausgeführten Arbeiten fachgerecht, klar und kundenfreundlich auf Deutsch (perfekt für eine Rechnung).
- Berechne die Netto-Stunden pro Mitarbeiter exakt (Bruttozeit minus Pause in Stunden als Dezimalzahl, z. B. 7.75 für 7 Std. 45 Min.).
- Falls mehrere Monteure genannt werden (z. B. "Lukas und ich"), führe beide getrennt in der Mitarbeiterliste auf.
- Trenne Mengen und Materialbezeichnungen sauber.
- Antworte AUSSCHLIESSLICH mit validem JSON exakt nach diesem Schema:
{{
  "datum": "TT.MM.JJJJ",
  "kunde": "Name des Kunden oder 'Nicht angegeben'",
  "baustelle": "Adresse oder Einsatzort oder 'Nicht angegeben'",
  "arbeiten": ["Fachgerechter Tätigkeitspunkt 1", "Fachgerechter Tätigkeitspunkt 2"],
  "mitarbeiter": [
    {{
      "name": "Name oder Rolle (z. B. Monteur 1)",
      "zeitraum": "z. B. 07:30 - 16:00 Uhr",
      "pause_min": 45,
      "netto_stunden": 7.75
    }}
  ],
  "material": [
    {{
      "menge": "z. B. 3 m",
      "bezeichnung": "Kupferrohr 22 mm"
    }}
  ],
  "hinweise": "Besondere Hinweise, Restarbeiten, Mängel oder leerer String"
}}
"""

    contents = [system_prompt]
    if audio_bytes and audio_mime:
        contents.append(types.Part.from_bytes(data=audio_bytes, mime_type=audio_mime))
    if text_notiz.strip():
        contents.append(f"Zusätzliche Text-Notiz vom Monteur:\n{text_notiz.strip()}")

    letzter_fehler = None
    for modell in MODELS:
        for _ in range(RETRIES_PER_MODEL):
            try:
                response = client.models.generate_content(
                    model=modell,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.2,
                    ),
                )
                roh_text = response.text.strip()
                roh_text = re.sub(r"^```json\s*|\s*```$", "", roh_text)
                return json.loads(roh_text), modell
            except errors.APIError as e:
                letzter_fehler = e
                if e.code not in RETRY_CODES:
                    raise
            except Exception as e:
                letzter_fehler = e

    raise RuntimeError(f"KI-Anfrage fehlgeschlagen: {letzter_fehler}")


# ---------------------------------------------------------------------------
# Benutzeroberfläche (UI)
# ---------------------------------------------------------------------------
st.title("🛠️ Regie-Pilot: WhatsApp-Chaos zu Regiebericht")
st.caption("Abrechnungsfertiger Regiebericht & PDF-Stundenlohnzettel in Sekunden – per Sprache oder Text.")

api_key = hole_api_key()

gewerk = st.selectbox("1. Gewerk auswählen:", GEWERKE)

st.markdown("### 2. Sprachnotiz direkt einsprechen (oder Text eingeben)")
audio_aufnahme = st.audio_input("🎙️ Mikrofon antippen und Baustellen-Bericht einsprechen")

with st.expander("📁 Optional: Vorhandene Audio-Datei hochladen (MP3, WAV, M4A, OGG)"):
    audio_upload = st.file_uploader("Audiodatei auswählen", type=["mp3", "wav", "m4a", "ogg"])

text_notiz = st.text_area(
    "✍️ Baustellen-Notiz als Text (optional oder ergänzend):",
    placeholder="z. B.: Moin Chef, waren heute bei Familie König in Düren, Lukas und ich von 07:30 bis 16:00 Uhr, 45 Min Pause. Neue Umwälzpumpe Grundfos Alpha2 eingebaut, 3m Kupferrohr 22mm verbraucht...",
    height=130,
)

with st.expander("⚙️ Optionen für den Stundenlohnzettel / PDF"):
    firma_name = st.text_input("Eigener Firmenname im Briefkopf (optional):", placeholder="z. B. Mustermann Haustechnik GmbH")
    mit_unterschrift = st.checkbox("Unterschriftsfelder für Monteur & Kunde ins PDF drucken", value=True)

if st.button("🚀 Regiebericht & PDF erstellen", type="primary", use_container_width=True):
    if not api_key:
        st.error("Bitte hinterlege zuerst deinen Gemini API-Key in den Streamlit Secrets.")
        st.stop()

    aktive_audio = audio_aufnahme if audio_aufnahme is not None else audio_upload
    if aktive_audio is None and not text_notiz.strip():
        st.warning("Bitte sprich zuerst eine kurze Sprachnotiz ein oder gib eine Text-Notiz ein.")
        st.stop()

    audio_bytes = None
    audio_mime = None
    if aktive_audio is not None:
        audio_bytes = aktive_audio.getvalue()
        audio_mime = getattr(aktive_audio, "type", None) or "audio/wav"

    with st.spinner("KI wertet Baustellen-Bericht aus und erstellt PDF..."):
        try:
            client = genai.Client(api_key=api_key)
            daten, verwendetes_modell = analysiere_eingabe(
                client=client,
                gewerk=gewerk,
                text_notiz=text_notiz,
                audio_bytes=audio_bytes,
                audio_mime=audio_mime,
            )
            pdf_bytes = erstelle_pdf(
                daten=daten,
                gewerk=gewerk,
                mit_unterschrift=mit_unterschrift,
                firma_name=firma_name,
            )

            st.success(f"Fertig! Regiebericht erfolgreich erstellt (Modell: {verwendetes_modell})")

            # Vorschau direkt in der App
            col1, col2 = st.columns(2)
            col1.metric("Kunde", daten.get("kunde", "-"))
            col2.metric("Baustelle", daten.get("baustelle", "-"))

            st.subheader("Ausgeführte Arbeiten")
            for a in daten.get("arbeiten", []):
                st.write(f"• {a}")

            st.subheader("Arbeitszeiten")
            for m in daten.get("mitarbeiter", []):
                st.write(f"• **{m.get('name')}**: {m.get('zeitraum')} (Pause: {m.get('pause_min')} Min.) → **{m.get('netto_stunden')} Std.**")

            if daten.get("material"):
                st.subheader("Verbrauchtes Material")
                for mat in daten.get("material", []):
                    st.write(f"• {mat.get('menge')} – {mat.get('bezeichnung')}")

            if daten.get("hinweise"):
                st.info(f"**Hinweis:** {daten.get('hinweise')}")

            # PDF-Download
            kunde_sauber = re.sub(r"[^a-zA-Z0-9_-]", "_", daten.get("kunde", "Kunde"))
            dateiname = f"Regiebericht_{kunde_sauber}_{date.today().strftime('%Y-%m-%d')}.pdf"

            st.download_button(
                label="📄 Regiebericht als PDF herunterladen",
                data=pdf_bytes,
                file_name=dateiname,
                mime="application/pdf",
                type="primary",
                use_container_width=True,
            )

        except Exception as fehler:
            st.error(f"Beim Erstellen des Berichts ist ein Fehler aufgetreten: {fehler}")