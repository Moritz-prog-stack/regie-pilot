import importlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import date

import streamlit as st
from fpdf import FPDF
from google import genai
from google.genai import errors, types

# ---------------------------------------------------------------
# Einstellungen: Modellnamen nur HIER ändern
# ---------------------------------------------------------------
MODELS = ["gemini-3.5-flash-lite", "gemini-3.8-flash"]  # erstes = Hauptmodell, Rest = Fallback
RETRIES_PRO_MODELL = 2
RETRY_CODES = {429, 500, 503, 504}
TIMEOUT_MS = 20000  # ein Versuch bricht nach 20 Sekunden ab

GEWERKE = [
    "SHK (Sanitär, Heizung, Klima)",
    "Elektro",
    "Maler & Lackierer",
    "Dachdecker",
    "Tischler / Schreiner",
    "Trockenbau",
    "Fliesenleger",
    "Garten- & Landschaftsbau",
]

PROMPT = """Du bist Assistent für einen deutschen Handwerksbetrieb im Gewerk: {gewerk}.
Wandle die folgende unstrukturierte Baustellen-Notiz in einen Regiebericht um.
Erfinde NICHTS dazu. Was nicht in der Notiz steht, bleibt leer ("" oder null).
Alles, was unklar oder unvollständig ist (z. B. fehlende Pause, ungenaue Mengen,
unklare Personenzahl), trägst du unter "hinweise" ein.

Antworte NUR mit gültigem JSON in genau diesem Format:
{{
  "kunde_baustelle": "z. B. Familie Weber, Keller",
  "mitarbeiter": ["Namen, falls genannt"],
  "anzahl_mitarbeiter": 1,
  "stunden_pro_mitarbeiter": 0.0,
  "pause_minuten": 0,
  "taetigkeiten": ["kurze, abrechnungsfähige Beschreibung je Tätigkeit"],
  "material": [{{"bezeichnung": "", "menge": 0, "einheit": "m / Stk / ..."}}],
  "hinweise": ["Unklarheiten oder fehlende Angaben, die der Chef prüfen sollte"]
}}

Notiz:
{notiz}
"""


# ---------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------
def to_float(x, default=0.0) -> float:
    try:
        if isinstance(x, str):
            x = x.replace(",", ".").strip()
        return float(x)
    except (TypeError, ValueError):
        return default


def as_list(x) -> list:
    if x is None:
        return []
    if isinstance(x, (list, tuple)):
        return [str(i).strip() for i in x if str(i).strip()]
    return [str(x).strip()] if str(x).strip() else []


def normalisiere(d) -> dict:
    """Macht aus der KI-Antwort ein sauberes, vorhersagbares Dictionary."""
    if not isinstance(d, dict):
        raise ValueError("KI-Antwort ist kein JSON-Objekt")

    material = []
    for m in d.get("material") or []:
        if isinstance(m, dict):
            menge = m.get("menge")
            material.append(
                {
                    "Bezeichnung": str(m.get("bezeichnung") or ""),
                    "Menge": "" if menge is None else str(menge),
                    "Einheit": str(m.get("einheit") or ""),
                }
            )

    mitarbeiter = as_list(d.get("mitarbeiter"))
    anzahl = int(to_float(d.get("anzahl_mitarbeiter"), 1))
    anzahl = max(anzahl, len(mitarbeiter), 1)

    return {
        "kunde_baustelle": str(d.get("kunde_baustelle") or ""),
        "mitarbeiter": mitarbeiter,
        "anzahl_mitarbeiter": anzahl,
        "stunden": max(to_float(d.get("stunden_pro_mitarbeiter")), 0.0),
        "pause_minuten": max(int(to_float(d.get("pause_minuten"))), 0),
        "taetigkeiten": as_list(d.get("taetigkeiten")),
        "material": material,
        "hinweise": as_list(d.get("hinweise")),
    }


def parse_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    return json.loads(text)


# ---------------------------------------------------------------
# Gemini-Aufruf mit Retry und Fallback-Modell
# ---------------------------------------------------------------
def get_api_key() -> str:
    try:
        if "GEMINI_API_KEY" in st.secrets:
            return st.secrets["GEMINI_API_KEY"]
    except Exception:
        pass
    return os.environ.get("GEMINI_API_KEY", "")


