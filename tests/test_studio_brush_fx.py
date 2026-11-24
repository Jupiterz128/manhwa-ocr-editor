import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image,ImageDraw,ImageFont
import web_api as studio
from manhwa_ocr import Page,Bubble
from text_layout import layout_text


@unittest.skipUnless(os.getenv('TEST_STUDIO_BROWSER')=='1','requires Chromium and Photopea')
class BrushAndStyleTests(unittest.TestCase):
    def test_solid_fill_mask_controls_and_export_feedback(self):
        from playwright.sync_api import sync_playwright
        import uvicorn
        with tempfile.TemporaryDirectory() as tmp,patch.object(studio,'ROOT',Path(tmp)/'studio'):
            page=Image.new('RGB',(800,1600),'white')
            draw=ImageDraw.Draw(page)
            font=ImageFont.truetype('arialbd.ttf',44)
            draw.text((200,250),'EEK!',font=font,fill='#c21875')
            path=Path(tmp)/'pink.png'
            page.save(path)
            bubble=Bubble([182,235,327,320],text='EEK!',translation='ว้าย!',lines=[{'box':[185,237,325,318]}])
            bubble.text_layer=layout_text(page,bubble)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0))
                server=uvicorn.Server(uvicorn.Config(studio.app,log_level='warning'))
                thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]});thread.start()
                try:
                    with sync_playwright() as pw:
                        browser=pw.chromium.launch()
                        for scale in (1,1.25):
                            with self.subTest(scale=scale):
                                key='pink'+str(scale)
                                studio.save(key,key,[Page(str(path),[bubble,Bubble([605,520,730,590],text='NEXT')])],{})
                                ctx=browser.new_context(viewport=dict(width=round(1920/scale),height=round(1080/scale)),device_scale_factor=scale)
                                tab=ctx.new_page()
                                tab.add_init_script(f"localStorage.setItem('studio-project','{key}')")
                                tab.goto(f'http://127.0.0.1:{sock.getsockname()[1]}')
                                tab.locator('#ink').wait_for()
                                tab.locator('[data-cmd=fx]').click()
                                tab.wait_for_function("document.querySelector('#ink').value==='#c21875'")
                                self.assertIn('B01',tab.locator('#notice').inner_text())
                                self.assertEqual(tab.locator('.bubble.selected').evaluate('e=>getComputedStyle(e).color'),'rgb(194, 24, 117)')
                                tab.locator('#percent').click()
                                tab.keyboard.press('m')
                                tab.wait_for_function("document.querySelector('#paint').dataset.ready==='yes'")
                                bar=tab.locator('#cleanPanel')
                                bar.wait_for(state='visible')
                                pos=bar.bounding_box()
                                frame=tab.locator('.bubble.selected').bounding_box()
                                viewport=tab.locator('#viewport').bounding_box()
                                self.assertAlmostEqual(pos['y'],frame['y']+frame['height']+14,delta=2)
                                for axis,span in [('x','width'),('y','height')]:
                                    self.assertGreaterEqual(pos[axis],viewport[axis])
                                    self.assertLessEqual(pos[axis]+pos[span],viewport[axis]+viewport[span])
                                grip=tab.locator('#maskGrip').bounding_box()
                                tab.mouse.move(grip['x']+5,grip['y']+5)
                                tab.mouse.down()
                                fps=tab.evaluate("""() => new Promise(resolve=>{
                                  let first,count=0;
                                  function step(t){
                                    first??=t;
                                    maskGrip.dispatchEvent(new PointerEvent('pointermove',{
                                      pointerId:barDrag.id,clientX:barDrag.viewport.left+30+count,
                                      clientY:barDrag.viewport.bottom-80,buttons:1}));
                                    if(++count===61)resolve(60000/(t-first));else requestAnimationFrame(step)
                                  }
                                  requestAnimationFrame(step)
                                })""")
                                tab.mouse.up()
                                pinned=bar.bounding_box()
                                self.assertGreater(abs(pinned['y']-pos['y']),20)
                                print(f'Mask toolbar drag {scale*100:.0f}%: {fps:.1f} frames/s',flush=True)
                                tab.locator('#eraser').click()
                                self.assertIn('active',tab.locator('#eraser').get_attribute('class'))
                                tab.locator('#brush').click()
                                stage=tab.locator('#stage').bounding_box()
                                tab.mouse.move(stage['x']+420,stage['y']+275)
                                radius=int(tab.locator('#radius').input_value())
                                tab.keyboard.down('Control');tab.mouse.wheel(0,-100);tab.keyboard.up('Control')
                                tab.wait_for_function(f"Number(document.querySelector('#radius').value)>{radius}")
                                self.assertTrue(tab.locator('#brushCursor').is_visible())
                                tab.keyboard.down('Space');tab.mouse.down();tab.mouse.move(stage['x']+420,stage['y']+220);tab.mouse.up();tab.keyboard.up('Space')
                                self.assertAlmostEqual(bar.bounding_box()['y'],pinned['y'],delta=1)
                                self.assertGreater(tab.locator('#viewport').evaluate('e=>e.scrollTop'),0)
                                stage=tab.locator('#stage').bounding_box()
                                tab.mouse.move(stage['x']+450,stage['y']+275);tab.mouse.down()
                                tab.mouse.move(stage['x']+470,stage['y']+275,steps=5)
                                self.assertGreater(tab.locator('#paint').evaluate("e=>e.getContext('2d').getImageData(462,275,1,1).data[3]"),0)
                                fps=tab.evaluate("""() => new Promise(resolve => {
                                    const port=document.getElementById('viewport'),stage=document.getElementById('stage').getBoundingClientRect();
                                    let first=0,count=0;
                                    function frame(t) {
                                        if(!first)first=t;
                                        port.dispatchEvent(new PointerEvent('pointermove',{
                                            clientX:stage.x+465+3*Math.sin(count),clientY:stage.y+275,buttons:1}));
                                        if(++count===61)resolve(60000/(t-first));else requestAnimationFrame(frame);
                                    }
                                    requestAnimationFrame(frame);
                                })""")
                                print(f'Brush Chromium {scale*100:.0f}%: {fps:.1f} frames/s',flush=True)
                                tab.mouse.up()
                                tab.wait_for_function("strokeWrites.then(()=>true)")
                                reg=studio.mask_sessions[key,0,bubble.id]['region']
                                self.assertGreater(reg.box[2],470)
                                self.assertEqual(reg.mask[275-reg.box[1],462-reg.box[0]],255)
                                with tab.expect_response(lambda r:r.url.endswith('/mask_undo') and r.ok):
                                    bar.locator('[data-cmd=maskUndo]').click()
                                with tab.expect_response(lambda r:r.url.endswith('/mask_start') and r.ok):
                                    tab.locator('#layers .layer-main').nth(1).click()
                                bar.wait_for(state='visible')
                                tab.wait_for_timeout(210)
                                next_frame=tab.locator('.bubble.selected').bounding_box()
                                self.assertAlmostEqual(bar.bounding_box()['y'],next_frame['y']+next_frame['height']+14,delta=2)
                                tab.locator('[data-cmd=maskCancel]').click()
                                tab.locator('#cleanPanel').wait_for(state='hidden')
                                self.assertNotIn(bubble.id,studio.load(key)[1])
                                tab.locator('[data-cmd=export]').click()
                                tab.locator('#modal button.primary').click()
                                self.assertTrue(tab.locator('#modal .working').is_disabled())
                                self.assertIn('กำลังส่งออก',tab.locator('#modal .working').inner_text())
                                self.assertTrue(tab.locator('#modal progress').is_visible())
                                tab.locator('#exportStatus.export-ok').wait_for(timeout=120000)
                                self.assertIn('100%',tab.locator('#exportStatus').inner_text())
                                self.assertTrue(tab.locator('#notice .btn-reveal').is_visible())
                                ctx.close()
                        browser.close()
                finally:server.should_exit=True;thread.join(10)

class StudioAuditTests(unittest.TestCase):
    def test_stitch_creates_independent_project_and_legacy_copy(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(studio,'ROOT',Path(tmp)/'store'):
            img=Path(tmp)/'01.png'
            Image.new('RGB',(180,300),'white').save(img)
            studio.save('parent','test',[Page(str(img)),Page(str(img))],{})
            out=studio.stitch_project('parent',{'width':180,'height':600})
            self.assertNotEqual(out['key'],'parent')
            self.assertEqual(len(studio.load('parent')[0]),2)
            self.assertTrue(all(Path(p.path).is_file() for p in studio.load(out['key'])[0]))
            with patch.object(studio.subprocess,'Popen') as launch:
                result=studio.open_advanced('parent')
                self.assertEqual(launch.call_args.args[0][-2:],['--project',result['path']])
                pages,_,_,_=studio._load_json_project_file(Path(result['path']))
                self.assertEqual(len(pages),2)
