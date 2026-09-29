# 图书馆自动约座

郑大图书馆空间预约，每天北京时间 7:30 放次日的位子。这个脚本提前一分钟登录好，
卡着 7:30:00.000 提交，抢不到就小量重试，结果推到手机。

## 还缺什么

`book.py` 里 `login()` 和 `submit()` 两个函数是空的 —— 学校系统的真实接口得抓包才知道。
其余部分（时钟校准、定点、重试、通知、日志）都写好了。

抓包步骤，电脑浏览器上做：

1. 打开约座页面，F12 → Network，勾上 Preserve log
2. 正常登录一次 → 在请求列表里找那条登录的 POST → 右键 → Copy → **Copy as cURL**
3. 正常约一个位子 → 同样复制提交那条请求的 cURL
4. 顺便在座位列表的请求里看一眼座位 id 长什么样（可能叫 dev_id / seat_id / room_id）

把这两条 cURL 发我，密码和 Cookie 那几段用 `xxx` 涂掉，我只要字段名和结构。

## 装在 VPS 上

```bash
cd /opt && git clone https://github.com/shuhan200603-star/alcove.page seat-tool
cd seat-tool/seat
pip3 install requests
cp config.example.toml config.toml && vi config.toml   # 填账号和座位
```

先试跑，确认这台机器能连上学校的系统：

```bash
python3 book.py --now --dry-run
```

成了再挂定时。**VPS 时区多半是 UTC**，先看一眼 `date`：

```bash
timedatectl set-timezone Asia/Shanghai   # 一劳永逸，之后 cron 直接按北京时间写
crontab -e
```

```cron
28 7 * * * cd /opt/seat-tool/seat && /usr/bin/python3 book.py >> log/cron.log 2>&1
```

7:28 起跑，脚本自己会登录、等到 7:30:00 再提交。

不想改系统时区的话，cron 写 `28 23 * * *`（UTC），效果一样 —— 脚本内部全程按北京时间算，
还会拿服务器的 `Date` 头校准本机时钟偏差。

## 注意

`config.toml` 里有账号密码，已经在 `.gitignore` 里，别提交上去。
