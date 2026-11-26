# 聽歌機器人（V6）

以 [discord.py](https://github.com/Rapptz/discord.py)、[yt-dlp](https://github.com/yt-dlp/yt-dlp) 與 ffmpeg 打造的 Discord 音樂機器人，附網頁控制面板與 Chrome 擴充功能。支援 YouTube、Bilibili、Spotify 與 SoundCloud。

授權為 [MIT](LICENSE)。

## 功能

- **點歌與播放**：在 Discord 指令或網頁面板貼上連結即可播放；佇列、跳過、音量、循環、跳轉、休眠計時都可從面板或 `/panel` 控制。
- **Bilibili**：支援 `bilibili.com/video/BV…`、av 號、`?p=` 指定分 P，以及 b23.tv 等短網址。多 P 影片沒指定分 P 時會整部加入，有指定就只加那一 P。面板搜尋可在 YouTube 與 Bilibili 之間切換，並會記住上次選項。
- **其他來源**：YouTube / YouTube Music、Spotify（匯入歌單或專輯）、SoundCloud。
- **自動推薦接歌**：佇列播完且沒有循環補歌時，可從 YouTube Mix 接著播相關歌曲（每個伺服器可開關，預設關閉）。
- **個人歌單**：網頁、「我的歌單」與 Discord `/mylist` 共用；可用 Chrome 擴充功能在 YouTube、YouTube Music、Bilibili 影片頁一鍵加入佇列。
- **播放紀錄與排行**、操作紀錄、音量平衡、換歌預先解析與預熱。

目前限制：B 站合集、收藏夾與番劇網址尚不支援；Discord `/play` 打關鍵字搜尋仍只走 YouTube（直接貼 B 站網址可以播）；需要登入或有地區限制的影片可能無法解析。

更細的功能說明見 [docs/linux-and-features.md](docs/linux-and-features.md)。

## 需求

- Python 3.10 或以上（建議用 venv）
- [ffmpeg](https://ffmpeg.org/) 在 `PATH` 裡，或用 `FFMPEG_PATH` 指定
- 一個 Discord 機器人 token（在 [Discord Developer Portal](https://discord.com/developers/applications) 建立）
- Linux 上用 systemd 常駐時，還需要 `python3-venv`

Debian / Ubuntu 範例：

```bash
sudo apt install python3 python3-venv ffmpeg
```

專案路徑請不要包含空白。

## 安裝

```bash
git clone https://github.com/pop0909/V6-music-bot.git
cd V6-music-bot
python3 -m venv venv
# Windows：py -3 -m venv venv 然後 venv\Scripts\activate
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 然後編輯 .env，至少填入 DISCORD_TOKEN
python main.py
```

Windows 也可直接在專案根目錄使用已建立的虛擬環境或系統 Python，執行 `python main.py`。請把設定寫在專案根目錄的 `.env`，不要把這個檔或 cookies 提交到 Git。

## 設定

設定寫在專案根目錄的 `.env`（請參考 [.env.example](.env.example)）。**下面只列出變數名稱與用途，請自行填入，不要把真實密鑰寫進 README 或提交到倉庫。**

### 必填

| 變數 | 說明 |
|---|---|
| `DISCORD_TOKEN` | Discord 機器人 token |

### 常用選項

| 變數 | 說明 |
|---|---|
| `PUBLIC_URL` | 面板的公開網址，給 Discord `/web` 顯示，建議搭配反向代理或 Cloudflare Tunnel |
| `WEB_HOST` | 面板監聽位址（預設 `0.0.0.0`；只給本機或 Tunnel 連時可設 `127.0.0.1`） |
| `WEB_PORT` | 面板埠號（預設 `5000`） |
| `WEB_THREADS` | waitress 執行緒數（預設 24，範圍約 12–64） |
| `ALLOWED_HOSTS` | 允許的 HTTP `Host`，逗號分隔；應包含你的面板網域 |
| `FFMPEG_PATH` | ffmpeg 執行檔路徑（預設從 `PATH` 尋找） |
| `DEFAULT_VOLUME` | 預設音量（`0.0`–`2.0`，大於 2 時會當成百分比，例如 `20` = 20%） |
| `EXT_TOKEN` | 擁有者擴充功能／本機狀態查詢用的金鑰（請自行設一串夠長的隨機字） |
| `OWNER_ID` | 出事或自我檢查時私訊的 Discord 使用者 ID（不設就用應用程式擁有者） |
| `SYNC_GUILD_ID` | slash 指令要同步的伺服器 ID；設 `0` 則不做這個同步 |
| `QUEUE_MAX` | 單一伺服器佇列上限 |
| `IDLE_SLEEP_MINUTES` | 沒人聽時進入休眠的分鐘數 |
| `AUTOPLAY_DEFAULT` | 設 `1` 則自動推薦接歌預設開啟 |
| `AUDIO_NORMALIZE` | 設 `0` 則音量平衡預設關閉（否則預設開啟） |
| `BACKUP_INTERVAL_DAYS` | 自動備份間隔（天） |

### YouTube cookies 與播放紀錄

| 變數 | 說明 |
|---|---|
| `YTDLP_COOKIES` | Netscape 格式的 `cookies.txt` 路徑（權限建議 `600`）。伺服器 IP 常被 YouTube 要求驗證時需要 |
| `YTDLP_COOKIES_FROM_BROWSER` | 本機可改讀瀏覽器（例如 `firefox` 或 `chrome`）；伺服器請用檔案 |
| `YTDLP_PLAYER_CLIENT` | 進階：yt-dlp 的 YouTube player client 順序，一般不必設 |
| `HISTORY_DB` | 播放紀錄 SQLite 路徑（預設 `data/history.db`） |
| `ACTION_LOG_FILE` | 操作紀錄 JSON 路徑（預設 `data/action_log.json`） |

### 網頁 Discord 登入（選用）與自我檢查

| 變數 | 說明 |
|---|---|
| `DISCORD_CLIENT_SECRET` | 開發者後台 OAuth2 的 Client Secret；沒設則面板只用金鑰登入 |
| `DISCORD_CLIENT_ID` | 選填；不填就用機器人自己的應用程式 ID |
| `OAUTH_REDIRECT_URI` | 必須與開發者後台 Redirects 完全一致，例如 `https://你的面板網域/auth/discord/callback` |
| `PANEL_SECRET` | 可覆寫面板簽章金鑰（通常讓程式寫入 `data/panel_secret` 即可） |
| `SELFCHECK_HOUR` | 每日自我檢查的台北時間（`0`–`23`；`off` 為關閉，預設 `9`） |
| `SELFCHECK_VIDEO` | 每日解析測試用的影片網址 |

cookies 設定步驟、風險與自我檢查細節見 [docs/linux-and-features.md](docs/linux-and-features.md)。`.gitignore` 已排除 `.env`、`data/`、`cookies*.txt` 與 `chrome-extension/token.txt`。

## Linux 部署（systemd）

把專案放到伺服器後：

```bash
chmod +x deploy/*.sh
sudo ./deploy/install.sh                 # 以 sudo 的那個使用者身分執行
# 或： sudo ./deploy/install.sh --user musicbot
# 只裝不啟動： sudo ./deploy/install.sh --no-start
```

`install.sh` 會建立 `venv/`、安裝相依套件、把 `.env` 權限設為 `600`，並安裝、啟用 `musicbot.service` 與 `musicbot-update.timer`。

不需 root 時可用 `./deploy/install.sh --user-mode`（若希望沒登入也要跑，再執行 `sudo loginctl enable-linger $USER`）。

每日更新（預設台北時間 05:00）**只會更新 yt-dlp**，不會自動 `git pull`。有人在聽就整次跳過；沒人在聽才更新，版本有變且仍沒人在聽才重啟機器人。

常用指令：

```bash
systemctl status musicbot
journalctl -u musicbot -f
sudo systemctl restart musicbot
systemctl list-timers musicbot-update.timer
sudo systemctl start musicbot-update.service   # 立刻手動檢查 yt-dlp 更新
sudo ./deploy/uninstall.sh                     # 移除服務；加 --purge-venv 連 venv 一起刪
```

完整行為說明（啟動前更新、當機重啟、cookies、waitress、面板快取等）見 [docs/linux-and-features.md](docs/linux-and-features.md)。

## Chrome 擴充功能

資料夾在 [`chrome-extension/`](chrome-extension/)，目前版本 1.2.1。

1. 打開 `chrome://extensions`
2. 開啟「開發人員模式」→「載入未封裝項目」→ 選 `chrome-extension` 資料夾
3. 在選項頁貼上金鑰並儲存（擁有者用 `EXT_TOKEN`，朋友向擁有者索取成員金鑰）

這個倉庫裡的擴充功能預設連到本專案已設定的面板網址。若你自己架設，請修改 `chrome-extension/manifest.json` 的 `host_permissions`、說明與相關前端檔案後再載入。更新檔案後記得在擴充功能頁按重新載入。更完整的使用說明見 [chrome-extension/README.md](chrome-extension/README.md)。

## 測試

改完程式後在專案根目錄執行：

```bash
python tools/check.py           # 單元測試 + API 煙霧測試 + 面板 UI 煙霧測試
python tools/check.py --no-ui   # 不跑 UI（較快）
python -m unittest discover -s tests
```

- UI 煙霧測試使用 Playwright（深色／淺色 × 1440／390 寬）。沒裝會自動略過：`pip install playwright && python -m playwright install chromium`。伺服器記憶體小的話不要裝在伺服器上。
- 模擬器（`tools/sim.py`）的資料放在暫存資料夾，不會動到專案的 `data/`、`list.txt`、`favorites/`；截圖在 `smoke_out/`（已列入 `.gitignore`）。
- 倉庫換行一律為 LF（見 `.gitattributes`）。

## 授權

[MIT License](LICENSE) © 2026 pop0909
