# WhatsApp 桌面端翻译助手

一个运行在 **Windows** 上的小工具，能**自动**读取桌面版 WhatsApp（WhatsApp Desktop）聊天窗口里的新消息并翻译成中文（或你指定的语言），**不需要手动截屏、不依赖剪贴板**。

## 特性

- 🪟 **非截屏式自动提取**：通过 Windows UI Automation 直接读取 WhatsApp 窗口控件树，安全、稳定。
- 🌐 **双翻译后端**：
  - **Google 翻译**：通过 `deep-translator` 调用 Google Translate 免费通道，**无需 API Key**，开箱即用。
  - **豆包翻译**：调用火山引擎 ARK 的 `doubao` 系列模型，准确度更高，支持术语自定义。
- 🧠 **自动去重 + 缓存**：同一条消息不会重复翻译；常见短语走本地缓存，秒回。
- 📥 **系统托盘 + 全局热键**（默认 `Ctrl+Alt+T` 唤起主窗口）。
- 🎚 **可配置**：
  - 目标 / 源语言
  - 轮询间隔（默认 1.5 秒）
  - 是否只翻译收到的消息
  - 豆包 API Key、模型名、Endpoint

## 工作原理

```
┌─────────────────────┐    UI Automation    ┌──────────────────────┐
│ WhatsApp Desktop    │ ──────────────────► │  whatsapp_reader.py  │
│ (Electron 窗口)     │   读取消息文本       │  控件树遍历 / 去重    │
└─────────────────────┘                     └──────────┬───────────┘
                                                      │ 新消息
                                                      ▼
                                          ┌──────────────────────┐
                                          │  translator.py        │
                                          │  Google / 豆包 API    │
                                          └──────────┬───────────┘
                                                     │ 译文
                                                     ▼
                                          ┌──────────────────────┐
                                          │  GUI 主窗口显示       │
                                          └──────────────────────┘
```

## 安装（Windows）

