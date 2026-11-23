"""Interactive bubble-mask inspection and repair dialog for Manhwa OCR Editor."""
import copy
import queue
import threading
import tkinter as tk
from tkinter import ttk, colorchooser, messagebox
import cv2
import numpy as np
from PIL import Image, ImageTk
from bubble_cleaner import prepare_region, encode_patch

COMPONENT_VERSION='3.1'


class CleanEditor:
    def __init__(self, app, page, bubble, working_image, draft=None):
        self.app,self.page,self.bubble=app,page,bubble
        self.source=working_image
        self.margin=tk.IntVar(value=6)
        self.grow=tk.IntVar(value=6)
        self.radius=tk.IntVar(value=10)
        self.tool=tk.StringVar(value='add')
        self.method=tk.StringVar(value='auto')
        self.verify_after=tk.BooleanVar(value=True)
        self.allow_outside=tk.BooleanVar(value=True)
        self.picked_color=None
        self.result=None
        self.used_method=''
        self.history=[]
        self.events=queue.Queue();self.cancel=threading.Event();self.busy=False
        self.remaining_lines=[];self.last_anchor=None;self.lasso_points=[];self.active_mode=None
        self.region=copy.deepcopy(draft) if draft is not None else prepare_region(self.source,self.bubble,self.margin.get(),self.grow.get())
        self.note=tk.StringVar(value=self.region.note)
        self.win=tk.Toplevel(app.root)
        self.win.title('CLEAN 3.1 — มาสก์ตัวอักษร / ดูผลก่อนใช้')
        self.win.geometry('1220x780')
        self.win.transient(app.root)
        self.win.protocol('WM_DELETE_WINDOW',self.close)
        self._ui()
        self.fit()
        self.poll_id=self.win.after(80,self._poll)

    def _ui(self):
        top=ttk.Frame(self.win,padding=6);top.pack(fill='x')
        ttk.Button(top,text='1. อ่านคำซ้ำ + สร้างมาสก์',command=self.scan_mask).pack(side='left')
        ttk.Label(top,text='กันขอบ').pack(side='left',padx=(8,2))
        ttk.Spinbox(top,from_=2,to=24,textvariable=self.margin,width=4,command=self.rebuild).pack(side='left')
        ttk.Label(top,text='เผื่อเงา').pack(side='left',padx=(6,2))
        ttk.Spinbox(top,from_=0,to=18,textvariable=self.grow,width=4,command=self.rebuild).pack(side='left')
        ttk.Button(top,text='สร้างมาสก์ใหม่',command=self.rebuild).pack(side='left',padx=5)
        ttk.Button(top,text='ล้างมาสก์',command=self.clear_mask).pack(side='left')
        ttk.Button(top,text='ย้อนมาสก์',command=self.undo_mask).pack(side='left',padx=4)
        ttk.Button(top,text='เก็บมาสก์ไว้ก่อน',command=self.save_mask).pack(side='left')
        ttk.Separator(top,orient='vertical').pack(side='left',fill='y',padx=6)
        ttk.Label(top,text='โหมดคลีน').pack(side='left')
        ttk.Combobox(top,textvariable=self.method,values=['auto','solid','gradient','lama','inpaint'],
                     width=9,state='readonly').pack(side='left',padx=4)
        ttk.Button(top,text='เลือกสีพื้น',command=self.pick_color).pack(side='left')
        self.color_chip=tk.Label(top,text=' สีอัตโนมัติ ',bg='#eeeeee',fg='#111111',relief='groove')
        self.color_chip.pack(side='left',padx=4)
        ttk.Checkbutton(top,text='ตรวจละเอียดหลังคลีน',variable=self.verify_after).pack(side='left',padx=4)
        ttk.Button(top,text='ตรวจความพร้อม LaMa',command=self.check_lama).pack(side='left',padx=4)

        tools=ttk.Frame(self.win,padding=(6,0,6,6));tools.pack(fill='x')
        for label,val in [('เพิ่ม (B)','add'),('ลบ (E)','erase'),('วาดคลุม (L)','lasso'),('วงลบ','lasso_erase'),('สี่เหลี่ยม (R)','rect'),('ดูดสี (I)','sample')]:
            ttk.Radiobutton(tools,text=label,variable=self.tool,value=val).pack(side='left',padx=3)
        ttk.Label(tools,text='รัศมี').pack(side='left',padx=(8,2))
        ttk.Spinbox(tools,from_=2,to=48,textvariable=self.radius,width=4).pack(side='left')
        ttk.Separator(tools,orient='vertical').pack(side='left',fill='y',padx=6)
        ttk.Checkbutton(tools,text='เพิ่ม Mask นอกเส้นได้',variable=self.allow_outside,command=self.render).pack(side='left',padx=2)
        ttk.Button(tools,text='ปลดเส้นกั้นทั้งกรอบ',command=self.unlock_safe_boundary).pack(side='left',padx=4)
        ttk.Button(tools,text='ใช้ผลนี้',command=self.apply).pack(side='right')
        ttk.Button(tools,text='หยุด',command=lambda:self.cancel.set()).pack(side='right',padx=4)
        ttk.Button(tools,text='เติมจุดที่ยังอ่านได้',command=self.add_remaining).pack(side='right',padx=4)
        ttk.Button(tools,text='2. คลีน / ดูผล',command=self.preview).pack(side='right',padx=5)

        panes=ttk.Frame(self.win,padding=6);panes.pack(fill='both',expand=True)
        panes.columnconfigure(0,weight=1);panes.columnconfigure(1,weight=1);panes.rowconfigure(1,weight=1)
        ttk.Label(panes,text='ซ้าย: ต้นฉบับ + เส้นฟ้าขอบเขต + สีแดงคือจุดที่จะลบ (ติ๊ก "เพิ่ม Mask นอกเส้นได้" เพื่อระบายทะลุเส้นฟ้า)').grid(row=0,column=0,sticky='w')
        ttk.Label(panes,text='ขวา: ตัวอย่างหลังคลีน (พิกเซลนอกมาสก์ไม่ถูกแตะต้อง)').grid(row=0,column=1,sticky='w')
        self.left=tk.Canvas(panes,bg='#1c1f24',highlightthickness=1);self.right=tk.Canvas(panes,bg='#1c1f24',highlightthickness=1)
        self.left.grid(row=1,column=0,sticky='nsew',padx=(0,4));self.right.grid(row=1,column=1,sticky='nsew',padx=(4,0))
        self.left.bind('<Configure>',lambda e:self.fit())
        self.left.bind('<ButtonPress-1>',self.down);self.left.bind('<B1-Motion>',self.drag);self.left.bind('<ButtonRelease-1>',self.up)
        self.left.bind('<ButtonPress-3>',self.right_down);self.left.bind('<B3-Motion>',self.right_drag);self.left.bind('<ButtonRelease-3>',self.right_up)
        self.left.bind('<Motion>',self.hover);self.left.bind('<Leave>',lambda e:self.left.delete('cursor_ring'))
        self.left.bind('<Control-MouseWheel>',self.wheel_radius)
        self.win.bind('<Control-z>',lambda e:self.undo_mask())
        self.win.bind('<Key>',self._shortcut)
        ttk.Label(self.win,textvariable=self.note,padding=(8,2,8,2),wraplength=1180).pack(fill='x')
        ttk.Label(self.win,textvariable=self.app.lama_note,padding=(8,0,8,8),foreground='#2b4c7e').pack(fill='x')

    def _shortcut(self,event):
        if isinstance(event.widget,(tk.Entry,ttk.Spinbox,ttk.Combobox)):return
        k=(event.keysym or '').lower()
        mapping={'b':'add','e':'erase','l':'lasso','r':'rect','i':'sample'}
        if k in mapping:
            self.tool.set(mapping[k])
        elif k=='bracketleft':
            self.radius.set(max(2,self.radius.get()-2));self.hover(event)
        elif k=='bracketright':
            self.radius.set(min(48,self.radius.get()+2));self.hover(event)

    def wheel_radius(self,event):
        delta=2 if event.delta>0 else -2
        self.radius.set(int(np.clip(self.radius.get()+delta,2,48)))
        self.hover(event)
        return 'break'

    def hover(self,event):
        self.left.delete('cursor_ring')
        if self.tool.get() in ('add','erase') and hasattr(self,'scale'):
            r=max(2,round(self.radius.get()*self.scale))
            color='#ff5252' if self.tool.get()=='add' else '#52e0ff'
            self.left.create_oval(event.x-r,event.y-r,event.x+r,event.y+r,outline=color,width=1,tags='cursor_ring')

    def fit(self):
        w=max(80,self.left.winfo_width()-16);h=max(80,self.left.winfo_height()-16)
        iw,ih=self.region.image.size
        self.scale=min(4.0,max(.1,min(w/iw,h/ih)))
        self.render()

    def _expand_safe_for_drawn(self,drawn_mask):
        """Expand `region.safe` around user-painted strokes when outside-safe painting is allowed."""
        added=(drawn_mask>0)&(self.region.safe==0)
        if np.any(added):
            # Include a small clean-context collar around the stroke so solid/gradient/LaMa
            # can sample adjacent background pixels cleanly.
            collar=cv2.dilate(added.astype(np.uint8)*255,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(15,15)))
            self.region.safe=cv2.bitwise_or(self.region.safe,collar)

    def unlock_safe_boundary(self):
        if self.busy:return
        self.remember()
        self.region.safe[:,:]=255
        self.allow_outside.set(True)
        self.result=None
        self.note.set('ปลดล็อกเส้นกั้นทั้งกรอบแล้ว — ระบายมาสก์ได้ทุกจุดในกรอบนี้ (กด Ctrl+Z เพื่อย้อนกลับได้)')
        self.render()

    def remember(self):
        self.history.append((self.region.mask.copy(),self.region.safe.copy(),bool(getattr(self.region,'manual_edited',False))))
        self.history=self.history[-25:]

    def undo_mask(self):
        if self.history and not self.busy:
            old_mask,old_safe,old_edited=self.history.pop()
            self.region.mask=old_mask
            self.region.safe=old_safe
            self.region.manual_edited=old_edited
            self.result=None;self.render()

    def clear_mask(self):
        if self.busy:return
        self.remember();self.region.mask[:]=0;self.region.manual_edited=True;self.result=None;self.render()

    def rebuild(self):
        if self.busy:return
        self.remember()
        self.region=prepare_region(self.source,self.bubble,self.margin.get(),self.grow.get())
        self.result=None;self.last_anchor=None;self.note.set(self.region.note);self.fit()

    def check_lama(self):
        if self.busy:return
        self.busy=True;self.note.set('กำลังโหลดและทดสอบ LaMa ในเครื่อง…')
        def work():self.events.put(('lama',self.app.cleaner.check_lama(force=True)))
        threading.Thread(target=work,daemon=True).start()

    def scan_mask(self):
        if self.busy:return
        self.remember();self.busy=True;self.cancel.clear()
        settings=self.app.settings();bubble=copy.deepcopy(self.bubble)
        margin,grow=self.margin.get(),self.grow.get()
        self.note.set('กำลังอ่านคำซ้ำเฉพาะบับเบิ้ลนี้ในเครื่องเพื่อสร้างมาสก์…')
        def work():
            try:
                bubble.lines+=self.app.cleaning_lines(self.source,bubble,settings,self.cancel)
                self.events.put(('mask',prepare_region(self.source,bubble,margin,grow)))
            except Exception as exc:self.events.put(('error',str(exc)))
        threading.Thread(target=work,daemon=True).start()

    def pick_color(self):
        chosen=colorchooser.askcolor(parent=self.win,title='เลือกสีพื้นสำหรับเติม')
        if chosen and chosen[0]:
            self.picked_color=tuple(int(round(v)) for v in chosen[0])
            self.method.set('solid')
            self.color_chip.configure(bg=chosen[1],text=chosen[1])

    def image_xy(self,event):
        s=self.scale
        x=int(np.clip(self.left.canvasx(event.x)/s,0,self.region.image.width-1))
        y=int(np.clip(self.left.canvasy(event.y)/s,0,self.region.image.height-1))
        return x,y

    def down(self,event):
        if self.busy:return
        pt=self.image_xy(event)
        mode=self.tool.get()
        if mode=='sample':
            x,y=pt
            rgb=self.region.image.getpixel((x,y))
            self.picked_color=rgb;self.method.set('solid')
            hx='#%02x%02x%02x'%rgb
            self.color_chip.configure(bg=hx,text=hx,fg='#ffffff' if sum(rgb)<380 else '#111111')
            return
        self.remember()
        self.active_mode=mode
        if mode=='rect':
            self.rect_start=pt;self.rect_base=self.region.mask.copy();self.rect_safe=self.region.safe.copy()
        elif mode in ('lasso','lasso_erase'):
            self.lasso_points=[pt]
            self.left.delete('lasso_guide')
        elif mode in ('add','erase'):
            shift=bool(event.state & 0x0001)
            if shift and self.last_anchor is not None:
                self._paint_segment(self.last_anchor,pt,mode)
            else:
                self._paint_segment(pt,pt,mode)
            self.last_anchor=pt

    def drag(self,event):
        if self.busy or not self.active_mode:return
        pt=self.image_xy(event)
        mode=self.active_mode
        if mode in ('lasso','lasso_erase'):
            if not self.lasso_points or pt!=self.lasso_points[-1]:
                self.lasso_points.append(pt)
            self._draw_lasso_guide(mode=='lasso')
            return
        if mode=='rect':
            self.stroke(pt)
            return
        if mode in ('add','erase'):
            start=self.last_anchor or pt
            self._paint_segment(start,pt,mode)
            self.last_anchor=pt

    def up(self,event):
        if self.busy or not self.active_mode:return
        mode=self.active_mode
        if mode in ('lasso','lasso_erase'):
            self.left.delete('lasso_guide')
            if len(self.lasso_points)>=3:
                self._commit_lasso(self.lasso_points,add=(mode=='lasso'))
            self.lasso_points=[]
        self.active_mode=None

    def right_down(self,event):
        if self.busy:return
        self.remember()
        self.active_mode='erase'
        pt=self.image_xy(event)
        self._paint_segment(pt,pt,'erase')
        self.last_anchor=pt

    def right_drag(self,event):
        if self.busy or self.active_mode!='erase':return
        pt=self.image_xy(event)
        start=self.last_anchor or pt
        self._paint_segment(start,pt,'erase')
        self.last_anchor=pt

    def right_up(self,event):
        if self.active_mode=='erase':
            self.active_mode=None

    def _draw_lasso_guide(self,is_add=True):
        self.left.delete('lasso_guide')
        if len(self.lasso_points)<2:return
        s=self.scale
        coords=[v*s for p in self.lasso_points for v in p]
        coords.extend([self.lasso_points[0][0]*s,self.lasso_points[0][1]*s])
        color='#ff5252' if is_add else '#52e0ff'
        self.left.create_line(*coords,fill=color,width=2,dash=(4,2),tags='lasso_guide')

    def _commit_lasso(self,points,add=True):
        poly=np.array([points],dtype=np.int32)
        if add:
            layer=np.zeros_like(self.region.mask)
            cv2.fillPoly(layer,poly,255)
            if self.allow_outside.get():
                self._expand_safe_for_drawn(layer)
            else:
                layer=cv2.bitwise_and(layer,self.region.safe)
            self.region.mask=cv2.bitwise_or(self.region.mask,layer)
        else:
            cv2.fillPoly(self.region.mask,poly,0)
        self.region.manual_edited=True
        self.result=None;self.render()

    def _paint_segment(self,p0,p1,mode):
        r=max(1,int(self.radius.get()))
        if mode=='add':
            layer=np.zeros_like(self.region.mask)
            cv2.line(layer,p0,p1,255,thickness=2*r,lineType=cv2.LINE_AA)
            cv2.circle(layer,p1,r,255,-1)
            layer=(layer>64).astype(np.uint8)*255
            if self.allow_outside.get():
                self._expand_safe_for_drawn(layer)
            else:
                layer=cv2.bitwise_and(layer,self.region.safe)
            self.region.mask=cv2.bitwise_or(self.region.mask,layer)
        else:
            erase_layer=np.zeros_like(self.region.mask)
            cv2.line(erase_layer,p0,p1,255,thickness=2*r,lineType=cv2.LINE_AA)
            cv2.circle(erase_layer,p1,r,255,-1)
            self.region.mask[erase_layer>64]=0
        self.region.manual_edited=True
        self.result=None;self.render()

    def stroke(self,pt):
        """Compatibility single-point/rect stroke entry used by GUI tests and drag."""
        x,y=pt;mode=self.tool.get()
        if mode=='rect' and hasattr(self,'rect_start'):
            self.region.mask=self.rect_base.copy()
            if hasattr(self,'rect_safe'):self.region.safe=self.rect_safe.copy()
            sx,sy=self.rect_start
            layer=np.zeros_like(self.region.mask)
            layer[min(sy,y):max(sy,y)+1,min(sx,x):max(sx,x)+1]=255
            if self.allow_outside.get():
                self._expand_safe_for_drawn(layer)
            else:
                layer=cv2.bitwise_and(layer,self.region.safe)
            self.region.mask=cv2.bitwise_or(self.region.mask,layer)
        elif mode in ('add','erase'):
            self._paint_segment(pt,pt,mode)
            return
        self.region.manual_edited=True
        self.result=None;self.render()

    def preview(self):
        if self.busy:return
        self.busy=True;self.cancel.clear();self.remaining_lines=[]
        region=copy.deepcopy(self.region);method=self.method.get();color=self.picked_color
        verify=self.verify_after.get();settings=self.app.settings();bubble=copy.deepcopy(self.bubble)
        self.note.set('กำลังคลีนและตรวจผลเฉพาะมาสก์สีแดง…')
        def work():
            try:
                from clean_quality import clean_checked,quality_note
                def read_result(result):
                    candidate=self.source.copy();candidate.paste(result,(region.box[0],region.box[1]))
                    return self.app.cleaning_lines(candidate,bubble,settings,self.cancel)
                out,mask,used,report=clean_checked(self.app.cleaner,region,method,color,self.cancel,
                    reader=read_result if verify else None,max_passes=4 if verify else 1,
                    progress=lambda s:self.events.put(('status',s)))
                self.events.put(('preview',(out,mask,used,report,quality_note(report))))
            except InterruptedError:self.events.put(('status','หยุดคลีนแล้ว'))
            except Exception as exc:self.events.put(('error','คลีนไม่สำเร็จ: '+str(exc)))
        threading.Thread(target=work,daemon=True).start()

    def add_remaining(self):
        if self.busy or not self.remaining_lines:return
        self.remember();x0,y0=self.region.box[:2]
        layer=np.zeros_like(self.region.mask)
        for line in self.remaining_lines:
            b=[int(round(v)) for v in line['box']]
            lx0=max(0,b[0]-x0-8);ly0=max(0,b[1]-y0-8)
            lx1=min(self.region.mask.shape[1],b[2]-x0+8);ly1=min(self.region.mask.shape[0],b[3]-y0+8)
            layer[ly0:ly1,lx0:lx1]=255
        if self.allow_outside.get():
            self._expand_safe_for_drawn(layer)
        else:
            layer=cv2.bitwise_and(layer,self.region.safe)
        self.region.mask=cv2.bitwise_or(self.region.mask,layer)
        self.region.manual_edited=True
        self.result=None;self.render();self.note.set('เพิ่มกรอบที่ยังอ่านได้เข้าในมาสก์แล้ว กด 2. คลีน / ดูผลอีกครั้ง')

    def save_mask(self):
        if self.busy:return
        self.app.clean_prepared[(self.page.id,self.bubble.id)]=copy.deepcopy(self.region)
        self.app.dirty=True
        self.app.show_masks.set(True);self.app.refresh_clean_view()
        self.note.set('เก็บมาสก์ไว้ในคิวคลีนแล้ว — บันทึกโปรเจกต์เพื่อเก็บต่อ หรือกด 2. คลีนมาสก์บนหน้าหลัก')

    def _poll(self):
        try:
            while True:
                kind,data=self.events.get_nowait()
                if kind=='status':self.note.set(data)
                elif kind=='lama':
                    self.busy=False;self.app.lama_note.set(data['message']);self.note.set(data['message'])
                elif kind=='mask':
                    self.busy=False;self.region=data;self.result=None;self.note.set(data.note);self.fit()
                elif kind=='preview':
                    self.busy=False;out,mask,used,report,note=data
                    self.result=out;self.region.mask=mask;self.used_method=used
                    self.remaining_lines=report['remaining']
                    self.note.set(f'คลีนด้วย {used} | {note} — ตรวจขวาแล้วค่อยกดใช้ผลนี้');self.render()
                elif kind=='error':
                    self.busy=False;self.save_mask();self.note.set(data+' — เก็บมาสก์ไว้ให้แก้แล้ว');messagebox.showerror('CLEAN',data,parent=self.win)
        except queue.Empty:pass
        if self.win.winfo_exists():self.poll_id=self.win.after(80,self._poll)

    def apply(self):
        if self.result is None:self.preview()
        if self.result is None:return
        if not np.any(self.region.mask):
            messagebox.showinfo('ไม่มีจุดต้องคลีน','มาสก์ว่างจึงไม่ต้องแก้ภาพของกรอบนี้',parent=self.win);return
        patch=encode_patch(self.region.box,self.result,self.region.mask,self.used_method,self.bubble.id)
        self.app.apply_clean_patch(self.page.id,patch)
        self.close()

    def close(self):
        self.cancel.set()
        if hasattr(self,'poll_id'):self.win.after_cancel(self.poll_id)
        self.win.destroy()

    def render(self):
        arr=np.asarray(self.region.image.convert('RGB')).copy()
        red=self.region.mask>0
        arr[red]=(arr[red]*.45+np.array([235,55,55])*.55).astype(np.uint8)
        contours,_=cv2.findContours(self.region.safe,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        safe_color=(90,235,175) if self.allow_outside.get() else (50,210,255)
        cv2.drawContours(arr,contours,-1,safe_color,1)
        s=self.scale;size=(max(1,round(arr.shape[1]*s)),max(1,round(arr.shape[0]*s)))
        self.left_photo=ImageTk.PhotoImage(Image.fromarray(arr).resize(size,Image.Resampling.BILINEAR))
        right_img=(self.result or self.region.image).resize(size,Image.Resampling.BILINEAR)
        self.right_photo=ImageTk.PhotoImage(right_img)
        for canvas,photo in [(self.left,self.left_photo),(self.right,self.right_photo)]:
            canvas.delete('preview_img')
            canvas.create_image(8,8,anchor='nw',image=photo,tags='preview_img')
            canvas.tag_lower('preview_img')
            canvas.configure(scrollregion=(0,0,size[0]+16,size[1]+16))
