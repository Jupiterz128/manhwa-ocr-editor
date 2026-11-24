"""Desktop workspace: large image, batch workflow and the original OCR editor."""
import tkinter as tk
from tkinter import ttk, scrolledtext, font as tkfont
from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageTk
from text_layout import FONT_CHOICES

COMPONENT_VERSION='4.0'
BG='#191a1a'
PANEL='#0b1218'
FIELD='#080d12'
WHITE='#f0f4ee'
GREEN='#98e55b'
PURPLE='#aa83f3'
SELECT='#493769'
COLORS={'files':GREEN,'batch':GREEN,'text':PURPLE,'settings':PURPLE,
        'stop':'#e2b96d','image':GREEN,'pages':PURPLE,'editor':PURPLE,
        'inspect':GREEN,'status':GREEN}


def draw_workspace_grid(canvas,viewport=False):
    """Uiverse adamgiebl grid adapted from two 55 px CSS gradient layers.

    Draw only the viewport, in screen coordinates, never into page pixels.
    See third_party/uiverse for the MIT notices and original CSS.
    """
    width=canvas.winfo_width();height=canvas.winfo_height()
    x0=canvas.canvasx(0) if viewport else 0
    y0=canvas.canvasy(0) if viewport else 0
    previous=getattr(canvas,'_workspace_grid_geometry',None)
    if previous and previous[:2]==(width,height):
        dx=x0-previous[2];dy=y0-previous[3]
        if dx or dy:canvas.move('workspace_grid',dx,dy)
        canvas._workspace_grid_geometry=(width,height,x0,y0)
        return
    canvas._workspace_grid_geometry=(width,height,x0,y0)
    canvas.delete('workspace_grid')
    canvas.create_rectangle(x0,y0,x0+width,y0+height,fill=BG,outline='',tags='workspace_grid')
    for x in range(int(width/27.5)+1):
        position=x0+13.75+x*27.5
        canvas.create_line(position,y0,position,y0+height,fill='#343434',tags='workspace_grid')
    for y in range(int(height/27.5)+1):
        position=y0+13.75+y*27.5
        canvas.create_line(x0,position,x0+width,position,fill='#343434',tags='workspace_grid')
    canvas.tag_lower('workspace_grid')


class RoundedScrollbar(tk.Canvas):
    """Bounded vector scrollbar: no tiled image redraws on each thumb move."""
    def __init__(self,parent,orient='vertical',command=None,wheel=None,unit_step=1):
        self.vertical=orient=='vertical';self.command=command;self.wheel_command=wheel
        self.unit_step=unit_step
        self.first=0.;self.last=1.;self.drag=None;self.hover=False;self.focused=False
        super().__init__(parent,width=14 if self.vertical else 28,height=28 if self.vertical else 14,
            bg=PANEL,highlightthickness=0,takefocus=True)
        self.parts={name:[self.create_rectangle(0,0,1,1,width=0,fill=color),
                         self.create_oval(0,0,1,1,width=0,fill=color),
                         self.create_oval(0,0,1,1,width=0,fill=color)]
                    for name,color in [('track','#111820'),('thumb','#8976b8')]}
        self.bind('<Configure>',lambda e:self.redraw())
        self.bind('<Enter>',lambda e:self.highlight(True));self.bind('<Leave>',lambda e:self.highlight(False))
        self.bind('<FocusIn>',lambda e:self.focus(True));self.bind('<FocusOut>',lambda e:self.focus(False))
        self.bind('<ButtonPress-1>',self.press);self.bind('<B1-Motion>',self.motion)
        self.bind('<ButtonRelease-1>',self.release);self.bind('<MouseWheel>',self.wheel)
        for key,amount,unit in [('Up',-1,'units'),('Left',-1,'units'),('Down',1,'units'),('Right',1,'units'),('Prior',-1,'pages'),('Next',1,'pages')]:
            self.bind('<'+key+'>',lambda e,a=amount,u=unit:self.scroll(a,u))
        self.bind('<Home>',lambda e:self.move(0));self.bind('<End>',lambda e:self.move(1))

    def get(self):return self.first,self.last

    def set(self,first,last):
        values=max(0.,min(1.,float(first))),max(0.,min(1.,float(last)))
        if values!=(self.first,self.last):self.first,self.last=values;self.redraw()

    def geometry_values(self):
        length=max(1,(self.winfo_height() if self.vertical else self.winfo_width())-4)
        span=max(0.,self.last-self.first);thumb=min(length,max(28,length*span))
        travel=length-thumb;start=2+travel*self.first/max(1e-9,1-span)
        return start,start+thumb,travel,span

    def capsule(self,name,start,end,inset,color):
        cross=self.winfo_width() if self.vertical else self.winfo_height()
        low=inset;high=cross-inset;r=max(0,min((high-low)/2,(end-start)/2))
        boxes=[(low,start+r,high,end-r),(low,start,high,start+2*r),(low,end-2*r,high,end)]
        for item,box in zip(self.parts[name],boxes):
            if not self.vertical:box=(box[1],box[0],box[3],box[2])
            self.coords(item,*box);self.itemconfigure(item,fill=color)

    def redraw(self):
        start,end,travel,span=self.geometry_values()
        length=self.winfo_height() if self.vertical else self.winfo_width()
        self.capsule('track',2,max(3,length-2),1,'#27313e' if self.focused else '#111820')
        color='#38434b' if span>=.99999 else GREEN if self.drag else '#b9a0f1' if self.hover else '#8976b8'
        self.capsule('thumb',start,end,3 if self.hover or self.drag else 4,color)

    def highlight(self,value):self.hover=value;self.redraw()
    def focus(self,value):self.focused=value;self.redraw()

    def move(self,fraction):
        if self.command:self.command('moveto',max(0.,min(1.,fraction)))
        return 'break'

    def scroll(self,amount,unit):
        if unit=='units':amount*=self.unit_step
        if self.command:self.command('scroll',amount,unit)
        return 'break'

    def press(self,event):
        self.focus_set();point=event.y if self.vertical else event.x
        start,end,travel,span=self.geometry_values()
        if span>=.99999:return 'break'
        if start<=point<=end:self.drag=(point,self.first);self.redraw()
        else:self.scroll(-1 if point<start else 1,'pages')
        return 'break'

    def motion(self,event):
        if self.drag:
            point=event.y if self.vertical else event.x
            _,_,travel,span=self.geometry_values()
            self.move(self.drag[1]+(point-self.drag[0])/max(1,travel)*(1-span))
        return 'break'

    def release(self,event):self.drag=None;self.redraw();return 'break'

    def wheel(self,event):
        if self.wheel_command:return self.wheel_command(event)
        return self.scroll(-3 if event.delta>0 else 3,'units')


