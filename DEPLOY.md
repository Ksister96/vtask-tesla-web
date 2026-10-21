# 部署指南（DEPLOY）

以一台全新的 Ubuntu/Debian VPS 为例（本项目的真实生产拓扑就是这样跑的）。
组件关系：

```
用户/小程序 ──HTTPS──▶ nginx(443, SNI 分流) ──▶ tesla-web(127.0.0.1:8794)
                                                 │ subprocess
                                                 ▼
                                          user_runner.py ──▶ run.py ──▶ Tesla Fleet API / 命令代理
                                                 │
      tesla-asr(127.0.0.1:8790, 语音转写, 可选) ◀──┘
```

## 1. 前置条件

- VPS：Debian/Ubuntu，Python 3.11+（`apt install python3.11`）
- 域名一个（可先用 [cpolar](https://www.cpolar.com) 等内网穿透快速联调）
- [Tesla 开发者账号](https://developer.tesla.com)：创建应用拿到
  **Client ID / Client Secret**，并按官方文档注册「公钥 → 虚拟钥匙」流程
  （本项目的 `tesla-pk-register` 定时任务与 `tesla-proxy` 即做这件事）
- [vehicle-command](https://github.com/teslamotors/vehicle-command)（Go 官方命令代理），
  编译后跑在 4443 端口（`tesla-proxy.service`，示例同 systemd/ 目录思路）

## 2. 文件落位

```bash
sudo mkdir -p /opt/tesla-fleet-multi && sudo chown $USER /opt/tesla-fleet-multi
git clone https://github.com/<你>/vtask-tesla-web.git /opt/tesla-fleet-multi
```

同目录还需要一份 **Fleet 基础库**（`run.py` 已含在本仓库；如果你的 Tesla Fleet API
封装是独立目录，把环境变量 `OWNER_BASE` 指向它）。

## 3. 环境变量（/etc/tesla.env）

`tesla-web.service` 通过 `EnvironmentFile=/etc/tesla.env` 注入：

```ini
# Tesla 开发者应用凭据（来自 developer.tesla.com）
TESLA_CLIENT_ID=你的ClientID
TESLA_CLIENT_SECRET=你的ClientSecret
# 微信小程序凭据（可选，用于 wx.login 换 openid）
MP_WX_APPID=
MP_WX_SECRET=
# Fleet 基础库目录（run.py 所在位置）
OWNER_BASE=/opt/tesla-fleet
# 本服务数据目录（用户数据/日志/静态资源）
MULTI_BASE=/opt/tesla-fleet-multi
# 高德地图 Key（行程地图；放在 BASE/amap_key.txt 亦可）
AMAP_KEY=你的高德Key
```

**权限**：`chmod 600 /etc/tesla.env`——里面是凭据，不要进 git。

## 4. systemd

```bash
sudo cp systemd/tesla-web.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now tesla-web
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8794/api/state   # 401 = 存活
```

## 5. HTTPS 暴露（三选一）

- **nginx stream SNI 分流**（生产推荐）：443 按域名分流到 8445（本服务 TLS）
- **cpolar/内网穿透**：`cpolar http 8794`，绑定自有域名
- 局域网联调：`CAR_HOST=0.0.0.0 python3 car-web-multi.py`

真机小程序必须在**合法域名**下走 HTTPS，纯 IP不行。

## 6. 车模资源（3D 仪表盘/贴膜工坊）

`static/wraps3d/models/` 下的 GLB 车模（单个 3~25MB）不入 git。
两种获取方式：

1. 从演示站按文件名下载（见 `static/wraps3d/models/` 目录名清单）；
2. 用你自己的 glTF 车模替换，并保持 `user_runner.py`/`car-viewer.js` 里的文件名映射。

贴膜模板 `static/wraps/*.png` 与灯具贴图 `static/wraps3d/models/tex/` 已随仓库附带。

## 7. 语音转写（可选）

生产环境用 [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) SenseVoice int8 模型
（`tesla-asr.service`，监听 8790，接口 `POST /transcribe {path}` → `{text}`）。
`asr_postprocess.py` 是转写后处理（车控语境同音纠正），web 与 ASR 服务共用一份，
**两边同步覆盖**。没有 ASR 时，小程序自动回落到「录音上传 → /api/transcribe」。

## 8. 小程序对接

微信小程序前端（`vtask-tesla-miniapp` 仓库）：

1. `miniprogram/utils/api.js` 的 `BASE` 指向你的域名；
2. 小程序后台配置 request/downloadFile 合法域名；
3. （可选）添加同声传译插件启用实时语音识别。

## 9. 安全清单

- [ ] `/etc/tesla.env` 权限 600，永不入库
- [ ] `users/`（用户令牌）与 `identity.sqlite3`（身份库）不入库、定期备份
- [ ] 8794 只监听 127.0.0.1，公网入口走 nginx TLS
- [ ] 定期轮换 Tesla Client Secret 与 refresh token
- [ ] 意图安全白名单（`intent_safety.py`）改动需跑 `test_voice_matrix.py` 回归

## 10. 升级

```bash
cd /opt/tesla-fleet-multi
git pull
python3 -X utf8 test_voice_matrix.py        # 解析层回归
sudo systemctl restart tesla-web
```
