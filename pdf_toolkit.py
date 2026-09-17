#!/usr/bin/env python3
"""
PDF Toolkit - an offline desktop app for merging, rearranging,
splitting, compressing and securing PDF files.

Everything runs locally. No file ever leaves the machine.

Run:    python pdf_toolkit.py
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# --------------------------------------------------------------------------
# Optional / required back-ends
# --------------------------------------------------------------------------
try:
    from pypdf import PdfReader, PdfWriter
except ImportError:  # pragma: no cover
    sys.exit("pypdf is required.  Install it with:  pip install pypdf")

try:
    import pikepdf
    HAVE_PIKEPDF = True
except ImportError:
    HAVE_PIKEPDF = False

try:
    import pymupdf                      # PyMuPDF 1.24+
    HAVE_FITZ = True
except ImportError:
    try:
        import fitz as pymupdf          # older PyMuPDF releases
        HAVE_FITZ = True
    except ImportError:
        HAVE_FITZ = False


APP_NAME = "PDF Toolkit"
PDF_TYPES = [("PDF files", "*.pdf"), ("All files", "*.*")]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def human_size(num_bytes: int) -> str:
    """Format a byte count the way a person reads it."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def parse_page_ranges(spec: str, page_count: int) -> list[int]:
    """
    Turn '1-3, 7, 10-' into zero-based page indices.

    An empty spec means every page. Raises ValueError on anything
    that doesn't make sense so the caller can show a clear message.
    """
    spec = (spec or "").strip()
    if not spec:
        return list(range(page_count))

    indices: list[int] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            start_txt, _, end_txt = chunk.partition("-")
            start = int(start_txt) if start_txt.strip() else 1
            end = int(end_txt) if end_txt.strip() else page_count
        else:
            start = end = int(chunk)

        if start < 1 or end > page_count or start > end:
            raise ValueError(
                f"Page range '{chunk}' is outside this document "
                f"(it has {page_count} pages)."
            )
        indices.extend(range(start - 1, end))

    if not indices:
        raise ValueError("No pages selected.")
    return indices


def open_reader(path: str, password: str | None = None) -> PdfReader:
    """Open a PDF, decrypting it first if it is password protected."""
    reader = PdfReader(path)
    if reader.is_encrypted:
        if not password:
            raise ValueError(
                f"{Path(path).name} is password protected. "
                "Remove the password on the Security tab first."
            )
        if reader.decrypt(password) == 0:
            raise ValueError(f"Wrong password for {Path(path).name}.")
    return reader


def unique_path(path: Path) -> Path:
    """Never silently overwrite: document.pdf -> document (2).pdf"""
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    n = 2
    while True:
        candidate = parent / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
        n += 1


# --------------------------------------------------------------------------
# Core operations - deliberately kept free of any Tkinter code so they
# can be unit tested, scripted, or reused in a CLI later.
# --------------------------------------------------------------------------
@dataclass
class MergeItem:
    path: str
    ranges: str = ""          # "" means all pages


def merge_pdfs(items: list[MergeItem], out_path: str, log=print) -> None:
    writer = PdfWriter()
    for item in items:
        reader = open_reader(item.path)
        wanted = parse_page_ranges(item.ranges, len(reader.pages))
        for i in wanted:
            writer.add_page(reader.pages[i])
        log(f"Added {len(wanted)} page(s) from {Path(item.path).name}")
    with open(out_path, "wb") as fh:
        writer.write(fh)


def save_arrangement(src: str, order: list[tuple[int, int]], out_path: str) -> None:
    """order is a list of (original_page_index, rotation_in_degrees)."""
    reader = open_reader(src)
    writer = PdfWriter()
    for index, rotation in order:
        writer.add_page(reader.pages[index])
        if rotation:
            writer.pages[-1].rotate(rotation)
    with open(out_path, "wb") as fh:
        writer.write(fh)


