# 晚安故事 · 音色睡前故事

晚安故事是知序工作台的生活模块，入口 `/bedtime.html`，复用现有账号、空间和同源 HTTPS。故事正文保存在独立数据对象里；切换朗读音色不会重新搜索或重新加载故事。

工作台入口在“生活”首页、生活专区侧栏及生活模块库中。PC 使用故事列表／阅读区／常驻播放器三栏，手机使用竖屏布局。返回工作台链接为 `/?area=life`。通用上传链接 `/bedtime.html?panel=voices` 可从播放器或音色窗口复制；链接没有账号或空间标识，首次登录后返回上传窗口，优先进入当前账号自己的可编辑空间。指定 `?space=` 的入口必须是当前账号已有权限的空间。记住的空间以 `zhixu:space:<userId>` 保存，不使用旧全局空间键。

云服务未配置时，录音仅在本机解码为 WAV 并试听，不会提交或标记为克隆音色。关闭窗口、换文件、切空间、退出及离页会停止试听并撤销本地预览地址。启用服务后，用户确认授权并创建音色；只有服务器真正返回就绪档案才自动选择该音色，保留原故事正文，点击播放调用云端合成。

当前无需外部账号即可使用 8 篇完整原创故事、关键词与分类搜索、设备系统朗读、私有收藏、私有播放记录，以及前端睡眠定时、音量渐弱与合成白噪音。设备朗读音色和后台播放能力由 Android/iOS 的浏览器提供，锁屏、切换应用或浏览器暂停时不能保证持续播放；网页音量渐弱不等同于可控制系统朗读音量。手机页面适配并不代表已制作原生 Android/iOS 安装包。

云端音色克隆、云端多音色朗读和联网检索实现了真实接口适配，默认关闭。没有密钥时不会请求云服务，不生成演示克隆音色，不把设备系统朗读称作本人克隆。目前未使用真实密钥进行提供商实测；正式启用需要管理员在服务器私有 `.env` 中配置账号与密钥，确认提供商费用及声音素材的使用授权。

## 服务端配置

仅在明确准备启用云服务时填写。密钥不得写入 Git、浏览器、发布包或共享空间记录。

| 变量 | 用途与默认值 |
| --- | --- |
| `DASHSCOPE_API_KEY` | 阿里云百炼密钥；空值关闭克隆和云端朗读 |
| `STORY_SEARCH_PROVIDER` | `local` 默认原创库；可选 `opensearch` 或 `tavily` |
| `OPENSEARCH_API_KEY` | 阿里云 OpenSearch AI 搜索服务专用密钥，与百炼密钥独立 |
| `OPENSEARCH_ENDPOINT` | 控制台提供的 HTTPS 公网服务 origin，主机须属于 `*.opensearch.aliyuncs.com`；不填路径 |
| `OPENSEARCH_WORKSPACE` | OpenSearch workspace 名称，默认 `default` |
| `TAVILY_API_KEY` | 仅在选用 Tavily 时填写；中国大陆网络可达性未验证 |

配置有效只表示具备调用条件，并不表示提供商验证、免费额度或联网可达性已确认。云服务存在用量费用；应用不会创建任何云付费资源，也不自动填入 API 密钥。

克隆使用官方 `qwen-voice-enrollment` HTTP 接口与 `qwen3-tts-vc-2026-01-22` 模型。浏览器将样本解码并导出为 **24 kHz、单声道、16 位 PCM WAV**；服务器再次检查实际 10–30 秒时长、完整帧数据、非空声音，最大 1.5 MB、JSON 上限 2.5 MB。素材通过受认证的服务端 Base64 请求发给百炼，不创建公开样本链接，也不持久保存原始克隆素材。用户必须确认声音为本人或已获授权。一次克隆请求只有提供商实际返回音色 ID 才标为 `ready`，失败保持 `failed` 并可删除。模型 ID 与供应商音色 ID 仅服务端保存，每个空间每个账号最多 10 个档案。

云端系统音色使用 `qwen3-tts-flash`，支持小婉 `Seren`、苏瑶 `Serena`、晨煦 `Ethan`、徐大爷 `Arthur`、沙小弥 `Mochi`、芊悦 `Cherry`。同一个克隆音色必须使用创建时的目标模型，不能跨模型调用。单次合成最多 600 字，前端按句分段顺序播放；语速通过播放器的 `playbackRate` 调整，不向提供商传不存在的速度参数。切换音色会取消旧的前端播放结果，保留已选文本；云端生成和网络传输需要时间，不保证零延迟切换。

云声音服务最多 2 个并发请求；每账号克隆最多 5 次/小时、10 次/日；合成最多 60 次/分钟、100 次/日，每次最多 600 字，因此输入最多 60,000 字/日。这些是应用防滥用上限，**不是云供应商免费额度**。供应商错误响应、签名 URL、密钥和样本不会返回浏览器。服务端仅下载官方结果 OSS 主机下的音频，拒绝跨主机重定向；文档中的 HTTP 结果链接只对该白名单主机升级 HTTPS。音频缓存 1 小时，GET 始终校验用户及空间，禁止共享成员读取他人的合成音频。

