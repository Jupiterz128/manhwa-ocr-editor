function applyTextFX(doc, layer, data, photopea) {
    var stroke = data.stroke, grad = data.gradient;
    if (!(stroke && stroke.enabled) && !(grad && grad.enabled)) return;
    doc.activeLayer = layer;
    var c = function(key) { return photopea ? stringIDToTypeID(key.replace(/ +$/, "")) : charIDToTypeID(key); };
    function rgb(v) {
        var d = new ActionDescriptor();
        d.putDouble(c("Rd  "), v[0]); d.putDouble(c("Grn "), v[1]); d.putDouble(c("Bl  "), v[2]);
        return d;
    }
    function effect() {
        var d = new ActionDescriptor();
        d.putBoolean(c("enab"), true);
        d.putBoolean(stringIDToTypeID("present"), true);
        d.putBoolean(stringIDToTypeID("showInDialog"), true);
        d.putEnumerated(c("Md  "), c("BlnM"), c("Nrml"));
        d.putUnitDouble(c("Opct"), c("#Prc"), 100);
        return d;
    }
    var fx = new ActionDescriptor();
    fx.putUnitDouble(c("Scl "), c("#Prc"), 100);
    if (stroke && stroke.enabled) {
        var s = effect();
        s.putEnumerated(c("Styl"), c("FStl"), c("OutF"));
        s.putEnumerated(c("PntT"), c("FrFl"), c("SClr"));
        s.putUnitDouble(c("Sz  "), c("#Pxl"), stroke.size);
        s.putObject(c("Clr "), c("RGBC"), rgb(stroke.color));
        fx.putObject(c("FrFX"), c("FrFX"), s);
    }
    if (grad && grad.enabled) {
        var g = effect(), ramp = new ActionDescriptor(), colors = new ActionList(), alpha = new ActionList();
        ramp.putString(c("Nm  "), "Text gradient");
        ramp.putEnumerated(c("GrdF"), c("GrdF"), c("CstS"));
        ramp.putDouble(c("Intr"), 4096);
        for (var i = 0; i < 2; i++) {
            var stop = new ActionDescriptor(), trans = new ActionDescriptor();
            stop.putObject(c("Clr "), c("RGBC"), rgb(i ? grad.end_color : grad.start_color));
            stop.putEnumerated(c("Type"), c("Clry"), c("UsrS"));
            stop.putInteger(c("Lctn"), i*4096); stop.putInteger(c("Mdpn"), 50);
            colors.putObject(c("Clrt"), stop);
            trans.putUnitDouble(c("Opct"), c("#Prc"), 100);
            trans.putInteger(c("Lctn"), i*4096); trans.putInteger(c("Mdpn"), 50);
            alpha.putObject(c("TrnS"), trans);
        }
        ramp.putList(c("Clrs"), colors); ramp.putList(c("Trns"), alpha);
        g.putObject(c("Grad"), c("Grdn"), ramp);
        g.putUnitDouble(c("Angl"), c("#Ang"), grad.angle);
        g.putEnumerated(c("Type"), c("GrdT"), c("Lnr "));
        var off = new ActionDescriptor();
        off.putUnitDouble(c("Hrzn"), c("#Prc"), 0);
        off.putUnitDouble(c("Vrtc"), c("#Prc"), 0);
        g.putObject(c("Ofst"), c("Pnt "), off);
        g.putBoolean(c("Rvrs"), false);
        g.putBoolean(c("Dthr"), false);
        g.putBoolean(c("Algn"), true);
        g.putUnitDouble(c("Scl "), c("#Prc"), 100);
        fx.putObject(c("GrFl"), c("GrFl"), g);
    }
    var ref = new ActionReference(), cmd = new ActionDescriptor();
    ref.putProperty(c("Prpr"), c("Lefx"));
    ref.putEnumerated(c("Lyr "), c("Ordn"), c("Trgt"));
    cmd.putReference(c("null"), ref); cmd.putObject(c("T   "), c("Lefx"), fx);
    executeAction(c("setd"), cmd, DialogModes.NO);
}
