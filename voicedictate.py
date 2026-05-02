#!/usr/bin/env python3
"""
voicedictate – Pressione Alt+Z para começar a gravar; pressione de novo para parar e transcrever.

Env overrides:
  VOICEDICTATE_MOD     modificador(es) separados por vírgula, default KEY_LEFTALT,KEY_RIGHTALT
  VOICEDICTATE_KEY     tecla principal, default KEY_Z
  VOICEDICTATE_MODEL   faster-whisper model, default base
  VOICEDICTATE_LANG    código do idioma, default pt  (use 'auto' para detectar)
  VOICEDICTATE_DEVICE  dispositivo de inferência: auto, cuda, cpu  (default auto)
  VOICEDICTATE_PROMPT  prompt inicial para o Whisper (ex: nome, contexto, vocabulário)
  DISPLAY              display X11, default :0
"""

import os
import queue
import sys
import signal
import subprocess
import threading
import time

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel
import evdev
from evdev import ecodes

# ── config ────────────────────────────────────────────────────────────────────
_MOD_NAMES   = os.getenv("VOICEDICTATE_MOD",    "KEY_LEFTALT,KEY_RIGHTALT").split(",")
_KEY_NAME    = os.getenv("VOICEDICTATE_KEY",     "KEY_Z")

HOTKEY_MODS  = {getattr(ecodes, n.strip()) for n in _MOD_NAMES if hasattr(ecodes, n.strip())}
HOTKEY_MAIN  = getattr(ecodes, _KEY_NAME, ecodes.KEY_Z)

if not HOTKEY_MODS:
    sys.exit(
        f"[voicedictate] ERRO: Nenhum modificador válido em VOICEDICTATE_MOD={os.getenv('VOICEDICTATE_MOD')!r}.\n"
        "  Exemplo válido: KEY_LEFTALT,KEY_RIGHTALT"
    )
MODEL_SIZE     = os.getenv("VOICEDICTATE_MODEL",   "base")
LANGUAGE       = os.getenv("VOICEDICTATE_LANG",    "pt")
DEVICE_PREF    = os.getenv("VOICEDICTATE_DEVICE",  "auto")
INITIAL_PROMPT = os.getenv("VOICEDICTATE_PROMPT",  "") or None
SAMPLE_RATE    = 16_000
BLOCKSIZE      = 512

# ── shared state ──────────────────────────────────────────────────────────────
_recording  = threading.Event()
_state_lock = threading.Lock()
_buf: list[np.ndarray] = []
_buf_lock   = threading.Lock()

_DISPLAY_ENV    = os.getenv("DISPLAY", ":0")
_DBUS_ENV       = os.getenv("DBUS_SESSION_BUS_ADDRESS", "")
_SUBPROCESS_ENV = {**os.environ, "DISPLAY": _DISPLAY_ENV, "DBUS_SESSION_BUS_ADDRESS": _DBUS_ENV}

# gravações acima de 5 minutos são descartadas para não esgotar a RAM
_BUF_MAX_FRAMES = int(300 * SAMPLE_RATE / BLOCKSIZE)

_indicator_q: queue.SimpleQueue[bool] = queue.SimpleQueue()
_raw_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=200)


# ── recording indicator ───────────────────────────────────────────────────────
def _run_indicator() -> None:
    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception:
        return  # sem display X11 ou tkinter indisponível — falha silenciosa

    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.88)
    sw = root.winfo_screenwidth()
    root.geometry(f"90x28+{sw - 100}+8")

    lbl = tk.Label(
        root, text="  ● REC  ",
        bg="#cc2020", fg="white",
        font=("sans-serif", 10, "bold"),
    )
    lbl.pack(fill="both", expand=True)
    root.withdraw()

    def _poll() -> None:
        try:
            while True:
                recording = _indicator_q.get_nowait()
                if recording:
                    root.deiconify()
                    root.lift()
                else:
                    root.withdraw()
        except queue.Empty:
            pass
        root.after(80, _poll)

    root.after(80, _poll)
    root.mainloop()


# ── audio feedback ────────────────────────────────────────────────────────────
def _beep(freq: float, dur: float = 0.12) -> None:
    try:
        t    = np.linspace(0, dur, int(SAMPLE_RATE * dur), endpoint=False)
        # envelope suave para evitar clique no início/fim
        env  = np.ones_like(t)
        fade = int(SAMPLE_RATE * 0.01)
        env[:fade]  = np.linspace(0, 1, fade)
        env[-fade:] = np.linspace(1, 0, fade)
        wave = (0.5 * env * np.sin(2 * np.pi * freq * t)).astype(np.float32)
        sd.play(wave, SAMPLE_RATE, blocking=False)
    except Exception:
        pass  # feedback de áudio é opcional; não crashar o listener por isso


