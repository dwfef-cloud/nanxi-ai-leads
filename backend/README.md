# Douyin Lead System

## 一键打开桌面工作区

首次安装桌面启动协议：

```powershell
powershell -ExecutionPolicy Bypass -File .\install-nanxi-protocol.ps1
```

之后可以点击 `nanxi://desktop`，或直接运行 `start-nanxi.ps1`。脚本会自动检查并启动后端，再打开 Electron 内嵌浏览器工作区。

抖音公开线索发现 + 私信辅助触达 + 轻量 CRM 的开发骨架。

当前结构：
- `app/` 后端服务
- `tests/` 基础校验
- `docs/` 设计与护栏
