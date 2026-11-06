# 部署到常开设备

`orchestrator` 不依赖 GitHub Actions，任何能跑 Python 3 的机器都可以当编排主机：
一台 VPS、家里的 Mac mini、NAS、树莓派都行。

比 Actions 强的三点：

1. **飞书 API 稳定**：`open.feishu.cn` 在国内直连，不用跨境。
2. **YouTube 不那么容易被判 bot**：住宅 IP + cookies 成功率高得多。
3. **能跑 whisper**：想彻底不依赖飞书，就本地转写。

---

## 一、准备

```bash
git clone https://github.com/<owner>/gongzhonghao.git /opt/gongzhonghao
cd /opt/gongzhonghao
python3 --version          # 需要 3.10+

# 凭证（放 ~/.config/gzh/env 或 systemd EnvironmentFile）
export GZH_LLM_API_KEY=sk-...        # 必填
export FEISHU_APP_ID=cli_xxx          # 用飞书妙记时
export FEISHU_APP_SECRET=xxx
export YT_COOKIES_FILE=~/cookies.txt  # 抓 YouTube 字幕时
```

案例目录默认落在仓库父目录；云端用的是 `cases/`。想两边一致就设：

```bash
export GZH_BASE=/opt/gongzhonghao/cases
```

---

## 二、跑起来

先手动验一次：

```bash
python3 orchestrator/loop.py tick --scan --provider deepseek
python3 orchestrator/loop.py status
```

没问题再交给系统定时器。

### Linux · systemd

```bash
sudo cp deploy/gzh-loop.service deploy/gzh-loop.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gzh-loop.timer
journalctl -u gzh-loop -f
```

### macOS · launchd

```bash
cp deploy/com.gzh.loop.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.gzh.loop.plist
```

### 最土的办法 · cron

```bash
crontab -e
# */30 * * * * cd /opt/gongzhonghao && . ~/.config/gzh/env && python3 orchestrator/loop.py tick --scan >> /var/log/gzh.log 2>&1
```

---

## 三、设备与 Actions 怎么分工

| 分工方式 | 适合 | 代价 |
|---|---|---|
| **设备跑全部** | 想彻底不依赖 GitHub | 设备要 24h 在线，回写靠 `git push` |
| **设备只采集，Actions 编排** | 想保留云端审计与免运维 | 两处配置，凭证要放两份 |
| **只用 Actions** | 不介意 cookies / 飞书 API 跨境 | 转写环节容易卡住 |

设备跑全部时，需要给它配好 git 凭据（或用 deploy key），让它能把 `runs/` 与 `cases/` 推回仓库——否则状态只存在本地。

---

## 四、注意

- **同一时间只让一个环境跑 tick**。两个地方同时推 `runs/` 会冲突。
- 凭证不要写进仓库；用环境变量或 `EnvironmentFile`。
- `policy.json` 是随仓库走的，设备上改了要 commit，否则下次 `git pull` 会被覆盖。