# ── visual feedback ───────────────────────────────────────────────────────────
def _notify(title: str, body: str = "", timeout_ms: int = 3000) -> None:
    subprocess.Popen(
        ["notify-send", "--app-name=voicedictate", "-t", str(timeout_ms), title, body],
        env=_SUBPROCESS_ENV,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# ── audio capture ─────────────────────────────────────────────────────────────
def _audio_callback(indata, _frames, _time_info, _status) -> None:
    chunk = indata[:, 0].copy()
    if _recording.is_set():
        with _buf_lock:
            if len(_buf) < _BUF_MAX_FRAMES:
                _buf.append(chunk)
    try:
        _raw_q.put_nowait(chunk)
    except queue.Full:
        pass  # descarta se o worker estiver atrasado


# ── clipboard ────────────────────────────────────────────────────────────────
def _clip(text: str) -> None:
    try:
        subprocess.run(
            ["xclip", "-selection", "clipboard"],
            input=text, text=True, env=_SUBPROCESS_ENV, check=False,
        )
    except Exception:
        pass


# ── text injection ────────────────────────────────────────────────────────────
def _inject(text: str) -> None:
    text = text.strip()
    if not text:
        return
    # --clearmodifiers omitido: se o Alt ainda constar como pressionado no X11,
    # ele seria re-pressionado sinteticamente ao fim, travando o teclado.
    # sleep(0.15): estabiliza o foco da janela antes do primeiro caractere.
    time.sleep(0.15)
    subprocess.run(
        ["xdotool", "type", "--delay", "20", "--", text],
        env=_SUBPROCESS_ENV,
        check=False,
    )


# ── transcription worker ──────────────────────────────────────────────────────
def _transcribe(model: WhisperModel, frames: list[np.ndarray]) -> None:
    if not frames:
        return

    try:
        audio = np.concatenate(frames)
        if np.max(np.abs(audio)) < 5e-4:
            print("[voicedictate] (silêncio – nada enviado)", flush=True)
            _notify("voicedictate", "Silêncio — nada inserido.")
            return

        lang = None if LANGUAGE == "auto" else LANGUAGE

        t0 = time.monotonic()
        segments, _ = model.transcribe(
            audio,
            language           = lang,
            beam_size          = 5,
            best_of            = 5,
            vad_filter         = True,
            without_timestamps = True,
            vad_parameters     = {"min_silence_duration_ms": 150, "speech_pad_ms": 80},
            initial_prompt     = INITIAL_PROMPT,
        )
        text    = "".join(s.text for s in segments).strip()
        elapsed = time.monotonic() - t0

        if text:
            print(f"[voicedictate] {elapsed:.2f}s │ {text}", flush=True)
            _notify("voicedictate ✓", text[:120], timeout_ms=5000)
            _clip(text)
            _inject(text)
        else:
            print(f"[voicedictate] {elapsed:.2f}s │ (nada detectado)", flush=True)
            _notify("voicedictate", "Nada detectado.")
    except Exception as exc:
        print(f"[voicedictate] ERRO na transcrição: {exc}", flush=True)
        _notify("voicedictate ✗", f"Erro na transcrição: {exc}"[:120], timeout_ms=5000)


# ── wake word + VAD worker ────────────────────────────────────────────────────
def _load_wakeword():
    if not WAKEWORD_MODEL:
        return None
    try:
        from openwakeword.model import Model
        oww = Model(wakeword_models=[WAKEWORD_MODEL], inference_framework="onnx")
        print(f"[voicedictate] Wake word '{WAKEWORD_MODEL}' ativo.", flush=True)
        return oww
    except Exception as exc:
        print(f"[voicedictate] Wake word indisponível: {exc}", flush=True)
        return None


def _wakeword_worker(model: WhisperModel, oww) -> None:
    silence_blocks = 0
    blocks_needed  = max(1, int(SILENCE_SEC * SAMPLE_RATE / BLOCKSIZE))

    while True:
        chunk = _raw_q.get()

        # ── wake word (apenas quando idle) ────────────────────────────────────
        if oww is not None and not _recording.is_set():
            try:
                scores = oww.predict(chunk)
                if any(s > WAKEWORD_THRESHOLD for s in scores.values()):
                    with _state_lock:
                        if not _recording.is_set():
                            _recording.set()
                            _indicator_q.put(True)
                            with _buf_lock:
                                _buf.clear()
                            _beep(880)
                            _notify("🎙 Gravando…", "Fale agora.", timeout_ms=10000)
                            print("[voicedictate] ● wake word — gravando …", flush=True)
                            silence_blocks = 0
            except Exception:
                pass

        # ── VAD / auto-stop (apenas quando gravando) ──────────────────────────
        if SILENCE_SEC > 0 and _recording.is_set():
            if np.max(np.abs(chunk)) < SILENCE_AMP:
                silence_blocks += 1
            else:
                silence_blocks = 0

            if silence_blocks >= blocks_needed:
                with _state_lock:
                    if _recording.is_set():
                        _recording.clear()
                        _indicator_q.put(False)
                        with _buf_lock:
                            frames = list(_buf)
                            _buf.clear()
                        _beep(440)
                        silence_blocks = 0
                        print("[voicedictate] ■ silêncio — transcrevendo …", flush=True)
                        threading.Thread(
                            target=_transcribe,
                            args=(model, frames),
                            daemon=True,
                        ).start()


# ── keyboard listener ─────────────────────────────────────────────────────────
def _listen(dev: evdev.InputDevice, model: WhisperModel) -> None:
    held_mods: set[int] = set()

    try:
        for evt in dev.read_loop():
            if evt.type != ecodes.EV_KEY:
                continue

            if evt.code in HOTKEY_MODS:
                if evt.value == 1:
                    held_mods.add(evt.code)
                elif evt.value == 0:
                    held_mods.discard(evt.code)
                continue

            if evt.code != HOTKEY_MAIN or evt.value != 1 or not (held_mods & HOTKEY_MODS):
                continue

            with _state_lock:
                if _recording.is_set():
                    _recording.clear()
                    _indicator_q.put(False)
                    with _buf_lock:
                        frames = list(_buf)
                        _buf.clear()
                    _beep(440)
                    print("[voicedictate] ■ transcrevendo …", flush=True)
                    threading.Thread(
                        target=_transcribe,
                        args=(model, frames),
                        daemon=True,
                    ).start()
                else:
                    _recording.set()
                    _indicator_q.put(True)
                    with _buf_lock:
                        _buf.clear()
                    _beep(880)
                    _notify("🎙 Gravando…", "Pressione Alt+Z para parar.", timeout_ms=10000)
                    print("[voicedictate] ● gravando …", flush=True)
    except OSError:
        print(f"[voicedictate] Teclado desconectado: {dev.path}", flush=True)
        _notify("voicedictate", f"Teclado desconectado: {dev.path}", timeout_ms=4000)


# ── device selection ──────────────────────────────────────────────────────────
def _pick_device() -> tuple[str, str]:
    if DEVICE_PREF == "cpu":
        return "cpu", "int8"
    if DEVICE_PREF == "cuda":
        return "cuda", "float16"
    # auto: tenta CUDA, cai em CPU se não disponível
    try:
        import ctranslate2
        if "float16" in ctranslate2.get_supported_compute_types("cuda"):
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"


# ── keyboard discovery ────────────────────────────────────────────────────────
def _pick_keyboards() -> list[evdev.InputDevice]:
    """Retorna um InputDevice por teclado físico único (deduplica por dev.phys)."""
    seen_phys: set[str] = set()
    result: list[evdev.InputDevice] = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
            try:
                caps = dev.capabilities()
                keys = caps.get(ecodes.EV_KEY, [])
                if not (ecodes.KEY_A in keys and ecodes.KEY_SPACE in keys):
                    dev.close()
                    continue
                phys = dev.phys or path  # dispositivos virtuais sem phys usam o path como chave
                if phys in seen_phys:
                    dev.close()
                    continue
                seen_phys.add(phys)
                result.append(dev)
            except Exception:
                dev.close()
        except (PermissionError, OSError):
            pass
    return result


# ── entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    device, compute_type = _pick_device()

    def _load_model(dev: str, ct: str) -> WhisperModel:
        return WhisperModel(
            MODEL_SIZE,
            device       = dev,
            compute_type = ct,
            num_workers  = 2,
            cpu_threads  = min(4, os.cpu_count() or 4),
        )

    if device != "cpu":
        try:
            model = _load_model(device, compute_type)
            # força o carregamento lazy das libs CUDA; falha aqui se libcublas não estiver disponível
            _silent = np.zeros(SAMPLE_RATE, dtype=np.float32)
            list(model.transcribe(_silent, language="pt")[0])
        except Exception as exc:
            print(f"[voicedictate] {device.upper()} indisponível ({exc}), usando CPU …", flush=True)
            model = _load_model("cpu", "int8")
    else:
        model = _load_model(device, compute_type)

    keyboards = _pick_keyboards()
    if not keyboards:
        sys.exit(
            "[voicedictate] ERRO: Nenhum teclado encontrado.\n"
            "  Execute: sudo usermod -aG input $USER\n"
            "  Depois faça logout e login novamente."
        )
    #print(f"[voicedictate] Monitorando {len(keyboards)} teclado(s).", flush=True)

    stream = sd.InputStream(
        samplerate = SAMPLE_RATE,
        channels   = 1,
        dtype      = np.float32,
        callback   = _audio_callback,
        blocksize  = BLOCKSIZE,
    )

    def _shutdown(_sig, _frame) -> None:
        print("\n[voicedictate] Encerrando.", flush=True)
        stream.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    threading.Thread(target=_run_indicator, daemon=True).start()

    with stream:
        for kb in keyboards[:-1]:
            threading.Thread(target=_listen, args=(kb, model), daemon=True).start()
        _listen(keyboards[-1], model)

    # Chegou aqui porque o teclado principal desconectou (OSError capturado em _listen).
    # Sai com código 1 para que o systemd reinicie o serviço (Restart=on-failure).
    sys.exit(1)


if __name__ == "__main__":
    main()