def split_pdf(src: str, out_dir: str, mode: str, value: str, log=print) -> int:
    """
    mode: 'every'   -> a new file every <value> pages
          'ranges'  -> one file containing <value> (e.g. '2-5, 9')
          'burst'   -> one file per page
    Returns the number of files written.
    """
    reader = open_reader(src)
    total = len(reader.pages)
    stem = Path(src).stem
    written = 0

    def write(indices: list[int], label: str) -> None:
        nonlocal written
        writer = PdfWriter()
        for i in indices:
            writer.add_page(reader.pages[i])
        target = unique_path(Path(out_dir) / f"{stem}_{label}.pdf")
        with open(target, "wb") as fh:
            writer.write(fh)
        log(f"Wrote {target.name}")
        written += 1

    if mode == "burst":
        for i in range(total):
            write([i], f"page{i + 1:03d}")
    elif mode == "every":
        step = int(value)
        if step < 1:
            raise ValueError("Split size must be at least 1 page.")
        for start in range(0, total, step):
            group = list(range(start, min(start + step, total)))
            write(group, f"{group[0] + 1}-{group[-1] + 1}")
    else:  # ranges
        write(parse_page_ranges(value, total), "extract")

    return written


def compress_pdf(src: str, out_path: str, level: str, log=print) -> None:
    """
    level: 'lossless' - rebuild and recompress streams, pages untouched
           'balanced' - also downsample images above 150 DPI
           'strong'   - also downsample images above 96 DPI, lower quality
    """
    if level == "lossless" or not HAVE_FITZ:
        if level != "lossless" and not HAVE_FITZ:
            log("PyMuPDF not installed - falling back to lossless compression.")
        _compress_lossless(src, out_path, log)
        return

    # Only images above the threshold get touched; those are resampled down
    # to the target. Anything already small enough is left exactly as it is.
    threshold, target, quality = (
        (200, 150, 70) if level == "balanced" else (130, 96, 55)
    )
    doc = pymupdf.open(src)
    try:
        if hasattr(doc, "rewrite_images"):
            doc.rewrite_images(
                dpi_threshold=threshold, dpi_target=target, quality=quality
            )
        else:
            log("This PyMuPDF version can't downsample images; "
                "compressing structure only.")
        doc.save(out_path, garbage=4, deflate=True, clean=True)
    finally:
        doc.close()


def _compress_lossless(src: str, out_path: str, log=print) -> None:
    if HAVE_PIKEPDF:
        with pikepdf.open(src) as pdf:
            pdf.save(
                out_path,
                compress_streams=True,
                object_stream_mode=pikepdf.ObjectStreamMode.generate,
                recompress_flate=True,
            )
        return

    log("pikepdf not installed - using pypdf's lighter compression.")
    reader = open_reader(src)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    for page in writer.pages:
        page.compress_content_streams()
    writer.compress_identical_objects()
    with open(out_path, "wb") as fh:
        writer.write(fh)


def encrypt_pdf(src: str, out_path: str, user_pw: str, owner_pw: str = "") -> None:
    reader = open_reader(src)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(
        user_password=user_pw,
        owner_password=owner_pw or None,
        algorithm="AES-256",
    )
    with open(out_path, "wb") as fh:
        writer.write(fh)


