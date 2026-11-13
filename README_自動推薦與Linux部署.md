# 自動推薦接歌 + Linux 部署（systemd）

## 自動推薦接歌（Autoplay）
- 佇列播完、而且沒有「全部循環 / 單曲循環 / 隨機循環」會補歌時，從 YouTube Mix（`watch?v=<ID>&list=RD<ID>`）挑一首相關歌曲接著播，不需要 API key。
- 種子＝最近一首成功播放的歌；不重複最近 50 首、跳過 >10 分鐘 / 直播 / 合輯 / 已刪除影片；播放失敗的推薦不再挑。
- 沿用失敗上限：連續 3 首播放失敗會停止，並同時關閉隨機循環與自動推薦。
- 開關（每個伺服器各自）：
  - 網頁面板：播放器卡片下方「自動推薦接歌」開關
- **預設關閉**。想預設開啟：在 `.env` 加 `AUTOPLAY_DEFAULT=1`。開關存在記憶體，bot 重啟後回到預設值。

## Linux 部署（systemd）

### 需求
- Python 3.10+、`python3-venv`、`ffmpeg`、systemd
  - Debian/Ubuntu：`sudo apt install python3 python3-venv ffmpeg`
- 專案路徑不能有空白，例如 `/opt/musicbot` 或 `/home/你/musicbot`
- `.env` 至少要有 `DISCORD_TOKEN=...`

### 安裝（系統層級，預設；開機就啟動，不需登入）
```bash
# 把整個資料夾傳到伺服器（.env 一起），例如：
scp -r v5_improved 使用者@伺服器:/home/使用者/musicbot
ssh 使用者@伺服器
cd ~/musicbot
chmod +x deploy/*.sh
sudo ./deploy/install.sh                 # 以 sudo 的那個使用者身分執行 bot
# 或指定帳號： sudo ./deploy/install.sh --user musicbot
# 只裝不啟動： sudo ./deploy/install.sh --no-start
```
install.sh 會：建立 `venv/`、安裝 requirements、把 `.env` 權限設為 600、
安裝並啟用 `musicbot.service` 與 `musicbot-update.timer`。

### 使用者層級（不需 root）
```bash
./deploy/install.sh --user-mode
sudo loginctl enable-linger $USER    # 沒登入（開機後）也要跑才需要
```
指令都改成 `systemctl --user ...`、`journalctl --user ...`。

### 行為
| 項目 | 說明 |
|---|---|
| 啟動前更新 | `ExecStartPre=-deploy/update-ytdlp.sh`：每次啟動先 `pip install -U yt-dlp`；離線/失敗也照樣啟動 |
| 當機重啟 | `Restart=on-failure`、`RestartSec=30`；10 分鐘內失敗超過 10 次就停止（`systemctl reset-failed musicbot` 後再啟動） |
| 每日更新 | `musicbot-update.timer` 每天 05:00（隨機延遲 ≤20 分，錯過會補跑）執行更新；**版本有變才重啟 bot**；若有伺服器在語音中，每 10 分鐘再看，最多延後 6 小時 |
| 日誌 | journald：`journalctl -u musicbot -f`、`journalctl -u musicbot-update` |
| 檔案路徑 | `list.txt`、`favorites/` 以專案資料夾為準，不受工作目錄影響 |

### 選用的 .env 設定
```
AUTOPLAY_DEFAULT=1        # 自動推薦預設開啟
FFMPEG_PATH=/usr/bin/ffmpeg
WEB_HOST=127.0.0.1        # 只給本機 / Cloudflare Tunnel 連（預設 0.0.0.0）
WEB_PORT=5000
AUDIO_NORMALIZE=1        # 音量平衡預設開啟（0 = 預設關閉）
YTDLP_COOKIES=/root/v6/cookies.txt   # YouTube 機器人驗證時用（見下方說明，chmod 600）
HISTORY_DB=/root/v6/data/history.db  # 播放紀錄位置（預設 data/history.db）
```

