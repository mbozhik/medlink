"""Render the six PlantUML sources using the official PlantUML renderer.

PNG files are committed; reading the report never depends on a renderer.
Only run this command when editing diagram sources. Requires network access.
"""
import base64
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import urllib.request
import zlib

DIAGRAMS = Path(__file__).resolve().parents[1] / "docs" / "diagrams"
ENCODING = bytes.maketrans(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/",
    b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_",
)


def render(source):
    text = source.read_text().replace("!include _style.iuml", (DIAGRAMS / "_style.iuml").read_text())
    compressed = zlib.compress(text.encode("utf-8"))[2:-4]
    encoded = base64.b64encode(compressed).translate(ENCODING).rstrip(b"=").decode()
    url = "https://www.plantuml.com/plantuml/png/" + encoded
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 MedLink-diagram-render"})
    with urllib.request.urlopen(request, timeout=45) as response:
        image = response.read()
    if not image.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError(f"Renderer did not return PNG: {source.name}")
    source.with_suffix(".png").write_bytes(image)
    return f"{source.name}: {len(image)} bytes"


if __name__ == "__main__":
    with ThreadPoolExecutor(max_workers=3) as pool:
        for result in pool.map(render, sorted(DIAGRAMS.glob("*.puml"))):
            print(result)