def style_text_scrollbar(widget):
    """ScrolledText uses a classic scrollbar; replace only its presentation."""
    widget.vbar.destroy()
    widget.vbar=RoundedScrollbar(widget.frame,orient='vertical',command=widget.yview)
    widget.vbar.pack(side='right',fill='y')
    widget.configure(yscrollcommand=widget.vbar.set)
    return widget


def rounded_buttons(root, style):
    """Cirrus-inspired pill faces: soft elevation, ink hover and pressed depth."""
    images=[]
    def skin(fill, accent, focused=False, state='normal'):
        scale=4;size=44
        image=Image.new('RGBA',(size*scale,size*scale))
        down=state=='pressed'
        muted=state=='disabled'
        top=(6 if down else 3)*scale
        face=(4*scale,top,40*scale-1,(39 if down else 37)*scale-1)
        mask=Image.new('L',image.size)
        ImageDraw.Draw(mask).rounded_rectangle(face,radius=18*scale,fill=255)
        if not muted:
            shadow=Image.new('L',image.size)
            ImageDraw.Draw(shadow).rounded_rectangle(
                (face[0],top+3*scale,face[2],41*scale-1),radius=18*scale,fill=230)
            layer=Image.new('RGBA',image.size,(0,0,0,0))
            layer.putalpha(shadow.filter(ImageFilter.GaussianBlur(scale)))
            image.alpha_composite(layer)
            strength=95 if state=='active' or focused else 0
            if down:strength=40
            halo=mask.filter(ImageFilter.GaussianBlur(1.6*scale))
            halo=halo.point(lambda value:round(value*strength/255))
            layer=Image.new('RGBA',image.size,ImageColor.getrgb(accent)+(0,))
            layer.putalpha(halo)
            image.alpha_composite(layer)
        draw=ImageDraw.Draw(image)
        draw.rounded_rectangle(face,radius=18*scale,fill=fill,
                               outline=accent if focused or state=='active' else '#dce1e8' if not muted and not down else '#36404b',width=scale)
        if not muted:
            bevel=Image.new('RGBA',image.size)
            bevel_draw=ImageDraw.Draw(bevel)
            for y in range(top+scale,37*scale-1):
                relative=(y-top)/(34*scale)
                alpha=round(45*(1-relative)**3) if relative<.5 else round(24*relative**3)
                color=(255,255,255,alpha) if relative<.5 else (0,0,0,alpha)
                bevel_draw.line((4*scale,y,40*scale-1,y),fill=color)
            inner=Image.new('L',image.size)
            ImageDraw.Draw(inner).rounded_rectangle(
                (5*scale,top+scale,39*scale-1,36*scale-1),radius=17*scale,fill=255)
            image.alpha_composite(Image.composite(bevel,Image.new('RGBA',image.size),inner))
        draw=ImageDraw.Draw(image)
        if focused:
            draw.rounded_rectangle((7*scale,top+3*scale,37*scale-1,34*scale-1),
                                   radius=15*scale,outline=accent,width=scale)
        base=Image.new('RGBA',image.size,PANEL);base.alpha_composite(image)
        photo=ImageTk.PhotoImage(base.convert('RGB').resize((size,size),Image.Resampling.LANCZOS),master=root)
        images.append(photo)
        return photo

    def install(name, base, active, accent, menu=False):
        style.configure(name,background=PANEL)
        style.map(name,background=[('active',PANEL),('!active',PANEL)])
        style.configure(name,foreground='#161c25',arrowcolor='#161c25')
        style.map(name,foreground=[('disabled','#84909b'),('pressed',WHITE),('active',WHITE)],
                  arrowcolor=[('disabled','#84909b'),('pressed',WHITE),('active',WHITE)])
        normal=skin(base,accent)
        disabled=skin('#11171b','#35403c',state='disabled')
        hover=skin(active,accent,state='active')
        pressed=skin('#10151d',accent,state='pressed')
        focus=skin(base,accent,True)
        focus_hover=skin(active,accent,True,state='active')
        focus_pressed=skin('#10151d',accent,True,state='pressed')
        element='Rounded.'+name
        style.element_create(element,'image',normal,('disabled',disabled),
            ('pressed','focus',focus_pressed),('active','focus',focus_hover),
            ('pressed',pressed),('active',hover),('focus',focus),
            border=21,padding=0,sticky='nsew')
        prefix='Menubutton' if menu else 'Button'
        children=[]
        if menu:
            children.append((prefix+'.indicator',{'side':'right','sticky':''}))
        children.append((prefix+'.label',{'sticky':'nswe'}))
        style.layout(name,[(element,{'sticky':'nswe','children':[
            (prefix+'.padding',{'sticky':'nswe','children':children})]})])

    install('TButton','#f5f7fa','#252e3a',GREEN)
    install('TMenubutton','#f5f7fa','#252e3a',GREEN,menu=True)
    for name,color in COLORS.items():
        install(name+'.TButton','#f5f7fa','#252e3a',color)
    root._rounded_button_images=images


