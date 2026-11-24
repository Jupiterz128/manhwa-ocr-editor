"""Real Tk integration tests. Run with DISPLAY pointing to an X server."""
import os,sys,tempfile,unittest,json,time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import manhwa_ocr as m
import numpy as np
from PIL import Image,ImageDraw,ImageFont

@unittest.skipUnless(sys.platform=='win32' or os.environ.get('DISPLAY'),'Tk display unavailable')
class GUITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.folder=Path(self.tmp.name)
        self.cache_patch=patch.object(m,'DATA_DIR',self.folder/'data')
        self.cache_patch.start()
        self.root=m.tk.Tk()
        self.errors=[]
        self.root.report_callback_exception=lambda *args:self.errors.append(args)
        self.app=m.ManhwaOCRApp(self.root)
        self.sound_patch=patch.object(self.app.sounds,'play')
        self.play_sound=self.sound_patch.start()
        self.lama_patch=patch.object(self.app.cleaner,'check_lama',return_value={'ready':False,'code':'test_missing','message':'LaMa: test unavailable'})
        self.lama_patch.start()
        self.root.update()
        im=Image.new('RGB',(800,1400),'#a9bacb')
        d=ImageDraw.Draw(im)
        d.ellipse((80,70,640,350),fill='white',outline='#222222',width=4)
        d.ellipse((100,550,650,850),fill='white',outline='#222222',width=4)
        font_path=next((p for p in ['/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            '/usr/share/fonts/opentype/urw-base35/NimbusSans-Regular.otf','C:/Windows/Fonts/arial.ttf'] if Path(p).is_file()),None)
        f=ImageFont.truetype(font_path,32) if font_path else ImageFont.load_default()
        d.text((170,165),'Where are you going?',font=f,fill='black')
        d.text((200,665),'I am going home.',font=f,fill='black')
        self.image_path=self.folder/'page01.png'
        im.save(self.image_path)
        self.app.add_paths([self.image_path])
        self.root.update()

    def tearDown(self):
        self.app.sounds.close();self.sound_patch.stop()
        self.root.after_cancel(self.app.poll_id)
        self.root.destroy()
        self.cache_patch.stop()
        self.lama_patch.stop()
        self.tmp.cleanup()

    def test_rebuild_uses_original_and_repair_replaces_old_patch_with_undo(self):
        from bubble_cleaner import encode_patch
        a=self.app;p=a.page();b=m.Bubble([80,70,640,350],text='Hello',score=.5,lines=[{'box':[165,155,555,210]}])
        p.bubbles=[b];a.refresh_tree()
        bad=encode_patch([80,70,640,350],Image.new('RGB',(560,280),'blue'),np.full((280,560),255,np.uint8),'gradient',b.id)
        p.clean_patches=[bad]
        observed=[]
        def read(image,*args):observed.append(image.getpixel((200,100)));return []
        with patch.object(a,'cleaning_lines',side_effect=read):
            a.prepare_clean_masks(True);self.pump(lambda:a.busy)
        self.assertEqual(observed,[a.image.getpixel((200,100))])
        self.assertNotEqual(observed[0],(0,0,255))
        good=encode_patch([150,150,300,230],Image.new('RGB',(150,80),'white'),np.full((80,150),255,np.uint8),'solid',b.id)
        a.apply_clean_patch(p.id,good)
        self.assertEqual(p.clean_patches,[good])
        a.undo();self.assertEqual(a.page().clean_patches,[bad])

    def test_completion_sound_once_and_silent_on_failed_or_cancelled_jobs(self):
        a=self.app
        a.launch(lambda:None,completion_sound='ocr_all');self.pump(lambda:a.busy)
        self.play_sound.assert_called_once_with('ocr_all');self.play_sound.reset_mock()
        a.launch(lambda:False,completion_sound='clean_all');self.pump(lambda:a.busy)
        self.play_sound.assert_not_called()
        a.launch(lambda:a.cancel.set(),completion_sound='mask_all');self.pump(lambda:a.busy)
        self.play_sound.assert_not_called()
        with patch.object(m.messagebox,'showerror'):
            def fail():raise RuntimeError('fixture')
            a.launch(fail,completion_sound='psd');self.pump(lambda:a.busy)
        self.play_sound.assert_not_called()

    def test_masks_and_clean_all_play_matching_sound_but_single_page_is_silent(self):
        a=self.app;b=m.Bubble([80,70,640,350],text='Hello',score=.9,lines=[{'box':[165,155,555,210]}])
        a.page().bubbles=[b];a.refresh_tree()
        with patch.object(a,'cleaning_lines',return_value=[]):
            a.prepare_clean_masks();self.pump(lambda:a.busy)
            self.play_sound.assert_not_called()
            a.prepare_clean_masks(True);self.pump(lambda:a.busy)
            self.play_sound.assert_called_once_with('mask_all');self.play_sound.reset_mock()
            a.start_clean(True);self.pump(lambda:a.busy)
            self.play_sound.assert_called_once_with('clean_all')

    def test_psd_sound_only_after_successful_export(self):
        report={'clean_layers':0,'text_layers':[]}
        with patch('photoshop_export.photoshop_progid',return_value='test'),patch('photoshop_export.export_psd',return_value=report),patch.object(m.filedialog,'askdirectory',return_value=str(self.folder)):
            self.app.export_psd_pages();self.pump(lambda:self.app.busy)
        self.play_sound.assert_called_once_with('psd')

    def test_ocr_sound_for_all_pages_only_and_silent_when_page_fails(self):
        a=self.app
        with patch.object(a,'get_pipeline') as pipeline:
            pipeline.return_value.scan.return_value=[]
            a.start_scan();self.pump(lambda:a.busy)
            self.play_sound.assert_not_called()
            a.start_scan(True);self.pump(lambda:a.busy)
            self.play_sound.assert_called_once_with('ocr_all');self.play_sound.reset_mock()
            pipeline.return_value.scan.side_effect=RuntimeError('OCR fixture failure')
            a.start_scan(True);self.pump(lambda:a.busy)
            self.play_sound.assert_not_called()

    def select(self,b):
        self.app.tree.selection_set(b.id)
        self.root.update()

    def test_new_batch_buttons_run_all_pages_and_stop_cancels(self):
        a=self.app
        for key,method in (('ocr_all','start_scan'),('mask_all','prepare_clean_masks'),('clean_all','start_clean')):
            with patch.object(a,method) as action:
                a.ui_buttons[key].invoke();action.assert_called_once_with(True)
        a.busy=True;a.ui_buttons['stop'].invoke()
        self.assertTrue(a.cancel.is_set());a.busy=False
        self.assertTrue(a.neural.get());self.assertTrue(a.fast.get());self.assertTrue(a.bubble_only.get())
        self.assertFalse(a.gpu.get());self.assertFalse(a.retry.get())

    def test_workspace_matches_reference_layout_at_full_hd(self):
        a=self.app;self.root.geometry('1880x990+0+0');self.root.update()
        sections=a.ui_sections
        self.assertEqual(self.root.cget('bg'),'#191a1a')
        self.assertGreater(a.canvas.winfo_width(),900)
        self.assertGreater(a.canvas.winfo_height(),550)
        self.assertLess(sections['files'].winfo_rootx(),sections['batch'].winfo_rootx())
        self.assertLess(sections['image'].winfo_rootx(),sections['inspect'].winfo_rootx())
        self.assertLess(sections['inspect'].winfo_rootx(),sections['editor'].winfo_rootx())
        self.assertGreater(sections['pages'].winfo_rooty(),a.canvas.winfo_rooty()+a.canvas.winfo_height())
        self.assertGreater(sections['status'].winfo_rooty(),sections['editor'].winfo_rooty()+sections['editor'].winfo_height())
        for widget in a.ui_buttons.values():
            self.assertTrue(widget.winfo_ismapped())
            x=widget.winfo_rootx()-self.root.winfo_rootx();y=widget.winfo_rooty()-self.root.winfo_rooty()
            self.assertGreaterEqual(x,0);self.assertGreaterEqual(y,0)
            self.assertLessEqual(x+widget.winfo_width(),self.root.winfo_width())
            self.assertLessEqual(y+widget.winfo_height(),self.root.winfo_height())
        self.assertFalse(a.output.winfo_ismapped())

    def test_game_meter_reflects_actual_progress_events(self):
        a=self.app
        a.events.put(('progress',35));self.pump(lambda:a.progress['value']!=35)
        self.assertEqual(a.progress['value'],35)
        labels=lambda:[a.progress.itemcget(item,'text') for item in a.progress.find_all() if a.progress.type(item)=='text']
        self.assertIn('35%',labels())
        a.events.put(('progress',100));self.pump(lambda:a.progress['value']!=100)
        self.assertEqual(a.progress['value'],100)
        self.assertIn('100%',labels())
        a.progress['value']=0
        self.assertIn('0%',labels())

    def test_reading_view_centered_with_draggable_seventy_thirty_split(self):
        a=self.app;self.root.geometry('1880x990+0+0');self.root.update()
        split=a.work_split
        self.assertAlmostEqual(split.sash_coord(0)[0]/(split.winfo_width()-8),.70,delta=.015)
        self.assertLessEqual(a.image.width*a.scale,700)
        self.assertLessEqual(a.scale,1)
        image_center=a.image.width*a.scale/2-a.canvas.canvasx(0)
        self.assertAlmostEqual(image_center,a.canvas.winfo_width()/2,delta=2)
        a.scroll_y('moveto',1)
        self.assertGreater(a.canvas.canvasy(0),0)
        split.sash_place(0,round((split.winfo_width()-8)*.60),0)
        split.event_generate('<ButtonRelease-1>');self.root.update()
        self.root.geometry('1750x940');self.root.update()
        self.assertAlmostEqual(split.sash_coord(0)[0]/(split.winfo_width()-8),.60,delta=.02)
        self.assertEqual(self.errors,[])

    def test_small_centered_image_click_draw_and_zoom_coordinates(self):
        from types import SimpleNamespace
        a=self.app;a.image=Image.new('RGB',(400,240),'white');a.fit();self.root.update()
        b=m.Bubble([80,50,200,140]);a.page().bubbles=[b];a.refresh_tree()
        self.assertLess(a.canvas.canvasx(0),0);self.assertLess(a.canvas.canvasy(0),0)
        def event(x,y):return SimpleNamespace(x=x*a.scale-a.canvas.canvasx(0),y=y*a.scale-a.canvas.canvasy(0))
        a.canvas_down(event(120,90));self.assertEqual(a.tree.selection(),(b.id,))
        a.draw_mode.set(True);a.canvas_down(event(230,70));a.canvas_up(event(350,180))
        self.assertTrue(any(np.allclose(v.box,[230,70,350,180],atol=1) for v in a.page().bubbles))
        self.assertAlmostEqual(a.image.height*a.scale/2-a.canvas.canvasy(0),a.canvas.winfo_height()/2,delta=2)
        a.fit('width');self.assertGreater(a.scale,1)
        a.fit();self.assertEqual(a.scale,.56)

    def test_manual_zoom_retains_reading_position_and_resize_does_not_reset_it(self):
        a=self.app;a.scroll_y('moveto',.30)
        def center_y():return a.canvas.canvasy(a.canvas.winfo_height()/2)/a.scale
        before=center_y();a.zoom(1.25)
        self.assertAlmostEqual(before,center_y(),delta=3)
        size=a.scale;self.root.geometry('1700x920');self.root.update()
        self.assertEqual(a.scale,size)
        self.assertEqual(a.view_mode,'manual')

    def test_default_zoom_56_survives_resize_and_page_change(self):
        a=self.app;self.assertEqual(a.scale,.56)
        self.root.geometry('1700x920');self.root.update();self.assertEqual(a.scale,.56)
        a.zoom(1.25);self.assertNotEqual(a.scale,.56)
        a.select_page(0);self.root.update();self.assertEqual(a.scale,.56)
        self.assertEqual(a.zoom_note.get(),'56%')

    def test_auto_tilt_text_survives_project_round_trip(self):
        from text_layout import layout_text,is_current
        q=[[110,200],[550,100],[580,250],[140,350]]
        line=m.Line('HELLO',m.quad_bounds(q),.3,'test',q)
        b=m.Bubble(line.box,lines=[m.asdict(line)],translation='สวัสดีครับ')
        m.auto_tilt(b,self.app.image.size);b.text_layer=layout_text(self.app.image,b)
        a=self.app;a.page().bubbles=[b];a.refresh_tree(b.id)
        before=m.asdict(b);path=self.folder/'auto-type.json'
        with patch.object(m.filedialog,'asksaveasfilename',return_value=str(path)):a.save_project()
        with patch.object(m.filedialog,'askopenfilename',return_value=str(path)):a.open_project()
        after=a.page().bubbles[0]
        self.assertEqual(m.asdict(after),before);self.assertTrue(is_current(after))
        self.assertEqual(self.errors,[])

    def test_scroll_cache_reuses_photo_and_refreshes_changed_preview(self):
        a=self.app;a.scroll_y('moveto',0);a.render()
        photo=a.photo;grid=a.canvas.find_withtag('workspace_grid')
        a.scroll_y('scroll',20,'units')
        self.assertIs(a.photo,photo);self.assertEqual(a.canvas.find_withtag('workspace_grid'),grid)
        a.cleaned_preview=Image.new('RGB',a.image.size,'red');a.show_cleaned.set(True);a.render()
        self.assertIsNot(a.photo,photo)
        self.assertEqual(m.ImageTk.getimage(a.photo).getpixel((20,20))[:3],(255,0,0))
        clean=a.photo;a.mask_preview=Image.new('RGB',a.image.size,'blue');a.show_masks.set(True);a.render()
        self.assertIsNot(a.photo,clean)
        self.assertEqual(m.ImageTk.getimage(a.photo).getpixel((20,20))[:3],(0,0,255))
        a.zoom(1.2);self.assertGreater(a.photo.width(),0)
        a.image=None;a.render();self.assertIsNone(a._view_cache)
        a.image=Image.open(self.image_path).convert('RGB');a.fit()
        self.assertGreater(float(a.canvas.cget('scrollregion').split()[3]),100)
        b=m.Bubble([50,50,300,200],translation='old',text_layer={'text':'old'})
        a.page().bubbles=[b]
        with patch('text_layout.composite_text_crop',side_effect=lambda crop,*args:crop.copy()) as composite:
            a.render();b.translation='changed';a.render()
            self.assertEqual(composite.call_count,2)

    def test_wheel_burst_coalesces_without_losing_delta_or_scrolling_new_page(self):
        from types import SimpleNamespace
        a=self.app;a.scroll_y('moveto',0)
        with patch.object(a,'render',wraps=a.render) as render:
            for _ in range(12):a.wheel(SimpleNamespace(state=0,delta=-10))
            self.root.update()
            self.assertEqual(render.call_count,1)
        self.assertAlmostEqual(a.canvas.canvasy(0),60,delta=1)
        a.wheel(SimpleNamespace(state=0,delta=-120));a.select_page(0);self.root.update()
        self.assertAlmostEqual(a.canvas.canvasy(0),0,delta=1)
        self.assertIsNone(a._wheel_job)

    def test_rounded_scrollbar_drag_page_click_keyboard_and_text_scroll(self):
        from app_ui import RoundedScrollbar
        from types import SimpleNamespace
        a=self.app;viewer=a.ui_sections['image']
        bar=next(w for w in viewer.winfo_children() if isinstance(w,RoundedScrollbar) and w.vertical)
        self.root.update();start,end,_,_=bar.geometry_values()
        bar.press(SimpleNamespace(x=7,y=(start+end)/2));bar.motion(SimpleNamespace(x=7,y=(start+end)/2+70));bar.release(None)
        self.root.update();self.assertGreater(a.canvas.yview()[0],0)
        bar.move(0);self.root.update();_,end,_,_=bar.geometry_values()
        bar.press(SimpleNamespace(x=7,y=end+10));self.root.update();self.assertGreater(a.canvas.yview()[0],0)
        bar.focus_force();bar.event_generate('<Home>');self.root.update();self.assertAlmostEqual(a.canvas.yview()[0],0,delta=.01)
        a.source.configure(state='normal');a.source.insert('1.0','long line\n'*100);self.root.update()
        self.assertIsInstance(a.source.vbar,RoundedScrollbar)
        a.source.vbar.move(.5);self.root.update();self.assertGreater(a.source.yview()[0],.1)
        self.assertEqual(self.errors,[])

    def test_alternative_controls_remain_visible_in_short_window_and_can_apply(self):
        b=m.Bubble([80,70,640,350],text='LGH',score=.5,engine='fixture',
                   candidates=[{'text':'UGH','score':.9,'engine':'alternative'}])
        self.app.page().bubbles=[b];self.app.refresh_tree();self.select(b)
        self.root.geometry('1100x700');self.root.update()
        self.assertEqual(self.root.winfo_height(),700)
        for widget in (self.app.candidate_combo,self.app.candidate_button):
            self.assertTrue(widget.winfo_ismapped())
            y=widget.winfo_rooty()-self.root.winfo_rooty()
            self.assertGreaterEqual(y,0)
            self.assertLessEqual(y+widget.winfo_height(),self.root.winfo_height())
        self.app.candidate_combo.current(0);self.app.candidate_button.invoke();self.root.update()
        self.assertEqual(b.text,'UGH')
        self.assertEqual(b.candidates[0]['text'],'LGH')

    def test_bad_translation_import_highlights_line_and_keeps_dialog_open(self):
        from text_editor import TextImportDialog
        dialog=TextImportDialog(self.app)
        dialog.editor.delete('1.0','end');dialog.editor.insert('1.0','\nข้อความเกริ่นก่อนรายการ')
        with patch('text_editor.messagebox.showerror') as error,patch.object(self.app,'start_text_layout') as layout:
            dialog.apply();self.root.update()
            self.assertTrue(dialog.win.winfo_exists())
            self.assertEqual(str(dialog.editor.tag_ranges('import_error')[0]),'2.0')
            self.assertIn('บรรทัด 2',error.call_args.args[1])
            layout.assert_not_called()
        dialog.win.destroy()

    def test_tolerant_import_button_passes_translation_and_logs_without_confirmation(self):
        from text_editor import TextImportDialog
        b=m.Bubble([80,70,640,350],id='fixture',text='WRONG')
        self.app.page().bubbles=[b]
        dialog=TextImportDialog(self.app)
        dialog.editor.delete('1.0','end')
        dialog.editor.insert('1.0','รหัส: fixture\nคำต้นฉbับ: WRING\nคำแปลไทย: ผิดแล้ว')
        with patch('text_editor.messagebox.showerror') as error,patch('text_editor.messagebox.askokcancel',return_value=True),patch.object(self.app,'start_text_layout') as layout,patch.object(self.app,'log') as log:
            dialog.apply_button.invoke();self.root.update()
            error.assert_not_called()
            layout.assert_called_once_with({'fixture':'ผิดแล้ว'})
            self.assertEqual(log.call_count,3)
            self.assertEqual(b.text,'WRONG')

    def test_edit_candidates_merge_split_save_reload_and_export(self):
        a=self.app
        first=m.Bubble([80,70,640,350],text='Where are you going?',score=.6,engine='fixture',
            candidates=[{'text':'Where are you going!','score':.9,'engine':'alternative'}])
        second=m.Bubble([100,550,650,850],text='I am going home.',score=.95,engine='fixture')
        a.page().bubbles=[first,second]
        a.refresh_tree()
        self.select(first)
        a.source.delete('1.0','end');a.source.insert('1.0','Where are you going now?')
        a.thai.insert('1.0','เธอกำลังจะไปไหน')
        self.root.update()
        self.assertEqual(first.text,'Where are you going now?')
        self.assertIn('เธอกำลังจะไปไหน',a.output.get('1.0','end'))
        a.candidate_combo.current(0);a.use_candidate();self.root.update()
        self.assertEqual(first.text,'Where are you going!')
        self.assertEqual(first.candidates[0]['text'],'Where are you going now?')
        a.tree.selection_set(first.id,second.id);self.root.update();a.merge();self.root.update()
        self.assertEqual(len(a.page().bubbles),1)
        a.source.delete('1.0','end');a.source.insert('1.0','First sentence\nSecond sentence')
        a.thai.delete('1.0','end');a.thai.insert('1.0','ประโยคหนึ่ง\nประโยคสอง')
        self.root.update();a.split();self.root.update()
        self.assertEqual([b.translation for b in a.page().bubbles],['ประโยคหนึ่ง','ประโยคสอง'])
        a.move(1);self.root.update()
        self.assertEqual(a.page().bubbles[0].text,'Second sentence')
        a.undo();self.root.update()
        self.assertEqual(a.page().bubbles[0].text,'First sentence')
        project=self.folder/'project.json'
        a.project_path=str(project)
        self.assertTrue(a.save_project())
        data=json.loads(project.read_text())
        self.assertEqual(data['pages'][0]['path'],'page01.png')
        with patch.object(m.filedialog,'askopenfilename',return_value=str(project)):
            a.open_project()
        self.root.update()
        self.assertEqual(a.page().bubbles[1].translation,'ประโยคสอง')
        out=self.folder/'export.txt'
        with patch.object(m.filedialog,'asksaveasfilename',return_value=str(out)):
            a.export()
        self.assertEqual(out.read_text().splitlines()[0],'คำต้นฉบับ: First sentence')
        self.assertIn('คำแปลไทย: ประโยคหนึ่ง',out.read_text())
        self.assertEqual(self.errors,[])
        self.select(a.page().bubbles[0])
        # Capture actual application; data shown is a synthetic fixture, not OCR evidence.
        try:
            from PIL import ImageGrab
            ImageGrab.grab(xdisplay=os.environ['DISPLAY']).save('qa-runtime/ui_verified.png')
        except Exception as exc:
            print('Screenshot unavailable:',exc)

    def test_background_result_routes_to_original_page(self):
        a=self.app
        p=a.page()
        other=self.folder/'page02.png';Image.new('RGB',(200,300),'white').save(other)
        a.add_paths([other]);self.root.update()
        class FakePipeline:
            hits=0
            def check(self):pass
            def scan(self,image):
                time.sleep(.05)
                return [m.Bubble([10,10,100,50],text='안녕하세요',score=.9)]
        with patch.object(a,'get_pipeline',return_value=FakePipeline()):
            a.start_scan()
            a.select_page(1)
            until=time.time()+4
            while a.busy and time.time()<until:
                self.root.update();time.sleep(.02)
        self.assertFalse(a.busy)
        self.assertEqual(p.bubbles[0].text,'안녕하세요')
        self.assertEqual(a.page().bubbles,[])
        self.assertEqual(self.errors,[])

    def test_undo_last_page_clears_view(self):
        self.app.undo();self.root.update()
        self.assertEqual(self.app.pages,[])
        self.assertIsNone(self.app.image)
        self.assertEqual(self.app.output.get('1.0','end').strip(),'')
        self.assertEqual(self.errors,[])

    def test_partial_result_stop_and_elapsed_status(self):
        a=self.app
        original=a.page()
        other=self.folder/'page02.png';Image.new('RGB',(200,300),'white').save(other)
        a.add_paths([other]);self.root.update()
        class PartialPipeline:
            hits=0
            def check(self):
                if a.cancel.is_set(): raise m.Cancelled()
            def scan(self,image):
                self.on_partial([m.Bubble([10,10,100,50],text='Partial text')])
                self.on_progress(.5)
                while not a.cancel.wait(.01): pass
                self.check()
        with patch.object(a,'get_pipeline',return_value=PartialPipeline()):
            a.start_scan()
            a.select_page(1)
            until=time.time()+3
            while not original.bubbles and time.time()<until:
                self.root.update();time.sleep(.01)
            self.assertTrue(a.busy)
            self.assertIn('รวม',a.status.get())
            self.assertFalse(original.processed)
            self.assertEqual(original.bubbles[0].text,'Partial text')
            self.assertEqual(a.page().bubbles,[])
            self.assertEqual(float(a.progress['value']),50)
            a.stop()
            while a.busy and time.time()<until:
                self.root.update();time.sleep(.01)
        self.assertFalse(a.busy)
        self.assertIn('หยุดแล้ว',a.status.get())
        self.assertEqual(original.bubbles[0].text,'Partial text')
        self.assertEqual(self.errors,[])

    def test_error_status_not_overwritten_by_done(self):
        a=self.app
        a.job_started=time.monotonic()
        a.busy=True
        a.events.put(('page_error',(a.page().id,'test failure')))
        a.events.put(('done',None))
        until=time.time()+2
        while a.busy and time.time()<until:
            self.root.update();time.sleep(.01)
        self.assertIn('ข้อผิดพลาด',a.status.get())
        self.assertFalse(a.page().processed)

    def test_rapid_button_offers_choice_without_overwriting(self):
        a=self.app
        bubble=m.Bubble([80,70,640,350],text='LGH',translation='คำแปลเดิม')
        a.page().bubbles=[bubble]
        a.refresh_tree();self.select(bubble)
        class FakeRapid:
            hits=0
            def candidates(self,image,b):
                return [{'text':'UGH','score':.98,'engine':'Rapid test'}]
        with patch.object(m,'LocalPipeline',return_value=FakeRapid()) as init:
            for _ in range(2):
                a.rapid_bubble()
                until=time.time()+2
                while a.busy and time.time()<until:
                    self.root.update();time.sleep(.01)
                self.assertFalse(a.busy)
            self.assertEqual(init.call_count,1)
            self.assertFalse(init.call_args.kwargs['neural'])
            self.assertEqual(init.call_args.kwargs['mode'],'rapid')
        self.assertEqual(bubble.text,'LGH')
        self.assertEqual(bubble.translation,'คำแปลเดิม')
        self.assertEqual(len(bubble.candidates),1)
        a.candidate_combo.current(0);a.use_candidate();self.root.update()
        self.assertEqual(bubble.text,'UGH')
        self.assertEqual(bubble.translation,'คำแปลเดิม')
        self.assertEqual(self.errors,[])

    def test_clean_editor_preview_apply_undo_save_reopen_export(self):
        import numpy as np
        a=self.app
        original=self.image_path.read_bytes()
        bubble=m.Bubble([80,70,640,350],text='Where are you going?',
            lines=[m.asdict(m.Line('Where are you going?',[165,155,555,210],.95))])
        a.page().bubbles=[bubble];a.refresh_tree();self.select(bubble)
        editor=a.open_clean_editor();self.root.update()
        self.assertTrue(editor.region.boundary_found)
        mask=editor.region.mask.copy()
        editor.clear_mask();self.assertFalse(np.any(editor.region.mask))
        editor.undo_mask();self.assertTrue(np.array_equal(mask,editor.region.mask))
        editor.verify_after.set(False)
        editor.preview()
        until=time.time()+5
        while editor.busy and time.time()<until:
            self.root.update();time.sleep(.01)
        self.assertIsNotNone(editor.result)
        try:
            from PIL import ImageGrab
            ImageGrab.grab(xdisplay=os.environ['DISPLAY']).save('qa-runtime/clean_editor_verified.png')
        except Exception:pass
        editor.apply();self.root.update()
        self.assertEqual(len(a.page().clean_patches),1)
        self.assertEqual(a.page().bubbles[0].text,'Where are you going?')
        self.assertIsNotNone(a.cleaned_preview)
        clean_patch=a.page().clean_patches[0]
        a.undo();self.root.update();self.assertEqual(a.page().clean_patches,[])
        a.apply_clean_patch(a.page().id,clean_patch)
        a.project_path=str(self.folder/'clean_project.json');self.assertTrue(a.save_project())
        with patch.object(m.filedialog,'askopenfilename',return_value=a.project_path):
            a.open_project()
        self.assertEqual(len(a.page().clean_patches),1)
        with patch.object(m.filedialog,'askdirectory',return_value=str(self.folder)),patch.object(m.messagebox,'showinfo'):
            a.export_clean_images()
            until=time.time()+5
            while a.busy and time.time()<until:
                self.root.update();time.sleep(.01)
        self.assertFalse(a.busy)
        self.assertTrue((self.folder/'cleaned_pages/page01_clean.png').is_file())
        self.assertIn('cleaned_pages',a.status.get())
        self.assertEqual(self.image_path.read_bytes(),original)
        self.assertEqual(self.errors,[])

    def pump(self,predicate,seconds=20):
        until=time.time()+seconds
        while predicate() and time.time()<until:
            self.root.update();time.sleep(.01)
        self.root.update()
        self.assertFalse(predicate())

    def test_batch_stages_then_cleans_all_selected_even_uncertain(self):
        a=self.app;original_page=a.page()
        first=m.Bubble([80,70,640,350],text="Hello",score=.01,lines=[{'box':[165,155,555,210]}])
        second=m.Bubble([100,550,650,850],text='No coordinates',score=.01)
        third=m.Bubble([0,1000,100,1100],text='Not selected')
        original_page.bubbles=[first,second,third];a.refresh_tree();self.root.update()
        a.tree.selection_set(first.id,second.id);self.root.update()
        with patch.object(a,'cleaning_lines',return_value=[]):
            a.prepare_clean_masks(selected=True);self.pump(lambda:a.busy)
        self.assertEqual(len(a.clean_prepared),2)
        self.assertEqual(original_page.clean_patches,[])
        self.assertIsNotNone(a.mask_preview)
        # Simulate an open contour. It must still be cleaned, without confidence skipping.
        a.clean_prepared[(original_page.id,second.id)].boundary_found=False
        a.tree.selection_set(first.id,second.id);self.root.update()
        with patch.object(a,'cleaning_lines',return_value=[]) as read:
            a.start_clean(selected=True);self.pump(lambda:a.busy)
            self.assertEqual(read.call_count,2)
        self.assertEqual(len(original_page.clean_patches),2)
        self.assertEqual(set(a.clean_prepared),{(original_page.id,second.id)})
        self.assertIn('ประมวลผล 2/2',a.status.get())
        self.assertEqual(self.errors,[])

    def test_editor_rescan_verification_and_saved_draft(self):
        from types import SimpleNamespace
        import numpy as np
        a=self.app;b=m.Bubble([80,70,640,350],text='KEEP',translation='เก็บคำแปล')
        a.page().bubbles=[b];a.refresh_tree();self.select(b)
        editor=a.open_clean_editor();self.root.update()
        line={'text':'Where are you going?','box':[165,155,555,210],'score':.01,'engine':'test'}
        with patch.object(a,'cleaning_lines',return_value=[line]):
            editor.scan_mask();self.pump(lambda:editor.busy)
        self.assertTrue(np.any(editor.region.mask))
        with patch.object(a,'cleaning_lines',return_value=[line]):
            editor.preview();self.pump(lambda:editor.busy)
        self.assertEqual(len(editor.remaining_lines),1)
        self.assertIn('ยังอ่านได้',editor.note.get())
        editor.add_remaining();self.assertIsNone(editor.result)
        # Rectangle tool covers a faint remnant that OCR cannot recognize.
        editor.tool.set('rect');editor.remember();editor.rect_start=(180,120)
        editor.rect_base=editor.region.mask.copy();editor.stroke((220,150))
        self.assertEqual(editor.region.mask[130,190],255)
        editor.save_mask();self.root.update()
        self.assertIn((a.page().id,b.id),a.clean_prepared)
        self.assertEqual(b.text,'KEEP');self.assertEqual(b.translation,'เก็บคำแปล')
        reopened=a.open_clean_editor();self.root.update()
        self.assertTrue(np.array_equal(reopened.region.mask,a.clean_prepared[(a.page().id,b.id)].mask))
        reopened.close();self.assertEqual(self.errors,[])

    def test_clean_requires_mask_before_any_pixel_changes(self):
        a=self.app;b=m.Bubble([80,70,640,350]);a.page().bubbles=[b]
        with patch.object(m.messagebox,'showinfo') as info:
            a.start_clean();self.assertEqual(info.call_count,1)
        self.assertEqual(a.page().clean_patches,[]);self.assertFalse(a.busy)

    def test_clean_draft_survives_project_save_and_reload(self):
        import numpy as np
        a=self.app;b=m.Bubble([80,70,640,350],lines=[{'box':[165,155,555,210]}])
        a.page().bubbles=[b];a.refresh_tree();self.select(b)
        editor=a.open_clean_editor();editor.save_mask();self.root.update()
        expected=a.clean_prepared[(a.page().id,b.id)].mask.copy()
        a.project_path=str(self.folder/'draft.json');self.assertTrue(a.save_project())
        a.clean_prepared={}
        with patch.object(m.filedialog,'askopenfilename',return_value=a.project_path):a.open_project()
        self.assertTrue(np.array_equal(a.clean_prepared[(a.page().id,b.id)].mask,expected))
        self.assertEqual(a.page().clean_patches,[])

    def test_batch_mixed_failures_have_distinct_reasons_and_continue(self):
        import numpy as np
        a=self.app
        a.page().bubbles=[m.Bubble([80,70,640,350],text="Hello",score=.01) for _ in range(4)]
        for b in a.page().bubbles:
            a.clean_prepared[(a.page().id,b.id)]=m.prepare_region(a.image,b)
        a.refresh_tree();calls=[]
        def fake(cleaner,region,**kwargs):
            calls.append(1)
            if len(calls)==1:raise RuntimeError('LaMa unavailable for this region')
            if len(calls)==2:raise ValueError('invalid mask for this region')
            report={'passes':1,'remaining':[],'verification_error':'offline' if len(calls)==3 else '',
                'visual_checked':True,'visual_residual_pixels':0,'needs_review':len(calls)==3}
            return region.image,region.mask,'solid',report
        with patch('clean_quality.clean_checked',side_effect=fake):
            a.start_clean();self.pump(lambda:a.busy)
        self.assertEqual(len(calls),4);self.assertEqual(len(a.page().clean_patches),2)
        self.assertEqual(len(a.clean_prepared),3)
        folder=next((self.folder/'data/clean_logs').iterdir())
        records=[json.loads(line) for line in (folder/'regions.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([r['status'] for r in records],['failed','failed','review','success'])
        self.assertIn('LaMa unavailable',records[0]['error'])
        self.assertIn('invalid mask',records[1]['error'])
        self.assertIn('สำเร็จตามการตรวจ 1',a.status.get());self.assertIn('ล้มเหลว 2',a.status.get())
        self.assertTrue((folder/'region-0001.json').is_file())

    def test_batch_cancel_keeps_mask_and_reports_unattempted(self):
        a=self.app;a.page().bubbles=[m.Bubble([80,70,640,350],text="Hello",score=.95) for _ in range(3)]
        for b in a.page().bubbles:a.clean_prepared[(a.page().id,b.id)]=m.prepare_region(a.image,b)
        a.refresh_tree()
        with patch('clean_quality.clean_checked',side_effect=InterruptedError('cancelled inside cleaner')):
            a.start_clean();self.pump(lambda:a.busy)
        self.assertEqual(len(a.clean_prepared),3);self.assertEqual(a.page().clean_patches,[])
        self.assertIn('ประมวลผล 1/3',a.status.get());self.assertIn('ยังไม่ทำ 2',a.status.get())
        self.assertIn('ยกเลิกระหว่างทำ 1',a.status.get())

    def test_editor_failure_retains_mask_without_a_patch(self):
        a=self.app;b=m.Bubble([80,70,640,350],lines=[{'box':[165,155,555,210]}])
        a.page().bubbles=[b];a.refresh_tree();self.select(b)
        editor=a.open_clean_editor()
        with patch.object(a.cleaner,'clean',side_effect=RuntimeError('repair failed')),patch('clean_editor.messagebox.showerror'):
            editor.preview();self.pump(lambda:editor.busy)
        self.assertIsNone(editor.result);self.assertEqual(a.page().clean_patches,[])
        self.assertIn((a.page().id,b.id),a.clean_prepared)
        self.assertIn('repair failed',editor.note.get());editor.close()

    def test_lama_status_button_displays_check_result(self):
        a=self.app;a.check_clean_readiness();self.pump(lambda:a.busy)
        self.assertIn('test unavailable',a.lama_note.get())
        a.cleaner.check_lama.assert_called_once_with(force=True)

    def test_restore_report_mask_checks_source_image(self):
        a=self.app;b=m.Bubble([80,70,640,350],lines=[{'box':[165,155,555,210]}])
        a.page().bubbles=[b];a.refresh_tree();self.select(b)
        region=m.prepare_region(a.image,b)
        path=self.folder/'region-0001.json'
        path.write_text(json.dumps(m.encode_region(region)),encoding='utf-8')
        with patch.object(m.filedialog,'askopenfilename',return_value=str(path)):
            a.restore_clean_draft()
        self.assertIn((a.page().id,b.id),a.clean_prepared)
        a.clean_prepared={};region.image=Image.new('RGB',region.image.size,'red')
        path.write_text(json.dumps(m.encode_region(region)),encoding='utf-8')
        with patch.object(m.filedialog,'askopenfilename',return_value=str(path)),patch.object(m.messagebox,'showerror') as error:
            a.restore_clean_draft()
        error.assert_called_once();self.assertEqual(a.clean_prepared,{})

    def test_add_text_import_layout_save_reload_and_undo(self):
        a=self.app;b=m.Bubble([80,70,640,350],text='Where are you going?',lines=[{'box':[165,155,555,210]}])
        a.page().bubbles=[b];a.refresh_tree();self.select(b)
        dialog=a.add_text();self.root.update()
        dialog.editor.delete('1.0','end')
        dialog.editor.insert('1.0',f'รหัส: {b.id}\nคำต้นฉบับ: Where are you going?\nคำแปลไทย: คุณกำลังจะไปไหน')
        with patch('text_editor.messagebox.askokcancel',return_value=True):
            dialog.apply();self.pump(lambda:a.busy)
        self.assertEqual(b.translation,'คุณกำลังจะไปไหน')
        self.assertEqual(b.text_layer['font'],'THSarabunNew-Bold');self.assertTrue(b.text_layer['auto_leading'])
        self.assertEqual(a.thai.get('1.0','end-1c'),b.translation)
        a.project_path=str(self.folder/'text_project.json');self.assertTrue(a.save_project())
        saved=json.loads(Path(a.project_path).read_text(encoding='utf-8'))
        self.assertEqual(saved['version'],5)
        self.assertEqual(saved['pages'][0]['bubbles'][0]['text_layer'],b.text_layer)
        with patch.object(m.filedialog,'askopenfilename',return_value=a.project_path):a.open_project()
        b=a.page().bubbles[0];self.select(b)
        self.assertEqual(b.text_layer['translation'],'คุณกำลังจะไปไหน')
        a.start_text_layout({b.id:'คำแปลใหม่'});self.pump(lambda:a.busy)
        a.undo();self.root.update();self.assertEqual(a.page().bubbles[0].translation,'คุณกำลังจะไปไหน')
        self.assertEqual(self.errors,[])

    def test_layout_failure_keeps_translation_without_fake_layer(self):
        a=self.app;b=m.Bubble([80,70,640,350],text='Hello');a.page().bubbles=[b]
        a.refresh_tree();self.select(b)
        with patch('text_layout.layout_text',side_effect=ValueError('no space')):
            a.start_text_layout({b.id:'คำแปล'});self.pump(lambda:a.busy)
        self.assertEqual(b.translation,'คำแปล');self.assertEqual(b.text_layer,{})
        self.assertIn('ล้มเหลว 1',a.status.get())

    def test_psd_batch_uses_all_pages_and_reports_failure_separately(self):
        a=self.app
        other=self.folder/'other.png';Image.new('RGB',(200,200),'white').save(other)
        a.add_paths([other]);self.root.update()
        report={'clean_layers':0,'text_layers':[],'warnings':[]}
        with patch('photoshop_export.photoshop_progid',return_value='test'),patch('photoshop_export.export_psd',side_effect=[RuntimeError('first page failed'),report]) as export,patch.object(m.filedialog,'askdirectory',return_value=str(self.folder)),patch.object(m.messagebox,'showwarning'):
            a.export_psd_pages(True,False);self.pump(lambda:a.busy)
        self.assertEqual(export.call_count,2)
        self.assertIn('สำเร็จ 1',a.status.get());self.assertIn('ล้มเหลว 1',a.status.get())
        self.play_sound.assert_not_called()
        self.assertEqual(self.errors,[])

    def test_tilted_frame_edit_save_reload_undo_and_invalid_geometry(self):
        from frame_geometry import quad_bounds
        from types import SimpleNamespace
        a=self.app;b=m.Bubble([80,70,640,350],text='HELLO',lines=[{'box':[165,155,555,210]}])
        a.page().bubbles=[b];a.refresh_tree(b.id);a.tree.selection_set(b.id);self.root.update()
        a.clean_prepared[(a.page().id,b.id)]=m.prepare_region(a.image,b)
        a.edit_frame.set(True);a.toggle_frame_edit()
        # Drag the upper-left corner in actual canvas coordinates.
        x,y=80*a.scale,70*a.scale
        event=SimpleNamespace(x=x-a.canvas.canvasx(0),y=y-a.canvas.canvasy(0))
        a.canvas_down(event)
        self.assertTrue(hasattr(a,'frame_drag'))
        event.x+=30*a.scale;event.y+=25*a.scale
        a.canvas_drag(event);a.canvas_up(event)
        # Tk rounds event coordinates to display pixels: half a screen pixel
        # corresponds to more than half a source pixel at the 56% default zoom.
        self.assertTrue(np.allclose(b.quad[0],[110,95],atol=.5/a.scale));self.assertEqual(b.box,quad_bounds(b.quad))
        self.assertEqual(b.lines,[]);self.assertNotIn((a.page().id,b.id),a.clean_prepared)
        q=m.copy.deepcopy(b.quad)
        a.set_frame(b,[[0,0],[80,80],[80,0],[0,80]])
        self.assertEqual(b.quad,q)
        path=self.folder/'tilted.json'
        with patch.object(m.filedialog,'asksaveasfilename',return_value=str(path)):a.save_project()
        with patch.object(m.filedialog,'askopenfilename',return_value=str(path)):a.open_project()
        self.assertEqual(a.page().bubbles[0].quad,q)
        a.refresh_tree(b.id);a.tree.selection_set(b.id);self.root.update()
        with patch.object(m.simpledialog,'askfloat',return_value=10):a.rotate_frame()
        self.assertNotEqual(a.page().bubbles[0].quad,q)
        a.undo();self.assertEqual(a.page().bubbles[0].quad,q)
        self.assertEqual(self.errors,[])

    def test_numeric_psd_export_preserves_existing_output_and_single_page_number(self):
        a=self.app;p=a.page();p.path=str(self.folder/'67_4.png');a.image.save(p.path)
        folder=self.folder/'psd_pages';folder.mkdir();(folder/'04.psd').write_bytes(b'old')
        report={'clean_layers':0,'text_layers':[],'warnings':[]}
        with patch('photoshop_export.photoshop_progid',return_value='test'),patch('photoshop_export.export_psd',return_value=report) as export,patch.object(m.filedialog,'askdirectory',return_value=str(self.folder)):
            a.export_psd_pages(False,False);self.pump(lambda:a.busy)
        self.assertEqual(export.call_args.args[2].name,'04_1.psd')
        self.assertEqual((folder/'04.psd').read_bytes(),b'old')

    def test_tilted_clean_reader_maps_both_passes_back_to_page(self):
        from frame_geometry import ocr_crop,map_points
        a=self.app;q=[[120,150],[580,90],[620,350],[140,410]]
        b=m.Bubble(m.quad_bounds(q),quad=q);crop,inverse=ocr_crop(a.image,b)
        engine=object()
        class FakePipeline:
            def __init__(self,*args,**kwargs):pass
            def iter_engines(self):yield engine
            def read(self,e,im,variant):
                sx,sy=im.width/crop.width,im.height/crop.height
                return [m.Line('HELLO',[10*sx,20*sy,120*sx,50*sy],.01,'fake')]
        with patch.object(m,'LocalPipeline',FakePipeline):
            result=a.cleaning_lines(a.image,b,('easy','en',False),a.cancel)
        self.assertEqual(len(result),1)
        expected=map_points([[10,20],[120,20],[120,50],[10,50]],inverse)
        self.assertTrue(np.allclose(result[0]['quad'],expected,atol=.01))
        self.assertTrue(np.allclose(result[0]['box'],m.quad_bounds(expected),atol=.01))

    def test_cleaning_reader_crop_coordinates_and_no_neural_detector(self):
        from types import SimpleNamespace
        a=self.app;b=m.Bubble([80,70,640,350],text='original')
        calls=[];engine=object()
        class FakePipeline:
            def __init__(self,*args,**kwargs):calls.append(kwargs)
            def iter_engines(self):yield engine
            def read(self,e,im,variant):
                self_size=im.size
                if self_size not in ((560,280),(1120,560)):raise AssertionError(self_size)
                scale=self_size[0]/560
                return [m.Line('HELLO',[v*scale for v in [10,20,120,50]],.01,'fake')]
        with patch.object(m,'LocalPipeline',FakePipeline),patch.object(a.google,'read',side_effect=AssertionError('cloud called')):
            lines=a.cleaning_lines(a.image,b,('easy','en',False),a.cancel)
        self.assertFalse(calls[0]['neural']);self.assertFalse(calls[0]['retry'])
        self.assertEqual(lines[0]['box'],[90,90,200,120]);self.assertEqual(b.text,'original')
        self.assertEqual(self.errors,[])
