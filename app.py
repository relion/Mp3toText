"""Mp3toText - desktop app that transcribes MP3 files to text using Gemini."""

import json
import os
import re
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit,
    QMainWindow, QMessageBox, QProgressBar, QPushButton, QSpinBox, QTextEdit, QVBoxLayout, QWidget,
)

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
DEFAULT_MODEL = "gemini-2.5-flash"
AUDIO_EXTS = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
              ".ogg": "audio/ogg", ".flac": "audio/flac", ".aac": "audio/aac"}
# code -> (label shown in the UI, language name used in the prompt)
LANGUAGES = {
    "he": ("עברית", "Hebrew"),
    "en": ("English", "English"),
    "ro": ("Română", "Romanian"),
}
DEFAULT_LANG = "he"


def lang_instruction(lang):
    name = LANGUAGES[lang][1]
    return (f"The audio is spoken in {name}. Write the transcription in {name}, exactly as spoken. "
            f"Never translate it into any other language. ")


PROMPT = (
    "Transcribe this audio file verbatim. "
    "Return only the transcription text, with paragraph breaks where natural. "
    "Do not add any commentary, headings or timestamps."
)

SRT_PROMPT = (
    "Transcribe this audio file verbatim, as subtitles. "
    "Split the speech into short subtitle segments (one or two short sentences, at most about "
    "80 characters each). For every segment give the exact time it is spoken in the audio: "
    "'start' and 'end' in the format MM:SS.mmm (or HH:MM:SS.mmm for audio longer than an hour), "
    "measured from the very beginning of the file. 'start' is the exact moment the first word "
    "begins to be heard, not the end of the preceding pause. Segments must be in chronological "
    "order and must not overlap. Do not add commentary or describe sounds."
)
SRT_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "start": {"type": "STRING"},
            "end": {"type": "STRING"},
            "text": {"type": "STRING"},
        },
        "required": ["start", "end", "text"],
    },
}
MIN_SEG_SECONDS = 0.5
DEFAULT_SEG_SECONDS = 2.0
# Gemini timestamps run late; subtitles start this much earlier when saving (adjustable in the UI).
DEFAULT_LEAD_IN_MS = 200

# USD per 1M tokens, paid tier (ai.google.dev/gemini-api/docs/pricing).
# Override or add models via "prices" in config.json.
PRICES = {
    "gemini-2.5-flash": {"audio_in": 1.00, "text_in": 0.30, "out": 2.50},
}
DEFAULT_USD_TO_ILS = 3.7  # approximate; override via "usd_to_ils" in config.json
BILLING_URL = "https://console.cloud.google.com/billing"
API_KEYS_URL = "https://aistudio.google.com/api-keys"

DROP_IDLE = "QLabel#drop { background:#eef3fb; color:#3a4a63; border:2px dashed #9fb3d1; border-radius:8px; }"
DROP_HOVER = "QLabel#drop { background:#d6e4fa; color:#3a4a63; border:2px dashed #4a78c2; border-radius:8px; }"


def load_config():
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def clean_key(key):
    # drop invisible/non-ASCII chars (e.g. RTL marks) that sneak in when pasting
    return "".join(c for c in key if c.isascii() and not c.isspace())


def extract_usage(resp):
    """Token counts from a Gemini response, split by what they are billed as."""
    um = resp.usage_metadata
    audio_in = 0
    for d in (um.prompt_tokens_details or []):
        if str(d.modality).upper().endswith("AUDIO"):
            audio_in += d.token_count or 0
    prompt = um.prompt_token_count or 0
    out = (um.candidates_token_count or 0) + (um.thoughts_token_count or 0)
    return {"audio_in": audio_in, "text_in": prompt - audio_in, "out": out,
            "total": um.total_token_count or (prompt + out)}


def usage_cost(usage, prices):
    """Cost in USD, or None when the model has no known price."""
    if not prices:
        return None
    return sum(usage[k] * prices[k] for k in ("audio_in", "text_in", "out")) / 1_000_000


def parse_timestamp(value):
    """'MM:SS.mmm', 'HH:MM:SS,mmm', '12.5' or a number -> seconds (float), or None."""
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", ".")
    if not s:
        return None
    try:
        secs = 0.0
        for part in s.split(":"):
            secs = secs * 60 + float(part)
        return secs
    except ValueError:
        return None


