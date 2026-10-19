"""Build a self-contained Windows portable package.

    python tools/build_portable.py --python-zip python-3.12.8-embed-amd64.zip
    python tools/build_portable.py --python-zip py.zip --out dist --name MyStudio

Produces:
    <out>/<name>/runtime/       embedded CPython (extracted from the zip)
    <out>/<name>/app/           studio + core + config template + outputs/
    <out>/<name>/launch.bat     double-click launcher
    <out>/<name>/README.txt     quick start in Chinese
    <out>/<name>-win64.zip      the whole thing, ready to ship

The Windows embeddable distribution can be fetched from python.org or a mirror:
    https://mirrors.aliyun.com/python-release/windows/
"""

import argparse
import os
import shutil
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LAUNCHER = r"""@echo off
setlocal
cd /d "%~dp0"
title {title} - keep this window open

if not exist "runtime\python.exe" (
  echo [ERROR] runtime\python.exe not found. Unzip the whole package again.
  pause
  exit /b 1
)
if not exist "app\studio\server.py" (
  echo [ERROR] app\studio\server.py not found. Unzip the whole package again.
  pause
  exit /b 1
)

echo.
echo  ====================================================
echo    {title}
echo    Browser will open automatically.
echo    Keep this window open while you work.
echo  ====================================================
echo.

"runtime\python.exe" -u "app\studio\server.py" --port 8756 --open

echo.
echo Service stopped.
pause
"""

README = """{title}
====================================================

【三步上手】
1. 双击 launch.bat，会弹出一个命令行窗口，用的时候别关它
2. 浏览器自动打开工作台；没自动开就手动访问窗口里显示的地址
3. 首次运行会弹出设置面板，填三样东西：
     接口地址  https://api.openai.com/v1        （或你的中转地址）
     模型名    gpt-image-2.5-flare              （按服务商文档填）
     API Key   你的密钥
   保存后就能生图：写提示词，Ctrl+Enter。

【说明】
- 整个文件夹可以随意移动、拷到 U 盘、换电脑用，目标机不需要装 Python
- 顶栏可以切换模型；尺寸支持预设比例和自定义宽高
- 图片存在 app\\outputs\\，同名 .json 记录了提示词和参数
- 密钥只写在 app\\config.json，仅本机服务端读取
- 端口被占用会自动顺延，看窗口里打印的地址即可

【目录】
  runtime\\        内置 Python 运行时（别删）
  app\\studio\\     工作台服务与页面
  app\\core\\       接口客户端（可单独拿去用）
  app\\outputs\\    生成结果

【许可】MIT
"""


def copy_app(dst):
    app = os.path.join(dst, "app")
    os.makedirs(os.path.join(app, "studio"))
    os.makedirs(os.path.join(app, "core"))
    os.makedirs(os.path.join(app, "outputs"))
    for name in ("server.py", "index.html"):
        shutil.copy(os.path.join(ROOT, "studio", name), os.path.join(app, "studio", name))
    shutil.copy(os.path.join(ROOT, "core", "image_api.py"), os.path.join(app, "core", "image_api.py"))
    with open(os.path.join(app, "outputs", "README.txt"), "w", encoding="utf-8") as f:
        f.write("生成的图片保存在这里。\n")
    # 不在包里放任何 config.json：首次运行由设置面板生成
    return app


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--python-zip", required=True, help="python-*-embed-amd64.zip")
    ap.add_argument("--out", default="dist")
    ap.add_argument("--name", default="gpt-image-studio")
    ap.add_argument("--title", default="GPT Image Studio")
    args = ap.parse_args()

    dist = os.path.join(ROOT, args.out)
    pkg = os.path.join(dist, args.name)
    if os.path.isdir(pkg):
        shutil.rmtree(pkg)
    os.makedirs(pkg)

    with zipfile.ZipFile(args.python_zip) as z:
        z.extractall(os.path.join(pkg, "runtime"))
    print("runtime  ->", len(os.listdir(os.path.join(pkg, "runtime"))), "files")

    copy_app(pkg)
    print("app      -> ok")

    with open(os.path.join(pkg, "launch.bat"), "w", encoding="utf-8", newline="\r\n") as f:
        f.write(LAUNCHER.format(title=args.title))
    with open(os.path.join(pkg, "README.txt"), "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(README.format(title=args.title))

    zip_path = os.path.join(dist, "%s-win64.zip" % args.name)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for root, _dirs, files in os.walk(pkg):
            for fn in files:
                full = os.path.join(root, fn)
                z.write(full, os.path.join(args.name, os.path.relpath(full, pkg)))
    print("package  ->", zip_path, round(os.path.getsize(zip_path) / 1024 / 1024, 2), "MB")


if __name__ == "__main__":
    main()