def call_gemini(api_key: str, prompt: str) -> tuple[dict, str]:
    client = genai.Client(
        api_key=api_key, http_options=types.HttpOptions(timeout=TIMEOUT_MS)
    )
    config = types.GenerateContentConfig(
        response_mime_type="application/json", temperature=0.2
    )
    last_error = None
    for model in MODELS:
        for versuch in range(RETRIES_PRO_MODELL):
            try:
                resp = client.models.generate_content(
                    model=model, contents=prompt, config=config
                )
                return normalisiere(parse_json(resp.text)), model
            except errors.APIError as e:
                last_error = e
                if e.code in RETRY_CODES:
                    time.sleep(1 + versuch)  # 1s, 2s
                    continue
                break  # z. B. 404 (Modell abgeschaltet) -> sofort nächstes Modell
            except ValueError as e:  # kein sauberes JSON -> nochmal versuchen
                last_error = e
                continue
            except Exception as e:  # z. B. Timeout -> nächstes Modell
                last_error = e
                break
    raise RuntimeError(f"Alle Modelle fehlgeschlagen. Letzter Fehler: {last_error}")


# ---------------------------------------------------------------
# PDF
# ---------------------------------------------------------------
def t(s) -> str:
    """Text PDF-sicher machen (Standardschrift kann nur Latin-1)."""
    s = "" if s is None else str(s)
    for a, b in {
        "–": "-", "—": "-", "€": "EUR", "„": '"', "“": '"', "”": '"', "’": "'", "•": "-",
    }.items():
        s = s.replace(a, b)
    return s.encode("latin-1", "replace").decode("latin-1")


def fmt(zahl: float) -> str:
    return f"{zahl:.2f}".replace(".", ",")