def parse_segments(raw):
    """Gemini JSON reply -> sorted, sanitized list of (start, end, text) in seconds.

    Tolerates truncated JSON (long files hitting the output limit) by salvaging
    the complete objects that were received.
    """
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        items = []
        for m in re.finditer(r"\{[^{}]*\}", raw):
            try:
                items.append(json.loads(m.group(0)))
            except json.JSONDecodeError:
                pass
    if isinstance(items, dict):
        items = items.get("segments", [])

    segs = []
    for it in items:
        if not isinstance(it, dict):
            continue
        text = " ".join(str(it.get("text", "")).split())
        start = parse_timestamp(it.get("start"))
        if not text or start is None:
            continue
        segs.append([start, parse_timestamp(it.get("end")), text])
    segs.sort(key=lambda s: s[0])

    for i, seg in enumerate(segs):
        nxt = segs[i + 1][0] if i + 1 < len(segs) else None
        end = seg[1]
        if end is None or end <= seg[0]:
            end = seg[0] + DEFAULT_SEG_SECONDS
        if nxt is not None and end > nxt:  # no overlap with the next subtitle
            end = max(nxt, seg[0] + MIN_SEG_SECONDS)
        seg[1] = end
    return [tuple(s) for s in segs]


def format_srt_time(seconds):
    ms = int(round(max(seconds, 0) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def apply_lead_in(segments, lead_in):
    """Move each start earlier by lead_in seconds; end times stay put.

    A start never goes below 0 and never before the previous subtitle's end
    (no overlap), and is never later than the original start.
    """
    out = []
    prev_end = 0.0
    for a, b, text in segments:
        new_a = min(max(a - lead_in, prev_end, 0.0), a)
        out.append((new_a, b, text))
        prev_end = b
    return out


def segments_to_srt(segments, lead_in=0.0):
    if lead_in:
        segments = apply_lead_in(segments, lead_in)
    blocks = [f"{i}\n{format_srt_time(a)} --> {format_srt_time(b)}\n{text}\n"
              for i, (a, b, text) in enumerate(segments, 1)]
    return "\n".join(blocks)


class TranscribeWorker(QThread):
    finished_ok = Signal(str, dict, list)  # text, usage, segments (empty unless SRT requested)
    failed = Signal(str)

    def __init__(self, key, model, path, want_srt=False, lang=DEFAULT_LANG):
        super().__init__()
        self.key, self.model, self.path, self.want_srt = key, model, path, want_srt
        self.lang = lang

    def run(self):
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=self.key)
            mime = AUDIO_EXTS[self.path.suffix.lower()]
            # Upload as a byte stream with an ASCII name: the SDK sends the file
            # name in an HTTP header, which fails for Hebrew file names.
            with open(self.path, "rb") as f:
                uploaded = client.files.upload(
                    file=f, config={"mime_type": mime, "display_name": "audio" + self.path.suffix.lower()})
            gen_cfg = types.GenerateContentConfig(temperature=0, max_output_tokens=65536)
            if self.want_srt:
                gen_cfg.response_mime_type = "application/json"
                gen_cfg.response_schema = SRT_SCHEMA
            if "flash" in self.model:
                # transcription doesn't need "thinking", which is billed as output
                gen_cfg.thinking_config = types.ThinkingConfig(thinking_budget=0)
            try:
                prompt = lang_instruction(self.lang) + (SRT_PROMPT if self.want_srt else PROMPT)
                resp = client.models.generate_content(
                    model=self.model, contents=[prompt, uploaded], config=gen_cfg)
            finally:
                try:
                    client.files.delete(name=uploaded.name)
                except Exception:
                    pass
            text = (resp.text or "").strip()
            if not text:
                raise RuntimeError("לא התקבל טקסט מהמודל.")
            segments = []
            if self.want_srt:
                segments = parse_segments(text)
                if not segments:
                    raise RuntimeError("לא ניתן היה לפענח כתוביות מתשובת המודל.")
                text = "\n".join(s[2] for s in segments)
            self.finished_ok.emit(text, extract_usage(resp), segments)
        except Exception as e:
            self.failed.emit(str(e))


class DropLabel(QLabel):
    file_dropped = Signal(str)
    clicked = Signal()

    def __init__(self):
        super().__init__("גרור לכאן קובץ MP3\nאו לחץ כאן לבחירת קובץ")
        self.setObjectName("drop")
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumHeight(90)
        self.setFont(QFont("Segoe UI", 13))
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(DROP_IDLE)
        self.setAcceptDrops(True)

    def mousePressEvent(self, e):
        self.clicked.emit()

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            self.setStyleSheet(DROP_HOVER)
            e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self.setStyleSheet(DROP_IDLE)

    def dropEvent(self, e):
        self.setStyleSheet(DROP_IDLE)
        urls = e.mimeData().urls()
        if urls:
            self.file_dropped.emit(urls[0].toLocalFile())


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.file_path = None
        self.worker = None
        self.segments = []  # (start, end, text) from the last SRT-enabled run

        self.setWindowTitle("Mp3toText – המרת אודיו לטקסט")
        self.resize(860, 640)
        self.setAcceptDrops(True)  # dropping anywhere on the window also works

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # --- top toolbar (layout is RTL, so first widget is on the right) ---
        top = QHBoxLayout()
        self.convert_btn = QPushButton("▶ המר לטקסט")
        self.convert_btn.setStyleSheet("font-weight:bold;")
        self.convert_btn.setEnabled(False)
        self.convert_btn.clicked.connect(self.convert)
        self.srt_check = QCheckBox("צור גם כתוביות (SRT)")
        self.srt_check.setToolTip("מבקש מהמודל גם חותמות זמן. הפלט ארוך יותר ולכן העלות מעט גבוהה יותר.")
        self.srt_check.setChecked(bool(self.cfg.get("srt", False)))
        self.srt_check.toggled.connect(self.on_srt_toggled)
        self.lang_combo = QComboBox()
        self.lang_combo.setToolTip("שפת הדיבור באודיו")
        for code, (label, _name) in LANGUAGES.items():
            self.lang_combo.addItem(label, code)
        saved = self.lang_combo.findData(self.cfg.get("lang", DEFAULT_LANG))
        self.lang_combo.setCurrentIndex(max(saved, 0))
        self.lang_combo.currentIndexChanged.connect(self.on_lang_changed)
        lang_label = QLabel("שפה:")
        key_btn = QPushButton("⚙ מפתח API")
        key_btn.clicked.connect(self.ask_api_key)
        self.total_label = QLabel()
        self.total_label.setStyleSheet("color:#555;")
        reset_btn = QPushButton("↺")
        reset_btn.setToolTip("איפוס הסכום המצטבר")
        reset_btn.setFixedWidth(30)
        reset_btn.clicked.connect(self.reset_totals)
        billing_btn = QPushButton("💳 חיוב")
        billing_btn.setToolTip(
            "פתיחת דף החיוב של Google Cloud בדפדפן\n"
            "שם רואים את החיוב בפועל, אמצעי התשלום וקביעת תקציב/התראות.\n"
            "הסכום המוצג כאן באפליקציה הוא הערכה בלבד.")
        billing_btn.setCursor(Qt.PointingHandCursor)
        billing_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(BILLING_URL)))
        top.addWidget(self.convert_btn)
        top.addWidget(lang_label)
        top.addWidget(self.lang_combo)
        top.addWidget(self.srt_check)
        top.addStretch()
        top.addWidget(self.total_label)
        top.addWidget(reset_btn)
        top.addWidget(billing_btn)
        top.addWidget(key_btn)
        root.addLayout(top)

        # --- drop zone ---
        self.drop = DropLabel()
        self.drop.file_dropped.connect(self.load_file)
        self.drop.clicked.connect(self.browse)
        root.addWidget(self.drop)

        # --- text output: Qt picks direction per paragraph (Hebrew RTL, English LTR) ---
        self.text = QTextEdit()
        self.text.setAcceptRichText(False)
        self.text.setFont(QFont("Segoe UI", 12))
        self.text.setPlaceholderText("כאן יופיע התמלול")
        root.addWidget(self.text, 1)

        # --- bottom bar ---
        bottom = QHBoxLayout()
        copy_btn = QPushButton("📋 העתק הכל")
        copy_btn.clicked.connect(self.copy_all)
        save_btn = QPushButton("💾 שמור כקובץ")
        save_btn.clicked.connect(self.save_text)
        lead_label = QLabel("הקדמה:")
        self.lead_spin = QSpinBox()
        self.lead_spin.setRange(0, 2000)
        self.lead_spin.setSingleStep(50)
        self.lead_spin.setSuffix(" ms")
        self.lead_spin.setToolTip("מקדים את הופעת הכתוביות. הגדל אם הכתוביות מאחרות")
        self.lead_spin.setValue(int(self.cfg.get("srt_lead_in_ms", DEFAULT_LEAD_IN_MS)))
        self.lead_spin.valueChanged.connect(self.on_lead_changed)
        self.save_srt_btn = QPushButton("🎬 שמור כ-SRT")
        self.save_srt_btn.setEnabled(False)
        self.save_srt_btn.clicked.connect(self.save_srt)
        clear_btn = QPushButton("נקה")
        clear_btn.clicked.connect(self.clear_output)
        self.status = QLabel("")
        self.op_label = QLabel("")
        self.op_label.setStyleSheet("color:#555;")
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)  # indeterminate
        self.progress.setFixedWidth(120)
        self.progress.hide()
        bottom.addWidget(copy_btn)
        bottom.addWidget(save_btn)
        bottom.addWidget(lead_label)
        bottom.addWidget(self.lead_spin)
        bottom.addWidget(self.save_srt_btn)
        bottom.addWidget(clear_btn)
        bottom.addStretch()
        bottom.addWidget(self.op_label)
        bottom.addWidget(self.status)
        bottom.addWidget(self.progress)
        root.addLayout(bottom)

        self.update_total_label()

        if not self.get_api_key():
            self.set_status("לא הוגדר מפתח API – לחץ על ⚙ מפתח API")

    # ---------- helpers ----------
    def set_status(self, msg):
        self.status.setText(msg)

    def on_srt_toggled(self, checked):
        self.cfg["srt"] = checked
        save_config(self.cfg)

    def on_lang_changed(self, _index):
        self.cfg["lang"] = self.lang_combo.currentData()
        save_config(self.cfg)

    def on_lead_changed(self, value):
        self.cfg["srt_lead_in_ms"] = value
        save_config(self.cfg)

    def set_segments(self, segments):
        self.segments = segments
        self.save_srt_btn.setEnabled(bool(segments))

    def clear_output(self):
        self.text.clear()
        self.set_segments([])

    def get_api_key(self):
        return clean_key(self.cfg.get("api_key") or os.environ.get("GEMINI_API_KEY", ""))

    def ask_api_key(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("מפתח Gemini API")
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel("הדבק את מפתח ה-API של Gemini:"))
        edit = QLineEdit(self.cfg.get("api_key", ""))
        edit.setMinimumWidth(360)
        layout.addWidget(edit)
        link_btn = QPushButton("🔑 קבלת מפתח ב-Google AI Studio")
        link_btn.setToolTip("פתיחת דף מפתחות ה-API של Google AI Studio בדפדפן:\n"
                            "יצירת מפתח חדש (Create API key) או העתקת מפתח קיים.")
        link_btn.setCursor(Qt.PointingHandCursor)
        link_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(API_KEYS_URL)))
        layout.addWidget(link_btn)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        if dlg.exec() == QDialog.Accepted:
            key = clean_key(edit.text())
            self.cfg["api_key"] = key
            save_config(self.cfg)
            self.set_status("המפתח נשמר" if key else "המפתח נמחק")

    # ---------- drag & drop on the whole window ----------
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        urls = e.mimeData().urls()
        if urls:
            self.load_file(urls[0].toLocalFile())

    # ---------- file loading ----------
    def browse(self):
        if self.worker:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "בחר קובץ אודיו", "",
            "קבצי אודיו (*.mp3 *.wav *.m4a *.ogg *.flac *.aac);;כל הקבצים (*.*)")
        if path:
            self.load_file(path)

    def load_file(self, path):
        if self.worker:
            return
        p = Path(path)
        if p.suffix.lower() not in AUDIO_EXTS:
            QMessageBox.warning(self, "קובץ לא נתמך",
                                f"סוג הקובץ {p.suffix} אינו נתמך.\nיש לבחור קובץ MP3.")
            return
        self.file_path = p
        self.set_segments([])  # old subtitles belong to the previous file
        size_mb = p.stat().st_size / (1024 * 1024)
        self.drop.setText(f"🎵 {p.name}\n({size_mb:.1f} MB) – גרור קובץ אחר להחלפה")
        self.convert_btn.setEnabled(True)
        self.set_status("הקובץ נטען. לחץ על 'המר לטקסט'")

    # ---------- transcription ----------
    def convert(self):
        if not self.file_path or self.worker:
            return
        if not self.get_api_key():
            self.ask_api_key()
            if not self.get_api_key():
                return
        self.convert_btn.setEnabled(False)
        self.progress.show()
        self.set_status("מעלה ומתמלל... (עשוי לקחת זמן בקבצים ארוכים)")
        self.worker = TranscribeWorker(self.get_api_key(), self.cfg.get("model", DEFAULT_MODEL),
                                       self.file_path, self.srt_check.isChecked(),
                                       self.lang_combo.currentData())
        self.worker.finished_ok.connect(self.on_done)
        self.worker.failed.connect(self.on_error)
        self.worker.start()

    def _finish(self):
        self.progress.hide()
        self.convert_btn.setEnabled(True)
        self.worker.deleteLater()
        self.worker = None

    def on_done(self, text, usage, segments):
        self._finish()
        self.text.setPlainText(text)
        self.set_segments(segments)
        extra = f", {len(segments)} כתוביות" if segments else ""
        self.set_status(f"הסתיים ✔ ({len(text.split())} מילים{extra})")

        model = self.cfg.get("model", DEFAULT_MODEL)
        cost = usage_cost(usage, self.prices_for(model))
        self.op_label.setText("פעולה זו: " + self.format_usage(usage["total"], cost))
        self.op_label.setToolTip(
            f"מודל: {model}\nאודיו נכנס: {usage['audio_in']:,}\n"
            f"טקסט נכנס: {usage['text_in']:,}\nפלט: {usage['out']:,}")

        totals = self.cfg.setdefault("totals", {"tokens": 0, "usd": 0.0, "runs": 0})
        totals["tokens"] += usage["total"]
        totals["usd"] += cost or 0.0
        totals["runs"] += 1
        save_config(self.cfg)
        self.update_total_label()

    # ---------- token / cost display ----------
    def prices_for(self, model):
        prices = {**PRICES, **self.cfg.get("prices", {})}
        return prices.get(model)

    def format_usage(self, tokens, usd):
        s = f"{tokens:,} טוקנים"
        if usd is not None:
            ils = usd * self.cfg.get("usd_to_ils", DEFAULT_USD_TO_ILS)
            # LRI..PDI isolate keeps the money part in LTR order inside the RTL label
            s += f" · ⁦${usd:.4f} ≈ ₪{ils:.3f}⁩"
        return s

    def update_total_label(self):
        t = self.cfg.get("totals", {"tokens": 0, "usd": 0.0, "runs": 0})
        self.total_label.setText("סה״כ: " + self.format_usage(t["tokens"], t["usd"]))
        self.total_label.setToolTip(
            f"{t['runs']} המרות\nהמחיר לפי שכבה בתשלום. בשכבה החינמית אין חיוב.\n"
            f"שער ₪ משוער: {self.cfg.get('usd_to_ils', DEFAULT_USD_TO_ILS)}")

    def reset_totals(self):
        if QMessageBox.question(self, "איפוס", "לאפס את הסכום המצטבר?") == QMessageBox.Yes:
            self.cfg["totals"] = {"tokens": 0, "usd": 0.0, "runs": 0}
            save_config(self.cfg)
            self.update_total_label()

    def on_error(self, msg):
        self._finish()
        self.set_status("ההמרה נכשלה")
        QMessageBox.critical(self, "שגיאה", f"ההמרה נכשלה:\n{msg}")

    # ---------- output ----------
    def copy_all(self):
        content = self.text.toPlainText()
        if not content.strip():
            self.set_status("אין טקסט להעתקה")
            return
        QGuiApplication.clipboard().setText(content)
        self.set_status("הטקסט הועתק ללוח ✔")

    def save_text(self):
        content = self.text.toPlainText()
        if not content.strip():
            return
        # default: same folder and base name as the loaded audio file
        if self.file_path:
            default = str(self.file_path.with_suffix(".txt"))
        else:
            default = "transcript.txt"
        path, _ = QFileDialog.getSaveFileName(self, "שמירת תמלול", default, "Text (*.txt)")
        if path:
            Path(path).write_text(content, encoding="utf-8")
            self.set_status(f"נשמר: {Path(path).name}")

    def save_srt(self):
        if not self.segments:
            return
        default = str(self.file_path.with_suffix(".srt")) if self.file_path else "subtitles.srt"
        path, _ = QFileDialog.getSaveFileName(self, "שמירת כתוביות", default, "SubRip (*.srt)")
        if path:
            # UTF-8 with BOM so Hebrew shows correctly in every player/editor
            srt = segments_to_srt(self.segments, self.lead_spin.value() / 1000)
            Path(path).write_text(srt, encoding="utf-8-sig")
            self.set_status(f"נשמר: {Path(path).name}")


def main():
    app = QApplication(sys.argv)
    app.setLayoutDirection(Qt.RightToLeft)
    app.setFont(QFont("Segoe UI", 10))
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
