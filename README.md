# 恋屿 Love Moments

恋屿是一个面向两个人的自托管生活空间，包含聊天、共同回忆、旅行规划、菜谱与采购、纪念日、愿望和轻量管理能力。

本仓库是经过脱敏的公开源码版本。它不包含任何真实账号、生产密码、聊天记录、照片、数据库、备份、API 密钥或线上实例地址。克隆后创建的是独立的新实例，不会连接项目维护者的私人服务。

## 主要功能

- 双人互动：秘密决策、约会方案、心愿私密认领、限时状态卡、表达辅助和应用内通知。
- 共同生活：一周菜单、冰箱库存、购物清单、分步烹饪、计时器与食材替代建议。
- 旅行规划：双方偏好协商、时间真实性检查、行李清单、离线应急卡、共同支出与 AI 行程调整。
- 回忆记录：关系搜索、回忆补全、语音回忆、未来信箱和受权限保护的纪念册 PDF 导出。
- 数据安全：内容可见性、AI 授权记录、来源标记、离线草稿、版本冲突、备份与媒体完整性检查。

所有写入操作都进入当前克隆实例自己的数据库；AI、地图、报价和云存储需使用部署者自己的账号和密钥。

## 一、本地启动

需要 Python 3.12；构建前端或运行浏览器测试时还需要 Node.js 与 pnpm。

```bash
cp .env.example .env
python3 -m pip install -r requirements.txt
python3 main.py
```

默认访问地址为 `http://127.0.0.1:18765/`。

所有网页接口默认访问当前同源后端，不包含维护者的线上地址，也不会自动连接其他恋屿实例。

## 二、创建两个人的账号

1. 确认 `.env` 使用：

   ```ini
   DEMO_MODE=false
   REGISTRATION_MODE=open
   ADMIN_USERNAMES=
   PUBLIC_BASE_URL=
   ```

2. 启动服务后，第一个人打开登录页，选择注册并创建自己的账号。
3. 第一个人在绑定页面生成一次性邀请码。
4. 第二个人注册另一个账号，输入邀请码完成情侣绑定。
5. 两边确认首页显示的昵称和情侣关系正确。
6. 立即把 `.env` 改回 `REGISTRATION_MODE=closed` 并重启，防止陌生人在公网注册。

账号密码只以不可逆哈希保存在克隆者自己的数据库中。公开仓库不会收集、同步或上传注册信息。

## 三、可选演示账号

`DEMO_MODE` 默认关闭。只有主动设置 `DEMO_MODE=true` 时，程序才会在克隆者自己的本地数据库中创建虚构演示数据：

- `test` / `test123`
- `test1` / `test123`

演示账号仅用于本机体验和自动化测试，不会连接维护者的服务器，也不应在公网正式部署中启用。正式注册会拒绝 `test123` 这类弱密码。

## 四、配置后台管理员

后台没有单独的默认账号。先正常注册一个账号，再将其用户名写入本机 `.env`：

```ini
ADMIN_USERNAMES=your_username
```

重启后使用该普通账号登录，再打开 `/admin`。多个管理员用英文逗号分隔。不要把生产管理员、密码或真实地址写入 `.env.example` 或提交到 Git。

演示模式下如需查看后台，可临时设置 `ADMIN_USERNAMES=test`；正式使用前必须关闭演示模式并改成自己的账号。

## 五、需要准备哪些外部 API

**不配置任何第三方 API 也能启动。** 账号注册、情侣绑定、文字聊天、回忆、纪念日、清单、本地菜谱、本地媒体和后台都使用自己的 Python/SQLite 服务。外部 API 只负责智能规划、地图、实时报价、OCR、云存储或语音增强。

所有密钥都只填在服务器的 `.env`，不得写入网页、README、Issue、截图或 GitHub Secrets 以外的公开位置。各平台的免费额度和实名/付款要求可能调整，请以申请当天的控制台为准，并把 `.env` 中的本地调用上限设置得比平台额度更低。

### 推荐先配置

