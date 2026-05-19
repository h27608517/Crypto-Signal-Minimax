# Crypto Daily Auto Report

一个用 GitHub Actions + MiniMax M2.7 自动生成币圈日报的小工具。

它每天定时抓取行情和新闻 RSS，交给 MiniMax M2.7 总结、分类，再生成静态 HTML，并自动发布到 GitHub Pages。

## 你会得到什么

- 每天自动生成 `public/reports/crypto-YYYY-MM-DD.html`
- 自动更新首页 `public/index.html`
- GitHub Actions 定时运行，不用服务器
- 支持手动运行
- 没有 MiniMax key 时也能生成一个基础版页面，方便先测试

## 快速开始

1. 新建一个 GitHub 仓库，把这些文件推上去。
2. 在仓库设置里开启 Pages：
   - `Settings` -> `Pages`
   - `Build and deployment` 选择 `GitHub Actions`
3. 配置 MiniMax API Key：
   - `Settings` -> `Secrets and variables` -> `Actions`
   - 新增 secret：`MINIMAX_API_KEY`
4. 到 `Actions` 页面手动运行一次 `Daily Crypto Report`。

生成后访问：

```text
https://你的用户名.github.io/你的仓库名/
```

## 本地运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python src/generate_daily.py
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python src\generate_daily.py
```

## 可配置项

环境变量：

- `MINIMAX_API_KEY`：MiniMax API key
- `MINIMAX_MODEL`：MiniMax 模型名，默认 `MiniMax-M2.7`
- `MINIMAX_BASE_URL`：MiniMax OpenAI 兼容接口地址，默认 `https://api.minimax.io/v1`
- `REPORT_DATE`：指定生成日期，例如 `2026-04-07`
- `MAX_ITEMS_PER_FEED`：每个 RSS 源最多取多少条，默认 `8`

新闻源在 [src/sources.json](src/sources.json) 里，可以自己增删。

## 注意

GitHub Actions 的定时时间是 UTC。当前配置是北京时间每天早上 8 点运行，也就是 UTC 00:00。
