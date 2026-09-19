# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

root = Path(SPECPATH)
datas = [(str(root / 'eng.traineddata'), '.'),
         (str(root / 'mevo.traineddata'), '.'),
         (str(root / 'LICENSE'), 'licenses/springbok-connector'),
         (str(root / 'vendor/springbok-putting'), 'vendor/springbok-putting')]
if (root / 'vendor/flighthook').exists():
    datas.append((str(root / 'vendor/flighthook'), 'vendor/flighthook'))
a = Analysis([str(root / 'MevoCompanionEngine.py')], pathex=[str(root)], binaries=[],
             datas=datas, hiddenimports=['companion.ocr_adapter', 'companion.putting_adapter',
                                         'companion.flighthook_adapter', 'tesserocr'],
             excludes=['PySide6.QtWidgets', 'PySide6.QtQml', 'PySide6.QtQuick',
                       'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets'],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='MevoCompanionEngine',
          debug=False, strip=False, upx=False, console=True)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='MevoCompanionEngine')
