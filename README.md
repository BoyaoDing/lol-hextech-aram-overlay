# 英雄联盟海克斯大乱斗（ARAM: Mayhem）直播海克斯实时显示插件

面向《英雄联盟》**海克斯大乱斗（ARAM: Mayhem）**主播的轻量级直播叠加层插件：在对局中实时识别主播选取的强化符文，并通过透明网页源实时展示在 **OBS Studio / B 站直播姬 / 虎牙开播 / 抖音直播伴侣** 的直播画面上，让半路进入直播间的观众随时看清已选海克斯与效果说明。

## 核心特性

- **双通道融合识别，合规无侵入**：不读取游戏内存、不注入 DLL、不修改游戏文件、不模拟键鼠操作。
  - **国服首选通道（腾讯 `icreate-sdk` 只读加密日志）**：在 LCU 检测到国服（`TENCENT` / `CN` / `CHINA`）时，仅在内存中派生解密密钥增量读取 `icreateLogs/main.log` 的 `693`（候选）、`694`（本地选取下标）、`696`（全员已选安全校准）事件，毫秒级精准同步，兼容海斗 4.0 四选一、“质变：混沌” 1 变 2 与 K/DA 额外海克斯。
  - **全服通用回退通道（Windows 原生 OCR + 视觉状态机）**：在非国服或缺少腾讯组件日志时自动接管，基于 Win32 GDI 屏幕像素采样（`SRCCOPY`）、3 卡/4 卡边框几何检测、Windows 内置 `Windows.Media.Ocr` 标题识别与被动鼠标点击/卡片高光闪光判断选取结果，支持“收起面板后重新展开”自动撤回误记。
- **开箱即用，无需 OBS WebSocket**：独立提供 `http://127.0.0.1:18777/overlay` 透明浏览器源与 `http://127.0.0.1:18777/control` 主播控制台。
- **4 套直播展示主题**：`vertical`（经典纵向卡片）、`showcase`（焦点弹窗 + 纵向列表）、`tray`（横向 HUD 托盘）、`strip`（极简单行紧凑条），还原游戏内白银 / 黄金 / 棱彩全息渐变视觉效果。
- **内置离线目录与同局断点恢复**：内置 574 个强化符文中文目录（含 346 个大乱斗活跃池与官方中文说明），直播途中断开重启自动恢复本局已选记录。
- **零游戏调试模式**：控制台内置「一键演示 / 单步推进」与 2 秒中文搜索补录、替换、撤销功能。

## 快速开始

1. 安装依赖（Python 3.10+，Windows 10/11）：
   ```powershell
   pip install -r requirements.txt
   ```
2. 双击 `start.bat` 或运行：
   ```powershell
   python -m hextech_overlay --open-browser
   ```
3. 在 **OBS Studio / 直播姬** 中添加 **浏览器（Browser）** 来源：
   - URL：`http://127.0.0.1:18777/overlay`
   - 推荐尺寸：`360 × 560`（横向 `tray`/`strip` 主题可用 `980 × 180`）
   - 背景默认透明，无需额外设置自定义 CSS。
4. 游戏视频设置建议为 **无边框全屏（Borderless）**，以便需要视觉回退时稳定采集窗口画面。

## 目录结构

- `hextech_overlay/models.py`：Pydantic 状态与配置模型
- `hextech_overlay/config.py`：配置文件与同局会话断点存储
- `hextech_overlay/catalog.py`：离线目录加载、LCU/CDragon 同步、OCR 模糊匹配与图标缓存
- `hextech_overlay/lcu.py` & `hextech_overlay/live_api.py`：LCU 与 `127.0.0.1:2999` 只读接口轮询
- `hextech_overlay/tencent_log.py`：国服 `icreate-sdk` 加密日志解码、增量 tail 与 `693/694/696` 状态机
- `hextech_overlay/vision_detector.py`：3/4 卡几何布局检测、GDI 采集、Windows OCR 工作器与点击/闪光状态机
- `hextech_overlay/engine.py`：双通道编排引擎、手动编辑与演示模式
- `hextech_overlay/server.py` & `hextech_overlay/__main__.py`：FastAPI HTTP/WebSocket 服务与 CLI 入口
- `hextech_overlay/static/overlay.html` & `control.html`：OBS 透明叠加层与主播控制台前端
- `scripts/sync_bundled_catalog.py`：从 CommunityDragon 更新离线强化目录
- `tests/`：单元测试与 API/WebSocket 集成测试

## 开发与测试

```powershell
# 运行全部单元测试与集成测试
python -m unittest discover -s tests -v

# 更新内置中文强化目录
python scripts/sync_bundled_catalog.py
```
