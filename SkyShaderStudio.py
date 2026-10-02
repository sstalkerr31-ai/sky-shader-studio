#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sky Shader Studio  -  редактор шейдеров Sky: Children of the Light (.spv / .ref)
в стиле Visual Studio (Dark+).

* .spv  -> открывается как SPIR-V ассемблер (или GLSL) и при сохранении (Ctrl+S)
           компилируется обратно в бинарный .spv (с резервной копией .bak)
* .ref  -> табличный редактор рефлексии (имена / поля) + hex-просмотр
* Массовая декомпиляция / компиляция всей папки Shaders/Bin в несколько потоков

Требуется только Python 3.8+ (tkinter идёт в комплекте с Windows-версией Python)
и утилиты из Vulkan SDK / SPIRV-Tools:
    spirv-dis, spirv-as, spirv-val, spirv-cross, glslangValidator
Положите их в папку  tools/  рядом со скриптом, либо установите Vulkan SDK
(https://vulkan.lunarg.com), либо укажите пути в  Tools -> Settings.
"""
import os
import sys
import re
import io
import json
import glob
import time
import queue
import shutil
import hashlib
import struct
import tempfile
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, font as tkfont

APP_NAME = "Sky Shader Studio"
APP_VER = "1.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(
    sys.executable if getattr(sys, "frozen", False) else __file__))
CFG_PATH = os.path.join(SCRIPT_DIR, "skyshaderstudio.json")
MANIFEST = ".sss_manifest.json"

# ----------------------------------------------------------------- палитра VS Dark+
C = dict(
    bg="#1e1e1e", side="#252526", bar="#333333", menu="#3c3c3c", tab_off="#2d2d2d",
    border="#3c3c3c", fg="#d4d4d4", dim="#858585", sel="#264f78", cur="#2a2d2e",
    accent="#007acc", btn="#0e639c", btn_h="#1177bb", input="#3c3c3c", tree_sel="#094771",
    hover="#2a2d2e", red="#f48771", green="#89d185", yellow="#cca700",
)
SYN = dict(
    opcode="#569cd6", id="#9cdcfe", number="#b5cea8", string="#ce9178",
    comment="#6a9955", enum="#4ec9b0", keyword="#569cd6", type="#4ec9b0",
    func="#dcdcaa", pre="#c586c0", ctl="#c586c0",
)

MODES = {"asm": "SPIR-V Assembly", "glsl": "GLSL (spirv-cross)"}
MODE_EXT = {"asm": ".spvasm", "glsl": ".glsl"}

DEFAULT_CFG = dict(tools={}, mode="asm", friendly=False,
                   workers=min(8, os.cpu_count() or 4), font_size=11, last_dir="")


# =============================================================================
#  Ядро: инструменты, декомпиляция, компиляция  (без GUI)
# =============================================================================
class ToolError(Exception):
    pass


class CompileError(Exception):
    pass


def load_cfg():
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_cfg(cfg):
    try:
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def run_cmd(cmd, timeout=180):
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    p = subprocess.run(cmd, capture_output=True, timeout=timeout, **kw)
    return p.returncode, p.stdout, p.stderr


def dec(b):
    return b.decode("utf-8", "replace").replace("\r\n", "\n")


GEN_MAGIC = {
    "Khronos LLVM/SPIR-V Translator": 6, "Khronos SPIR-V Tools Assembler": 7,
    "Khronos Glslang Reference Front End": 8, "Google Shaderc over Glslang": 13,
    "Google spiregg": 14, "Google rspirv": 15, "Khronos SPIR-V Tools Linker": 17,
    "Google Clspv": 3,
}
STAGES = {"vs": "vert", "fs": "frag", "ps": "frag", "cs": "comp", "gs": "geom"}
VK_ENV = {(1, 0): "vulkan1.0", (1, 1): "vulkan1.1", (1, 2): "vulkan1.1", (1, 3): "vulkan1.1",
          (1, 4): "vulkan1.1", (1, 5): "vulkan1.2", (1, 6): "vulkan1.3"}


def spv_version(data):
    if len(data) < 8 or struct.unpack("<I", data[:4])[0] != 0x07230203:
        return None
    w = struct.unpack("<I", data[4:8])[0]
    return (w >> 16) & 0xFF, (w >> 8) & 0xFF


def stage_from_name(name, text=""):
    m = re.search(r"(?:^|[._-])(vs|fs|ps|cs|gs)(?:[._-]|$)", name.lower())
    if m:
        return STAGES[m.group(1)]
    if "gl_Position" in text:
        return "vert"
    if "gl_FragCoord" in text or re.search(r"\bout\s+vec4\b", text):
        return "frag"
    return None


def ref_for_spv(path):
    d, n = os.path.split(path)
    m = re.match(r"^(.*)[._](?:vs|fs|cs|gs)\.spv$", n, re.I)
    if m:
        return os.path.join(d, m.group(1) + ".ref")
    return None


def backup_once(path):
    bak = path + ".bak"
    if os.path.exists(path) and not os.path.exists(bak):
        shutil.copyfile(path, bak)


def sha1(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class Core:
    NAMES = ["spirv-dis", "spirv-as", "spirv-val", "spirv-cross", "glslangValidator"]

    def __init__(self, cfg):
        self.cfg = cfg
        self.paths = {}
        self.detect()

    def detect(self):
        for n in self.NAMES:
            self.paths[n] = self._find(n)

    def _find(self, n):
        custom = self.cfg.get("tools", {}).get(n)
        if custom and os.path.isfile(custom):
            return custom
        exe = n + (".exe" if os.name == "nt" else "")
        dirs = [os.path.join(SCRIPT_DIR, "tools"), os.path.join(SCRIPT_DIR, "tools", "bin"),
                os.path.join(SCRIPT_DIR, "bin"), SCRIPT_DIR]
        sdk = os.environ.get("VULKAN_SDK")
        if sdk:
            dirs.append(os.path.join(sdk, "Bin"))
        if os.name == "nt":
            dirs += sorted(glob.glob("C:/VulkanSDK/*/Bin"), reverse=True)
        for d in dirs:
            p = os.path.join(d, exe)
            if os.path.isfile(p):
                return p
        return shutil.which(n)

    def need(self, n):
        p = self.paths.get(n)
        if not p:
            raise ToolError(
                "Не найден '%s'.\nПоложите утилиты в папку tools/ рядом со скриптом, "
                "установите Vulkan SDK или укажите путь в Tools -> Settings." % n)
        return p

    # ---------------------------------------------------------------- decompile
    def decompile_file(self, path, mode="asm", friendly=False):
        if mode == "asm":
            cmd = [self.need("spirv-dis")] + ([] if friendly else ["--raw-id"]) + [path, "-o", "-"]
        else:
            cmd = [self.need("spirv-cross"), path, "--vulkan-semantics", "--version", "450"]
        rc, so, se = run_cmd(cmd)
        if rc != 0:
            raise CompileError(dec(se) or dec(so) or "Ошибка декомпиляции (код %d)" % rc)
        return dec(so)

    # ------------------------------------------------------------------ compile
    def compile_text(self, text, mode, name, orig=None):
        """text -> bytes(spv). orig - прежние байты (для версии/generator)."""
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "out.spv")
            if mode == "asm":
                src = os.path.join(td, "in.spvasm")
                with open(src, "w", encoding="utf-8", newline="\n") as f:
                    f.write(text)
                m = re.search(r"^;\s*Version:\s*(\d+)\.(\d+)", text, re.M)
                env = "spv%s.%s" % (m.group(1), m.group(2)) if m else "spv1.5"
                rc, so, se = run_cmd([self.need("spirv-as"), "--preserve-numeric-ids",
                                      "--target-env", env, src, "-o", out])
                if rc != 0:
                    raise CompileError(dec(se) or dec(so))
                with open(out, "rb") as f:
                    data = bytearray(f.read())
                # восстановить generator-слово, чтобы файл был идентичен оригиналу
                g = re.search(r"^;\s*Generator:\s*(.+?);\s*(\d+)", text, re.M)
                if g and g.group(1).strip() in GEN_MAGIC:
                    word = (GEN_MAGIC[g.group(1).strip()] << 16) | int(g.group(2))
                    data[8:12] = struct.pack("<I", word)
                elif orig and len(orig) >= 12 and len(data) >= 12:
                    data[8:12] = orig[8:12]
                return bytes(data)
            # ---- GLSL
            stage = stage_from_name(name, text)
            if not stage:
                raise CompileError("Не удалось определить стадию шейдера по имени '%s'. "
                                   "Назовите файл ...vs.glsl / ...fs.glsl" % name)
            src = os.path.join(td, "in." + stage)
            with open(src, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            ver = spv_version(orig) if orig else None
            env = VK_ENV.get(ver, "vulkan1.2")
            rc, so, se = run_cmd([self.need("glslangValidator"), "-V", "--target-env", env,
                                  src, "-o", out])
            if rc != 0 or not os.path.exists(out):
                raise CompileError(dec(so) + dec(se))
            with open(out, "rb") as f:
                return f.read()

    def validate_bytes(self, data):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "v.spv")
            with open(p, "wb") as f:
                f.write(data)
            rc, so, se = run_cmd([self.need("spirv-val"), p])
            if rc != 0:
                raise CompileError("spirv-val: " + (dec(se) or dec(so)))


def parse_problems(msg):
    """-> [(line, text)] из вывода spirv-as / glslang."""
    res = []
    for ln in msg.splitlines():
        m = re.search(r"error:\s*(\d+):\s*(.*)", ln, re.I) or \
            re.search(r"ERROR:\s*[^:]*:(\d+):\s*(.*)", ln)
        if m:
            res.append((int(m.group(1)), m.group(2).strip()))
    return res


# =============================================================================
#  Массовые операции (поток + пул воркеров)
# =============================================================================
class BatchRunner(threading.Thread):
    def __init__(self, core, kind, opts, q, cancel):
        super().__init__(daemon=True)
        self.core, self.kind, self.o, self.q, self.cancel = core, kind, opts, q, cancel

    def collect(self):
        o = self.o
        pats = [p.strip() for p in o["pattern"].split(";") if p.strip()]
        import fnmatch
        res = []
        if o["recursive"]:
            walker = os.walk(o["src"])
        else:
            walker = [(o["src"], [], os.listdir(o["src"]))]
        dst_abs = os.path.abspath(o["dst"])
        for d, dirs, files in walker:
            if o["recursive"]:
                dirs[:] = [x for x in dirs if os.path.abspath(os.path.join(d, x)) != dst_abs]
            for fn in files:
                if any(fnmatch.fnmatch(fn.lower(), p.lower()) for p in pats):
                    res.append(os.path.join(d, fn))
        res.sort()
        return res

    def run(self):
        o = self.o
        t0 = time.time()
        try:
            files = self.collect()
        except Exception as e:
            self.q.put(("done", "Ошибка: %s" % e))
            return
        self.q.put(("total", len(files)))
        man_path = os.path.join(o["dst"] if self.kind == "decompile" else o["src"], MANIFEST)
        manifest = {}
        try:
            with open(man_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception:
            pass
        lock = threading.Lock()
        mode, src, dst = o["mode"], o["src"], o["dst"]
        ext = MODE_EXT[mode]

        def t_decompile(p):
            if self.cancel.is_set():
                return ("cancel", p, "")
            rel = os.path.relpath(p, src)
            out = os.path.join(dst, os.path.splitext(rel)[0] + ext)
            if o["skip_existing"] and os.path.exists(out):
                return ("skip", rel, "уже есть")
            text = self.core.decompile_file(p, mode, o["friendly"])
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            with lock:
                manifest[rel] = sha1(text)
            return ("ok", rel, "")

        def t_compile(p):
            if self.cancel.is_set():
                return ("cancel", p, "")
            rel = os.path.relpath(p, src)
            out = os.path.join(dst, os.path.splitext(rel)[0] + ".spv")
            with open(p, "r", encoding="utf-8", errors="replace", newline="") as f:
                text = f.read().replace("\r\n", "\n")
            h = sha1(text)
            if o["only_changed"] and os.path.exists(out) and manifest.get(rel) == h:
                return ("skip", rel, "не изменён")
            orig = None
            if os.path.exists(out):
                with open(out, "rb") as f:
                    orig = f.read()
            data = self.core.compile_text(text, mode, os.path.basename(p), orig)
            if o["validate"]:
                self.core.validate_bytes(data)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            if o["backup"]:
                backup_once(out)
            with open(out, "wb") as f:
                f.write(data)
            with lock:
                manifest[rel] = h
            return ("ok", rel, "%d байт" % len(data))

        task = t_decompile if self.kind == "decompile" else t_compile

        def wrapped(p):
            try:
                return task(p)
            except Exception as e:
                msg = str(e).strip().splitlines()
                return ("err", os.path.relpath(p, src), " | ".join(msg[:3]))

        counts = {"ok": 0, "skip": 0, "err": 0, "cancel": 0}
        with ThreadPoolExecutor(max_workers=max(1, o["workers"])) as ex:
            for r in ex.map(wrapped, files):
                counts[r[0]] += 1
                self.q.put(("res",) + r)
        try:
            os.makedirs(os.path.dirname(man_path), exist_ok=True)
            with open(man_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f)
        except Exception:
            pass
        self.q.put(("done", "Готово за %.1f c:  успешно %d,  пропущено %d,  ошибок %d%s" % (
            time.time() - t0, counts["ok"], counts["skip"], counts["err"],
            ",  отменено %d" % counts["cancel"] if counts["cancel"] else "")))


# =============================================================================
#  .ref : сканирование
# =============================================================================
def scan_ref(data):
    res = []
    d = bytes(data)
    for m in re.finditer(rb"[A-Za-z_][A-Za-z0-9_$.]{2,}(?=\x00)", d):
        start, end = m.start(), m.end()
        j = end
        while j < len(d) and d[j] == 0:
            j += 1
        cap = min(j - start - 1, 27)
        res.append(dict(off=start, name=m.group().decode(), len=end - start, cap=cap))
    return res


# =============================================================================
#  GUI-помощники
# =============================================================================
def mkbtn(parent, text, cmd, primary=False, **kw):
    bg = C["btn"] if primary else C["bar"]
    hv = C["btn_h"] if primary else "#4a4a4a"
    b = tk.Button(parent, text=text, command=cmd, bg=bg, fg="#ffffff" if primary else C["fg"],
                  activebackground=hv, activeforeground="#ffffff", relief="flat", bd=0,
                  padx=10, pady=3, cursor="hand2", font=("Segoe UI", 9), **kw)
    b.bind("<Enter>", lambda e: b.config(bg=hv))
    b.bind("<Leave>", lambda e: b.config(bg=bg))
    return b


def mkentry(parent, var=None, width=30):
    return tk.Entry(parent, textvariable=var, width=width, bg=C["input"], fg=C["fg"],
                    insertbackground="#ffffff", relief="flat", bd=0, highlightthickness=1,
                    highlightbackground=C["input"], highlightcolor=C["accent"],
                    font=("Segoe UI", 9))


def mkcheck(parent, text, var):
    return tk.Checkbutton(parent, text=text, variable=var, bg=C["side"], fg=C["fg"],
                          selectcolor=C["input"], activebackground=C["side"],
                          activeforeground=C["fg"], bd=0, highlightthickness=0,
                          font=("Segoe UI", 9), anchor="w")


def mklabel(parent, text, **kw):
    return tk.Label(parent, text=text, bg=kw.pop("bg", C["side"]), fg=kw.pop("fg", C["fg"]),
                    font=kw.pop("font", ("Segoe UI", 9)), **kw)


class DarkDialog(tk.Toplevel):
    def __init__(self, app, title, w=560, h=300):
        super().__init__(app.root)
        self.app = app
        self.title(title)
        self.configure(bg=C["side"])
        self.transient(app.root)
        x = app.root.winfo_x() + (app.root.winfo_width() - w) // 2
        y = app.root.winfo_y() + (app.root.winfo_height() - h) // 3
        self.geometry("%dx%d+%d+%d" % (w, h, max(0, x), max(0, y)))
        app.dark_titlebar(self)


def ask_string(app, title, prompt, initial="", w=420):
    dlg = DarkDialog(app, title, w, 130)
    res = {"v": None}
    mklabel(dlg, prompt).pack(anchor="w", padx=14, pady=(14, 4))
    var = tk.StringVar(value=initial)
    e = mkentry(dlg, var, 50)
    e.pack(fill="x", padx=14, ipady=3)
    e.focus_set()
    e.select_range(0, "end")
    row = tk.Frame(dlg, bg=C["side"])
    row.pack(fill="x", padx=14, pady=12)

    def ok(*_):
        res["v"] = var.get()
        dlg.destroy()
    mkbtn(row, "OK", ok, primary=True).pack(side="right")
    mkbtn(row, "Отмена", dlg.destroy).pack(side="right", padx=6)
    dlg.bind("<Return>", ok)
    dlg.bind("<Escape>", lambda e: dlg.destroy())
    dlg.grab_set()
    dlg.wait_window()
    return res["v"]


# =============================================================================
#  Редактор кода: номера строк, подсветка, поиск
# =============================================================================
ASM_RX = re.compile(
    r"(?P<comment>;.*$)|(?P<string>\"[^\"]*\")|(?P<opcode>\bOp[A-Z]\w*)|(?P<id>%[\w.]+)"
    r"|(?P<number>\b0x[0-9a-fA-F]+\b|-?\b\d+(?:\.\d+)?(?:e[+-]?\d+)?\b)|(?P<enum>\b[A-Z][A-Za-z0-9_]*\b)")
GLSL_KW = set("""if else for while do switch case default break continue return discard struct
const uniform buffer in out inout layout flat smooth noperspective centroid sample patch
precision highp mediump lowp true false invariant coherent volatile restrict readonly
writeonly shared subroutine location binding set push_constant std140 std430 #""".split())
GLSL_TY = set("""void bool int uint float double vec2 vec3 vec4 ivec2 ivec3 ivec4 uvec2 uvec3 uvec4
bvec2 bvec3 bvec4 mat2 mat3 mat4 mat2x2 mat2x3 mat2x4 mat3x2 mat3x3 mat3x4 mat4x2 mat4x3 mat4x4
sampler sampler2D sampler3D samplerCube sampler2DArray sampler2DShadow texture2D textureCube
texture3D subpassInput image2D""".split())
GLSL_RX = re.compile(
    r"(?P<comment>//.*$|/\*.*?\*/)|(?P<pre>^\s*#\s*\w+)|(?P<string>\"[^\"]*\")"
    r"|(?P<number>\b0[xX][0-9a-fA-F]+u?\b|\b\d+\.?\d*(?:[eE][+-]?\d+)?[fFuU]?\b|\.\d+(?:[eE][+-]?\d+)?[fF]?)"
    r"|(?P<word>[A-Za-z_]\w*)")
ALL_TAGS = ["comment", "string", "opcode", "id", "number", "enum", "keyword", "type", "func", "pre"]


class CodeEditor(tk.Frame):
    def __init__(self, parent, app, language="plain", text="", doc=None):
        super().__init__(parent, bg=C["bg"])
        self.app, self.language, self.doc = app, language, doc
        self._loading = True
        self._hl_job = None
        self._find_cache = ""
        t = self.text = tk.Text(
            self, bg=C["bg"], fg=C["fg"], insertbackground="#aeafad", selectbackground=C["sel"],
            inactiveselectbackground="#3a3d41", undo=True, autoseparators=True, maxundo=-1,
            wrap="none", font=app.code_font, bd=0, highlightthickness=0, padx=6, pady=2,
            exportselection=False)
        self.lines = tk.Canvas(self, width=56, bg=C["bg"], highlightthickness=0, bd=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=t.yview)
        self.hsb = ttk.Scrollbar(self, orient="horizontal", command=t.xview)
        t.configure(yscrollcommand=self._yscroll, xscrollcommand=self.hsb.set)

        # --- панель поиска (скрыта)
        self.fbar = tk.Frame(self, bg=C["side"], highlightthickness=1, highlightbackground=C["border"])
        self.fvar, self.rvar = tk.StringVar(), tk.StringVar()
        self.case = tk.BooleanVar(value=False)
        self.fent = mkentry(self.fbar, self.fvar, 28)
        self.fent.grid(row=0, column=0, padx=(8, 4), pady=(6, 2), ipady=2)
        self.fcnt = mklabel(self.fbar, "", fg=C["dim"], width=10)
        self.fcnt.grid(row=0, column=1)
        for i, (txt, cmd) in enumerate([("▲", lambda: self.find(-1)), ("▼", lambda: self.find(1)),
                                        ("✕", self.hide_find)]):
            b = mkbtn(self.fbar, txt, cmd)
            b.grid(row=0, column=2 + i, padx=1, pady=(6, 2))
        tk.Checkbutton(self.fbar, text="Aa", variable=self.case, bg=C["side"], fg=C["fg"],
                       selectcolor=C["input"], activebackground=C["side"], bd=0,
                       highlightthickness=0, command=self._refresh_find).grid(row=0, column=5, padx=4)
        self.rent = mkentry(self.fbar, self.rvar, 28)
        self.rent.grid(row=1, column=0, padx=(8, 4), pady=(2, 6), ipady=2)
        mkbtn(self.fbar, "Заменить", self.replace_one).grid(row=1, column=1, columnspan=2, pady=(2, 6), sticky="we")
        mkbtn(self.fbar, "Заменить все", self.replace_all).grid(row=1, column=3, columnspan=3, pady=(2, 6), sticky="we", padx=2)

        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(1, weight=1)
        self.lines.grid(row=1, column=0, sticky="ns")
        t.grid(row=1, column=1, sticky="nsew")
        self.vsb.grid(row=1, column=2, sticky="ns")
        self.hsb.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.fbar.place(relx=1.0, x=-24, y=2, anchor="ne")
        self.fbar.place_forget()

        # --- теги
        for k, v in SYN.items():
            t.tag_configure(k, foreground=v)
        t.tag_configure("curline", background=C["cur"])
        t.tag_configure("find", background="#515c6a")
        t.tag_configure("findcur", background="#a8ac94", foreground="#000000")
        t.tag_configure("errline", background="#5a1d1d")
        t.tag_lower("curline")
        t.tag_raise("sel")
        t.tag_raise("find")
        t.tag_raise("findcur")

        t.bind("<<Modified>>", self._modified)
        t.bind("<KeyRelease>", self._cursor_moved)
        t.bind("<ButtonRelease-1>", self._cursor_moved)
        t.bind("<Configure>", lambda e: self._schedule())
        t.bind("<Return>", self._auto_indent)
        t.bind("<Tab>", lambda e: (t.insert("insert", "    "), "break")[1])
        t.bind("<Control-MouseWheel>", self._zoom)
        t.bind("<Control-Button-1>", self._goto_def)
        t.bind("<F12>", self._goto_def_cursor)
        t.bind("<Control-slash>", self._toggle_comment)
        t.bind("<Control-a>", lambda e: (t.tag_add("sel", "1.0", "end-1c"), "break")[1])
        t.bind("<Control-f>", lambda e: (self.show_find(), "break")[1])
        t.bind("<Control-h>", lambda e: (self.show_find(True), "break")[1])
        t.bind("<Button-3>", self._ctx)
        self.fent.bind("<Return>", lambda e: self.find(1))
        self.fent.bind("<Shift-Return>", lambda e: self.find(-1))
        self.fent.bind("<Escape>", lambda e: self.hide_find())
        self.rent.bind("<Escape>", lambda e: self.hide_find())
        self.fvar.trace_add("write", lambda *a: self._refresh_find())

        self.set_text(text)

    # ------------------------------------------------------------ текст
    def set_text(self, text):
        self._loading = True
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        self.text.edit_reset()
        self.text.edit_modified(False)
        self.text.mark_set("insert", "1.0")
        self._loading = False
        self._schedule()
        self._cursor_moved()

    def get_text(self):
        return self.text.get("1.0", "end-1c")

    def _modified(self, e=None):
        if self.text.edit_modified():
            self.text.edit_modified(False)
            if not self._loading and self.doc:
                self.doc.set_modified(True)
            self._schedule()

    def _yscroll(self, a, b):
        self.vsb.set(a, b)
        self._schedule()

    def _schedule(self):
        if self._hl_job is None:
            self._hl_job = self.after(40, self._refresh)

    def _refresh(self):
        self._hl_job = None
        self.redraw_lines()
        self.highlight()

    def redraw_lines(self):
        t, c = self.text, self.lines
        total = int(t.index("end-1c").split(".")[0])
        w = self.app.code_font.measure("0") * max(3, len(str(total))) + 22
        if int(c.cget("width")) != w:
            c.config(width=w)
        c.delete("all")
        cur = int(t.index("insert").split(".")[0])
        i = t.index("@0,0")
        last = -1
        while True:
            d = t.dlineinfo(i)
            ln = int(i.split(".")[0])
            if d is None or ln == last:
                break
            c.create_text(w - 10, d[1], anchor="ne", text=str(ln), font=self.app.code_font,
                          fill="#c6c6c6" if ln == cur else C["dim"])
            last = ln
            i = t.index("%s+1line" % i)

    def highlight(self):
        if self.language == "plain":
            return
        t = self.text
        total = int(t.index("end-1c").split(".")[0])
        first = int(t.index("@0,0").split(".")[0])
        lastv = int(t.index("@0,%d" % t.winfo_height()).split(".")[0])
        s, e = max(1, first - 150), min(total, lastv + 150)
        for tag in ALL_TAGS:
            t.tag_remove(tag, "%d.0" % s, "%d.end" % e)
        rx = ASM_RX if self.language == "asm" else GLSL_RX
        body = t.get("%d.0" % s, "%d.end" % e).split("\n")
        add = t.tag_add
        for off, line in enumerate(body):
            if not line.strip():
                continue
            ln = s + off
            for m in rx.finditer(line):
                kind = m.lastgroup
                if kind == "word":
                    w = m.group()
                    if w in GLSL_KW:
                        kind = "keyword"
                    elif w in GLSL_TY:
                        kind = "type"
                    elif line[m.end():m.end() + 1] == "(":
                        kind = "func"
                    elif w.startswith("gl_"):
                        kind = "id"
                    else:
                        continue
                add(kind, "%d.%d" % (ln, m.start()), "%d.%d" % (ln, m.end()))

    def _cursor_moved(self, e=None):
        t = self.text
        t.tag_remove("curline", "1.0", "end")
        t.tag_add("curline", "insert linestart", "insert lineend+1c")
        self.app.update_cursor(self)
        self.redraw_lines()

    # ------------------------------------------------------------ редактирование
    def _auto_indent(self, e):
        line = self.text.get("insert linestart", "insert")
        indent = re.match(r"\s*", line).group()
        self.text.insert("insert", "\n" + indent)
        self.text.see("insert")
        return "break"

    def _zoom(self, e):
        self.app.zoom(1 if e.delta > 0 else -1)
        return "break"

    def _toggle_comment(self, e=None):
        t = self.text
        pre = ";" if self.language == "asm" else "//"
        try:
            a, b = t.index("sel.first linestart"), t.index("sel.last lineend")
        except tk.TclError:
            a, b = t.index("insert linestart"), t.index("insert lineend")
        la, lb = int(a.split(".")[0]), int(b.split(".")[0])
        lines = [t.get("%d.0" % i, "%d.end" % i) for i in range(la, lb + 1)]
        uncomment = all(l.lstrip().startswith(pre) or not l.strip() for l in lines)
        t.edit_separator()
        for i, l in enumerate(lines, la):
            if uncomment:
                n = re.sub(r"^(\s*)" + re.escape(pre) + r" ?", r"\1", l, count=1)
            else:
                n = pre + " " + l if l.strip() else l
            t.delete("%d.0" % i, "%d.end" % i)
            t.insert("%d.0" % i, n)
        t.edit_separator()
        return "break"

    def _ctx(self, e):
        m = tk.Menu(self, tearoff=0, bg=C["side"], fg=C["fg"], activebackground=C["tree_sel"],
                    activeforeground="#fff", bd=0)
        m.add_command(label="Cut", command=lambda: self.text.event_generate("<<Cut>>"))
        m.add_command(label="Copy", command=lambda: self.text.event_generate("<<Copy>>"))
        m.add_command(label="Paste", command=lambda: self.text.event_generate("<<Paste>>"))
        m.add_separator()
        m.add_command(label="Select All", command=lambda: self.text.tag_add("sel", "1.0", "end-1c"))
        m.add_command(label="Find / Replace…", command=self.show_find)
        m.add_command(label="Go to Definition (F12)", command=self._goto_def_cursor)
        m.tk_popup(e.x_root, e.y_root)

    # ------------------------------------------------------------ переход к определению %id
    def _id_at(self, index):
        line = self.text.get("%s linestart" % index, "%s lineend" % index)
        col = int(index.split(".")[1])
        for m in re.finditer(r"%[\w.]+", line):
            if m.start() <= col <= m.end():
                return m.group()
        return None

    def _goto(self, ident):
        if not ident:
            return
        pos = self.text.search(r"^\s*" + re.escape(ident) + r"\s*=", "1.0", "end", regexp=True)
        if pos:
            self.goto_line(int(pos.split(".")[0]))
        else:
            self.app.status("Определение %s не найдено" % ident)

    def _goto_def(self, e):
        self._goto(self._id_at(self.text.index("@%d,%d" % (e.x, e.y))))
        return "break"

    def _goto_def_cursor(self, e=None):
        self._goto(self._id_at(self.text.index("insert")))
        return "break"

    def goto_line(self, n, flash=False):
        t = self.text
        t.mark_set("insert", "%d.0" % n)
        t.see("%d.0" % n)
        t.focus_set()
        self._cursor_moved()
        if flash:
            t.tag_remove("errline", "1.0", "end")
            t.tag_add("errline", "%d.0" % n, "%d.end+1c" % n)
            self.after(2500, lambda: t.tag_remove("errline", "1.0", "end"))

    # ------------------------------------------------------------ поиск
    def show_find(self, replace=False):
        self.fbar.place(relx=1.0, x=-24, y=2, anchor="ne")
        self.fbar.lift()
        try:
            sel = self.text.get("sel.first", "sel.last")
            if sel and "\n" not in sel:
                self.fvar.set(sel)
        except tk.TclError:
            pass
        (self.rent if replace else self.fent).focus_set()
        self.fent.select_range(0, "end")
        self._refresh_find()

    def hide_find(self):
        self.fbar.place_forget()
        self.text.tag_remove("find", "1.0", "end")
        self.text.tag_remove("findcur", "1.0", "end")
        self.text.focus_set()

    def _refresh_find(self):
        t = self.text
        t.tag_remove("find", "1.0", "end")
        pat = self.fvar.get()
        if not pat:
            self.fcnt.config(text="")
            return
        n, idx = 0, "1.0"
        while n < 5000:
            idx = t.search(pat, idx, "end", nocase=not self.case.get())
            if not idx:
                break
            end = "%s+%dc" % (idx, len(pat))
            t.tag_add("find", idx, end)
            idx = end
            n += 1
        self.fcnt.config(text=("%d найд." % n) if n else "нет")

    def find(self, direction=1):
        t, pat = self.text, self.fvar.get()
        if not pat:
            return
        nc = not self.case.get()
        if direction > 0:
            start = t.index("insert")
            try:
                if t.tag_ranges("sel"):
                    start = t.index("sel.last")
            except tk.TclError:
                pass
            idx = t.search(pat, start, "end", nocase=nc) or t.search(pat, "1.0", "end", nocase=nc)
        else:
            start = t.index("sel.first") if t.tag_ranges("sel") else t.index("insert")
            idx = t.search(pat, start, "1.0", backwards=True, nocase=nc) or \
                t.search(pat, "end", "1.0", backwards=True, nocase=nc)
        if not idx:
            self.fcnt.config(text="нет")
            return
        end = "%s+%dc" % (idx, len(pat))
        t.tag_remove("sel", "1.0", "end")
        t.tag_add("sel", idx, end)
        t.mark_set("insert", end if direction > 0 else idx)
        t.see(idx)
        self._cursor_moved()

    def replace_one(self):
        t = self.text
        if t.tag_ranges("sel") and t.get("sel.first", "sel.last").lower() == self.fvar.get().lower():
            a = t.index("sel.first")
            t.delete("sel.first", "sel.last")
            t.insert(a, self.rvar.get())
        self.find(1)

    def replace_all(self):
        t, pat = self.text, self.fvar.get()
        if not pat:
            return
        n, idx = 0, "1.0"
        t.edit_separator()
        while True:
            idx = t.search(pat, idx, "end", nocase=not self.case.get())
            if not idx:
                break
            t.delete(idx, "%s+%dc" % (idx, len(pat)))
            t.insert(idx, self.rvar.get())
            idx = "%s+%dc" % (idx, len(self.rvar.get()))
            n += 1
        t.edit_separator()
        self.fcnt.config(text="заменено %d" % n)
        self._refresh_find()


# =============================================================================
#  Документы (вкладки)
# =============================================================================
class Doc:
    kind = "text"
    language = "plain"

    def __init__(self, app, path):
        self.app, self.path = app, path
        self.modified = False
        self.view = None
        self.tab = None

    @property
    def title(self):
        return os.path.basename(self.path)

    def set_modified(self, v):
        if self.modified != v:
            self.modified = v
            self.app.refresh_tab(self)

    def save(self):
        return True

    def build(self):
        return self.save()

    def focus(self):
        pass

    def status_lang(self):
        return "Plain Text"


class EditorDoc(Doc):
    def make_editor(self, parent, lang, text):
        self.ed = CodeEditor(parent, self.app, lang, text, doc=self)
        self.view = self.ed
        self.language = lang

    def focus(self):
        self.ed.text.focus_set()
        self.ed._schedule()


class TextDoc(EditorDoc):
    def __init__(self, app, path, parent):
        super().__init__(app, path)
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
            text = f.read().replace("\r\n", "\n")
        n = path.lower()
        lang = "asm" if n.endswith(".spvasm") else \
            "glsl" if re.search(r"\.(glsl|vert|frag|comp|vs|fs|geom|inc|h)$", n) else "plain"
        self.make_editor(parent, lang, text)

    def status_lang(self):
        return {"asm": "SPIR-V Assembly", "glsl": "GLSL", "plain": "Plain Text"}[self.language]

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8", newline="\n") as f:
                f.write(self.ed.get_text())
        except Exception as e:
            self.app.log("Не удалось сохранить: %s" % e, "err")
            return False
        self.set_modified(False)
        self.app.log("Сохранено: %s" % self.path, "ok")
        return True

    def build(self):
        """Скомпилировать текст в соседний .spv"""
        mode = "asm" if self.language == "asm" else "glsl" if self.language == "glsl" else None
        if not mode:
            self.app.log("Этот файл нельзя скомпилировать (нужен .spvasm / .glsl).", "err")
            return False
        if self.modified:
            self.save()
        out = os.path.splitext(self.path)[0] + ".spv"
        return self.app.compile_to(self, self.ed.get_text(), mode, out)


class SpvDoc(EditorDoc):
    kind = "spv"

    def __init__(self, app, path, parent):
        super().__init__(app, path)
        self.mode = app.cfg["mode"]
        with open(path, "rb") as f:
            self.orig = f.read()
        text = app.core.decompile_file(path, self.mode, app.cfg["friendly"])
        self.make_editor(parent, self.mode, text)

    def status_lang(self):
        return "SPIR-V Assembly" if self.mode == "asm" else "GLSL (spirv-cross)"

    def save(self):
        return self.build()

    def build(self):
        ok = self.app.compile_to(self, self.ed.get_text(), self.mode, self.path)
        if ok:
            self.set_modified(False)
        return ok

    def reload(self):
        self.mode = self.app.cfg["mode"]
        text = self.app.core.decompile_file(self.path, self.mode, self.app.cfg["friendly"])
        self.ed.language = self.language = self.mode
        self.ed.set_text(text)
        self.set_modified(False)
        with open(self.path, "rb") as f:
            self.orig = f.read()


class RefDoc(Doc):
    kind = "ref"

    def __init__(self, app, path, parent):
        super().__init__(app, path)
        with open(path, "rb") as f:
            self.data = bytearray(f.read())
        self.entries = scan_ref(self.data)
        v = self.view = tk.Frame(parent, bg=C["bg"])
        top = tk.Frame(v, bg=C["side"])
        top.pack(fill="x")
        hdr = struct.unpack("<7I", bytes(self.data[:28])) if len(self.data) >= 28 else ()
        mklabel(top, "  %s   |   %d байт   |   заголовок: %s" % (
            os.path.basename(path), len(self.data), " ".join("%X" % x for x in hdr)),
            fg=C["dim"]).pack(side="left", pady=6)
        self.fvar = tk.StringVar()
        mkentry(top, self.fvar, 24).pack(side="right", padx=8, pady=4, ipady=2)
        mklabel(top, "Фильтр:", fg=C["dim"]).pack(side="right")
        self.mode_var = tk.StringVar(value="table")
        for txt, val in (("Таблица", "table"), ("Hex", "hex")):
            tk.Radiobutton(top, text=txt, value=val, variable=self.mode_var, indicatoron=False,
                           bg=C["bar"], fg=C["fg"], selectcolor=C["btn"], bd=0, padx=10,
                           activebackground=C["btn_h"], command=self.switch).pack(side="right", padx=1)
        self.body = tk.Frame(v, bg=C["bg"])
        self.body.pack(fill="both", expand=True)
        cols = ("idx", "off", "name", "cap", "tail", "dec")
        self.tree = ttk.Treeview(self.body, columns=cols, show="headings", style="Dark.Treeview")
        for c, t, w in (("idx", "#", 50), ("off", "Offset", 80), ("name", "Имя (двойной клик — переименовать)", 300),
                        ("cap", "Макс.длина", 80), ("tail", "Следующие 8 байт (двойной клик — править)", 260),
                        ("dec", "type / count / offset / flags", 260)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w", stretch=(c in ("name", "dec")))
        sb = ttk.Scrollbar(self.body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.hex = tk.Text(self.body, bg=C["bg"], fg=C["fg"], font=app.code_font, bd=0,
                           highlightthickness=0, wrap="none", state="disabled")
        self.tree.bind("<Double-1>", self.edit)
        self.fvar.trace_add("write", lambda *a: self.fill())
        self.fill()

    def tail(self, e):
        return bytes(self.data[e["off"] + 28:e["off"] + 36])

    def fill(self):
        flt = self.fvar.get().lower()
        self.tree.delete(*self.tree.get_children())
        for i, e in enumerate(self.entries):
            if flt and flt not in e["name"].lower():
                continue
            tl = self.tail(e)
            dec = ""
            if len(tl) == 8:
                t, c, o, f = struct.unpack("<4H", tl)
                dec = "t=0x%04X  n=%d  off=0x%X  f=0x%04X" % (t, c, o, f)
            self.tree.insert("", "end", iid=str(i), values=(
                i, "0x%X" % e["off"], e["name"], e["cap"], tl.hex(" ").upper(), dec))
        self.fill_hex()

    def fill_hex(self):
        d = bytes(self.data)
        lines = []
        for o in range(0, len(d), 16):
            ch = d[o:o + 16]
            lines.append("%08X  %-47s  %s" % (o, " ".join("%02X" % b for b in ch),
                                              "".join(chr(b) if 32 <= b < 127 else "." for b in ch)))
        self.hex.config(state="normal")
        self.hex.delete("1.0", "end")
        self.hex.insert("1.0", "\n".join(lines))
        self.hex.config(state="disabled")

    def switch(self):
        if self.mode_var.get() == "hex":
            self.tree.pack_forget()
            self.hex.pack(side="left", fill="both", expand=True)
        else:
            self.hex.pack_forget()
            self.tree.pack(side="left", fill="both", expand=True)

    def edit(self, ev):
        iid = self.tree.identify_row(ev.y)
        col = self.tree.identify_column(ev.x)
        if not iid:
            return
        e = self.entries[int(iid)]
        if col == "#3":
            new = ask_string(self.app, "Переименование", "Новое имя (до %d символов):" % e["cap"], e["name"])
            if new is None or new == e["name"]:
                return
            if not re.fullmatch(r"[A-Za-z0-9_$.]+", new) or len(new) > e["cap"]:
                messagebox.showerror(APP_NAME, "Имя должно быть ASCII без пробелов и не длиннее %d символов." % e["cap"])
                return
            slot_end = e["off"] + e["len"]
            while slot_end < len(self.data) and self.data[slot_end] == 0 and slot_end < e["off"] + 28:
                slot_end += 1
            slot = e["off"], max(slot_end, e["off"] + e["len"])
            self.data[slot[0]:slot[1]] = new.encode("ascii").ljust(slot[1] - slot[0], b"\x00")
            e["name"], e["len"] = new, len(new)
        elif col == "#5":
            cur = self.tail(e).hex(" ").upper()
            new = ask_string(self.app, "Правка байт", "8 байт в hex (например 04 00 01 00 20 00 00 FF):", cur, 520)
            if new is None:
                return
            try:
                b = bytes.fromhex(new.replace(" ", ""))
                assert len(b) == 8
            except Exception:
                messagebox.showerror(APP_NAME, "Нужно ровно 8 байт в hex.")
                return
            self.data[e["off"] + 28:e["off"] + 36] = b
        else:
            return
        self.set_modified(True)
        self.fill()

    def save(self):
        try:
            backup_once(self.path)
            with open(self.path, "wb") as f:
                f.write(self.data)
        except Exception as ex:
            self.app.log("Не удалось сохранить .ref: %s" % ex, "err")
            return False
        self.set_modified(False)
        self.app.log("Сохранено: %s" % self.path, "ok")
        return True

    def status_lang(self):
        return "Shader Reflection (.ref)"


# =============================================================================
#  Диалоги: настройки, массовые операции
# =============================================================================
class SettingsDialog(DarkDialog):
    def __init__(self, app):
        super().__init__(app, "Settings", 700, 380)
        self.vars = {}
        mklabel(self, "Пути к утилитам (пусто = автопоиск):", font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=14, pady=(12, 6))
        grid = tk.Frame(self, bg=C["side"])
        grid.pack(fill="x", padx=14)
        for r, n in enumerate(Core.NAMES):
            mklabel(grid, n, width=16, anchor="w").grid(row=r, column=0, pady=3, sticky="w")
            v = tk.StringVar(value=app.cfg["tools"].get(n, ""))
            self.vars[n] = v
            mkentry(grid, v, 52).grid(row=r, column=1, padx=6, ipady=2)
            mkbtn(grid, "…", lambda n=n: self.browse(n)).grid(row=r, column=2)
            st = mklabel(grid, "", width=3)
            st.grid(row=r, column=3)
            self.vars["_st_" + n] = st
        row2 = tk.Frame(self, bg=C["side"])
        row2.pack(fill="x", padx=14, pady=10)
        mklabel(row2, "Потоков для массовых операций:").pack(side="left")
        self.workers = tk.IntVar(value=app.cfg["workers"])
        tk.Spinbox(row2, from_=1, to=64, textvariable=self.workers, width=4, bg=C["input"], fg=C["fg"],
                   buttonbackground=C["bar"], relief="flat", insertbackground="#fff").pack(side="left", padx=8)
        btns = tk.Frame(self, bg=C["side"])
        btns.pack(fill="x", padx=14, pady=8, side="bottom")
        mkbtn(btns, "Сохранить", self.ok, primary=True).pack(side="right")
        mkbtn(btns, "Отмена", self.destroy).pack(side="right", padx=6)
        mkbtn(btns, "Найти автоматически", self.redetect).pack(side="left")
        self.status()
        self.grab_set()

    def browse(self, n):
        p = filedialog.askopenfilename(parent=self, title=n)
        if p:
            self.vars[n].set(p)
            self.status()

    def status(self):
        tmp = Core({"tools": {n: self.vars[n].get() for n in Core.NAMES}})
        for n in Core.NAMES:
            ok = bool(tmp.paths.get(n))
            self.vars["_st_" + n].config(text="✔" if ok else "✘", fg=C["green"] if ok else C["red"])
        self.app_tmp = tmp

    def redetect(self):
        for n in Core.NAMES:
            self.vars[n].set("")
        self.status()
        for n in Core.NAMES:
            if self.app_tmp.paths.get(n):
                self.vars[n].set(self.app_tmp.paths[n])

    def ok(self):
        self.app.cfg["tools"] = {n: self.vars[n].get() for n in Core.NAMES if self.vars[n].get()}
        self.app.cfg["workers"] = max(1, int(self.workers.get()))
        save_cfg(self.app.cfg)
        self.app.core.detect()
        self.app.update_tools_status()
        self.destroy()


class BatchDialog(DarkDialog):
    def __init__(self, app, kind):
        super().__init__(app, "Массовая декомпиляция" if kind == "decompile" else "Массовая компиляция", 760, 640)
        self.kind, self.q, self.cancel, self.thread = kind, queue.Queue(), threading.Event(), None
        root = app.root_dir or app.cfg.get("last_dir") or ""
        dec = kind == "decompile"
        self.src = tk.StringVar(value=root if dec else (os.path.join(root, "_decompiled") if root else ""))
        self.dst = tk.StringVar(value=os.path.join(root, "_decompiled") if dec and root else root)
        self.mode = tk.StringVar(value=MODES[app.cfg["mode"]])
        self.pattern = tk.StringVar(value="*.spv" if dec else "*" + MODE_EXT[app.cfg["mode"]])
        self.recursive = tk.BooleanVar(value=False)
        self.opt1 = tk.BooleanVar(value=False if dec else True)   # skip existing / only changed
        self.opt2 = tk.BooleanVar(value=app.cfg["friendly"] if dec else True)  # friendly / backup
        self.opt3 = tk.BooleanVar(value=False if dec else True)   # - / validate
        title = "Декомпиляция .spv  →  текст" if dec else "Компиляция текст  →  .spv"
        mklabel(self, title, font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=14, pady=(12, 8))
        g = tk.Frame(self, bg=C["side"])
        g.pack(fill="x", padx=14)
        rows = [("Исходная папка:", self.src), ("Папка назначения:", self.dst)]
        for r, (lbl, var) in enumerate(rows):
            mklabel(g, lbl, width=18, anchor="w").grid(row=r, column=0, pady=3, sticky="w")
            mkentry(g, var, 60).grid(row=r, column=1, padx=6, ipady=3, sticky="we")
            mkbtn(g, "…", lambda v=var: self.pick(v)).grid(row=r, column=2)
        mklabel(g, "Формат:", width=18, anchor="w").grid(row=2, column=0, pady=3, sticky="w")
        cb = ttk.Combobox(g, textvariable=self.mode, values=list(MODES.values()), state="readonly", width=30)
        cb.grid(row=2, column=1, sticky="w", padx=6)
        cb.bind("<<ComboboxSelected>>", self.mode_changed)
        mklabel(g, "Маска файлов (; через):", width=18, anchor="w").grid(row=3, column=0, pady=3, sticky="w")
        mkentry(g, self.pattern, 30).grid(row=3, column=1, sticky="w", padx=6, ipady=3)
        g.grid_columnconfigure(1, weight=1)
        o = tk.Frame(self, bg=C["side"])
        o.pack(fill="x", padx=14, pady=6)
        mkcheck(o, "Включая подпапки", self.recursive).pack(anchor="w")
        if dec:
            mkcheck(o, "Пропускать уже существующие файлы", self.opt1).pack(anchor="w")
            mkcheck(o, "Дружественные имена (%main вместо %4; результат уже не побайтно идентичен)", self.opt2).pack(anchor="w")
        else:
            mkcheck(o, "Только изменённые (по сравнению с моментом декомпиляции)", self.opt1).pack(anchor="w")
            mkcheck(o, "Делать резервную копию .bak при перезаписи .spv", self.opt2).pack(anchor="w")
            mkcheck(o, "Проверять результат через spirv-val", self.opt3).pack(anchor="w")
        pf = tk.Frame(self, bg=C["side"])
        pf.pack(fill="x", padx=14, pady=(8, 2))
        self.pb = ttk.Progressbar(pf, style="Dark.Horizontal.TProgressbar", mode="determinate")
        self.pb.pack(fill="x")
        self.lbl = mklabel(self, "Готово к запуску", fg=C["dim"])
        self.lbl.pack(anchor="w", padx=14)
        self.log = tk.Text(self, bg=C["bg"], fg=C["fg"], font=("Consolas", 9), bd=0, height=10,
                           highlightthickness=1, highlightbackground=C["border"], wrap="none")
        self.log.pack(fill="both", expand=True, padx=14, pady=6)
        self.log.tag_configure("err", foreground=C["red"])
        self.log.tag_configure("ok", foreground=C["green"])
        self.log.tag_configure("dim", foreground=C["dim"])
        bt = tk.Frame(self, bg=C["side"])
        bt.pack(fill="x", padx=14, pady=(0, 12))
        self.go = mkbtn(bt, "▶  Запустить", self.start, primary=True)
        self.go.pack(side="right")
        self.stop = mkbtn(bt, "Остановить", lambda: self.cancel.set())
        self.stop.pack(side="right", padx=6)
        mkbtn(bt, "Закрыть", self.close).pack(side="left")
        self.n = self.total = 0
        self.protocol("WM_DELETE_WINDOW", self.close)

    def mode_changed(self, e=None):
        m = "asm" if self.mode.get() == MODES["asm"] else "glsl"
        if self.kind == "compile":
            self.pattern.set("*" + MODE_EXT[m])

    def pick(self, var):
        p = filedialog.askdirectory(parent=self, initialdir=var.get() or None)
        if p:
            var.set(os.path.normpath(p))

    def close(self):
        if self.thread and self.thread.is_alive():
            self.cancel.set()
        self.destroy()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        src, dst = self.src.get().strip(), self.dst.get().strip()
        if not os.path.isdir(src):
            messagebox.showerror(APP_NAME, "Исходная папка не существует.", parent=self)
            return
        if not dst:
            messagebox.showerror(APP_NAME, "Укажите папку назначения.", parent=self)
            return
        mode = "asm" if self.mode.get() == MODES["asm"] else "glsl"
        try:
            core = self.app.core
            if self.kind == "decompile":
                core.need("spirv-dis" if mode == "asm" else "spirv-cross")
            else:
                core.need("spirv-as" if mode == "asm" else "glslangValidator")
                if self.opt3.get():
                    core.need("spirv-val")
        except ToolError as e:
            messagebox.showerror(APP_NAME, str(e), parent=self)
            return
        if self.kind == "compile" and os.path.abspath(dst) == os.path.abspath(self.app.root_dir or "") \
                and not self.opt2.get():
            if not messagebox.askyesno(APP_NAME, "Файлы .spv будут перезаписаны БЕЗ резервных копий. Продолжить?", parent=self):
                return
        o = dict(src=src, dst=dst, mode=mode, pattern=self.pattern.get(), recursive=self.recursive.get(),
                 workers=self.app.cfg["workers"])
        if self.kind == "decompile":
            o.update(skip_existing=self.opt1.get(), friendly=self.opt2.get())
        else:
            o.update(only_changed=self.opt1.get(), backup=self.opt2.get(), validate=self.opt3.get())
        self.log.delete("1.0", "end")
        self.cancel.clear()
        self.n = self.total = 0
        self.pb.config(value=0)
        self.go.config(state="disabled")
        self.t0 = time.time()
        self.thread = BatchRunner(self.app.core, self.kind, o, self.q, self.cancel)
        self.thread.start()
        self.after(60, self.poll)

    def poll(self):
        done = None
        for _ in range(400):
            try:
                m = self.q.get_nowait()
            except queue.Empty:
                break
            if m[0] == "total":
                self.total = m[1]
                self.pb.config(maximum=max(1, m[1]))
                self.log.insert("end", "Найдено файлов: %d\n" % m[1], "dim")
            elif m[0] == "res":
                self.n += 1
                st, rel, info = m[1], m[2], m[3]
                if st == "err":
                    self.log.insert("end", "✘ %s — %s\n" % (rel, info), "err")
                elif st == "ok" and self.total <= 400:
                    self.log.insert("end", "✔ %s %s\n" % (rel, info), "ok")
                elif st == "skip" and self.total <= 400:
                    self.log.insert("end", "– %s (%s)\n" % (rel, info), "dim")
            elif m[0] == "done":
                done = m[1]
        self.pb.config(value=self.n)
        self.lbl.config(text="%d / %d    %.1f c" % (self.n, self.total, time.time() - self.t0))
        self.log.see("end")
        if done:
            self.log.insert("end", "\n" + done + "\n", "ok")
            self.log.see("end")
            self.go.config(state="normal")
            self.app.log("[%s] %s" % ("Mass decompile" if self.kind == "decompile" else "Mass compile", done), "ok")
            self.app.refresh_explorer()
        elif self.thread.is_alive() or not self.q.empty():
            self.after(60, self.poll)


# =============================================================================
#  Главное приложение
# =============================================================================
class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_cfg()
        self.core = Core(self.cfg)
        self.docs, self.cur, self.root_dir = [], None, None
        self.node_path = {}
        root.title(APP_NAME)
        root.geometry("1500x900")
        root.minsize(900, 560)
        root.configure(bg=C["bg"])
        self.code_font = tkfont.Font(family=self.pick_font(), size=self.cfg["font_size"])
        self.setup_style()
        self.build_menu()
        self.build_toolbar()
        self.build_body()
        self.build_status()
        self.bind_keys()
        self.dark_titlebar(root)
        self.update_tools_status()
        self.show_welcome()
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.log("%s %s запущен." % (APP_NAME, APP_VER), "dim")
        missing = [n for n in Core.NAMES if not self.core.paths.get(n)]
        if missing:
            self.log("Не найдены утилиты: %s. Положите их в папку tools/ или укажите путь в Tools → Settings."
                     % ", ".join(missing), "err")
        last = self.cfg.get("last_dir")
        if last and os.path.isdir(last):
            self.set_root(last)

    # -------------------------------------------------------------- оформление
    def pick_font(self):
        fams = set(tkfont.families())
        for f in ("Cascadia Code", "Consolas", "DejaVu Sans Mono", "Menlo", "Courier New"):
            if f in fams:
                return f
        return "TkFixedFont"

    def dark_titlebar(self, win):
        if os.name != "nt":
            return
        try:
            import ctypes
            win.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
            v = ctypes.c_int(1)
            for attr in (20, 19):
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), 4)
        except Exception:
            pass

    def setup_style(self):
        r = self.root
        s = ttk.Style(r)
        s.theme_use("clam")
        s.configure("Dark.Treeview", background=C["side"], fieldbackground=C["side"],
                    foreground=C["fg"], rowheight=22, borderwidth=0, font=("Segoe UI", 9))
        s.map("Dark.Treeview", background=[("selected", C["tree_sel"])], foreground=[("selected", "#ffffff")])
        s.configure("Dark.Treeview.Heading", background=C["bar"], foreground=C["fg"], relief="flat",
                    font=("Segoe UI", 9, "bold"), borderwidth=0)
        s.map("Dark.Treeview.Heading", background=[("active", "#4a4a4a")])
        s.layout("Dark.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        for o in ("Vertical", "Horizontal"):
            s.configure(o + ".TScrollbar", background="#424242", troughcolor=C["bg"], bordercolor=C["bg"],
                        arrowcolor="#c5c5c5", lightcolor="#424242", darkcolor="#424242", gripcount=0, relief="flat")
            s.map(o + ".TScrollbar", background=[("active", "#686868"), ("pressed", "#9e9e9e")])
        s.configure("Dark.Horizontal.TProgressbar", background=C["accent"], troughcolor=C["input"],
                    bordercolor=C["side"], lightcolor=C["accent"], darkcolor=C["accent"])
        s.configure("TCombobox", fieldbackground=C["input"], background=C["bar"], foreground=C["fg"],
                    arrowcolor=C["fg"], bordercolor=C["input"], lightcolor=C["input"], darkcolor=C["input"])
        s.map("TCombobox", fieldbackground=[("readonly", C["input"])], foreground=[("readonly", C["fg"])],
              selectbackground=[("readonly", C["input"])], selectforeground=[("readonly", C["fg"])])
        r.option_add("*TCombobox*Listbox.background", C["input"])
        r.option_add("*TCombobox*Listbox.foreground", C["fg"])
        r.option_add("*TCombobox*Listbox.selectBackground", C["tree_sel"])

    def mkmenu(self, parent):
        return tk.Menu(parent, tearoff=0, bg=C["side"], fg=C["fg"], activebackground=C["tree_sel"],
                       activeforeground="#ffffff", bd=0, relief="flat", font=("Segoe UI", 9))

    def build_menu(self):
        bar = tk.Frame(self.root, bg=C["menu"])
        bar.pack(fill="x")
        mklabel(bar, " ◆ SKY SHADER STUDIO ", bg=C["menu"], fg="#c586c0", font=("Segoe UI", 9, "bold")).pack(side="left")
        self.menubar = bar
        defs = [
            ("File", [("Open Folder…", self.open_folder, "Ctrl+Shift+O"), ("Open File…", self.open_file, "Ctrl+O"),
                      None, ("Save", self.cmd_save, "Ctrl+S"), ("Save All", self.save_all, ""),
                      None, ("Close Tab", self.close_current, "Ctrl+W"), None, ("Exit", self.quit, "Alt+F4")]),
            ("Edit", [("Undo", lambda: self.ed_event("<<Undo>>"), "Ctrl+Z"), ("Redo", lambda: self.ed_event("<<Redo>>"), "Ctrl+Y"),
                      None, ("Cut", lambda: self.ed_event("<<Cut>>"), "Ctrl+X"), ("Copy", lambda: self.ed_event("<<Copy>>"), "Ctrl+C"),
                      ("Paste", lambda: self.ed_event("<<Paste>>"), "Ctrl+V"),
                      None, ("Find / Replace", self.cmd_find, "Ctrl+F"), ("Go to Line…", self.cmd_goto, "Ctrl+G"),
                      ("Toggle Comment", lambda: self.cur_ed() and self.cur_ed()._toggle_comment(), "Ctrl+/")]),
            ("Build", [("Compile Current → .spv", self.cmd_build, "F5"), ("Decompile Current → file…", self.cmd_decompile_current, "F6"),
                       ("Validate Current (spirv-val)", self.cmd_validate, "F7"),
                       None, ("Mass Decompile…", lambda: self.batch("decompile"), "Ctrl+Shift+D"),
                       ("Mass Compile…", lambda: self.batch("compile"), "Ctrl+Shift+B"),
                       None, ("Reload Current from .spv", self.cmd_reload, "")]),
            ("View", [("Zoom In", lambda: self.zoom(1), "Ctrl++"), ("Zoom Out", lambda: self.zoom(-1), "Ctrl+-"),
                      None, ("Toggle Explorer", self.toggle_side, "Ctrl+B"), ("Toggle Output Panel", self.toggle_panel, "Ctrl+J")]),
            ("Tools", [("Settings…", lambda: SettingsDialog(self), "")]),
            ("Help", [("About", self.about, "")]),
        ]
        for label, items in defs:
            mb = tk.Menubutton(bar, text=label, bg=C["menu"], fg=C["fg"], activebackground="#505050",
                               activeforeground="#fff", bd=0, padx=9, pady=4, font=("Segoe UI", 9))
            m = self.mkmenu(mb)
            for it in items:
                if it is None:
                    m.add_separator()
                else:
                    m.add_command(label=it[0], command=it[1], accelerator=it[2])
            mb["menu"] = m
            mb.pack(side="left")

    def build_toolbar(self):
        tb = tk.Frame(self.root, bg=C["bar"])
        tb.pack(fill="x")
        def sep():
            tk.Frame(tb, bg="#555555", width=1).pack(side="left", fill="y", padx=6, pady=4)
        mkbtn(tb, "📁 Open Folder", self.open_folder).pack(side="left", padx=(8, 1), pady=4)
        mkbtn(tb, "💾 Save", self.cmd_save).pack(side="left", padx=1)
        sep()
        mkbtn(tb, "▶ Compile", self.cmd_build, primary=True).pack(side="left", padx=1)
        mkbtn(tb, "⇩ Decompile", self.cmd_decompile_current).pack(side="left", padx=1)
        mkbtn(tb, "✔ Validate", self.cmd_validate).pack(side="left", padx=1)
        sep()
        mkbtn(tb, "⇩⇩ Mass Decompile", lambda: self.batch("decompile"), primary=True).pack(side="left", padx=1)
        mkbtn(tb, "⇧⇧ Mass Compile", lambda: self.batch("compile"), primary=True).pack(side="left", padx=1)
        sep()
        mklabel(tb, "Формат:", bg=C["bar"]).pack(side="left")
        self.mode_var = tk.StringVar(value=MODES[self.cfg["mode"]])
        cb = ttk.Combobox(tb, textvariable=self.mode_var, values=list(MODES.values()), state="readonly", width=20)
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", self.mode_changed)
        self.friendly = tk.BooleanVar(value=self.cfg["friendly"])
        tk.Checkbutton(tb, text="Friendly names", variable=self.friendly, bg=C["bar"], fg=C["fg"],
                       selectcolor=C["input"], activebackground=C["bar"], activeforeground=C["fg"], bd=0,
                       highlightthickness=0, font=("Segoe UI", 9), command=self.friendly_changed).pack(side="left", padx=6)

    def build_body(self):
        self.hpane = tk.PanedWindow(self.root, orient="horizontal", bg=C["border"], sashwidth=3, bd=0,
                                    sashrelief="flat", opaqueresize=True)
        self.hpane.pack(fill="both", expand=True)
        # ---- Explorer
        self.side = tk.Frame(self.hpane, bg=C["side"])
        mklabel(self.side, "EXPLORER", bg=C["side"], fg=C["dim"], font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=14, pady=(10, 4))
        self.search = tk.StringVar()
        e = mkentry(self.side, self.search, 20)
        e.pack(fill="x", padx=10, pady=(0, 6), ipady=3)
        self._search_job = None
        self.search.trace_add("write", lambda *a: self.schedule_search())
        tf = tk.Frame(self.side, bg=C["side"])
        tf.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tf, show="tree", style="Dark.Treeview", selectmode="browse")
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        for tag, col in (("spv", SYN["enum"]), ("ref", SYN["func"]), ("asm", SYN["opcode"]), ("glsl", SYN["pre"]),
                         ("dir", C["fg"]), ("root", "#ffffff")):
            self.tree.tag_configure(tag, foreground=col)
        self.tree.bind("<<TreeviewOpen>>", self.on_tree_open)
        self.tree.bind("<Double-1>", self.on_tree_dbl)
        self.tree.bind("<Return>", self.on_tree_dbl)
        self.tree.bind("<Button-3>", self.tree_ctx)
        self.count_lbl = mklabel(self.side, "", bg=C["side"], fg=C["dim"])
        self.count_lbl.pack(anchor="w", padx=12, pady=4)
        self.hpane.add(self.side, minsize=180, width=300)
        # ---- Правая часть
        self.vpane = tk.PanedWindow(self.hpane, orient="vertical", bg=C["border"], sashwidth=3, bd=0, opaqueresize=True)
        self.hpane.add(self.vpane, minsize=400)
        ed = self.editor_wrap = tk.Frame(self.vpane, bg=C["bg"])
        self.tabbar = tk.Frame(ed, bg=C["side"], height=35)
        self.tabbar.pack(fill="x")
        self.tabbar.pack_propagate(False)
        self.stage = tk.Frame(ed, bg=C["bg"])
        self.stage.pack(fill="both", expand=True)
        self.vpane.add(ed, minsize=200)
        # ---- Нижняя панель
        pn = self.panel = tk.Frame(self.vpane, bg=C["bg"])
        ph = tk.Frame(pn, bg=C["bg"])
        ph.pack(fill="x")
        self.panel_tab = tk.StringVar(value="out")
        for txt, val in (("OUTPUT", "out"), ("PROBLEMS", "prob")):
            tk.Radiobutton(ph, text=txt, value=val, variable=self.panel_tab, indicatoron=False, bg=C["bg"], fg=C["dim"],
                           selectcolor=C["bg"], activebackground=C["bg"], activeforeground="#fff", bd=0, padx=12, pady=4,
                           font=("Segoe UI", 8, "bold"), command=self.switch_panel).pack(side="left")
        mkbtn(ph, "Clear", self.clear_output).pack(side="right", padx=6, pady=2)
        self.pbody = tk.Frame(pn, bg=C["bg"])
        self.pbody.pack(fill="both", expand=True)
        self.out = tk.Text(self.pbody, bg=C["bg"], fg=C["fg"], font=("Consolas", 9), bd=0, highlightthickness=0,
                           wrap="word", state="disabled", height=9, padx=8)
        self.osb = osb = ttk.Scrollbar(self.pbody, orient="vertical", command=self.out.yview)
        self.out.configure(yscrollcommand=osb.set)
        for tag, col in (("err", C["red"]), ("ok", C["green"]), ("dim", C["dim"]), ("warn", C["yellow"])):
            self.out.tag_configure(tag, foreground=col)
        self.prob = ttk.Treeview(self.pbody, columns=("file", "line", "msg"), show="headings", style="Dark.Treeview", height=8)
        for c, t, w in (("file", "Файл", 240), ("line", "Строка", 70), ("msg", "Сообщение", 700)):
            self.prob.heading(c, text=t)
            self.prob.column(c, width=w, anchor="w")
        self.prob.bind("<Double-1>", self.goto_problem)
        self.prob_data = {}
        self.out.pack(side="left", fill="both", expand=True)
        osb.pack(side="right", fill="y")
        self.vpane.add(pn, minsize=80, height=190)
        self.panel_visible = True

    def build_status(self):
        sb = tk.Frame(self.root, bg=C["accent"])
        sb.pack(fill="x", side="bottom")
        def lab(side, **kw):
            l = tk.Label(sb, bg=C["accent"], fg="#ffffff", font=("Segoe UI", 9), padx=10, pady=2, **kw)
            l.pack(side=side)
            return l
        self.st_msg = lab("left", text="Готово")
        self.st_tools = lab("left", text="")
        self.st_lang = lab("right", text="")
        self.st_enc = lab("right", text="UTF-8   LF")
        self.st_pos = lab("right", text="Ln 1, Col 1")

    def bind_keys(self):
        r = self.root
        def bind(seq, fn):
            def h(e=None):
                fn()
                return "break"
            r.bind_all(seq, h)
            r.bind_class("Text", seq, h)
            r.bind_class("Entry", seq, h)
        bind("<Control-O>", self.open_folder)
        bind("<Control-o>", self.open_file)
        bind("<Control-s>", self.cmd_save)
        bind("<Control-w>", self.close_current)
        bind("<F5>", self.cmd_build)
        bind("<F6>", self.cmd_decompile_current)
        bind("<F7>", self.cmd_validate)
        bind("<Control-D>", lambda: self.batch("decompile"))
        bind("<Control-B>", lambda: self.batch("compile"))
        bind("<Control-b>", self.toggle_side)
        bind("<Control-j>", self.toggle_panel)
        bind("<Control-g>", self.cmd_goto)
        bind("<Control-Tab>", lambda: self.cycle(1))
        bind("<Control-Shift-Tab>", lambda: self.cycle(-1))
        bind("<Control-plus>", lambda: self.zoom(1))
        bind("<Control-equal>", lambda: self.zoom(1))
        bind("<Control-minus>", lambda: self.zoom(-1))

    # -------------------------------------------------------------- утилиты UI
    def status(self, msg):
        self.st_msg.config(text=msg)

    def update_tools_status(self):
        miss = [n for n in Core.NAMES if not self.core.paths.get(n)]
        self.st_tools.config(text="⚠ нет: " + ", ".join(miss) if miss else "✔ инструменты найдены")

    def log(self, msg, tag=""):
        self.out.config(state="normal")
        self.out.insert("end", "[%s] %s\n" % (time.strftime("%H:%M:%S"), msg), tag)
        self.out.config(state="disabled")
        self.out.see("end")
        self.status(msg.splitlines()[0][:120] if msg else "")

    def clear_output(self):
        self.out.config(state="normal")
        self.out.delete("1.0", "end")
        self.out.config(state="disabled")
        self.prob.delete(*self.prob.get_children())

    def switch_panel(self):
        self.out.pack_forget()
        self.osb.pack_forget()
        self.prob.pack_forget()
        if self.panel_tab.get() == "out":
            self.osb.pack(side="right", fill="y")
            self.out.pack(side="left", fill="both", expand=True)
        else:
            self.prob.pack(side="left", fill="both", expand=True)

    def show_panel(self, which):
        if not self.panel_visible:
            self.toggle_panel()
        self.panel_tab.set(which)
        self.switch_panel()

    def toggle_panel(self):
        if self.panel_visible:
            self.vpane.forget(self.panel)
        else:
            self.vpane.add(self.panel, minsize=80, height=190)
        self.panel_visible = not self.panel_visible

    def toggle_side(self):
        if self.side.winfo_ismapped():
            self.hpane.forget(self.side)
        else:
            self.hpane.add(self.side, minsize=180, width=300, before=self.vpane)

    def zoom(self, d):
        s = max(7, min(28, self.cfg["font_size"] + d))
        self.cfg["font_size"] = s
        self.code_font.configure(size=s)
        save_cfg(self.cfg)
        for dc in self.docs:
            if hasattr(dc, "ed"):
                dc.ed._schedule()

    def update_cursor(self, ed):
        if self.cur is not None and getattr(self.cur, "ed", None) is ed:
            ln, col = ed.text.index("insert").split(".")
            self.st_pos.config(text="Ln %s, Col %d" % (ln, int(col) + 1))

    def cur_ed(self):
        return getattr(self.cur, "ed", None)

    def ed_event(self, ev):
        e = self.cur_ed()
        if e:
            e.text.event_generate(ev)

    def cmd_find(self):
        e = self.cur_ed()
        if e:
            e.show_find()

    def cmd_goto(self):
        e = self.cur_ed()
        if not e:
            return
        v = ask_string(self, "Go to Line", "Номер строки:", "")
        if v and v.strip().isdigit():
            e.goto_line(int(v))

    def about(self):
        messagebox.showinfo(APP_NAME, "%s %s\n\nРедактор шейдеров Sky: CotL (.spv/.ref)\nСтиль: Visual Studio Dark+\n\n"
                            "Ctrl+Click / F12 по %%id — переход к определению." % (APP_NAME, APP_VER))

    # -------------------------------------------------------------- Explorer
    def open_folder(self):
        p = filedialog.askdirectory(title="Папка с шейдерами (например ...\\Shaders\\Bin)",
                                    initialdir=self.cfg.get("last_dir") or None)
        if p:
            self.set_root(os.path.normpath(p))

    def set_root(self, path):
        self.root_dir = path
        self.cfg["last_dir"] = path
        save_cfg(self.cfg)
        self.root.title("%s — %s" % (os.path.basename(path), APP_NAME))
        self.refresh_explorer()

    def refresh_explorer(self):
        if not self.root_dir:
            return
        self.tree.delete(*self.tree.get_children())
        self.node_path = {}
        if self.search.get().strip():
            return self.run_search()
        rid = self.tree.insert("", "end", text=os.path.basename(self.root_dir).upper(), open=True, tags=("root",))
        self.node_path[rid] = self.root_dir
        self.fill_dir(rid)

    def file_tag(self, name):
        n = name.lower()
        return "spv" if n.endswith(".spv") else "ref" if n.endswith(".ref") else \
            "asm" if n.endswith(".spvasm") else "glsl" if re.search(r"\.(glsl|vert|frag|comp)$", n) else ""

    def fill_dir(self, node):
        path = self.node_path[node]
        self.tree.delete(*self.tree.get_children(node))
        try:
            ents = sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError:
            return
        nfiles = 0
        for e in ents:
            if e.name.startswith("."):
                continue
            if e.is_dir():
                iid = self.tree.insert(node, "end", text="  📁 " + e.name, tags=("dir",))
                self.node_path[iid] = e.path
                self.tree.insert(iid, "end", text="…")
            else:
                nfiles += 1
                iid = self.tree.insert(node, "end", text="  " + e.name, tags=(self.file_tag(e.name),))
                self.node_path[iid] = e.path
        if self.tree.parent(node) == "":
            self.count_lbl.config(text="%d файлов в корне" % nfiles)

    def on_tree_open(self, e):
        node = self.tree.focus()
        kids = self.tree.get_children(node)
        if kids and self.tree.item(kids[0], "text") == "…":
            self.fill_dir(node)

    def selected_path(self):
        sel = self.tree.selection()
        return self.node_path.get(sel[0]) if sel else None

    def on_tree_dbl(self, e):
        p = self.selected_path()
        if p and os.path.isfile(p):
            self.open_path(p)

    def schedule_search(self):
        if self._search_job:
            self.root.after_cancel(self._search_job)
        self._search_job = self.root.after(250, self.refresh_explorer)

    def run_search(self):
        q = self.search.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        self.node_path = {}
        n = 0
        for d, dirs, files in os.walk(self.root_dir):
            dirs[:] = [x for x in dirs if not x.startswith(".")]
            for fn in sorted(files):
                if q in fn.lower():
                    n += 1
                    if n <= 3000:
                        iid = self.tree.insert("", "end", text="  " + os.path.relpath(os.path.join(d, fn), self.root_dir),
                                               tags=(self.file_tag(fn),))
                        self.node_path[iid] = os.path.join(d, fn)
        self.count_lbl.config(text="найдено: %d%s" % (n, " (показано 3000)" if n > 3000 else ""))

    def tree_ctx(self, e):
        iid = self.tree.identify_row(e.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        p = self.node_path.get(iid)
        m = self.mkmenu(self.root)
        if p and os.path.isfile(p):
            m.add_command(label="Open", command=lambda: self.open_path(p))
            if p.lower().endswith(".spv"):
                m.add_command(label="Decompile to file…", command=lambda: self.decompile_path(p))
                r = ref_for_spv(p)
                if r and os.path.exists(r):
                    m.add_command(label="Open matching .ref", command=lambda: self.open_path(r))
            m.add_separator()
        m.add_command(label="Copy Path", command=lambda: (self.root.clipboard_clear(), self.root.clipboard_append(p or "")))
        if os.name == "nt":
            m.add_command(label="Reveal in Explorer", command=lambda: subprocess.Popen(
                ["explorer", "/select,", os.path.normpath(p)] if os.path.isfile(p) else ["explorer", os.path.normpath(p)]))
        m.tk_popup(e.x_root, e.y_root)

    # -------------------------------------------------------------- вкладки
    def show_welcome(self):
        w = self.welcome = tk.Frame(self.stage, bg=C["bg"])
        tk.Label(w, text="◆ Sky Shader Studio", bg=C["bg"], fg="#3e3e42", font=("Segoe UI", 28, "bold")).pack(pady=(110, 14))
        for k, t in (("Ctrl+Shift+O", "открыть папку Shaders\\Bin"), ("Ctrl+S / F5", "скомпилировать текущий шейдер в .spv"),
                     ("Ctrl+Shift+D", "массовая декомпиляция"), ("Ctrl+Shift+B", "массовая компиляция"),
                     ("Ctrl+Click / F12", "перейти к определению %id")):
            r = tk.Frame(w, bg=C["bg"])
            r.pack()
            tk.Label(r, text=k, width=18, anchor="e", bg=C["bg"], fg="#6e6e6e", font=("Segoe UI", 10)).pack(side="left")
            tk.Label(r, text="  " + t, width=40, anchor="w", bg=C["bg"], fg="#858585", font=("Segoe UI", 10)).pack(side="left")
        w.pack(fill="both", expand=True)

    def open_file(self):
        ps = filedialog.askopenfilenames(title="Открыть файл", filetypes=[
            ("Shader files", "*.spv *.ref *.spvasm *.glsl *.vert *.frag *.comp"), ("All files", "*.*")])
        for p in ps:
            self.open_path(p)

    def open_path(self, path):
        path = os.path.abspath(path)
        for d in self.docs:
            if os.path.abspath(d.path) == path:
                return self.select(d)
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext == ".spv":
                doc = SpvDoc(self, path, self.stage)
            elif ext == ".ref":
                doc = RefDoc(self, path, self.stage)
            else:
                doc = TextDoc(self, path, self.stage)
        except (CompileError, ToolError) as e:
            self.log("Не удалось открыть %s:\n%s" % (os.path.basename(path), e), "err")
            self.show_panel("out")
            messagebox.showerror(APP_NAME, str(e)[:800])
            return
        except Exception as e:
            self.log("Не удалось открыть %s: %s" % (path, e), "err")
            return
        self.add_doc(doc)

    def add_doc(self, doc):
        self.docs.append(doc)
        t = tk.Frame(self.tabbar, bg=C["tab_off"])
        strip = tk.Frame(t, bg=C["tab_off"], height=2)
        strip.pack(fill="x")
        row = tk.Frame(t, bg=C["tab_off"])
        row.pack(fill="both", expand=True)
        lbl = tk.Label(row, text=doc.title, bg=C["tab_off"], fg=C["dim"], font=("Segoe UI", 9), padx=10, pady=6)
        lbl.pack(side="left")
        x = tk.Label(row, text="✕", bg=C["tab_off"], fg=C["dim"], font=("Segoe UI", 8), padx=6, cursor="hand2")
        x.pack(side="left", padx=(0, 4))
        doc.tab = dict(frame=t, strip=strip, row=row, lbl=lbl, x=x)
        for w in (t, strip, row, lbl):
            w.bind("<Button-1>", lambda e, d=doc: self.select(d))
            w.bind("<Button-2>", lambda e, d=doc: self.close_doc(d))
        x.bind("<Button-1>", lambda e, d=doc: self.close_doc(d))
        t.pack(side="left", fill="y", padx=(0, 1))
        self.select(doc)

    def refresh_tab(self, doc):
        if not doc.tab:
            return
        doc.tab["lbl"].config(text=doc.title + (" ●" if doc.modified else ""))
        self.paint_tabs()

    def paint_tabs(self):
        for d in self.docs:
            act = d is self.cur
            bg = C["bg"] if act else C["tab_off"]
            tb = d.tab
            for k in ("frame", "row", "lbl", "x"):
                tb[k].config(bg=bg)
            tb["lbl"].config(fg="#ffffff" if act else C["dim"])
            tb["x"].config(fg="#c5c5c5" if act else C["dim"])
            tb["strip"].config(bg=C["accent"] if act else C["tab_off"])

    def select(self, doc):
        if self.cur is not None and self.cur.view is not None:
            self.cur.view.pack_forget()
        self.welcome.pack_forget()
        self.cur = doc
        doc.view.pack(fill="both", expand=True)
        self.paint_tabs()
        doc.focus()
        self.st_lang.config(text=doc.status_lang())
        self.st_pos.config(text="Ln 1, Col 1")
        if getattr(doc, "ed", None):
            self.update_cursor(doc.ed)
        self.root.title("%s — %s" % (doc.title, APP_NAME))

    def close_current(self):
        if self.cur:
            self.close_doc(self.cur)

    def close_doc(self, doc):
        if doc.modified:
            a = messagebox.askyesnocancel(APP_NAME, "Сохранить изменения в %s?" % doc.title)
            if a is None:
                return
            if a and not doc.save():
                return
        idx = self.docs.index(doc)
        self.docs.remove(doc)
        doc.tab["frame"].destroy()
        doc.view.destroy()
        if self.cur is doc:
            self.cur = None
            if self.docs:
                self.select(self.docs[min(idx, len(self.docs) - 1)])
            else:
                self.welcome.pack(fill="both", expand=True)
                self.st_lang.config(text="")
                self.st_pos.config(text="")

    def cycle(self, d):
        if self.docs and self.cur:
            self.select(self.docs[(self.docs.index(self.cur) + d) % len(self.docs)])

    def quit(self):
        dirty = [d for d in self.docs if d.modified]
        if dirty and not messagebox.askyesno(APP_NAME, "Есть несохранённые изменения (%d). Выйти без сохранения?" % len(dirty)):
            return
        save_cfg(self.cfg)
        self.root.destroy()

    # -------------------------------------------------------------- команды
    def cmd_save(self):
        if self.cur:
            self.cur.save()

    def save_all(self):
        for d in self.docs:
            if d.modified:
                d.save()

    def cmd_build(self):
        if self.cur:
            self.cur.build()

    def compile_to(self, doc, text, mode, out):
        """Скомпилировать текст и записать в out (с .bak). Возвращает True/False."""
        self.status("Компиляция %s …" % os.path.basename(out))
        self.root.update_idletasks()
        self.prob.delete(*self.prob.get_children())
        orig = None
        if os.path.exists(out):
            with open(out, "rb") as f:
                orig = f.read()
        try:
            t0 = time.time()
            data = self.core.compile_text(text, mode, os.path.basename(doc.path), orig)
            warn = ""
            if self.core.paths.get("spirv-val"):
                try:
                    self.core.validate_bytes(data)
                except CompileError as e:
                    warn = str(e).strip()
            backup_once(out)
            with open(out, "wb") as f:
                f.write(data)
        except ToolError as e:
            self.log(str(e), "err")
            self.show_panel("out")
            return False
        except CompileError as e:
            msg = str(e).strip()
            self.log("Ошибка компиляции %s:\n%s" % (os.path.basename(doc.path), msg), "err")
            self.fill_problems(doc, msg)
            return False
        self.log("✔ Скомпилировано: %s (%d байт, %.2f c)%s" % (
            out, len(data), time.time() - t0, "  (оригинал бэкапнут в .bak)" if os.path.exists(out + ".bak") else ""), "ok")
        if warn:
            self.log("Предупреждение валидатора:\n" + warn, "warn")
        return True

    def fill_problems(self, doc, msg):
        self.prob_data = {}
        items = parse_problems(msg)
        for i, (ln, tx) in enumerate(items):
            iid = self.prob.insert("", "end", values=(doc.title, ln, tx))
            self.prob_data[iid] = (doc, ln)
        self.show_panel("prob" if items else "out")

    def goto_problem(self, e):
        iid = self.prob.identify_row(e.y)
        if iid in self.prob_data:
            doc, ln = self.prob_data[iid]
            if doc in self.docs:
                self.select(doc)
                doc.ed.goto_line(ln, flash=True)

    def decompile_path(self, p):
        mode = self.cfg["mode"]
        try:
            text = self.core.decompile_file(p, mode, self.cfg["friendly"])
        except (CompileError, ToolError) as e:
            self.log(str(e), "err")
            return
        out = filedialog.asksaveasfilename(title="Сохранить декомпиляцию", initialdir=os.path.dirname(p),
                                           initialfile=os.path.splitext(os.path.basename(p))[0] + MODE_EXT[mode],
                                           defaultextension=MODE_EXT[mode])
        if out:
            with open(out, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            self.log("✔ Декомпилировано: %s" % out, "ok")
            self.open_path(out)

    def cmd_decompile_current(self):
        p = None
        if isinstance(self.cur, SpvDoc):
            p = self.cur.path
        else:
            sp = self.selected_path()
            if sp and sp.lower().endswith(".spv"):
                p = sp
        if p:
            self.decompile_path(p)
        else:
            self.log("Выберите .spv в Explorer или откройте его во вкладке.", "warn")

    def cmd_validate(self):
        d = self.cur
        if isinstance(d, (SpvDoc, TextDoc)) and d.language in ("asm", "glsl"):
            mode = d.language
            orig = d.orig if isinstance(d, SpvDoc) else None
            try:
                data = self.core.compile_text(d.ed.get_text(), mode, os.path.basename(d.path), orig)
                self.core.validate_bytes(data)
                self.log("✔ %s: компиляция и spirv-val прошли успешно (%d байт)" % (d.title, len(data)), "ok")
            except (CompileError, ToolError) as e:
                self.log("%s:\n%s" % (d.title, str(e).strip()), "err")
                self.fill_problems(d, str(e))
        else:
            self.log("Нечего валидировать: откройте .spv / .spvasm / .glsl", "warn")

    def cmd_reload(self):
        if isinstance(self.cur, SpvDoc):
            if self.cur.modified and not messagebox.askyesno(APP_NAME, "Отбросить несохранённые правки?"):
                return
            try:
                self.cur.reload()
                self.st_lang.config(text=self.cur.status_lang())
            except (CompileError, ToolError) as e:
                self.log(str(e), "err")

    def mode_changed(self, e=None):
        self.cfg["mode"] = "asm" if self.mode_var.get() == MODES["asm"] else "glsl"
        save_cfg(self.cfg)
        self.log("Формат декомпиляции: %s (применяется к новым вкладкам; View/Build → Reload — для текущей)" %
                 self.mode_var.get(), "dim")

    def friendly_changed(self):
        self.cfg["friendly"] = bool(self.friendly.get())
        save_cfg(self.cfg)

    def batch(self, kind):
        BatchDialog(self, kind)


def main():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    app = App(root)
    for a in sys.argv[1:]:
        if os.path.isdir(a):
            app.set_root(os.path.abspath(a))
        elif os.path.isfile(a):
            app.open_path(a)
    root.mainloop()


if __name__ == "__main__":
    main()
