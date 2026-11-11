#!/usr/bin/env python3
"""
AutoClicker - app de autoclick local com interface gráfica (Tkinter).

- Botão esquerdo ou direito
- Intervalo configurável (horas / minutos / segundos / milissegundos)
- Posição fixa salva: o mouse é SEMPRE movido para ela antes de cada clique
- Captura de posição (contagem regressiva) ou digitando X / Y
- Configuração persistida em autoclicker_config.json
- Sem dependências externas (usa ctypes: CoreGraphics no macOS, user32 no Windows)

Parada de emergência: leve o mouse ao canto superior esquerdo da tela (0,0),
ou pressione ESC com a janela em foco.
"""

import ctypes
import ctypes.util
import json
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

def _config_path():
    if not getattr(sys, "frozen", False):  # rodando como script: ao lado do arquivo
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "autoclicker_config.json")
    # empacotado (.app / .exe): pasta de dados do usuário
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support/AutoClicker")
    else:
        base = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "AutoClicker")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "autoclicker_config.json")


CONFIG_PATH = _config_path()

DEFAULT_CONFIG = {
    "button": "left",          # "left" | "right"
    "hours": 0,
    "minutes": 0,
    "seconds": 1,
    "milliseconds": 0,
    "x": 500,
    "y": 500,
    "move_to_position": True,  # sempre mover o mouse para (x, y) antes do clique
    "max_clicks": 0,           # 0 = infinito
    "start_delay": 3,          # segundos antes do primeiro clique
    "capture_delay": 3,        # segundos da contagem para capturar posição
}

MIN_INTERVAL = 0.01  # 10 ms
FAILSAFE_PX = 3      # canto (0,0) para parada de emergência


# --------------------------------------------------------------------------
# Backend de mouse (por plataforma)
# --------------------------------------------------------------------------
class MouseBackend:
    def get_pos(self):
        raise NotImplementedError

    def move(self, x, y):
        raise NotImplementedError

    def click(self, button, x, y):
        raise NotImplementedError

    def permission_ok(self):
        return True


class MacBackend(MouseBackend):
    """CoreGraphics via ctypes (nenhuma dependência externa)."""

    class CGPoint(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

    def __init__(self):
        path = (ctypes.util.find_library("ApplicationServices")
                or "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        cf_path = (ctypes.util.find_library("CoreFoundation")
                   or "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.cg = ctypes.CDLL(path)
        self.cf = ctypes.CDLL(cf_path)
        P = self.CGPoint
        cg = self.cg
        cg.CGEventCreate.restype = ctypes.c_void_p
        cg.CGEventCreate.argtypes = [ctypes.c_void_p]
        cg.CGEventGetLocation.restype = P
        cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
        cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, P, ctypes.c_uint32]
        cg.CGEventPost.restype = None
        cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        cg.CGEventSetIntegerValueField.restype = None
        cg.CGEventSetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int64]
        self.cf.CFRelease.restype = None
        self.cf.CFRelease.argtypes = [ctypes.c_void_p]

    # constantes CGEventType
    _MOVED = 5
    _DOWN = {"left": 1, "right": 3}
    _UP = {"left": 2, "right": 4}
    _BTN = {"left": 0, "right": 1}
    _CLICK_STATE_FIELD = 1

    def get_pos(self):
        ev = self.cg.CGEventCreate(None)
        p = self.cg.CGEventGetLocation(ev)
        self.cf.CFRelease(ev)
        return int(round(p.x)), int(round(p.y))

    def _post(self, etype, x, y, button, click_state=0):
        ev = self.cg.CGEventCreateMouseEvent(None, etype, self.CGPoint(float(x), float(y)), button)
        if click_state:
            self.cg.CGEventSetIntegerValueField(ev, self._CLICK_STATE_FIELD, click_state)
        self.cg.CGEventPost(0, ev)  # kCGHIDEventTap
        self.cf.CFRelease(ev)

    def move(self, x, y):
        self._post(self._MOVED, x, y, 0)

    def click(self, button, x, y):
        b = self._BTN[button]
        self._post(self._DOWN[button], x, y, b, 1)
        time.sleep(0.01)
        self._post(self._UP[button], x, y, b, 1)

    def permission_ok(self):
        try:
            fn = self.cg.AXIsProcessTrusted
            fn.restype = ctypes.c_bool
            return bool(fn())
        except Exception:
            return True


class WinBackend(MouseBackend):
    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    def __init__(self):
        self.u = ctypes.windll.user32
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass

    def get_pos(self):
        p = self.POINT()
        self.u.GetCursorPos(ctypes.byref(p))
        return p.x, p.y

    def move(self, x, y):
        self.u.SetCursorPos(int(x), int(y))

    def click(self, button, x, y):
        down, up = (0x0002, 0x0004) if button == "left" else (0x0008, 0x0010)
        self.u.mouse_event(down, 0, 0, 0, 0)
        time.sleep(0.01)
        self.u.mouse_event(up, 0, 0, 0, 0)


