# 样例数据

`day1.csv` / `day2.csv`：2024-05-01 与 2024-05-02 两个连续 UTC 日的闸机记录。
`correction.csv`：对 `E2`（跨午夜出站）的修订号 1 更正，可用来观察两日同时重算。

验证隔离：先上传 `day1.csv`，页面“隔离测试卡”输入 `CARD-1`（或
`curl -X POST localhost:8000/api/quarantine -H 'Content-Type: application/json' -d '{"card_id":"CARD-1"}'`）。
2024-05-01 的“西直门 → 东直门”行程与 2024-05-02 的相关记录应立即消失；
再次上传 `correction.csv` 或任何含 `E1`/`E2` 的文件会收到整批 400。

无 Docker 时本地运行：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
# 浏览器打开 http://127.0.0.1:8000
```
