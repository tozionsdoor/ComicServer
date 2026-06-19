"""PyAVのCython純Pythonモードソース(.py)を一時的に退避/復元する。

avパッケージの一部モジュール(av/audio/frame.py等)はCythonの
"pure python mode"ソースとして配布されており、wheelビルド時に
.pydへコンパイルされた後は実行時に使われない想定だが、Nuitkaが
ビルド時にこの.pyソースを誤って採用し、`import cython`が解決
できずクラッシュする問題がある(av.audio.frame等17ファイル)。

Nuitkaにこの.pyを見せないようにするため、ビルド前に.py.nuitkabak
へリネームして隠し、ビルド後に元へ戻す。
"""
import importlib.util
import os
import sys

_REL_PATHS = [
    "audio/codeccontext.py",
    "audio/format.py",
    "audio/frame.py",
    "audio/plane.py",
    "audio/resampler.py",
    "audio/stream.py",
    "container/output.py",
    "filter/loudnorm.py",
    "frame.py",
    "packet.py",
    "stream.py",
    "subtitles/codeccontext.py",
    "subtitles/stream.py",
    "subtitles/subtitle.py",
    "utils.py",
    "video/frame.py",
    "video/stream.py",
]

_BAK_SUFFIX = ".nuitkabak"


def _av_dir():
    spec = importlib.util.find_spec("av")
    return os.path.dirname(spec.origin)


def strip(av_dir):
    for rel in _REL_PATHS:
        src = os.path.join(av_dir, rel)
        bak = src + _BAK_SUFFIX
        if os.path.exists(src):
            os.replace(src, bak)
            print(f"stripped: {rel}")


def restore(av_dir):
    for rel in _REL_PATHS:
        src = os.path.join(av_dir, rel)
        bak = src + _BAK_SUFFIX
        if os.path.exists(bak):
            os.replace(bak, src)
            print(f"restored: {rel}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    av_dir = _av_dir()
    if mode == "strip":
        strip(av_dir)
    elif mode == "restore":
        restore(av_dir)
    else:
        print("usage: nuitka_strip_av_cython.py [strip|restore]")
        sys.exit(1)
