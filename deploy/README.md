# 部署「同行」到自己的服务器

数据全程在你自己的一台机器上，模型 API 是唯一的外部调用 —— 这是「私密优先」的实际含义。

## 一、服务器规格

这个应用**特别省资源**：真正的算力在模型厂商那边，服务器只做 HTTP + SQLite + 转发流式文本。

| 项 | 建议 | 说明 |
|---|---|---|
| CPU / 内存 | **2 核 2G** | Python + uvicorn + SQLite 实测吃不到 500MB，留一倍余量 |
| 带宽 | **1-3 Mbps 或按流量** | 一次请求 ~15KB，一天 20 轮 ≈ 9MB/月，流量费几分钱 |
| 地域 | **内地节点**（杭州/上海/华北2） | 模型在国内，跨境延迟会让流式聊天明显变慢 |
| 形态 | ECS 经济型 e 实例 或 轻量应用服务器 | 轻量自带应用镜像和防火墙控制台，更省事 |
| 系统盘 | 40 GiB ESSD | 数据库 + trace 很小 |

**必须注意两点：**

1. **不能用多 worker。** 进程内有回复注册表（`GenerationService._executions`），多进程会让"断线后恢复回复"失效。nginx 反代没问题，但只能反代到 **1 个** uvicorn 进程。
2. **没有 HTTPS 就没有完整 PWA。** Service Worker 只在安全上下文注册 —— 纯 HTTP 下离线缓存、推送、安装提示都不工作（但"添加到主屏幕"的图标和外观仍然正常）。

## 二、三条接入路线

| 路线 | 备案 | HTTPS | 适合 |
|---|---|---|---|
| **A. 公网 IP + 端口 8000** | 不用 | 没有 | 先跑通，验证没问题 |
| **B. Tailscale** | 不用 | 自动 | 只自己用 —— 最贴「私密优先」，机器不用配公网 IP |
| **C. 域名 + 备案 + 证书** | 要（1-2 周） | 完整 | 想分享给别人 |

**推荐路径**：先 A 跑通 → 同时提交备案 → 批下来切 C。
不想备案又想要 HTTPS，就走 B。

## 三、部署

```bash
# 1. 把代码传到服务器（scp / git clone 都行）
scp -r companion-ever-memory user@服务器IP:/tmp/

# 2. 跑部署脚本（要 root）
ssh user@服务器IP
sudo bash /tmp/companion-ever-memory/deploy/deploy.sh /tmp/companion-ever-memory
```

脚本会：装 Python 3.10+ → 建 `companion` 系统用户 → 代码放 `/opt/companion` 并建 venv →
写 `/etc/companion/companion.env`（**自动生成随机 PIN**）→ 装 systemd unit → 启动并自检。

### 部署后必做

```bash
# 1. 改模型配置
sudo nano /etc/companion/companion.env   # OPENAI_API_KEY / OPENAI_BASE_URL / COMPANION_LLM_MODEL
sudo systemctl restart companion

# 2. 建第一个用户和角色
sudo -u companion /opt/companion/.venv/bin/python -m companion_ever setup \
    --user alice --persona 小伴 --template hot_girlfriend

# 3. 防火墙放行
#    走 nginx：放 80 / 443
#    直连应用：放 8000
```

### 拿随机 PIN

脚本会打印 PIN；忘了就重看：

```bash
sudo grep COMPANION_WEB_PIN /etc/companion/companion.env
```

## 四、日常运维

```bash
# 看日志（含启动失败、模型报错）
sudo journalctl -u companion -f

# 更新代码（editable 安装，静态文件也一起更新）
cd /opt/companion && sudo git pull
sudo -u companion /opt/companion/.venv/bin/python -m pip install -e /opt/companion
sudo systemctl restart companion

# 备份数据库（写在本目录，文件名带时间戳）
sudo -u companion /opt/companion/.venv/bin/python -m companion_ever maintain backup

# 看模型究竟收到了什么（诊断 prompt / 响应）
sudo -u companion /opt/companion/.venv/bin/python -m companion_ever trace list --last 5
sudo -u companion /opt/companion/.venv/bin/python -m companion_ever trace show <trace_id> --request
```

**备份建议挂 cron**（每天一次，保留 7 份）：

```cron
17 4 * * * root /opt/companion/.venv/bin/python -m companion_ever --db /var/lib/companion/companion.db maintain backup
```

## 五、安全清单

- [ ] `COMPANION_WEB_PIN` **已设**。留空 = 不鉴权，`check_pin` 直接放行，任何拿到地址的人都能看全部聊天记录
- [ ] 走 nginx 时 `COMPANION_WEB_HOST=127.0.0.1`，别绑 `0.0.0.0`
- [ ] 数据库在 `/var/lib/companion`（`StateDirectory`，systemd 已设权限），不在代码目录里
- [ ] `COMPANION_TRACE=1` 的 trace 含**私人业务原文**，只落在服务器本地
- [ ] 用了 Tailscale 就不必配公网 IP —— 少一个攻击面

## 六、故障排查

| 现象 | 多半是 |
|---|---|
| 手机打不开 | 防火墙没放行 / `COMPANION_WEB_HOST` 绑了 127.0.0.1 但你走了公网 |
| 能打开但提示 PIN 不对 | PIN 没设（`check_pin` 直接放行）或 `.env` 没生效 |
| 回复一段一段蹦出来 | nginx 没加 `proxy_buffering off` |
| 聊到一半断了 | nginx 的 `proxy_read_timeout` 默认 60s，记忆整理要跑几十秒 |
| 改了代码没生效 | 静态文件在包目录里 —— editable 安装下 `git pull` 即可；非 editable 要重新 `pip install` |
| 模型报 401 / 连不上 | `.env` 里的 `OPENAI_BASE_URL` / `COMPANION_LLM_MODEL` 没改对 |
