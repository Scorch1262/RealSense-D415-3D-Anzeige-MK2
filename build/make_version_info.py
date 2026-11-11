"""Liest APP_VERSION aus app.py, schreibt build/version_info.txt (Windows-Versionsressource)
und gibt 'version=x.y.z' für $GITHUB_OUTPUT aus."""
import re
from pathlib import Path

root = Path(__file__).resolve().parent.parent
src = (root / "app.py").read_text(encoding="utf-8")
version = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', src).group(1)
name = re.search(r'APP_NAME\s*=\s*"([^"]+)"', src).group(1)
nums = [int(x) for x in re.findall(r"\d+", version)][:4]
nums += [0] * (4 - len(nums))
t = tuple(nums)

(root / "build" / "version_info.txt").write_text(f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(filevers={t}, prodvers={t}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040704B0', [
      StringStruct('FileDescription', '{name} - 3D-Ansicht und Raumscan fuer Intel RealSense D415 (Desktop)'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', '{name}'),
      StringStruct('OriginalFilename', '{name}.exe'),
      StringStruct('ProductName', '{name}'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [0x0407, 1200])])
  ]
)
""", encoding="utf-8")
print(f"version={version}")