def decrypt_pdf(src: str, out_path: str, password: str) -> None:
    reader = open_reader(src, password)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    with open(out_path, "wb") as fh:
        writer.write(fh)


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
class PdfToolkitApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("880x620")
        self.minsize(760, 540)

        self._messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self._busy = False

        self._build_style()
        self._build_layout()
        self.after(100, self._drain_messages)

    # ---- chrome ----------------------------------------------------------
    def _build_style(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Hint.TLabel", foreground="#666")
        style.configure("Head.TLabel", font=("Segoe UI", 11, "bold"))

    def _build_layout(self) -> None:
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(10, 4))

        self._build_merge_tab()
        self._build_organise_tab()
        self._build_split_tab()
        self._build_compress_tab()
        self._build_security_tab()

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=(0, 8))
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=140)
        self.progress.pack(side="right")
        self.status = tk.StringVar(value="Ready. Everything runs offline.")
        ttk.Label(bar, textvariable=self.status, style="Hint.TLabel").pack(
            side="left", fill="x", expand=True
        )

    # ---- threading plumbing ---------------------------------------------
    def _log(self, text: str) -> None:
        self._messages.put(("status", text))

    def _run_async(self, work, done_message: str) -> None:
        """Run a blocking job off the UI thread so the window stays alive."""
        if self._busy:
            messagebox.showinfo(APP_NAME, "A job is already running.")
            return
        self._busy = True
        self.progress.start(12)

        def runner() -> None:
            try:
                work()
                self._messages.put(("done", done_message))
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                self._messages.put(("error", str(exc)))

        threading.Thread(target=runner, daemon=True).start()

    def _drain_messages(self) -> None:
        while True:
            try:
                kind, payload = self._messages.get_nowait()
            except queue.Empty:
                break
            if kind == "status":
                self.status.set(str(payload))
            elif kind == "done":
                self._busy = False
                self.progress.stop()
                self.status.set(str(payload))
                messagebox.showinfo(APP_NAME, str(payload))
            elif kind == "error":
                self._busy = False
                self.progress.stop()
                self.status.set("Job failed.")
                messagebox.showerror(APP_NAME, str(payload))
        self.after(100, self._drain_messages)

    # ================= MERGE ==============================================
    def _build_merge_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="Merge")
        self.merge_items: list[MergeItem] = []

        ttk.Label(tab, text="Combine several PDFs into one",
                  style="Head.TLabel").pack(anchor="w")
        ttk.Label(tab, text="Files are merged top to bottom. Leave the page "
                            "range blank to include the whole file.",
                  style="Hint.TLabel").pack(anchor="w", pady=(0, 10))

        body = ttk.Frame(tab)
        body.pack(fill="both", expand=True)

        cols = ("file", "pages", "range")
        self.merge_tree = ttk.Treeview(body, columns=cols, show="headings",
                                       selectmode="browse")
        self.merge_tree.heading("file", text="File")
        self.merge_tree.heading("pages", text="Pages")
        self.merge_tree.heading("range", text="Range to use")
        self.merge_tree.column("file", width=420)
        self.merge_tree.column("pages", width=70, anchor="center")
        self.merge_tree.column("range", width=140)
        self.merge_tree.pack(side="left", fill="both", expand=True)
        self.merge_tree.bind("<Double-1>", self._edit_merge_range)

        side = ttk.Frame(body, padding=(10, 0, 0, 0))
        side.pack(side="left", fill="y")
        for label, cmd in (
            ("Add files", self._merge_add),
            ("Move up", lambda: self._merge_move(-1)),
            ("Move down", lambda: self._merge_move(1)),
            ("Set range", self._edit_merge_range),
            ("Remove", self._merge_remove),
            ("Clear all", self._merge_clear),
        ):
            ttk.Button(side, text=label, command=cmd, width=14).pack(pady=2)

        ttk.Button(tab, text="Merge and save as...",
                   command=self._do_merge).pack(anchor="e", pady=(12, 0))

    def _refresh_merge_tree(self) -> None:
        self.merge_tree.delete(*self.merge_tree.get_children())
        for item in self.merge_items:
            try:
                count = len(PdfReader(item.path).pages)
            except Exception:  # noqa: BLE001
                count = "?"
            self.merge_tree.insert(
                "", "end",
                values=(Path(item.path).name, count, item.ranges or "all"),
            )

    def _merge_selection(self) -> int | None:
        sel = self.merge_tree.selection()
        if not sel:
            return None
        return self.merge_tree.index(sel[0])

    def _merge_add(self) -> None:
        paths = filedialog.askopenfilenames(title="Choose PDFs", filetypes=PDF_TYPES)
        self.merge_items.extend(MergeItem(p) for p in paths)
        self._refresh_merge_tree()

    def _merge_move(self, offset: int) -> None:
        i = self._merge_selection()
        if i is None:
            return
        j = i + offset
        if 0 <= j < len(self.merge_items):
            self.merge_items[i], self.merge_items[j] = (
                self.merge_items[j], self.merge_items[i])
            self._refresh_merge_tree()
            kids = self.merge_tree.get_children()
            self.merge_tree.selection_set(kids[j])

    def _merge_remove(self) -> None:
        i = self._merge_selection()
        if i is not None:
            self.merge_items.pop(i)
            self._refresh_merge_tree()

    def _merge_clear(self) -> None:
        self.merge_items.clear()
        self._refresh_merge_tree()

    def _edit_merge_range(self, _event=None) -> None:
        i = self._merge_selection()
        if i is None:
            return
        from tkinter.simpledialog import askstring
        answer = askstring(
            "Page range",
            f"Pages to take from {Path(self.merge_items[i].path).name}\n"
            "Examples:  1-3   or   2, 5, 8-10   (blank = all pages)",
            initialvalue=self.merge_items[i].ranges,
            parent=self,
        )
        if answer is not None:
            self.merge_items[i].ranges = answer.strip()
            self._refresh_merge_tree()

    def _do_merge(self) -> None:
        if len(self.merge_items) < 2:
            messagebox.showinfo(APP_NAME, "Add at least two files to merge.")
            return
        out = filedialog.asksaveasfilename(
            title="Save merged PDF", defaultextension=".pdf",
            initialfile="merged.pdf", filetypes=PDF_TYPES)
        if not out:
            return
        items = list(self.merge_items)
        self._run_async(
            lambda: merge_pdfs(items, out, self._log),
            f"Merged {len(items)} files into {Path(out).name}",
        )

    # ================= ORGANISE ===========================================
    def _build_organise_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="Organise")
        self.org_path: str | None = None
        self.org_order: list[list[int]] = []   # [original_index, rotation]

        top = ttk.Frame(tab)
        top.pack(fill="x")
        ttk.Label(top, text="Rearrange, rotate and delete pages",
                  style="Head.TLabel").pack(side="left")
        ttk.Button(top, text="Open PDF...", command=self._org_open).pack(side="right")

        self.org_label = ttk.Label(tab, text="No file open.", style="Hint.TLabel")
        self.org_label.pack(anchor="w", pady=(2, 10))

        body = ttk.Frame(tab)
        body.pack(fill="both", expand=True)

        self.org_list = tk.Listbox(body, selectmode="extended",
                                   activestyle="none", exportselection=False)
        self.org_list.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(body, command=self.org_list.yview)
        scroll.pack(side="left", fill="y")
        self.org_list.config(yscrollcommand=scroll.set)

        side = ttk.Frame(body, padding=(10, 0, 0, 0))
        side.pack(side="left", fill="y")
        for label, cmd in (
            ("Move up", lambda: self._org_move(-1)),
            ("Move down", lambda: self._org_move(1)),
            ("Rotate left", lambda: self._org_rotate(-90)),
            ("Rotate right", lambda: self._org_rotate(90)),
            ("Delete page", self._org_delete),
            ("Reverse order", self._org_reverse),
            ("Reset", self._org_reset),
        ):
            ttk.Button(side, text=label, command=cmd, width=14).pack(pady=2)

        ttk.Button(tab, text="Save as...",
                   command=self._org_save).pack(anchor="e", pady=(12, 0))

    def _org_open(self) -> None:
        path = filedialog.askopenfilename(title="Open PDF", filetypes=PDF_TYPES)
        if not path:
            return
        try:
            reader = open_reader(path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_NAME, str(exc))
            return
        self.org_path = path
        self.org_order = [[i, 0] for i in range(len(reader.pages))]
        self.org_label.config(
            text=f"{Path(path).name}  -  {len(self.org_order)} pages, "
                 f"{human_size(os.path.getsize(path))}")
        self._refresh_org_list()

    def _refresh_org_list(self) -> None:
        keep = self.org_list.curselection()
        self.org_list.delete(0, "end")
        for position, (orig, rot) in enumerate(self.org_order, start=1):
            suffix = f"   rotated {rot % 360}deg" if rot % 360 else ""
            self.org_list.insert(
                "end", f"{position:>3}.  original page {orig + 1}{suffix}")
        for i in keep:
            if i < self.org_list.size():
                self.org_list.selection_set(i)

    def _org_indices(self) -> list[int]:
        return list(self.org_list.curselection())

    def _org_move(self, offset: int) -> None:
        selected = self._org_indices()
        if not selected or not self.org_order:
            return
        order = sorted(selected, reverse=offset > 0)
        if (offset < 0 and order[0] == 0) or (
                offset > 0 and order[0] == len(self.org_order) - 1):
            return
        for i in order:
            j = i + offset
            self.org_order[i], self.org_order[j] = self.org_order[j], self.org_order[i]
        self._refresh_org_list()
        self.org_list.selection_clear(0, "end")
        for i in selected:
            self.org_list.selection_set(i + offset)

    def _org_rotate(self, degrees: int) -> None:
        for i in self._org_indices():
            self.org_order[i][1] = (self.org_order[i][1] + degrees) % 360
        self._refresh_org_list()

    def _org_delete(self) -> None:
        selected = self._org_indices()
        if not selected:
            return
        if len(selected) == len(self.org_order):
            messagebox.showinfo(APP_NAME, "A PDF needs at least one page.")
            return
        for i in sorted(selected, reverse=True):
            self.org_order.pop(i)
        self._refresh_org_list()

    def _org_reverse(self) -> None:
        self.org_order.reverse()
        self._refresh_org_list()

    def _org_reset(self) -> None:
        if self.org_path:
            self._org_open_path(self.org_path)

    def _org_open_path(self, path: str) -> None:
        reader = open_reader(path)
        self.org_order = [[i, 0] for i in range(len(reader.pages))]
        self._refresh_org_list()

    def _org_save(self) -> None:
        if not self.org_path:
            messagebox.showinfo(APP_NAME, "Open a PDF first.")
            return
        out = filedialog.asksaveasfilename(
            title="Save rearranged PDF", defaultextension=".pdf",
            initialfile=f"{Path(self.org_path).stem}_organised.pdf",
            filetypes=PDF_TYPES)
        if not out:
            return
        src = self.org_path
        order = [(a, b) for a, b in self.org_order]
        self._run_async(
            lambda: save_arrangement(src, order, out),
            f"Saved {len(order)} pages to {Path(out).name}",
        )

    # ================= SPLIT ==============================================
    def _build_split_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="Split")

        ttk.Label(tab, text="Break one PDF into smaller files",
                  style="Head.TLabel").pack(anchor="w", pady=(0, 10))

        row = ttk.Frame(tab)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="Source file", width=14).pack(side="left")
        self.split_src = tk.StringVar()
        ttk.Entry(row, textvariable=self.split_src).pack(
            side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse...", command=lambda: self._pick_file(
            self.split_src)).pack(side="left", padx=(6, 0))

        row2 = ttk.Frame(tab)
        row2.pack(fill="x", pady=2)
        ttk.Label(row2, text="Save into", width=14).pack(side="left")
        self.split_dir = tk.StringVar()
        ttk.Entry(row2, textvariable=self.split_dir).pack(
            side="left", fill="x", expand=True)
        ttk.Button(row2, text="Browse...", command=lambda: self._pick_dir(
            self.split_dir)).pack(side="left", padx=(6, 0))

        box = ttk.LabelFrame(tab, text="How to split", padding=10)
        box.pack(fill="x", pady=14)
        self.split_mode = tk.StringVar(value="every")

        r1 = ttk.Frame(box); r1.pack(fill="x", pady=3)
        ttk.Radiobutton(r1, text="A new file every", variable=self.split_mode,
                        value="every").pack(side="left")
        self.split_every = tk.StringVar(value="1")
        ttk.Entry(r1, textvariable=self.split_every, width=5).pack(
            side="left", padx=6)
        ttk.Label(r1, text="page(s)").pack(side="left")

        r2 = ttk.Frame(box); r2.pack(fill="x", pady=3)
        ttk.Radiobutton(r2, text="Extract only these pages", 
                        variable=self.split_mode, value="ranges").pack(side="left")
        self.split_ranges = tk.StringVar()
        ttk.Entry(r2, textvariable=self.split_ranges, width=22).pack(
            side="left", padx=6)
        ttk.Label(r2, text="e.g. 2-5, 9", style="Hint.TLabel").pack(side="left")

        r3 = ttk.Frame(box); r3.pack(fill="x", pady=3)
        ttk.Radiobutton(r3, text="One file per page", variable=self.split_mode,
                        value="burst").pack(side="left")

        ttk.Button(tab, text="Split", command=self._do_split).pack(anchor="e")

    def _do_split(self) -> None:
        src, out_dir = self.split_src.get(), self.split_dir.get()
        if not src or not out_dir:
            messagebox.showinfo(APP_NAME, "Choose a source file and an output folder.")
            return
        mode = self.split_mode.get()
        value = self.split_every.get() if mode == "every" else self.split_ranges.get()
        self._run_async(
            lambda: split_pdf(src, out_dir, mode, value, self._log),
            f"Split finished. Files are in {out_dir}",
        )

    # ================= COMPRESS ===========================================
    def _build_compress_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="Compress")
        self.comp_files: list[str] = []

        ttk.Label(tab, text="Make PDFs smaller",
                  style="Head.TLabel").pack(anchor="w")
        note = ("Lossless keeps every page pixel-identical. Balanced and Strong "
                "shrink oversized images, which is where the real savings are "
                "in scanned documents.")
        if not HAVE_FITZ:
            note += "\nInstall PyMuPDF to unlock image downsampling."
        ttk.Label(tab, text=note, style="Hint.TLabel",
                  wraplength=760, justify="left").pack(anchor="w", pady=(0, 10))

        body = ttk.Frame(tab)
        body.pack(fill="both", expand=True)
        self.comp_list = tk.Listbox(body, selectmode="extended")
        self.comp_list.pack(side="left", fill="both", expand=True)
        side = ttk.Frame(body, padding=(10, 0, 0, 0))
        side.pack(side="left", fill="y")
        ttk.Button(side, text="Add files", width=14,
                   command=self._comp_add).pack(pady=2)
        ttk.Button(side, text="Remove", width=14,
                   command=self._comp_remove).pack(pady=2)
        ttk.Button(side, text="Clear all", width=14,
                   command=self._comp_clear).pack(pady=2)

        opts = ttk.Frame(tab)
        opts.pack(fill="x", pady=(12, 0))
        ttk.Label(opts, text="Level").pack(side="left")
        self.comp_level = tk.StringVar(value="balanced")
        for text, value in (("Lossless", "lossless"),
                            ("Balanced", "balanced"),
                            ("Strong", "strong")):
            state = "normal" if (value == "lossless" or HAVE_FITZ) else "disabled"
            ttk.Radiobutton(opts, text=text, value=value,
                            variable=self.comp_level,
                            state=state).pack(side="left", padx=6)
        if not HAVE_FITZ:
            self.comp_level.set("lossless")

        row = ttk.Frame(tab)
        row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text="Save into", width=14).pack(side="left")
        self.comp_dir = tk.StringVar()
        ttk.Entry(row, textvariable=self.comp_dir).pack(
            side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse...", command=lambda: self._pick_dir(
            self.comp_dir)).pack(side="left", padx=(6, 0))

        ttk.Button(tab, text="Compress", command=self._do_compress).pack(
            anchor="e", pady=(12, 0))

    def _comp_add(self) -> None:
        for p in filedialog.askopenfilenames(title="Choose PDFs", filetypes=PDF_TYPES):
            if p not in self.comp_files:
                self.comp_files.append(p)
                self.comp_list.insert(
                    "end", f"{Path(p).name}   ({human_size(os.path.getsize(p))})")

    def _comp_remove(self) -> None:
        for i in sorted(self.comp_list.curselection(), reverse=True):
            self.comp_list.delete(i)
            self.comp_files.pop(i)

    def _comp_clear(self) -> None:
        self.comp_files.clear()
        self.comp_list.delete(0, "end")

    def _do_compress(self) -> None:
        if not self.comp_files:
            messagebox.showinfo(APP_NAME, "Add at least one file.")
            return
        out_dir = self.comp_dir.get()
        if not out_dir:
            messagebox.showinfo(APP_NAME, "Choose an output folder.")
            return
        files, level = list(self.comp_files), self.comp_level.get()

        def work() -> None:
            before = after = 0
            for path in files:
                target = unique_path(
                    Path(out_dir) / f"{Path(path).stem}_compressed.pdf")
                self._log(f"Compressing {Path(path).name}...")
                compress_pdf(path, str(target), level, self._log)
                b, a = os.path.getsize(path), os.path.getsize(target)
                before += b
                after += a
                saved = (1 - a / b) * 100 if b else 0
                self._log(f"{Path(path).name}: {human_size(b)} -> "
                          f"{human_size(a)} ({saved:.0f}% smaller)")
            total = (1 - after / before) * 100 if before else 0
            self._messages.put((
                "done",
                f"Compressed {len(files)} file(s): {human_size(before)} -> "
                f"{human_size(after)}, {total:.0f}% smaller overall.",
            ))

        if self._busy:
            return
        self._busy = True
        self.progress.start(12)
        threading.Thread(
            target=lambda: self._guarded(work), daemon=True).start()

    def _guarded(self, work) -> None:
        try:
            work()
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._messages.put(("error", str(exc)))

    # ================= SECURITY ===========================================
    def _build_security_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="Security")

        ttk.Label(tab, text="Add or remove a password",
                  style="Head.TLabel").pack(anchor="w")
        ttk.Label(tab, text="Encryption uses AES-256. If you forget the password "
                            "the file cannot be recovered.",
                  style="Hint.TLabel").pack(anchor="w", pady=(0, 12))

        row = ttk.Frame(tab)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="Source file", width=14).pack(side="left")
        self.sec_src = tk.StringVar()
        ttk.Entry(row, textvariable=self.sec_src).pack(
            side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse...", command=lambda: self._pick_file(
            self.sec_src)).pack(side="left", padx=(6, 0))

        row2 = ttk.Frame(tab)
        row2.pack(fill="x", pady=(10, 2))
        ttk.Label(row2, text="Password", width=14).pack(side="left")
        self.sec_pw = tk.StringVar()
        ttk.Entry(row2, textvariable=self.sec_pw, show="\u2022", width=28).pack(
            side="left")

        actions = ttk.Frame(tab)
        actions.pack(anchor="w", pady=16)
        ttk.Button(actions, text="Add password",
                   command=lambda: self._do_security(True)).pack(side="left")
        ttk.Button(actions, text="Remove password",
                   command=lambda: self._do_security(False)).pack(
            side="left", padx=8)

    def _do_security(self, encrypting: bool) -> None:
        src, pw = self.sec_src.get(), self.sec_pw.get()
        if not src or not pw:
            messagebox.showinfo(APP_NAME, "Choose a file and type a password.")
            return
        suffix = "_locked" if encrypting else "_unlocked"
        out = filedialog.asksaveasfilename(
            title="Save as", defaultextension=".pdf",
            initialfile=f"{Path(src).stem}{suffix}.pdf", filetypes=PDF_TYPES)
        if not out:
            return
        if encrypting:
            self._run_async(lambda: encrypt_pdf(src, out, pw),
                            f"Password added. Saved as {Path(out).name}")
        else:
            self._run_async(lambda: decrypt_pdf(src, out, pw),
                            f"Password removed. Saved as {Path(out).name}")

    # ---- shared pickers --------------------------------------------------
    def _pick_file(self, var: tk.StringVar) -> None:
        path = filedialog.askopenfilename(title="Choose a PDF", filetypes=PDF_TYPES)
        if path:
            var.set(path)

    def _pick_dir(self, var: tk.StringVar) -> None:
        path = filedialog.askdirectory(title="Choose a folder")
        if path:
            var.set(path)


def main() -> None:
    PdfToolkitApp().mainloop()


if __name__ == "__main__":
    main()
