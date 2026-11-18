import base64
import io
import json
import os
import re
import time
from datetime import date

import streamlit as st
import streamlit.components.v1 as components
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
# Zugangsschutz (Firmen-Code: RP-7482-X)
# ---------------------------------------------------------
def check_access():
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
# Hilfsfunktionen: Gemini API, Unterschrift & PDF-Generierung
# ---------------------------------------------------------
def get_gemini_client():
    api_key = st.secrets.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        st.error("❌ Kein GEMINI_API_KEY gefunden. Bitte in den Streamlit Secrets hinterlegen.")
        st.stop()
    return genai.Client(api_key=api_key)


def clean_pdf_text(text: str) -> str:
    """Säubert Sonderzeichen & lange Strings ohne Leerzeichen für fehlerfreies PDF-Rendering."""
    if not text:
        return "-"
    text = str(text)
    replacements = {
        "€": "EUR",
        "–": "-",
        "—": "-",
        "„": '"',
        "“": '"',
        "‚": "'",
        "’": "'",
        "•": "-",
        "\t": " ",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    words = []
    for word in text.split(" "):
        if len(word) > 65:
            word = " ".join(word[i : i + 65] for i in range(0, len(word), 65))
        words.append(word)
    safe_text = " ".join(words)
    return safe_text.encode("latin-1", "replace").decode("latin-1")


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


def decode_signature_base64(data_url: str) -> bytes | None:
    """Wandelt eine Base64-Data-URL aus dem Touch-Pad in PNG-Bytes um."""
    if not data_url:
        return None
    cleaned = data_url.strip()
    if cleaned.startswith("data:image/png;base64,"):
        cleaned = cleaned.split(",", 1)[1]
    try:
        return base64.b64decode(cleaned)
    except Exception:
        return None


def render_signature_pad():
    """Eigenständiges HTML5-Touch-Unterschriftsfeld (ohne externe Python-Pakete)."""
    html_code = """
    <div style="font-family: sans-serif; background: #f8f9fa; border: 2px dashed #adb5bd; border-radius: 10px; padding: 10px;">
        <canvas id="sig-canvas" width="520" height="150"
            style="width: 100%; height: 150px; background: #ffffff; border-radius: 6px; touch-action: none; cursor: crosshair; display: block;">
        </canvas>
        <div style="display: flex; gap: 10px; margin-top: 8px; align-items: center;">
            <button type="button" id="clear-btn"
                style="padding: 6px 14px; border: 1px solid #ced4da; background: #ffffff; border-radius: 6px; cursor: pointer; font-size: 13px;">
                🗑️ Löschen
            </button>
            <button type="button" id="copy-btn"
                style="padding: 6px 14px; border: none; background: #0d6efd; color: white; border-radius: 6px; cursor: pointer; font-size: 13px; font-weight: 600;">
                ✅ Unterschrift übernehmen
            </button>
            <span id="status-msg" style="font-size: 12px; color: #198754; font-weight: 600;"></span>
        </div>
    </div>
    <script>
        const canvas = document.getElementById("sig-canvas");
        const ctx = canvas.getContext("2d");
        const statusMsg = document.getElementById("status-msg");
        let drawing = false;
        let hasDrawn = false;

        ctx.lineWidth = 3;
        ctx.lineCap = "round";
        ctx.strokeStyle = "#000000";

        function getPos(e) {
            const rect = canvas.getBoundingClientRect();
            const scaleX = canvas.width / rect.width;
            const scaleY = canvas.height / rect.height;
            const clientX = e.touches ? e.touches[0].clientX : e.clientX;
            const clientY = e.touches ? e.touches[0].clientY : e.clientY;
            return {
                x: (clientX - rect.left) * scaleX,
                y: (clientY - rect.top) * scaleY
            };
        }

        function startDraw(e) {
            e.preventDefault();
            drawing = true;
            hasDrawn = true;
            const pos = getPos(e);
            ctx.beginPath();
            ctx.moveTo(pos.x, pos.y);
        }

        function draw(e) {
            if (!drawing) return;
            e.preventDefault();
            const pos = getPos(e);
            ctx.lineTo(pos.x, pos.y);
            ctx.stroke();
        }

        function stopDraw(e) {
            if (!drawing) return;
            e.preventDefault();
            drawing = false;
        }

        canvas.addEventListener("mousedown", startDraw);
        canvas.addEventListener("mousemove", draw);
        canvas.addEventListener("mouseup", stopDraw);
        canvas.addEventListener("mouseout", stopDraw);

        canvas.addEventListener("touchstart", startDraw, { passive: false });
        canvas.addEventListener("touchmove", draw, { passive: false });
        canvas.addEventListener("touchend", stopDraw, { passive: false });

        document.getElementById("clear-btn").addEventListener("click", () => {
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            hasDrawn = false;
            statusMsg.textContent = "Feld geleert.";
        });

        document.getElementById("copy-btn").addEventListener("click", () => {
            if (!hasDrawn) {
                statusMsg.style.color = "#dc3545";
                statusMsg.textContent = "Bitte zuerst unterschreiben!";
                return;
            }
            const dataUrl = canvas.toDataURL("image/png");
            navigator.clipboard.writeText(dataUrl).then(() => {
                statusMsg.style.color = "#198754";
                statusMsg.textContent = "Kopiert! Jetzt unten in das Feld einfügen.";
            }).catch(() => {
                prompt("Bitte diesen Code kopieren (Strg+C):", dataUrl);
            });
        });
    </script>
    """
    components.html(html_code, height=215)


def build_regiebericht_pdf(
    firma: str,
    gewerk: str,
    datum_str: str,
    daten: dict,
    unterzeichner_kunde: str = "",
    logo_bytes: bytes | None = None,
    signature_bytes: bytes | None = None,
) -> bytes:
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_margins(left=15, top=15, right=15)
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()

    full_w = 180.0

    # Optionales Firmenlogo oben rechts
    if logo_bytes:
        tmp_logo = "/tmp/company_logo.png"
        with open(tmp_logo, "wb") as f:
            f.write(logo_bytes)
        try:
            pdf.image(tmp_logo, x=157, y=12, w=38)
        except Exception:
            pass

    # Kopfbereich
    pdf.set_x(15)
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(full_w, 10, clean_pdf_text(firma), new_x="LMARGIN", new_y="NEXT")

    pdf.set_x(15)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(full_w, 6, clean_pdf_text(f"Gewerk: {gewerk}  |  Datum: {datum_str}"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(5)

    pdf.set_draw_color(40, 40, 40)
    pdf.line(15, pdf.get_y(), 195, pdf.get_y())
    pdf.ln(6)

    pdf.set_x(15)
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(full_w, 9, "REGIEBERICHT / TAGESNACHWEIS", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    # Stammdaten
    label_w = 46.0
    val_w = full_w - label_w

    for label, key in [
        ("Auftraggeber / Kunde:", "kunde"),
        ("Baustelle / Einsatzort:", "baustelle"),
        ("Arbeitszeit / Personal:", "arbeitszeit"),
    ]:
        pdf.set_x(15)
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(label_w, 7, label)
        pdf.set_font("Helvetica", "", 10)
        pdf.multi_cell(val_w, 7, clean_pdf_text(daten.get(key, "-")), new_x="LMARGIN", new_y="NEXT")

    pdf.ln(4)

    # Ausgeführte Arbeiten
    pdf.set_x(15)
    pdf.set_fill_color(240, 242, 246)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(full_w, 8, "  Ausgefuehrte Arbeiten", new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 10)
    arbeiten = daten.get("ausgefuehrte_arbeiten") or ["Keine Angaben"]
    for item in arbeiten:
        pdf.set_x(15)
        pdf.multi_cell(full_w, 6, clean_pdf_text(f"- {item}"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Verwendetes Material
    pdf.set_x(15)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(full_w, 8, "  Verwendetes Material", new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 10)
    materialien = daten.get("material") or ["Kein Material angegeben"]
    for mat in materialien:
        pdf.set_x(15)
        pdf.multi_cell(full_w, 6, clean_pdf_text(f"- {mat}"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Besonderheiten / Hinweise
    pdf.set_x(15)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(full_w, 8, "  Besonderheiten / Absprachen", new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_x(15)
    pdf.multi_cell(full_w, 6, clean_pdf_text(daten.get("besonderheiten", "-")), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(18)

    # Unterschriftsfelder (inkl. optionaler digitaler Touch-Unterschrift des Kunden)
    if pdf.get_y() > 245:
        pdf.add_page()
        pdf.ln(15)

    pdf.set_x(15)
    y_sig = pdf.get_y()

    if signature_bytes:
        tmp_sig = "/tmp/kunde_sig.png"
        with open(tmp_sig, "wb") as f:
            f.write(signature_bytes)
        try:
            pdf.image(tmp_sig, x=120, y=y_sig - 16, w=65, h=15)
        except Exception:
            pass

    pdf.line(15, y_sig, 95, y_sig)
    pdf.line(115, y_sig, 195, y_sig)
    pdf.ln(2)
    pdf.set_x(15)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(100, 5, clean_pdf_text(f"Datum / Unterschrift {firma}"))
    kunde_label = (
        f"Unterschrift Auftraggeber ({unterzeichner_kunde})"
        if unterzeichner_kunde
        else "Unterschrift Auftraggeber / Bauleitung"
    )
    pdf.cell(80, 5, clean_pdf_text(kunde_label), new_x="LMARGIN", new_y="NEXT")

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
# Ergebnis prüfen, unterschreiben lassen & PDF herunterladen
# ---------------------------------------------------------
if "last_report" in st.session_state and st.session_state.last_report:
    st.divider()
    st.subheader("2. Bericht prüfen, unterschreiben & PDF laden")

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

    with st.expander("✍️ Digitale Kunden-Unterschrift hinzufügen (optional)", expanded=False):
        st.caption("1. Im weißen Feld unterschreiben → 2. Auf **„Unterschrift übernehmen“** tippen → 3. Unten einfügen:")
        render_signature_pad()
        sig_data_input = st.text_input(
            "Unterschrifts-Code hier einfügen (Strg+V / Einfügen)",
            placeholder="Nach Klick auf 'Unterschrift übernehmen' hier einfügen...",
        )

    sig_bytes = decode_signature_base64(sig_data_input)
    if sig_bytes:
        st.success("✅ Digitale Unterschrift wird ins PDF eingefügt.")

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
        signature_bytes=sig_bytes,
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
