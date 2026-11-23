#!/usr/bin/env python3
"""
╔══════════════════════════════════════════╗
║       Manhwa OCR Tool  v1.0              ║
║  Korean / English → Thai Translation    ║
║  Powered by Google Cloud Vision API     ║
╚══════════════════════════════════════════╝
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import os
import json
import threading
import requests
import base64
from pathlib import Path
from datetime import datetime

try:
    from paddleocr import PaddleOCR
    PADDLE_AVAILABLE = True
except ImportError:
    PADDLE_AVAILABLE = False

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request as GoogleAuthRequest
    GOOGLE_AUTH_AVAILABLE = True
except ImportError:
    GOOGLE_AUTH_AVAILABLE = False


# ════════════════════════════════════════════════════
#  Config Manager
# ════════════════════════════════════════════════════
class Config:
    FILE = "manhwa_ocr_config.json"

    @staticmethod
    def load():
        if os.path.exists(Config.FILE):
            try:
                with open(Config.FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    @staticmethod
    def save(data):
        try:
            with open(Config.FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass


# ════════════════════════════════════════════════════
#  Google Cloud Vision OCR Engine
# ════════════════════════════════════════════════════
class GoogleVisionOCR:
    ENDPOINT = "https://vision.googleapis.com/v1/images:annotate"
    SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

    def __init__(self, key_or_path):
        """
        key_or_path can be either:
          - a plain API key string (e.g. "AIzaSy...")
          - a path to a Service Account JSON credentials file
        Auto-detected based on whether it points to an existing .json file.
        """
        self.api_key = None
        self.credentials = None

        cleaned = key_or_path.strip().strip('"').strip("'")

        if cleaned.lower().endswith(".json") and os.path.isfile(cleaned):
            if not GOOGLE_AUTH_AVAILABLE:
                raise Exception(
                    "ตรวจพบไฟล์ Service Account JSON แต่ยังไม่ได้ติดตั้งไลบรารีที่จำเป็น\n"
                    "กรุณารัน: pip install google-auth"
                )
            try:
                self.credentials = service_account.Credentials.from_service_account_file(
                    cleaned, scopes=self.SCOPES
                )
            except Exception as exc:
                raise Exception(f"ไม่สามารถอ่านไฟล์ Service Account JSON ได้: {exc}")
        else:
            self.api_key = cleaned

    def _get_access_token(self):
        self.credentials.refresh(GoogleAuthRequest())
        return self.credentials.token

    def _encode_image(self, image_path):
        if PIL_AVAILABLE:
            img = Image.open(image_path)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            max_dim = 4096
            if img.width > max_dim or img.height > max_dim:
                ratio = min(max_dim / img.width, max_dim / img.height)
                img = img.resize(
                    (int(img.width * ratio), int(img.height * ratio)),
                    Image.LANCZOS,
                )
            import io
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=92)
            return base64.b64encode(buf.getvalue()).decode("utf-8")
        else:
            with open(image_path, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")

    def detect_bubbles(self, image_path):
        image_b64 = self._encode_image(image_path)
        payload = {
            "requests": [
                {
                    "image": {"content": image_b64},
                    "features": [{"type": "DOCUMENT_TEXT_DETECTION", "maxResults": 200}],
                    "imageContext": {"languageHints": ["ko", "en", "ja"]},
                }
            ]
        }

        if self.credentials is not None:
            token = self._get_access_token()
            resp = requests.post(
                self.ENDPOINT,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json=payload,
                timeout=60,
            )
        else:
            resp = requests.post(
                self.ENDPOINT,
                params={"key": self.api_key},
                json=payload,
                timeout=60,
            )

        if resp.status_code != 200:
            raise Exception(f"HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        api_resp = data.get("responses", [{}])[0]
        if "error" in api_resp:
            err = api_resp["error"]
            raise Exception(f"Vision API: {err.get('message', 'Unknown error')} (code {err.get('code')})")
        return self._parse_blocks(api_resp)

    def _parse_blocks(self, api_resp):
        bubbles = []
        full_text = api_resp.get("fullTextAnnotation", {})
        for page in full_text.get("pages", []):
            for block in page.get("blocks", []):
                block_lines = []
                vertices = block.get("boundingBox", {}).get("vertices", [])
                for para in block.get("paragraphs", []):
                    words_in_para = []
                    for word in para.get("words", []):
                        word_str = "".join(
                            sym.get("text", "") for sym in word.get("symbols", [])
                        )
                        words_in_para.append(word_str)
                    para_text = _join_words(words_in_para)
                    if para_text.strip():
                        block_lines.append(para_text.strip())
                full_text_block = "\n".join(block_lines).strip()
                if not full_text_block:
                    continue
                xs = [v.get("x", 0) for v in vertices]
                ys = [v.get("y", 0) for v in vertices]
                bubbles.append({
                    "text": full_text_block,
                    "x": min(xs) if xs else 0,
                    "y": min(ys) if ys else 0,
                    "vertices": vertices,
                })
        return bubbles


def _join_words(words):
    result = ""
    for i, word in enumerate(words):
        if i == 0:
            result += word
        else:
            prev = words[i - 1]
            if _is_cjk(prev[-1]) or _is_cjk(word[0]):
                result += word
            else:
                result += " " + word
    return result


def _is_cjk(ch):
    cp = ord(ch)
    return (
        0xAC00 <= cp <= 0xD7A3
        or 0x1100 <= cp <= 0x11FF
        or 0x3040 <= cp <= 0x30FF
        or 0x4E00 <= cp <= 0x9FFF
    )


def natural_sort_key(filename: str):
    """
    Split filename into text/number chunks so that '97_2.png' sorts
    before '97_10.png' (natural order) instead of plain alphabetical order.
    """
    import re
    return [
        int(chunk) if chunk.isdigit() else chunk.lower()
        for chunk in re.split(r"(\d+)", filename)
    ]


# ════════════════════════════════════════════════════
#  PaddleOCR Engine (fallback)
# ════════════════════════════════════════════════════
class PaddleOCREngine:
    def __init__(self, lang="korean"):
        if not PADDLE_AVAILABLE:
            raise RuntimeError("PaddleOCR not installed. Run: pip install paddleocr")
        self._ocr = PaddleOCR(use_angle_cls=True, lang=lang, show_log=False)

    def detect_bubbles(self, image_path):
        results = self._ocr.ocr(image_path, cls=True)
        bubbles = []
        if not results:
            return bubbles
        for line in results[0] or []:
            box, (text, _conf) = line
            if not text.strip():
                continue
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            bubbles.append({
                "text": text.strip(),
                "x": min(xs),
                "y": min(ys),
                "vertices": [{"x": int(p[0]), "y": int(p[1])} for p in box],
            })
        return bubbles


# ════════════════════════════════════════════════════
#  Bubble Sorting
# ════════════════════════════════════════════════════
def sort_bubbles(bubbles, row_gap=60):
    if not bubbles:
        return []
    sorted_by_y = sorted(bubbles, key=lambda b: b["y"])
    rows = []
    current_row = [sorted_by_y[0]]
    for bubble in sorted_by_y[1:]:
        if abs(bubble["y"] - current_row[0]["y"]) <= row_gap:
            current_row.append(bubble)
        else:
            rows.append(sorted(current_row, key=lambda b: b["x"]))
            current_row = [bubble]
    if current_row:
        rows.append(sorted(current_row, key=lambda b: b["x"]))
    return [b for row in rows for b in row]


# ════════════════════════════════════════════════════
#  Output Writer
# ════════════════════════════════════════════════════
def write_output(results, output_dir):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = Path(output_dir) / f"manhwa_ocr_{timestamp}.txt"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("━" * 60 + "\n")
        f.write("  MANHWA OCR OUTPUT\n")
        f.write(f"  สร้างเมื่อ: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n")
        f.write("━" * 60 + "\n\n")
        for img_result in results:
            f.write("═" * 60 + "\n")
            f.write(f"【 ไฟล์ 】 {img_result['filename']}\n")
            f.write("═" * 60 + "\n\n")
            if img_result.get("error"):
                f.write(f"❌ เกิดข้อผิดพลาด: {img_result['error']}\n\n")
                continue
            if not img_result.get("bubbles"):
                f.write("  (ไม่พบข้อความในภาพนี้)\n\n")
                continue
            for bubble in img_result["bubbles"]:
                f.write(f"คำต้นฉบับ: {bubble['text']}\n")
                f.write(f"คำแปลไทย: \n\n")
    return str(out_path)


# ════════════════════════════════════════════════════
#  GUI Application
# ════════════════════════════════════════════════════
DARK_BG   = "#0d0f18"
PANEL_BG  = "#13162a"
ACCENT    = "#ff4d6d"
ACCENT2   = "#4d9fff"
TEXT_MAIN = "#e8eaf6"
TEXT_MUTED= "#6b7280"
TEXT_GREEN= "#00e5a0"
ENTRY_BG  = "#1c2035"
BORDER    = "#2a2f4e"


class ManhwaOCRApp:
    def __init__(self, root):
        self.root = root
        self.root.title("🎌 Manhwa OCR Tool")
        self.root.geometry("860x720")
        self.root.minsize(700, 600)
        self.root.configure(bg=DARK_BG)

        self.input_folder  = tk.StringVar()
        self.output_folder = tk.StringVar()
        self.api_key       = tk.StringVar()
        self.engine_var    = tk.StringVar(value="google")
        self.is_processing = False

        cfg = Config.load()
        self.api_key.set(cfg.get("api_key", ""))
        self.input_folder.set(cfg.get("input_folder", ""))
        self.output_folder.set(cfg.get("output_folder", ""))
        self.engine_var.set(cfg.get("engine", "google"))

        self._build_ui()
        self._update_engine_state()

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Horizontal.TProgressbar", troughcolor=PANEL_BG, background=ACCENT, thickness=6)

        outer = tk.Frame(self.root, bg=DARK_BG, padx=24, pady=20)
        outer.pack(fill=tk.BOTH, expand=True)

        # Header
        hdr = tk.Frame(outer, bg=DARK_BG)
        hdr.pack(fill=tk.X, pady=(0, 18))
        tk.Label(hdr, text="🎌 Manhwa OCR Tool", font=("Helvetica", 22, "bold"), bg=DARK_BG, fg=ACCENT).pack(side=tk.LEFT)
        tk.Label(hdr, text="Korean · English → Thai", font=("Helvetica", 11), bg=DARK_BG, fg=TEXT_MUTED).pack(side=tk.LEFT, padx=(14, 0), pady=(8, 0))

        # API Key
        self._section(outer, "🔑 Google Cloud Vision — API Key หรือไฟล์ Service Account JSON")
        api_row = tk.Frame(outer, bg=PANEL_BG, padx=14, pady=10)
        api_row.pack(fill=tk.X, pady=(0, 12))
        self.api_entry = tk.Entry(api_row, textvariable=self.api_key, show="●", bg=ENTRY_BG, fg=TEXT_MAIN, insertbackground=TEXT_MAIN, relief="flat", font=("Courier", 10))
        self.api_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=5)
        tk.Button(api_row, text="📄 .json", bg=BORDER, fg=TEXT_MAIN, activebackground=ACCENT2, activeforeground="white", relief="flat", padx=10, cursor="hand2", command=self._browse_json_key).pack(side=tk.LEFT, padx=(8, 0))
        tk.Button(api_row, text="👁 Show", bg=BORDER, fg=TEXT_MAIN, activebackground=ACCENT, activeforeground="white", relief="flat", padx=10, cursor="hand2", command=self._toggle_key_vis).pack(side=tk.LEFT, padx=(8, 0))

        # Engine
        self._section(outer, "⚙️ OCR Engine")
        eng_row = tk.Frame(outer, bg=PANEL_BG, padx=14, pady=10)
        eng_row.pack(fill=tk.X, pady=(0, 12))
        tk.Radiobutton(eng_row, text="Google Cloud Vision  (recommended)", variable=self.engine_var, value="google", bg=PANEL_BG, fg=TEXT_MAIN, selectcolor=ENTRY_BG, activebackground=PANEL_BG, font=("Helvetica", 10), command=self._update_engine_state).pack(side=tk.LEFT)
        tk.Radiobutton(eng_row, text=f"PaddleOCR  {'(installed)' if PADDLE_AVAILABLE else '(not installed)'}", variable=self.engine_var, value="paddle", bg=PANEL_BG, fg=TEXT_MAIN if PADDLE_AVAILABLE else TEXT_MUTED, selectcolor=ENTRY_BG, activebackground=PANEL_BG, font=("Helvetica", 10), state=tk.NORMAL if PADDLE_AVAILABLE else tk.DISABLED, command=self._update_engine_state).pack(side=tk.LEFT, padx=(20, 0))

        # Folders
        self._section(outer, "📂 โฟลเดอร์ภาพ Manhwa")
        self._folder_row(outer, self.input_folder, self._browse_input)
        self._section(outer, "💾 โฟลเดอร์บันทึก Output (.txt)")
        self._folder_row(outer, self.output_folder, self._browse_output)

        # Start button
        btn_row = tk.Frame(outer, bg=DARK_BG)
        btn_row.pack(fill=tk.X, pady=(10, 0))
        self.start_btn = tk.Button(btn_row, text="🚀  เริ่ม OCR", font=("Helvetica", 13, "bold"), bg=ACCENT, fg="white", activebackground="#c0253e", activeforeground="white", relief="flat", padx=36, pady=11, cursor="hand2", command=self._on_start)
        self.start_btn.pack(side=tk.LEFT)
        self.file_count_label = tk.Label(btn_row, text="", bg=DARK_BG, fg=TEXT_MUTED, font=("Helvetica", 10))
        self.file_count_label.pack(side=tk.LEFT, padx=16)

        # Progress
        self.progress_var = tk.DoubleVar()
        ttk.Progressbar(outer, variable=self.progress_var, maximum=100, style="Horizontal.TProgressbar").pack(fill=tk.X, pady=(12, 3))
        self.status_lbl = tk.Label(outer, text="พร้อมใช้งาน", bg=DARK_BG, fg=TEXT_MUTED, font=("Helvetica", 9), anchor="w")
        self.status_lbl.pack(fill=tk.X)

        # Log
        self._section(outer, "📋 Log การทำงาน")
        log_wrap = tk.Frame(outer, bg=PANEL_BG)
        log_wrap.pack(fill=tk.BOTH, expand=True, pady=(0, 4))
        self.log_text = scrolledtext.ScrolledText(log_wrap, bg="#070910", fg=TEXT_GREEN, font=("Courier", 9), insertbackground="white", relief="flat", padx=10, pady=8, state="disabled", height=10)
        self.log_text.pack(fill=tk.BOTH, expand=True)

        self.input_folder.trace_add("write", self._update_file_count)

    def _section(self, parent, title):
        tk.Label(parent, text=title, font=("Helvetica", 10, "bold"), bg=DARK_BG, fg=ACCENT2, anchor="w").pack(fill=tk.X, pady=(6, 3))

    def _folder_row(self, parent, var, cmd):
        row = tk.Frame(parent, bg=PANEL_BG, padx=14, pady=8)
        row.pack(fill=tk.X, pady=(0, 12))
        tk.Entry(row, textvariable=var, bg=ENTRY_BG, fg=TEXT_MAIN, insertbackground=TEXT_MAIN, relief="flat", font=("Helvetica", 10)).pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=5)
        tk.Button(row, text="Browse", bg=ACCENT2, fg="white", activebackground="#2a7fd4", activeforeground="white", relief="flat", padx=14, cursor="hand2", command=cmd).pack(side=tk.LEFT, padx=(8, 0))

    def _toggle_key_vis(self):
        self.api_entry.config(show="" if self.api_entry.cget("show") else "●")

    def _browse_json_key(self):
        path = filedialog.askopenfilename(
            title="เลือกไฟล์ Service Account JSON",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if path:
            self.api_key.set(path)
            self.api_entry.config(show="")

    def _browse_input(self):
        folder = filedialog.askdirectory(title="เลือกโฟลเดอร์ภาพ Manhwa")
        if folder:
            self.input_folder.set(folder)

    def _browse_output(self):
        folder = filedialog.askdirectory(title="เลือกโฟลเดอร์บันทึก Output")
        if folder:
            self.output_folder.set(folder)

    def _update_engine_state(self):
        self.api_entry.config(state="disabled" if self.engine_var.get() == "paddle" else "normal")

    def _update_file_count(self, *_):
        folder = self.input_folder.get()
        if not folder or not os.path.isdir(folder):
            self.file_count_label.config(text="")
            return
        exts = {".jpg", ".jpeg", ".png", ".webp"}
        count = sum(1 for f in Path(folder).iterdir() if f.suffix.lower() in exts)
        self.file_count_label.config(
            text=f"พบ {count} ภาพ {'✓' if count >= 1 else ''}",
            fg=TEXT_GREEN if count >= 1 else ACCENT,
        )

    def _log(self, msg):
        def _do():
            self.log_text.config(state="normal")
            self.log_text.insert(tk.END, msg + "\n")
            self.log_text.see(tk.END)
            self.log_text.config(state="disabled")
        self.root.after(0, _do)

    def _set_status(self, msg):
        self.root.after(0, lambda: self.status_lbl.config(text=msg))

    def _set_progress(self, pct):
        self.root.after(0, lambda: self.progress_var.set(pct))

    def _on_start(self):
        if self.is_processing:
            return
        engine = self.engine_var.get()
        if engine == "google" and not self.api_key.get().strip():
            messagebox.showerror("Error", "กรุณาใส่ Google Cloud Vision API Key")
            return
        inp = self.input_folder.get()
        if not inp or not os.path.isdir(inp):
            messagebox.showerror("Error", "กรุณาเลือกโฟลเดอร์ภาพที่ถูกต้อง")
            return
        out = self.output_folder.get()
        if not out:
            messagebox.showerror("Error", "กรุณาเลือกโฟลเดอร์บันทึก Output")
            return
        os.makedirs(out, exist_ok=True)
        exts = {".jpg", ".jpeg", ".png", ".webp"}
        images = sorted([f for f in Path(inp).iterdir() if f.suffix.lower() in exts], key=lambda p: natural_sort_key(p.name))
        if not images:
            messagebox.showerror("Error", "ไม่พบไฟล์ภาพในโฟลเดอร์ที่เลือก")
            return
        Config.save({"api_key": self.api_key.get(), "input_folder": inp, "output_folder": out, "engine": engine})
        self.is_processing = True
        self.start_btn.config(state="disabled", text="⏳  กำลังประมวลผล…")
        self.progress_var.set(0)
        threading.Thread(target=self._run_ocr, args=(images, engine), daemon=True).start()

    def _run_ocr(self, images, engine):
        try:
            ocr_engine = GoogleVisionOCR(self.api_key.get().strip()) if engine == "google" else PaddleOCREngine(lang="korean")
            all_results = []
            self._log(f"🚀 เริ่มประมวลผล {len(images)} ภาพ  (engine: {engine})")
            self._log("─" * 52)
            for i, img_path in enumerate(images):
                self._set_status(f"กำลังอ่าน ({i+1}/{len(images)}): {img_path.name}")
                self._log(f"\n📄 {img_path.name}")
                result = {"filename": img_path.name, "bubbles": []}
                try:
                    raw_bubbles = ocr_engine.detect_bubbles(str(img_path))
                    sorted_bubbles = sort_bubbles(raw_bubbles)
                    result["bubbles"] = sorted_bubbles
                    self._log(f"   ✅ พบ {len(sorted_bubbles)} bubble(s)")
                    for j, b in enumerate(sorted_bubbles, 1):
                        preview = b["text"][:70].replace("\n", " ")
                        self._log(f"   [{j}] {preview}{'…' if len(b['text']) > 70 else ''}")
                except Exception as exc:
                    result["error"] = str(exc)
                    self._log(f"   ❌ Error: {exc}")
                all_results.append(result)
                self._set_progress((i + 1) / len(images) * 100)
            self._log("\n" + "─" * 26)
            self._log("💾 กำลังบันทึกผลลัพธ์…")
            out_file = write_output(all_results, self.output_folder.get())
            self._log(f"✅ บันทึกแล้วที่: {out_file}")
            self._set_status(f"เสร็จสิ้น! → {out_file}")
            total = sum(len(r.get("bubbles", [])) for r in all_results)
            self.root.after(0, lambda: messagebox.showinfo("เสร็จสิ้น!", f"OCR เสร็จแล้ว!\n\n• ประมวลผล: {len(images)} ภาพ\n• Bubble ทั้งหมด: {total}\n\nOutput:\n{out_file}"))
        except Exception as fatal:
            self._log(f"\n❌ Fatal: {fatal}")
            self.root.after(0, lambda: messagebox.showerror("Error", f"เกิดข้อผิดพลาด:\n{fatal}"))
        finally:
            self.is_processing = False
            self.root.after(0, lambda: self.start_btn.config(state="normal", text="🚀  เริ่ม OCR"))


def main():
    root = tk.Tk()
    ManhwaOCRApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
