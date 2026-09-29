# 图书馆自动约座

郑大图书馆空间预约（<http://order.lib.zzu.edu.cn/h5/index.html>），每天北京时间 7:30 放次日的位子。
脚本提前一分钟登录好，卡着 7:30:00.000 提交三楼中 006，抢不到就换备选、小量重试，结果推到手机。

## 第一步：在 VPS 上探路

先看这台机器在校外够不够得到学校系统，顺便把它的接口摸出来。**只读，不会预约任何东西。**

```bash
sudo apt update && sudo apt install -y git python3-requests
cd ~ && git clone -b claude/claude-flufut https://github.com/shuhan200603-star/alcove.page seat-tool
cd seat-tool/seat && python3 probe.py
```

把打印出来的东西发我，我照着填 `book.py` 里的 `login()` 和 `submit()`。

## 第二步：填配置

```bash
cp config.example.toml config.toml
vi config.toml     # 学号、密码、座位 id
```

`config.toml` 存明文密码，已经在 `.gitignore` 里，不会进仓库。

## 第三步：试一次

```bash
python3 book.py --now --dry-run   # 只登录，不提交
python3 book.py --now             # 真的约一次，验证整条链路
```

## 第四步：挂上定时

VPS 时区多半是 UTC，先一劳永逸改掉：

```bash
sudo timedatectl set-timezone Asia/Shanghai
date          # 确认是北京时间
crontab -e
```

```cron
28 7 * * * cd ~/seat-tool/seat && mkdir -p log && /usr/bin/python3 book.py >> log/cron.log 2>&1
```

（`mkdir -p log` 不能省——重定向是 shell 在 Python 启动前就做的，目录不在会直接报错。）

7:28 起跑，脚本自己登录、等到 7:30:00 再提交。

不想改系统时区的话 cron 写 `28 23 * * *`（UTC）也一样——脚本内部全程按北京时间算，
还会拿服务器的 `Date` 头校准本机时钟偏差，整点不会差半拍。

## 文件

| 文件 | 干什么的 |
|---|---|
| `probe.py` | 探路：测连通性、摸接口。只读 |
| `book.py` | 正主：定点抢座 |
| `config.example.toml` | 配置模板 |
| `log/` | 每天一份日志，不进仓库 |