> 要求：Windows 10/11，**Python 3.9+**（勾选「Add Python to PATH」），桌面版 WhatsApp（从 Microsoft Store 或 [whatsapp.com](https://www.whatsapp.com/download) 下载）。

```powershell
# 1. 克隆或解压本项目
cd path\to\whatsapp-translator

# 2. 创建虚拟环境（可选，但推荐）
python -m venv .venv
.\.venv\Scripts\activate

# 3. 安装依赖
pip install -r requirements.txt
```

> ⚠️ 如果 `keyboard` 安装失败（管理员权限相关），可先忽略；它只影响全局热键，不影响主功能。

## 使用

```powershell
python main.py
```

1. 启动后会弹出主窗口。**先打开桌面版 WhatsApp 并进入要翻译的聊天**。
2. 点击「▶ 开始监听」，程序开始周期性扫描窗口控件。
3. 每收到一条新消息，窗口会自动追加一行：原文 + 译文。
4. 点击「⏸ 暂停监听」可暂时停止；「🧹 清空」清空已显示列表。
5. 关闭主窗口默认最小化到托盘；右键托盘图标可恢复 / 退出。

> 首次启动时，**当前聊天窗口已存在的历史消息会被记录但不会触发翻译**（避免一启动就刷屏）。只有真正**新出现**的消息才会被翻译。

## 配置

配置文件位于：

```
%APPDATA%\WhatsAppTranslator\config.yaml
```

首次运行会自动生成。也可以通过主界面右上角「⚙ 设置」修改。

示例：

```yaml
translator:
  backend: google                 # google 或 doubao
  target_lang: zh-CN              # 目标语言
  source_lang: auto               # 源语言，auto=自动检测
  doubao_api_key: ""              # 留空则后端不可用
  doubao_model: doubao-seed-1-6-250615
  doubao_endpoint: https://ark.cn-beijing.volces.com/api/v3/chat/completions
  doubao_timeout: 15
reader:
  process_name: WhatsApp.exe
  window_keyword: WhatsApp
  poll_interval: 1.5              # 扫描频率（秒）
  max_history: 500
  only_incoming: true             # 只翻译收到的消息
  min_length: 2
gui:
  start_minimized: false
  close_to_tray: true
  hotkey_show: ctrl+alt+t
  theme: light
```

### 使用豆包翻译

1. 打开 [火山引擎 ARK 控制台](https://www.volcengine.com/product/doubao) 开通「在线推理」。
2. 创建一个 API Key，并在「在线推理」中开通一个模型（推荐 `doubao-lite-32k`，便宜量大）。
3. 在「⚙ 设置」中：
   - 翻译后端 → `doubao`
   - 填入 API Key
   - 确认模型名（如 `doubao-lite-32k` 或 `doubao-seed-1-6-250615`）
4. 保存后会自动重启监听器。

## 常见问题

**Q1：启动后状态一直是「正在监听」，但没有翻译？**
- 确认桌面 WhatsApp 已经打开并且有**可见的聊天窗口**（不是最小化或被遮挡到桌面外）。
- WhatsApp Desktop 升级后控件树偶尔会变。`whatsapp_reader.py` 中提供了 List 优先 + 文本兜底两套策略；如失效请把 `logs/app.log` 中相关行贴出来反馈。

**Q2：翻译速度慢 / 经常失败？**
- Google 免费通道偶有限流。打开「⚙ 设置」→ 后端切到 `doubao`。
- 在大陆地区访问 Google 不稳定时尤其建议豆包。

**Q3：能翻译图片 / 表情吗？**
- 只能翻译纯文本消息。图片 / 语音 / 表情包不会被识别（它们的 Name 不含可翻译文本）。

**Q4：能同时翻译多个聊天吗？**
- 可以，但同一时间只能跟踪一个聊天窗口（程序监听的是当前显示的会话）。要切换聊天只需在 WhatsApp 中点开其他聊天，程序会自动跟随。

**Q5：能否把译文作为「气泡叠加」直接显示在 WhatsApp 窗口里？**
- 当前版本仅在主窗口显示对照表。后续可叠加 OCR/Overlay 模式，但需要更复杂的窗口绘制与定位，不在 v1 范围内。

## 目录结构

```
whatsapp-translator/
├── main.py              # 入口
├── config.py            # 配置（yaml 持久化）
├── translator.py        # Google + 豆包翻译后端
├── whatsapp_reader.py   # UI Automation 消息读取
├── gui.py               # tkinter 主界面 + 托盘
├── requirements.txt
├── WhatsAppTranslator.spec   # PyInstaller 打包配置
├── build.bat                 # 一键打包（onedir 模式，推荐）
├── build_onefile.bat         # 一键打包（单文件 exe）
└── README.md
```

## 打包为 EXE（目标电脑无需安装 Python）

本项目已内置打包脚本，**双击运行即可**。打包在 Windows 上完成，生成的产物可拷贝到任意未安装 Python 的 Windows 电脑直接运行。

### 方式一：onedir 模式（推荐，启动快）

```powershell
双击 build.bat
```

或在 cmd 中：

```powershell
build.bat
```

脚本会自动完成：创建虚拟环境 → 安装依赖 → 安装 PyInstaller → 执行打包。

产物：

```
dist\WhatsAppTranslator\WhatsAppTranslator.exe   ← 主程序
dist\WhatsAppTranslator\*.dll, *.pyd             ← 依赖库
```

将整个 `dist\WhatsAppTranslator` 文件夹拷贝到目标电脑，双击 `WhatsAppTranslator.exe` 即可运行。启动速度比单文件模式快 2~3 秒。

### 方式二：单文件 exe（分发最方便）

```powershell
双击 build_onefile.bat
```

产物：`dist\WhatsAppTranslator.exe`（约 30~50 MB，单文件，无依赖）。

> 单文件模式每次启动会解压到临时目录，启动慢 2~3 秒；首次运行可能被 Windows SmartScreen 拦截，点击「更多信息 → 仍要运行」即可。

### 方式三：手动打包

```powershell
pip install -r requirements.txt
pip install pyinstaller
pyinstaller --noconfirm --clean WhatsAppTranslator.spec
```

### 关于 .spec 文件

[WhatsAppTranslator.spec](WhatsAppTranslator.spec) 中显式声明了以下隐藏依赖，避免运行时报 `ImportError`：

- `deep_translator` 的全部子模块（Google 通道按需导入）
- `pystray._win32` / `pystray._util`（Windows 托盘后端）
- `PIL`（Pillow 图标生成）
- `keyboard`、`uiautomation`、`comtypes` 子模块

并排除了 `matplotlib`、`numpy` 等不必要的库以减小体积。

### 自定义图标

把 `assets\app.ico` 放到项目根目录，然后将 `WhatsAppTranslator.spec` 中的：

```python
icon=None,
```

改为：

```python
icon='assets/app.ico',
```

重新运行 `build.bat` 即可。

### 体积优化建议

默认产物约 30~50 MB。如需进一步压缩：

1. 在 `WhatsAppTranslator.spec` 的 `excludes` 中追加更多未使用的库。
2. 安装 [UPX](https://upx.github.io/) 并加入 PATH，spec 中已开启 `upx=True`，会自动压缩可执行文件。
3. 使用 `build_onefile.bat` 单文件模式（仍含全部依赖，但分发更简单）。

## 安全与隐私

- Google 后端：消息原文会发送给 Google Translate 的公开接口。
- 豆包后端：消息原文会发送给火山引擎 ARK，请遵守其使用条款；**不要在「豆包控制台」开启「数据用于训练」之类的选项**。
- 程序本身只在本地运行，不上传任何额外数据。

## License

MIT
