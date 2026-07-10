"""tgbridge — медиа: извлечение аудиодорожки (ffmpeg) и распознавание речи (whisper).

Зависит только от tgconfig (константы + log) и системных бинарей — состояния не держит.
"""
import os, subprocess
from tgconfig import log, FFMPEG_BIN, WHISPER_BIN, WHISPER_MODEL, WHISPER_LANG


def extract_audio(video_path):
    """Вытащить аудиодорожку в .m4a (без перекодирования; fallback aac). Вернуть путь или None.
    Смысл — дисковая гигиена: тяжёлое видео потом удаляем, лёгкий звук оставляем для распознавания."""
    if not os.path.isfile(FFMPEG_BIN):
        log("ffmpeg not found:", FFMPEG_BIN); return None
    base = os.path.splitext(video_path)[0]
    out = base + ".m4a"
    attempts = [
        [FFMPEG_BIN, "-y", "-i", video_path, "-vn", "-c:a", "copy", out],            # быстро, без перекодирования
        [FFMPEG_BIN, "-y", "-i", video_path, "-vn", "-ac", "1", "-c:a", "aac", "-b:a", "48k", out],
    ]
    last_err = ""
    for cmd in attempts:
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except Exception as e:
            last_err = repr(e); log("ffmpeg err", repr(e)); continue
        if p.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 0:
            return out
        last_err = (p.stderr or "").strip()[-300:]
    log("extract_audio: ffmpeg не дал звук из", os.path.basename(video_path), "| stderr:", last_err)
    return None


def transcribe(path):
    """Распознать речь через whisper (env ml13). Вернуть текст или None."""
    if not os.path.isfile(WHISPER_BIN):
        log("whisper not found:", WHISPER_BIN); return None
    env = dict(os.environ, PATH=os.path.dirname(WHISPER_BIN) + ":" + os.environ.get("PATH", ""))
    cmd = [WHISPER_BIN, path, "--model", WHISPER_MODEL, "--output_format", "txt",
           "--output_dir", os.path.dirname(path), "--fp16", "False", "--verbose", "False"]
    if WHISPER_LANG and WHISPER_LANG.lower() != "auto":
        cmd += ["--language", WHISPER_LANG]
    try:
        subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)  # 1-й раз качает модель
    except Exception as e:
        log("whisper err", repr(e)); return None
    txt = os.path.splitext(path)[0] + ".txt"
    try:
        return open(txt, encoding="utf-8").read().strip() if os.path.isfile(txt) else None
    except Exception:
        return None
