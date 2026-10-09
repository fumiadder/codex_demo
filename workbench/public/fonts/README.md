# 晚安故事本地字体

`zhixu-sleep-sans-sc-v1.woff2` 是 Google Fonts 中 Noto Sans SC 的本地子集，派生字体名称为 **Zhixu Sleep Sans**。它是中性、清晰的无衬线字体，不是阿里巴巴普惠体，也不是站酷快乐体。应用不向字体 CDN 发起请求。

字体文件为 1,720,228 字节，提供真实可变字重 **400–500**，默认 400；正文使用 400，标题使用 500。子集包含 GB2312 的全部 **6,763 个汉字**，并补充现有界面、内置原创故事中的字符以及拉丁字母和常用标点。它不包含全部 Unicode 汉字，联网故事中的其他生僻字由设备字体接续显示。`font-display: swap` 让页面先使用设备字体显示内容。

推荐样式：

```css
font-family: "Zhixu Sleep Sans", "PingFang SC", "Microsoft YaHei",
  "Noto Sans CJK SC", system-ui, sans-serif;
```

来源和文件 SHA-256 记录在 `FONT-SOURCE.json`。上游文件固定于 `google/fonts` 提交 `2eb0b48d5f760f62e286216f0859a8c540dbc1bd`，路径 `ofl/notosanssc/NotoSansSC[wght].ttf`；保留的完整授权为 `LICENSE-NOTO-SANS-SC.txt`。上游 Copyright 为 Adobe，SIL OFL 1.1 允许嵌入和派生；派生字体已更名，未使用保留字体名 “Source”。

子集制作使用 FontTools 4.61.1 和 Brotli 1.2.0，仅在开发时需要，部署运行不需要这两个 Python 包。处理顺序为：保留 GB2312、U+0020–024F、U+2000–206F、U+3000–303F、U+FF00–FFEF 和现有页面/故事字符；FontTools Subsetter 保留授权 name 表；`instantiateVariableFont` 将 `wght` 轴限制为 `(400, 400, 500)`；将字体 family、full name、PostScript 名称改为 Zhixu Sleep Sans；输出 WOFF2。

验证已重新解析 WOFF2 的 cmap、fvar 和命名字重，确认字重范围为 400–500、GB2312 汉字零缺失、许可证 name 记录仍保留。增加新的罕见文字时可重新制作子集，也可继续使用设备字体回退。
