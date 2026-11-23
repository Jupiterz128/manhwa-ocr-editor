"""Windows Uniscribe text measurement/rendering, including Thai mark shaping."""
import ctypes as c
from ctypes import wintypes as w
import os
from PIL import Image

COMPONENT_VERSION='3.1'


class SIZE(c.Structure):
    _fields_=[('cx',w.LONG),('cy',w.LONG)]


class BITMAPINFOHEADER(c.Structure):
    _fields_=[('size',w.DWORD),('width',w.LONG),('height',w.LONG),('planes',w.WORD),
              ('bits',w.WORD),('compression',w.DWORD),('image_size',w.DWORD),
              ('x',w.LONG),('y',w.LONG),('used',w.DWORD),('important',w.DWORD)]


class TextRenderer:
    def __init__(self,size,font_path,family='TH Sarabun New',weight=700,italic=0):
        if os.name!='nt':raise RuntimeError('การจัดอักษรไทยรุ่นนี้ใช้ Windows Uniscribe')
        self.size=int(size);self.cache={};self.dc=None;self.font=None;self.oldfont=None
        self.g=c.WinDLL('gdi32');self.u=c.WinDLL('usp10')
        def bind(dll,name,args,result):
            f=getattr(dll,name);f.argtypes=args;f.restype=result;return f
        ptr=c.c_void_p;integer=c.c_int
        bind(self.g,'CreateCompatibleDC',[ptr],ptr)
        bind(self.g,'DeleteDC',[ptr],w.BOOL)
        bind(self.g,'AddFontResourceExW',[w.LPCWSTR,w.DWORD,ptr],integer)
        bind(self.g,'RemoveFontResourceExW',[w.LPCWSTR,w.DWORD,ptr],w.BOOL)
        bind(self.g,'CreateFontW',[integer]*5+[w.DWORD]*8+[w.LPCWSTR],ptr)
        bind(self.g,'SelectObject',[ptr,ptr],ptr)
        bind(self.g,'DeleteObject',[ptr],w.BOOL)
        bind(self.g,'SetBkMode',[ptr,integer],integer)
        bind(self.g,'SetTextColor',[ptr,w.DWORD],w.DWORD)
        bind(self.g,'CreateDIBSection',[ptr,ptr,w.UINT,c.POINTER(ptr),ptr,w.DWORD],ptr)
        bind(self.g,'GdiFlush',[],w.BOOL)
        bind(self.u,'ScriptStringAnalyse',[ptr,ptr,integer,integer,integer,w.DWORD,integer]+[ptr]*6,w.LONG)
        bind(self.u,'ScriptString_pSize',[ptr],c.POINTER(SIZE))
        bind(self.u,'ScriptStringOut',[ptr,integer,integer,w.UINT,ptr,integer,integer,w.BOOL],w.LONG)
        bind(self.u,'ScriptStringFree',[c.POINTER(ptr)],w.LONG)
        self.font_path=str(font_path)
        self.added=self.g.AddFontResourceExW(self.font_path,0x10,None)
        try:
            self.dc=self.g.CreateCompatibleDC(None)
            self.font=self.g.CreateFontW(-self.size,0,0,0,int(weight),1 if italic else 0,0,0,1,0,0,4,0,family)
            if not self.dc or not self.font:raise RuntimeError('สร้างตัววาดฟอนต์ไม่สำเร็จ')
            self.oldfont=self.g.SelectObject(self.dc,self.font)
            self.g.SetBkMode(self.dc,1);self.g.SetTextColor(self.dc,0)
        except Exception:self.close();raise

    def analyse(self,text):
        buffer=c.create_unicode_buffer(text or ' ')
        length=len((text or ' ').encode('utf-16-le'))//2
        analysis=c.c_void_p()
        result=self.u.ScriptStringAnalyse(self.dc,buffer,length,length*2+16,-1,0x80,0,
            None,None,None,None,None,c.byref(analysis))
        if result!=0:raise RuntimeError(f'ฟอนต์ไม่รองรับข้อความ หรือจัดอักษรไม่สำเร็จ ({result})')
        return analysis

    def measure(self,text):
        if text not in self.cache:
            analysis=self.analyse(text)
            try:
                size=self.u.ScriptString_pSize(analysis).contents
                self.cache[text]=(size.cx if text else 0,size.cy)
            finally:self.u.ScriptStringFree(c.byref(analysis))
        return self.cache[text]

    def render(self,lines):
        spacing=round(self.size*1.2)
        sizes=[self.measure(line) for line in lines]
        width=max(v[0] for v in sizes)+self.size*2
        height=max(v[1] for v in sizes)+(len(lines)-1)*spacing+self.size*2
        if width*height>12_000_000:raise ValueError('พื้นที่ข้อความใหญ่เกินไป')
        header=BITMAPINFOHEADER(c.sizeof(BITMAPINFOHEADER),width,-height,1,32,0,0,0,0,0,0)
        bits=c.c_void_p();bitmap=self.g.CreateDIBSection(self.dc,c.byref(header),0,c.byref(bits),None,0)
        if not bitmap:raise RuntimeError('สร้างภาพตัวอย่างข้อความไม่สำเร็จ')
        previous=self.g.SelectObject(self.dc,bitmap)
        try:
            c.memset(bits,255,width*height*4)
            for index,line in enumerate(lines):
                if not line:continue
                analysis=self.analyse(line)
                try:
                    if self.u.ScriptStringOut(analysis,(width-sizes[index][0])//2,self.size+index*spacing,0,None,0,0,False)!=0:
                        raise RuntimeError('วาดตัวอักษรไม่สำเร็จ')
                finally:self.u.ScriptStringFree(c.byref(analysis))
            self.g.GdiFlush()
            rgb=Image.frombytes('RGB',(width,height),c.string_at(bits,width*height*4),'raw','BGRX')
            alpha=rgb.getchannel('R').point(lambda x:255-x)
            box=alpha.getbbox()
            if box is None:raise ValueError('ข้อความไม่มีตัวอักษรที่วาดได้')
            tile=Image.new('RGBA',(width,height),(0,0,0,0));tile.putalpha(alpha)
            return tile.crop(box)
        finally:
            self.g.SelectObject(self.dc,previous);self.g.DeleteObject(bitmap)

    def close(self):
        if self.dc and self.oldfont:self.g.SelectObject(self.dc,self.oldfont)
        if self.font:self.g.DeleteObject(self.font)
        if self.dc:self.g.DeleteDC(self.dc)
        if getattr(self,'added',0):self.g.RemoveFontResourceExW(self.font_path,0x10,None)
        self.dc=self.font=None;self.added=0

    def __enter__(self):return self
    def __exit__(self,*args):self.close()