### 常用指令
```bash
systemctl status musicbot
journalctl -u musicbot -f
sudo systemctl restart musicbot
systemctl list-timers musicbot-update.timer
sudo systemctl start musicbot-update.service   # 立刻手動檢查更新
```

### 移除
```bash
sudo ./deploy/uninstall.sh                 # 加 --purge-venv 連 venv 一起刪
./deploy/uninstall.sh --user-mode          # 使用者層級
```
不會刪除 `.env`、`list.txt`、`favorites/`。

## YouTube「確認你不是機器人」與 cookies

伺服器（尤其是 VPS / 雲端主機）的 IP 常被 YouTube 要求驗證，yt-dlp 會出現
`Sign in to confirm you're not a bot`。bot 偵測到這個錯誤時會：

- 在 log 寫一行清楚的中文提示（10 分鐘內只寫一次，不洗版）
- 在 Discord 頻道與網頁跳出「需要設定 cookies」的提示（同一個問題 10 分鐘內只發一次）
- **暫停自動接歌**：這首歌放回佇列最前面，不計入「連續失敗 3 次」的次數、不會一直重試；設定好 cookies 重啟後按播放即可續播

### 設定 cookies（建議用拋棄式 Google 帳號）

1. 用**另外註冊的拋棄式 Google 帳號**（不要用主帳號）在瀏覽器登入 youtube.com，隨便播一部影片。
2. 安裝瀏覽器擴充功能 **Get cookies.txt LOCALLY**（Chrome / Edge / Firefox 都有；認明「LOCALLY」版本，它不會把資料傳到外部）。
3. 在 youtube.com 分頁點擴充功能 → Export（Netscape 格式）→ 存成 `cookies.txt`。
4. （建議）匯出後用無痕視窗或直接關掉該分頁，不要再用這個瀏覽器設定檔操作 YouTube，避免 cookies 很快被輪替失效。
5. 放到伺服器，例如 `/root/v6/cookies.txt`，並限制權限：
   ```bash
   chmod 600 /root/v6/cookies.txt
   ```
6. 在 `.env` 加上：
   ```
   YTDLP_COOKIES=/root/v6/cookies.txt
   ```
7. 重啟：`systemctl restart musicbot`（若只是覆寫主檔、程式已在跑，下一次解析就會改用新檔，不必重啟）。

匯出的檔案應含 `LOGIN_INFO` 或 `SID`（或 `__Secure-1PSID`／`SAPISID`），才像已登入的工作階段。啟動時與狀態面板會在主檔缺少這些名稱時發出警告。

yt-dlp 結束時會把 cookiejar 寫回它使用的檔案。為避免把 YouTube 輪替後較弱的 cookies 覆寫掉你匯出的主檔，機器人會先複製到暫存檔再交給 yt-dlp，**永遠不寫回主檔**。你只要覆寫 `YTDLP_COOKIES` 指定的那個檔即可（mtime 變了會自動再複製）。

本機（Windows）執行也可以改用直接讀瀏覽器：`YTDLP_COOKIES_FROM_BROWSER=firefox`（或 `chrome`、`edge`、`firefox:設定檔名稱`）。
Chrome / Edge 執行中時 cookies 資料庫可能被鎖住或加密而讀不到，Firefox 最穩定；伺服器請用 `YTDLP_COOKIES` 檔案。

進階（選用）：`YTDLP_PLAYER_CLIENT=default,mweb` 可指定 yt-dlp 嘗試的 YouTube player client 順序；不設定就用 yt-dlp 預設（建議先不設）。

### 風險與注意

