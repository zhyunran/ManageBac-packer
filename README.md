# ManageBac-packer

- 一个整合了希悦校园与managebac系统的一站式小组件，面向**国际部/国际学校**学生，计划接入更多网页......敬请期待！


- Windows 桌面小组件，整合 ManageBac 与希悦校园管理系统的数据。


- v0.3版本起，并在以后将对北京市101中学ID（Beijing 101 middle school international department) 环境提供优先技术支持。


- 如有issues, 十分欢迎并建议提交zyr712zh123@gmail.com

  
- 本次（v0.3)开发难点，主要为**ManageBac 解析规则**，整理附于 [v0.3开发技术文档](TECHNICAL0.3.md)
---

## 面向环境

| 项目 | 要求 |
|---|---|
| 操作系统 | Windows 10 / 11（x64） |
| 运行时 | WebView2 Runtime（Win11 与较新 Win10 已预装） |
| 账号 | 本人的 ManageBac 账号，希悦账号可选 |

不需要安装 Python，不需要配置环境变量。


## 快速开始

### 用打包好的 exe

双击 `CampusPulse.exe`，第一次会弹出登录页，填学校网址、账号、密码。

### 从源码运行

需要 Python 3.11 以上。

```bash
pip install pywebview beautifulsoup4 websocket-client

copy credentials.example.json credentials.json
# 编辑 credentials.json，填入学校网址与账号

python widget.py
```

### 重新打包

```bash
pip install pyinstaller
pyinstaller build.spec --noconfirm
# 产物：dist/CampusPulse.exe
```

---

## 目录结构

```
.
├── widget.py              桌面窗口入口（启动这里）
├── warmup.py              预热入口（开机后台跑，命令行用）
├── build.spec             PyInstaller 打包配置
│
├── app/                   主要逻辑
├── web/                   界面
├── docs/                  技术文档
│
├── check_ui.py            界面语法检查
├── make_preview.py        生成带模拟数据的预览页
├── split_files.py         把 index.html 拆成 css / js
│
├── cold_start_check.py    冷启动自查（54 项）
├── status.py              查看当前状态
├── status_all.py          查看全部状态
│
├── connect.py             手动登录 ManageBac
├── schedule_setup.py      设置课表登录
├── schedule_login.py      课表登录（侦察用）
├── auto_login.py          手动触发自动登录
│
├── make_startup.py        安装/卸载开机预热
├── mb_warmup_launch.vbs   开机预热启动器（由 make_startup.py 生成）
├── stress_start.py        反复启动测试
│
├── diag_detail_speed.py   详情页耗时诊断
├── probe_attachments.py   附件结构侦察
├── probe_dates.py         日期解析侦察
├── probe_files2.py        资料目录侦察
│
├── test_*.py              各项自测
│
├── credentials.example.json   凭据模板
├── pyproject.toml             项目配置
└── uv.lock                    依赖锁定
```

---

## 功能与对应文件

### 数据获取

| 功能 | 文件 |
|---|---|
| HTTP 会话、Cookie、登录、限速处理 | `app/httpclient.py` |
| 解析 ManageBac 页面 | `app/scraper.py` |
| 抓取流程调度 | `app/pipeline.py` |
| 数据模型 | `app/models.py` |
| 任务详情（附件 / 老师要求 / 成绩） | `app/taskdetail.py` |
| 单实例锁 | `app/lock.py` |
| 本地缓存与成绩历史 | `app/store.py` |

### 课表

| 功能 | 文件 |
|---|---|
| 课表抓取（含节次时间表） | `app/schedule.py` |
| 课表界面、空档填充、自编课表 | `web/app.js` |

### GPA

| 功能 | 文件 |
|---|---|
| 成绩解析与换算表 | `app/gpa.py` |
| 汇总计算 | `app/scraper.py` 的 `summarize_courses()` |
| GPA 页面渲染 | `web/app.js` 的 `renderGpaPage()` |

### 登录态维护

| 功能 | 文件 |
|---|---|
| 自动续期、退避、防锁号 | `app/autologin.py` |
| 预热（打开即有内容） | `app/warmup.py` |
| 开机启动安装 | `make_startup.py` |

### 资料网盘与下载

| 功能 | 文件 |
|---|---|
| 课程资料浏览（纯 HTTP） | `app/files.py` |
| 文件下载 | `app/download.py` |

### 其他功能

| 功能 | 文件 |
|---|---|
| 晚报（每个工作日晚 9 点） | `app/digest.py` |
| 详情页预取 | `app/prefetch.py` |
| 链接安全校验 | `app/safelink.py` |
| 手动完成标记 | `app/manual_done.py` |
| 数据备份导出/导入 | `app/backup.py` |
| 本地网页版看板 | `app/server.py` |

### 界面

| 功能 | 文件 |
|---|---|
| 页面骨架、SVG 图标库 | `web/index.html` |
| 样式（含外观设置的三项变量） | `web/app.css` |
| 全部前端逻辑 | `web/app.js` |

### 配置

| 功能 | 文件 |
|---|---|
| 路径、凭据读取、刷新间隔等 | `app/config.py` |
| 站点地址与账号 | `credentials.json`（需自己创建） |

---

## 开发时的注意事项

### 改界面之后必须跑检查

```bash
python check_ui.py
```

它会把内联脚本交给 Edge 无头模式执行，能发现语法错误。
界面脚本出错会导致白屏，且不易定位。

### 用预览页调试

```bash
python make_preview.py
```

生成 `_preview.html`，注入了模拟数据，可以直接用浏览器打开。
比反复启动桌面窗口快。

### 不要用正则批量改 web/ 下的文件

早期用正则替换改 `index.html`，把大段代码吃掉了。
现在文件已拆分成 html / css / js 三份，用编辑器的精确替换修改。

如果确实要做大段改动，建议写临时 Python 脚本按行号操作。

### 控制台编码

Windows 控制台默认 GBK，脚本里输出中文需要：

```python
sys.stdout.reconfigure(encoding="utf-8")
```

---

## 已知限制

- 仅支持 ManageBac 与希悦校园管理系统，解析逻辑与这两个站点绑定
- 首次公开测试，测试覆盖不完整
- 部分课程类型或页面布局可能导致解析失败

---

## 许可证

MIT License。

第三方依赖：pywebview（BSD 3-Clause）、BeautifulSoup4（MIT）、
websocket-client（Apache 2.0）、PyInstaller（GPL 2.0 + 例外条款）。


### 特别鸣谢

- @xuanqiwang645     提供的UI美化技术支持
- @hs89n5km86-coder  提供灵感、建议与支持
- 如需iOS版本，请移步[CampusDesk](https://github.com/xuanqiwang645/CampusDesk)