def make_backend():
    if sys.platform == "darwin":
        return MacBackend()
    if sys.platform.startswith("win"):
        return WinBackend()
    raise RuntimeError("Plataforma não suportada (somente macOS e Windows).")


# --------------------------------------------------------------------------
# Motor de cliques (thread)
# --------------------------------------------------------------------------
class ClickEngine:
    def __init__(self, backend):
        self.backend = backend
        self._thread = None
        self._stop = threading.Event()
        self.running = False
        self.clicks = 0
        self.message = "Parado"

    def start(self, cfg):
        if self.running:
            return
        self._stop.clear()
        self.clicks = 0
        self.running = True
        self._thread = threading.Thread(target=self._run, args=(dict(cfg),), daemon=True)
        self._thread.start()

    def stop(self, reason="Parado"):
        self._stop.set()
        if self.running:
            self.message = reason

    def _run(self, cfg):
        be = self.backend
        interval = max(
            MIN_INTERVAL,
            cfg["hours"] * 3600 + cfg["minutes"] * 60 + cfg["seconds"] + cfg["milliseconds"] / 1000.0,
        )
        max_clicks = cfg["max_clicks"]
        try:
            if cfg["start_delay"] > 0:
                end = time.monotonic() + cfg["start_delay"]
                while not self._stop.is_set():
                    left = end - time.monotonic()
                    if left <= 0:
                        break
                    self.message = f"Iniciando em {left:0.1f}s..."
                    self._stop.wait(min(0.1, left))

            next_t = time.monotonic()
            while not self._stop.is_set():
                wait = next_t - time.monotonic()
                if wait > 0:
                    self.message = "Rodando"
                    if self._stop.wait(wait):
                        break

                # parada de emergência: mouse no canto superior esquerdo
                px, py = be.get_pos()
                if 0 <= px <= FAILSAFE_PX and 0 <= py <= FAILSAFE_PX:
                    self.message = "Parado (parada de emergência: canto da tela)"
                    break

                if cfg["move_to_position"]:
                    be.move(cfg["x"], cfg["y"])
                    x, y = cfg["x"], cfg["y"]
                    time.sleep(0.005)
                else:
                    x, y = px, py
                be.click(cfg["button"], x, y)
                self.clicks += 1

                if max_clicks and self.clicks >= max_clicks:
                    self.message = "Concluído (limite de cliques atingido)"
                    break
                next_t += interval
                if next_t < time.monotonic():  # atrasou: não acumular rajadas
                    next_t = time.monotonic() + interval
        except Exception as exc:  # noqa: BLE001
            self.message = f"Erro: {exc}"
        finally:
            if self.message == "Rodando" or self.message.startswith("Iniciando"):
                self.message = "Parado"
            self.running = False


