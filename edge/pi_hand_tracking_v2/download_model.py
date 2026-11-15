"""Download the official MediaPipe Hand Landmarker task bundle (not a classifier)."""
from pathlib import Path
from urllib.request import urlopen

URL = "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/latest/hand_landmarker.task"
TARGET = Path(__file__).resolve().parent / "models" / "hand_landmarker.task"


def main():
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    if TARGET.exists() and TARGET.stat().st_size > 100000:
        print(f"Model already available: {TARGET}")
        return
    tmp = TARGET.with_suffix(".part")
    try:
        with urlopen(URL, timeout=40) as response, tmp.open("wb") as file:
            while True:
                data = response.read(1024 * 1024)
                if not data:
                    break
                file.write(data)
        if tmp.stat().st_size < 100000:
            raise RuntimeError("Downloaded model is unexpectedly small")
        tmp.replace(TARGET)
        print(f"Saved {TARGET} ({TARGET.stat().st_size / 1e6:.1f} MB)")
    finally:
        tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
