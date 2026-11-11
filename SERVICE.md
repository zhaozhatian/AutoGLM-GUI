# AutoGLM-GUI 本地服务运行说明

本仓库是 `suyiiyii/AutoGLM-GUI` 的内部 fork，额外包含：

- `<answer>...</answer>` 动作解析修复
- 坐标校验、四舍五入、屏幕边界限制
- 滑动安全边距、最小距离、250~600ms 时长
- 模型提示词中的屏幕尺寸
- 可选的 0~999 坐标网格叠加
- ADBKeyboard 安装时的事件循环修复

## 1. 首次安装

要求：

- Windows 10/11
- Python 3.11+（当前实测 3.14.8）
- Android 手机已开启开发者选项和无线调试

```bat
cd D:\yl\project\wework\AutoGLM-GUI

python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -e .
```

`-e .` 表示 editable 安装，后续修改源码不需要重新安装。

## 2. 启动

CMD：

```bat
cd D:\yl\project\wework\AutoGLM-GUI
run_service.bat
```

Git Bash：

```bash
cd /d/yl/project/wework/AutoGLM-GUI
./run_service.sh
```

默认地址：

```text
http://127.0.0.1:8765
```

## 3. 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AUTOGLM_PORT` | `8765` | Web 监听端口 |
| `AUTOGLM_GRID_OVERLAY` | `1` | 给模型截图叠加 0~999 坐标网格；设为 `0` 关闭 |
| `AUTOGLM_ADB_PATH` | 自动设置 | 指向 `%USERPROFILE%\.cache\autoglm\platform-tools\adb.exe` |

启动器会自动把 ADB 目录加入 `PATH`，避免代码里裸 `adb` 命令找不到。

## 4. 模型配置

配置文件：

```text
C:\Users\<用户名>\.config\autoglm\config.json
```

也可以在命令行临时指定：

```bat
run_service.bat ^
  --base-url https://open.bigmodel.cn/api/paas/v4 ^
  --model autoglm-phone ^
  --apikey sk-xxxxx
```

推荐使用 GUI 专用模型：

- 智谱 BigModel：`autoglm-phone`
- ModelScope：`ZhipuAI/AutoGLM-Phone-9B`

通用的 `glm-5.3-flash` 也能跑，但坐标定位精度明显不如专用模型。

## 5. 手机要求（小米 / HyperOS）

开发者选项中必须打开：

- **USB 调试**
- **USB 调试（安全设置）**：允许 ADB 注入点击/滑动
- **USB 安装**：允许 ADB 安装 ADBKeyboard

部分系统需要登录小米账号、插入 SIM 卡或等待几分钟。

验证：

```bash
"$HOME/.cache/autoglm/platform-tools/adb.exe" \
  -s <设备IP>:<端口> shell input tap 540 1200
echo $?
```

返回 `0` 才说明权限生效。

## 6. 无线调试连接

手机开启无线调试后，在 AutoGLM 页面扫码配对，或手动：

```bash
adb connect <手机IP>:<端口>
```

设备 IP 建议在路由器里做静态分配，避免重启后变化。

## 7. 日志与数据

| 内容 | 位置 |
|---|---|
| 运行日志 | `AutoGLM-GUI/logs/` |
| 模型配置 | `%USERPROFILE%\.config\autoglm\config.json` |
| 任务/历史数据库 | `%USERPROFILE%\.config\autoglm\tasks.db` |
| ADB | `%USERPROFILE%\.cache\autoglm\platform-tools\` |

## 8. 同步上游

```bash
cd D:\yl\project\wework\AutoGLM-GUI
git fetch upstream
git switch feat/click-accuracy
git rebase upstream/main
```

如果上游已经修复了 `<answer>` 解析或 ADBKeyboard 事件循环问题，可以删掉对应补丁，只保留网格和滑动增强。

## 9. 可选：注册为 Windows 服务

当前使用手动脚本启动。如果后续需要开机自启和崩溃重启，可以用 NSSM：

```bat
nssm install AutoGLM "D:\yl\project\wework\AutoGLM-GUI\run_service.bat"
nssm set AutoGLM AppDirectory "D:\yl\project\wework\AutoGLM-GUI"
nssm set AutoGLM AppStdout "D:\yl\project\wework\AutoGLM-GUI\logs\service.log"
nssm set AutoGLM AppStderr "D:\yl\project\wework\AutoGLM-GUI\logs\service-error.log"
nssm set AutoGLM AppExit Default Restart
nssm start AutoGLM
```

注册服务前先确认 8765 端口没有被其他实例占用。