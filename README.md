# PDF Toolkit

An offline desktop app for working with PDF files: merge, rearrange, split,
compress, and password-protect. No internet connection, no uploads, no
account. Every operation happens on your own machine.

That last point is the whole reason this exists. The free online PDF tools
people reach for require uploading the document to a stranger's server — which
is exactly what you cannot do with a payroll run, a signed contract, an
incident report, or anything else covered by a company's data policy.

---

## 1. Set up

You need Python 3.10 or newer.

```bash
# from the folder containing pdf_toolkit.py
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

**Why a virtual environment?** It keeps these libraries out of your system
Python. When you later hand this app to someone else, you know exactly which
packages it needs, because nothing leaked in from elsewhere.

On most Linux distributions Tkinter is a separate package:

```bash
sudo apt install python3-tk      # Debian / Ubuntu
```

On Windows and macOS it ships with Python already.

## 2. Run

```bash
python pdf_toolkit.py
```

---

## What each tab does

**Merge** — add files, order them with Move up / Move down, and merge. Double
click any row to take only part of a file (`1-3`, `2, 5, 8-10`, or blank for
everything). Useful for stitching a cover letter, CV and transcripts into one
attachment.

**Organise** — open one PDF and reorder, rotate or delete its pages. Nothing
is written until you press Save as, so you can experiment freely. Rotation is
the one people need most: pages scanned sideways.

**Split** — break a file into fixed-size chunks, pull out a specific range, or
burst it into one file per page.

**Compress** — three levels:

| Level | What it does | Typical saving |
|---|---|---|
| Lossless | Rebuilds the file structure and recompresses streams. Pages stay pixel-identical. | 0–40% |
| Balanced | Also resamples images above 200 DPI down to 150 DPI. | ~55% on scans |
| Strong | Resamples above 130 DPI down to 96 DPI at lower quality. | ~85% on scans |

Balanced and Strong need PyMuPDF. Without it the app quietly falls back to
Lossless and tells you so in the status bar.

The reason the levels differ so much is that in a scanned document, almost all
the bytes are images. Rearranging the PDF's internal structure saves very
little; resampling a 600 DPI photo of a page down to 150 DPI saves a lot. A
text-only PDF barely shrinks at any level, because there is nothing bulky to
shrink — that is expected behaviour, not a bug.

**Security** — add or remove an AES-256 password. If you lose the password the
file is gone; there is no recovery path, by design.

---

## How the code is arranged

The file is deliberately split in two halves:

- **Core operations** (`merge_pdfs`, `save_arrangement`, `split_pdf`,
  `compress_pdf`, `encrypt_pdf`, `decrypt_pdf`) contain no Tkinter code at
  all. They take paths and return nothing, so you can test them, script them,
  or wrap them in a CLI later without touching the GUI.
- **`PdfToolkitApp`** is the Tkinter layer on top.

Keeping the two apart is the single most useful habit in desktop app work. A
UI is hard to test automatically; a function that takes two file paths is
trivial to test.

Long jobs run on a background thread (`_run_async`) and report back through a
`queue.Queue` that the UI polls every 100 ms. **Tkinter is not thread safe** —
only the main thread may touch widgets — so worker threads post messages to
the queue instead of updating labels directly. Without this the window would
freeze and Windows would grey it out with "Not responding" every time you
compressed a large file.

---

## Build a standalone .exe

So you can hand it to someone who does not have Python installed:

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name "PDF Toolkit" pdf_toolkit.py
```

The executable lands in `dist/`. `--windowed` suppresses the console window;
drop that flag while debugging so you can see tracebacks.

---

## Ideas for the next version

- Page thumbnails in the Organise tab (PyMuPDF can render a page to a PNG in
  about three lines) — dragging real thumbnails beats reading a list.
- Drag and drop files onto the window, via `tkinterdnd2`.
- A watch folder that compresses anything dropped into it automatically.
- Extract text and tables to Excel with `pdfplumber` — the natural bridge from
  this app to analytics work.