删除已经就绪的音色会先请求提供商删除音色，成功后删除本地档案。若密钥缺失或供应商删除失败，保留本地档案并提示重试，避免界面声称云端已删除。创建过程中退出账号或被移除空间时，结果不会保存或返回，后台会尽力清理刚生成的供应商音色。网络失败时仍可能需要管理员到提供商控制台核对残留音色；百炼规则可能在一年未使用后自动清理音色。

## 文本检索与离线边界

原创库为本项目自行创作，来源明示为“知序原创故事”。联网检索直接调用受配置的 OpenSearch 或固定 Tavily API，不请求用户提交的网页 URL，不允许任意内网目标或供应商端点重定向。搜索服务返回的正文可能不完整、包含网页附加内容，且不保证每个结果都有正文；摘要不等同于全文。结果保持原始来源链接与版权说明、`isExcerpt: true`，正文为空时不得直接当完整故事朗读。网页内容版权仍归原作者，保存、复制和离线下载前应确认使用权。

收藏与播放记录按 `space_id + user_id` 隔离；共享空间的其他成员看不到这些私人记录、克隆音色或音频。`viewer` 可阅读原创故事及自己的已有记录，不能修改收藏、历史、音色或调用云合成。每账号每空间最多 200 篇收藏，最近 200 条不同故事播放记录；收藏可主动保存网页检索文本或自有文本。用户可主动把选中的故事正文保存到当前设备缓存，也可下载为 TXT 文件；缓存按账号与空间分隔，在退出账号或检测到权限撤销时清理。浏览器不后台缓存私密 API、账号凭据、密码箱或音频。已打开的页面可读取主动保存的正文，但当前不保证断网后重新打开完整应用或重新验证登录。

## 接口约定

所有接口以 `/api/spaces/:spaceId/bedtime/` 开头，需现有会话及空间成员身份。写入要求 owner/editor，现有 `X-Requested-With: Workspace` 和 Origin 检查统一保护。所有 JSON 响应 `Cache-Control: no-store`。

| 方法与路径 | 结果或输入 |
| --- | --- |
| `GET config` | 实际能力开关，系统设备朗读、联网搜索、克隆、云合成；不含密钥 |
| `GET search?q=&category=&scope=local\|web` | `{stories,scope,query}`；正文、分类、标签、来源、阅读分钟；`web` 无配置返回 503 |
| `GET stories/:storyId` | `{story}`，原创或当前用户已收藏故事 |
| `GET favorites` | `{favorites}`，包含完整保存正文与 `favorited_at` |
| `PUT favorites/:storyId` | 原创可传 `{}`；导入需 `{story:{title,text,category,source}}` |
| `DELETE favorites/:storyId` | 删除当前用户收藏 |
| `GET history` | `{history}`，含文本快照、`voiceId`、`positionSeconds`、`playedAt` |
| `POST history` | `{storyId,voiceId,positionSeconds,story?}`；未收藏导入故事需带 `story` 正文 |
| `DELETE history` | 清除当前用户记录 |
| `GET voices` | `{voices,systemVoices}`；仅返回当前用户克隆档案和可用系统云音色 |
| `POST voices` | `{name,mimeType:"audio/wav",audioBase64,consent:true}`；成功返回 `{voice}` 与 201 |
| `DELETE voices/:voiceId` | 删除本人档案与供应商音色 |
| `POST synthesize` | `{text,voiceId}`；最多 600 字；返回同源私有 `{audioUrl,mimeType,expiresIn}` |
| `GET audio/:audioId` | 仅本人可读的 WAV；1 小时到期后需重新生成 |

## 持久化、迁移与验证

`bedtime.SCHEMA` 只新增 `bedtime_favorites`、`bedtime_history`、`bedtime_voices`、`bedtime_audio` 和索引，不修改既有表。用户或空间删除时新表通过外键级联删除。数据库和 `data/bedtime-audio` 必须随现有持久化数据卷备份；旧程序可以忽略新增表，应用回退兼容，但供应商已经执行的调用和计费不随镜像回退。音频与普通媒体共享容量限制和 100 MiB 磁盘余量检查。合成缓存按请求及启动清理到期条目，不能以此代替正式备份策略。

`python -m unittest discover -s tests -v` 包含真实 HTTP 权限及隔离测试，云服务使用显式注入的假提供商验证协议和授权竞态，不会触发真实计费。覆盖共享成员隔离、只读角色、实际样本时长和 30 秒边界、大 Base64、音色切换保持文本、供应商 ID 不外泄、合成音频鉴权、退出登录/成员移除期间的结果丢弃、失败不假报完成、并发限制、磁盘余量、固定音频主机与重定向拒绝、官方裸 PCM 包装为 WAV。通过本地测试不代表已经公网部署或云供应商实测。

官方资料：[音色复刻](https://help.aliyun.com/zh/model-studio/voice-cloning-user-guide)、[Qwen-TTS HTTP API](https://help.aliyun.com/zh/model-studio/qwen-tts-api)、[音色复刻 HTTP API](https://help.aliyun.com/zh/model-studio/voice-clone-design-http-api)、[系统音色列表](https://help.aliyun.com/zh/model-studio/qwen-tts-voice-list)、[OpenSearch 网页搜索](https://help.aliyun.com/zh/open-search/search-platform/developer-reference/web-search)、[OpenSearch 公网服务地址](https://help.aliyun.com/zh/open-search/search-platform/user-guide/get-service-call-address)。
