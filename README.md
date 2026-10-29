# 双店月度经营报告 · 本地 / 在线

金力 × 江升 双店经营月度对比：上传 Excel → 自动汇总 → 网页报告。

## 本地使用

1. 双击 `启动.bat`（首次自动建虚拟环境并装依赖）
2. 浏览器打开 http://127.0.0.1:8787
3. 登录：
   - 管理员 `admin` / `admin123`
   - 只读 `viewer` / `view123`
4. 后台可「一键导入现有文件夹」或按月上传 Excel

### 每月上传

| 文件 | 门店 |
|------|------|
| 商品零售汇总 | 金力 / 江升 |
| 历史进货查询 | 金力 / 江升 |
| 自取柜出库（如有） | 金力 |

选择正确的 `YYYY-MM` 后上传。同月同类型会覆盖。

---

## 在线部署（Render 免费 + Neon 免费库）

适合公网访问。注意：免费实例闲置 15 分钟会**休眠**，下次打开可能要等几十秒（保活方法见下方「四、让线上不休眠」）；服务器在国外，国内可能偏慢。

### 一、准备 Neon 数据库

1. 打开 https://neon.tech 注册并创建项目  
2. 在 Dashboard 复制 **Connection string**（`DATABASE_URL`，需带 SSL，一般是 `postgresql://...?sslmode=require`）

### 二、准备 GitHub 仓库

1. 在 GitHub 新建空仓库（不要勾选自动加 README）  
2. 在本项目目录执行（把 `你的用户名/仓库名` 换成你的）：

```bash
git init
git add .
git commit -m "Deploy dual-store report app"
git branch -M main
git remote add origin https://github.com/你的用户名/仓库名.git
git push -u origin main
```

### 三、Render 部署

1. 打开 https://render.com 注册，用 GitHub 登录  
2. **New → Web Service**，选中该仓库  
3. 设置：
   - Runtime: Python  
   - Build Command: `pip install -r requirements.txt`  
   - Start Command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`  
4. Environment 添加：
   - `DATABASE_URL` = Neon 连接串  
   - `SESSION_SECRET` = 一长串随机字符（可点 Generate）  
   - （建议）`ADMIN_PASSWORD` / `VIEWER_PASSWORD` = 你自己的密码  
5. 创建后等待 Deploy 成功，打开 `https://xxx.onrender.com`

首次启动会自动建表，并导入仓库里的 `app/data/seed_data.json`（当前 5–9 月数据）。用 `admin` + 你设的密码登录即可。

### 四、以后更新代码

本地改完 → `git push` → Render 自动重新部署。业务数据在 Neon，不会丢。

---

## 让线上不休眠（免费）

`/health` 接口只返回 `{"ok":true}`，不查数据库，专门给监控站用。

1. 打开 https://cron-job.org （或 https://uptimerobot.com），免费注册
2. 新建一个 HTTP(S) 监控：
   - URL：`https://你的域名.onrender.com/health`
   - 间隔：每 **10 分钟**（cron-job.org 最低 1 分钟，设 10 分钟足够）
   - 方法：GET
3. 保存后，Render 因有外部请求就不会进入休眠

注意事项：

- Render 免费计划每月 **750 小时**，一个月约 730 小时，24 小时跑一个月刚好用满；如果同期还有别的免费服务，可能月中就被暂停到月底。被暂停时到 Render 面板手动 Resume 即可。
- Neon 免费库 5 分钟无访问会自动挂起，下次登录时**秒级唤醒**，不影响使用。
- 想彻底稳定就升级 Render 到 Starter（$7/月），无需探活。

---

## 说明

- 本地数据：`app/data/app.db`  
- 云端数据：Neon Postgres（`DATABASE_URL`）  
- 上传的 Excel 解析后写入数据库；云端不依赖本地 `金力/`、`江升/` 文件夹  
- 请尽快修改默认密码
