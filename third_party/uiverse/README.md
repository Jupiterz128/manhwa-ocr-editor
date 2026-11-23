# Uiverse visual references

Retrieved 2026-10-13. Both source pages provide the MIT license notices retained
verbatim in `button-LICENSE.txt` and `background-LICENSE.txt`.

- [Pill segmented tabs by uiverse-astronaut (Astronaut)](https://uiverse.io/uiverse-astronaut/odd-bobcat-3)
  — reference CSS in `button-reference.css`. Adapted capsule geometry, cloud/ink
  palette, soft elevation and visible focus into native ttk button skins. These
  are momentary action buttons, not radio tabs; original app actions are retained.
- [Grid pattern by adamgiebl (Adam Giebl)](https://uiverse.io/adamgiebl/curvy-earwig-79)
  — reference CSS in `background-reference.css`. Adapted #191a1a background and
  55 px repeating gradient lattice to bounded Tk canvas strokes. The CSS uses
  two lines per repeat; Tk uses 27.5 px spacing and a preblended gray line color.

Implementation: `app_ui.py` (`rounded_buttons`, `draw_workspace_grid`). The source
CSS is for attribution/reference, not executed or fetched by the application.
There is no browser renderer, network dependency, animation timer or background
modification of comic files. Fonts, group accents, shortcuts and 70/30 layout
are retained. Hover/press/focus are native Tk states rather than CSS transitions.

Additional reference reviewed on 2026-10-14:
[scroll snap cards by kamehame-ha](https://uiverse.io/kamehame-ha/ancient-insect-32).
The linked CSS implements mandatory vertical scroll snapping, not a custom
scrollbar. The user explicitly chose continuous scrolling and rounded scrollbars
instead. `RoundedScrollbar` is an independently written Tk vector control; no
scroll-snap CSS or card content is included or executed in the application.
