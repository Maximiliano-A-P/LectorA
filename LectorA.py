import os
import sys
import ctypes
import struct
import queue
import threading
import subprocess
import tempfile
import zipfile
import re
import tkinter as tk
from tkinter import ttk, messagebox

from send2trash import send2trash

FONT = ("Segoe UI", -24)          # -24 = 24 píxeles
FONT_BOLD = ("Segoe UI", -24, "bold")
NO_EXT = "(sin extensión)"
NO_WINDOW = 0x08000000

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GetConsoleWindow.restype = ctypes.c_void_p
user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.OpenClipboard.argtypes = [ctypes.c_void_p]
user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p
user32.GetParent.argtypes = [ctypes.c_void_p]
user32.GetParent.restype = ctypes.c_void_p


def hide_console():
    try:
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


# ---------------------------------------------------------------- portapapeles
def _alloc(data: bytes):
    h = kernel32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
    p = kernel32.GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    kernel32.GlobalUnlock(h)
    return h


def set_clipboard_files(hwnd, paths, cut):
    header = struct.pack("<IiiII", 20, 0, 0, 0, 1)   # DROPFILES, wide chars
    body = ("\0".join(paths) + "\0\0").encode("utf-16-le")
    if not user32.OpenClipboard(hwnd):
        raise RuntimeError("No se pudo abrir el portapapeles")
    try:
        user32.EmptyClipboard()
        user32.SetClipboardData(15, _alloc(header + body))  # CF_HDROP
        fmt = user32.RegisterClipboardFormatW("Preferred DropEffect")
        user32.SetClipboardData(fmt, _alloc(struct.pack("<I", 2 if cut else 1)))
    finally:
        user32.CloseClipboard()


def set_clipboard_text(root, text):
    root.clipboard_clear()
    root.clipboard_append(text)
    root.update()


# ---------------------------------------------------------------------- datos
SORT_OPTIONS = [
    "Nombre ↑",
    "Nombre ↓",
    "Creación ↑",
    "Creación ↓",
    "Modificación ↑",
    "Modificación ↓",
]


def natural_key(text):
    """Orden natural: archivo2 antes que archivo10."""
    return [
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", text)
    ]


def sort_files(paths, mode):
    """Ordena de forma determinista; el nombre resuelve empates de fechas."""
    reverse = mode.endswith("↓")

    if mode.startswith("Nombre"):
        key = lambda p: natural_key(os.path.basename(p))
    elif mode.startswith("Creación"):
        key = lambda p: (os.path.getctime(p), natural_key(os.path.basename(p)))
    else:
        key = lambda p: (os.path.getmtime(p), natural_key(os.path.basename(p)))

    return sorted(paths, key=key, reverse=reverse)


def scan_folder(folder, sort_mode="Nombre ↑"):
    by_ext = {}

    with os.scandir(folder) as it:
        for e in it:
            try:
                if not e.is_file():
                    continue
                path = e.path
                ext = os.path.splitext(e.name)[1].lower() or NO_EXT
                by_ext.setdefault(ext, []).append(path)
            except OSError:
                continue

    for ext in by_ext:
        by_ext[ext] = sort_files(by_ext[ext], sort_mode)

    all_paths = sort_files(
        [p for paths in by_ext.values() for p in paths],
        sort_mode,
    )

    return len(all_paths), by_ext, all_paths



def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024


