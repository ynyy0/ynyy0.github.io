# 插件图标库

将每个插件的图标 PNG 放在此目录，文件名使用插件包 ID：

```text
icons/<Package-ID>.png
```

例如：

```text
icons/iya.banana.ios27animation.png
```

推送 `icons/` 下的图片后，GitHub Actions 会重新生成 APT 索引，并为对应插件条目添加 `Icon: https://ynyy0.github.io/icons/<Package-ID>.png`。没有对应图片文件时不会写入 `Icon:` 字段。

iOS27 Animation 的原始图标可从 [Banana 源](https://iyaway.github.io/depictions/iya.banana.ios27animation/icon.png) 获取。将图片保存为 `iya.banana.ios27animation.png` 后上传到本目录即可。
