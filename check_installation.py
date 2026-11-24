"""Check component versions before opening Tk or loading any model."""
import ast
from pathlib import Path
import sys
EXPECTED='4.0'

def check(folder):
    failures=[]
    for name,key in [('manhwa_ocr.py','APP_VERSION'),('bubble_cleaner.py','COMPONENT_VERSION'),('clean_editor.py','COMPONENT_VERSION'),('clean_quality.py','COMPONENT_VERSION'),('clean_diagnostics.py','COMPONENT_VERSION'),('text_layout.py','COMPONENT_VERSION'),('text_editor.py','COMPONENT_VERSION'),('windows_text.py','COMPONENT_VERSION'),('photoshop_export.py','COMPONENT_VERSION'),('app_ui.py','COMPONENT_VERSION'),('sound_effects.py','COMPONENT_VERSION')]:
        try:
            tree=ast.parse((folder/name).read_text(encoding='utf-8'))
            versions=[ast.literal_eval(node.value) for node in tree.body if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id==key for t in node.targets)]
            if versions!=[EXPECTED]:failures.append(f'{name}: expected {EXPECTED}, got {versions or "OLD VERSION"}')
        except Exception as exc:failures.append(f'{name}: {exc}')
    if not (folder/'frame_geometry.py').is_file():failures.append('Missing frame_geometry.py')
    if not (folder/'photoshop_export.jsx').is_file():failures.append('Missing photoshop_export.jsx')
    return failures

if __name__=='__main__':
    folder=Path(__file__).resolve().parent;errors=check(folder)
    if errors:
        print('UPDATE INCOMPLETE. Copy ALL files from the 4.0 ZIP into this folder:')
        print(folder);print('\n'.join(errors));sys.exit(1)
    print('All components are 4.0. Starting detailed cleaner...')