def build_pdf(d: dict, firma: str, gewerk: str, stunden_netto: float, stundensatz: float) -> bytes:
    anzahl = d["anzahl_mitarbeiter"]
    gesamt_std = stunden_netto * anzahl

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    def zeile(text, h=6, bold=False):
        pdf.set_font("Helvetica", "B" if bold else "", 10)
        pdf.multi_cell(0, h, t(text), new_x="LMARGIN", new_y="NEXT")

    def heading(text):
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_fill_color(230, 230, 230)
        pdf.cell(0, 8, t(text), new_x="LMARGIN", new_y="NEXT", fill=True)

    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 10, "Regiebericht / Stundenlohnzettel", new_x="LMARGIN", new_y="NEXT")
    if firma:
        zeile(firma)
    zeile(f"Datum: {date.today().strftime('%d.%m.%Y')}   |   Gewerk: {gewerk}")
    zeile(f"Baustelle / Kunde: {d['kunde_baustelle'] or '-'}")
    pdf.ln(4)

    heading("Arbeitszeit")
    namen = ", ".join(d["mitarbeiter"]) if d["mitarbeiter"] else f"{anzahl} Mitarbeiter"
    zeile(f"Mitarbeiter: {namen}")
    zeile(f"Stunden je Mitarbeiter: {fmt(stunden_netto)} h   |   Pause: {d['pause_minuten']} min")
    zeile(f"Gesamtstunden: {fmt(gesamt_std)} h", bold=True)
    if stundensatz > 0:
        zeile(f"Lohnkosten ({fmt(stundensatz)} EUR/h netto): {fmt(gesamt_std * stundensatz)} EUR", bold=True)
    pdf.ln(4)

    heading("Ausgeführte Arbeiten")
    for x in d["taetigkeiten"] or ["-"]:
        zeile(f"- {x}")
    pdf.ln(4)

    heading("Material")
    if d["material"]:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(110, 6, "Bezeichnung", border=1)
        pdf.cell(35, 6, "Menge", border=1)
        pdf.cell(35, 6, "Einheit", border=1, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        for m in d["material"]:
            pdf.cell(110, 6, t(m["Bezeichnung"])[:60], border=1)
            pdf.cell(35, 6, t(m["Menge"])[:15], border=1)
            pdf.cell(35, 6, t(m["Einheit"])[:15], border=1, new_x="LMARGIN", new_y="NEXT")
    else:
        zeile("Kein Material angegeben.")
    pdf.ln(14)

    y = pdf.get_y()
    pdf.line(10, y, 90, y)
    pdf.line(110, y, 190, y)
    pdf.set_y(y + 1)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(100, 6, "Unterschrift Auftragnehmer")
    pdf.cell(0, 6, "Unterschrift Auftraggeber", new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())


# ---------------------------------------------------------------
# Sprachnotiz -> Text (lokal mit faster-whisper)
# ---------------------------------------------------------------
@st.cache_resource(show_spinner="Spracherkennung wird geladen ...")
def load_whisper():
    """Lädt das Whisper-Modell nur einmal und hält es im Speicher."""
    from faster_whisper import WhisperModel

    return WhisperModel("tiny", device="cpu", compute_type="int8")


def stelle_paket_sicher(modul: str, pip_name: str) -> None:
    """Installiert ein fehlendes Paket automatisch in genau die Python-Umgebung,
    in der diese App läuft (sys.executable)."""
    try:
        vorhanden = importlib.util.find_spec(modul) is not None
    except (ImportError, ValueError):
        vorhanden = False
    if vorhanden:
        return
    res = subprocess.run(
        [sys.executable, "-m", "pip", "install", pip_name],
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if res.returncode != 0:
        raise RuntimeError(f"Installation von {pip_name} fehlgeschlagen: {res.stderr[-300:]}")
    importlib.invalidate_caches()


def lade_audio(pfad: str):
    """Dekodiert jedes Audioformat mit dem mitgelieferten ffmpeg (imageio-ffmpeg)
    zu 16-kHz-Mono. So braucht die Erkennung kein funktionierendes PyAV."""
    stelle_paket_sicher("imageio_ffmpeg", "imageio-ffmpeg")
    stelle_paket_sicher("numpy", "numpy")
    import imageio_ffmpeg
    import numpy as np

    cmd = [
        imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-i", pfad,
        "-f", "s16le", "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le", "-",
    ]
    res = subprocess.run(
        cmd, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    if res.returncode != 0 or not res.stdout:
        raise RuntimeError("ffmpeg konnte die Datei nicht lesen: " + res.stderr.decode(errors="replace")[-300:])
    return np.frombuffer(res.stdout, np.int16).astype(np.float32) / 32768.0


def transkribiere(datei) -> str:
    """Speichert die hochgeladene Audiodatei temporär und transkribiert sie auf Deutsch."""
    endung = os.path.splitext(datei.name)[1].lower() or ".wav"
    pfad = None
    fehler = []
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=endung) as tmp:
            tmp.write(datei.getvalue())
            pfad = tmp.name

        # Weg 1: selbst dekodieren (unabhängig von PyAV). Weg 2: faster-whisper liest die Datei selbst.
        try:
            quelle = lade_audio(pfad)
        except Exception as e:
            fehler.append(f"Dekodieren: {type(e).__name__}: {e}")
            quelle = pfad

        try:
            model = load_whisper()
            segmente, _info = model.transcribe(quelle, language="de")
            text = " ".join(s.text.strip() for s in segmente).strip()
        except Exception as e:
            fehler.append(f"Erkennung: {type(e).__name__}: {e}")
            raise RuntimeError(" | ".join(fehler))
    finally:
        if pfad and os.path.exists(pfad):
            os.remove(pfad)
    if not text:
        raise ValueError("In der Sprachnotiz wurde keine Sprache erkannt.")
    return text


def versionen() -> str:
    teile = [f"Python {sys.version.split()[0]}: {sys.executable}"]
    for modul in ("streamlit", "faster_whisper", "av", "imageio_ffmpeg", "numpy"):
        try:
            m = importlib.import_module(modul)
            teile.append(f"{modul}: {getattr(m, '__version__', 'installiert')}")
        except Exception:
            teile.append(f"{modul}: NICHT INSTALLIERT")
    return "\n".join(teile)


# ---------------------------------------------------------------
# Oberfläche
# ---------------------------------------------------------------
st.set_page_config(page_title="Regie-Pilot", page_icon="🛠️")
st.title("🛠️ Regie-Pilot: WhatsApp-Chaos zu Regiebericht")
st.caption("Version 2 – Abrechnungsfertiger Regiebericht & PDF-Stundenlohnzettel in Sekunden.")

api_key = get_api_key()
if not api_key:
    api_key = st.text_input("Gemini API-Key", type="password")

gewerk = st.selectbox("1. Gewerk auswählen:", GEWERKE)

audio = st.file_uploader(
    "🎤 Sprachnotiz hochladen (optional)",
    type=["mp3", "wav", "m4a", "ogg"],
)
if audio is not None:
    audio_id = f"{audio.name}-{audio.size}"
    if st.session_state.get("audio_id") != audio_id:
        try:
            with st.spinner("Sprachnotiz wird transkribiert ..."):
                st.session_state["notiz_text"] = transkribiere(audio)
            st.session_state["audio_id"] = audio_id
        except Exception as e:
            st.error(f"Fehler bei der Spracherkennung: {e}")
            with st.expander("Technische Details"):
                st.code(versionen())
            if "metadata_errors" in str(e):
                st.info(
                    "Die Audio-Bibliothek 'av' ist zu alt für faster-whisper. "
                    "Ein Klick auf den Button aktualisiert beide Pakete."
                )
                if st.button("🔧 Bibliotheken jetzt aktualisieren", key="fix_av"):
                    with st.spinner("Pakete werden aktualisiert ..."):
                        res = subprocess.run(
                            [sys.executable, "-m", "pip", "install", "--upgrade", "av", "faster-whisper"],
                            capture_output=True,
                            text=True,
                        )
                    if res.returncode == 0:
                        st.success(
                            "Aktualisiert. Beende Streamlit jetzt mit Strg+C in der "
                            "Eingabeaufforderung und starte es neu: streamlit run app.py"
                        )
                    else:
                        st.code((res.stdout + res.stderr)[-1500:])
    if st.session_state.get("audio_id") == audio_id:
        st.caption("Text aus der Sprachnotiz übernommen – du kannst ihn unten noch korrigieren.")
else:
    st.session_state.pop("audio_id", None)

notiz = st.text_area(
    "2. Baustellen-Notiz eingeben (wie bei WhatsApp):",
    placeholder="Jo, Chef waren heute bei Familie Weber im Keller. 8,5 Std gearbeitet, Pause 30 min ...",
    height=150,
    key="notiz_text",
)

with st.expander("Optionen für den Stundenlohnzettel"):
    firma = st.text_input("Firmenname (erscheint im PDF)")
    stundensatz = st.number_input(
        "Stundensatz netto in EUR (0 = nicht anzeigen)", min_value=0.0, value=0.0, step=5.0
    )
    pause_abziehen = st.checkbox("Pause von den genannten Stunden abziehen", value=False)

if st.button("⚡ Regiebericht & PDF erstellen", type="primary"):
    if not api_key:
        st.error("Bitte zuerst den API-Key eintragen.")
    elif not notiz.strip():
        st.warning("Bitte eine Baustellen-Notiz eingeben.")
    else:
        try:
            with st.spinner("KI erstellt den Regiebericht ..."):
                daten, modell = call_gemini(api_key, PROMPT.format(gewerk=gewerk, notiz=notiz))
            std = daten["stunden"]
            if pause_abziehen:
                std -= daten["pause_minuten"] / 60
            pdf_bytes = build_pdf(daten, firma, gewerk, max(std, 0.0), stundensatz)
            st.session_state["ergebnis"] = {"daten": daten, "pdf": pdf_bytes, "modell": modell}
        except Exception as e:
            st.session_state.pop("ergebnis", None)
            st.error(f"Fehler bei der Verarbeitung: {e}")

# Ergebnis bleibt sichtbar, auch nach Klick auf den Download-Button
erg = st.session_state.get("ergebnis")
if erg:
    d = erg["daten"]
    st.success(f"Fertig (Modell: {erg['modell']})")
    st.subheader("Regiebericht")
    st.write(f"**Baustelle / Kunde:** {d['kunde_baustelle'] or '-'}")
    st.write(
        f"**Mitarbeiter:** {', '.join(d['mitarbeiter']) or d['anzahl_mitarbeiter']}  |  "
        f"**Stunden je Mitarbeiter:** {d['stunden']} h  |  "
        f"**Pause:** {d['pause_minuten']} min"
    )
    st.write("**Ausgeführte Arbeiten:**")
    for x in d["taetigkeiten"]:
        st.write(f"- {x}")
    if d["material"]:
        st.write("**Material:**")
        st.table(d["material"])
    for h in d["hinweise"]:
        st.warning(h)
    st.download_button(
        "📄 PDF herunterladen",
        data=erg["pdf"],
        file_name=f"Regiebericht_{date.today().isoformat()}.pdf",
        mime="application/pdf",
    )