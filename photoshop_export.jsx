/* Native editable PSD writer. Run only through photoshop_export.py with a generated job. */
(function () {
    function encode(v) {
        if (v === null) return 'null';
        if (typeof v === 'string') return '"' + v.replace(/[\\"\u0000-\u001f]/g, function(c) {
            var n=c.charCodeAt(0).toString(16);return '\\u'+('0000'+n).slice(-4);
        }) + '"';
        if (typeof v === 'number' || typeof v === 'boolean') return String(v);
        var a=[], k;
        if (v instanceof Array) {for(k=0;k<v.length;k++) a.push(encode(v[k]));return '['+a.join(',')+']';}
        for(k in v) if(v.hasOwnProperty(k)) a.push(encode(k)+':'+encode(v[k]));
        return '{'+a.join(',')+'}';
    }
    function cancelled() {if (new File(job.cancel_file).exists) throw new Error('CANCELLED');}
    function bounds(layer) {var b=layer.bounds;return [b[0].as('px'),b[1].as('px'),b[2].as('px'),b[3].as('px')];}
    var previous=null, document=null, opened=null;
    var oldDialogs=app.displayDialogs, oldUnits=app.preferences.rulerUnits, oldType=app.preferences.typeUnits;
    var report={ok:false,photoshop:app.version,text_layers:[],clean_layers:0};
    try {
        if(app.documents.length) previous=app.activeDocument;
        app.displayDialogs=DialogModes.NO;
        app.preferences.rulerUnits=Units.PIXELS;app.preferences.typeUnits=TypeUnits.POINTS;
        cancelled();
        if(job.texts.length) {
            var fontFound=false;
            for(var f=0;f<app.fonts.length;f++) if(app.fonts[f].postScriptName===job.font) {fontFound=true;break;}
            if(!fontFound) throw new Error('Photoshop cannot find '+job.font+'. Restart Photoshop after installing the font.');
        }
        document=app.open(new File(job.background));
        document.resizeImage(undefined,undefined,72,ResampleMethod.NONE);
        document.flatten();document.activeLayer.name='Background';
        for(var i=0;i<job.patches.length;i++) {
            cancelled();var patch=job.patches[i];
            opened=app.open(new File(patch.path));
            var added=opened.activeLayer.duplicate(document,ElementPlacement.PLACEATBEGINNING);
            opened.close(SaveOptions.DONOTSAVECHANGES);opened=null;app.activeDocument=document;
            added.name=patch.name;
            var b=bounds(added);added.translate(patch.left-b[0],patch.top-b[1]);
            report.clean_layers++;
        }
        var textGroup=null;
        if(job.texts.length) {textGroup=document.layerSets.add();textGroup.name='TEXT - Thai';}
        var black=new SolidColor();black.rgb.red=0;black.rgb.green=0;black.rgb.blue=0;
        for(var t=0;t<job.texts.length;t++) {
            cancelled();var data=job.texts[t], rect=data.box;
            var fitWidth=data.text_size ? data.text_size[0] : rect[2]-rect[0];
            var fitHeight=data.text_size ? data.text_size[1] : rect[3]-rect[1];
            var layer=textGroup.artLayers.add();layer.name=data.name;layer.kind=LayerKind.TEXT;
            var item=layer.textItem;item.kind=TextType.POINTTEXT;
            item.contents=data.text.replace(/\n/g,'\r');item.font=job.font;
            var ink=new SolidColor();ink.rgb.red=data.color[0];ink.rgb.green=data.color[1];ink.rgb.blue=data.color[2];
            item.color=ink;item.justification=Justification.CENTER;
            item.fauxBold=false;item.fauxItalic=false;
            item.horizontalScale=100;item.verticalScale=100;item.tracking=0;item.baselineShift=0;
            item.autoKerning=AutoKernType.METRICS;item.language=Language.ENGLISHUK;
            item.useAutoLeading=true;item.autoLeadingAmount=120;
            item.antiAliasMethod=AntiAlias.SHARP;
            item.position=[(rect[0]+rect[2])/2,rect[1]+data.font_size];
            var size=data.font_size, fits=false;
            for(var step=0;step<40;step++) {
                cancelled();item.size=new UnitValue(size,'pt');
                b=bounds(layer);
                if(b[2]-b[0]<=fitWidth && b[3]-b[1]<=fitHeight) {fits=true;break;}
                if(size<=8) break;
                var ratio=Math.min(fitWidth/Math.max(1,b[2]-b[0]),fitHeight/Math.max(1,b[3]-b[1]));
                size=Math.max(8,Math.floor(Math.min(size-.5,size*ratio*.97)*2)/2);
            }
            if(!fits) throw new Error('Text does not fit: '+data.name);
            layer.translate((rect[0]+rect[2]-b[0]-b[2])/2,(rect[1]+rect[3]-b[1]-b[3])/2);
            if(data.rotation) {
                layer.rotate(data.rotation,AnchorPosition.MIDDLECENTER);
                b=bounds(layer);
                layer.translate((rect[0]+rect[2]-b[0]-b[2])/2,(rect[1]+rect[3]-b[1]-b[3])/2);
            }
            b=bounds(layer);
            if(item.font!==job.font || !item.useAutoLeading) throw new Error('Text style was not preserved: '+data.name);
            if(item.tracking!==0 || item.autoKerning!==AutoKernType.METRICS || item.antiAliasMethod!==AntiAlias.SHARP ||
                item.horizontalScale!==100 || item.verticalScale!==100 || item.baselineShift.as('pt')!==0 || item.language!==Language.ENGLISHUK)
                throw new Error('Character settings were not preserved: '+data.name);
            report.text_layers.push({id:data.id,name:layer.name,text:item.contents,font:item.font,
                font_size:item.size.as('pt'),auto_leading:item.useAutoLeading,color:item.color.rgb.hexValue,bounds:b,box:rect,rotation:data.rotation || 0,
                tracking:item.tracking,kerning:String(item.autoKerning),antialias:String(item.antiAliasMethod),
                horizontal_scale:item.horizontalScale,vertical_scale:item.verticalScale,baseline_shift:item.baselineShift.as('pt'),
                language:String(item.language),auto_leading_amount:item.autoLeadingAmount});
        }
        cancelled();
        var options=new PhotoshopSaveOptions();options.layers=true;options.embedColorProfile=true;
        options.alphaChannels=true;
        document.saveAs(new File(job.output),options,true,Extension.LOWERCASE);
        report.ok=true;
    } catch(error) {
        report.error=String(error);report.line=error.line || 0;
    } finally {
        if(opened) {try {opened.close(SaveOptions.DONOTSAVECHANGES);} catch(ignored) {}}
        if(document) {try {document.close(SaveOptions.DONOTSAVECHANGES);} catch(ignored2) {}}
        app.preferences.rulerUnits=oldUnits;app.preferences.typeUnits=oldType;app.displayDialogs=oldDialogs;
        if(previous) {try {app.activeDocument=previous;} catch(ignored3) {}}
        var file=new File(job.report);file.encoding='UTF8';file.open('w');file.write(encode(report));file.close();
    }
})();