# -------------------------------------------------------------------- ventanas
def open_form(parent, title, message, fields, on_ok, ok_text="Aceptar"):
    """Ventana NO modal: no hace grab, se puede mover y usar el resto."""
    win = tk.Toplevel(parent)
    win.title(title)
    win.configure(padx=20, pady=20)
    tk.Label(win, text=message, font=FONT, justify="left", wraplength=700).pack(anchor="w")
    entries = []
    for label, default in fields:
        tk.Label(win, text=label, font=FONT).pack(anchor="w", pady=(12, 0))
        var = tk.StringVar(value=default)
        ent = tk.Entry(win, textvariable=var, font=FONT, width=34)
        ent.pack(fill="x")
        entries.append(var)
    if entries:
        first = win.winfo_children()[-1]
        first.focus_set()
        first.select_range(0, "end")

    def accept(_e=None):
        values = [v.get() for v in entries]
        win.destroy()
        on_ok(values)

    row = tk.Frame(win)
    row.pack(fill="x", pady=(18, 0))
    tk.Button(row, text=ok_text, font=FONT, command=accept).pack(side="right", padx=(10, 0))
    tk.Button(row, text="Cancelar", font=FONT, command=win.destroy).pack(side="right")
    win.bind("<Return>", accept)
    win.bind("<Escape>", lambda e: win.destroy())
    return win


def open_info(parent, title, text):
    win = tk.Toplevel(parent)
    win.title(title)
    win.configure(padx=20, pady=20)
    tk.Label(win, text=text, font=FONT, justify="left", wraplength=800).pack(anchor="w")
    tk.Button(win, text="Cerrar", font=FONT, command=win.destroy).pack(anchor="e", pady=(18, 0))
    win.bind("<Escape>", lambda e: win.destroy())


# ------------------------------------------------------- panel con scroll
class ScrollFrame(tk.Frame):
    """Panel con su propio canvas. El scroll vertical (barra y rueda) solo
    existe cuando el contenido es más alto que el área visible."""

    def __init__(self, parent):
        super().__init__(parent)
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, highlightthickness=0)
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.inner = tk.Frame(self.canvas)
        self.inner.columnconfigure(0, weight=1)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.scrollable = False
        self.inner.bind("<Configure>", lambda e: self._update())
        self.canvas.bind("<Configure>", self._on_canvas)

    def _on_canvas(self, e):
        self.canvas.itemconfigure(self._win, width=e.width)
        self._update()

    def _update(self):
        content_h = self.inner.winfo_reqheight()
        view_h = self.canvas.winfo_height()
        if content_h > view_h > 1:
            self.canvas.configure(scrollregion=(0, 0, 1, content_h))
            if not self.scrollable:
                self.scrollable = True
                self.sb.grid(row=0, column=1, sticky="ns")
        else:
            self.canvas.configure(scrollregion=(0, 0, 1, max(view_h, 1)))
            self.canvas.yview_moveto(0)
            if self.scrollable:
                self.scrollable = False
                self.sb.grid_remove()

    def scroll(self, units):
        if self.scrollable:
            self.canvas.yview_scroll(units, "units")