| 能力 | 去哪里申请 | 需要填写 | 不配置时 |
| --- | --- | --- | --- |
| AI 旅行、约会、智能界面、生活助理和菜谱中文整理 | [DeepSeek 开放平台与 API 文档](https://api-docs.deepseek.com/api/deepseek-api/)；也可换成其他 OpenAI-compatible 服务 | `LLM_API_KEY`，必要时修改 `LLM_BASE_URL`、`LLM_MODEL` | 使用本地规则和安全模板，不会冒充真实 AI 结果 |
| 国内地点、餐厅、路线和天气 | [高德 Web 服务 Key 申请](https://lbs.amap.com/api/webservice/create-project-and-key)，服务平台必须选“Web 服务” | `AMAP_WEB_KEY` | 可创建和手工编辑旅行，但国内真实地点、附近餐厅及路线会降级 |

最小智能配置示例：

```ini
LLM_API_KEY=your_deepseek_api_key
AMAP_WEB_KEY=your_amap_web_service_key
```

### 按需要选择

| 能力 | 去哪里申请 | 需要填写 | 说明 |
| --- | --- | --- | --- |
| 海外地点与路线 | [Geoapify 项目与 API Key](https://myprojects.geoapify.com/) | `GEOAPIFY_ENABLED=true`、`GEOAPIFY_API_KEY` | 只做国内旅行可不配 |
| 酒店、航班和公开网页结果查询 | [SerpApi API Key](https://serpapi.com/manage-api-key) | `SERPAPI_ENABLED=true`、`SERPAPI_API_KEY` | 有月度调用额度；应用内有缓存和硬上限，不负责下单 |
| 国内火车票查询 | [聚合数据注册与控制台](https://www.juhe.cn/register)，申请 12306/火车票站到站接口后在“我的数据”复制 AppKey | `JUHE_TRAIN_ENABLED=true`、`JUHE_TRAIN_API_KEY` | 只查询车次、票价和余票，购买跳转第三方平台 |
| 聊天截图 OCR | [阿里云文字识别 OCR](https://help.aliyun.com/zh/ocr/)；开通“统一识别”并给 RAM 用户最小权限 | `ALIBABA_CLOUD_ACCESS_KEY_ID`、`ALIBABA_CLOUD_ACCESS_KEY_SECRET` | 未配置时只支持手工粘贴/校对文字或明确标记的 mock |
| 一对一语音（推荐方案） | [声网 RTC 开通与创建项目](https://doc.shengwang.cn/doc/rtc/javascript/get-started/enable-service) | `RTC_PROVIDER=shengwang`、`SHENGWANG_APP_ID`、`SHENGWANG_APP_CERTIFICATE` | 证书只能留在服务端；是否有免费额度以声网控制台为准 |
| 原生 WebRTC 跨网络中继 | [Cloudflare Realtime TURN](https://developers.cloudflare.com/realtime/turn/generate-credentials/) | `RTC_PROVIDER=webrtc`、`CLOUDFLARE_TURN_KEY_ID`、`CLOUDFLARE_TURN_API_TOKEN` | 不配 TURN 仍会尝试免费 STUN/P2P，但复杂网络下不保证接通；与声网二选一即可 |
| 七牛云媒体双副本 | [七牛云 Kodo 密钥与空间说明](https://developer.qiniu.com/kodo/7450/the-third-party-plug-in-key-parameters) | `MEDIA_STORAGE_PROVIDER=qiniu`、`QINIU_ACCESS_KEY`、`QINIU_SECRET_KEY`、`QINIU_BUCKET`、Region、Endpoint | 建议创建私有空间；本地磁盘仍保留一份 |
| Supabase Storage | [Supabase Storage Quickstart](https://supabase.com/docs/guides/storage/quickstart) 与 [API Key 说明](https://supabase.com/docs/guides/getting-started/api-keys) | `MEDIA_STORAGE_PROVIDER=supabase`、`SUPABASE_URL`、`SUPABASE_ANON_KEY`、`SUPABASE_BUCKET` | 必须先建 Bucket 并配置 RLS；不要填写 `service_role`/secret key |
| 中文在线菜谱 | [天聚数行菜谱查询](https://www.tianapi.com/apiview/23) | `TIANAPI_RECIPE_ENABLED=true`、`TIANAPI_RECIPE_KEY` | 本仓库已有本地中文菜谱，不配也能正常点菜和采购 |
| 海外菜谱补充 | [Spoonacular API Console](https://spoonacular.com/food-api/console) | `SPOONACULAR_ENABLED=true`、`SPOONACULAR_API_KEY` | 可选；结果会转为中文草稿并经过用量/过敏原校验 |

### 无需申请 Key 的能力

- Open-Meteo 天气：保持 `OPEN_METEO_ENABLED=true` 即可。
- TheMealDB：保持 `THEMEALDB_ENABLED=true`，开发和个人使用走其公开接口；公开发布前请重新核对[官方使用条款](https://themealdb.com/terms_of_use.php)。
- iPhone/iPad 主屏幕 Web Push：无需 Firebase 或 Apple 开发者账号；程序首次启动会在私有媒体目录自动生成 VAPID 私钥。必须使用 HTTPS，并在设备上主动开启通知。
- 本地 SQLite、照片存储、备份和 STUN 点对点尝试：无需外部账号。

`BOOKING_API_URL` / `BOOKING_API_KEY` 是预留给已获授权的统一报价服务的自定义适配器，普通自用用户不需要填写；不要把携程、飞猪、去哪儿网页地址或未授权爬虫地址填到这里。

## 六、数据隔离

- 真实配置只写入本机或服务器的 `.env`，不要提交。
- SQLite 数据库、媒体、日志和运行状态保存在 `data/`，该目录内容默认不进入 Git。
- 备份保存在 `backups/` 或外部持久磁盘，不进入 Git。
- 生产部署应把数据库、媒体和备份挂载到代码目录之外的持久卷。
- `.docs/` 与 `.planning/` 是本地私有资料，不进入公开仓库。
- CI 会执行仓库安全检查，拒绝数据库、私钥、环境文件和常见密钥格式。

公开仓库与维护者的私有生产实例完全隔离：

- 公开仓库拥有独立 Git 历史和独立远程地址。
- 私有实例的 `.env`、SQLite、媒体和备份不会复制到公开仓库。
- 公开后端默认创建自己的 `data/love_moments.db`。
- 所有第三方 API 均默认未配置，使用者必须填写自己的密钥。
- 删除了针对维护者生产实例的远程验收和个人数据库重置脚本。
- 公开仓库的 CI 不引用私有仓库文件；可选实时接口只读取公开仓库自身配置的 GitHub Secrets。

## 七、测试

```bash
python3 scripts/check_repo_safety.py
python3 -m unittest discover -s tests -v
pnpm install --frozen-lockfile
pnpm typecheck
pnpm build:web
```

浏览器端到端测试使用隔离的临时数据库与虚构演示账号，不读取本机或生产数据。

## 八、部署

Docker、Nginx 和 systemd 示例位于 `deploy/`。部署前必须自行配置 HTTPS、访问控制、强密码、关闭公开注册，并把所有密钥放入服务器环境变量或密钥管理服务。

## 九、安全问题

不要在公开 Issue 中粘贴 API 密钥、数据库、聊天内容或生产地址。安全问题请使用 GitHub 仓库的 Security Advisory 私下报告。

## License

[MIT](LICENSE)
