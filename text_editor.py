"""Paste/import translations with stable bubble IDs, then create type layers."""
import tkinter as tk
from tkinter import ttk,filedialog,messagebox,scrolledtext
from pathlib import Path
from text_layout import translation_template,export_source_script,detect_and_parse_translations
from app_ui import BG,FIELD,WHITE,SELECT,style_text_scrollbar

COMPONENT_VERSION='3.1'


class TextImportDialog:
    def __init__(self,app):
        self.app=app;self.pages=list(app.pages)
        self.win=tk.Toplevel(app.root);self.win.title('เพิ่ม Text — รับคำแปลและจัดวางในบับเบิ้ล')
        self.win.configure(bg=BG)
        width=min(840,self.win.winfo_screenwidth()-80)
        height=min(650,self.win.winfo_screenheight()-120)
        self.win.geometry(f'{width}x{height}');self.win.transient(app.root);self.win.grab_set()
        # Reserve actions before the expanding editor, including on high-DPI screens.
        footer=ttk.Frame(self.win);footer.pack(side='bottom',fill='x',padx=12,pady=(8,12))
        self.apply_button=ttk.Button(footer,text='นำเข้าคำแปล + เพิ่ม Text',command=self.apply)
        self.apply_button.pack(fill='x',pady=(0,6))
        actions=ttk.Frame(footer);actions.pack(fill='x')
        ttk.Button(actions,text='ปิด',command=self.win.destroy).pack(side='right')
        ttk.Button(actions,text='จัด Text จากคำแปลที่มีในโปรแกรม',command=self.existing).pack(side='left')
        self.note=tk.StringVar(value='เปิด TXT แล้ว กด “นำเข้าคำแปล + เพิ่ม Text” เพื่อจัดวางข้อความ')
        note=ttk.Label(footer,textvariable=self.note,wraplength=width-24)
        note.pack(fill='x',pady=(6,0))
        footer.bind('<Configure>',lambda event:note.configure(wraplength=max(100,event.width)))
        ttk.Label(self.win,text='เลือกรูปแบบส่งแปล แล้ววางคำแปลหรือเปิดไฟล์ TXT เพื่อตรวจการจับคู่ก่อนจัด Text',wraplength=800).pack(padx=12,pady=10,anchor='w')
        ttk.Label(self.win,text='TH Sarabun New Bold • สีดำ/ขาวตามพื้น • Auto • Tracking 0 • Metrics • Sharp • ขนาดตามบับเบิ้ล').pack(padx=12,anchor='w')
        bar=ttk.Frame(self.win);bar.pack(fill='x',padx=12,pady=8)
        self.formats={'แบบรหัสกรอบ':'id_template','แบบเลขข้อ':'numbered','ต้นฉบับล้วน':'plain_lines'}
        self.export_choice=tk.StringVar(value='แบบรหัสกรอบ')
        ttk.Combobox(bar,textvariable=self.export_choice,values=list(self.formats),state='readonly',width=15).pack(side='left')
        for title,action in [('คัดลอกไฟล์ส่งแปล',self.copy),('บันทึกไฟล์ส่งแปล',self.save),('เปิดคำแปล TXT',self.load)]:
            ttk.Button(bar,text=title,command=action).pack(side='left',padx=3)
        self.editor=scrolledtext.ScrolledText(self.win,wrap='word',font=app.ui_font,undo=True,height=4,
            bg=FIELD,fg=WHITE,insertbackground=WHITE,selectbackground=SELECT,selectforeground=WHITE)
        style_text_scrollbar(self.editor)
        self.editor.pack(fill='both',expand=True,padx=12)
        self.editor.insert('1.0',translation_template(self.pages))

    def copy(self):
        self.win.clipboard_clear();self.win.clipboard_append(export_source_script(self.pages,self.formats[self.export_choice.get()]))
        self.note.set('คัดลอกแล้ว • '+self.export_choice.get())

    def save(self):
        path=filedialog.asksaveasfilename(parent=self.win,defaultextension='.txt',initialfile='manhwa_for_translation.txt',filetypes=[('Text','*.txt')])
        if path:
            try:Path(path).write_text(export_source_script(self.pages,self.formats[self.export_choice.get()])+'\n',encoding='utf-8')
            except Exception as exc:messagebox.showerror('บันทึกไม่สำเร็จ',str(exc),parent=self.win)

    def load(self):
        path=filedialog.askopenfilename(parent=self.win,filetypes=[('Text','*.txt')])
        if path:
            try:
                value=Path(path).read_text(encoding='utf-8-sig')
                self.editor.delete('1.0','end');self.editor.insert('1.0',value)
                self.note.set('เปิดคำแปลแล้ว — กด “นำเข้าคำแปล + เพิ่ม Text” ด้านล่างเพื่อจัดวาง')
            except Exception as exc:messagebox.showerror('เปิดไม่สำเร็จ',str(exc),parent=self.win)

    def apply(self):
        self.editor.tag_remove('import_error','1.0','end')
        try:changes,report=detect_and_parse_translations(self.editor.get('1.0','end-1c'),self.pages)
        except Exception as exc:
            line=getattr(exc,'line',None)
            if line:
                self.editor.tag_configure('import_error',background='#ffe0df',foreground='#8b0000')
                self.editor.tag_add('import_error',f'{line}.0',f'{line}.end')
                self.editor.see(f'{line}.0')
            self.note.set('ยังไม่ได้เปลี่ยนคำแปลในโปรแกรม — ตรวจข้อความที่แจ้งแล้วนำเข้าอีกครั้ง')
            messagebox.showerror('ยังนำเข้าไม่ได้',str(exc),parent=self.win);return
        labels={'id_template':'รหัสกรอบ','numbered':'เลขข้อ','tsv':'ตาราง TSV / Google Sheets',
                'labelled_pairs':'คู่ต้นฉบับและคำแปลมีป้าย','alternating_pairs':'ต้นฉบับสลับคำแปล','plain_lines':'คำแปลเรียงบรรทัด'}
        summary=[labels[report['detected_format']],f"จับคู่ {len(changes)} / {report['total_bubbles']} กรอบ",
                 f"กรอบขาดคำแปล {report['unmatched_bubbles']} • รายการเกินหรือไม่ตรง {report['extra_items']}"]
        summary.extend(report['warnings'][:3])
        def short(value):
            value=' '.join(value.split())
            return value[:39]+'…' if len(value)>40 else value
        summary.extend(f'กรอบ #{i}: {short(src)} → {short(tr)}' for i,src,tr in report['samples'])
        if not changes:
            messagebox.showinfo('ไม่มีคำแปลที่จับคู่ได้','\n'.join(summary),parent=self.win);return
        if not messagebox.askokcancel('ตรวจคำแปลก่อนจัด Text','\n'.join(summary),parent=self.win):return
        self.app.log(summary[1])
        for note in report['warnings']:self.app.log(note)
        self.win.destroy()
        if report["frame_tags"]:self.app.start_text_layout(changes,frame_tags=report["frame_tags"])
        else:self.app.start_text_layout(changes)

    def existing(self):
        self.win.destroy();self.app.start_text_layout()
