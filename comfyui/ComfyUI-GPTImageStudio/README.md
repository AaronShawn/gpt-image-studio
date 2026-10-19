# ComfyUI-GPTImageStudio

用你自己的 Key 调 OpenAI 格式的图像接口，在 ComfyUI 里直接出图。

## 安装

把本文件夹整体复制到 `ComfyUI/custom_nodes/` 下，重启 ComfyUI。节点出现在 **GPT Image Studio** 分类。

## 节点

| 节点 | 输入 | 输出 |
|---|---|---|
| GPT Image · 文生图 | prompt / size / quality / background / model / base_url / api_key / seed | IMAGE, file_path, usage |
| GPT Image · 参考图编辑 | prompt / image1–image4 / size / quality / input_fidelity / model / base_url / api_key / seed | IMAGE, file_path, usage |

`IMAGE` 输出可继续接下游节点；同时会落盘到 `ComfyUI/output/gptimage-*.png`。

## 配置

三个来源，优先级从高到低：

1. 节点上的 `model` / `base_url` / `api_key` 字段
2. 环境变量 `IMAGE_API_KEY`
3. 本目录下的 `config.json`

`config.json` 示例（该文件已在 .gitignore 中，不会被提交）：

```json
{
  "base_url": "https://api.openai.com/v1",
  "model": "gpt-image-2.5-flare",
  "api_key": "sk-..."
}
```

## 注意

- `seed` 变了才会重新请求，否则命中 ComfyUI 缓存 —— 想省钱就别乱动 seed
- 一次请求出一张图，单张 20–45 秒，编辑更久
- `size` 只决定比例；`auto` 让模型按提示词决定
