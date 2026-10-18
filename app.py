"""Mp3toText - desktop app that transcribes MP3 files to text using Gemini."""

import json
import os
import re
import sys
import time
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont, QGuiApplication
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit,
    QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton, QSlider, QSpinBox, QStyle, QTabWidget, QTextEdit,
    QVBoxLayout, QWidget,
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
RTL_LANGS = {"he"}


def lang_instruction(lang):
    name = LANGUAGES[lang][1]
    return (f"The audio is spoken in {name}. Write the transcription in {name}, exactly as spoken. "
            f"Never translate it into any other language. ")


# Subtitles come back as JSON, where an ASCII " ends the text string: the model cut every
# Hebrew abbreviation there (רמב"ם -> "...הרמב" + next subtitle "ם ...", likewise תנ"ך, י"ב).
# Plain-text transcription has no JSON, so this is added to subtitle requests only.
HEBREW_JSON_QUOTES = ("Write Hebrew abbreviations with the Hebrew gershayim ״ and geresh ׳ "
                      "characters (e.g. רמב״ם, תנ״ך, ג׳), never with ASCII quote marks. ")


def duration_instruction(seconds):
    if not seconds:
        return ""
    m, s = divmod(int(round(seconds)), 60)
    return f"The audio is {m}:{s:02d} (minutes:seconds) long; every timestamp must be within it. "


PROMPT = (
    "Transcribe this audio file verbatim. "
    "Return only the transcription text, with paragraph breaks where natural. "
    "Do not add any commentary, headings or timestamps."
)

SRT_PROMPT = (
    "Transcribe this audio file verbatim, as subtitles. "
    "Split the speech into short subtitle segments (one or two short sentences, at most about "
    "80 characters each). For every segment give the exact time it is spoken in the audio: "
    "'start' and 'end' in the format MM:SS.mmm, with one colon and a dot before the milliseconds "
    "(e.g. 00:07.400 is 7.4 seconds and 12:05.250 is 12 minutes 5.25 seconds; use HH:MM:SS.mmm "
    "only for audio longer than an hour), measured from the very beginning of the file. 'start' is the exact moment the first word "
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

# "Translate everything" matters: without it the model drops content it judges to be noise,
# e.g. "1 2 3 שלום עולם." came back as just "Hello world." (reproduced 3/3 with gemini-2.5-flash).
TRANSLATE_PROMPT = (
    "Translate the following {src} transcript into {tgt}. Translate everything that is in it: "
    "keep every number, count, name and repeated or filler word. Never omit, summarize or "
    "'clean up' anything. Keep paragraph breaks. Return only the translation, without commentary."
)
TRANSLATE_SRT_PROMPT = (
    "Translate these subtitles from {src} into {tgt}. The input is a JSON array of objects "
    "with 'id' and 'text'. Return a JSON array with exactly the same ids, each with the "
    "translated text. Translate everything in each subtitle: keep every number, count, name "
    "and repeated or filler word, and never omit, summarize or 'clean up' anything. Use the "
    "neighbouring subtitles as context, and never merge, split, skip or reorder items."
)
TRANSLATE_SRT_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {"id": {"type": "INTEGER"}, "text": {"type": "STRING"}},
        "required": ["id", "text"],
    },
}
SRT_TRANSLATE_BATCH = 100  # subtitles per request, so long files don't hit the output limit

# Gemini timestamps run late; subtitles start this much earlier when saving (adjustable in the UI).
DEFAULT_LEAD_IN_MS = 200

# USD per 1M tokens, paid tier (ai.google.dev/gemini-api/docs/pricing).
# Override or add models via "prices" in config.json.
PRICES = {
    "gemini-2.5-flash": {"audio_in": 1.00, "text_in": 0.30, "out": 2.50},
}
DEFAULT_USD_TO_ILS = 3.7  # approximate; override via "usd_to_ils" in config.json
BILLING_URL = "https://console.cloud.google.com/billing"
RATE_LIMIT_URL = "https://ai.dev/rate-limit"
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


