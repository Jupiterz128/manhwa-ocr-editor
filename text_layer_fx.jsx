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
        function parseStopRgb(col) {
            if (col instanceof Array && col.length >= 3) return [Number(col[0]) || 0, Number(col[1]) || 0, Number(col[2]) || 0];
            var hex = String(col || "#FFFFFF").replace(/^#/, "");
            if (hex.length === 3) hex = hex.charAt(0)+hex.charAt(0)+hex.charAt(1)+hex.charAt(1)+hex.charAt(2)+hex.charAt(2);
            return [parseInt(hex.slice(0, 2), 16) || 0, parseInt(hex.slice(2, 4), 16) || 0, parseInt(hex.slice(4, 6), 16) || 0];
        }
        var blendMap = { normal: "Nrml", multiply: "Mltp", screen: "Scrn", overlay: "Ovrl" };
        var styleMap = { linear: "Lnr ", radial: "Rdl ", reflected: "Rflc" };
        var bKey = blendMap[String(grad.blend_mode || "normal").toLowerCase()] || "Nrml";
        var tKey = styleMap[String(grad.style || "linear").toLowerCase()] || "Lnr ";
        var opct = (typeof grad.opacity === "number") ? Math.max(0, Math.min(100, grad.opacity)) : 100;
        var scl = (typeof grad.scale === "number") ? Math.max(10, Math.min(150, grad.scale)) : 100;
        var psAngle = (typeof grad.angle === "number") ? grad.angle : (typeof grad.css_angle === "number" ? (90 - grad.css_angle) : 90);
        while (psAngle > 180) psAngle -= 360;
        while (psAngle < -180) psAngle += 360;

        var g = effect(), ramp = new ActionDescriptor(), colors = new ActionList(), alpha = new ActionList();
        g.putEnumerated(c("Md  "), c("BlnM"), c(bKey));
        g.putUnitDouble(c("Opct"), c("#Prc"), opct);
        ramp.putString(c("Nm  "), "Text gradient");
        ramp.putEnumerated(c("GrdF"), c("GrdF"), c("CstS"));
        ramp.putDouble(c("Intr"), 4096);

        var rawStops = (grad.stops && grad.stops.length >= 2) ? grad.stops : [
            { pos: 0, color: grad.start_color || [255, 255, 255] },
            { pos: 100, color: grad.end_color || [200, 200, 200] }
        ];
        var sortedStops = [];
        for (var si = 0; si < rawStops.length; si++) sortedStops.push(rawStops[si]);
        for (var a = 0; a < sortedStops.length; a++) {
            for (var b = a + 1; b < sortedStops.length; b++) {
                if ((Number(sortedStops[b].pos) || 0) < (Number(sortedStops[a].pos) || 0)) {
                    var tmpStop = sortedStops[a];
                    sortedStops[a] = sortedStops[b];
                    sortedStops[b] = tmpStop;
                }
            }
        }
        for (var i = 0; i < sortedStops.length; i++) {
            var st = sortedStops[i];
            var loc = Math.max(0, Math.min(4096, Math.round(((Number(st.pos) || 0) / 100) * 4096)));
            var stop = new ActionDescriptor();
            stop.putObject(c("Clr "), c("RGBC"), rgb(parseStopRgb(st.color)));
            stop.putEnumerated(c("Type"), c("Clry"), c("UsrS"));
            stop.putInteger(c("Lctn"), loc);
            stop.putInteger(c("Mdpn"), 50);
            colors.putObject(c("Clrt"), stop);
        }
        for (var ti = 0; ti < 2; ti++) {
            var trans = new ActionDescriptor();
            trans.putUnitDouble(c("Opct"), c("#Prc"), 100);
            trans.putInteger(c("Lctn"), ti * 4096);
            trans.putInteger(c("Mdpn"), 50);
            alpha.putObject(c("TrnS"), trans);
        }
        ramp.putList(c("Clrs"), colors); ramp.putList(c("Trns"), alpha);
        g.putObject(c("Grad"), c("Grdn"), ramp);
        g.putUnitDouble(c("Angl"), c("#Ang"), psAngle);
        g.putEnumerated(c("Type"), c("GrdT"), c(tKey));
        var off = new ActionDescriptor();
        off.putUnitDouble(c("Hrzn"), c("#Prc"), 0);
        off.putUnitDouble(c("Vrtc"), c("#Prc"), 0);
        g.putObject(c("Ofst"), c("Pnt "), off);
        g.putBoolean(c("Rvrs"), Boolean(grad.reverse));
        g.putBoolean(c("Dthr"), false);
        g.putBoolean(c("Algn"), grad.align_with_layer !== false);
        g.putUnitDouble(c("Scl "), c("#Prc"), scl);
        fx.putObject(c("GrFl"), c("GrFl"), g);
    }
    var ref = new ActionReference(), cmd = new ActionDescriptor();
    ref.putProperty(c("Prpr"), c("Lefx"));
    ref.putEnumerated(c("Lyr "), c("Ordn"), c("Trgt"));
    cmd.putReference(c("null"), ref); cmd.putObject(c("T   "), c("Lefx"), fx);
    executeAction(c("setd"), cmd, DialogModes.NO);
}