class PixelProgress(tk.Canvas):
    """Segmented game-style meter driven only by real task progress."""
    def __init__(self,parent):
        super().__init__(parent,width=220,height=24,bg=PANEL,highlightthickness=0)
        self.value=0.0
        self.bind('<Configure>',lambda event:self.redraw())

    def __setitem__(self,key,value):
        if key=='value':
            self.value=max(0.0,min(100.0,float(value)));self.redraw()
        else:super().__setitem__(key,value)

    def __getitem__(self,key):
        return self.value if key=='value' else super().__getitem__(key)

    def redraw(self):
        self.delete('all')
        width=max(220,self.winfo_width());left=50;right=width-3
        self.create_text(1,12,anchor='w',text=f'{self.value:.0f}%',font=('Consolas',-13,'bold'),fill=GREEN)
        self.create_rectangle(left,3,right,21,outline='#879583',fill=FIELD)
        cell=(right-left-6)/20
        for i in range(20):
            x=left+3+i*cell
            self.create_rectangle(x,6,x+cell-2,18,fill='#1e3024',outline='')
            amount=min(1,max(0,self.value/5-i))
            if amount:self.create_rectangle(x,6,x+(cell-2)*amount,18,fill=GREEN,outline='')


def build_ui(app):
    root=app.root
    families=tkfont.families(root)
    app.ui_font_family=next((f for f in families if f.casefold()=='mn khop khun'),'Segoe UI')
    app.ui_font=(app.ui_font_family,-20)
    small=(app.ui_font_family,-18)
    root.configure(bg=BG)
    backdrop=tk.Canvas(root,bg=BG,highlightthickness=0,takefocus=False)
    backdrop.place(x=0,y=0,relwidth=1,relheight=1)
    root.tk.call('lower',backdrop._w)
    backdrop.bind('<Configure>',lambda event:draw_workspace_grid(backdrop))
    app.workspace_background=backdrop
    root.option_add('*Font',app.ui_font)
    root.option_add('*TCombobox*Listbox.background',FIELD)
    root.option_add('*TCombobox*Listbox.foreground',WHITE)
    root.option_add('*TCombobox*Listbox.selectBackground',SELECT)
    root.option_add('*TCombobox*Listbox.selectForeground',WHITE)
    root.option_add('*TCombobox*Listbox.font',app.ui_font)
    for name in ('TkDefaultFont','TkTextFont','TkMenuFont','TkHeadingFont'):
        tkfont.nametofont(name,root=root).configure(family=app.ui_font_family,size=-20)
    style=ttk.Style(root);style.theme_use('clam')
    style.configure('.',font=app.ui_font,background=PANEL,foreground=WHITE)
    style.configure('TFrame',background=PANEL)
    style.configure('TLabel',background=PANEL,foreground=WHITE)
    style.configure('TButton',background='#131e25',foreground=WHITE,padding=(8,5),borderwidth=1)
    style.map('TButton',background=[('pressed','#35462b'),('active','#202e23')],foreground=[('disabled','#7b877d')])
    style.configure('TCheckbutton',background=PANEL,foreground=WHITE,indicatorbackground=FIELD)
    style.map('TCheckbutton',background=[('active','#1a2b20')],indicatorbackground=[('selected',GREEN)])
    style.configure('TCombobox',fieldbackground=FIELD,background='#17232b',foreground=WHITE,arrowcolor=GREEN,padding=4)
    style.map('TCombobox',fieldbackground=[('readonly',FIELD)],foreground=[('readonly',WHITE)],selectbackground=[('readonly',SELECT)],selectforeground=[('readonly',WHITE)])
    style.configure('TEntry',fieldbackground=FIELD,foreground=WHITE,insertcolor=WHITE)
    style.configure('TSpinbox',fieldbackground=FIELD,background='#17232b',foreground=WHITE,arrowcolor=GREEN,insertcolor=WHITE)
    style.configure('TRadiobutton',background=PANEL,foreground=WHITE)
    style.map('TRadiobutton',background=[('active','#1a2b20')])
    style.configure('TMenubutton',background='#17232b',foreground=WHITE,arrowcolor=GREEN,padding=5)
    style.configure('Treeview',background=FIELD,fieldbackground=FIELD,foreground=WHITE,rowheight=28,font=small)
    style.configure('Treeview.Heading',background='#18242d',foreground=PURPLE,font=small)
    style.map('Treeview',background=[('selected',SELECT)],foreground=[('selected',WHITE)])
    style.configure('TScrollbar',background='#283633',troughcolor=FIELD,arrowcolor=GREEN,bordercolor=PANEL)
    style.configure('Horizontal.TProgressbar',background=COLORS['status'],troughcolor=FIELD,bordercolor=PANEL)
    for name,color in COLORS.items():
        base='#121d18' if color==GREEN else '#191624'
        active='#223722' if color==GREEN else '#2d2341'
        style.configure(name+'.TButton',bordercolor='#3b4c45',lightcolor='#3b4c45',darkcolor='#3b4c45',background=base,foreground=WHITE)
        style.map(name+'.TButton',background=[('pressed',active),('active',active)],foreground=[('active',color)],bordercolor=[('active',color),('!active','#3b4c45')])
    style.configure('editor.TButton',padding=(8,3))
    rounded_buttons(root,style)
    app.ui_sections={};app.ui_buttons={}

    def panel(parent,name):
        frame=tk.Frame(parent,bg=PANEL,highlightthickness=1,highlightbackground='#48515a',highlightcolor=COLORS[name],padx=9,pady=7)
        for flipx,flipy in ((False,False),(True,False),(False,True),(True,True)):
            corner=tk.Canvas(frame,width=14,height=14,bg=PANEL,highlightthickness=0)
            corner.place(relx=1 if flipx else 0,rely=1 if flipy else 0,anchor=('s' if flipy else 'n')+('e' if flipx else 'w'))
            points=[(1,12),(1,5),(5,5),(5,1),(12,1)]
            coords=[v for x,y in points for v in (13-x if flipx else x,13-y if flipy else y)]
            corner.create_line(*coords,fill=COLORS[name],width=2)
        app.ui_sections[name]=frame
        return frame

    def button(parent,label,command,kind='editor',key=None,**kwargs):
        widget=ttk.Button(parent,text=label,command=command,style=kind+'.TButton',**kwargs)
        if key:app.ui_buttons[key]=widget
        return widget

    def text_widget(parent,height=3):
        return style_text_scrollbar(scrolledtext.ScrolledText(parent,height=height,width=1,wrap='word',undo=True,
            font=app.ui_font,bg=FIELD,fg=WHITE,insertbackground=WHITE,selectbackground=SELECT,
            selectforeground=WHITE,relief='flat',padx=6,pady=4))

    root.columnconfigure(0,weight=70,uniform='workspace')
    root.columnconfigure(1,weight=30,uniform='workspace')
    root.rowconfigure(2,weight=1)
    files=panel(root,'files');files.grid(row=0,column=0,sticky='nsew',padx=(12,6),pady=(12,6))
    ttk.Label(files,text='MANHWA  //  PROJECT FILES',font=('Consolas',-12,'bold'),foreground=GREEN).grid(row=0,column=0,columnspan=3,sticky='w',padx=4,pady=(0,3))
    hearts=tk.Canvas(files,width=78,height=15,bg=PANEL,highlightthickness=0)
    hearts.grid(row=0,column=2,sticky='e',padx=4)
    for index,color in enumerate((GREEN,GREEN,PURPLE,WHITE)):
        for y,row in enumerate(('0110110','1111111','1111111','0111110','0011100','0001000')):
            for x,pixel in enumerate(row):
                if pixel=='1':hearts.create_rectangle(index*20+x*2,y*2,index*20+x*2+2,y*2+2,fill=color,outline='')
    for col,(label,cmd,key) in enumerate((('เพิ่มภาพ',app.add_images,'add_images'),('เพิ่มโฟลเดอร์',app.add_folder,'add_folder'),('เปิดโปรเจกต์',app.open_project,'open_project'))):
        files.columnconfigure(col,weight=1,uniform='files')
        button(files,label,cmd,'files',key).grid(row=1,column=col,sticky='nsew',padx=3,ipady=4)
    batch=panel(root,'batch');batch.grid(row=0,column=1,sticky='nsew',padx=(6,12),pady=(12,6))
    ttk.Label(batch,text='MAIN QUEST  //  OCR > MASK > CLEAN',font=('Consolas',-12,'bold'),foreground=GREEN).grid(row=0,column=0,columnspan=3,sticky='w',padx=4,pady=(0,3))
    for col,(label,cmd,key) in enumerate((('OCR ทุกหน้า',lambda:app.start_scan(True),'ocr_all'),
        ('สร้าง Mask ทุกหน้า',lambda:app.prepare_clean_masks(True),'mask_all'),
        ('Clean ทุกหน้า',lambda:app.start_clean(True),'clean_all'))):
        batch.columnconfigure(col,weight=1,uniform='batch')
        button(batch,label,cmd,'batch',key).grid(row=1,column=col,sticky='nsew',padx=3,ipady=4)

    options=tk.Frame(root,bg=BG);options.grid(row=1,column=0,sticky='nsew',padx=(12,6),pady=6)
    options.columnconfigure(0,weight=1)
    settings=panel(options,'settings');settings.grid(row=0,column=0,sticky='nsew',padx=(0,8))
    ttk.Label(settings,text='OCR').grid(row=0,column=0,padx=(0,6))
    ttk.Combobox(settings,textvariable=app.mode,values=['auto','easy','paddle','rapid'],width=8,state='readonly').grid(row=0,column=1,sticky='ew')
    ttk.Label(settings,text='ภาษา').grid(row=0,column=2,padx=8)
    ttk.Combobox(settings,textvariable=app.lang,values=['ko+en','en','zh+en','zh_tw+en'],width=9,state='readonly').grid(row=0,column=3,sticky='ew')
    ttk.Checkbutton(settings,text='เร่ง GPU',variable=app.gpu).grid(row=0,column=4,padx=(10,2))
    ttk.Checkbutton(settings,text='AI กรอบ 12 คลาส',variable=app.type_scan).grid(row=1,column=4,sticky='w')
    settings.columnconfigure(1,weight=1);settings.columnconfigure(3,weight=1)
    stop=panel(options,'stop');stop.grid(row=0,column=1,sticky='nsew')
    button(stop,'หยุดการทำงาน',app.stop,'stop','stop').pack(fill='both',expand=True)
    types=panel(root,'text');types.grid(row=1,column=1,sticky='nsew',padx=(6,12),pady=6)
    for col,(label,cmd,key) in enumerate((('เพิ่ม Text / รับคำแปล',app.add_text,'add_text'),('ส่งออกเป็น PSD',app.psd_menu,'psd'))):
        types.columnconfigure(col,weight=1,uniform='type')
        button(types,label,cmd,'text',key).grid(row=0,column=col,sticky='nsew',padx=3)

    app.work_split=tk.PanedWindow(root,orient='horizontal',bg='#48515a',bd=0,
        sashwidth=8,sashrelief='raised',opaqueresize=True)
    app.work_split.grid(row=2,column=0,columnspan=2,sticky='nsew',padx=12,pady=6)
    left=tk.Frame(app.work_split,bg=BG);right=tk.Frame(app.work_split,bg=BG)
    left.columnconfigure(0,weight=1);left.rowconfigure(0,weight=1)
    right.columnconfigure(1,weight=1);right.rowconfigure(0,weight=1)
    app.work_split.add(left,minsize=320,stretch='always')
    app.work_split.add(right,minsize=460,stretch='always')
    app.workspace_ratio=.70
    def arrange_split(event=None):
        def place():
            width=app.work_split.winfo_width()
            if width>1:app.work_split.sash_place(0,round((width-8)*app.workspace_ratio),0)
        app.work_split.after_idle(place)
    def remember_split(event=None):
        app.workspace_ratio=app.work_split.sash_coord(0)[0]/max(1,app.work_split.winfo_width()-8)
    def reset_split(event=None):
        app.workspace_ratio=.70;arrange_split()
    app.work_split.bind('<Configure>',arrange_split)
    app.work_split.bind('<ButtonRelease-1>',remember_split)
    app.work_split.bind('<Double-Button-1>',reset_split)
    viewer=panel(left,'image');viewer.grid(row=0,column=0,sticky='nsew',padx=(0,5))
    viewer.columnconfigure(0,weight=1);viewer.rowconfigure(0,weight=1)
    app.canvas=tk.Canvas(viewer,bg=FIELD,highlightthickness=0,width=1,height=1,xscrollincrement=1,yscrollincrement=1)
    vs=RoundedScrollbar(viewer,orient='vertical',command=app.scroll_y,wheel=app.wheel,unit_step=20)
    hs=RoundedScrollbar(viewer,orient='horizontal',command=app.scroll_x,unit_step=20)
    app.canvas.configure(yscrollcommand=vs.set,xscrollcommand=hs.set)
    app.canvas.grid(row=0,column=0,sticky='nsew');vs.grid(row=0,column=1,sticky='ns');hs.grid(row=1,column=0,sticky='ew')
    app.canvas.bind('<Configure>',app.resize_view)
    app.canvas.bind('<MouseWheel>',app.wheel)
    app.canvas.bind('<Button-4>',lambda event:app.scroll_y('scroll',-60,'units'))
    app.canvas.bind('<Button-5>',lambda event:app.scroll_y('scroll',60,'units'))
    app.canvas.bind('<ButtonPress-1>',app.canvas_down);app.canvas.bind('<B1-Motion>',app.canvas_drag);app.canvas.bind('<ButtonRelease-1>',app.canvas_up)
    app.canvas.bind('<ButtonPress-3>',app.cancel_active_drag)
    app.canvas.bind('<Motion>',app.canvas_hover)

    nav=panel(left,'pages');nav.grid(row=1,column=0,sticky='ew',padx=(0,5),pady=(2,0))
    nav.columnconfigure(1,weight=1)
    button(nav,'‹',lambda:app.navigate(-1),'pages','previous',width=3).grid(row=0,column=0)
    app.page_combo=ttk.Combobox(nav,state='readonly',width=10)
    app.page_combo.grid(row=0,column=1,sticky='ew',padx=8)
    app.page_combo.bind('<<ComboboxSelected>>',lambda event:app.select_page(app.page_combo.current()))
    button(nav,'›',lambda:app.navigate(1),'pages','next',width=3).grid(row=0,column=2)
    app.page_note=ttk.Label(nav,text='',font=small,wraplength=800)
    app.page_note.grid(row=1,column=0,columnspan=3,sticky='w')
    nav.bind('<Configure>',lambda event:app.page_note.configure(wraplength=max(100,event.width-24)))

    inspect=panel(right,'inspect');inspect.grid(row=0,column=0,sticky='new',padx=5)
    ttk.Label(inspect,text='ตรวจภาพ',foreground=GREEN,font=(app.ui_font_family,-22)).pack(anchor='w',pady=(0,4))
    for label,var,cmd in (('ลากเพิ่มกรอบ',app.draw_mode,lambda:(app.edit_frame.set(False),app.render())),('ดู Mask',app.show_masks,app.refresh_clean_view),('ดูผล Clean',app.show_cleaned,app.refresh_clean_view),('ดู Text',app.show_text,app.render)):
        ttk.Checkbutton(inspect,text=label,variable=var,command=cmd).pack(fill='x',pady=2)
    snap_var=getattr(app,'snap_guides',None)
    if snap_var is None:
        snap_var=tk.BooleanVar(master=root,value=True)
        app.snap_guides=snap_var
    ttk.Checkbutton(inspect,text='ล็อกเส้นไกด์ (Snap)',variable=snap_var).pack(fill='x',pady=2)
    ttk.Checkbutton(inspect,text='ปรับกรอบเอียง',variable=app.edit_frame,command=app.toggle_frame_edit).pack(fill='x',pady=2)
    button(inspect,'แก้ Mask กรอบนี้',app.open_clean_editor,'inspect').pack(fill='x',pady=2)
    button(inspect,'หมุนกรอบ…',app.rotate_frame,'inspect').pack(fill='x',pady=2)
    root.bind('<Control-m>',lambda e:app.open_clean_editor() if not isinstance(e.widget,tk.Text) else None)
    ttk.Separator(inspect).pack(fill='x',pady=5)
    zoom=ttk.Frame(inspect);zoom.pack(fill='x')
    button(zoom,'-',lambda:app.zoom(.8),'inspect',width=2).pack(side='left',fill='x',expand=True)
    button(zoom,'+',lambda:app.zoom(1.25),'inspect',width=2).pack(side='left',fill='x',expand=True)
    app.zoom_note=tk.StringVar(value='56%')
    ttk.Label(inspect,textvariable=app.zoom_note,anchor='center').pack(fill='x',pady=2)
    button(inspect,'ซูม 56%',app.fit,'inspect','reading_view').pack(fill='x',pady=2)
    button(inspect,'พอดีความกว้าง',lambda:app.fit('width'),'inspect').pack(fill='x',pady=2)
    button(inspect,'SmartStitch',getattr(app,'open_smart_stitch_dialog',lambda:None),'inspect','smart_stitch').pack(fill='x',pady=3)
    more=ttk.Menubutton(inspect,text='เมนูเพิ่มเติม');more.pack(fill='x',pady=(3,2))
    menu=tk.Menu(more,tearoff=False,bg=PANEL,fg=WHITE,font=small);more.configure(menu=menu)
    menu.add_command(label='บันทึกโปรเจกต์ (.json)   Ctrl+S',command=app.save_project)
    menu.add_command(label='ส่งออกเป็นไฟล์ .mtp',command=getattr(app,'export_mtp_project',lambda:None))
    menu.add_command(label='CLEAN / แก้มาสก์รายกรอบ',command=app.clean_menu)
    menu.add_command(label='ตรวจความพร้อม LaMa',command=app.check_clean_readiness)
    menu.add_command(label='โหลด/ตรวจ AI จับทรงบับเบิ้ล (ONNX)',command=app.prepare_bubble_seg_ai)
    menu.add_command(label='เลือก Google credential JSON',command=app.choose_credential)
    menu.add_separator()
    menu.add_command(label='ส่งออก TXT',command=app.export)
    menu.add_command(label='คัดลอกข้อความทั้งหมด',command=app.copy_output)
    menu.add_command(label='วิธีใช้',command=app.help)
    menu.add_separator()
    menu.add_command(label='เสียงเอฟเฟกต์: ไม่เกิน 15%',state='disabled')

    editor=panel(right,'editor');editor.grid(row=0,column=1,sticky='nsew',padx=(5,0))
    editor_head=ttk.Frame(editor);editor_head.pack(fill='x',pady=(0,2))
    ttk.Label(editor_head,text='2  ตรวจและแก้ไขข้อความ OCR',foreground=PURPLE,font=(app.ui_font_family,-22)).pack(side='left')
    button(editor_head,'RapidOCR กรอบนี้',app.rapid_bubble,'text').pack(side='right',padx=(6,0))
    candidates=ttk.Frame(editor);candidates.pack(side='bottom',fill='x',pady=(5,0))
    ttk.Label(candidates,text='ผลอ่านทางเลือก',font=small).pack(anchor='w')
    app.candidate_combo=ttk.Combobox(candidates,state='readonly');app.candidate_combo.pack(fill='x',pady=3)
    app.candidate_button=button(candidates,'ใช้ข้อความทางเลือก',app.use_candidate)
    app.candidate_button.pack(fill='x')
    body=ttk.Frame(editor);body.pack(fill='both',expand=True)
    body.columnconfigure(0,weight=1);body.rowconfigure(0,weight=1)
    scroller=tk.Canvas(body,bg=PANEL,highlightthickness=0,width=1,height=1)
    scrollbar=RoundedScrollbar(body,orient='vertical',command=scroller.yview)
    scroller.configure(yscrollcommand=scrollbar.set)
    scroller.grid(row=0,column=0,sticky='nsew');scrollbar.grid(row=0,column=1,sticky='ns')
    middle=ttk.Frame(scroller);window=scroller.create_window((0,0),window=middle,anchor='nw')
    def resize_editor(event=None):
        width=max(1,scroller.winfo_width());height=max(middle.winfo_reqheight(),scroller.winfo_height())
        scroller.itemconfigure(window,width=width,height=height)
        scroller.configure(scrollregion=(0,0,width,height))
    scroller.bind('<Configure>',resize_editor);middle.bind('<Configure>',resize_editor)
    app.tree=ttk.Treeview(middle,columns=('n','text','score'),show='headings',height=3,selectmode='extended')
    for col,title,width in (('n','#',35),('text','คำพูด',220),('score','ตรวจ',60)):
        app.tree.heading(col,text=title);app.tree.column(col,width=width,minwidth=30,stretch=col=='text')
    app.tree.tag_configure('low',foreground='#ffc983')
    app.tree.tag_configure('suspect',foreground='#ff7676')
    app.tree.pack(fill='both',expand=True,pady=(4,6));app.tree.bind('<<TreeviewSelect>>',app.select_bubble)
    actions=ttk.Frame(middle);actions.pack(fill='x');action_buttons=[]
    for index,(label,cmd) in enumerate((('รวมที่เลือก',app.merge),('แยกบรรทัด',app.split),('ลบ',app.delete),('↑',lambda:app.move(-1)),('↓',lambda:app.move(1)),('ย้อนกลับ',app.undo))):
        action_buttons.append(button(actions,label,cmd))
    action_columns=[0]
    def fit_actions(event):
        columns=6 if event.width>=500 else 3
        if columns==action_columns[0]:return
        action_columns[0]=columns
        for col in range(6):actions.columnconfigure(col,weight=1 if col<columns else 0,uniform='edit' if col<columns else '')
        for i,widget in enumerate(action_buttons):widget.grid(row=i//columns,column=i%columns,sticky='ew',padx=2,pady=2)
        scroller.after_idle(resize_editor)
    actions.bind('<Configure>',fit_actions)

    domino=ttk.Frame(middle);domino.pack(fill='x',pady=(3,0))
    domino.columnconfigure(0,weight=1,uniform='domino');domino.columnconfigure(1,weight=1,uniform='domino')
    button(domino,'เลื่อนคำแปล ↓',lambda:getattr(app,'shift_translations',lambda d:None)(1),'text').grid(row=0,column=0,sticky='ew',padx=2,pady=1)
    button(domino,'เลื่อนคำแปล ↑',lambda:getattr(app,'shift_translations',lambda d:None)(-1),'text').grid(row=0,column=1,sticky='ew',padx=2,pady=1)
    button(domino,'สลับคำแปล ⇄',getattr(app,'swap_translations',lambda:None),'editor').grid(row=1,column=0,sticky='ew',padx=2,pady=1)
    button(domino,'ลบกรอบ+คืนภาพ',getattr(app,'delete_and_restore_art',lambda:None),'stop').grid(row=1,column=1,sticky='ew',padx=2,pady=1)

    app.bubble_note=ttk.Label(middle,text='เลือกกรอบในภาพหรือรายการ (Ctrl+คลิกเลือกหลายกรอบได้)',font=small,wraplength=420)
    app.bubble_note.pack(fill='x',pady=(6,0))
    middle.bind('<Configure>',lambda event:app.bubble_note.configure(wraplength=max(100,event.width-12)),add='+')
    ttk.Label(middle,text='คำต้นฉบับ • Enter เพื่อแยกบรรทัด',font=small).pack(anchor='w')
    app.source=text_widget(middle,2);app.source.pack(fill='both',expand=True,pady=3)
    thai_head=ttk.Frame(middle);thai_head.pack(fill='x')
    ttk.Label(thai_head,text='คำแปลไทย • Enter แบ่งบรรทัดเองได้',font=small).pack(side='left')
    button(thai_head,'อัปเดตบรรทัด/คำแปล',app.apply_editor_text_now,'text').pack(side='right')
    app.thai=text_widget(middle,6);app.thai.pack(fill='both',expand=True,pady=3)
    app.thai.bind('<Control-Return>',lambda e:(app.apply_editor_text_now(),'break')[1])
    app.source.bind('<<Modified>>',app.edited);app.thai.bind('<<Modified>>',app.edited)
    app.reviewed=tk.BooleanVar(value=False)
    ttk.Checkbutton(middle,text='ตรวจข้อความนี้แล้ว',variable=app.reviewed,command=app.mark_reviewed).pack(anchor='w',pady=2)

    font_box=ttk.Frame(middle);font_box.pack(fill='x',pady=(4,6))
    font_box.columnconfigure(0,weight=1);font_box.columnconfigure(1,weight=1)
    ttk.Label(font_box,text='ปุ่มขอบกรอบ: บนฟ้า=ย้ายล็อก • มุมขวาบนส้ม=หมุน • ขวาเขียว=บรรทัด • มุมล่างม่วง=ขนาด',foreground=PURPLE,font=small).grid(row=0,column=0,columnspan=2,sticky='w')
    font_pick=ttk.Frame(font_box);font_pick.grid(row=1,column=0,columnspan=2,sticky='ew',pady=2)
    font_pick.columnconfigure(0,weight=1)
    app.font_combo=ttk.Combobox(font_pick,textvariable=app.selected_font,values=FONT_CHOICES,state='readonly')
    app.font_combo.grid(row=0,column=0,sticky='ew',padx=(0,4))
    button(font_pick,'เลือกทั้งหน้า',app.select_all_bubbles,'editor').grid(row=0,column=1,sticky='ew')
    button(font_box,'เปลี่ยนฟอนต์ที่เลือก',lambda:app.apply_font_to_bubbles(False),'text').grid(row=2,column=0,sticky='ew',padx=(0,2),pady=2)
    button(font_box,'เปลี่ยนทั้งหน้า',lambda:app.apply_font_to_bubbles(True),'text').grid(row=2,column=1,sticky='ew',padx=(2,0),pady=2)

    size_row=ttk.Frame(font_box);size_row.grid(row=3,column=0,columnspan=2,sticky='ew',pady=(4,2))
    ttk.Label(size_row,text='ขนาด:',font=small).pack(side='left',padx=(0,4))
    button(size_row,'−',lambda:app.step_font_size(-2),'text',width=3).pack(side='left',padx=1)
    sz_spin=ttk.Spinbox(size_row,from_=6,to=180,textvariable=app.font_size_var,width=4)
    sz_spin.pack(side='left',padx=3)
    sz_spin.bind('<Return>',lambda e:app.apply_size_to_bubbles())
    button(size_row,'+',lambda:app.step_font_size(2),'text',width=3).pack(side='left',padx=1)
    button(size_row,'ใช้ขนาด',app.apply_size_to_bubbles,'text').pack(side='left',padx=4)
    button(size_row,'ออโต้',app.reset_text_auto,'editor').pack(side='left',fill='x',expand=True)

    wrap_row=ttk.Frame(font_box);wrap_row.grid(row=4,column=0,columnspan=2,sticky='ew',pady=2)
    wrap_row.columnconfigure(0,weight=1);wrap_row.columnconfigure(1,weight=1)
    button(wrap_row,'บีบบรรทัด (แคบลง)',lambda:app.step_wrap_width(0.82),'editor').grid(row=0,column=0,sticky='ew',padx=(0,2))
    button(wrap_row,'ขยายบรรทัด (กว้างขึ้น)',lambda:app.step_wrap_width(1.22),'editor').grid(row=0,column=1,sticky='ew',padx=(2,0))

    preset_names=list(getattr(app,'font_presets_map',None) or ['1. มังฮวาเกาหลี','2. การ์ตูนต่อสู้'])
    preset_var=getattr(app,'font_preset',None)
    if preset_var is None:
        preset_var=tk.StringVar(master=root,value=preset_names[0])
        app.font_preset=preset_var
    ttk.Separator(font_box).grid(row=5,column=0,columnspan=2,sticky='ew',pady=(6,4))
    preset_head=ttk.Frame(font_box);preset_head.grid(row=6,column=0,columnspan=2,sticky='ew')
    ttk.Label(preset_head,text='โปรไฟล์ฟอนต์ตามประเภทกรอบ (กดปุ่มเพื่อจัด)',foreground=GREEN,font=small).pack(side='left')
    button(preset_head,'ตั้งค่าชุดฟอนต์…',getattr(app,'open_preset_dialog',lambda:None),'editor').pack(side='right')
    app.preset_combo=ttk.Combobox(font_box,textvariable=preset_var,values=preset_names,state='readonly')
    app.preset_combo.grid(row=7,column=0,columnspan=2,sticky='ew',pady=2)
    app.auto_stroke=tk.BooleanVar(root,value=False)
    app.auto_gradient=tk.BooleanVar(root,value=False)
    auto_row=ttk.Frame(font_box)
    auto_row.grid(row=10,column=0,columnspan=2,sticky='ew')
    ttk.Checkbutton(auto_row,text='Auto Stroke',variable=app.auto_stroke).pack(side='left')
    ttk.Checkbutton(auto_row,text='Auto Gradient',variable=app.auto_gradient).pack(side='left')
    button(font_box,'ดูดสีกรอบที่เลือก',lambda:sample_page_effects(app,False),'text').grid(row=11,column=0,sticky='ew')
    button(font_box,'ดูดสีทั้งหน้า',lambda:sample_page_effects(app,True),'text').grid(row=11,column=1,sticky='ew')
    button(font_box,'แก้เอฟเฟกต์กรอบที่เลือก',lambda:edit_text_effects(app),'text').grid(row=12,column=0,columnspan=2,sticky='ew')
    quick=ttk.Frame(font_box);quick.grid(row=9,column=0,columnspan=2,sticky='ew',pady=3)
    for col,(caption,category) in enumerate(zip(('พูด','ตะโกน','คิด','ระบบ','บรรยาย','ลอย'),
                                               ('normal','shout','thought','system','narration','floating'))):
        quick.columnconfigure(col,weight=1)
        ttk.Button(quick,text=caption,width=4,command=lambda k=category:app.apply_preset_fonts(quick_type=k)).grid(row=0,column=col,sticky='ew')
    button(font_box,'จัดฟอนต์ตามกรอบ (หน้านี้)',lambda:app.apply_preset_fonts(False),'text').grid(row=8,column=0,sticky='ew',padx=(0,2),pady=2)
    button(font_box,'จัดฟอนต์ตามกรอบ (ทุกหน้า)',lambda:app.apply_preset_fonts(True),'text').grid(row=8,column=1,sticky='ew',padx=(2,0),pady=2)

    reread=ttk.Frame(middle);reread.pack(fill='x')
    reread.columnconfigure(0,weight=1);reread.columnconfigure(1,weight=1)
    reread_buttons=[]
    for i,(label,cmd) in enumerate((('อ่านกรอบนี้ซ้ำในเครื่อง',app.retry_bubble),('ใช้ Google เฉพาะกรอบนี้',app.google_bubble))):
        reread_buttons.append(button(reread,label,cmd))
    def fit_reread(event):
        columns=2 if event.width>=480 else 1
        for i,widget in enumerate(reread_buttons):
            widget.grid(row=i//columns,column=i%columns,sticky='ew',padx=2,pady=2)
        reread.columnconfigure(1,weight=1 if columns==2 else 0)
    reread.bind('<Configure>',fit_reread)

    app.output=tk.Text(root,state='disabled',font=app.ui_font,bg=FIELD,fg=WHITE)
    status=panel(root,'status');status.grid(row=4,column=0,columnspan=2,sticky='ew',padx=12,pady=(6,10))
    status.columnconfigure(0,weight=1)
    status_label=ttk.Label(status,textvariable=app.status,font=small,wraplength=1300)
    status_label.grid(row=0,column=0,sticky='w')
    app.progress=PixelProgress(status)
    app.progress.grid(row=0,column=1,sticky='e',padx=(8,0))
    status.bind('<Configure>',lambda event:status_label.configure(wraplength=max(200,event.width-245)))
    app.log_text=tk.Text(status,height=1,width=1,wrap='word',state='disabled',font=small,
        bg=FIELD,fg=WHITE,insertbackground=WHITE,selectbackground=SELECT,selectforeground=WHITE,
        relief='flat',padx=6,pady=4)
    app.log_text.grid(row=1,column=0,columnspan=2,sticky='ew',pady=(4,0))
    if app.ui_font_family!='MN Khop Khun':
        app.log('ไม่พบ MN Khop Khun — ใช้ Segoe UI ชั่วคราว ติดตั้งฟอนต์แล้วเปิดโปรแกรมใหม่')

def sample_page_effects(app, entire_page):
    from tkinter import messagebox
    from bubble_cleaner import compose_page
    from text_style_sampler import sample_style
    if not app.guard() or not app.page():
        return
    keys = [k for k, var in [('stroke', app.auto_stroke), ('gradient', app.auto_gradient)] if var.get()]
    targets = app.page().bubbles if entire_page else app._selected_page_bubbles()
    targets = [b for b in targets if b.text_layer]
    if not keys or not targets:
        messagebox.showinfo('ดูดสี', 'เปิด Auto Stroke หรือ Auto Gradient แล้วเลือกกรอบที่จัด Text แล้ว')
        return
    patches = app.page().clean_patches
    cleaned = compose_page(app.image, patches) if patches else None
    changes = [(b, sample_style(app.image, cleaned, b)) for b in targets]
    app.snapshot()
    for bubble, style in changes:
        bubble.text_layer.update({k:style[k] for k in keys})
    app.dirty = True
    app.show_text.set(True)
    app.refresh_clean_view()


def edit_text_effects(app):
    from tkinter import colorchooser, messagebox
    from text_style_sampler import effects
    if not app.guard():
        return
    selected = app._selected_page_bubbles()
    if len(selected) != 1 or not selected[0].text_layer:
        messagebox.showinfo('แก้เอฟเฟกต์', 'เลือกหนึ่งกรอบที่จัด Text แล้ว')
        return
    bubble = selected[0]
    style = effects(bubble.text_layer)
    popup = tk.Toplevel(app.root)
    popup.title('เอฟเฟกต์ข้อความ')
    popup.transient(app.root)
    popup.grab_set()
    stroke = tk.BooleanVar(popup,style['stroke']['enabled'])
    gradient = tk.BooleanVar(popup,style['gradient']['enabled'])
    size = tk.StringVar(popup,str(style['stroke']['size']))
    angle = tk.StringVar(popup,str(style['gradient']['angle']))
    def swatch(row,col,label,key,field):
        color = '#%02x%02x%02x' % tuple(style[key][field])
        def choose():
            chosen = colorchooser.askcolor(parent=popup,color=btn.cget('background'))[1]
            if chosen:
                style[key][field] = list(ImageColor.getrgb(chosen))
                btn.configure(background=chosen)
        btn = tk.Button(popup,text=label,background=color,command=choose)
        btn.configure(foreground='white' if sum(style[key][field])<382 else 'black')
        btn.grid(row=row,column=col,padx=4,pady=4)
    ttk.Checkbutton(popup,text='Stroke',variable=stroke).grid(row=0,column=0)
    swatch(0,1,'สีขอบ','stroke','color')
    ttk.Combobox(popup,state='readonly',width=5,textvariable=size,values=tuple(range(1,9))).grid(row=0,column=2)
    ttk.Checkbutton(popup,text='Gradient Overlay',variable=gradient).grid(row=1,column=0)
    swatch(1,1,'สีเริ่ม','gradient','start_color')
    swatch(1,2,'สีจบ','gradient','end_color')
    ttk.Label(popup,text='มุม (90° แนวตั้ง / 0° แนวนอน)').grid(row=2,column=0,columnspan=2)
    ttk.Combobox(popup,state='readonly',width=5,textvariable=angle,values=('90','0')).grid(row=2,column=2)
    def save(clear=False):
        app.snapshot()
        style['stroke'].update(enabled=stroke.get() and not clear,size=int(size.get()))
        style['gradient'].update(enabled=gradient.get() and not clear,angle=int(angle.get()))
        bubble.text_layer.update(style,manual_effects=True)
        app.dirty = True
        app.show_text.set(True)
        app.refresh_clean_view()
        popup.destroy()
    ttk.Button(popup,text='ล้างเอฟเฟกต์',command=lambda:save(True)).grid(row=3,column=0,pady=6)
    ttk.Button(popup,text='บันทึก',command=save).grid(row=3,column=2,pady=6)
