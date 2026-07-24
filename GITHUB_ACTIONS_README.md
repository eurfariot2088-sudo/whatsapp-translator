# 云端打包说明（GitHub Actions）

通过 GitHub Actions 在微软 Windows 服务器上自动打包，**你自己的电脑不用装 Python、不用装任何东西**，打包完成后直接从网页下载 `.exe`。

## 一次性准备

1. 注册 / 登录 [github.com](https://github.com)（免费账号即可）。
2. 点击右上角 `+` → **New repository**，名字随便取（比如 `whatsapp-translator`），选 **Private** 或 Public 都行，**不要**勾选 "Initialize with README"。
3. 把本目录所有文件上传到这个仓库：
   - 最简单：用浏览器在仓库页面点 `Add file → Upload files`，把 `whatsapp-translator/` 里**所有文件和文件夹**拖进去提交。
   - 注意：`.github` 是隐藏文件夹，浏览器上传时会保留目录结构，确保 `.github/workflows/build-exe.yml` 路径正确。

## 触发打包（三种方式任选）

### 方式一：手动触发（最简单，推荐）

1. 进入你的仓库页面。
2. 点击顶部 **Actions** 标签页。
3. 左侧选择 **Build Windows EXE**。
4. 右侧点击 **Run workflow** → 选择 `main` 分支 → 点击绿色 **Run workflow** 按钮。
5. 等待 2~3 分钟，构建完成会出现绿色 ✅。

### 方式二：推送代码自动触发

每次 `git push` 到 `main` 分支会自动触发打包。

### 方式三：打 Tag 触发（会附到 Release）

```bash
git tag v1.0
git push origin v1.0
```

这种方式会把 `WhatsAppTranslator.exe` 作为附件发布到 GitHub Releases 页面，**永久下载链接**。

## 下载 exe

### 构建制品（保留 90 天）

1. 进入 **Actions** 标签页。
2. 点击最新一次成功的 **Build Windows EXE** 运行。
3. 拉到页面最底部 **Artifacts** 区域。
4. 点击 **WhatsAppTranslator-exe** 下载，得到一个 zip，解压后就是 `WhatsAppTranslator.exe`。

### Release 制品（永久，需打 Tag 触发）

1. 进入仓库右侧 **Releases** 页面。
2. 点击对应版本，下载附件里的 `WhatsAppTranslator.exe`。

## 常见问题

**Q：构建失败怎么办？**
- 点进失败的运行，展开红色步骤看日志。
- 大部分是依赖安装问题，检查 `requirements.txt` 是否完整上传。

**Q：下载的 exe 提示 "Windows 已保护你的电脑"？**
- 这是 SmartScreen 对未签名 exe 的常规提示。
- 点击 **更多信息 → 仍要运行** 即可。正式分发建议购买代码签名证书。

**Q：能用手机下载吗？**
- 可以，GitHub 制品和 Release 在手机浏览器也能下载。

**Q：构建要钱吗？**
- 免费账号每月有 2000 分钟 Actions 额度，Windows runner 按 2 倍计算，即约 1000 分钟。每次构建约 3 分钟，足够用几百次。
