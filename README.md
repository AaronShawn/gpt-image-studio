# GPT Image Studio

**本地生图工作台 · 自带 API Key · 兼容任何 OpenAI 格式的图像接口**

一个单文件就能跑起来的本地 Web 工作台：左边写提示词，右边出图，图自动落盘。
支持文生图和参考图编辑，前端零构建、后端零依赖，附带 ComfyUI 节点。

![界面截图](docs/screenshot.png)

---

## 特性

- **零依赖**：只用 Python 标准库。不需要 `pip install`，官方 CPython / Windows 嵌入式版 / ComfyUI 内置 Python 都能直接跑
- **自带 Key**：直连 OpenAI 官方，或任何同协议的第三方中转，不经过第三方计费
- **两种模式**：文生图（`/images/generations`）与参考图编辑（`/images/edits`，最多 16 张参考图）
- **一把 Key 管多个模型**：顶栏下拉直接切换模型，尺寸支持预设比例与自定义宽高；如果你的网关提供 Auto 分组，同一个 Key 就能在普通档和高分辨率档之间来回切
- **本地留存**：图片存 `outputs/`，同名 `.json` 记录提示词和参数；历史面板可回看最近 60 张
- **密钥不出机器**：Key 只写进本机 `config.json`，仅服务端读取，不会出现在页面、前端源码或日志里
- **可打包成便携版**：内置 Python 运行时，拷到 U 盘双击即用，目标机不需要装任何东西

## 快速开始

### 方式一：源码运行（需要 Python 3.9+）

```bash
git clone https://github.com/AaronShawn/gpt-image-studio.git
cd gpt-image-studio
python studio/server.py --open
```

浏览器会打开 `http://127.0.0.1:8756`。首次运行会自动弹出设置面板：

![设置面板](docs/settings.png)

填三项就能用。**不确定模型名怎么写？** 填好地址和 Key 后点「拉取模型列表」，
程序会请求该网关的 `/v1/models`，直接把可用的模型名列出来给你选。

| 字段 | 说明 | 例子 |
|---|---|---|
| 接口地址 `base_url` | 到 `/v1` 为止 | `https://api.openai.com/v1` |
| 模型名 `model` | 对方文档给的模型 ID | `gpt-image-2.5-flare` |
| API Key | 你的密钥 | `sk-...` |

端口被占用会自动往后顺延，控制台会打印真实地址。

### 方式二：Windows 便携版

到 [Releases](../../releases) 下载 `gpt-image-studio-*-win64.zip`，解压后双击 `一键启动.bat`。
包内自带 Python 3.12 运行时，不需要安装任何东西。

### 方式三：ComfyUI 节点

把 `comfyui/ComfyUI-GPTImageStudio` 整个文件夹复制到 `ComfyUI/custom_nodes/`，重启 ComfyUI，
节点库搜索 `GPT Image`，出现「文生图」和「参考图编辑」两个节点。

## 参数说明

| 界面控件 | 接口字段 | 取值 | 作用 |
|---|---|---|---|
| 模式页签 | 决定接口 | `/images/generations`、`/images/edits` | 编辑模式才有参考图和保真度 |
| 提示词 | `prompt` | 文本 | 唯一决定画面内容的输入 |
| 参考图 | `image`（重复字段，文件上传） | ≤16 张 | 第一张是主体，顺序有意义 |
| 保真度 | `input_fidelity` | `high` / `low` | 高则尽量不动原图，低则允许重画 |
| 尺寸 | `size` | `auto` 或 `宽x高` | 只决定比例，实际像素由模型决定 |
| 质量 | `quality` | `low` / `medium` / `high` / `auto` | 细节与耗时 |
| 背景 | `background` | 不传 / `opaque` / `transparent` | 透明背景输出带 alpha 的 PNG |
| 返回格式 | `response_format` | `b64_json` / `url` | 接程序建议 `b64_json` |

**默认值**：尺寸 `auto`、质量 `high`、保真度 `high`、背景不传、返回 `b64_json`。

### 关于分辨率

`size` 只影响**画面比例**，不是精确像素。多数图像模型会把输出量化到固定像素档位
（常见约 1.5 MP），不同服务商、不同计费档的档位不一样。填 `3840x2160` 不一定真给你 4K，
判断真实尺寸请看图片文件本身，而不是响应里的 `size` 字段 —— 有些网关会把请求值原样回显。

小技巧：
- 竖版用标准值（如 `1080x1920`），短边太小的尺寸可能被服务端丢弃并回退到默认横版
- 先 `quality=low` 试构图，定了再 `high` 重跑同一段提示词

## 目录结构

```
gpt-image-studio/
├─ studio/
│  ├─ server.py          # 本地 HTTP 服务（标准库）
│  └─ index.html         # 单页界面（无框架、无构建）
├─ core/
│  └─ image_api.py       # 接口客户端：generate / edit / save
├─ comfyui/
│  └─ ComfyUI-GPTImageStudio/   # ComfyUI 自定义节点
├─ tools/
│  └─ build_portable.py  # 打包免安装 Windows 便携版
└─ docs/
```

## 自己打包便携版

```bash
python tools/build_portable.py \
  --python-zip /path/to/python-3.12.8-embed-amd64.zip \
  --out dist
```

`python-*-embed-amd64.zip` 可从 python.org 或国内镜像（如
`https://mirrors.aliyun.com/python-release/windows/`）获取。脚本会组装 `runtime/ + app/ + 启动器` 并压成 zip。

## 常见问题

| 现象 | 原因 |
|---|---|
| 页面提示没有 API Key | 点右上角「设置」填好三项并保存 |
| 400 / 模型不存在 | `model` 填错，必须是对方文档里的模型 ID |
| 图片一直转圈 | 单张通常 20–45 秒，编辑更久；超 3 分钟检查网络能否访问网关 |
| 返回的 `url` 打不开 | 部分网关的 CDN 校验 UA 或链接有有效期，改用 `b64_json` |
| 想让 ComfyUI 用官方积分节点 | 那个节点走 Comfy 账号计费，不支持自带 Key，与本项目无关 |

## 许可

[MIT](LICENSE)
