"""Capture only our disposable Tk test window, never the user's desktop."""
import ctypes
import sys
from pathlib import Path
import tkinter as tk
from PIL import ImageGrab
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manhwa_ocr import ManhwaOCRApp,Bubble

root=tk.Tk();app=ManhwaOCRApp(root)
try:
    root.update()
    folder=Path(__file__).resolve().parents[1]/'examples/v3_0'
    app.add_paths([folder/'original.png']);root.update()
    bubble=Bubble([40,30,600,390],text='HELLO WORLD',translation='สวัสดี ยินดีที่ได้รู้จัก',score=.95,engine='ตัวอย่าง')
    app.page().bubbles=[bubble];app.select_page(0);app.tree.selection_set(bubble.id)
    root.update();app.fit();root.update()
    states='--states' in sys.argv
    if states:
        app.ui_buttons['add_images'].state(['active','focus'])
        app.ui_buttons['add_folder'].state(['pressed'])
        app.ui_buttons['open_project'].state(['disabled'])
        root.update()
    hwnd=ctypes.windll.user32.GetParent(root.winfo_id())
    target=folder/('UI_button_states.png' if states else 'UI_game.png')
    ImageGrab.grab(window=hwnd).save(target)
    # Grid stays under page pixels; redraws replace items rather than accumulate.
    original=app.image.tobytes();count=len(app.canvas.find_withtag('workspace_grid'))
    for _ in range(5):app.render()
    assert app.image.tobytes()==original
    assert len(app.canvas.find_withtag('workspace_grid'))==count
    stacking=list(app.canvas.find_all())
    assert max(stacking.index(i) for i in app.canvas.find_withtag('workspace_grid'))<min(stacking.index(i) for i in app.canvas.find_withtag('page_image'))
    print('Preview:',target)
    print('Font:',app.ui_font_family,'Canvas:',app.canvas.winfo_width(),app.canvas.winfo_height())
finally:
    root.after_cancel(app.poll_id);root.destroy()