# --------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("AutoClicker")
        self.resizable(False, False)
        self.attributes("-topmost", True)

        try:
            self.backend = make_backend()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("AutoClicker", str(exc))
            raise SystemExit(1)

        self.engine = ClickEngine(self.backend)
        self.cfg = self._load_config()
        self._capturing = False

        self._build_ui()
        self._load_into_ui()
        self.bind("<Escape>", lambda e: self.stop())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._tick()

        if not self.backend.permission_ok():
            messagebox.showwarning(
                "Permissão necessária",
                "Para o app controlar o mouse no macOS, conceda permissão de Acessibilidade ao "
                "Terminal / Python em:\n\nAjustes do Sistema → Privacidade e Segurança → Acessibilidade\n\n"
                "Depois reinicie o app.",
            )

    # ---- config ----
    def _load_config(self):
        cfg = dict(DEFAULT_CONFIG)
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for k in DEFAULT_CONFIG:
                if k in data:
                    cfg[k] = data[k]
        except (OSError, ValueError):
            pass
        return cfg

    def _save_config(self):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(self.cfg, fh, indent=2)
            return True
        except OSError as exc:
            messagebox.showerror("AutoClicker", f"Não foi possível salvar: {exc}")
            return False

    # ---- UI ----
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}
        main = ttk.Frame(self, padding=10)
        main.grid(row=0, column=0)

        # Botão do mouse
        f_btn = ttk.LabelFrame(main, text="Botão do mouse", padding=8)
        f_btn.grid(row=0, column=0, sticky="ew", **pad)
        self.v_button = tk.StringVar(value="left")
        ttk.Radiobutton(f_btn, text="Esquerdo", variable=self.v_button, value="left").grid(row=0, column=0, padx=10)
        ttk.Radiobutton(f_btn, text="Direito", variable=self.v_button, value="right").grid(row=0, column=1, padx=10)

        # Intervalo
        f_int = ttk.LabelFrame(main, text="Intervalo entre cliques", padding=8)
        f_int.grid(row=1, column=0, sticky="ew", **pad)
        self.v_h, self.v_m, self.v_s, self.v_ms = (tk.StringVar() for _ in range(4))
        for i, (label, var, mx) in enumerate(
            (("horas", self.v_h, 99), ("min", self.v_m, 59), ("seg", self.v_s, 59), ("ms", self.v_ms, 999))
        ):
            ttk.Spinbox(f_int, from_=0, to=mx, width=5, textvariable=var).grid(row=0, column=i * 2, padx=(6, 2))
            ttk.Label(f_int, text=label).grid(row=0, column=i * 2 + 1, padx=(0, 6))

        # Posição
        f_pos = ttk.LabelFrame(main, text="Posição do clique", padding=8)
        f_pos.grid(row=2, column=0, sticky="ew", **pad)
        self.v_move = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            f_pos, text="Sempre mover o mouse para a posição salva antes de cada clique", variable=self.v_move
        ).grid(row=0, column=0, columnspan=4, sticky="w")
        self.v_x, self.v_y = tk.StringVar(), tk.StringVar()
        ttk.Label(f_pos, text="X:").grid(row=1, column=0, sticky="e", pady=6)
        ttk.Entry(f_pos, width=7, textvariable=self.v_x).grid(row=1, column=1, sticky="w")
        ttk.Label(f_pos, text="Y:").grid(row=1, column=2, sticky="e")
        ttk.Entry(f_pos, width=7, textvariable=self.v_y).grid(row=1, column=3, sticky="w")
        self.btn_capture = ttk.Button(f_pos, text="Capturar posição do mouse", command=self.capture)
        self.btn_capture.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(4, 2))
        ttk.Button(f_pos, text="Testar (mover mouse até a posição)", command=self.test_move).grid(
            row=3, column=0, columnspan=4, sticky="ew", pady=2
        )
        self.v_live = tk.StringVar(value="Mouse agora: -")
        ttk.Label(f_pos, textvariable=self.v_live, foreground="#555").grid(row=4, column=0, columnspan=4, sticky="w")

        # Opções
        f_opt = ttk.LabelFrame(main, text="Opções", padding=8)
        f_opt.grid(row=3, column=0, sticky="ew", **pad)
        self.v_max, self.v_delay, self.v_cdelay = tk.StringVar(), tk.StringVar(), tk.StringVar()
        ttk.Label(f_opt, text="Nº de cliques (0 = infinito):").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(f_opt, from_=0, to=10_000_000, width=8, textvariable=self.v_max).grid(row=0, column=1, padx=6)
        ttk.Label(f_opt, text="Atraso para iniciar (s):").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Spinbox(f_opt, from_=0, to=60, width=8, textvariable=self.v_delay).grid(row=1, column=1, padx=6)
        ttk.Label(f_opt, text="Contagem da captura (s):").grid(row=2, column=0, sticky="w")
        ttk.Spinbox(f_opt, from_=1, to=30, width=8, textvariable=self.v_cdelay).grid(row=2, column=1, padx=6)

        # Controles
        f_ctl = ttk.Frame(main)
        f_ctl.grid(row=4, column=0, sticky="ew", **pad)
        f_ctl.columnconfigure((0, 1, 2), weight=1)
        self.btn_start = ttk.Button(f_ctl, text="▶ Iniciar", command=self.start)
        self.btn_start.grid(row=0, column=0, sticky="ew", padx=2)
        self.btn_stop = ttk.Button(f_ctl, text="■ Parar (Esc)", command=self.stop, state="disabled")
        self.btn_stop.grid(row=0, column=1, sticky="ew", padx=2)
        ttk.Button(f_ctl, text="Salvar", command=self.save).grid(row=0, column=2, sticky="ew", padx=2)

        # Status
        self.v_status = tk.StringVar(value="Parado")
        self.v_count = tk.StringVar(value="Cliques: 0")
        ttk.Label(main, textvariable=self.v_status, font=("Helvetica", 12, "bold")).grid(row=5, column=0, sticky="w", **pad)
        ttk.Label(main, textvariable=self.v_count).grid(row=6, column=0, sticky="w", padx=8)
        ttk.Label(
            main,
            text="Parada de emergência: leve o mouse ao canto superior esquerdo da tela.",
            foreground="#777",
            wraplength=380,
        ).grid(row=7, column=0, sticky="w", **pad)

    def _load_into_ui(self):
        c = self.cfg
        self.v_button.set(c["button"])
        self.v_h.set(c["hours"]); self.v_m.set(c["minutes"]); self.v_s.set(c["seconds"]); self.v_ms.set(c["milliseconds"])
        self.v_x.set(c["x"]); self.v_y.set(c["y"])
        self.v_move.set(c["move_to_position"])
        self.v_max.set(c["max_clicks"]); self.v_delay.set(c["start_delay"]); self.v_cdelay.set(c["capture_delay"])

    def _read_from_ui(self):
        """Lê e valida os campos. Retorna dict ou None (mostrando erro)."""
        try:
            cfg = {
                "button": self.v_button.get(),
                "hours": int(self.v_h.get() or 0),
                "minutes": int(self.v_m.get() or 0),
                "seconds": float(self.v_s.get() or 0),
                "milliseconds": int(self.v_ms.get() or 0),
                "x": int(self.v_x.get()),
                "y": int(self.v_y.get()),
                "move_to_position": bool(self.v_move.get()),
                "max_clicks": int(self.v_max.get() or 0),
                "start_delay": float(self.v_delay.get() or 0),
                "capture_delay": int(self.v_cdelay.get() or 3),
            }
        except ValueError:
            messagebox.showerror("AutoClicker", "Valores inválidos. Use apenas números nos campos.")
            return None
        if cfg["button"] not in ("left", "right"):
            cfg["button"] = "left"
        # X e Y podem ser negativos (monitor à esquerda/acima do principal)
        for k in ("hours", "minutes", "seconds", "milliseconds", "max_clicks", "start_delay"):
            if cfg[k] < 0:
                messagebox.showerror("AutoClicker", f"O campo '{k}' não pode ser negativo.")
                return None
        total = cfg["hours"] * 3600 + cfg["minutes"] * 60 + cfg["seconds"] + cfg["milliseconds"] / 1000
        if total < MIN_INTERVAL:
            messagebox.showerror("AutoClicker", f"O intervalo mínimo é {int(MIN_INTERVAL * 1000)} ms.")
            return None
        cfg["capture_delay"] = max(1, cfg["capture_delay"])
        return cfg

    # ---- ações ----
    def save(self):
        cfg = self._read_from_ui()
        if cfg is None:
            return False
        self.cfg = cfg
        if self._save_config():
            self.v_status.set("Configuração salva")
            return True
        return False

    def start(self):
        if self.engine.running or self._capturing:
            return
        if not self.save():
            return
        self.engine.start(self.cfg)
        self._set_running_ui(True)

    def stop(self, reason="Parado"):
        self.engine.stop(reason)

    def test_move(self):
        cfg = self._read_from_ui_position()
        if cfg:
            self.backend.move(*cfg)

    def _read_from_ui_position(self):
        try:
            return int(self.v_x.get()), int(self.v_y.get())
        except ValueError:
            messagebox.showerror("AutoClicker", "X e Y devem ser números inteiros.")
            return None

    def capture(self):
        if self._capturing or self.engine.running:
            return
        try:
            secs = max(1, int(self.v_cdelay.get() or 3))
        except ValueError:
            secs = 3
        self._capturing = True
        self.btn_capture.state(["disabled"])
        self.btn_start.state(["disabled"])
        self._capture_countdown(secs)

    def _capture_countdown(self, left):
        if left > 0:
            self.v_status.set(f"Leve o mouse ao local desejado... capturando em {left}s")
            self.after(1000, self._capture_countdown, left - 1)
            return
        x, y = self.backend.get_pos()
        self.v_x.set(x)
        self.v_y.set(y)
        self.cfg["x"], self.cfg["y"] = x, y
        self._save_config()
        self._capturing = False
        self.btn_capture.state(["!disabled"])
        self.btn_start.state(["!disabled"])
        self.v_status.set(f"Posição capturada e salva: ({x}, {y})")

    def _set_running_ui(self, running):
        self.btn_start.state(["disabled"] if running else ["!disabled"])
        self.btn_stop.state(["!disabled"] if running else ["disabled"])
        self.btn_capture.state(["disabled"] if running else ["!disabled"])

    def _tick(self):
        x, y = self.backend.get_pos()
        self.v_live.set(f"Mouse agora: ({x}, {y})")
        if self.engine.running or self.btn_stop.instate(["!disabled"]):
            self.v_status.set(self.engine.message)
            self.v_count.set(f"Cliques: {self.engine.clicks}")
            if not self.engine.running:
                self._set_running_ui(False)
        self.after(100, self._tick)

    def _on_close(self):
        self.engine.stop()
        self.destroy()


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