- cookies 等同登入狀態：**任何拿到 cookies.txt 的人都能用該帳號**。絕不要上傳到 GitHub、Discord 或貼給別人（`.gitignore` 已排除 `cookies*.txt`）。
- YouTube 可能對大量自動存取的帳號做限制或停權，所以務必用拋棄式帳號。
- cookies 會過期（數天到數週）；log 若出現「已設定 cookies，但仍被擋」就重新匯出一次。
- 也記得定期更新 yt-dlp（`deploy/update-ytdlp.sh` 或 `pip install -U yt-dlp`）。

## 音量平衡與淡入淡出

- **音量平衡**（預設開啟）：ffmpeg `dynaudnorm` 動態音量平衡，讓不同影片音量差不多。CPU 成本約為舊版 `loudnorm` 的 1/9。
  - `.env`：`AUDIO_NORMALIZE=0` 可改成預設關閉
  - 每個伺服器可各自切換：網頁播放卡片的「音量平衡」開關（播放中切換會從目前位置立即生效，不切歌）
- 每首歌開頭 0.25 秒淡入、跳轉後 0.05 秒；跳過 / 停止時 0.12 秒淡出再切換（按下去馬上聽得到變化；音量滑桿與進度時間不受影響）。
- 音量平衡用 `dynaudnorm` 的 `b=1`（開頭直接套用增益），換歌 / 跳轉後約 0.08 秒就到正常音量（舊設定要約 1.5 秒）。

## 播放紀錄與排行

- 每首歌播放超過 **30 秒或一半長度**（取較短者）才記錄一次，存在 `data/history.db`（SQLite，背景執行緒寫入，不影響播放）。
  - 可用 `.env` 的 `HISTORY_DB=/path/history.db` 改位置
- 網頁：右側分頁「播放紀錄」→「最近」/「排行」（7 天 / 30 天 / 全部），每列的 ＋ 可一鍵加回佇列。

## 操作紀錄

- 記三種操作：**點歌**（加進佇列）、**跳過**、**刪歌**（從佇列移除），每筆有誰、歌名、來源（Discord / 網頁面板 / 擴充功能）、台北時間。
- 一次加很多首（匯入歌單、YouTube 播放清單、整份歌單播放）只記一筆「等 N 首」。清空佇列、停止、自動推薦接歌不記。
- 每個伺服器各一份，存在 `data/action_log.json`，只保留 **7 天**（寫入時和啟動時自動清掉），每個伺服器最多 2000 筆。
- 網頁：展開播放畫面 → 「操作紀錄」分頁（可篩選點歌 / 跳過 / 刪歌），有金鑰登入的人都看得到。

## Discord 指令

只保留 10 個常用指令：`/play`、`/import`、`/skip`、`/stop`（清空佇列、留在語音）、`/leave`、`/queue`、`/volume`、`/web`、`/join`、`/panel`。另外還有 `/mylist` 指令群組（`show`／`play`／`add`／`import`）管理個人歌單。
暫停 / 繼續、跳轉、移除 / 排序佇列、最愛、歌單、循環、自動推薦、音量平衡、播放紀錄 / 排行、休眠後恢復，都在網頁面板（`/web`）或 Discord 控制面板按鈕（`/panel`）。
指令在每次啟動時自動同步到伺服器，並清掉 Discord 上殘留的全域指令，不會重複出現。

## 測試（改完程式先跑這個）

```bash
python tools/check.py           # 單元測試 + 模擬器 API 煙霧測試 + 面板 UI 煙霧測試（約 4 分鐘）
python tools/check.py --no-ui   # 不跑 UI（快）
python -m unittest discover -s tests   # 只跑單元測試（UI 測試預設略過；RUN_UI_SMOKE=1 才跑）
```

- UI 煙霧測試用 Playwright（深色/淺色 × 1440/390 寬，會先登入）；沒裝會自動略過：
  `pip install playwright && python -m playwright install chromium`（伺服器記憶體小，不要裝在伺服器上）。
- 模擬器（`tools/sim.py`）的資料放在暫存資料夾，不會動到專案的 `data/`、`list.txt`、`favorites/`；截圖在 `smoke_out/`。
