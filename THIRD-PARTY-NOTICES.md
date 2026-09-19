# Third-party components

Mevo Companion is a derivative of springbok/MLM2PRO-GSPro-Connector and retains its GPL-3.0 license. See LICENSE. The original source remains in src/; the companion implementation is in companion/ and desktop/.

- Springbok connector: https://github.com/springbok/MLM2PRO-GSPro-Connector — GPL-3.0.
- Springbok-distributed cam-putting: see vendor/springbok-putting/ for the exact binary, GPL license, pinned source and provenance. The ball-detection and putting algorithm are unmodified.
- Tesseract OCR / English trained data: https://github.com/tesseract-ocr/tesseract and https://github.com/tesseract-ocr/tessdata_fast — Apache-2.0. The English model is used for automatic FS Golf labels.
- tesserocr: https://github.com/sirfz/tesserocr — MIT.
- OpenCV: https://opencv.org/license/ — Apache-2.0 for the bundled version.
- PySide6 / Qt: https://www.qt.io/licensing/open-source-lgpl-obligations — see the distributed Qt/PySide license files. The integration worker uses Qt Core; it is separate from the WPF user interface.
- Python: https://docs.python.org/3/license.html — PSF license.
- NumPy: https://numpy.org/doc/stable/license.html — BSD-3-Clause.
- Pillow: https://github.com/python-pillow/Pillow — HPND.
- psutil: https://github.com/giampaolo/psutil — BSD-3-Clause.
- websocket-client: https://github.com/websocket-client/websocket-client — Apache-2.0.
- cv2-enumerate-cameras: https://github.com/chinaheyu/cv2_enumerate_cameras — see bundled package metadata/license.
- .NET and WPF: https://github.com/dotnet/wpf — MIT.

GSPro and FS Golf are separate products. Their application binaries, licenses and accounts are not included. Mevo Companion is an independent community application, not a FlightScope or GSPro product.

Development dependencies and full licenses must accompany redistributed source/binary releases. The local build is unsigned because no code-signing certificate is configured.