# ------------------------------------------------------------------------ app
class LectorA(tk.Tk):
    def __init__(self, folder):
        super().__init__()
        self.folder = folder
        self.title(f"LectorA - {folder}")
        self.geometry("1150x820")
        self.minsize(900, 600)

        self.ready = False
        self.busy = False
        self.selected = []
        self.selected_key = None
        self.rows = {}
        self.action_buttons = []
        self.results = queue.Queue()
        self.sort_mode = tk.StringVar(value="Nombre ↑")
        self.undo_history = []

        self._build_ui()
        self._bind_keys()
        self.after(100, self._poll_results)
        self.refresh()

    # ---------------------------------------------------------------- interfaz
    def _build_ui(self):
        self.columnconfigure(0, weight=3)
        self.columnconfigure(1, weight=2)
        self.rowconfigure(0, weight=1)

        left = tk.Frame(self, padx=10, pady=10)
        left.grid(row=0, column=0, sticky="nsew")
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)

        self.reload_button = tk.Button(left, text="Recargar", font=FONT, padx=12,
                                       command=self.refresh)
        self.reload_button.grid(row=0, column=0, pady=(0, 10))  # centrado

        self.left_scroll = ScrollFrame(left)
        self.left_scroll.grid(row=1, column=0, sticky="nsew")
        self.list_frame = self.left_scroll.inner

        right_box = tk.Frame(self, padx=10, pady=10)
        right_box.grid(row=0, column=1, sticky="nsew")
        right_box.rowconfigure(0, weight=1)
        right_box.columnconfigure(0, weight=1)

        self.right_scroll = ScrollFrame(right_box)
        self.right_scroll.grid(row=0, column=0, sticky="nsew")
        right = self.right_scroll.inner

        self.status = tk.Label(right, text="Escaneando...", font=FONT_BOLD,
                               anchor="w", justify="left", wraplength=420)
        self.status.grid(row=0, column=0, sticky="ew", pady=(0, 10))

        tk.Label(
            right,
            text="Ordenar archivos por:",
            font=FONT,
        ).grid(row=1, column=0, sticky="w", pady=(0, 4))

        self.sort_combo = ttk.Combobox(
            right,
            textvariable=self.sort_mode,
            values=SORT_OPTIONS,
            state="readonly",
            font=("Segoe UI", 14),
        )
        self.sort_combo.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        self.sort_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh())

        actions = [
            ("Abrir", self.act_open),
            ("Añadir al archivo...", self.act_archive),
            ("Copiar", self.act_copy),
            ("Copiar como ruta", self.act_copy_path),
            ("Cortar", self.act_cut),
            ("Crear acceso directo", self.act_shortcut),
            ("Eliminar", self.act_delete),
            ("Propiedades", self.act_properties),
            ("Renombrar", self.act_rename),
            ("Deshacer última acción", self.act_undo),
        ]
        actions.sort(key=lambda a: a[0].casefold())
        for i, (text, fn) in enumerate(actions, start=3):
            b = tk.Button(right, text=text, font=FONT, anchor="w", padx=12,
                          state="disabled", command=fn)
            b.grid(row=i, column=0, sticky="ew", pady=3)
            self.action_buttons.append(b)

    def _bind_keys(self):
        for k in ("c", "C"):
            self.bind(f"<Control-{k}>", lambda e: self.act_copy())
        for k in ("x", "X"):
            self.bind(f"<Control-{k}>", lambda e: self.act_cut())
        self.bind("<Delete>", lambda e: self.act_delete())
        self.bind("<F2>", lambda e: self.act_rename())
        self.bind("<Control-z>", lambda e: self.act_undo())
        self.bind("<Control-Z>", lambda e: self.act_undo())
        self.bind("<MouseWheel>", self._on_wheel)

    def _on_wheel(self, e):
        try:
            w = self.winfo_containing(e.x_root, e.y_root)
        except (KeyError, tk.TclError):
            return
        while w is not None:
            if isinstance(w, ScrollFrame):
                w.scroll(int(-e.delta / 120))
                return
            w = w.master

    # ------------------------------------------------------------------ estado
    def _set_buttons(self):
        enabled = self.ready and not self.busy and bool(self.selected)
        self.reload_button.configure(state="disabled" if self.busy else "normal")

        for b in self.action_buttons:
            if b.cget("text") == "Deshacer última acción":
                can_undo = (
                    self.ready
                    and not self.busy
                    and bool(self.undo_history)
                )
                b.configure(state="normal" if can_undo else "disabled")
            else:
                b.configure(state="normal" if enabled else "disabled")

        for r in self.rows.values():
            r["button"].configure(state="normal" if self.ready and not self.busy else "disabled")

    def _poll_results(self):
        try:
            while True:
                fn = self.results.get_nowait()
                fn()
        except queue.Empty:
            pass
        self.after(100, self._poll_results)

    def run_task(self, work, done):
        """Ejecuta work() en un hilo; done(resultado, error) corre en el hilo de la UI."""
        if self.busy:
            return
        self.busy = True
        self._set_buttons()

        def runner():
            try:
                res, err = work(), None
            except Exception as ex:
                res, err = None, ex

            def finish():
                self.busy = False
                done(res, err)
                self._set_buttons()
            self.results.put(finish)

        threading.Thread(target=runner, daemon=True).start()

    # ------------------------------------------------------------------ escaneo
    def refresh(self):
        self.ready = False
        self.selected = []
        self.selected_key = None
        self.status.configure(text="Escaneando...")
        self._set_buttons()

        def work():
            return scan_folder(self.folder, self.sort_mode.get())

        def done(res, err):
            if err:
                self.status.configure(text=f"Error: {err}")
                return
            total, by_ext, all_paths = res
            self.data = (total, by_ext)
            self.all_paths = all_paths
            self._render(total, by_ext)
            self.ready = True
            self.status.configure(text="Nada seleccionado")

        self.run_task(work, done)

    def _render(self, total, by_ext):
        for w in self.list_frame.winfo_children():
            w.destroy()
        self.rows = {}
        items = [("__total__", f"Total: {total}")]
        for ext in sorted(by_ext, key=str.casefold):
            items.append((ext, f"{ext}: {len(by_ext[ext])}"))
        for i, (key, text) in enumerate(items):
            lbl = tk.Label(self.list_frame, text=text, font=FONT, anchor="w", padx=8, pady=4)
            lbl.grid(row=i, column=0, sticky="ew")
            btn = tk.Button(self.list_frame, text="Seleccionar", font=FONT,
                            command=lambda k=key: self.select(k))
            btn.grid(row=i, column=1, sticky="e", padx=(10, 0), pady=2)
            self.rows[key] = {"label": lbl, "button": btn}

    def select(self, key):
        total, by_ext = self.data
        if key == "__total__":
            self.selected = list(self.all_paths)
            name = "toda la carpeta"
        else:
            self.selected = list(by_ext[key])
            name = key
        self.selected_key = key
        for k, r in self.rows.items():
            r["label"].configure(bg="#cde4ff" if k == key else self.cget("bg"))
        self.status.configure(text=f"Seleccionados: {len(self.selected)} ({name})")
        self._set_buttons()

    def _need_selection(self):
        return self.ready and not self.busy and bool(self.selected)

    # ------------------------------------------------------------------ acciones
    def act_open(self):
        if not self._need_selection():
            return
        for p in self.selected:
            try:
                os.startfile(p)
            except OSError:
                pass

    def act_copy(self):
        self._clip(cut=False)

    def act_cut(self):
        self._clip(cut=True)

    def _clip(self, cut):
        if not self._need_selection():
            return
        hwnd = user32.GetParent(self.winfo_id()) or self.winfo_id()
        try:
            set_clipboard_files(hwnd, [os.path.normpath(p) for p in self.selected], cut)
            verbo = "Cortados" if cut else "Copiados"
            self.status.configure(text=f"{verbo}: {len(self.selected)} archivos")
        except Exception as ex:
            open_info(self, "Error", str(ex))

    def act_copy_path(self):
        if not self._need_selection():
            return
        text = "\n".join('"' + os.path.normpath(p) + '"' for p in self.selected)
        set_clipboard_text(self, text)
        self.status.configure(text="Rutas copiadas al portapapeles")

    def act_delete(self):
        if not self._need_selection():
            return
        paths = [os.path.normpath(p) for p in self.selected]

        def confirmed(_values):
            def work():
                send2trash(paths)
            def done(_r, err):
                if err:
                    open_info(self, "Error", str(err))
                self.refresh()
            self.run_task(work, done)

        open_form(self, "Eliminar",
                  f"¿Enviar {len(paths)} archivos a la papelera?", [], confirmed, "Eliminar")

    def act_rename(self):
        if not self._need_selection():
            return
        # Volver a ordenar justo antes de renombrar: el número asignado a cada
        # archivo no depende del orden interno con el que llegó la selección.
        paths = sort_files(list(self.selected), self.sort_mode.get())
        single = len(paths) == 1
        default = os.path.basename(paths[0]) if single else "archivo"
        msg = ("Nuevo nombre (con extensión):" if single else
               "Nombre base. Se numerarán: nombre (1), nombre (2)... conservando cada extensión:")

        def accepted(values):
            base = values[0].strip()
            if not base:
                return

            def work():
                errors = []
                renamed = []

                for i, p in enumerate(paths, start=1):
                    folder, name = os.path.split(p)
                    ext = os.path.splitext(name)[1]
                    new = base if single else f"{base} ({i}){ext}"
                    dest = os.path.join(folder, new)
                    if os.path.exists(dest):
                        errors.append(f"Ya existe: {new}")
                        continue
                    try:
                        os.rename(p, dest)
                        renamed.append((dest, p))
                    except OSError as ex:
                        errors.append(f"{name}: {ex}")

                if renamed:
                    self.undo_history.append({
                        "type": "rename",
                        "items": renamed,
                    })

                return errors

            def done(errors, err):
                msgs = [str(err)] if err else (errors or [])
                if msgs:
                    open_info(self, "Renombrar", "\n".join(msgs[:10]))
                self.refresh()

            self.run_task(work, done)

        open_form(self, "Renombrar", msg, [("Nombre:", default)], accepted)

    def act_undo(self):
        if self.busy or not self.undo_history:
            return

        action = self.undo_history[-1]

        if action["type"] == "rename":
            items = action["items"]

            # Comprobar todo antes de modificar nada.
            for current, original in items:
                if not os.path.exists(current):
                    open_info(
                        self,
                        "No se puede deshacer",
                        f"No se encuentra el archivo:\n{current}",
                    )
                    return

                if os.path.exists(original):
                    open_info(
                        self,
                        "No se puede deshacer",
                        f"El nombre original ya está ocupado:\n{original}",
                    )
                    return

            def work():
                restored = []

                try:
                    for current, original in items:
                        os.rename(current, original)
                        restored.append((original, current))
                except Exception:
                    # Si algo falla, intentar revertir el deshacer parcial.
                    for original, current in reversed(restored):
                        try:
                            os.rename(original, current)
                        except OSError:
                            pass
                    raise

            def done(_result, err):
                if err:
                    open_info(self, "Error al deshacer", str(err))
                else:
                    self.undo_history.pop()
                    self.status.configure(text="Renombramiento deshecho")
                self.refresh()

            self.run_task(work, done)

    def act_shortcut(self):
        if not self._need_selection():
            return
        paths = list(self.selected)

        def work():
            script = ("$s=(New-Object -COM WScript.Shell).CreateShortcut($env:LA_LNK);"
                      "$s.TargetPath=$env:LA_TGT;$s.Save()")
            for p in paths:
                folder, name = os.path.split(p)
                lnk = os.path.join(folder, f"{name} - acceso directo.lnk")
                env = dict(os.environ, LA_LNK=lnk, LA_TGT=os.path.normpath(p))
                subprocess.run(["powershell", "-NoProfile", "-Command", script],
                               env=env, creationflags=NO_WINDOW, check=True)

        def done(_r, err):
            if err:
                open_info(self, "Error", str(err))
            self.refresh()

        self.run_task(work, done)

    def act_properties(self):
        if not self._need_selection():
            return
        paths = list(self.selected)
        total = 0
        for p in paths:
            try:
                total += os.path.getsize(p)
            except OSError:
                pass
        exts = sorted({os.path.splitext(p)[1].lower() or NO_EXT for p in paths})
        open_info(self, "Propiedades",
                  f"Carpeta: {self.folder}\nArchivos: {len(paths)}\n"
                  f"Tamaño total: {fmt_size(total)}\nTipos: {', '.join(exts)}")

    def act_archive(self):
        if not self._need_selection():
            return

        paths = [os.path.abspath(os.path.normpath(p)) for p in self.selected]
        default = os.path.basename(os.path.normpath(self.folder)) or "archivo"

        def accepted(values):
            name = values[0].strip().strip('"')
            if not name:
                return

            invalid_chars = '<>:/\\|?*'
            if name in (".", "..") or any(c in name for c in invalid_chars):
                open_info(
                    self,
                    "Nombre no válido",
                    "Ingresá solamente un nombre de archivo, sin rutas ni caracteres reservados de Windows."
                )
                return

            # CBZ es un ZIP estándar con otra extensión. Se permiten ambos.
            if not name.casefold().endswith((".zip", ".cbz")):
                name += ".zip"

            stem, extension = os.path.splitext(name)
            if not stem:
                open_info(self, "Nombre no válido", "Ingresá un nombre para el archivo.")
                return

            destination = os.path.abspath(os.path.join(self.folder, name))
            destination_key = os.path.normcase(destination)

            # No incluir el archivo de salida entre sus propios contenidos.
            files_to_add = [
                p for p in paths
                if os.path.normcase(os.path.abspath(p)) != destination_key
            ]

            if not files_to_add:
                open_info(
                    self,
                    "Añadir al archivo...",
                    "No hay archivos para añadir después de excluir el archivo de salida."
                )
                return

            for path in files_to_add:
                if not os.path.isfile(path):
                    open_info(
                        self,
                        "Archivo no encontrado",
                        f"El archivo seleccionado ya no existe:\n{path}"
                    )
                    return

            # Si ya existe el destino, nunca lo actualizamos: podría ser un ZIP
            # protegido creado por la versión anterior. Pedimos permiso para
            # reemplazarlo y luego creamos un archivo completamente nuevo.
            replace_existing = os.path.exists(destination)
            if replace_existing:
                replace = messagebox.askyesno(
                    "Reemplazar archivo",
                    f"Ya existe este archivo:\n\n{name}\n\n"
                    "¿Querés reemplazarlo por un archivo nuevo sin contraseña?",
                    parent=self,
                )
                if not replace:
                    return

            def work():
                # Crear primero un ZIP temporal. Solo sustituye el destino cuando
                # el archivo nuevo se terminó de crear correctamente.
                fd, temp_path = tempfile.mkstemp(
                    prefix=".lectora_",
                    suffix=".tmp",
                    dir=self.folder,
                )
                os.close(fd)

                try:
                    # ZIP_STORED = no comprimir. zipfile no añade contraseña;
                    # los nombres se guardan en la raíz para facilitar CBZ.
                    with zipfile.ZipFile(
                        temp_path,
                        mode="w",
                        compression=zipfile.ZIP_STORED,
                        allowZip64=True,
                    ) as archive:
                        for path in files_to_add:
                            archive.write(
                                path,
                                arcname=os.path.basename(path),
                                compress_type=zipfile.ZIP_STORED,
                            )

                    # Comprobar que el archivo recién creado es un ZIP válido.
                    with zipfile.ZipFile(temp_path, mode="r") as archive:
                        bad_file = archive.testzip()
                        if bad_file is not None:
                            raise RuntimeError(
                                f"El ZIP no pasó la comprobación interna: {bad_file}"
                            )

                    # No sobrescribir un archivo que haya aparecido mientras se
                    # preparaba el proceso si el usuario no confirmó reemplazo.
                    if os.path.exists(destination) and not replace_existing:
                        raise FileExistsError(
                            f"Ya existe un archivo con ese nombre: {destination}"
                        )

                    os.replace(temp_path, destination)
                    return len(files_to_add)

                finally:
                    if os.path.exists(temp_path):
                        try:
                            os.remove(temp_path)
                        except OSError:
                            pass

            def done(file_count, err):
                if err:
                    open_info(self, "Error al crear el archivo", str(err))
                else:
                    output_type = "CBZ" if extension.casefold() == ".cbz" else "ZIP"
                    self.status.configure(
                        text=f"Archivo {output_type} creado: {name} ({file_count} archivos)"
                    )
                self.refresh()

            self.run_task(work, done)

        open_form(
            self,
            "Añadir al archivo...",
            "Se creará un archivo ZIP/CBZ sin compresión y sin contraseña.\n"
            "Para un cómic, ingresá un nombre terminado en .cbz.\n"
            "Los archivos quedarán en la raíz del archivo para facilitar la lectura.",
            [("Nombre:", default)],
            accepted,
            "Crear archivo",
        )


def main():
    hide_console()
    folder = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    folder = os.path.abspath(folder)
    LectorA(folder).mainloop()


if __name__ == "__main__":
    main()