def parse_timestamp(value, three_part="hms"):
    """'MM:SS.mmm', 'HH:MM:SS,mmm', '12.5' or a number -> seconds (float), or None.

    A three-part value like '00:07:40' is ambiguous: H:M:S (7 min 40 s), or what Gemini
    sometimes means, M:S:fraction (7.40 s). three_part="msf" picks the second reading.
    """
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", ".")
    if not s:
        return None
    parts = s.split(":")
    try:
        if len(parts) == 3 and three_part == "msf":
            frac = parts[2].split(".")[0]
            return int(parts[0]) * 60 + float(parts[1]) + (int(frac) / 10 ** len(frac) if frac else 0.0)
        secs = 0.0
        for part in parts:
            secs = secs * 60 + float(part)
        return secs
    except ValueError:
        return None


def parse_segments(raw, duration=None):
    """Gemini JSON reply -> sorted, sanitized list of (start, end, text) in seconds.

    Tolerates truncated JSON (long files hitting the output limit) by salvaging
    the complete objects that were received. With the audio duration known,
    three-part timestamps that would land far past the end of the file are re-read
    as M:S:fraction (a 20-minute file once came back ending at "20:17:40").
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

    def build(three_part):
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            text = " ".join(str(it.get("text", "")).split())
            start = parse_timestamp(it.get("start"), three_part)
            if not text or start is None:
                continue
            out.append([start, parse_timestamp(it.get("end"), three_part), text])
        return out

    segs = build("hms")
    if duration and segs and max(s[0] for s in segs) > duration * 1.1:
        alt = build("msf")
        if alt and max(s[0] for s in alt) <= duration * 1.1:
            segs = alt
    segs.sort(key=lambda s: s[0])

    for i, seg in enumerate(segs):
        nxt = segs[i + 1][0] if i + 1 < len(segs) else None
        end = seg[1]
        if end is None or end <= seg[0]:
            end = seg[0] + DEFAULT_SEG_SECONDS
        if nxt is not None and end > nxt:  # no overlap with the next subtitle
            end = max(nxt, seg[0] + MIN_SEG_SECONDS)
        if duration:
            end = max(min(end, duration), seg[0] + MIN_SEG_SECONDS)
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


def rtl_line(text):
    """Force right-to-left display of a subtitle line in players that default to LTR.

    RLM at both ends makes the first strong character RTL and keeps edge punctuation
    on the correct side; the RLE..PDF embedding keeps mixed Hebrew/English word order.
    """
    return f"‏‫{text}‬‏"


def segments_to_srt(segments, lead_in=0.0, rtl=False):
    if lead_in:
        segments = apply_lead_in(segments, lead_in)
    blocks = [f"{i}\n{format_srt_time(a)} --> {format_srt_time(b)}\n{rtl_line(text) if rtl else text}\n"
              for i, (a, b, text) in enumerate(segments, 1)]
    return "\n".join(blocks)


def parse_translations(raw):
    """Gemini JSON reply [{id, text}] -> {id: text}; salvages complete objects from truncated JSON."""
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        items = []
        for m in re.finditer(r"\{[^{}]*\}", raw):
            try:
                items.append(json.loads(m.group(0)))
            except json.JSONDecodeError:
                pass
    out = {}
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        try:
            i = int(it.get("id"))
        except (TypeError, ValueError):
            continue
        text = " ".join(str(it.get("text", "")).split())
        if text:
            out[i] = text
    return out


def call_with_quota_retry(fn, notify, attempts=4):
    """Run fn(); on a per-minute quota error (HTTP 429, e.g. free tier's 5 requests/minute)
    wait the delay Gemini asks for and try again. Daily quotas are not retried."""
    from google.genai import errors

    for attempt in range(attempts):
        try:
            return fn()
        except errors.ClientError as e:
            msg = str(e)
            if getattr(e, "code", None) != 429 or attempt == attempts - 1 or "PerDay" in msg:
                raise
            m = re.search(r"retry in ([\d.]+)\s*s", msg)
            delay = min(float(m.group(1)) if m else 30.0, 90.0) + 1
            notify(f"הגעת למכסת הבקשות לדקה (מסלול חינמי?) – ממתין {int(delay)} שניות ומנסה שוב...")
            time.sleep(delay)


def add_usage(total, usage):
    for k in ("audio_in", "text_in", "out", "total"):
        total[k] = total.get(k, 0) + usage.get(k, 0)
    return total


class TranscribeWorker(QThread):
    finished_ok = Signal(str, dict, list)  # text, usage, segments (empty unless SRT requested)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, key, model, path, want_srt=False, lang=DEFAULT_LANG, duration=None):
        super().__init__()
        self.key, self.model, self.path, self.want_srt = key, model, path, want_srt
        self.lang = lang
        self.duration = duration  # seconds, or None if the player hasn't read it yet

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
                if self.want_srt:
                    prompt = (lang_instruction(self.lang)
                              + (HEBREW_JSON_QUOTES if self.lang == "he" else "")
                              + duration_instruction(self.duration) + SRT_PROMPT)
                else:
                    prompt = lang_instruction(self.lang) + PROMPT
                resp = call_with_quota_retry(lambda: client.models.generate_content(
                    model=self.model, contents=[prompt, uploaded], config=gen_cfg), self.progress.emit)
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
                segments = parse_segments(text, self.duration)
                if not segments:
                    raise RuntimeError("לא ניתן היה לפענח כתוביות מתשובת המודל.")
                text = "\n".join(s[2] for s in segments)
            self.finished_ok.emit(text, extract_usage(resp), segments)
        except Exception as e:
            self.failed.emit(str(e))


class TranslateWorker(QThread):
    """Translates the finished transcript (text only, no audio upload) and, if present, the subtitles."""

    finished_ok = Signal(str, dict, list)  # translated text, usage, translated segments
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, key, model, src, tgt, text, segments):
        super().__init__()
        self.key, self.model = key, model
        self.src, self.tgt = LANGUAGES[src][1], LANGUAGES[tgt][1]
        self.text, self.segments = text, segments

    def _config(self, types, json_schema=None):
        cfg = types.GenerateContentConfig(temperature=0, max_output_tokens=65536)
        if json_schema:
            cfg.response_mime_type = "application/json"
            cfg.response_schema = json_schema
        if "flash" in self.model:
            cfg.thinking_config = types.ThinkingConfig(thinking_budget=0)
        return cfg

    def _translate_batch(self, client, types, items, usage):
        """items: [(id, text)] -> {id: translated}; one retry for ids the model skipped."""
        prompt = TRANSLATE_SRT_PROMPT.format(src=self.src, tgt=self.tgt)
        result = {}
        todo = items
        for _attempt in range(2):
            payload = json.dumps([{"id": i, "text": t} for i, t in todo], ensure_ascii=False)
            resp = call_with_quota_retry(lambda: client.models.generate_content(
                model=self.model, contents=[prompt, payload],
                config=self._config(types, TRANSLATE_SRT_SCHEMA)), self.progress.emit)
            add_usage(usage, extract_usage(resp))
            got = parse_translations(resp.text or "")
            wanted = {i for i, _ in todo}
            result.update({i: t for i, t in got.items() if i in wanted})
            todo = [(i, t) for i, t in todo if i not in result]
            if not todo:
                break
        return result

    def run(self):
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=self.key)
            usage = {}
            new_segments = []
            if self.segments:
                items = [(i, seg[2]) for i, seg in enumerate(self.segments, 1)]
                batches = [items[k:k + SRT_TRANSLATE_BATCH] for k in range(0, len(items), SRT_TRANSLATE_BATCH)]
                translated = {}
                for n, batch in enumerate(batches, 1):
                    self.progress.emit(f"מתרגם כתוביות... (חלק {n} מתוך {len(batches)})")
                    translated.update(self._translate_batch(client, types, batch, usage))
                # a subtitle the model never returned stays in the source language
                new_segments = [(a, b, translated.get(i, text))
                                for i, (a, b, text) in enumerate(self.segments, 1)]

            source_text = "\n".join(s[2] for s in self.segments)
            if self.segments and self.text.strip() == source_text.strip():
                # transcript wasn't edited: the subtitle translation already is the text translation
                text = "\n".join(s[2] for s in new_segments)
            else:
                self.progress.emit("מתרגם טקסט...")
                resp = call_with_quota_retry(lambda: client.models.generate_content(
                    model=self.model,
                    contents=[TRANSLATE_PROMPT.format(src=self.src, tgt=self.tgt), self.text],
                    config=self._config(types)), self.progress.emit)
                add_usage(usage, extract_usage(resp))
                text = (resp.text or "").strip()
                if not text:
                    raise RuntimeError("לא התקבל תרגום מהמודל.")
            self.finished_ok.emit(text, usage, new_segments)
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


def format_clock(ms):
    """Milliseconds -> 'M:SS' (or 'H:MM:SS' for an hour or more)."""
    s = max(int(ms), 0) // 1000
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class SeekSlider(QSlider):
    """Horizontal slider that jumps straight to the clicked point (and can then be dragged)."""

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            value = QStyle.sliderValueFromPosition(
                self.minimum(), self.maximum(), int(e.position().x()), self.width())
            self.setValue(value)
            self.sliderMoved.emit(value)
        super().mousePressEvent(e)


class AudioPlayer(QWidget):
    """Play/Pause, Stop, seek bar and elapsed/total time for the loaded audio file."""

    def __init__(self):
        super().__init__()
        # media controls read left-to-right even in a Hebrew UI (time flows left -> right)
        self.setLayoutDirection(Qt.LeftToRight)

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)

        self.play_btn = QPushButton()
        self.play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.play_btn.setToolTip("ניגון / השהיה")
        self.play_btn.clicked.connect(self.toggle_play)
        self.stop_btn = QPushButton()
        self.stop_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaStop))
        self.stop_btn.setToolTip("עצירה וחזרה להתחלה")
        self.stop_btn.clicked.connect(self.player.stop)

        self.slider = SeekSlider(Qt.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.setToolTip("לחץ או גרור כדי לקפוץ לנקודה בקובץ")
        self.slider.sliderMoved.connect(self.on_slider_moved)
        self.slider.sliderReleased.connect(lambda: self.player.setPosition(self.slider.value()))

        self.time_label = QLabel("0:00 / 0:00")
        self.time_label.setMinimumWidth(110)
        self.time_label.setAlignment(Qt.AlignCenter)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.play_btn)
        row.addWidget(self.stop_btn)
        row.addWidget(self.slider, 1)
        row.addWidget(self.time_label)

        self.player.positionChanged.connect(self.on_position)
        self.player.durationChanged.connect(self.on_duration)
        self.player.playbackStateChanged.connect(self.on_state)
        self.set_enabled(False)

    def set_enabled(self, on):
        for w in (self.play_btn, self.stop_btn, self.slider):
            w.setEnabled(on)

    def load(self, path):
        self.player.stop()
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.slider.setValue(0)
        self.set_enabled(True)

    def toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def on_slider_moved(self, value):
        # while dragging, show the target time; the actual seek happens on release
        self.time_label.setText(f"{format_clock(value)} / {format_clock(self.player.duration())}")

    def on_position(self, pos):
        if not self.slider.isSliderDown():
            self.slider.setValue(pos)
            self.time_label.setText(f"{format_clock(pos)} / {format_clock(self.player.duration())}")

    def on_duration(self, dur):
        self.slider.setRange(0, dur)
        self.time_label.setText(f"{format_clock(self.player.position())} / {format_clock(dur)}")

    def on_state(self, state):
        icon = QStyle.SP_MediaPause if state == QMediaPlayer.PlayingState else QStyle.SP_MediaPlay
        self.play_btn.setIcon(self.style().standardIcon(icon))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.file_path = None
        self.worker = None
        self.segments = []  # (start, end, text) from the last SRT-enabled run
        self.source_lang = None  # language of the current transcript
        self.pending_lang = None  # language of the transcription in progress
        self.trans_lang = None  # language of the current translation
        self.trans_segments = []  # translated subtitles, same timestamps as self.segments

        self.setWindowTitle("Mp3toText – המרת אודיו לטקסט")
        self.resize(1060, 680)
        self.setAcceptDrops(True)  # dropping anywhere on the window also works

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 2)  # the status line below supplies the bottom edge
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
        billing_btn.setToolTip("קישורים לחיוב ולמעקב אחר ניצול המכסה (נפתחים בדפדפן)")
        billing_btn.setCursor(Qt.PointingHandCursor)
        billing_menu = QMenu(billing_btn)
        billing_menu.setToolTipsVisible(True)
        act = billing_menu.addAction("💳 חיוב – Google Cloud")
        act.setToolTip("החיוב בפועל, אמצעי התשלום וקביעת תקציב/התראות.\n"
                       "הסכום המוצג באפליקציה הוא הערכה בלבד.")
        act.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(BILLING_URL)))
        act = billing_menu.addAction("📊 מעקב אחר ניצול המכסה")
        act.setToolTip("כמה בקשות נוצלו מהמכסה לדקה וליום (במסלול החינמי: 20 בקשות ביום למודל),\n"
                       "ומתי היא מתאפסת.")
        act.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(RATE_LIMIT_URL)))
        billing_btn.setMenu(billing_menu)
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

        # --- audio player for the loaded file ---
        self.audio_player = AudioPlayer()
        root.addWidget(self.audio_player)

        # --- text output: Qt picks direction per paragraph (Hebrew RTL, English LTR) ---
        self.text = QTextEdit()
        self.text.setAcceptRichText(False)
        self.text.setFont(QFont("Segoe UI", 12))
        self.text.setPlaceholderText("כאן יופיע התמלול")
        self.trans_text = QTextEdit()
        self.trans_text.setAcceptRichText(False)
        self.trans_text.setFont(QFont("Segoe UI", 12))
        self.trans_text.setPlaceholderText("כאן יופיע התרגום")
        self.tabs = QTabWidget()
        self.tabs.addTab(self.text, "מקור")
        self.tabs.addTab(self.trans_text, "תרגום")
        self.tabs.setTabEnabled(1, False)
        self.tabs.currentChanged.connect(self.update_output_buttons)

        # translate controls sit at the far end of the tab bar
        corner = QWidget()
        corner_row = QHBoxLayout(corner)
        corner_row.setContentsMargins(0, 0, 0, 0)
        self.target_combo = QComboBox()
        self.target_combo.setToolTip("שפת היעד לתרגום")
        self.target_combo.currentIndexChanged.connect(self.on_target_changed)
        self.translate_btn = QPushButton("🌐 תרגם")
        self.translate_btn.setToolTip(
            "מתרגם את התמלול (וגם את הכתוביות, אם נוצרו) לשפה שנבחרה.\n"
            "נשלח רק טקסט, בלי האודיו, ולכן זה מהיר וזול. התזמון של הכתוביות נשמר.")
        self.translate_btn.setEnabled(False)
        self.translate_btn.clicked.connect(self.translate)
        corner_row.addWidget(QLabel("תרגם ל:"))
        corner_row.addWidget(self.target_combo)
        corner_row.addWidget(self.translate_btn)
        self.tabs.setCornerWidget(corner, Qt.TopRightCorner)
        # by default the corner is squeezed below the button's natural height, clipping its bottom edge
        corner.setMinimumHeight(self.translate_btn.sizeHint().height() + 2)
        root.addWidget(self.tabs, 1)

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
        self.rtl_check = QCheckBox("RTL לנגנים")
        self.rtl_check.setToolTip(
            "בכתוביות בעברית: מוסיף לכל שורה סימני כיוון בלתי נראים,\n"
            "כדי שנגנים יציגו את הנקודה ואת סימן השאלה בצד הנכון.")
        self.rtl_check.setChecked(bool(self.cfg.get("srt_rtl_fix", True)))
        self.rtl_check.toggled.connect(self.on_rtl_toggled)
        clear_btn = QPushButton("נקה")
        clear_btn.clicked.connect(self.clear_output)
        self.status = QLabel("")
        self.op_label = QLabel("")
        self.op_label.setStyleSheet("color:#555;")
        # long status/cost texts may be clipped instead of widening the window (full text in tooltip)
        for label in (self.status, self.op_label, self.total_label):
            label.setMinimumWidth(1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)  # indeterminate
        self.progress.setFixedWidth(120)
        self.progress.hide()
        bottom.addWidget(copy_btn)
        bottom.addWidget(save_btn)
        bottom.addWidget(lead_label)
        bottom.addWidget(self.lead_spin)
        bottom.addWidget(self.save_srt_btn)
        bottom.addWidget(self.rtl_check)
        bottom.addWidget(clear_btn)
        bottom.addStretch()
        root.addLayout(bottom)

        # --- status line: one thin row at the bottom of the window ---
        bar = self.statusBar()
        bar.setSizeGripEnabled(False)
        bar.setContentsMargins(9, 0, 9, 0)  # line its text up with the buttons above
        bar.addWidget(self.status, 1)
        bar.addPermanentWidget(self.op_label)
        bar.addPermanentWidget(self.progress)

        self.update_total_label()
        self.refresh_targets()

        if not self.get_api_key():
            self.set_status("לא הוגדר מפתח API – לחץ על ⚙ מפתח API")

    # ---------- helpers ----------
    def set_status(self, msg):
        self.status.setText(msg)
        self.status.setToolTip(msg)

    def on_srt_toggled(self, checked):
        self.cfg["srt"] = checked
        save_config(self.cfg)

    def on_lang_changed(self, _index):
        self.cfg["lang"] = self.lang_combo.currentData()
        save_config(self.cfg)

    def on_lead_changed(self, value):
        self.cfg["srt_lead_in_ms"] = value
        save_config(self.cfg)

    def on_rtl_toggled(self, checked):
        self.cfg["srt_rtl_fix"] = checked
        save_config(self.cfg)

    def on_target_changed(self, _index):
        if self.target_combo.currentData():
            self.cfg["translate_to"] = self.target_combo.currentData()
            save_config(self.cfg)

    def set_segments(self, segments):
        self.segments = segments
        self.update_output_buttons()

    def current_output(self):
        """(text box, subtitles, language) of the tab currently shown."""
        if self.tabs.currentIndex() == 1:
            return self.trans_text, self.trans_segments, self.trans_lang
        return self.text, self.segments, self.source_lang

    def update_output_buttons(self, *_):
        self.save_srt_btn.setEnabled(bool(self.current_output()[1]))

    def refresh_targets(self):
        """Fill "translate to" with every language except the transcript's own."""
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        for code, (label, _name) in LANGUAGES.items():
            if code != self.source_lang:
                self.target_combo.addItem(label, code)
        idx = self.target_combo.findData(self.cfg.get("translate_to"))
        self.target_combo.setCurrentIndex(max(idx, 0))
        self.target_combo.blockSignals(False)
        self.translate_btn.setEnabled(bool(self.source_lang) and not self.worker)

    def reset_translation(self):
        self.trans_text.clear()
        self.trans_segments = []
        self.trans_lang = None
        self.tabs.setTabText(1, "תרגום")
        self.tabs.setTabEnabled(1, False)
        self.tabs.setCurrentIndex(0)
        self.update_output_buttons()

    def clear_output(self):
        self.text.clear()
        self.set_segments([])
        self.reset_translation()

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
        self.reset_translation()
        self.source_lang = None
        self.tabs.setTabText(0, "מקור")
        self.refresh_targets()
        self.audio_player.load(p)
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
        self.pending_lang = self.lang_combo.currentData()
        self.worker = TranscribeWorker(self.get_api_key(), self.cfg.get("model", DEFAULT_MODEL),
                                       self.file_path, self.srt_check.isChecked(), self.pending_lang,
                                       self.audio_player.player.duration() / 1000 or None)
        self.worker.finished_ok.connect(self.on_done)
        self.worker.progress.connect(self.set_status)
        self._start("מעלה ומתמלל... (עשוי לקחת זמן בקבצים ארוכים)")

    def _start(self, status):
        """Common start for transcription/translation workers (self.worker must be set)."""
        self.convert_btn.setEnabled(False)
        self.translate_btn.setEnabled(False)
        self.progress.show()
        self.set_status(status)
        self.worker.failed.connect(self.on_error)
        self.worker.start()

    def _finish(self):
        self.progress.hide()
        self.convert_btn.setEnabled(bool(self.file_path))
        self.worker.deleteLater()
        self.worker = None
        self.translate_btn.setEnabled(bool(self.source_lang))

    def on_done(self, text, usage, segments):
        self.source_lang = self.pending_lang
        self._finish()
        self.text.setPlainText(text)
        self.set_segments(segments)
        self.reset_translation()
        self.tabs.setTabText(0, f"מקור ({LANGUAGES[self.source_lang][0]})")
        self.refresh_targets()
        extra = f", {len(segments)} כתוביות" if segments else ""
        self.set_status(f"הסתיים ✔ ({len(text.split())} מילים{extra})")
        self.record_usage(usage)

    # ---------- translation ----------
    def translate(self):
        if self.worker or not self.source_lang:
            return
        text = self.text.toPlainText()
        if not text.strip():
            self.set_status("אין טקסט לתרגום")
            return
        if not self.get_api_key():
            self.ask_api_key()
            if not self.get_api_key():
                return
        tgt = self.target_combo.currentData()
        self.worker = TranslateWorker(self.get_api_key(), self.cfg.get("model", DEFAULT_MODEL),
                                      self.source_lang, tgt, text, list(self.segments))
        self.worker.tgt_code = tgt
        self.worker.progress.connect(self.set_status)
        self.worker.finished_ok.connect(self.on_translated)
        self._start(f"מתרגם ל{LANGUAGES[tgt][0]}...")

    def on_translated(self, text, usage, segments):
        self.trans_lang = self.worker.tgt_code
        self._finish()
        self.trans_text.setPlainText(text)
        self.trans_segments = segments
        self.tabs.setTabText(1, f"תרגום ({LANGUAGES[self.trans_lang][0]})")
        self.tabs.setTabEnabled(1, True)
        self.tabs.setCurrentIndex(1)
        self.update_output_buttons()
        kept = sum(1 for (_, _, t), (_, _, o) in zip(segments, self.segments) if t == o)
        extra = f", {len(segments)} כתוביות" if segments else ""
        warn = f" ⚠ {kept} כתוביות זהות למקור (ייתכן שלא תורגמו)" if kept else ""
        self.set_status(f"התרגום הסתיים ✔ ({len(text.split())} מילים{extra}){warn}")
        self.record_usage(usage)

    def record_usage(self, usage):
        """Show the cost of the last operation and add it to the running total."""
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
            f"{t['runs']} פעולות (תמלול / תרגום)\nהמחיר לפי שכבה בתשלום. בשכבה החינמית אין חיוב.\n"
            f"שער ₪ משוער: {self.cfg.get('usd_to_ils', DEFAULT_USD_TO_ILS)}")

    def reset_totals(self):
        if QMessageBox.question(self, "איפוס", "לאפס את הסכום המצטבר?") == QMessageBox.Yes:
            self.cfg["totals"] = {"tokens": 0, "usd": 0.0, "runs": 0}
            save_config(self.cfg)
            self.update_total_label()

    def on_error(self, msg):
        self._finish()
        self.set_status("הפעולה נכשלה")
        QMessageBox.critical(self, "שגיאה", f"הפעולה נכשלה:\n{msg}")

    # ---------- output ----------
    def copy_all(self):
        content = self.current_output()[0].toPlainText()
        if not content.strip():
            self.set_status("אין טקסט להעתקה")
            return
        QGuiApplication.clipboard().setText(content)
        self.set_status("הטקסט הועתק ללוח ✔")

    def default_save_path(self, ext, fallback):
        """Same folder and base name as the audio file; a translation adds its language
        code (lecture.en.srt), which players such as VLC recognise."""
        if not self.file_path:
            return fallback + ext
        lang_tag = f".{self.trans_lang}" if self.tabs.currentIndex() == 1 else ""
        return str(self.file_path.with_name(self.file_path.stem + lang_tag + ext))

    def save_text(self):
        content = self.current_output()[0].toPlainText()
        if not content.strip():
            return
        default = self.default_save_path(".txt", "transcript")
        path, _ = QFileDialog.getSaveFileName(self, "שמירת טקסט", default, "Text (*.txt)")
        if path:
            Path(path).write_text(content, encoding="utf-8")
            self.set_status(f"נשמר: {Path(path).name}")

    def save_srt(self):
        _edit, segments, lang = self.current_output()
        if not segments:
            return
        default = self.default_save_path(".srt", "subtitles")
        path, _ = QFileDialog.getSaveFileName(self, "שמירת כתוביות", default, "SubRip (*.srt)")
        if path:
            rtl = self.rtl_check.isChecked() and lang in RTL_LANGS
            # UTF-8 with BOM so Hebrew shows correctly in every player/editor
            srt = segments_to_srt(segments, self.lead_spin.value() / 1000, rtl=rtl)
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
