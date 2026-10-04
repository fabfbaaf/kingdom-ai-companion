"""Bundle the Windows x64 default PortAudio backend for our portable app."""

from pathlib import Path

from PyInstaller.utils.hooks import get_module_file_attribute

library = (Path(get_module_file_attribute("sounddevice")).parent
           / "_sounddevice_data" / "portaudio-binaries" / "libportaudio64bit.dll")
if not library.is_file():
    raise RuntimeError("Windows x64 non-ASIO PortAudio library is missing")
binaries = [(str(library), "_sounddevice_data/portaudio-binaries")]
hiddenimports = ["_sounddevice_data", "_cffi_backend"]
